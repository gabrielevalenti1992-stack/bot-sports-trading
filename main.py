import json
import time
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# =============================================================================
# ============================================================
# ANTI-SPAM: tracker partite già notificate
# Chiave = "matchID_golHome_golAway"
# Se il punteggio cambia, la chiave cambia e ri-notifica
# ============================================================
notified_matches = set()

# SERVER HTTP PER RENDER FREE (fix 501 HEAD)
# =============================================================================
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server, daemon=True)
server_thread.start()

# =============================================================================
# =============================================================================
# CONFIGURAZIONE
# =============================================================================
# --- TOKEN: solo da Environment Variables (mai su GitHub) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY")

CONFIG_VALIDA = all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, API_FOOTBALL_KEY])
print(f"TOKEN presente: {'SI' if TELEGRAM_BOT_TOKEN else 'NO'}", flush=True)
print(f"CHAT_ID presente: {'SI' if TELEGRAM_CHAT_ID else 'NO'}", flush=True)
print(f"API_KEY presente: {'SI' if API_FOOTBALL_KEY else 'NO'}", flush=True)

if not CONFIG_VALIDA:
    print("CONFIGURAZIONE INCOMPLETA - Impossibile avviare il bot", flush=True)

# --- SOGLIE NOTIFICHE: da config.json (opzionale, fallback a valori default) ---
DIFF_TIRI_SOGLIA = 3
TIRI_TOTALI_ATTIVA = 6
MINUTI_ATTIVA = 25
INTERVALLO_FORZATO = 1800

# Soglie Regola MOMENTUM (ultimi 15 min)
MOMENTUM_TIRI_IN_PORTA = 3
MOMENTUM_TIRI_TOTALI = 5
MOMENTUM_CORNER = 4

try:
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    with open(config_path, 'r') as f:
        config = json.load(f)
    DIFF_TIRI_SOGLIA = config.get("diff_tiri_soglia", DIFF_TIRI_SOGLIA)
    TIRI_TOTALI_ATTIVA = config.get("tiri_totali_attiva", TIRI_TOTALI_ATTIVA)
    MINUTI_ATTIVA = config.get("minuti_attiva", MINUTI_ATTIVA)
    INTERVALLO_FORZATO = config.get("intervallo_forzato", INTERVALLO_FORZATO)
    MOMENTUM_TIRI_IN_PORTA = config.get("momentum_tiri_in_porta", MOMENTUM_TIRI_IN_PORTA)
    MOMENTUM_TIRI_TOTALI = config.get("momentum_tiri_totali", MOMENTUM_TIRI_TOTALI)
    MOMENTUM_CORNER = config.get("momentum_corner", MOMENTUM_CORNER)
    # Nuove soglie per trigger gol/punteggio
    MIN_GOALS_TOTAL = config.get("min_goals_total", 2)
    MIN_GOALS_DIFF = config.get("min_goals_diff", 2)
    MIN_TOTAL_SHOTS_LOW_GOALS = config.get("min_total_shots_low_goals", 8)
    MAX_GOALS_FOR_SHOTS_TRIGGER = config.get("max_goals_for_shots_trigger", 1)
    print(f"Soglie caricate: diff={DIFF_TIRI_SOGLIA}, tot={TIRI_TOTALI_ATTIVA}, min={MINUTI_ATTIVA}, int={INTERVALLO_FORZATO}, gol_min={MIN_GOALS_TOTAL}, gol_diff={MIN_GOALS_DIFF}", flush=True)
except Exception as e:
    print(f"Soglie default (config.json non trovato o errore): {e}", flush=True)

PAROLE_ESCLUSE = [
    "women", "femminile", "female", "u20", "u19", "u18", "u17", "u16", "u15",
    "under-20", "under-19", "under-18", "under-17", "under 20", "under 19",
    "under 18", "under 17", "youth", "amateur", "dilettanti", "regional",
    "reserves", "riserve", "friendlies", "amichevoli", "friendly"
]

stato_partite = {}
ciclo_numero = 0

# =============================================================================
# STATO SILENZIATI (dict con score al momento del silenzio)
# =============================================================================
SILENCED_FILE = "silenced_matches.json"

def load_silenced():
    if os.path.exists(SILENCED_FILE):
        with open(SILENCED_FILE, 'r') as f:
            data = json.load(f)
            if isinstance(data, list):
                return {str(fid): {"score_home": 0, "score_away": 0} for fid in data}
            return data
    return {}

def save_silenced(silenced):
    with open(SILENCED_FILE, 'w') as f:
        json.dump(silenced, f)

SILENCED_MATCHES = load_silenced()

