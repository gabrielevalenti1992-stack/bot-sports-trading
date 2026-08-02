import os
import json
import time
import threading
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from flask import Flask

# =============================================================================
# CONFIGURAZIONE
# =============================================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY")

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, API_FOOTBALL_KEY]):
    raise ValueError("Mancano variabili d'ambiente! Controlla Render env vars.")

CHAT_ID = int(TELEGRAM_CHAT_ID)
API_BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_FOOTBALL_KEY}

# =============================================================================
# STATO IN MEMORIA + PERSISTENZA SILENZIO
# =============================================================================
MATCH_STATE = {}          # fixture_id -> stato partita
SILENCED_FILE = "silenced_matches.json"

def load_silenced():
    if os.path.exists(SILENCED_FILE):
        with open(SILENCED_FILE, 'r') as f:
            return set(json.load(f))
    return set()

def save_silenced(silenced):
    with open(SILENCED_FILE, 'w') as f:
        json.dump(list(silenced), f)

SILENCED_MATCHES = load_silenced()

# =============================================================================
# HEALTH CHECK RENDER (fix 501 HEAD)
# =============================================================================
app_flask = Flask(__name__)

@app_flask.route('/')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    app_flask.run(host='0.0.0.0', port=port)

# =============================================================================
# GRAFICO A BARRE ORIZZONTALI (sostituisce il grafico sintetico)
# =============================================================================
def generate_match_chart(home_name, away_name, stats):
    metrics = list(stats.keys())
    home_vals = [stats[m][0] for m in metrics]
    away_vals = [stats[m][1] for m in metrics]

    fig, ax = plt.subplots(figsize=(5.0, 2.6), dpi=150)
    fig.patch.set_facecolor('#1e1e1e')
    ax.set_facecolor('#1e1e1e')

    color_home = '#22c55e'
    color_away = '#ef4444'
    color_bg = '#2a2a2a'
    color_text = '#e5e5e5'
    color_muted = '#888888'

    for i, metric in enumerate(metrics):
        total = home_vals[i] + away_vals[i]
        ax.barh(i, 1, height=0.30, color=color_bg, left=0, zorder=1, edgecolor='none')

        if total == 0:
            ax.text(0.5, i, 'Nessun dato', ha='center', va='center',
                    fontsize=8, color=color_muted, zorder=3)
            continue

        home_pct = home_vals[i] / total
        away_pct = away_vals[i] / total

        ax.barh(i, home_pct, height=0.30, color=color_home, left=0, zorder=2, edgecolor='none')
        ax.barh(i, away_pct, height=0.30, color=color_away, left=home_pct, zorder=2, edgecolor='none')

        ax.text(-0.04, i, str(home_vals[i]), ha='right', va='center',
                fontsize=11, fontweight='bold', color=color_home, zorder=3)
        ax.text(1.04, i, str(away_vals[i]), ha='left', va='center',
                fontsize=11, fontweight='bold', color=color_away, zorder=3)

    ax.set_yticks(range(len(metrics)))
    ax.set_yticklabels(metrics, fontsize=10, color=color_text)
    ax.set_xlim(-0.18, 1.18)
    ax.set_xticks([])
    for spine in ['top', 'right', 'bottom', 'left']:
        ax.spines[spine].set_visible(False)
    ax.tick_params(left=False, pad=10)
    ax.invert_yaxis()

    home_patch = mpatches.Patch(color=color_home, label=home_name)
    away_patch = mpatches.Patch(color=color_away, label=away_name)
    ax.legend(handles=[home_patch, away_patch], loc='lower center',
              bbox_to_anchor=(0.5, -0.20), ncol=2, frameon=False,
              fontsize=9, labelcolor=color_text)

    plt.tight_layout(rect=[0, 0.06, 1, 1])
    buf = BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight',
                facecolor='#1e1e1e', edgecolor='none', pad_inches=0.1)
    buf.seek(0)
    plt.close()
    return buf

# =============================================================================
# API-FOOTBALL
# =============================================================================
def fetch_live_fixtures():
    url = f"{API_BASE}/fixtures"
    params = {"live": "all"}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        return r.json().get("response", [])
    except Exception as e:
        print(f"Errore fetch live: {e}", flush=True)
        return []

