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

# Filtro leghe con statistiche note (per evitare notifiche su campionati minori senza dati API)
SOLO_LEGHE_CON_STATISTICHE = True
LEGHE_CON_STATISTICHE = [
    "Premier League", "La Liga", "Serie A", "Bundesliga", "Ligue 1",
    "Eredivisie", "Primeira Liga", "Championship",
    "Champions League", "Europa League", "Conference League",
    "World Cup", "Euro Championship", "Copa America", "Copa Libertadores"
]

# Cache dinamica delle leghe con statistiche coperte, ricavata dall'API /leagues.
# Usata al posto di LEGHE_CON_STATISTICHE quando disponibile; quest'ultima resta come fallback
# se l'API non risponde (es. all'avvio o in caso di errore).
LEGHE_ATTIVE_CACHE = set()
LEGHE_ATTIVE_TIMESTAMP = 0
LEGHE_ATTIVE_TTL = 43200  # 12 ore

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
    SOLO_LEGHE_CON_STATISTICHE = config.get("solo_leghe_con_statistiche", SOLO_LEGHE_CON_STATISTICHE)
    LEGHE_CON_STATISTICHE = config.get("leghe_con_statistiche", LEGHE_CON_STATISTICHE)
    print(f"Soglie caricate da config.json: diff={DIFF_TIRI_SOGLIA}, tot={TIRI_TOTALI_ATTIVA}, min={MINUTI_ATTIVA}, int={INTERVALLO_FORZATO}", flush=True)
    print(f"Filtro leghe con statistiche: {'ATTIVO' if SOLO_LEGHE_CON_STATISTICHE else 'disattivo'} ({len(LEGHE_CON_STATISTICHE)} leghe in whitelist)", flush=True)
except Exception as e:
    print(f"Soglie default (config.json non trovato o errore): {e}", flush=True)

PAROLE_ESCLUSE = [
    "women", "femminile", "female", "u20", "u19", "u18", "u17", "u16", "u15",
    "under-20", "under-19", "under-18", "under-17", "under 20", "under 19",
    "under 18", "under 17", "youth", "amateur", "dilettanti", "regional",
    "reserves", "riserve"
    # TEST TEMPORANEO: "friendlies", "amichevoli", "friendly" rimossi per verificare grafici/notifiche
    # Ripristinare dopo il test!
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
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                            json={"callback_query_id": cq["id"]}, timeout=5)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
                            json={
                                "chat_id": chat_id,
                                "message_id": msg_id,
                                "reply_markup": json.dumps({"inline_keyboard": []})
                            }, timeout=5)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            json={
                                "chat_id": chat_id,
                                "text": "\U0001F515 Partita silenziata. Non riceverai piu alert live. Il risultato finale arrivera comunque.",
                                "parse_mode": "Markdown"
                            }, timeout=5)

                    elif data.startswith("fav:"):
                        fid = str(int(data.split(":")[1]))
                        if fid in FAVORITE_MATCHES:
                            FAVORITE_MATCHES.discard(fid)
                            text = "Rimossa dai preferiti"
                        else:
                            FAVORITE_MATCHES.add(fid)
                            text = "Aggiunta ai preferiti"
                        save_favorites(FAVORITE_MATCHES)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                            json={"callback_query_id": cq["id"], "text": text}, timeout=5)
                        is_fav = fid in FAVORITE_MATCHES
                        is_sil = fid in SILENCED_MATCHES
                        keyboard = get_notification_keyboard(int(fid), is_fav, is_sil)
                        if keyboard:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
                                json={
                                    "chat_id": chat_id,
                                    "message_id": msg_id,
                                    "reply_markup": json.dumps(keyboard)
                                }, timeout=5)

                    elif data.startswith("cmd:"):
                        azione = data.split(":", 1)[1]
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                            json={"callback_query_id": cq["id"]}, timeout=5)
                        if azione == "live":
                            cmd_live(chat_id)
                        elif azione == "favorites":
                            cmd_favorites(chat_id)
                        elif azione == "clearfavorites":
                            cmd_clearfavorites(chat_id)
                        elif azione == "silenced":
                            cmd_silenced(chat_id)
                        elif azione == "leghestats":
                            cmd_leghestats(chat_id)
                        elif azione == "help":
                            cmd_help(chat_id)

                msg = upd.get("message")
                if msg and msg.get("text"):
                    text = msg["text"].strip()
                    chat_id = msg["chat"]["id"]
                    parts = text.split()
                    cmd = parts[0].lower()
                    args = parts[1:] if len(parts) > 1 else []

                    if cmd == "/help":
                        cmd_help(chat_id)

                    elif cmd == "/setup":
                        cmd_setup(chat_id)

                    elif cmd == "/status":
                        if not args:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": "Usa: /status <nome squadra>", "parse_mode": "Markdown"}, timeout=5)
                            continue
                        cmd_status(chat_id, " ".join(args).lower().strip("<>").strip())

                    elif cmd == "/statstypes":
                        if not args:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": "Usa: /statstypes <nome squadra>", "parse_mode": "Markdown"}, timeout=5)
                            continue
                        cmd_statstypes(chat_id, " ".join(args).lower().strip("<>").strip())

                    elif cmd == "/favorites":
                        cmd_favorites(chat_id)

                    elif cmd == "/clearfavorites":
                        cmd_clearfavorites(chat_id)

                    elif cmd == "/silenced":
                        cmd_silenced(chat_id)

                    elif cmd == "/test":
                        try:
                            stats_test = {
                                "Tiri totali": (5, 3),
                                "Tiri in porta": (2, 1),
                                "Corner": (3, 2),
                            }
                            foto_test = genera_grafico_barre("test", "Squadra Test A", "Squadra Test B", stats_test)
                            messaggio_test = (
                                "🧪 NOTIFICA DI TEST\n\n"
                                "Squadra Test A vs Squadra Test B\n"
                                "Se ricevi questo messaggio con il grafico, "
                                "la consegna Telegram funziona correttamente.\n\n"
                                "Il problema (se persiste) è nella logica dei trigger, non nella consegna."
                            )
                            invia_notifica_telegram(foto_test, messaggio_test)
                            if foto_test and os.path.exists(foto_test):
                                try:
                                    os.remove(foto_test)
                                except:
                                    pass
                        except Exception as e:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": f"Errore test: {e}"}, timeout=5)

                    elif cmd == "/live":
                        cmd_live(chat_id)

                    elif cmd == "/leghestats":
                        cmd_leghestats(chat_id)
        except Exception as e:
            log(f"Errore poll callback: {e}")
        time.sleep(5)

