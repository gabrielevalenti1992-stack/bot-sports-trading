import threading
import time
import os
import json
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

print("--> [INIT] Avvio dello script main.py...", flush=True)

def load_config():
    if os.path.exists("config.json"):
        try:
            with open("config.json", "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"--> [CONFIG] Errore lettura config.json: {e}", flush=True)
    return {}

def send_telegram_message(token, chat_id, text):
    if not token or token == "INSERISCI_TOKEN_BOT":
        print("--> [TELEGRAM] Token non configurato nel file json.", flush=True)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"--> [TELEGRAM] Errore invio: {response.text}", flush=True)
    except Exception as e:
        print(f"--> [TELEGRAM] Eccezione invio: {e}", flush=True)

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    port = int(os.environ.get("PORT", 10000))
    print(f"--> [SERVER] Tentativo avvio HTTP sulla porta {port}...", flush=True)
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    print(f"--> [SERVER] HTTP avviato con successo", flush=True)
    server.serve_forever()

def run_bot():
    print("--> [BOT] Entrato nella funzione run_bot", flush=True)
    
    while True:
        config = load_config()
        token = config.get("telegram_bot_token")
        chat_id = config.get("telegram_chat_id")
        api_key = config.get("api_football_key")
        
        min_min = config.get("min_minute", 1)
        max_min = config.get("max_minute", 90)
        min_s_ot = config.get("min_shots_on_target", 1)
        min_t_shots = config.get("min_total_shots", 1)
        min_corn = config.get("min_corners", 0)
        
        print(f"--> [BOT] Scansione live API-Football in corso...", flush=True)
        try:
            url = "https://v3.football.api-sports.io/fixtures?live=all"
            headers = {
                "x-apisports-key": api_key
            }
            
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                matches = data.get("response", [])
                print(f"--> [BOT] Trovate {len(matches)} partite live totali.", flush=True)
                
                for match in matches:
                    fixture_id = match.get("fixture", {}).get("id")
                    elapsed = match.get("fixture", {}).get("status", {}).get("elapsed", 0)
                    home_team = match.get("teams", {}).get("home", {}).get("name")
                    away_team = match.get("teams", {}).get("away", {}).get("name")
                    score_home = match.get("goals", {}).get("home", 0)
                    score_away = match.get("goals", {}).get("away", 0)
                    
                    if elapsed is not None and min_min <= elapsed <= max_min:
                        stats_url = f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fixture_id}"
                        stats_resp = requests.get(stats_url, headers=headers, timeout=10)
                        
                        total_shots = 0
                        shots_on_target = 0
                        corners = 0
                        
                        if stats_resp.status_code == 200:
                            stats_data = stats_resp.json().get("response", [])
                            for team_stats in stats_data:
                                statistics = team_stats.get("statistics", [])
                                for stat in statistics:
                                    stype = stat.get("type")
                                    sval = stat.get("value")
                                    if sval is not None:
                                        if stype == "Total Shots":
                                            total_shots += int(sval)
                                        elif stype == "Shots on Goal":
                                            shots_on_target += int(sval)
                                        elif stype == "Corner Kicks":
                                            corners += int(sval)
                        
                        if (total_shots >= min_t_shots and 
                            shots_on_target >= min_s_ot and 
                            corners >= min_corn):
                            
                            msg = (
                                f"🎯 *Match Live* (Min: {elapsed}')\n"
                                f"*{home_team}* vs *{away_team}*\n"
                                f"Risultato: {score_home} - {score_away}\n"
                                f"📊 Tiri Totali: {total_shots}\n"
                                f"🎯 Tiri in Porta: {shots_on_target}\n"
                                f"🚩 Calci d'angolo: {corners}"
                            )
                            print(f"--> [NOTIFICA] Invio per {home_team} vs {away_team}", flush=True)
                            send_telegram_message(token, chat_id, msg)
            else:
                print(f"--> [BOT] Risposta non valida: {response.status_code}", flush=True)
                
        except Exception as e:
            print(f"--> [BOT] Errore nel ciclo: {e}", flush=True)
            
        time.sleep(60)

if __name__ == "__main__":
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    
    run_bot()
