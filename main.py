import json
import time
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# =============================================================================
# ============================================================
# ANTI-SPAM: tracker partite già notificate
# Chiave = "matchID_golHome_golAway"
# Se il punteggio cambia, la chiave cambia e ri-notifica
# ============================================================
notified_matches = set()

# SERVER HTTP PER RENDER FREE (fix 501 HEAD)
# =============================================================================
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

server_thread = threading.Thread(target=run_server, daemon=True)
server_thread.start()

# =============================================================================
# =============================================================================
# CONFIGURAZIONE
# =============================================================================
# --- TOKEN: solo da Environment Variables (mai su GitHub) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY")

CONFIG_VALIDA = all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, API_FOOTBALL_KEY])
print(f"TOKEN presente: {'SI' if TELEGRAM_BOT_TOKEN else 'NO'}", flush=True)
print(f"CHAT_ID presente: {'SI' if TELEGRAM_CHAT_ID else 'NO'}", flush=True)
print(f"API_KEY presente: {'SI' if API_FOOTBALL_KEY else 'NO'}", flush=True)

if not CONFIG_VALIDA:
    print("CONFIGURAZIONE INCOMPLETA - Impossibile avviare il bot", flush=True)

# --- SOGLIE NOTIFICHE: da config.json (opzionale, fallback a valori default) ---
DIFF_TIRI_SOGLIA = 3
TIRI_TOTALI_ATTIVA = 6
MINUTI_ATTIVA = 25
INTERVALLO_FORZATO = 1800

# Soglie Regola MOMENTUM (ultimi 15 min)
MOMENTUM_TIRI_IN_PORTA = 3
MOMENTUM_TIRI_TOTALI = 5
MOMENTUM_CORNER = 4

try:
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    with open(config_path, 'r') as f:
        config = json.load(f)
    DIFF_TIRI_SOGLIA = config.get("diff_tiri_soglia", DIFF_TIRI_SOGLIA)
    TIRI_TOTALI_ATTIVA = config.get("tiri_totali_attiva", TIRI_TOTALI_ATTIVA)
    MINUTI_ATTIVA = config.get("minuti_attiva", MINUTI_ATTIVA)
    INTERVALLO_FORZATO = config.get("intervallo_forzato", INTERVALLO_FORZATO)
    MOMENTUM_TIRI_IN_PORTA = config.get("momentum_tiri_in_porta", MOMENTUM_TIRI_IN_PORTA)
    MOMENTUM_TIRI_TOTALI = config.get("momentum_tiri_totali", MOMENTUM_TIRI_TOTALI)
    MOMENTUM_CORNER = config.get("momentum_corner", MOMENTUM_CORNER)
    # Nuove soglie per trigger gol/punteggio
    MIN_GOALS_TOTAL = config.get("min_goals_total", 2)
    MIN_GOALS_DIFF = config.get("min_goals_diff", 2)
    MIN_TOTAL_SHOTS_LOW_GOALS = config.get("min_total_shots_low_goals", 8)
    MAX_GOALS_FOR_SHOTS_TRIGGER = config.get("max_goals_for_shots_trigger", 1)
    print(f"Soglie caricate: diff={DIFF_TIRI_SOGLIA}, tot={TIRI_TOTALI_ATTIVA}, min={MINUTI_ATTIVA}, int={INTERVALLO_FORZATO}, gol_min={MIN_GOALS_TOTAL}, gol_diff={MIN_GOALS_DIFF}", flush=True)
except Exception as e:
    print(f"Soglie default (config.json non trovato o errore): {e}", flush=True)

PAROLE_ESCLUSE = [
    "women", "femminile", "female", "u20", "u19", "u18", "u17", "u16", "u15",
    "under-20", "under-19", "under-18", "under-17", "under 20", "under 19",
    "under 18", "under 17", "youth", "amateur", "dilettanti", "regional",
    "reserves", "riserve", "friendlies", "amichevoli", "friendly"
]

stato_partite = {}
ciclo_numero = 0

# =============================================================================
# STATO SILENZIATI (dict con score al momento del silenzio)
# =============================================================================
SILENCED_FILE = "silenced_matches.json"

