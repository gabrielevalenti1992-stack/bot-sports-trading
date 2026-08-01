import threading
import time
import os
import json
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

print("--> [INIT] Avvio dello script main.py...", flush=True)

match_history = {}

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

def parse_int(val):
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        val_clean = val.replace("%", "").strip()
        if val_clean.isdigit() or (val_clean.startswith('-') and val_clean[1:].isdigit()):
            return int(val_clean)
    return 0

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
                
                current_time = time.time()
                
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
                        
                        home_total_shots = 0
                        away_total_shots = 0
                        home_shots_on_target = 0
                        away_shots_on_target = 0
                        home_corners = 0
                        away_corners = 0
                        
                        if stats_resp.status_code == 200:
                            stats_data = stats_resp.json().get("response", [])
                            for idx, team_stats in enumerate(stats_data):
                                statistics = team_stats.get("statistics", [])
                                for stat in statistics:
                                    stype = stat.get("type")
                                    sval = stat.get("value")
                                    val_int = parse_int(sval)
                                    
                                    if idx == 0:
                                        if stype == "Total Shots":
                                            home_total_shots = val_int
                                        elif stype == "Shots on Goal":
                                            home_shots_on_target = val_int
                                        elif stype == "Corner Kicks":
                                            home_corners = val_int
                                    elif idx == 1:
                                        if stype == "Total Shots":
                                            away_total_shots = val_int
                                        elif stype == "Shots on Goal":
                                            away_shots_on_target = val_int
                                        elif stype == "Corner Kicks":
                                            away_corners = val_int
                        
                        total_shots = home_total_shots + away_total_shots
                        shots_on_target = home_shots_on_target + away_shots_on_target
                        corners = home_corners + away_corners
                        
                        if (total_shots >= min_t_shots and 
                            shots_on_target >= min_s_ot and 
                            corners >= min_corn):
                            
                            send_notification = False
                            diff_shots_home = 0
                            diff_shots_away = 0
                            diff_sot_home = 0
                            diff_sot_away = 0
                            diff_corn_home = 0
                            diff_corn_away = 0
                            is_first_notification = False
                            
                            if fixture_id not in match_history:
                                send_notification = True
                                is_first_notification = True
                            else:
                                last = match_history[fixture_id]
                                diff_shots_home = home_total_shots - last["home_shots"]
                                diff_shots_away = away_total_shots - last["away_shots"]
                                diff_sot_home = home_shots_on_target - last["home_sot"]
                                diff_sot_away = away_shots_on_target - last["away_sot"]
                                diff_corn_home = home_corners - last["home_corn"]
                                diff_corn_away = away_corners - last["away_corn"]
                                
                                total_shots_prev = last["home_shots"] + last["away_shots"]
                                if total_shots > total_shots_prev:
                                    send_notification = True
                            
                            if send_notification:
                                match_history[fixture_id] = {
                                    "time": current_time,
                                    "home_shots": home_total_shots,
                                    "away_shots": away_total_shots,
                                    "home_sot": home_shots_on_target,
                                    "away_sot": away_shots_on_target,
                                    "home_corn": home_corners,
                                    "away_corn": away_corners
                                }
                                
                                if is_first_notification:
                                    shots_str = f"{total_shots} ({home_total_shots}:{away_total_shots})"
                                    sot_str = f"{shots_on_target} ({home_shots_on_target}:{away_shots_on_target})"
                                    corn_str = f"{corners} ({home_corners}:{away_corners})"
                                else:
                                    shots_str = f"{total_shots} ({home_total_shots}:{away_total_shots})"
                                    if diff_shots_home > 0 or diff_shots_away > 0:
                                        shots_str += f" `{diff_shots_home}:{diff_shots_away}`"
                                        
                                    sot_str = f"{shots_on_target} ({home_shots_on_target}:{away_shots_on_target})"
                                    if diff_sot_home > 0 or diff_sot_away > 0:
                                        sot_str += f" `{diff_sot_home}:{diff_sot_away}`"
                                        
                                    corn_str = f"{corners} ({home_corners}:{away_corners})"
                                    if diff_corn_home > 0 or diff_corn_away > 0:
                                        corn_str += f" `{diff_corn_home}:{diff_corn_away}`"

                                msg = (
                                    f"🎯 *Match Live* (Min: {elapsed}')\n"
                                    f"*{home_team}* vs *{away_team}*\n"
                                    f"Risultato: {score_home} - {score_away}\n"
                                    f"📊 Tiri Totali: {shots_str}\n"
                                    f"🎯 Tiri in Porta: {sot_str}\n"
                                    f"🚩 Calci d'angolo: {corn_str}"
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