callback_thread = threading.Thread(target=poll_callbacks, daemon=True)
callback_thread.start()

# =============================================================================
# FUNZIONI UTILITY
# =============================================================================
def log(msg):
    print(msg, flush=True)


def invia_messaggio_telegram(testo):
    if not CONFIG_VALIDA:
        log(f"[SKIP Telegram] Config mancante: {testo[:50]}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {'chat_id': TELEGRAM_CHAT_ID, 'text': testo, 'parse_mode': 'Markdown'}
        response = requests.post(url, data=data, timeout=10)
        log(f"Telegram testo - Status: {response.status_code} - {response.text[:100]}")
    except Exception as e:
        log(f"Errore invio testo Telegram: {e}")


def invia_notifica_telegram(foto_path, messaggio, reply_markup=None):
    if not CONFIG_VALIDA:
        log(f"[SKIP Telegram] Config mancante: {messaggio[:50]}")
        return
    try:
        if foto_path and os.path.exists(foto_path):
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            with open(foto_path, 'rb') as photo:
                files = {'photo': photo}
                data = {
                    'chat_id': TELEGRAM_CHAT_ID,
                    'caption': messaggio,
                    'parse_mode': 'Markdown'
                }
                if reply_markup:
                    data['reply_markup'] = json.dumps(reply_markup)
                response = requests.post(url, data=data, files=files, timeout=10)
                log(f"Telegram foto - Status: {response.status_code}")
        else:
            invia_messaggio_telegram(messaggio)
    except Exception as e:
        log(f"Errore invio Telegram: {e}")


def campionato_valido(league_name, league_type, league_country=""):
    nome = league_name.lower()
    for parola in PAROLE_ESCLUSE:
        if parola in nome:
            return False
    if league_type and league_type.lower() not in ["league", "cup", "championship"]:
        return False
    if SOLO_LEGHE_CON_STATISTICHE:
        aggiorna_leghe_attive()
        if LEGHE_ATTIVE_CACHE:
            if (league_country.lower(), nome) not in LEGHE_ATTIVE_CACHE:
                return False
        elif not any(lega.lower() in nome for lega in LEGHE_CON_STATISTICHE):
            return False
    return True


def get_partite_live():
    if not API_FOOTBALL_KEY:
        log("API_FOOTBALL_KEY mancante, skip get_partite_live")
        return []
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"live": "all"}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        log(f"API-Football status: {response.status_code}")
        if response.status_code != 200:
            invia_messaggio_telegram(f"Errore API\nHTTP {response.status_code}")
            return []
        data = response.json()
        errori = data.get("errors", {})
        if errori:
            invia_messaggio_telegram(f"Errore API\n{errori}")
            return []
        return data.get("response", [])
    except Exception as e:
        log(f"Errore get_partite_live: {e}")
        invia_messaggio_telegram(f"Eccezione API\n{e}")
        return []


def get_leghe_con_copertura_statistiche_raw():
    """Interroga /leagues e restituisce coppie (paese, nome lega) con copertura statistiche nella stagione corrente."""
    if not API_FOOTBALL_KEY:
        return []
    url = "https://v3.football.api-sports.io/leagues"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"current": "true"}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)
        if response.status_code != 200:
            log(f"Errore /leagues: HTTP {response.status_code}")
            return []
        data = response.json()
        risultati = []
        for item in data.get("response", []):
            league = item.get("league", {})
            country = item.get("country", {})
            for season in item.get("seasons", []):
                if not season.get("current"):
                    continue
                coverage = season.get("coverage", {}) or {}
                fixtures_cov = coverage.get("fixtures", {}) or {}
                stats_ok = bool(fixtures_cov.get("statistics_fixtures", fixtures_cov.get("statistics", False)))
                if stats_ok:
                    nome = league.get("name", "?")
                    paese = country.get("name", "?")
                    risultati.append((paese, nome))
        return sorted(set(risultati))
    except Exception as e:
        log(f"Errore get_leghe_con_copertura_statistiche_raw: {e}")
        return []