def load_silenced():
    if os.path.exists(SILENCED_FILE):
        with open(SILENCED_FILE, 'r') as f:
            data = json.load(f)
            if isinstance(data, list):
                return {str(fid): {"score_home": 0, "score_away": 0} for fid in data}
            return data
    return {}

def save_silenced(silenced):
    with open(SILENCED_FILE, 'w') as f:
        json.dump(silenced, f)

SILENCED_MATCHES = load_silenced()

# =============================================================================
# STATO PREFERITI
# =============================================================================
FAVORITES_FILE = "favorite_matches.json"

def load_favorites():
    if os.path.exists(FAVORITES_FILE):
        with open(FAVORITES_FILE, 'r') as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(str(x) for x in data)
            return set()
    return set()

def save_favorites(favs):
    with open(FAVORITES_FILE, 'w') as f:
        json.dump(list(favs), f)

FAVORITE_MATCHES = load_favorites()

# =============================================================================
# FIAMME - SOGLIE
# =============================================================================
FIRE_THRESHOLDS = {
    "Tiri totali":    (2, 4),
    "Tiri in porta":  (2, 3),
    "Corner":         (999, 999),
}

def get_fire_suffix(delta):
    low, high = FIRE_THRESHOLDS.get("Tiri totali", (999, 999))
    if delta >= high:
        return "\U0001F525\U0001F525"
    elif delta >= low:
        return "\U0001F525"
    return ""

def get_fire_suffix_shots(delta):
    low, high = FIRE_THRESHOLDS.get("Tiri in porta", (999, 999))
    if delta >= high:
        return "\U0001F525\U0001F525"
    elif delta >= low:
        return "\U0001F525"
    return ""

def get_fire_suffix_corner(delta):
    return ""

# =============================================================================
# THREAD: ASCOLTA CLICK SUI BOTTONI + COMANDI MANUALI
# =============================================================================
def poll_callbacks():
    offset = 0
    while True:
        if not TELEGRAM_BOT_TOKEN:
            time.sleep(10)
            continue
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            r = requests.get(url, params={"offset": offset, "limit": 10}, timeout=10)
            updates = r.json().get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1

                cq = upd.get("callback_query")
                if cq:
                    data = cq.get("data", "")
                    chat_id = cq["message"]["chat"]["id"]
                    msg_id = cq["message"]["message_id"]

                    if data.startswith("mute:"):
                        fid = str(int(data.split(":")[1]))
                        stato = stato_partite.get(int(fid), {})
                        score_h = stato.get("score_home", 0)
                        score_a = stato.get("score_away", 0)
                        minuto = stato.get("last_minute", 0)
                        SILENCED_MATCHES[fid] = {
                            "score_home": score_h,
                            "score_away": score_a,
                            "muted_at_minute": minuto
                        }
                        save_silenced(SILENCED_MATCHES)
                        invia_messaggio_telegram(f"Partita silenziata al {minuto}'", chat_id)
                    elif data.startswith("unmute:"):
                        fid = str(int(data.split(":")[1]))
                        SILENCED_MATCHES.pop(fid, None)
                        save_silenced(SILENCED_MATCHES)
                        invia_messaggio_telegram(f"Partita riattivata", chat_id)
                    elif data.startswith("fav:"):
                        fid = str(int(data.split(":")[1]))
                        if fid not in FAVORITE_MATCHES:
                            FAVORITE_MATCHES.add(fid)
                            save_favorites(FAVORITE_MATCHES)
                            invia_messaggio_telegram(f"Aggiunto ai preferiti", chat_id)
                        else:
                            FAVORITE_MATCHES.discard(fid)
                            save_favorites(FAVORITE_MATCHES)
                            invia_messaggio_telegram(f"Rimosso dai preferiti", chat_id)

        except Exception as e:
            print(f"Errore poll_callbacks: {e}", flush=True)
            time.sleep(5)

# Avvia thread callback
callback_thread = threading.Thread(target=poll_callbacks, daemon=True)
callback_thread.start()

