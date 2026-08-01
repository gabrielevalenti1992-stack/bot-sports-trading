import requests
import matplotlib.pyplot as plt
import numpy as np

def genera_grafico_momentum_fotmob(match_id):
    url = f"https://www.fotmob.com/api/matchDetails?matchId={match_id}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print("Errore di connessione ai dati di FotMob.")
        return
    
    data = response.json()
    
    try:
        match_facts = data.get("content", {}).get("matchFacts", {})
        momentum_obj = match_facts.get("momentum", {})
        momentum_data = momentum_obj.get("data", [])
    except Exception as e:
        print(f"Errore nella lettura della struttura dati: {e}")
        return
    
    if not momentum_data:
        print("Dati di pressione non ancora disponibili per questa partita.")
        return

    minuti = []
    valori_onda = []
    
    for item in momentum_data:
        minuto = item.get("minute")
        valore = item.get("value", 0)
        minuti.append(minuto)
        valori_onda.append(valore)

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
    
    print("Grafico 'momentum_reale.png' generato correttamente.")