def get_leghe_con_copertura_statistiche():
    """Versione per /leghestats: righe 'Paese - Nome lega' pronte da mostrare."""
    return [f"{paese} - {nome}" for paese, nome in get_leghe_con_copertura_statistiche_raw()]


def aggiorna_leghe_attive(force=False):
    """Aggiorna la cache dinamica delle leghe con statistiche coperte (usata da campionato_valido).
    Se l'API non risponde o non restituisce nulla, la cache precedente resta valida (fallback)."""
    global LEGHE_ATTIVE_CACHE, LEGHE_ATTIVE_TIMESTAMP
    now = time.time()
    if not force and LEGHE_ATTIVE_CACHE and (now - LEGHE_ATTIVE_TIMESTAMP) < LEGHE_ATTIVE_TTL:
        return
    raw = get_leghe_con_copertura_statistiche_raw()
    if raw:
        LEGHE_ATTIVE_CACHE = set((paese.lower(), nome.lower()) for paese, nome in raw)
        LEGHE_ATTIVE_TIMESTAMP = now
        log(f"Whitelist leghe aggiornata dinamicamente: {len(LEGHE_ATTIVE_CACHE)} campionati con statistiche coperte")
    else:
        log("Whitelist leghe non aggiornata (API vuota o errore) - mantengo cache/fallback precedente")
    return raw


def get_statistiche_partita(fixture_id, debug=False):
    if not API_FOOTBALL_KEY:
        return None
    url = "https://v3.football.api-sports.io/fixtures/statistics"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"fixture": fixture_id}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if debug:
            log(f"    [DEBUG stats {fixture_id}] HTTP {response.status_code} - body: {response.text[:1500]}")
        if response.status_code != 200:
            return None
        data = response.json()
        return data.get("response", [])
    except Exception as e:
        log(f"Errore statistiche {fixture_id}: {e}")
        return None


def fetch_fixture_events(fixture_id):
    if not API_FOOTBALL_KEY:
        return []
    url = "https://v3.football.api-sports.io/fixtures/events"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"fixture": fixture_id}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        return response.json().get("response", [])
    except Exception as e:
        log(f"Errore eventi {fixture_id}: {e}")
        return []


def extract_goals(events):
    goals = []
    for ev in events:
        if ev.get("type") == "Goal":
            goals.append({
                "minute": ev["time"]["elapsed"],
                "player": (ev.get("player") or {}).get("name") or "Sconosciuto",
                "team": ev["team"]["name"]
            })
    goals.sort(key=lambda g: g["minute"])
    return goals


def estrai_valore_stat(stats_team, nome_stat):
    for stat in stats_team:
        if stat.get("type", "").lower() == nome_stat.lower():
            val = stat.get("value")
            if val is None:
                return 0
            try:
                return int(val)
            except:
                return 0
    return 0


def ha_statistiche_disponibili(stats):
    """True se l'API ha restituito dati statistici reali (non solo liste vuote/nulle) per entrambe le squadre."""
    if not stats or len(stats) < 2:
        return False
    stats_home = stats[0].get("statistics", []) or []
    stats_away = stats[1].get("statistics", []) or []
    if not stats_home or not stats_away:
        return False
    return any(s.get("value") is not None for s in stats_home + stats_away)


# =============================================================================
# COMANDI TELEGRAM (funzioni riutilizzabili da testo e da bottoni inline)
# =============================================================================
def cmd_help(chat_id):
    help_text = (
        "Comandi disponibili:\n"
        "/help - Mostra questo messaggio\n"
        "/status <squadra> - Info live su una partita\n"
        "/statstypes <squadra> - Tipi di statistiche disponibili da API (diagnostica)\n"
        "/favorites - Lista partite preferite\n"
        "/clearfavorites - Svuota lista preferiti\n"
        "/silenced - Lista partite silenziate\n"
        "/live - Mostra tutte le partite live (✅✅ = statistiche disponibili)\n"
        "/leghestats - Elenco campionati con statistiche coperte da API-Football\n"
        "/setup - Menu comandi a bottoni"
    )
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": help_text, "parse_mode": "Markdown"}, timeout=5)


def cmd_favorites(chat_id):
    if not FAVORITE_MATCHES:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Nessuna partita preferita.", "parse_mode": "Markdown"}, timeout=5)
        return
    lines = ["Partite preferite:"]
    partite_cmd = get_partite_live()
    live_map = {str(f["fixture"]["id"]): f for f in partite_cmd}
    for fid in FAVORITE_MATCHES:
        f = live_map.get(fid)
        if f:
            home = f["teams"]["home"]["name"]
            away = f["teams"]["away"]["name"]
            minute = f["fixture"]["status"].get("elapsed", "?")
            lines.append(f"- {home} vs {away} ({minute}')")
        else:
            lines.append(f"- ID {fid} (non live)")
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "Markdown"}, timeout=5)