# =============================================================================
# LOGGING
# =============================================================================
def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# =============================================================================
# API-FOOTBALL
# =============================================================================
def get_partite_live():
    try:
        url = "https://v3.football.api-sports.io/fixtures"
        params = {"live": "all"}
        headers = {"x-apisports-key": API_FOOTBALL_KEY}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json().get("response", [])
        log(f"API error: {r.status_code}")
        return []
    except Exception as e:
        log(f"get_partite_live error: {e}")
        return []

def get_fixture_statistics(fixture_id):
    try:
        url = f"https://v3.football.api-sports.io/fixtures/statistics"
        params = {"fixture": fixture_id}
        headers = {"x-apisports-key": API_FOOTBALL_KEY}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json().get("response", [])
            if data:
                return {
                    "home": data[0].get("statistics", []),
                    "away": data[1].get("statistics", [])
                }
        return None
    except Exception as e:
        log(f"get_fixture_statistics error: {e}")
        return None

def get_fixture_events(fixture_id):
    try:
        url = f"https://v3.football.api-sports.io/fixtures/events"
        params = {"fixture": fixture_id}
        headers = {"x-apisports-key": API_FOOTBALL_KEY}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json().get("response", [])
        return []
    except Exception as e:
        log(f"get_fixture_events error: {e}")
        return []

# =============================================================================
# UTILITÀ
# =============================================================================
def campionato_valido(league_name, league_type):
    if not league_name:
        return False
    league_lower = league_name.lower()
    # Scarta se contiene parole escluse (women, friendly, youth, etc)
    for escluso in PAROLE_ESCLUSE:
        if escluso in league_lower:
            return False
    # Se non contiene parole escluse, accetta (tipo League/Cup/altro non importa)
    return True

def estrai_statistiche(stats_home, stats_away):
    def get_stat(stats, stat_name):
        for s in stats:
            if s.get("type") == stat_name:
                return int(s.get("value", 0)) if s.get("value") else 0
        return 0

    return {
        "Tiri totali": (get_stat(stats_home, "Total Shots"), get_stat(stats_away, "Total Shots")),
        "Tiri in porta": (get_stat(stats_home, "Shots on Goal"), get_stat(stats_away, "Shots on Goal")),
        "Corner": (get_stat(stats_home, "Corner Kicks"), get_stat(stats_away, "Corner Kicks")),
    }

def calcola_delta_15min(fixture_id, current_stats):
    stato = stato_partite.get(fixture_id, {})
    prev_stats = stato.get("prev_stats")
    
    if not prev_stats:
        return current_stats, False
    
    delta = {}
    for key in current_stats:
        delta[key] = (
            current_stats[key][0] - prev_stats[key][0],
            current_stats[key][1] - prev_stats[key][1]
        )
    return delta, True

# =============================================================================
# TELEGRAM
# =============================================================================
def invia_messaggio_telegram(msg, chat_id=None):
    try:
        chat_id = chat_id or TELEGRAM_CHAT_ID
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": msg}
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        log(f"invia_messaggio error: {e}")

def invia_notifica_telegram(photo_path, message, reply_markup=None):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        with open(photo_path, 'rb') as f:
            files = {"photo": f}
            data = {
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": message,
                "parse_mode": "HTML"
            }
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
            requests.post(url, files=files, data=data, timeout=10)
    except Exception as e:
        log(f"invia_notifica error: {e}")

def get_notification_keyboard(fixture_id, is_fav, is_sil):
    buttons = []
    fav_btn = "❌ Rimuovi dai preferiti" if is_fav else "⭐ Aggiungi ai preferiti"
    sil_btn = "🔊 Riattiva" if is_sil else "🔇 Silenzia"
    
    buttons.append([{"text": fav_btn, "callback_data": f"fav:{fixture_id}"}])
    buttons.append([{"text": sil_btn, "callback_data": f"{'unmute' if is_sil else 'mute'}:{fixture_id}"}])
    return {"inline_keyboard": buttons}

