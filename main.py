import threading
import time
import os
import json
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

print("--> [INIT] Avvio dello script main.py...", flush=True)

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
        print("--> [BOT] Scansione live API-Football in corso...", flush=True)
        try:
            url = "https://v3.football.api-sports.io/fixtures?live=all"
            headers = {
                "x-apisports-key": "38b8c27078bfa2fc88afdaf5dbcb9079"
            }
            
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                matches = data.get("response", [])
                print(f"--> [BOT] Trovate {len(matches)} partite live.", flush=True)
                
                for match in matches:
                    fixture_id = match.get("fixture", {}).get("id")
                    home_team = match.get("teams", {}).get("home", {}).get("name")
                    away_team = match.get("teams", {}).get("away", {}).get("name")
                    print(f"--> [MATCH] {home_team} vs {away_team} (ID: {fixture_id})", flush=True)
            else:
                print(f"--> [BOT] Risposta non valida: {response.status_code} - {response.text}", flush=True)
                
        except Exception as e:
            print(f"--> [BOT] Errore nel ciclo: {e}", flush=True)
            
        time.sleep(60)

if __name__ == "__main__":
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    
    run_bot()