def cmd_clearfavorites(chat_id):
    FAVORITE_MATCHES.clear()
    save_favorites(FAVORITE_MATCHES)
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "Lista preferiti svuotata.", "parse_mode": "Markdown"}, timeout=5)


def cmd_silenced(chat_id):
    if not SILENCED_MATCHES:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Nessuna partita silenziata.", "parse_mode": "Markdown"}, timeout=5)
        return
    lines = ["Partite silenziate:"]
    for fid, info in SILENCED_MATCHES.items():
        lines.append(f"- ID {fid} al {info.get('muted_at_minute','?')}'")
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "Markdown"}, timeout=5)


def cmd_live(chat_id):
    partite_cmd_raw = get_partite_live()
    partite_cmd = [
        f for f in partite_cmd_raw
        if campionato_valido(
            f.get("league", {}).get("name", ""),
            f.get("league", {}).get("type", ""),
            f.get("league", {}).get("country", "")
        )
    ]
    if not partite_cmd:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Nessuna partita live monitorata al momento.", "parse_mode": "Markdown"}, timeout=5)
        return
    MAX_PARTITE_MOSTRATE = 20
    header = f"Partite live monitorate: {len(partite_cmd)} (su {len(partite_cmd_raw)} totali)"
    match_lines = []
    n_con_dati = 0
    for f in partite_cmd[:MAX_PARTITE_MOSTRATE]:
        fid = f["fixture"]["id"]
        home = f["teams"]["home"]["name"]
        away = f["teams"]["away"]["name"]
        league = f["league"]["name"]
        minute = f["fixture"]["status"].get("elapsed", "?")
        score_h = f["goals"]["home"] or 0
        score_a = f["goals"]["away"] or 0

        stats_live = get_statistiche_partita(fid)
        dati_ok = ha_statistiche_disponibili(stats_live)
        segnale = " ✅✅" if dati_ok else ""
        if dati_ok:
            n_con_dati += 1
        log(f"  /live check: {home} vs {away} (id {fid}) - statistiche {'DISPONIBILI' if dati_ok else 'assenti'}")

        match_lines.append(f"- {home} {score_h}-{score_a} {away} ({league}, {minute}'){segnale}")
        time.sleep(0.3)

    n_mostrate = len(match_lines)
    lines = [header] + match_lines
    if len(partite_cmd) > n_mostrate:
        lines.append(f"\n... e altre {len(partite_cmd) - n_mostrate} partite non mostrate")
    lines.append(f"\n✅✅ = statistiche disponibili ({n_con_dati}/{n_mostrate} mostrate)")
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "Markdown"}, timeout=5)


def cmd_leghestats(chat_id):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "Recupero elenco campionati con statistiche coperte da API-Football...", "parse_mode": "Markdown"}, timeout=5)
    raw = aggiorna_leghe_attive(force=True) or []
    leghe = [f"{paese} - {nome}" for paese, nome in raw]
    if not leghe:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Nessun campionato trovato o errore chiamando /leagues.", "parse_mode": "Markdown"}, timeout=5)
        return
    testo = (
        f"Campionati con statistiche coperte (stagione corrente): {len(leghe)}\n"
        f"Questa lista è ora usata direttamente come filtro per /live e le notifiche.\n\n"
    )
    for riga in leghe:
        linea = f"- {riga}\n"
        if len(testo) + len(linea) > 3800:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": testo, "parse_mode": "Markdown"}, timeout=10)
            testo = ""
        testo += linea
    if testo:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": testo, "parse_mode": "Markdown"}, timeout=10)


def cmd_status(chat_id, query):
    partite_cmd = get_partite_live()
    trovate = []
    for f in partite_cmd:
        home = f.get("teams", {}).get("home", {}).get("name", "").lower()
        away = f.get("teams", {}).get("away", {}).get("name", "").lower()
        if query in home or query in away or home in query or away in query:
            trovate.append(f)
    if not trovate:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": f"Nessuna partita live trovata per '{query}'", "parse_mode": "Markdown"}, timeout=5)
        return
    for f in trovate:
        fid = f["fixture"]["id"]
        home = f["teams"]["home"]["name"]
        away = f["teams"]["away"]["name"]
        minuto = f["fixture"]["status"].get("elapsed") or 0
        score_h = f["goals"]["home"] or 0
        score_a = f["goals"]["away"] or 0
        stats = get_statistiche_partita(fid)
        stats_text = ""
        if stats and len(stats) >= 2:
            sh = stats[0].get("statistics", [])
            sa = stats[1].get("statistics", [])
            tc = estrai_valore_stat(sh, "Total Shots")
            to = estrai_valore_stat(sa, "Total Shots")
            tp = estrai_valore_stat(sh, "Shots on Goal")
            tpo = estrai_valore_stat(sa, "Shots on Goal")
            cc = estrai_valore_stat(sh, "Corner Kicks")
            co = estrai_valore_stat(sa, "Corner Kicks")
            stats_text = f"\nStats: Tiri {tc}-{to} | Porta {tp}-{tpo} | Corner {cc}-{co}"
        events = fetch_fixture_events(fid)
        goals = extract_goals(events)
        last_text = ""
        if goals:
            last_text = f"\nUltimo gol: {goals[-1]['minute']}' ({goals[-1]['player']})"
        msg_text = f"{home} vs {away}\n{minuto}' | {score_h}-{score_a}{last_text}{stats_text}"
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": msg_text, "parse_mode": "Markdown"}, timeout=5)


