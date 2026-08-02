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
        pass  # Silenzia i log HTTP

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server, daemon=True)
server_thread.start()

# --- LETTURA CONFIGURAZIONE ---
config_path = os.path.join(os.path.dirname(__file__), 'config.json')
with open(config_path, 'r') as f:
    config = json.load(f)

TELEGRAM_BOT_TOKEN = config.get("telegram_bot_token")
TELEGRAM_CHAT_ID = config.get("telegram_chat_id")
API_FOOTBALL_KEY = config.get("api_football_key")

# Soglie notifica
DIFF_TIRI_SOGLIA = 3        # Differenza tiri tra casa e ospite
TIRI_TOTALI_ATTIVA = 6      # Tiri totali per partita "attiva"
MINUTI_ATTIVA = 25          # Entro quanti minuti per considerare partita attiva
INTERVALLO_FORZATO = 1800   # 30 minuti: se non arriva notifica, forza se ci sono dati

# Campionati e parole chiave da ESCLUDERE
PAROLE_ESCLUSE = [
    "women", "femminile", "female", "u20", "u19", "u18", "u17", "u16", "u15",
    "under-20", "under-19", "under-18", "under-17", "under 20", "under 19",
    "under 18", "under 17", "youth", "amateur", "dilettanti", "regional",
    "reserves", "riserve", "friendlies", "amichevoli", "friendly"
]

# Stato per ogni partita monitorata
stato_partite = {}


def campionato_valido(league_name: str, league_type: str) -> bool:
    """Ritorna False se il campionato è da escludere."""
    nome = league_name.lower()
    for parola in PAROLE_ESCLUSE:
        if parola in nome:
            return False
    # Esclude tornei non ufficiali (tipo Cup di categoria minore)
    if league_type and league_type.lower() not in ["league", "cup", "championship"]:
        return False
    return True


def invia_notifica_telegram(foto_path, messaggio):
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
                response = requests.post(url, data=data, files=files, timeout=10)
                print(f"Telegram (foto) - Status: {response.status_code}")
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            data = {
                'chat_id': TELEGRAM_CHAT_ID,
                'text': messaggio,
                'parse_mode': 'Markdown'
            }
            response = requests.post(url, data=data, timeout=10)
            print(f"Telegram (testo) - Status: {response.status_code}")
    except Exception as e:
        print(f"Errore invio Telegram: {e}")


def get_partite_live():
    """Recupera tutte le partite live da API-Football."""
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {
        "x-apisports-key": API_FOOTBALL_KEY
    }
    params = {"live": "all"}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if response.status_code != 200:
            print(f"Errore API-Football: {response.status_code}")
            return []
        data = response.json()
        return data.get("response", [])
    except Exception as e:
        print(f"Errore nel recupero partite live: {e}")
        return []


def get_statistiche_partita(fixture_id):
    """Recupera le statistiche dettagliate di una partita."""
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
        print(f"Errore statistiche partita {fixture_id}: {e}")
        return None


def estrai_valore_stat(stats_team, nome_stat):
    """Estrae il valore numerico di una statistica specifica."""
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
    """
    Genera un grafico a onda che mostra il momentum basato sui tiri nel tempo.
    Usa shots on goal e total shots come proxy del momentum.
    """
    try:
        tiri_casa = estrai_valore_stat(stats_home, "Total Shots")
        tiri_ospite = estrai_valore_stat(stats_away, "Total Shots")
        tiri_p_casa = estrai_valore_stat(stats_home, "Shots on Goal")
        tiri_p_ospite = estrai_valore_stat(stats_away, "Shots on Goal")
        corner_casa = estrai_valore_stat(stats_home, "Corner Kicks")
        corner_ospite = estrai_valore_stat(stats_away, "Corner Kicks")
        att_casa = estrai_valore_stat(stats_home, "Attacks")
        att_ospite = estrai_valore_stat(stats_away, "Attacks")

        # Punteggio pressione: peso tiri in porta + tiri totali + corner + attacchi
        pressione_casa = (tiri_p_casa * 3) + (tiri_casa * 2) + corner_casa + (att_casa * 0.1)
        pressione_ospite = (tiri_p_ospite * 3) + (tiri_ospite * 2) + corner_ospite + (att_ospite * 0.1)

        # Crea curva a onda sintetica con i punti chiave
        x = np.linspace(0, 90, 300)
        
        # Differenza normalizzata come ampiezza dell'onda
        diff = pressione_casa - pressione_ospite
        totale = pressione_casa + pressione_ospite if (pressione_casa + pressione_ospite) > 0 else 1
        ampiezza = (diff / totale) * 10

        # Onda con componente sinusoidale + tendenza
        y = ampiezza * np.sin(np.linspace(0, 4 * np.pi, 300)) + np.linspace(0, ampiezza * 0.5, 300)
        y += np.random.normal(0, 0.3, 300)  # rumore leggero per renderla più naturale

        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(12, 4))
        fig.patch.set_facecolor('#1a1a2e')
        ax.set_facecolor('#1a1a2e')

        ax.axhline(0, color='#ffffff', linewidth=0.8, linestyle='--', alpha=0.4)
        ax.fill_between(x, y, 0, where=(y >= 0), color='#00E676', alpha=0.7, interpolate=True)
        ax.fill_between(x, y, 0, where=(y < 0), color='#FF5252', alpha=0.7, interpolate=True)
        ax.plot(x, y, color='white', linewidth=1.2, alpha=0.8)

        # Labels squadre
        ax.text(2, ax.get_ylim()[1] * 0.85 if ax.get_ylim()[1] != 0 else 1,
                home_name, color='#00E676', fontsize=11, fontweight='bold', va='top')
        ax.text(88, ax.get_ylim()[0] * 0.85 if ax.get_ylim()[0] != 0 else -1,
                away_name, color='#FF5252', fontsize=11, fontweight='bold', va='bottom', ha='right')

        # Scoreboard tiri
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
        print(f"Errore generazione grafico momentum: {e}")
        return None