# =============================================================================
# STATO PREFERITI
# =============================================================================
FAVORITES_FILE = "favorite_matches.json"

def load_favorites():
    if os.path.exists(FAVORITES_FILE):
        with open(FAVORITES_FILE, 'r') as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(str(x) for x in data)
            return set()
    return set()

def save_favorites(favs):
    with open(FAVORITES_FILE, 'w') as f:
        json.dump(list(favs), f)

FAVORITE_MATCHES = load_favorites()

# =============================================================================
# FIAMME - SOGLIE
# =============================================================================
FIRE_THRESHOLDS = {
    "Tiri totali":    (2, 4),
    "Tiri in porta":  (2, 3),
    "Corner":         (999, 999),
}

def get_fire_suffix(delta):
    low, high = FIRE_THRESHOLDS.get("Tiri totali", (999, 999))
    if delta >= high:
        return "\U0001F525\U0001F525"
    elif delta >= low:
        return "\U0001F525"
    return ""

def get_fire_suffix_shots(delta):
    low, high = FIRE_THRESHOLDS.get("Tiri in porta", (999, 999))
    if delta >= high:
        return "\U0001F525\U0001F525"
    elif delta >= low:
        return "\U0001F525"
    return ""

def get_fire_suffix_corner(delta):
    return ""