def cmd_statstypes(chat_id, query):
    """Diagnostica: mostra tutti i 'type' di statistiche che l'API restituisce per una partita live,
    per verificare se sono coperti Shots insidebox / expected_goals (xG) sul piano attuale."""
    partite_cmd = get_partite_live()
    trovate = []
    for f in partite_cmd:
        home = f.get("teams", {}).get("home", {}).get("name", "").lower()
        away = f.get("teams", {}).get("away", {}).get("name", "").lower()
        if query in home or query in away or home in query or away in query:
            trovate.append(f)
    if not trovate:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": f"Nessuna partita live trovata per '{query}'", "parse_mode": "Markdown"}, timeout=5)
        return
    for f in trovate:
        fid = f["fixture"]["id"]
        home = f["teams"]["home"]["name"]
        away = f["teams"]["away"]["name"]
        stats = get_statistiche_partita(fid, debug=True)
        if not stats or len(stats) < 2:
            testo = f"{home} vs {away}\nNessuna statistica disponibile da API per questa partita."
        else:
            tipi_home = [s.get("type") for s in stats[0].get("statistics", [])]
            tipi_away = [s.get("type") for s in stats[1].get("statistics", [])]
            tipi = sorted(set(tipi_home) | set(tipi_away))
            ha_insidebox = any("insidebox" in (t or "").lower() for t in tipi)
            ha_xg = any("expected" in (t or "").lower() or t == "xG" for t in tipi)
            testo = (
                f"{home} vs {away}\n"
                f"Tipi di statistiche restituiti dall'API:\n"
                + "\n".join(f"- {t}" for t in tipi)
                + f"\n\nShots insidebox: {'SI' if ha_insidebox else 'NO'}"
                + f"\nexpected_goals (xG): {'SI' if ha_xg else 'NO'}"
            )
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": testo, "parse_mode": "Markdown"}, timeout=5)


def cmd_setup(chat_id):
    keyboard = {
        "inline_keyboard": [
            [{"text": "📡 Live", "callback_data": "cmd:live"}],
            [{"text": "⭐ Preferiti", "callback_data": "cmd:favorites"},
             {"text": "🗑 Svuota preferiti", "callback_data": "cmd:clearfavorites"}],
            [{"text": "🔇 Silenziate", "callback_data": "cmd:silenced"}],
            [{"text": "📊 Leghe con statistiche", "callback_data": "cmd:leghestats"}],
            [{"text": "❓ Help", "callback_data": "cmd:help"}],
        ]
    }
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": "Menu comandi - scegli un'opzione:",
            "reply_markup": json.dumps(keyboard)
        }, timeout=5)


# =============================================================================
# GRAFICO A BARRE ORIZZONTALI (totali cumulativi)
# =============================================================================
def genera_grafico_barre(fixture_id, home_name, away_name, stats):
    try:
        metrics = list(stats.keys())
        home_vals = [stats[m][0] for m in metrics]
        away_vals = [stats[m][1] for m in metrics]

        fig, ax = plt.subplots(figsize=(5.0, 2.6), dpi=150)
        fig.patch.set_facecolor('#1e1e1e')
        ax.set_facecolor('#1e1e1e')

        color_home = '#22c55e'
        color_away = '#ef4444'
        color_bg = '#2a2a2a'
        color_text = '#e5e5e5'
        color_muted = '#888888'

        for i, metric in enumerate(metrics):
            total = home_vals[i] + away_vals[i]
            ax.barh(i, 1, height=0.30, color=color_bg, left=0, zorder=1, edgecolor='none')

            if total == 0:
                ax.text(0.5, i, 'Nessun dato', ha='center', va='center',
                        fontsize=8, color=color_muted, zorder=3)
                continue

            home_pct = home_vals[i] / total
            away_pct = away_vals[i] / total

            ax.barh(i, home_pct, height=0.30, color=color_home, left=0, zorder=2, edgecolor='none')
            ax.barh(i, away_pct, height=0.30, color=color_away, left=home_pct, zorder=2, edgecolor='none')

            ax.text(-0.04, i, str(home_vals[i]), ha='right', va='center',
                    fontsize=11, fontweight='bold', color=color_home, zorder=3)
            ax.text(1.04, i, str(away_vals[i]), ha='left', va='center',
                    fontsize=11, fontweight='bold', color=color_away, zorder=3)

        ax.set_yticks(range(len(metrics)))
        ax.set_yticklabels(metrics, fontsize=10, color=color_text)
        ax.set_xlim(-0.18, 1.18)
        ax.set_xticks([])
        for spine in ['top', 'right', 'bottom', 'left']:
            ax.spines[spine].set_visible(False)
        ax.tick_params(left=False, pad=10)
        ax.invert_yaxis()

        home_patch = mpatches.Patch(color=color_home, label=home_name)
        away_patch = mpatches.Patch(color=color_away, label=away_name)
        ax.legend(handles=[home_patch, away_patch], loc='lower center',
                  bbox_to_anchor=(0.5, -0.20), ncol=2, frameon=False,
                  fontsize=9, labelcolor=color_text)

        plt.tight_layout(rect=[0, 0.06, 1, 1])

        foto_path = os.path.join(os.path.dirname(__file__), f'chart_{fixture_id}.png')
        plt.savefig(foto_path, format='png', bbox_inches='tight',
                    facecolor='#1e1e1e', edgecolor='none', pad_inches=0.1)
        plt.close()
        return foto_path
    except Exception as e:
        log(f"Errore grafico barre: {e}")
        return None