# =============================================================================
# GRAFICO
# =============================================================================
def genera_grafico_barre(fixture_id, home_name, away_name, stats):
    try:
        fig, ax = plt.subplots(figsize=(10, 6), facecolor='#1a1a1a')
        ax.set_facecolor('#2a2a2a')

        categories = ['Tiri totali', 'Tiri in porta', 'Corner']
        home_vals = [stats[cat][0] for cat in categories]
        away_vals = [stats[cat][1] for cat in categories]

        x = np.arange(len(categories))
        width = 0.35

        bars1 = ax.barh(x - width/2, home_vals, width, label=home_name, color='#00dd00')
        bars2 = ax.barh(x + width/2, [-v for v in away_vals], width, label=away_name, color='#dd0000')

        ax.set_yticks(x)
        ax.set_yticklabels(categories, color='white')
        ax.set_xlabel('Valore', color='white')
        ax.tick_params(colors='white')
        ax.legend(loc='upper right', facecolor='#2a2a2a', edgecolor='white', labelcolor='white')
        ax.axvline(x=0, color='white', linestyle='-', linewidth=0.8)
        ax.grid(axis='x', alpha=0.3, color='white')

        for bars in [bars1, bars2]:
            for bar in bars:
                height = bar.get_width()
                ax.text(height, bar.get_y() + bar.get_height()/2,
                       f'{int(abs(height))}',
                       ha='left' if height > 0 else 'right',
                       va='center', color='white', fontweight='bold')

        plt.tight_layout()
        path = f"/tmp/grafico_{fixture_id}.png"
        plt.savefig(path, facecolor='#1a1a1a', bbox_inches='tight')
        plt.close()
        return path
    except Exception as e:
        log(f"genera_grafico error: {e}")
        return None