# =============================================================================
# THREAD: ASCOLTA CLICK SUI BOTTONI + COMANDI MANUALI
# =============================================================================
def poll_callbacks():
    offset = 0
    while True:
        if not TELEGRAM_BOT_TOKEN:
            time.sleep(10)
            continue
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            r = requests.get(url, params={"offset": offset, "limit": 10}, timeout=10)
            updates = r.json().get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1

                cq = upd.get("callback_query")
                if cq:
                    data = cq.get("data", "")
                    chat_id = cq["message"]["chat"]["id"]
                    msg_id = cq["message"]["message_id"]

                    if data.startswith("mute:"):
                        fid = str(int(data.split(":")[1]))
                        stato = stato_partite.get(int(fid), {})
                        score_h = stato.get("score_home", 0)
                        score_a = stato.get("score_away", 0)
                        minuto = stato.get("last_minute", 0)
                        SILENCED_MATCHES[fid] = {
                            "score_home": score_h,
                            "score_away": score_a,
                            "muted_at_minute": minuto
                        }
                        save_silenced(SILENCED_MATCHES)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                            json={"callback_query_id": cq["id"]}, timeout=5)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
                            json={
                                "chat_id": chat_id,
                                "message_id": msg_id,
                                "reply_markup": json.dumps({"inline_keyboard": []})
                            }, timeout=5)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            json={
                                "chat_id": chat_id,
                                "text": "\U0001F515 Partita silenziata. Non riceverai piu alert live. Il risultato finale arrivera comunque.",
                                "parse_mode": "Markdown"
                            }, timeout=5)

                    elif data.startswith("fav:"):
                        fid = str(int(data.split(":")[1]))
                        if fid in FAVORITE_MATCHES:
                            FAVORITE_MATCHES.discard(fid)
                            text = "Rimossa dai preferiti"
                        else:
                            FAVORITE_MATCHES.add(fid)
                            text = "Aggiunta ai preferiti"
                        save_favorites(FAVORITE_MATCHES)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                            json={"callback_query_id": cq["id"], "text": text}, timeout=5)
                        is_fav = fid in FAVORITE_MATCHES
                        is_sil = fid in SILENCED_MATCHES
                        keyboard = get_notification_keyboard(int(fid), is_fav, is_sil)
                        if keyboard:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
                                json={
                                    "chat_id": chat_id,
                                    "message_id": msg_id,
                                    "reply_markup": json.dumps(keyboard)
                                }, timeout=5)

                msg = upd.get("message")
                if msg and msg.get("text"):
                    text = msg["text"].strip()
                    chat_id = msg["chat"]["id"]
                    parts = text.split()
                    cmd = parts[0].lower()
                    args = parts[1:] if len(parts) > 1 else []

                    if cmd == "/help":
                        help_text = (
                            "Comandi disponibili:\n"
                            "/help - Mostra questo messaggio\n"
                            "/status <squadra> - Info live su una partita\n"
                            "/favorites - Lista partite preferite\n"
                            "/clearfavorites - Svuota lista preferiti\n"
                            "/silenced - Lista partite silenziate\n"
                            "/live - Mostra tutte le partite live"
                        )
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": help_text, "parse_mode": "Markdown"}, timeout=5)

                    elif cmd == "/status":
                        if not args:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": "Usa: /status <nome squadra>", "parse_mode": "Markdown"}, timeout=5)
                            continue
                        query = " ".join(args).lower()
                        partite_cmd = get_partite_live()
                        trovate = []
                        for f in partite_cmd:
                            home = f.get("teams", {}).get("home", {}).get("name", "").lower()
                            away = f.get("teams", {}).get("away", {}).get("name", "").lower()
                            if query in home or query in away:
                                trovate.append(f)
                        if not trovate:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": f"Nessuna partita live trovata per '{query}'", "parse_mode": "Markdown"}, timeout=5)
                        else:
                            for f in trovate:
                                fid = f["fixture"]["id"]
                                home = f["teams"]["home"]["name"]
                                away = f["teams"]["away"]["name"]
                                minuto = f["fixture"]["status"].get("elapsed") or 0
                                score_h = f["goals"]["home"] or 0
                                score_a = f["goals"]["away"] or 0
                                stats = get_statistiche_partita(fid)
                                stats_text = ""
                                if stats and len(stats) >= 2:
                                    sh = stats[0].get("statistics", [])
                                    sa = stats[1].get("statistics", [])
                                    tc = estrai_valore_stat(sh, "Total Shots")
                                    to = estrai_valore_stat(sa, "Total Shots")
                                    tp = estrai_valore_stat(sh, "Shots on Goal")
                                    tpo = estrai_valore_stat(sa, "Shots on Goal")
                                    cc = estrai_valore_stat(sh, "Corner Kicks")
                                    co = estrai_valore_stat(sa, "Corner Kicks")
                                    stats_text = f"\nStats: Tiri {tc}-{to} | Porta {tp}-{tpo} | Corner {cc}-{co}"
                                events = fetch_fixture_events(fid)
                                goals = extract_goals(events)
                                last_text = ""
                                if goals:
                                    last_text = f"\nUltimo gol: {goals[-1]['minute']}' ({goals[-1]['player']})"
                                msg_text = f"{home} vs {away}\n{minuto}' | {score_h}-{score_a}{last_text}{stats_text}"
                                requests.post(
                                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                    json={"chat_id": chat_id, "text": msg_text, "parse_mode": "Markdown"}, timeout=5)

                    elif cmd == "/favorites":
                        if not FAVORITE_MATCHES:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": "Nessuna partita preferita.", "parse_mode": "Markdown"}, timeout=5)
                        else:
                            lines = ["Partite preferite:"]
                            partite_cmd = get_partite_live()
                            live_map = {str(f["fixture"]["id"]): f for f in partite_cmd}
                            for fid in FAVORITE_MATCHES:
                                f = live_map.get(fid)
                                if f:
                                    home = f["teams"]["home"]["name"]
                                    away = f["teams"]["away"]["name"]
                                    minute = f["fixture"]["status"].get("elapsed", "?")
                                    lines.append(f"- {home} vs {away} ({minute}')")
                                else:
                                    lines.append(f"- ID {fid} (non live)")
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "Markdown"}, timeout=5)

                    elif cmd == "/clearfavorites":
                        FAVORITE_MATCHES.clear()
                        save_favorites(FAVORITE_MATCHES)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": "Lista preferiti svuotata.", "parse_mode": "Markdown"}, timeout=5)

                    elif cmd == "/silenced":
                        if not SILENCED_MATCHES:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": "Nessuna partita silenziata.", "parse_mode": "Markdown"}, timeout=5)
                        else:
                            lines = ["Partite silenziate:"]
                            for fid, info in SILENCED_MATCHES.items():
                                lines.append(f"- ID {fid} al {info.get('muted_at_minute','?')}'")
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "Markdown"}, timeout=5)

                    elif cmd == "/live":
                        partite_cmd = get_partite_live()
                        if not partite_cmd:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": "Nessuna partita live trovata al momento.", "parse_mode": "Markdown"}, timeout=5)
                        else:
                            lines = [f"Partite live trovate: {len(partite_cmd)}"]
                            for f in partite_cmd:
                                home = f["teams"]["home"]["name"]
                                away = f["teams"]["away"]["name"]
                                league = f["league"]["name"]
                                minute = f["fixture"]["status"].get("elapsed", "?")
                                score_h = f["goals"]["home"] or 0
                                score_a = f["goals"]["away"] or 0
                                lines.append(f"- {home} {score_h}-{score_a} {away} ({league}, {minute}')")
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": "\n".join(lines[:20]), "parse_mode": "Markdown"}, timeout=5)
        except Exception as e:
            log(f"Errore poll callback: {e}")
        time.sleep(5)

callback_thread = threading.Thread(target=poll_callbacks, daemon=True)
callback_thread.start()