# =============================================================================
# TASTIERA INLINE
# =============================================================================
def get_notification_keyboard(fixture_id, is_favorite=False, is_silenced=False):
    if is_silenced:
        return None
    buttons = []
    fav_text = "Rimuovi dai preferiti" if is_favorite else "Aggiungi ai preferiti"
    buttons.append([{"text": fav_text, "callback_data": f"fav:{fixture_id}"}])
    buttons.append([{"text": "Silenzia questa partita", "callback_data": f"mute:{fixture_id}"}])
    return {"inline_keyboard": buttons}


# =============================================================================
# DELTA 15 MINUTI
# =============================================================================
def calcola_delta_15min(fixture_id, current_stats):
    stato = stato_partite.get(fixture_id, {})
    history = stato.get("history", [])
    now = time.time()

    history_15m = [h for h in history if now - h["timestamp"] <= 900]

    if not history_15m or len(history_15m) < 2:
        return {k: (0, 0) for k in current_stats}, False

    old = history_15m[0]
    delta = {}
    for key in current_stats:
        curr_h, curr_a = current_stats[key]
        old_h, old_a = old["stats"].get(key, (0, 0))
        delta[key] = (max(0, curr_h - old_h), max(0, curr_a - old_a))

    return delta, True


# =============================================================================
# REGOLE DI NOTIFICA
# =============================================================================
def deve_notificare(fixture_id, tiri_casa, tiri_ospite, minuto, delta_stats=None, gol_appena_segnato=False):
    # PRIORITÀ MASSIMA: gol appena segnato -> notifica sempre, nessun cooldown
    if gol_appena_segnato:
        return True

    stato = stato_partite.get(fixture_id, {})
    ultima_casa = stato.get("tiri_casa", -1)
    ultima_ospite = stato.get("tiri_ospite", -1)
    ultimo_invio = stato.get("timestamp_notifica", 0)

    if tiri_casa == ultima_casa and tiri_ospite == ultima_ospite:
        return False

    # Preferiti: notifica sempre se le stats sono cambiate (bypassa le soglie)
    if str(fixture_id) in FAVORITE_MATCHES:
        return True

    tiri_totali = tiri_casa + tiri_ospite
    diff = abs(tiri_casa - tiri_ospite)
    tempo_passato = time.time() - ultimo_invio

    # Regola 1: Differenza tiri significativa
    if diff >= DIFF_TIRI_SOGLIA:
        return True

    # Regola 2: Partita molto attiva nei primi 25 min
    if minuto <= MINUTI_ATTIVA and tiri_totali >= TIRI_TOTALI_ATTIVA:
        return True

    # Regola 3: Forzata ogni 30 min se abbastanza tiri
    if tempo_passato >= INTERVALLO_FORZATO and tiri_totali >= 4:
        return True

    # Regola 4: MOMENTUM - ritmo recente negli ultimi 15 min
    # Cattura partite che si svegliano nel secondo tempo anche se totali bassi
    if delta_stats:
        d_tiri = delta_stats.get("Tiri totali", (0, 0))
        d_porta = delta_stats.get("Tiri in porta", (0, 0))
        d_corner = delta_stats.get("Corner", (0, 0))
        if (d_porta[0] + d_porta[1]) >= MOMENTUM_TIRI_IN_PORTA:
            return True
        if (d_tiri[0] + d_tiri[1]) >= MOMENTUM_TIRI_TOTALI:
            return True
        if (d_corner[0] + d_corner[1]) >= MOMENTUM_CORNER:
            return True

    return False