def deve_notificare(fixture_id, tiri_casa, tiri_ospite, minuto):
    """
    Determina se inviare la notifica in base alle regole:
    - Regola 1: differenza tiri >= 3
    - Regola 2: partita molto attiva (>= 6 tiri totali entro 25 min)
    - Non inviare se le stats non sono cambiate dall'ultima notifica
    - Forza notifica ogni 30 min se ci sono dati significativi
    """
    stato = stato_partite.get(fixture_id, {})
    ultima_notifica_tiri_casa = stato.get("tiri_casa", -1)
    ultima_notifica_tiri_ospite = stato.get("tiri_ospite", -1)
    ultimo_invio = stato.get("timestamp_notifica", 0)

    # Se nulla è cambiato, non notificare
    if tiri_casa == ultima_notifica_tiri_casa and tiri_ospite == ultima_notifica_tiri_ospite:
        return False

    tiri_totali = tiri_casa + tiri_ospite
    diff = abs(tiri_casa - tiri_ospite)
    ora = time.time()
    tempo_passato = ora - ultimo_invio

    # Regola 1: differenza >= 3
    if diff >= DIFF_TIRI_SOGLIA:
        return True

    # Regola 2: partita molto attiva entro 25 minuti
    if minuto <= MINUTI_ATTIVA and tiri_totali >= TIRI_TOTALI_ATTIVA:
        return True

    # Regola 3: forzata ogni 30 min se ci sono almeno 4 tiri totali
    if tempo_passato >= INTERVALLO_FORZATO and tiri_totali >= 4:
        return True

    return False


def processa_partita(fixture):
    """Elabora una singola partita live e invia notifica se necessario."""
    try:
        fixture_id = fixture["fixture"]["id"]
        league = fixture.get("league", {})
        league_name = league.get("name", "")
        league_type = league.get("type", "")

        # Filtro campionati
        if not campionato_valido(league_name, league_type):
            return

        home = fixture["teams"]["home"]["name"]
        away = fixture["teams"]["away"]["name"]
        score_home = fixture["score"]["fulltime"]["home"] or fixture["goals"]["home"] or 0
        score_away = fixture["score"]["fulltime"]["away"] or fixture["goals"]["away"] or 0
        minuto = fixture["fixture"]["status"].get("elapsed") or 0
        status_short = fixture["fixture"]["status"].get("short", "LIVE")

        print(f"Analisi: {home} vs {away} - {minuto}' ({league_name})")

        # Statistiche
        stats = get_statistiche_partita(fixture_id)
        if not stats or len(stats) < 2:
            print(f"  -> Statistiche non disponibili")
            return

        stats_home = stats[0].get("statistics", [])
        stats_away = stats[1].get("statistics", [])

        tiri_casa = estrai_valore_stat(stats_home, "Total Shots")
        tiri_ospite = estrai_valore_stat(stats_away, "Total Shots")
        tiri_p_casa = estrai_valore_stat(stats_home, "Shots on Goal")
        tiri_p_ospite = estrai_valore_stat(stats_away, "Shots on Goal")
        corner_casa = estrai_valore_stat(stats_home, "Corner Kicks")
        corner_ospite = estrai_valore_stat(stats_away, "Corner Kicks")

        # Controllo se notificare
        if not deve_notificare(fixture_id, tiri_casa, tiri_ospite, minuto):
            print(f"  -> Nessuna variazione significativa, skip")
            return

        # Genera grafico
        foto_path = genera_grafico_momentum(fixture_id, home, away, stats_home, stats_away)

        # Costruisce messaggio
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

        # Aggiorna stato
        stato_partite[fixture_id] = {
            "tiri_casa": tiri_casa,
            "tiri_ospite": tiri_ospite,
            "timestamp_notifica": time.time()
        }

        # Pulizia file grafico
        if foto_path and os.path.exists(foto_path):
            try:
                os.remove(foto_path)
            except:
                pass

    except Exception as e:
        print(f"Errore processando partita {fixture.get('fixture', {}).get('id', '?')}: {e}")


def pulisci_partite_terminate(fixture_ids_live):
    """Rimuove dallo stato le partite non più live."""
    ids_da_rimuovere = [fid for fid in stato_partite if fid not in fixture_ids_live]
    for fid in ids_da_rimuovere:
        del stato_partite[fid]
        print(f"Partita {fid} terminata, rimossa dallo stato.")


# --- CICLO PRINCIPALE ---
if __name__ == "__main__":
    print("Bot avviato. Monitoraggio partite live in corso...")

    while True:
        print(f"\n{'='*50}")
        print(f"Ciclo alle {time.strftime('%H:%M:%S')}")

        if not API_FOOTBALL_KEY:
            print("ERRORE: api_football_key non configurata nel config.json")
            time.sleep(180)
            continue

        partite = get_partite_live()
        print(f"Partite live trovate: {len(partite)}")

        fixture_ids_live = set()

        for fixture in partite:
            fixture_id = fixture.get("fixture", {}).get("id")
            if fixture_id:
                fixture_ids_live.add(fixture_id)
            processa_partita(fixture)
            time.sleep(1)  # Evita rate limit API

        pulisci_partite_terminate(fixture_ids_live)
        print(f"Attesa 3 minuti...")
        time.sleep(180)