# =============================================================================
# FUNZIONI UTILITY
# =============================================================================
def log(msg):
    print(msg, flush=True)


def invia_messaggio_telegram(testo):
    if not CONFIG_VALIDA:
        log(f"[SKIP Telegram] Config mancante: {testo[:50]}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {'chat_id': TELEGRAM_CHAT_ID, 'text': testo, 'parse_mode': 'Markdown'}
        response = requests.post(url, data=data, timeout=10)
        log(f"Telegram testo - Status: {response.status_code} - {response.text[:100]}")
    except Exception as e:
        log(f"Errore invio testo Telegram: {e}")


def invia_notifica_telegram(foto_path, messaggio, reply_markup=None):
    if not CONFIG_VALIDA:
        log(f"[SKIP Telegram] Config mancante: {messaggio[:50]}")
        return
    try:
        if foto_path and os.path.exists(foto_path):
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            with open(foto_path, 'rb') as photo:
                files = {'photo': photo}
                data = {
                    'chat_id': TELEGRAM_CHAT_ID,
                    'caption': messaggio,
                    'parse_mode': 'Markdown'
                }
                if reply_markup:
                    data['reply_markup'] = json.dumps(reply_markup)
                response = requests.post(url, data=data, files=files, timeout=10)
                log(f"Telegram foto - Status: {response.status_code}")
        else:
            invia_messaggio_telegram(messaggio)
    except Exception as e:
        log(f"Errore invio Telegram: {e}")


def campionato_valido(league_name, league_type):
    nome = league_name.lower()
    for parola in PAROLE_ESCLUSE:
        if parola in nome:
            return False
    if league_type and league_type.lower() not in ["league", "cup", "championship"]:
        return False
    return True


def get_partite_live():
    if not API_FOOTBALL_KEY:
        log("API_FOOTBALL_KEY mancante, skip get_partite_live")
        return []
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"live": "all"}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        log(f"API-Football status: {response.status_code}")
        if response.status_code != 200:
            invia_messaggio_telegram(f"Errore API\nHTTP {response.status_code}")
            return []
        data = response.json()
        errori = data.get("errors", {})
        if errori:
            invia_messaggio_telegram(f"Errore API\n{errori}")
            return []
        return data.get("response", [])
    except Exception as e:
        log(f"Errore get_partite_live: {e}")
        invia_messaggio_telegram(f"Eccezione API\n{e}")
        return []


def get_statistiche_partita(fixture_id):
    if not API_FOOTBALL_KEY:
        return None
    url = "https://v3.football.api-sports.io/fixtures/statistics"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"fixture": fixture_id}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if response.status_code != 200:
            return None
        data = response.json()
       resp = data.get("response", [])
        log(f"API stats {fixture_id}: {resp}")
        return resp

    except Exception as e:
        log(f"Errore statistiche {fixture_id}: {e}")
        return None


def fetch_fixture_events(fixture_id):
    if not API_FOOTBALL_KEY:
        return []
    url = "https://v3.football.api-sports.io/fixtures/events"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"fixture": fixture_id}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        return response.json().get("response", [])
    except Exception as e:
        log(f"Errore eventi {fixture_id}: {e}")
        return []


def extract_goals(events):
    goals = []
    for ev in events:
        if ev.get("type") == "Goal":
            goals.append({
                "minute": ev["time"]["elapsed"],
                "player": (ev.get("player") or {}).get("name") or "Sconosciuto",
                "team": ev["team"]["name"]
            })
    goals.sort(key=lambda g: g["minute"])
    return goals


def estrai_valore_stat(stats_team, nome_stat):
    for stat in stats_team:
        if stat.get("type", "").lower() == nome_stat.lower():
            val = stat.get("value")
            if val is None:
                return 0
            try:
                return int(val)
            except:
                return 0
    return 0


# =============================================================================
# GRAFICO A BARRE ORIZZONTALI (totali cumulativi)
# =============================================================================
def genera_grafico_barre(fixture_id, home_name, away_name, stats):
    try:
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

        foto_path = os.path.join(os.path.dirname(__file__), f'chart_{fixture_id}.png')
        plt.savefig(foto_path, format='png', bbox_inches='tight',
                    facecolor='#1e1e1e', edgecolor='none', pad_inches=0.1)
        plt.close()
        return foto_path
    except Exception as e:
        log(f"Errore grafico barre: {e}")
        return None