# =============================================================================
# PROCESSA SINGOLA PARTITA
# =============================================================================
def processa_partita(fixture):
    try:
        fixture_id = fixture["fixture"]["id"]
        league = fixture.get("league", {})
        league_name = league.get("name", "")
        league_type = league.get("type", "")
        league_country = league.get("country", "")

        if not campionato_valido(league_name, league_type, league_country):
            motivo = "Type non riconosciuto"
            for escluso in PAROLE_ESCLUSE:
                if escluso in league_name.lower():
                    motivo = f"Parola esclusa: '{escluso}'"
                    break
            else:
                if SOLO_LEGHE_CON_STATISTICHE:
                    if LEGHE_ATTIVE_CACHE:
                        motivo = "Non in whitelist dinamica leghe con statistiche"
                    elif not any(lega.lower() in league_name.lower() for lega in LEGHE_CON_STATISTICHE):
                        motivo = "Non in whitelist leghe con statistiche"
            log(f"  ❌ {league_name} - SCARTATA ({motivo})")
            return

        home = fixture["teams"]["home"]["name"]
        away = fixture["teams"]["away"]["name"]
        score_home = fixture["goals"]["home"] or 0
        score_away = fixture["goals"]["away"] or 0
        minuto = fixture["fixture"]["status"].get("elapsed") or 0
        status_short = fixture["fixture"]["status"].get("short", "LIVE")

        log(f"  ✅ {home} vs {away} - {minuto}' ({league_name})")

        stato_precedente = stato_partite.get(fixture_id, {})
        prev_score_home = stato_precedente.get("score_home", score_home)
        prev_score_away = stato_precedente.get("score_away", score_away)
        gol_appena_segnato = (score_home != prev_score_home) or (score_away != prev_score_away)
        if gol_appena_segnato and stato_precedente:
            log(f"    ⚽🚨 GOL RILEVATO! Punteggio cambiato: {prev_score_home}-{prev_score_away} -> {score_home}-{score_away}")

        if fixture_id not in stato_partite:
            stato_partite[fixture_id] = {}
        stato_partite[fixture_id].update({
            "score_home": score_home,
            "score_away": score_away,
            "last_minute": minuto,
            "home": home,
            "away": away,
            "league": league_name,
        })

        events = fetch_fixture_events(fixture_id)
        goals = extract_goals(events)
        if goals:
            log(f"    ⚽ Gol trovati: {len(goals)}")
        else:
            log(f"    ⚽ Nessun gol registrato")

        stats = get_statistiche_partita(fixture_id)
        if stats and len(stats) >= 2:
            stats_home = stats[0].get("statistics", [])
            stats_away = stats[1].get("statistics", [])
            tiri_casa = estrai_valore_stat(stats_home, "Total Shots")
            tiri_ospite = estrai_valore_stat(stats_away, "Total Shots")
            tiri_p_casa = estrai_valore_stat(stats_home, "Shots on Goal")
            tiri_p_ospite = estrai_valore_stat(stats_away, "Shots on Goal")
            corner_casa = estrai_valore_stat(stats_home, "Corner Kicks")
            corner_ospite = estrai_valore_stat(stats_away, "Corner Kicks")

            current_stats = {
                "Tiri totali": (tiri_casa, tiri_ospite),
                "Tiri in porta": (tiri_p_casa, tiri_p_ospite),
                "Corner": (corner_casa, corner_ospite),
            }
            log(f"    📊 Statistiche: Tiri {tiri_casa}-{tiri_ospite} | Porta {tiri_p_casa}-{tiri_p_ospite} | Corner {corner_casa}-{corner_ospite}")

            history = stato_partite[fixture_id].get("history", [])
            history.append({"timestamp": time.time(), "stats": current_stats})
            history = [h for h in history if time.time() - h["timestamp"] <= 1200]
            stato_partite[fixture_id]["history"] = history
        else:
            current_stats = None
            tiri_casa = tiri_ospite = tiri_p_casa = tiri_p_ospite = corner_casa = corner_ospite = 0
            log(f"    ⚠️ Statistiche non disponibili da API (lega potrebbe non supportare stats)")

        if status_short in ("FT", "AET", "PEN"):
            stato = stato_partite.get(fixture_id, {})
            if not stato.get("notified_final"):
                muted_data = SILENCED_MATCHES.get(str(fixture_id))

                if muted_data:
                    diff_h = score_home - muted_data.get("score_home", 0)
                    diff_a = score_away - muted_data.get("score_away", 0)
                    muted_minute = muted_data.get("muted_at_minute", 0)

                    after_text = ""
                    if diff_h > 0:
                        after_text += f" +{diff_h}CASA"
                    if diff_a > 0:
                        after_text += f" +{diff_a}OSP"

                    goals_after = [g for g in goals if g["minute"] > muted_minute]
                    minutes_text = ""
                    for g in goals_after:
                        team_emoji = "CASA" if g["team"] == home else "OSP"
                        minutes_text += f" {g['minute']}'{team_emoji}"
                    if not minutes_text:
                        minutes_text = " Nessun gol dopo il silenzio"

                    messaggio = (
                        f"{home} vs {away}\n"
                        f"{league_name}\n"
                        f"Risultato finale: {score_home} - {score_away}{after_text}\n"
                        f"Silenziato al {muted_minute}'\n"
                        f"Gol dopo:{minutes_text}"
                    )
                    foto_path = None
                else:
                    if current_stats:
                        foto_path = genera_grafico_barre(fixture_id, home, away, current_stats)
                    else:
                        foto_path = None

                    goals_text = ""
                    if goals:
                        goals_text += f"\nPrimo gol: {goals[0]['minute']}' ({goals[0]['player']})\n"
                        if len(goals) > 1:
                            goals_text += f"Ultimo gol: {goals[-1]['minute']}' ({goals[-1]['player']})\n"

                    messaggio = (
                        f"{home} vs {away}\n"
                        f"{league_name}\n"
                        f"RISULTATO FINALE\n\n"
                        f"{score_home} - {score_away}\n"
                        f"{goals_text}\n"
                        f"Statistiche finali:\n"
                        f"- Tiri totali: {tiri_casa if current_stats else '?'} - {tiri_ospite if current_stats else '?'}\n"
                        f"- Tiri in porta: {tiri_p_casa if current_stats else '?'} - {tiri_p_ospite if current_stats else '?'}\n"
                        f"- Corner: {corner_casa if current_stats else '?'} - {corner_ospite if current_stats else '?'}"
                    )

                invia_notifica_telegram(foto_path, messaggio)

                SILENCED_MATCHES.pop(str(fixture_id), None)
                save_silenced(SILENCED_MATCHES)
                if foto_path and os.path.exists(foto_path):
                    try:
                        os.remove(foto_path)
                    except:
                        pass

            stato_partite[fixture_id] = {
                "tiri_casa": stato.get("tiri_casa", 0),
                "tiri_ospite": stato.get("tiri_ospite", 0),
                "timestamp_notifica": stato.get("timestamp_notifica", 0),
                "home": home,
                "away": away,
                "league": league_name,
                "notified_final": True,
            }
            return

        if str(fixture_id) in SILENCED_MATCHES:
            muted_data = SILENCED_MATCHES[str(fixture_id)]
            if "muted_at_minute" not in muted_data:
                muted_data["muted_at_minute"] = minuto
                save_silenced(SILENCED_MATCHES)
            log(f"  -> Silenziata, skip")
            return

        if current_stats:
            delta_stats, is_real_delta = calcola_delta_15min(fixture_id, current_stats)
            stats_dict = delta_stats
            header_stats = "Statistiche ultimi 15 min" if is_real_delta else "Primo rilevamento"
        else:
            stats_dict = {"Tiri totali": (0, 0), "Tiri in porta": (0, 0), "Corner": (0, 0)}
            header_stats = "Statistiche"

        log(f"  Tiri: {tiri_casa}-{tiri_ospite} | Porta: {tiri_p_casa}-{tiri_p_ospite} | Corner: {corner_casa}-{corner_ospite}")
        log(f"  Delta 15min: {stats_dict}")

        if not deve_notificare(fixture_id, tiri_casa, tiri_ospite, minuto, delta_stats=stats_dict, gol_appena_segnato=gol_appena_segnato):
            prev_notified = stato_partite.get(fixture_id, {}).get("notified_final", False)
            stato_partite[fixture_id].update({
                "tiri_casa": tiri_casa,
                "tiri_ospite": tiri_ospite,
                "timestamp_notifica": stato_partite[fixture_id].get("timestamp_notifica", 0),
                "notified_final": prev_notified,
            })
            log(f"  -> Skip")
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
            goals_text += f"\nPrimo gol: {goals[0]['minute']}' ({goals[0]['player']})\n"
            if len(goals) > 1:
                goals_text += f"Ultimo gol: {goals[-1]['minute']}' ({goals[-1]['player']})\n"

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
        log(f"Errore processa_partita: {e}")