def fetch_fixture_events(fixture_id):
    url = f"{API_BASE}/fixtures/events"
    params = {"fixture": fixture_id}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        return r.json().get("response", [])
    except Exception as e:
        print(f"Errore eventi {fixture_id}: {e}", flush=True)
        return []

def extract_goals(events):
    goals = []
    for ev in events:
        if ev.get("type") == "Goal":
            goals.append({
                "minute": ev["time"]["elapsed"],
                "team": ev["team"]["name"],
                "player": (ev.get("player") or {}).get("name") or "Sconosciuto"
            })
    goals.sort(key=lambda g: g["minute"])
    return goals

def extract_stats(fixture):
    stats_raw = fixture.get("statistics", [])
    if len(stats_raw) < 2:
        return {}

    home_stats = {s["type"]: s["value"] for s in stats_raw[0].get("statistics", [])}
    away_stats = {s["type"]: s["value"] for s in stats_raw[1].get("statistics", [])}

    def get_val(d, key):
        v = d.get(key)
        if v is None or v == "None":
            return 0
        return int(v)

    return {
        "Tiri totali": (
            get_val(home_stats, "Shots on Goal") + get_val(home_stats, "Shots off Goal"),
            get_val(away_stats, "Shots on Goal") + get_val(away_stats, "Shots off Goal")
        ),
        "Tiri in porta": (
            get_val(home_stats, "Shots on Goal"),
            get_val(away_stats, "Shots on Goal")
        ),
        "Corner": (
            get_val(home_stats, "Corner Kicks"),
            get_val(away_stats, "Corner Kicks")
        ),
    }

# =============================================================================
# FILTRI CAMPIONATI
# =============================================================================
EXCLUDED_KEYWORDS = [
    "women", "feminine", "u17", "u18", "u19", "u20",
    "amateur", "friendly", "reserve", "dilettanti",
    "amichevole", "femminile", "riserve", "youth"
]

def is_valid_league(fixture):
    league_name = (fixture.get("league", {}).get("name") or "").lower()
    country = (fixture.get("league", {}).get("country") or "").lower()
    combined = f"{league_name} {country}"
    return not any(k in combined for k in EXCLUDED_KEYWORDS)

# =============================================================================
# REGOLE DI NOTIFICA
# =============================================================================
def check_rules(stats, minute, fixture_id):
    if not stats:
        return None

    home_shots, away_shots = stats.get("Tiri totali", (0, 0))
    total = home_shots + away_shots
    diff = abs(home_shots - away_shots)

    # Regola 1: differenza tiri >= 3
    if diff >= 3:
        return "Regola 1"

    # Regola 2: molto attiva entro il 25'
    if minute <= 25 and total >= 6:
        return "Regola 2"

    # Regola 3: forzata ogni 30 min se tiri >= 4
    if total >= 4:
        last_force = MATCH_STATE.get(fixture_id, {}).get("last_forced_notify", 0)
        if time.time() - last_force >= 1800:
            MATCH_STATE[fixture_id]["last_forced_notify"] = time.time()
            return "Regola 3"

    return None

# =============================================================================
# COSTRUZIONE MESSAGGIO
# =============================================================================
def build_message(home, away, league, minute, status_short, score_home, score_away,
                  stats, goals, is_final=False, trigger_rule=None):
    emoji = "🏁" if is_final else "⚽"
    status_text = "🏁 *FINALE*" if is_final else f"⏱️ {minute}' | {status_short}"

    msg = f"{emoji} *{home}* vs *{away}*\n"
    msg += f"🏆 {league}\n"
    msg += f"{status_text}\n"
    if trigger_rule and not is_final:
        msg += f"🔥 *{trigger_rule}*\n"
    msg += f"\n🔢 *Risultato: {score_home} - {score_away}*\n"

    # === NUOVO: primo e ultimo gol ===
    if goals:
        msg += f"\n🥇 Primo gol: {goals[0]['minute']}' ({goals[0]['player']})\n"
        if len(goals) > 1:
            msg += f"⚡ Ultimo gol: {goals[-1]['minute']}' ({goals[-1]['player']})\n"

    msg += f"\n📊 *Statistiche:*\n"
    for metric, (h, a) in stats.items():
        msg += f"• {metric}: {h} - {a}\n"

    return msg

def get_notification_keyboard(fixture_id):
    if fixture_id in SILENCED_MATCHES:
        return None
    keyboard = [[
        InlineKeyboardButton("🔕 Silenzia questa partita", callback_data=f"mute:{fixture_id}")
    ]]
    return InlineKeyboardMarkup(keyboard)

