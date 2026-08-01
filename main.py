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
        print("--> [BOT] Scansione live SofaScore in corso...", flush=True)
        try:
            # Endpoint JSON pubblico delle partite in corso su SofaScore
            url = "https://api.sofascore.com/api/v1/sport/football/events/live"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Origin": "https://www.sofascore.com",
                "Referer": "https://www.sofascore.com/"
            }
            
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                events = data.get("events", [])
                print(f"--> [BOT] Trovate {len(events)} partite live.", flush=True)
                
                for event in events:
                    match_id = event.get("id")
                    home_team = event.get("homeTeam", {}).get("name")
                    away_team = event.get("awayTeam", {}).get("name")
                    print(f"--> [MATCH] {home_team} vs {away_team} (ID: {match_id})", flush=True)
                    
                    # Esempio per recuperare le statistiche di dettaglio (es. tiri e angoli) per singolo match ID
                    # stats_url = f"https://api.sofascore.com/api/v1/event/{match_id}/statistics"
            else:
                print(f"--> [BOT] Risposta non valida: {response.status_code}", flush=True)
                
        except Exception as e:
            print(f"--> [BOT] Errore nel ciclo: {e}", flush=True)
            
        time.sleep(60)

if __name__ == "__main__":
    print("--> [MAIN] Avvio applicazione...", flush=True)
    
    # Avvia il bot in un thread separato
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Avvia il server HTTP sul thread principale (richiesto da Render)
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    print(f"--> [SERVER] In ascolto sulla porta {port}", flush=True)
    
    try:
        server.serve_forever()
    except Exception as e:
        print(f"--> [SERVER] Errore critico server: {e}", flush=True)

