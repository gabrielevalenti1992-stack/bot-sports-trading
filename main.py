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

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server, daemon=True)
server_thread.start()

# --- LETTURA CONFIGURAZIONE ---
config_path = os.path.join(os.path.dirname(__file__), 'config.json')
with open(config_path, 'r') as f:
    config = json.load(f)

telegram_bot_token = config.get("telegram_bot_token")
telegram_chat_id = config.get("telegram_chat_id")
fotmob_match_id = config.get("fotmob_match_id")

def invia_notifica_telegram(foto_path, messaggio):
    if foto_path and os.path.exists(foto_path):
        url = f"https://api.telegram.org/bot{telegram_bot_token}/sendPhoto"
        with open(foto_path, 'rb') as photo:
            files = {'photo': photo}
            data = {'chat_id': telegram_chat_id, 'caption': messaggio, 'parse_mode': 'Markdown'}
            response = requests.post(url, data=data, files=files)
            print(f"Risposta Telegram con foto: {response.status_code} - {response.text}")
    else:
        url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
        data = {'chat_id': telegram_chat_id, 'text': messaggio, 'parse_mode': 'Markdown'}
        response = requests.post(url, data=data)
        print(f"Risposta Telegram solo testo: {response.status_code} - {response.text}")

def controlla_partita_fotmob(match_id):
    url = f"https://www.fotmob.com/api/matchDetails?matchId={match_id}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        print(f"Risposta FotMob API: {response.status_code}")
        if response.status_code != 200:
            return None, None
            
        data = response.json()
        
        general = data.get("general", {})
        home_name = general.get("homeTeam", {}).get("name", "Casa")
        away_name = general.get("awayTeam", {}).get("name", "Ospite")
        
        status = general.get("status", {})
        status_str = status.get("reason", {}).get("short", "LIVE")
        
        # Messaggio base con lo stato e i nomi delle squadre (sempre inviato)
        messaggio = (
            f"📊 **Aggiornamento Match Live**\n\n"
            f"⚽ **{home_name} vs {away_name}**\n"
            f"⏱️ Stato: `{status_str}`"
        )
        
        # Tentativo di recuperare il momentum per il grafico
        momentum_data = data.get("content", {}).get("matchFacts", {}).get("momentum", {}).get("data", [])
        
        if not momentum_data:
            print("Momentum non disponibile per questa partita, invio solo il testo di aggiornamento.")
            return None, messaggio

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
        
        messaggio += (
            f"\n\n🟢 **Verde**: Pressione {home_name}\n"
            f"🔴 **Rosso**: Pressione {away_name}"
        )
        
        return foto_path, messaggio

    except Exception as e:
        print(f"Errore durante il controllo della partita: {e}")
        return None, None

# --- CICLO PRINCIPALE ---
if __name__ == "__main__":
    while True:
        if fotmob_match_id:
            print("Controllo dati partita su FotMob...")
            foto, testo = controlla_partita_fotmob(fotmob_match_id)
            if testo:
                print("Invio aggiornamento a Telegram...")
                invia_notifica_telegram(foto, testo)
            else:
                print("Impossibile recuperare i dati della partita.")
        else:
            print("Attenzione: fotmob_match_id non configurato nel file JSON.")
                
        time.sleep(180)