# =============================================================================
# PROCESSA PARTITA
# =============================================================================
def processa_partita(fixture):
    try:
        fixture_id = fixture.get("fixture", {}).get("id")
        if not fixture_id:
            return

        status = fixture.get("fixture", {}).get("status", {}).get("short", "")
        status_long = fixture.get("fixture", {}).get("status", {}).get("long", "")
        minuto = fixture.get("fixture", {}).get("status", {}).get("elapsed", 0) or 0
        
        home = fixture.get("teams", {}).get("home", {}).get("name", "Home")
        away = fixture.get("teams", {}).get("away", {}).get("name", "Away")
        league_name = fixture.get("league", {}).get("name", "")
        league_type = fixture.get("league", {}).get("type", "")
        score_home = fixture.get("goals", {}).get("home", 0)
        score_away = fixture.get("goals", {}).get("away", 0)
        
        if status in ["NS", "PST"]:
            return

        # DEBUG: Partita rilevata
        log(f"[PARTITA RILEVATA] {home} vs {away} | Lega: '{league_name}' | Tipo: '{league_type}' | Minuto: {minuto}'")
        
        # DEBUG: Validazione lega
        is_valid = campionato_valido(league_name, league_type)
        if not is_valid:
            motivo = "Type non League/Cup"
            for escluso in PAROLE_ESCLUSE:
                if escluso in league_name.lower():
                    motivo = f"Parola esclusa: '{escluso}'"
                    break
            log(f"  ❌ Lega SCARTATA - Motivo: {motivo}")
            return
        log(f"  ✅ Lega VALIDA - Monitorata")

        # Registra in stato_partite
        if fixture_id not in stato_partite:
            stato_partite[fixture_id] = {}

        stato = stato_partite[fixture_id]
        stato.update({
            "home": home,
            "away": away,
            "league": league_name,
            "last_minute": minuto,
            "score_home": score_home,
            "score_away": score_away,
            "status": status,
        })

        current_stats = get_fixture_statistics(fixture_id)
        if current_stats:
            stats_dict = estrai_statistiche(current_stats["home"], current_stats["away"])
            tiri_casa, tiri_ospite = stats_dict["Tiri totali"]
            tiri_p_casa, tiri_p_ospite = stats_dict["Tiri in porta"]
            corner_casa, corner_ospite = stats_dict["Corner"]
            log(f"  📊 Statistiche ricevute: Tiri {tiri_casa}-{tiri_ospite} | Porta {tiri_p_casa}-{tiri_p_ospite} | Corner {corner_casa}-{corner_ospite}")
        else:
            stats_dict = {"Tiri totali": (0, 0), "Tiri in porta": (0, 0), "Corner": (0, 0)}
            tiri_casa = tiri_ospite = tiri_p_casa = tiri_p_ospite = corner_casa = corner_ospite = 0
            log(f"  ⚠️ Statistiche NON disponibili da API (potrebbe essere lega non supportata o piano API non include stats)")

        # Conta gol via events
        events = get_fixture_events(fixture_id)
        goals = [e for e in events if e.get("type") == "Goal"]
        if goals:
            log(f"  ⚽ Gol trovati: {len(goals)}")
        else:
            log(f"  ⚽ Nessun gol registrato")

        status_short = "1H" if minuto < 45 else "2H" if minuto < 90 else "ET" if minuto < 120 else "P"

        # ============================================================
        # VERIFICA SILENZIO
        # ============================================================
        is_silenced = str(fixture_id) in SILENCED_MATCHES
        if is_silenced:
            muted_data = SILENCED_MATCHES.get(str(fixture_id), {})
            muted_score_h = muted_data.get("score_home", 0)
            muted_score_a = muted_data.get("score_away", 0)
            if score_home == muted_score_h and score_away == muted_score_a:
                muted_data["muted_at_minute"] = minuto
                save_silenced(SILENCED_MATCHES)
            log(f"  -> Silenziata, skip")
            return

        if current_stats:
            delta_stats, is_real_delta = calcola_delta_15min(fixture_id, stats_dict)
            stats_dict = delta_stats
            header_stats = "Statistiche ultimi 15 min" if is_real_delta else "Primo rilevamento"
        else:
            stats_dict = {"Tiri totali": (0, 0), "Tiri in porta": (0, 0), "Corner": (0, 0)}
            header_stats = "Statistiche"

        log(f"  Tiri: {tiri_casa}-{tiri_ospite} | Porta: {tiri_p_casa}-{tiri_p_ospite} | Corner: {corner_casa}-{corner_ospite}")
        log(f"  Delta 15min: {stats_dict}")

        # ============================================================
        # 4 TRIGGER DI NOTIFICA + ANTI-SPAM
        # ============================================================
        total_goals = score_home + score_away
        goal_diff = abs(score_home - score_away)
        total_shots_all = tiri_casa + tiri_ospite
        notify_key = f"{fixture_id}_{score_home}_{score_away}"

        # Trigger A: Delta 15min (momentum)
        d_tiri = stats_dict.get("Tiri totali", (0, 0))
        d_porta = stats_dict.get("Tiri in porta", (0, 0))
        d_corner = stats_dict.get("Corner", (0, 0))
        trigger_stats = (
            (d_tiri[0] + d_tiri[1]) >= MOMENTUM_TIRI_TOTALI or
            (d_porta[0] + d_porta[1]) >= MOMENTUM_TIRI_IN_PORTA or
            (d_corner[0] + d_corner[1]) >= MOMENTUM_CORNER
        )

        # Trigger B: Almeno N gol totali
        trigger_goals = total_goals >= MIN_GOALS_TOTAL

        # Trigger C: Tiri totali alti ma pochi gol
        trigger_shots_low_goals = (
            total_shots_all >= MIN_TOTAL_SHOTS_LOW_GOALS and
            total_goals <= MAX_GOALS_FOR_SHOTS_TRIGGER
        )

        # Trigger D: Differenza gol >= N
        trigger_diff = goal_diff >= MIN_GOALS_DIFF

        # DEBUG: Verifica trigger
        log(f"  🎯 Analisi trigger:")
        log(f"     - Gol totali: {total_goals} (soglia: {MIN_GOALS_TOTAL}) → trigger_goals={trigger_goals}")
        log(f"     - Differenza gol: {goal_diff} (soglia: {MIN_GOALS_DIFF}) → trigger_diff={trigger_diff}")
        log(f"     - Tiri totali: {total_shots_all} (soglia: {MIN_TOTAL_SHOTS_LOW_GOALS}) + gol ≤{MAX_GOALS_FOR_SHOTS_TRIGGER} → trigger_shots_low_goals={trigger_shots_low_goals}")
        log(f"     - Stats 15min: Tiri={d_tiri[0]+d_tiri[1]} (soglia {MOMENTUM_TIRI_TOTALI}), Porta={d_porta[0]+d_porta[1]} (soglia {MOMENTUM_TIRI_IN_PORTA}), Corner={d_corner[0]+d_corner[1]} (soglia {MOMENTUM_CORNER}) → trigger_stats={trigger_stats}")

        # Determina motivo
        alert_reason = None
        if trigger_stats:
            alert_reason = f"Stats 15min: Tiri {d_tiri[0]+d_tiri[1]}, Porta {d_porta[0]+d_porta[1]}, Corner {d_corner[0]+d_corner[1]}"
        elif trigger_goals:
            alert_reason = f"Partita golosa: {score_home}-{score_away} al {minuto}'"
        elif trigger_shots_low_goals:
            alert_reason = f"Tiri alti ({total_shots_all}) ma pochi gol ({total_goals}) — possibile Over"
        elif trigger_diff:
            alert_reason = f"Differenza gol {goal_diff}: {score_home}-{score_away} al {minuto}'"

        # Decisione finale
        if alert_reason and notify_key not in notified_matches:
            log(f"  ✅ NOTIFICA INVIATA - Motivo: {alert_reason}")
            pass  # continua sotto per inviare il messaggio
        elif alert_reason and notify_key in notified_matches:
            log(f"  ⏭️ Già notificata questo punteggio: {home} vs {away}")
            return
        else:
            prev_notified = stato_partite.get(fixture_id, {}).get("notified_final", False)
            stato_partite[fixture_id].update({
                "tiri_casa": tiri_casa,
                "tiri_ospite": tiri_ospite,
                "timestamp_notifica": stato_partite[fixture_id].get("timestamp_notifica", 0),
                "notified_final": prev_notified,
            })
            log(f"  ⏭️ Nessun trigger attivo - Skip")
            return

        foto_path = genera_grafico_barre(fixture_id, home, away, current_stats if current_stats else stats_dict)

        diff = stats_dict["Tiri totali"][0] - stats_dict["Tiri totali"][1]
        freccia = "CASA" if diff > 0 else "OSP" if diff < 0 else "EQ"

        d_tiri_c = stats_dict["Tiri totali"][0]
        d_tiri_o = stats_dict["Tiri totali"][1]
        d_porta_c = stats_dict["Tiri in porta"][0]
        d_porta_o = stats_dict["Tiri in porta"][1]
        d_corner_c = stats_dict["Corner"][0]
        d_corner_o = stats_dict["Corner"][1]

        fire_t_c = get_fire_suffix(d_tiri_c)
        fire_t_o = get_fire_suffix(d_tiri_o)
        fire_p_c = get_fire_suffix_shots(d_porta_c)
        fire_p_o = get_fire_suffix_shots(d_porta_o)

        goals_text = ""
        if goals:
            try:
                primo_minuto = goals[0].get('time', {}).get('elapsed') or goals[0].get('minute', '?')
                primo_player = goals[0].get('player', {}).get('name') or goals[0].get('player', '?')
                goals_text += f"\nPrimo gol: {primo_minuto}' ({primo_player})\n"
                if len(goals) > 1:
                    ultimo_minuto = goals[-1].get('time', {}).get('elapsed') or goals[-1].get('minute', '?')
                    ultimo_player = goals[-1].get('player', {}).get('name') or goals[-1].get('player', '?')
                    goals_text += f"Ultimo gol: {ultimo_minuto}' ({ultimo_player})\n"
            except Exception as e:
                log(f"  ⚠️ Errore parsing gol: {e}")
                goals_text = "\n⚽ Gol registrati ma dettagli non disponibili\n"

        messaggio = (
            f"{home} vs {away}\n"
            f"{league_name}\n"
            f"Minuto: {minuto}' | Stato: {status_short}\n\n"
            f"Risultato: {score_home} - {score_away}\n"
            f"{goals_text}\n"
            f"{header_stats}:\n"
            f"- Tiri totali: {stats_dict['Tiri totali'][0]}{fire_t_c} - {stats_dict['Tiri totali'][1]}{fire_t_o} {freccia}\n"
            f"- Tiri in porta: {stats_dict['Tiri in porta'][0]}{fire_p_c} - {stats_dict['Tiri in porta'][1]}{fire_p_o}\n"
            f"- Corner: {stats_dict['Corner'][0]} - {stats_dict['Corner'][1]}\n\n"
            f"Verde = {home}\n"
            f"Rosso = {away}"
        )

        is_fav = str(fixture_id) in FAVORITE_MATCHES
        is_sil = str(fixture_id) in SILENCED_MATCHES
        keyboard = get_notification_keyboard(fixture_id, is_fav, is_sil)
        invia_notifica_telegram(foto_path, messaggio, reply_markup=keyboard)
        # Anti-spam: marca questa partita+score come notificata
        notified_matches.add(notify_key)

        prev_notified = stato_partite.get(fixture_id, {}).get("notified_final", False)
        stato_partite[fixture_id].update({
            "tiri_casa": tiri_casa,
            "tiri_ospite": tiri_ospite,
            "timestamp_notifica": time.time(),
            "notified_final": prev_notified,
        })

        if foto_path and os.path.exists(foto_path):
            try:
                os.remove(foto_path)
            except:
                pass

    except Exception as e:
        import traceback
        log(f"❌ ERRORE processa_partita: {e}")
        log(f"   Traceback: {traceback.format_exc()}")


