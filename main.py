import threading
import time
import os
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

print("--> [INIT] Avvio dello script main.py...", flush=True)

# 1. Server HTTP per Render
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

# 2. Logica del bot
def run_bot():
    print("--> [BOT] Entrato nella funzione run_bot", flush=True)
    
    # Verifica immediata del config.json
    try:
        if os.path.exists("config.json"):
            print("--> [BOT] File config.json trovato.", flush=True)
            with open("config.json", "r") as f:
                config = json.load(f)
            print("--> [BOT] config.json letto correttamente.", flush=True)
        else:
            print("--> [BOT] ERRORE: File config.json NON trovato!", flush=True)
    except Exception as e:
        print(f"--> [BOT] ERRORE lettura config.json: {e}", flush=True)

    while True:
        print("--> [BOT] Inizio ciclo di scansione...", flush=True)
        try:
            # Inserisci qui la tua chiamata alle API-Football
            pass
        except Exception as e:
            print(f"--> [BOT] Errore critico nel ciclo: {e}", flush=True)
            
        time.sleep(60)

if __name__ == "__main__":
    print("--> [MAIN] Configurazione dei thread...", flush=True)
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    
    run_bot()