def pulisci_partite_terminate(fixture_ids_live):
    ids_da_rimuovere = [fid for fid in stato_partite if fid not in fixture_ids_live]
    for fid in ids_da_rimuovere:
        stato = stato_partite.get(fid, {})
        # Nessun messaggio di fine partita
        SILENCED_MATCHES.pop(str(fid), None)
        del stato_partite[fid]

    if ids_da_rimuovere:
        save_silenced(SILENCED_MATCHES)
        log(f"Partite terminate rimosse: {len(ids_da_rimuovere)}")


# =============================================================================
# CICLO PRINCIPALE
# =============================================================================
def imposta_comandi_telegram():
    if not CONFIG_VALIDA:
        return
    comandi = [
        {"command": "setup", "description": "Menu comandi a bottoni"},
        {"command": "live", "description": "Partite live monitorate"},
        {"command": "status", "description": "Info live su una squadra"},
        {"command": "favorites", "description": "Lista partite preferite"},
        {"command": "clearfavorites", "description": "Svuota lista preferiti"},
        {"command": "silenced", "description": "Lista partite silenziate"},
        {"command": "leghestats", "description": "Leghe con statistiche coperte"},
        {"command": "help", "description": "Mostra i comandi disponibili"},
    ]
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setMyCommands",
            json={"commands": comandi}, timeout=10)
    except Exception as e:
        log(f"Errore setMyCommands: {e}")


if __name__ == "__main__":
    log("=== Bot avviato ===")
    imposta_comandi_telegram()
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
                f.get("league", {}).get("type", ""),
                f.get("league", {}).get("country", "")
            )
        ]
        log(f"Partite live: {len(partite)} totali, {len(partite_valide)} valide")

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