# =============================================================================
# CALLBACK HANDLER (bottone silenzia)
# =============================================================================
async def button_callback(update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("mute:"):
        fixture_id = int(data.split(":")[1])
        SILENCED_MATCHES.add(fixture_id)
        save_silenced(SILENCED_MATCHES)

        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🔕 Partita silenziata. Non riceverai più alert live. "
                 "Il risultato finale arriverà comunque."
        )

# =============================================================================
# PROCESSA SINGOLA PARTITA
# =============================================================================
async def process_single_match(fixture, context: ContextTypes.DEFAULT_TYPE):
    fixture_id = fixture["fixture"]["id"]
    status_short = fixture["fixture"]["status"]["short"]

    home = fixture["teams"]["home"]["name"]
    away = fixture["teams"]["away"]["name"]
    league = fixture["league"]["name"]
    minute = fixture["fixture"]["status"]["elapsed"] or 0
    score_home = fixture["goals"]["home"] or 0
    score_away = fixture["goals"]["away"] or 0

    # Recupera eventi e gol
    events = fetch_fixture_events(fixture_id)
    goals = extract_goals(events)

    # Stato precedente
    prev = MATCH_STATE.get(fixture_id, {})
    prev_status = prev.get("status")

    # Aggiorna stato
    MATCH_STATE[fixture_id] = {
        "status": status_short,
        "score_home": score_home,
        "score_away": score_away,
        "goals": goals,
        "last_forced_notify": prev.get("last_forced_notify", 0),
    }

    # === NOTIFICA FINALE (sempre, anche se silenziata) ===
    final_statuses = ["FT", "AET", "PEN"]
    if status_short in final_statuses and prev_status not in final_statuses:
        stats = extract_stats(fixture)
        msg = build_message(home, away, league, minute, status_short,
                           score_home, score_away, stats, goals, is_final=True)
        chart = generate_match_chart(home, away, stats)
        await context.bot.send_photo(chat_id=CHAT_ID, photo=chart,
                                      caption=msg, parse_mode="Markdown")

        # Pulizia
        SILENCED_MATCHES.discard(fixture_id)
        save_silenced(SILENCED_MATCHES)
        MATCH_STATE.pop(fixture_id, None)
        return

    # === SALTA SE SILENZIATA ===
    if fixture_id in SILENCED_MATCHES:
        return

    stats = extract_stats(fixture)
    prev_stats = prev.get("last_notified_stats")

    # Anti-spam: non notificare se statistiche identiche
    if prev_stats == stats:
        return

    trigger = check_rules(stats, minute, fixture_id)

    if trigger:
        msg = build_message(home, away, league, minute, status_short,
                           score_home, score_away, stats, goals,
                           is_final=False, trigger_rule=trigger)
        chart = generate_match_chart(home, away, stats)
        keyboard = get_notification_keyboard(fixture_id)

        await context.bot.send_photo(
            chat_id=CHAT_ID,
            photo=chart,
            caption=msg,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        MATCH_STATE[fixture_id]["last_notified_stats"] = stats

# =============================================================================
# CICLO PRINCIPALE
# =============================================================================
async def main_loop(context: ContextTypes.DEFAULT_TYPE):
    fixtures = fetch_live_fixtures()
    valid = [f for f in fixtures if is_valid_league(f)]

    print(f"[{time.strftime('%H:%M')}] Live: {len(fixtures)} | Valide: {len(valid)}", flush=True)

    for fixture in valid:
        try:
            await process_single_match(fixture, context)
        except Exception as e:
            print(f"Errore fixture {fixture['fixture']['id']}: {e}", flush=True)

async def start_cmd(update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot avviato e operativo.")

# =============================================================================
# ENTRYPOINT
# =============================================================================
async def main():
    # Avvia Flask health check in thread separato (fix Render 501)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CallbackQueryHandler(button_callback))

    # Job ogni 3 minuti
    application.job_queue.run_repeating(main_loop, interval=180, first=10)

    # Messaggio di avvio su Telegram
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": "✅ Bot avviato", "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print(f"Errore messaggio avvio: {e}", flush=True)

    print("Bot avviato. Polling in corso...", flush=True)
    await application.run_polling()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
