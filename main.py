import json
import time
import requests
import matplotlib.pyplot as plt
import numpy as np
import os

# Legge le configurazioni dal file config.json
with open('config.json', 'r') as f:
    config = json.load(f)

TELEGRAM_BOT_TOKEN = config.get("telegram_bot_token")
TELEGRAM_CHAT_ID = config.get("telegram_chat_id")
API_FOOTBALL_KEY = config.get("api_football_key")
FOTMOB_MATCH_ID = config.get("fotmob_match_id")

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
    
    response = requests.get(url, headers=headers)
    print(f"Risposta FotMob API: {response.status_code}")
    if response.status_code != 200:
        return False
    
    data = response.json()
    try:
        momentum_data = data.get("content", {}).get("matchFacts", {}).get("momentum", {}).get("data", [])
    except Exception as e:
        print(f"Errore nel parsing del JSON di FotMob: {e}")
        momentum_data = []
    
    # Se FotMob non restituisce dati di momentum (es. partita finita o non iniziata), 
    # generiamo comunque un grafico di fallback per testare l'invio della foto su Telegram
    if not momentum_data:
        print("Nessun dato di momentum trovato per questo match_id, genero un grafico di fallback.")
        minuti = [0, 15, 30, 45, 60, 75, 90]
        valori_onda = [0, 2, -1, 3, 0, -2, 1]
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
    plt.savefig('momentum_reale.png', dpi=300, bbox_inches='tight', transparent=True)
    plt.close()
    return True

# --- CICLO PRINCIPALE ---
if __name__ == "__main__":
    notifica_inviata = False
    
    while True:
        if FOTMOB_MATCH_ID:
            successo = genera_grafico_momentum_fotmob(FOTMOB_MATCH_ID)
            
            if successo and not notifica_inviata:
                invia_notifica_telegram('momentum_reale.png', "📊 Grafico pressione live (FotMob)")
                notifica_inviata = True
                
        time.sleep(60)