# =============================================================================
# TASTIERA INLINE
# =============================================================================
def get_notification_keyboard(fixture_id, is_favorite=False, is_silenced=False):
    if is_silenced:
        return None
    buttons = []
    fav_text = "Rimuovi dai preferiti" if is_favorite else "Aggiungi ai preferiti"
    buttons.append([{"text": fav_text, "callback_data": f"fav:{fixture_id}"}])
    buttons.append([{"text": "Silenzia questa partita", "callback_data": f"mute:{fixture_id}"}])
    return {"inline_keyboard": buttons}


# =============================================================================
# DELTA 15 MINUTI
# =============================================================================
def calcola_delta_15min(fixture_id, current_stats):
    stato = stato_partite.get(fixture_id, {})
    history = stato.get("history", [])
    now = time.time()

    history_15m = [h for h in history if now - h["timestamp"] <= 900]

    if not history_15m or len(history_15m) < 2:
        return {k: (0, 0) for k in current_stats}, False

    old = history_15m[0]
    delta = {}
    for key in current_stats:
        curr_h, curr_a = current_stats[key]
        old_h, old_a = old["stats"].get(key, (0, 0))
        delta[key] = (max(0, curr_h - old_h), max(0, curr_a - old_a))

    return delta, True


# =============================================================================
# REGOLE DI NOTIFICA
# =============================================================================
def deve_notificare(fixture_id, tiri_casa, tiri_ospite, minuto, delta_stats=None):
    stato = stato_partite.get(fixture_id, {})
    ultima_casa = stato.get("tiri_casa", -1)
    ultima_ospite = stato.get("tiri_ospite", -1)
    ultimo_invio = stato.get("timestamp_notifica", 0)

    if tiri_casa == ultima_casa and tiri_ospite == ultima_ospite:
        return False

    # Preferiti: notifica sempre se le stats sono cambiate (bypassa le soglie)
    if str(fixture_id) in FAVORITE_MATCHES:
        return True

    tiri_totali = tiri_casa + tiri_ospite
    diff = abs(tiri_casa - tiri_ospite)
    tempo_passato = time.time() - ultimo_invio

    # Regola 1: Differenza tiri significativa
    if diff >= DIFF_TIRI_SOGLIA:
        return True

    # Regola 2: Partita molto attiva nei primi 25 min
    if minuto <= MINUTI_ATTIVA and tiri_totali >= TIRI_TOTALI_ATTIVA:
        return True

    # Regola 3: Forzata ogni 30 min se abbastanza tiri
    if tempo_passato >= INTERVALLO_FORZATO and tiri_totali >= 4:
        return True

    # Regola 4: MOMENTUM - ritmo recente negli ultimi 15 min
    # Cattura partite che si svegliano nel secondo tempo anche se totali bassi
    if delta_stats:
        d_tiri = delta_stats.get("Tiri totali", (0, 0))
        d_porta = delta_stats.get("Tiri in porta", (0, 0))
        d_corner = delta_stats.get("Corner", (0, 0))
        if (d_porta[0] + d_porta[1]) >= MOMENTUM_TIRI_IN_PORTA:
            return True
        if (d_tiri[0] + d_tiri[1]) >= MOMENTUM_TIRI_TOTALI:
            return True
        if (d_corner[0] + d_corner[1]) >= MOMENTUM_CORNER:
            return True

    return False


