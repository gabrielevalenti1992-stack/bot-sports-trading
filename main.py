import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests

# 1. Server Web per Render
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Online!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    server.serve_forever()

# 2. Invio notifiche Telegram
def send_telegram(messaggio):
    try:
        with open("config.json", "r") as f:
            cfg = json.load(f)
        token = cfg.get("telegram_bot_token") or cfg.get("telegram_token")
        chat_id = cfg.get("telegram_chat_id")
        
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": messaggio, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Errore Telegram: {e}")

# 3. Scansione tramite API-Football (Backup)
def scansiona_api_football():
    print("--> [FALLBACK] Scansione tramite API-Football...")
    try:
        with open("config.json", "r") as f:
            cfg = json.load(f)

        api_key = cfg.get("api_football_key")
        if not api_key or "INSERISCI" in api_key:
            print("--> Chiave API-Football mancante o non valida nel config.json.")
            return

        headers = {
            "x-rapidapi-host": "v3.football.api-sports.io",
            "x-rapidapi-key": api_key
        }

        url = "https://v3.football.api-sports.io/fixtures?live=all"
        res = requests.get(url, headers=headers, timeout=10)
        
        if res.status_code != 200:
            print(f"Errore API-Football HTTP {res.status_code}")
            return

        matches = res.json().get("response", [])
        print(f"[API-Football] Partite live trovate: {len(matches)}")

        for match in matches:
            fixture = match.get("fixture", {})
            teams = match.get("teams", {})
            goals = match.get("goals", {})
            
            elapsed = fixture.get("status", {}).get("elapsed", 0)
            home_name = teams.get("home", {}).get("name")
            away_name = teams.get("away", {}).get("name")
            home_goals = goals.get("home", 0)
            away_goals = goals.get("away", 0)

            msg = (
                f"⚽ *PARTITA LIVE (API-Football)*\n"
                f"⏱️ Minuto: {elapsed}'\n"
                f"⚔️ {home_name} ({home_goals}) - ({away_goals}) {away_name}"
            )
            send_telegram(msg)

    except Exception as e:
        print(f"Errore esecuzione API-Football: {e}")

# 4. Scansione principale tramite SofaScore
def scansiona_sofascore():
    print("--> Scansione principale SofaScore...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    url = "https://api.sofascore.com/api/v1/sport/football/events/live"
    
    res = requests.get(url, headers=headers, timeout=10)
    
    # Se SofaScore fallisce (blocco 403, 429, 500, ecc.)
    if res.status_code != 200:
        raise Exception(f"SofaScore HTTP {res.status_code}")

    events = res.json().get("events", [])
    print(f"[SofaScore] Partite live trovate: {len(events)}")

    for ev in events:
        home = ev.get("homeTeam", {}).get("name", "Casa")
        away = ev.get("awayTeam", {}).get("name", "Ospiti")
        time_status = ev.get("status", {}).get("description", "Live")
        
        msg = f"⚽ *PARTITA LIVE (SofaScore)*\n⏱️ Stato: {time_status}\n⚔️ {home} vs {away}"
        send_telegram(msg)

# 5. Ciclo di controllo integrato
if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    send_telegram("🚀 *Bot attivo con sistema primario SofaScore + Backup API-Football!*")
    
    sofascore_error_sent = False

    while True:
        try:
            scansiona_sofascore()
            # Se funziona, resetta il flag di errore
            sofascore_error_sent = False
        except Exception as e:
            print(f"⚠️ Errore SofaScore: {e}")
            
            # Invia l'avviso Telegram solo una volta per evitare spam
            if not sofascore_error_sent:
                send_telegram(f"⚠️ *ATTENZIONE*: SofaScore non risponde ({e}). Passaggio automatico ad *API-Football*.")
                sofascore_error_sent = True
            
            # Passa ad API-Football
            scansiona_api_football()

        time.sleep(60)
