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
# CONFIGURAZIONE
# =============================================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not API_FOOTBALL_KEY:
    try:
        config_path = os.path.join(os.path.dirname(__file__), 'config.json')
        with open(config_path, 'r') as f:
            config = json.load(f)
        TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN or config.get("telegram_bot_token")
        TELEGRAM_CHAT_ID = TELEGRAM_CHAT_ID or config.get("telegram_chat_id")
        API_FOOTBALL_KEY = API_FOOTBALL_KEY or config.get("api_football_key")
        print("Configurazione caricata da config.json", flush=True)
    except Exception as e:
        print(f"Errore lettura config.json: {e}", flush=True)

print(f"TOKEN presente: {'SI' if TELEGRAM_BOT_TOKEN else 'NO'}", flush=True)
print(f"CHAT_ID presente: {'SI' if TELEGRAM_CHAT_ID else 'NO'}", flush=True)
print(f"API_KEY presente: {'SI' if API_FOOTBALL_KEY else 'NO'}", flush=True)

DIFF_TIRI_SOGLIA = 3
TIRI_TOTALI_ATTIVA = 6
MINUTI_ATTIVA = 25
INTERVALLO_FORZATO = 1800

PAROLE_ESCLUSE = [
    "women", "femminile", "female", "u20", "u19", "u18", "u17", "u16", "u15",
    "under-20", "under-19", "under-18", "under-17", "under 20", "under 19",
    "under 18", "under 17", "youth", "amateur", "dilettanti", "regional",
    "reserves", "riserve", "friendlies", "amichevoli", "friendly"
]

stato_partite = {}
ciclo_numero = 0

# =============================================================================
# STATO SILENZIATI (persistenza su file)
# =============================================================================
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
# THREAD: ASCOLTA CLICK SUL BOTTONE SILENZIA
# =============================================================================
def poll_callbacks():
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            r = requests.get(url, params={"offset": offset, "limit": 10}, timeout=10)
            updates = r.json().get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                cq = upd.get("callback_query")
                if cq:
                    data = cq.get("data", "")
                    if data.startswith("mute:"):
                        fid = int(data.split(":")[1])
                        SILENCED_MATCHES.add(fid)
                        save_silenced(SILENCED_MATCHES)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                            json={"callback_query_id": cq["id"]}, timeout=5)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
                            json={
                                "chat_id": cq["message"]["chat"]["id"],
                                "message_id": cq["message"]["message_id"],
                                "reply_markup": json.dumps({"inline_keyboard": []})
                            }, timeout=5)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            json={
                                "chat_id": cq["message"]["chat"]["id"],
                                "text": "🔕 Partita silenziata. Non riceverai più alert live. Il risultato finale arriverà comunque.",
                                "parse_mode": "Markdown"
                            }, timeout=5)
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
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {'chat_id': TELEGRAM_CHAT_ID, 'text': testo, 'parse_mode': 'Markdown'}
        response = requests.post(url, data=data, timeout=10)
        log(f"Telegram testo - Status: {response.status_code} - {response.text[:100]}")
    except Exception as e:
        log(f"Errore invio testo Telegram: {e}")


def invia_notifica_telegram(foto_path, messaggio, reply_markup=None):
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
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"live": "all"}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        log(f"API-Football status: {response.status_code}")
        if response.status_code != 200:
            invia_messaggio_telegram(f"⚠️ *Errore API*\nHTTP {response.status_code}")
            return []
        data = response.json()
        errori = data.get("errors", {})
        if errori:
            invia_messaggio_telegram(f"⚠️ *Errore API*\n{errori}")
            return []
        return data.get("response", [])
    except Exception as e:
        log(f"Errore get_partite_live: {e}")
        invia_messaggio_telegram(f"⚠️ *Eccezione API*\n{e}")
        return []


