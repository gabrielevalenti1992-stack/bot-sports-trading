import json
import time
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- SERVER HTTP PER RENDER FREE ---
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

# --- LETTURA CONFIGURAZIONE ---
# Prima prova le variabili d'ambiente (Render), poi il config.json come fallback
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY")

# Fallback su config.json se le env var non sono impostate
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


def invia_notifica_telegram(foto_path, messaggio):
    try:
        if foto_path and os.path.exists(foto_path):
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            with open(foto_path, 'rb') as photo:
                files = {'photo': photo}
                data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': messaggio, 'parse_mode': 'Markdown'}
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


def genera_grafico_momentum(fixture_id, home_name, away_name, stats_home, stats_away):
    try:
        tiri_casa = estrai_valore_stat(stats_home, "Total Shots")
        tiri_ospite = estrai_valore_stat(stats_away, "Total Shots")
        tiri_p_casa = estrai_valore_stat(stats_home, "Shots on Goal")
        tiri_p_ospite = estrai_valore_stat(stats_away, "Shots on Goal")
        corner_casa = estrai_valore_stat(stats_home, "Corner Kicks")
        corner_ospite = estrai_valore_stat(stats_away, "Corner Kicks")
        att_casa = estrai_valore_stat(stats_home, "Attacks")
        att_ospite = estrai_valore_stat(stats_away, "Attacks")

        pressione_casa = (tiri_p_casa * 3) + (tiri_casa * 2) + corner_casa + (att_casa * 0.1)
        pressione_ospite = (tiri_p_ospite * 3) + (tiri_ospite * 2) + corner_ospite + (att_ospite * 0.1)

        x = np.linspace(0, 90, 300)
        diff = pressione_casa - pressione_ospite
        totale = pressione_casa + pressione_ospite if (pressione_casa + pressione_ospite) > 0 else 1
        ampiezza = (diff / totale) * 10
        y = ampiezza * np.sin(np.linspace(0, 4 * np.pi, 300)) + np.linspace(0, ampiezza * 0.5, 300)
        y += np.random.normal(0, 0.3, 300)

        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(12, 4))
        fig.patch.set_facecolor('#1a1a2e')
        ax.set_facecolor('#1a1a2e')

        ax.axhline(0, color='#ffffff', linewidth=0.8, linestyle='--', alpha=0.4)
        ax.fill_between(x, y, 0, where=(y >= 0), color='#00E676', alpha=0.7, interpolate=True)
        ax.fill_between(x, y, 0, where=(y < 0), color='#FF5252', alpha=0.7, interpolate=True)
        ax.plot(x, y, color='white', linewidth=1.2, alpha=0.8)

        ylim = ax.get_ylim()
        ax.text(2, ylim[1] * 0.85 if ylim[1] != 0 else 1,
                home_name, color='#00E676', fontsize=11, fontweight='bold', va='top')
        ax.text(88, ylim[0] * 0.85 if ylim[0] != 0 else -1,
                away_name, color='#FF5252', fontsize=11, fontweight='bold', va='bottom', ha='right')

        info = (f"Tiri: {home_name} {tiri_casa} ({tiri_p_casa} in porta)  |  "
                f"{away_name} {tiri_ospite} ({tiri_p_ospite} in porta)")
        ax.set_title(info, color='white', fontsize=9, pad=8)
        ax.axis('off')
        plt.tight_layout()

        foto_path = os.path.join(os.path.dirname(__file__), f'momentum_{fixture_id}.png')
        plt.savefig(foto_path, dpi=200, bbox_inches='tight', transparent=False, facecolor='#1a1a2e')
        plt.close(fig)
        plt.close('all')
        return foto_path
    except Exception as e:
        log(f"Errore grafico: {e}")
        return None


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

        foto_path = genera_grafico_momentum(fixture_id, home, away, stats_home, stats_away)

        diff = tiri_casa - tiri_ospite
        freccia = "🏠" if diff > 0 else "✈️" if diff < 0 else "⚖️"

        messaggio = (
            f"⚽ *{home}* vs *{away}*\n"
            f"🏆 {league_name}\n"
            f"⏱️ Minuto: `{minuto}'` | Stato: `{status_short}`\n\n"
            f"🔢 *Risultato:* {score_home} - {score_away}\n\n"
            f"📊 *Statistiche:*\n"
            f"• Tiri totali: {tiri_casa} - {tiri_ospite} {freccia}\n"
            f"• Tiri in porta: {tiri_p_casa} - {tiri_p_ospite}\n"
            f"• Corner: {corner_casa} - {corner_ospite}\n\n"
            f"🟢 Verde = pressione {home}\n"
            f"🔴 Rosso = pressione {away}"
        )

        invia_notifica_telegram(foto_path, messaggio)

        stato_partite[fixture_id] = {
            "tiri_casa": tiri_casa,
            "tiri_ospite": tiri_ospite,
            "timestamp_notifica": time.time()
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
        del stato_partite[fid]
        log(f"Partita {fid} terminata.")


# --- CICLO PRINCIPALE ---
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