# =============================================================================
# PROCESSA SINGOLA PARTITA
# =============================================================================
def processa_partita(fixture):
    try:
        fixture_id = fixture["fixture"]["id"]
        league = fixture.get("league", {})
        league_name = league.get("name", "")
        league_type = league.get("type", "")

        if not campionato_valido(league_name, league_type):
            return

        home = fixture["teams"]["home"]["name"]
        away = fixture["teams"]["away"]["name"]
        score_home = fixture["goals"]["home"] or 0
        score_away = fixture["goals"]["away"] or 0
        minuto = fixture["fixture"]["status"].get("elapsed") or 0
        status_short = fixture["fixture"]["status"].get("short", "LIVE")

        log(f"  {home} vs {away} - {minuto}' ({league_name})")

        if fixture_id not in stato_partite:
            stato_partite[fixture_id] = {}
        stato_partite[fixture_id].update({
            "score_home": score_home,
            "score_away": score_away,
            "last_minute": minuto,
            "home": home,
            "away": away,
            "league": league_name,
        })

        events = fetch_fixture_events(fixture_id)
        goals = extract_goals(events)

        stats = get_statistiche_partita(fixture_id)
        if stats and len(stats) >= 2:
            stats_home = stats[0].get("statistics", [])
            stats_away = stats[1].get("statistics", [])
            tiri_casa = estrai_valore_stat(stats_home, "Total Shots")
            tiri_ospite = estrai_valore_stat(stats_away, "Total Shots")
            tiri_p_casa = estrai_valore_stat(stats_home, "Shots on Goal")
            tiri_p_ospite = estrai_valore_stat(stats_away, "Shots on Goal")
            corner_casa = estrai_valore_stat(stats_home, "Corner Kicks")
            corner_ospite = estrai_valore_stat(stats_away, "Corner Kicks")

            current_stats = {
                "Tiri totali": (tiri_casa, tiri_ospite),
                "Tiri in porta": (tiri_p_casa, tiri_p_ospite),
                "Corner": (corner_casa, corner_ospite),
            }

            history = stato_partite[fixture_id].get("history", [])
            history.append({"timestamp": time.time(), "stats": current_stats})
            history = [h for h in history if time.time() - h["timestamp"] <= 1200]
            stato_partite[fixture_id]["history"] = history
        else:
            current_stats = None
            tiri_casa = tiri_ospite = tiri_p_casa = tiri_p_ospite = corner_casa = corner_ospite = 0

        if status_short in ("FT", "AET", "PEN"):
            stato = stato_partite.get(fixture_id, {})
            if not stato.get("notified_final"):
                muted_data = SILENCED_MATCHES.get(str(fixture_id))

                if muted_data:
                    diff_h = score_home - muted_data.get("score_home", 0)
                    diff_a = score_away - muted_data.get("score_away", 0)
                    muted_minute = muted_data.get("muted_at_minute", 0)

                    after_text = ""
                    if diff_h > 0:
                        after_text += f" +{diff_h}CASA"
                    if diff_a > 0:
                        after_text += f" +{diff_a}OSP"

                    goals_after = [g for g in goals if g["minute"] > muted_minute]
                    minutes_text = ""
                    for g in goals_after:
                        team_emoji = "CASA" if g["team"] == home else "OSP"
                        minutes_text += f" {g['minute']}'{team_emoji}"
                    if not minutes_text:
                        minutes_text = " Nessun gol dopo il silenzio"

                    messaggio = (
                        f"{home} vs {away}\n"
                        f"{league_name}\n"
                        f"Risultato finale: {score_home} - {score_away}{after_text}\n"
                        f"Silenziato al {muted_minute}'\n"
                        f"Gol dopo:{minutes_text}"
                    )
                    foto_path = None
                else:
                    if current_stats:
                        foto_path = genera_grafico_barre(fixture_id, home, away, current_stats)
                    else:
                        foto_path = None

                    goals_text = ""
                    if goals:
                        goals_text += f"\nPrimo gol: {goals[0]['minute']}' ({goals[0]['player']})\n"
                        if len(goals) > 1:
                            goals_text += f"Ultimo gol: {goals[-1]['minute']}' ({goals[-1]['player']})\n"

                    messaggio = (
                        f"{home} vs {away}\n"
                        f"{league_name}\n"
                        f"RISULTATO FINALE\n\n"
                        f"{score_home} - {score_away}\n"
                        f"{goals_text}\n"
                        f"Statistiche finali:\n"
                        f"- Tiri totali: {tiri_casa if current_stats else '?'} - {tiri_ospite if current_stats else '?'}\n"
                        f"- Tiri in porta: {tiri_p_casa if current_stats else '?'} - {tiri_p_ospite if current_stats else '?'}\n"
                        f"- Corner: {corner_casa if current_stats else '?'} - {corner_ospite if current_stats else '?'}"
                    )

                invia_notifica_telegram(foto_path, messaggio)

                SILENCED_MATCHES.pop(str(fixture_id), None)
                save_silenced(SILENCED_MATCHES)
                if foto_path and os.path.exists(foto_path):
                    try:
                        os.remove(foto_path)
                    except:
                        pass

            stato_partite[fixture_id] = {
                "tiri_casa": stato.get("tiri_casa", 0),
                "tiri_ospite": stato.get("tiri_ospite", 0),
                "timestamp_notifica": stato.get("timestamp_notifica", 0),
                "home": home,
                "away": away,
                "league": league_name,
                "notified_final": True,
            }
            return

        if str(fixture_id) in SILENCED_MATCHES:
            muted_data = SILENCED_MATCHES[str(fixture_id)]
            if "muted_at_minute" not in muted_data:
                muted_data["muted_at_minute"] = minuto
                save_silenced(SILENCED_MATCHES)
            log(f"  -> Silenziata, skip")
            return

        if current_stats:
            delta_stats, is_real_delta = calcola_delta_15min(fixture_id, current_stats)
            stats_dict = delta_stats
            header_stats = "Statistiche ultimi 15 min" if is_real_delta else "Primo rilevamento"
        else:
            stats_dict = {"Tiri totali": (0, 0), "Tiri in porta": (0, 0), "Corner": (0, 0)}
            header_stats = "Statistiche"

        log(f"  Tiri: {tiri_casa}-{tiri_ospite} | Porta: {tiri_p_casa}-{tiri_p_ospite} | Corner: {corner_casa}-{corner_ospite}")
        log(f"  Delta 15min: {stats_dict}")

        # ============================================================
        # 4 TRIGGER DI NOTIFICA + ANTI-SPAM
        # ============================================================
        total_goals = score_home + score_away
        goal_diff = abs(score_home - score_away)
        total_shots_all = tiri_casa + tiri_ospite
        notify_key = f"{fixture_id}_{score_home}_{score_away}"

        # Trigger A: Delta 15min (momentum)
        d_tiri = stats_dict.get("Tiri totali", (0, 0))
        d_porta = stats_dict.get("Tiri in porta", (0, 0))
        d_corner = stats_dict.get("Corner", (0, 0))
        trigger_stats = (
            (d_tiri[0] + d_tiri[1]) >= MOMENTUM_TIRI_TOTALI or
            (d_porta[0] + d_porta[1]) >= MOMENTUM_TIRI_IN_PORTA or
            (d_corner[0] + d_corner[1]) >= MOMENTUM_CORNER
        )

        # Trigger B: Almeno N gol totali
        trigger_goals = total_goals >= MIN_GOALS_TOTAL

        # Trigger C: Tiri totali alti ma pochi gol
        trigger_shots_low_goals = (
            total_shots_all >= MIN_TOTAL_SHOTS_LOW_GOALS and
            total_goals <= MAX_GOALS_FOR_SHOTS_TRIGGER
        )

        # Trigger D: Differenza gol >= N
        trigger_diff = goal_diff >= MIN_GOALS_DIFF

        # Determina motivo
        alert_reason = None
        if trigger_stats:
            alert_reason = f"Stats 15min: Tiri {d_tiri[0]+d_tiri[1]}, Porta {d_porta[0]+d_porta[1]}, Corner {d_corner[0]+d_corner[1]}"
        elif trigger_goals:
            alert_reason = f"Partita golosa: {score_home}-{score_away} al {minuto}'"
        elif trigger_shots_low_goals:
            alert_reason = f"Tiri alti ({total_shots_all}) ma pochi gol ({total_goals}) — possibile Over"
        elif trigger_diff:
            alert_reason = f"Differenza gol {goal_diff}: {score_home}-{score_away} al {minuto}'"

        # Decisione finale
        if alert_reason and notify_key not in notified_matches:
            log(f"  -> TRIGGER ATTIVO: {alert_reason}")
            pass  # continua sotto per inviare il messaggio
        elif alert_reason and notify_key in notified_matches:
            log(f"  -> Già notificata questo punteggio: {home} vs {away}")
            return
        else:
            prev_notified = stato_partite.get(fixture_id, {}).get("notified_final", False)
            stato_partite[fixture_id].update({
                "tiri_casa": tiri_casa,
                "tiri_ospite": tiri_ospite,
                "timestamp_notifica": stato_partite[fixture_id].get("timestamp_notifica", 0),
                "notified_final": prev_notified,
            })
            log(f"  -> Skip")
            return

        if current_stats and any(v[0] + v[1] > 0 for v in current_stats.values()):
            foto_path = genera_grafico_barre(fixture_id, home, away, current_stats)
        else:
            foto_path = None


        diff = stats_dict["Tiri totali"][0] - stats_dict["Tiri totali"][1]
        freccia = "CASA" if diff > 0 else "OSP" if diff < 0 else "EQ"

        

        fire_t_c = get_fire_suffix(stats_dict["Tiri totali"][0])
        fire_t_o = get_fire_suffix(stats_dict["Tiri totali"][1])
        fire_p_c = get_fire_suffix_shots(stats_dict["Tiri in porta"][0])
        fire_p_o = get_fire_suffix_shots(stats_dict["Tiri in porta"][1])
        if current_stats and any(v[0] + v[1] > 0 for v in current_stats.values()):
            stats_text = (
                f"{header_stats}:\n"
                f"- Tiri totali: {stats_dict['Tiri totali'][0]}{fire_t_c} - {stats_dict['Tiri totali'][1]}{fire_t_o} {freccia}\n"
                f"- Tiri in porta: {stats_dict['Tiri in porta'][0]}{fire_p_c} - {stats_dict['Tiri in porta'][1]}{fire_p_o}\n"
                f"- Corner: {stats_dict['Corner'][0]} - {stats_dict['Corner'][1]}\n\n"
                f"Verde = {home}\n"
                f"Rosso = {away}"
            )
        else:
            stats_text = "📊 Statistiche non disponibili per questa competizione"


        goals_text = ""
        if goals:
            goals_text += f"\nPrimo gol: {goals[0]['minute']}' ({goals[0]['player']})\n"
            if len(goals) > 1:
                goals_text += f"Ultimo gol: {goals[-1]['minute']}' ({goals[-1]['player']})\n"

        messaggio = (
            f"{home} vs {away}\n"
            f"{league_name}\n"
            f"Minuto: {minuto}' | Stato: {status_short}\n\n"
            f"Risultato: {score_home} - {score_away}\n"
            f"{goals_text}\n"
            f"{header_stats}:\n"
            f"- Tiri totali: {stats_dict['Tiri totali'][0]}{fire_t_c} - {stats_dict['Tiri totali'][1]}{fire_t_o} {freccia}\n"
f"- Tiri in porta: {stats_dict['Tiri in porta'][0]}{fire_p_c} - {stats_dict['Tiri in porta'][1]}{fire_p_o}\n"
f"- Corner: {stats_dict['Corner'][0]} - {stats_dict['Corner'][1]}\n\n"
            f"Verde = {home}\n"
            f"Rosso = {away}"
        )

        is_fav = str(fixture_id) in FAVORITE_MATCHES
        is_sil = str(fixture_id) in SILENCED_MATCHES
        keyboard = get_notification_keyboard(fixture_id, is_fav, is_sil)
        invia_notifica_telegram(foto_path, messaggio, reply_markup=keyboard)
        # Anti-spam: marca questa partita+score come notificata
        notified_matches.add(notify_key)

        prev_notified = stato_partite.get(fixture_id, {}).get("notified_final", False)
        stato_partite[fixture_id].update({
            "tiri_casa": tiri_casa,
            "tiri_ospite": tiri_ospite,
            "timestamp_notifica": time.time(),
            "notified_final": prev_notified,
        })

        if foto_path and os.path.exists(foto_path):
            try:
                os.remove(foto_path)
            except:
                pass

    except Exception as e:
        log(f"Errore processa_partita: {e}")