def pulisci_partite_terminate(fixture_ids_live):
    ids_da_rimuovere = [fid for fid in stato_partite if fid not in fixture_ids_live]
    for fid in ids_da_rimuovere:
        stato = stato_partite.get(fid, {})
        # Notifica finale solo se la partita era stata notificata in precedenza
        if not stato.get("notified_final") and stato.get("timestamp_notifica", 0) > 0:
            home = stato.get("home", "Squadra A")
            away = stato.get("away", "Squadra B")
            invia_messaggio_telegram(f"{home} vs {away}\nRisultato finale: partita terminata.")

        SILENCED_MATCHES.pop(str(fid), None)
        del stato_partite[fid]

    if ids_da_rimuovere:
        save_silenced(SILENCED_MATCHES)
        log(f"Partite terminate rimosse: {len(ids_da_rimuovere)}")


# =============================================================================
# CICLO PRINCIPALE
# =============================================================================
if __name__ == "__main__":
    log("=== Bot avviato ===")
    invia_messaggio_telegram("Bot avviato\nMonitoraggio partite live in corso...")

    while True:
        if not CONFIG_VALIDA:
            log("CONFIGURAZIONE INCOMPLETA - Attendo 30 secondi...")
            time.sleep(30)
            continue

        ciclo_numero += 1
        log(f"\n=== Ciclo #{ciclo_numero} - {time.strftime('%H:%M:%S')} ===")

        partite = get_partite_live()
        partite_valide = [
            f for f in partite
            if campionato_valido(
                f.get("league", {}).get("name", ""),
                f.get("league", {}).get("type", "")
            )
        ]
        scartate = len(partite) - len(partite_valide)
        log(f"📊 CICLO PARTITE: {len(partite)} totali | {len(partite_valide)} valide | {scartate} scartate")
        if scartate > 0 and len(partite) <= 10:
            for f in partite:
                if not campionato_valido(f.get("league", {}).get("name", ""), f.get("league", {}).get("type", "")):
                    h = f.get("teams", {}).get("home", {}).get("name", "?")
                    a = f.get("teams", {}).get("away", {}).get("name", "?")
                    l = f.get("league", {}).get("name", "?")
                    log(f"   ❌ Scartata: {h} vs {a} ({l})")

        if ciclo_numero == 1 or ciclo_numero % 10 == 0:
            invia_messaggio_telegram(
                f"Bot attivo - Ciclo #{ciclo_numero}\n"
                f"Partite live: {len(partite)} totali, {len(partite_valide)} monitorate"
            )

        fixture_ids_live = set()
        for fixture in partite:
            fixture_id = fixture.get("fixture", {}).get("id")
            if fixture_id:
                fixture_ids_live.add(fixture_id)
            processa_partita(fixture)
            time.sleep(1)

        pulisci_partite_terminate(fixture_ids_live)
        log("Attesa 3 minuti...")
        time.sleep(180)