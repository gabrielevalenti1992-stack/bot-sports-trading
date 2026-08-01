import threading
import time
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

# 1. Server HTTP fittizio per mantenere vivo Render
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
    server = HTTPServer(('0.0.0.0', port), SimpleHandler)
    print(f"--> Server HTTP avviato sulla porta {port}", flush=True)
    server.serve_forever()

# 2. Logica principale del tuo bot di trading
def run_bot():
    print("--> Ciclo bot avviato con successo", flush=True)
    while True:
        try:
            print("--> Scansione API-Football in corso...", flush=True)
            # Inserisci qui la chiamata alle tue API e la logica del bot
            
        except Exception as e:
            print(f"Errore nel ciclo del bot: {e}", flush=True)
            
        time.sleep(60) # Pausa di 1 minuto tra le richieste

if __name__ == "__main__":
    # Avvia il server HTTP in un thread separato
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    # Avvia la logica del bot nel thread principale
    run_bot()