def get_statistiche_partita(fixture_id):
    url = "https://v3.football.api-sports.io/fixtures/statistics"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"fixture": fixture_id}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if response.status_code != 200:
            return None
        data = response.json()
        return data.get("response", [])
    except Exception as e:
        log(f"Errore statistiche {fixture_id}: {e}")
        return None


def fetch_fixture_events(fixture_id):
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
                "player": (ev.get("player") or {}).get("name") or "Sconosciuto"
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
# NUOVO GRAFICO: BARRE ORIZZONTALI COMPARATIVE
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
# TASTIERA INLINE: BOTTONE SILENZIA
# =============================================================================
def get_notification_keyboard(fixture_id):
    if fixture_id in SILENCED_MATCHES:
        return None
    return {
        "inline_keyboard": [[
            {"text": "🔕 Silenzia questa partita", "callback_data": f"mute:{fixture_id}"}
        ]]
    }


# =============================================================================
# REGOLE DI NOTIFICA
# =============================================================================
def deve_notificare(fixture_id, tiri_casa, tiri_ospite, minuto):
    stato = stato_partite.get(fixture_id, {})
    ultima_casa = stato.get("tiri_casa", -1)
    ultima_ospite = stato.get("tiri_ospite", -1)
    ultimo_invio = stato.get("timestamp_notifica", 0)

    if tiri_casa == ultima_casa and tiri_ospite == ultima_ospite:
        return False

    tiri_totali = tiri_casa + tiri_ospite
    diff = abs(tiri_casa - tiri_ospite)
    tempo_passato = time.time() - ultimo_invio

    if diff >= DIFF_TIRI_SOGLIA:
        return True
    if minuto <= MINUTI_ATTIVA and tiri_totali >= TIRI_TOTALI_ATTIVA:
        return True
    if tempo_passato >= INTERVALLO_FORZATO and tiri_totali >= 4:
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

        events = fetch_fixture_events(fixture_id)
        goals = extract_goals(events)

        # --- NOTIFICA FINALE (sempre, anche se silenziata) ---
        if status_short in ("FT", "AET", "PEN"):
            stato = stato_partite.get(fixture_id, {})
            if not stato.get("notified_final"):
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

                    stats_dict = {
                        "Tiri totali": (tiri_casa, tiri_ospite),
                        "Tiri in porta": (tiri_p_casa, tiri_p_ospite),
                        "Corner": (corner_casa, corner_ospite),
                    }
                    foto_path = genera_grafico_barre(fixture_id, home, away, stats_dict)
                else:
                    foto_path = None
                    tiri_casa = tiri_p_casa = corner_casa = 0
                    tiri_ospite = tiri_p_ospite = corner_ospite = 0

                goals_text = ""
                if goals:
                    goals_text += f"\n🥇 Primo gol: {goals[0]['minute']}' ({goals[0]['player']})\n"
                    if len(goals) > 1:
                        goals_text += f"⚡ Ultimo gol: {goals[-1]['minute']}' ({goals[-1]['player']})\n"

                messaggio = (
                    f"🏁 *{home}* vs *{away}*\n"
                    f"🏆 {league_name}\n"
                    f"🏁 *RISULTATO FINALE*\n\n"
                    f"🔢 {score_home} - {score_away}\n"
                    f"{goals_text}\n"
                    f"📊 Statistiche finali:\n"
                    f"• Tiri totali: {tiri_casa if stats else '?'} - {tiri_ospite if stats else '?'}\n"
                    f"• Tiri in porta: {tiri_p_casa if stats else '?'} - {tiri_p_ospite if stats else '?'}\n"
                    f"• Corner: {corner_casa if stats else '?'} - {corner_ospite if stats else '?'}"
                )

                invia_notifica_telegram(foto_path, messaggio)

                SILENCED_MATCHES.discard(fixture_id)
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

        # --- SALTA SE SILENZIATA ---
        if fixture_id in SILENCED_MATCHES:
            log(f"  -> Silenziata, skip")
            return

        # --- STATISTICHE LIVE ---
        stats = get_statistiche_partita(fixture_id)
        if not stats or len(stats) < 2:
            log(f"  -> Stats non disponibili")
            return

        stats_home = stats[0].get("statistics", [])
        stats_away = stats[1].get("statistics", [])

        tiri_casa = estrai_valore_stat(stats_home, "Total Shots")
        tiri_ospite = estrai_valore_stat(stats_away, "Total Shots")
        tiri_p_casa = estrai_valore_stat(stats_home, "Shots on Goal")
        tiri_p_ospite = estrai_valore_stat(stats_away, "Shots on Goal")
        corner_casa = estrai_valore_stat(stats_home, "Corner Kicks")
        corner_ospite = estrai_valore_stat(stats_away, "Corner Kicks")

        log(f"  Tiri: {tiri_casa}-{tiri_ospite} | Porta: {tiri_p_casa}-{tiri_p_ospite} | Corner: {corner_casa}-{corner_ospite}")

        if not deve_notificare(fixture_id, tiri_casa, tiri_ospite, minuto):
            log(f"  -> Skip")
            return

        stats_dict = {
            "Tiri totali": (tiri_casa, tiri_ospite),
            "Tiri in porta": (tiri_p_casa, tiri_p_ospite),
            "Corner": (corner_casa, corner_ospite),
        }
        foto_path = genera_grafico_barre(fixture_id, home, away, stats_dict)

        diff = tiri_casa - tiri_ospite
        freccia = "🏠" if diff > 0 else "✈️" if diff < 0 else "⚖️"

        goals_text = ""
        if goals:
            goals_text += f"\n🥇 Primo gol: {goals[0]['minute']}' ({goals[0]['player']})\n"
            if len(goals) > 1:
                goals_text += f"⚡ Ultimo gol: {goals[-1]['minute']}' ({goals[-1]['player']})\n"

        messaggio = (
            f"⚽ *{home}* vs *{away}*\n"
            f"🏆 {league_name}\n"
            f"⏱️ Minuto: `{minuto}'` | Stato: `{status_short}`\n\n"
            f"🔢 *Risultato:* {score_home} - {score_away}\n"
            f"{goals_text}\n"
            f"📊 *Statistiche:*\n"
            f"• Tiri totali: {tiri_casa} - {tiri_ospite} {freccia}\n"
            f"• Tiri in porta: {tiri_p_casa} - {tiri_p_ospite}\n"
            f"• Corner: {corner_casa} - {corner_ospite}\n\n"
            f"🟢 Verde = {home}\n"
            f"🔴 Rosso = {away}"
        )

        keyboard = get_notification_keyboard(fixture_id)
        invia_notifica_telegram(foto_path, messaggio, reply_markup=keyboard)

        prev_notified = stato_partite.get(fixture_id, {}).get("notified_final", False)
        stato_partite[fixture_id] = {
            "tiri_casa": tiri_casa,
            "tiri_ospite": tiri_ospite,
            "timestamp_notifica": time.time(),
            "home": home,
            "away": away,
            "league": league_name,
            "notified_final": prev_notified,
        }

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
        if not stato.get("notified_final"):
            home = stato.get("home", "Squadra A")
            away = stato.get("away", "Squadra B")
            invia_messaggio_telegram(f"🏁 *{home}* vs *{away}*\nRisultato finale: partita terminata.")
        SILENCED_MATCHES.discard(fid)
        del stato_partite[fid]
    if ids_da_rimuovere:
        save_silenced(SILENCED_MATCHES)
        log(f"Partite terminate rimosse: {len(ids_da_rimuovere)}")


# =============================================================================
# CICLO PRINCIPALE
# =============================================================================
if __name__ == "__main__":
    log("=== Bot avviato ===")
    invia_messaggio_telegram("✅ *Bot avviato*\nMonitoraggio partite live in corso...")

    while True:
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
                f"🤖 *Bot attivo* - Ciclo #{ciclo_numero}\n"
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