def pulisci_partite_terminate(fixture_ids_live):
    ids_da_rimuovere = [fid for fid in stato_partite if fid not in fixture_ids_live]
    for fid in ids_da_rimuovere:
        stato = stato_partite.get(fid, {})
        # Notifica finale solo se la partita era stata notificata in precedenza
        if not stato.get("notified_final") and stato.get("timestamp_notifica", 0) > 0:
            home = stato.get("home", "Squadra A")
            away = stato.get("away", "Squadra B")
            invia_messaggio_telegram(f"{home} vs {away}\nRisultato finale: partita terminata.")

        SILENCED_MATCHES.pop(str(fid), None)
        del stato_partite[fid]

    if ids_da_rimuovere:
        save_silenced(SILENCED_MATCHES)
        log(f"Partite terminate rimosse: {len(ids_da_rimuovere)}")


# =============================================================================
# CICLO PRINCIPALE
# =============================================================================
if __name__ == "__main__":
    log("=== Bot avviato ===")
    invia_messaggio_telegram("Bot avviato\nMonitoraggio partite live in corso...")

    while True:
        if not CONFIG_VALIDA:
            log("CONFIGURAZIONE INCOMPLETA - Attendo 30 secondi...")
            time.sleep(30)
            continue

        ciclo_numero += 1
        log(f"\n=== Ciclo #{ciclo_numero} - {time.strftime('%H:%M:%S')} ===")

        partite = get_partite_live()
        partite_valide = [
            f for f in partite
            if campionato_valido(
                f.get("league", {}).get("name", ""),
                f.get("league", {}).get("type", "")
            )
        ]
        log(f"Partite live: {len(partite)} totali, {len(partite_valide)} valide")

        if ciclo_numero == 1 or ciclo_numero % 10 == 0:
            invia_messaggio_telegram(
                f"Bot attivo - Ciclo #{ciclo_numero}\n"
                f"Partite live: {len(partite)} totali, {len(partite_valide)} monitorate"
            )

        fixture_ids_live = set()
        for fixture in partite:
            fixture_id = fixture.get("fixture", {}).get("id")
            if fixture_id:
                fixture_ids_live.add(fixture_id)
            processa_partita(fixture)
            time.sleep(1)

        pulisci_partite_terminate(fixture_ids_live)
        log("Attesa 3 minuti...")
        time.sleep(180)