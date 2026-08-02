import json
import time
import requests
import matplotlib
matplotlib.use('Agg')  # Necessario per ambienti server senza interfaccia grafica
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

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server, daemon=True)
server_thread.start()

# --- LETTURA CONFIGURAZIONE ---
config_path = os.path.join(os.path.dirname(__file__), 'config.json')
with open(config_path, 'r') as f:
    config = json.load(f)

TELEGRAM_BOT_TOKEN = config.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = config.get("TELEGRAM_CHAT_ID")
FOTMOB_MATCH_ID = config.get("FOTMOB_MATCH_ID")

def invia_notifica_telegram(foto_path, messaggio):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    if not os.path.exists(foto_path):
        print(f"Errore: Il file immagine {foto_path} non esiste.")
        return
    with open(foto_path, 'rb') as photo:
        files = {'photo': photo}
        data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': messaggio}
        response = requests.post(url, data=data, files=files)
        print(f"Risposta Telegram sendPhoto: {response.status_code} - {response.text}")

def genera_grafico_momentum_fotmob(match_id):
    url = f"https://www.fotmob.com/api/matchDetails?matchId={match_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        print(f"Risposta FotMob API: {response.status_code}")
        if response.status_code != 200:
            momentum_data = []
        else:
            data = response.json()
            momentum_data = data.get("content", {}).get("matchFacts", {}).get("momentum", {}).get("data", [])
    except Exception as e:
        print(f"Errore di connessione a FotMob: {e}")
        momentum_data = []
    
    # Fallback se non ci sono dati live al momento
    if not momentum_data:
        print("Nessun dato di momentum live, genero il grafico di test.")
        minuti = [0, 15, 30, 45, 60, 75, 90]
        valori_onda = [0, 3, -2, 4, 1, -3, 2]
    else:
        minuti = [item.get("minute") for item in momentum_data]
        valori_onda = [item.get("value", 0) for item in momentum_data]

    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10, 4))
    
    x = np.array(minuti)
    y = np.array(valori_onda)
    
    ax.axhline(0, color='#ffffff', linewidth=0.8, linestyle='--', alpha=0.5)
    ax.fill_between(x, y, 0, where=(y >= 0), color='#00E676', alpha=0.6, interpolate=True)
    ax.fill_between(x, y, 0, where=(y < 0), color='#FF5252', alpha=0.6, interpolate=True)
    ax.plot(x, y, color='white', linewidth=1.5)
    ax.axis('off')
    
    plt.tight_layout()
    foto_path = os.path.join(os.path.dirname(__file__), 'momentum_reale.png')
    plt.savefig(foto_path, dpi=300, bbox_inches='tight', transparent=True)
    plt.close(fig)
    plt.close('all')
    return True

# --- CICLO PRINCIPALE ---
if __name__ == "__main__":
    while True:
        if FOTMOB_MATCH_ID:
            print("Tentativo di generazione grafico...")
            successo = genera_grafico_momentum_fotmob(FOTMOB_MATCH_ID)
            if successo:
                print("Grafico generato, invio a Telegram in corso...")
                foto_path = os.path.join(os.path.dirname(__file__), 'momentum_reale.png')
                invia_notifica_telegram(foto_path, "📊 Grafico pressione live (FotMob)")
            else:
                print("Generazione grafico fallita.")
        else:
            print("Attenzione: FOTMOB_MATCH_ID non è configurato nel file JSON.")
                
        time.sleep(180)
