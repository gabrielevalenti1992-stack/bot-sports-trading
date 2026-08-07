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

# Chat/canale dedicato alle notifiche delle partite preferite (opzionale). Se non impostata,
# le notifiche dei preferiti restano nella chat principale come tutte le altre.
TELEGRAM_CHAT_ID_PREFERITI = os.environ.get("TELEGRAM_CHAT_ID_PREFERITI") or TELEGRAM_CHAT_ID

CONFIG_VALIDA = all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, API_FOOTBALL_KEY])
print(f"TOKEN presente: {'SI' if TELEGRAM_BOT_TOKEN else 'NO'}", flush=True)
print(f"CHAT_ID presente: {'SI' if TELEGRAM_CHAT_ID else 'NO'}", flush=True)
print(f"API_KEY presente: {'SI' if API_FOOTBALL_KEY else 'NO'}", flush=True)
print(f"CHAT_ID preferiti dedicato: {'SI' if os.environ.get('TELEGRAM_CHAT_ID_PREFERITI') else 'NO (uso la chat principale)'}", flush=True)

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

# Pesi per l'indice di intensità (usato dal comando /intensita e dal report automatico)
PESO_INTENSITA_TIRI = 1
PESO_INTENSITA_PORTA = 2
PESO_INTENSITA_CORNER = 1

# Report automatico di intensità: ogni quanto (secondi) inviarlo, una volta che i dati sono pronti
INTERVALLO_REPORT_INTENSITA = 900  # 15 minuti
ULTIMO_REPORT_INTENSITA = 0

# Storico minutaggi (analisi pre-partita /analisi): ogni quanto (secondi) ricontrollare le leghe
# whitelist per nuove partite terminate da processare, e quante partite nuove processare al
# massimo (in totale, su tutte le leghe insieme) ad ogni esecuzione, per non sforare le quote API
# in un colpo solo. L'aggiornamento automatico è spento di default: con ~40 leghe in whitelist,
# ogni riavvio del bot altrimenti riproverebbe il backfill su tutte, consumando in fretta la quota
# giornaliera di API-Football. Va acceso esplicitamente in config.json quando si è pronti, oppure
# si usa /aggiornastorico a mano quando si decide di spendere quota.
INTERVALLO_AGGIORNAMENTO_STORICO = 604800  # 7 giorni
STORICO_MAX_FIXTURES_PER_RUN = 30
STORICO_AGGIORNAMENTO_AUTOMATICO = False
FASCE_MINUTO = ["0-15", "16-30", "31-45", "46-60", "61-75", "76-90"]

# Filtro leghe con statistiche note (per evitare notifiche su campionati minori senza dati API)
SOLO_LEGHE_CON_STATISTICHE = True
LEGHE_CON_STATISTICHE = [
    "Premier League", "La Liga", "Serie A", "Bundesliga", "Ligue 1",
    "Eredivisie", "Primeira Liga", "Championship",
    # Seconde divisioni dei campionati top
    "Segunda División", "Segunda Division", "Serie B", "2. Bundesliga", "Ligue 2", "Eerste Divisie",
    "League One", "League Two",
    # Giappone e Corea del Sud
    "J1 League", "J2 League", "K League 1", "K League 2",
    # Belgio, Croazia, Danimarca, Romania, Turchia, Svizzera, Scozia/Irlanda del Nord, Arabia Saudita, USA
    # (Austria è già coperta da "Bundesliga", nome condiviso con la Germania nell'API)
    "Jupiler Pro League", "First Division A", "HNL", "Superliga", "Liga I",
    "Süper Lig", "Super Lig", "Super League", "Premiership",
    "Saudi Pro League", "Pro League", "Major League Soccer",
    # Svezia, Polonia, Slovenia, Slovacchia
    "Allsvenskan", "Ekstraklasa", "Prva Liga", "Super Liga", "Fortuna Liga",
    # Serbia (già coperta da "Super Liga"), Repubblica Ceca, Ungheria, Finlandia, Islanda
    "Czech Liga", "NB I", "Veikkausliiga", "Besta deild", "Úrvalsdeild",
    # Brasile e Argentina (Brasile già coperto da "Serie A"/"Serie B"), Colombia, Uruguay
    "Liga Profesional Argentina", "Copa de la Liga Profesional", "Primera A", "Primera División",
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
    PESO_INTENSITA_TIRI = config.get("peso_intensita_tiri", PESO_INTENSITA_TIRI)
    PESO_INTENSITA_PORTA = config.get("peso_intensita_porta", PESO_INTENSITA_PORTA)
    PESO_INTENSITA_CORNER = config.get("peso_intensita_corner", PESO_INTENSITA_CORNER)
    INTERVALLO_REPORT_INTENSITA = config.get("intervallo_report_intensita", INTERVALLO_REPORT_INTENSITA)
    INTERVALLO_AGGIORNAMENTO_STORICO = config.get("intervallo_aggiornamento_storico", INTERVALLO_AGGIORNAMENTO_STORICO)
    STORICO_MAX_FIXTURES_PER_RUN = config.get("storico_max_fixtures_per_run", STORICO_MAX_FIXTURES_PER_RUN)
    STORICO_AGGIORNAMENTO_AUTOMATICO = config.get("storico_aggiornamento_automatico", STORICO_AGGIORNAMENTO_AUTOMATICO)
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
# STORICO MINUTAGGI (analisi pre-partita /analisi)
# =============================================================================
STORICO_MINUTAGGI_FILE = "storico_minutaggi.json"

def carica_storico_minutaggi():
    if os.path.exists(STORICO_MINUTAGGI_FILE):
        try:
            with open(STORICO_MINUTAGGI_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Errore lettura {STORICO_MINUTAGGI_FILE}: {e}", flush=True)
    return {}

def salva_storico_minutaggi(dati):
    with open(STORICO_MINUTAGGI_FILE, 'w') as f:
        json.dump(dati, f)

STORICO_MINUTAGGI = carica_storico_minutaggi()

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


def esegui_comando_sicuro(chat_id, funzione, *args):
    """Esegue una funzione cmd_* intercettando qualsiasi eccezione, così un errore
    non passa mai inosservato: viene loggato e l'utente riceve un avviso invece del silenzio."""
    try:
        funzione(chat_id, *args)
    except Exception as e:
        log(f"Errore comando {funzione.__name__}: {e}")
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": f"Errore durante l'esecuzione del comando: {e}", "parse_mode": "Markdown"}, timeout=5)
        except Exception:
            pass

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

                    elif data.startswith("unmute:"):
                        fid = str(int(data.split(":")[1]))
                        SILENCED_MATCHES.pop(fid, None)
                        save_silenced(SILENCED_MATCHES)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                            json={"callback_query_id": cq["id"], "text": "Partita riattivata"}, timeout=5)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
                            json={
                                "chat_id": chat_id,
                                "message_id": msg_id,
                                "reply_markup": json.dumps({"inline_keyboard": []})
                            }, timeout=5)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": "\U0001F514 Partita riattivata. Torneranno gli alert live."}, timeout=5)

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
                            esegui_comando_sicuro(chat_id, cmd_live)
                        elif azione == "favorites":
                            esegui_comando_sicuro(chat_id, cmd_favorites)
                        elif azione == "clearfavorites":
                            esegui_comando_sicuro(chat_id, cmd_clearfavorites)
                        elif azione == "silenced":
                            esegui_comando_sicuro(chat_id, cmd_silenced)
                        elif azione == "leghestats":
                            esegui_comando_sicuro(chat_id, cmd_leghestats)
                        elif azione == "intensita":
                            esegui_comando_sicuro(chat_id, cmd_intensita)
                        elif azione == "help":
                            esegui_comando_sicuro(chat_id, cmd_help)

                msg = upd.get("message")
                if msg and msg.get("text"):
                    text = msg["text"].strip()
                    chat_id = msg["chat"]["id"]
                    parts = text.split()
                    cmd = parts[0].lower()
                    args = parts[1:] if len(parts) > 1 else []

                    if cmd == "/help":
                        esegui_comando_sicuro(chat_id, cmd_help)

                    elif cmd == "/setup":
                        esegui_comando_sicuro(chat_id, cmd_setup)

                    elif cmd == "/status":
                        if not args:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": "Usa: /status <nome squadra>", "parse_mode": "Markdown"}, timeout=5)
                            continue
                        esegui_comando_sicuro(chat_id, cmd_status, " ".join(args).lower().strip("<>").strip())

                    elif cmd == "/statstypes":
                        if not args:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": "Usa: /statstypes <nome squadra>", "parse_mode": "Markdown"}, timeout=5)
                            continue
                        esegui_comando_sicuro(chat_id, cmd_statstypes, " ".join(args).lower().strip("<>").strip())

                    elif cmd == "/statscoverage":
                        esegui_comando_sicuro(chat_id, cmd_statscoverage)

                    elif cmd == "/cercastat":
                        if not args:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": "Usa: /cercastat <statistica> [soglia]\nEs: /cercastat tiri in porta 3"}, timeout=5)
                            continue
                        esegui_comando_sicuro(chat_id, cmd_cercastat, " ".join(args))

                    elif cmd == "/intensita":
                        esegui_comando_sicuro(chat_id, cmd_intensita)

                    elif cmd == "/analisi":
                        if not args:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": "Usa: /analisi <squadra in casa> - <squadra in trasferta>\nEs: /analisi Milan - Juventus"}, timeout=5)
                            continue
                        esegui_comando_sicuro(chat_id, cmd_analisi, " ".join(args))

                    elif cmd == "/aggiornastorico":
                        esegui_comando_sicuro(chat_id, cmd_aggiornastorico)

                    elif cmd == "/quotebetfair":
                        if not args:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": "Usa: /quotebetfair <squadra casa> - <squadra trasferta>\nEs: /quotebetfair Milan - Juventus"}, timeout=5)
                            continue
                        esegui_comando_sicuro(chat_id, cmd_quotebetfair, " ".join(args))

                    elif cmd == "/favorites":
                        esegui_comando_sicuro(chat_id, cmd_favorites)

                    elif cmd == "/clearfavorites":
                        esegui_comando_sicuro(chat_id, cmd_clearfavorites)

                    elif cmd == "/silenced":
                        esegui_comando_sicuro(chat_id, cmd_silenced)

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
                        esegui_comando_sicuro(chat_id, cmd_live)

                    elif cmd == "/leghestats":
                        esegui_comando_sicuro(chat_id, cmd_leghestats)
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


def invia_messaggio_telegram(testo, chat_id=None):
    if not CONFIG_VALIDA:
        log(f"[SKIP Telegram] Config mancante: {testo[:50]}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {'chat_id': chat_id or TELEGRAM_CHAT_ID, 'text': testo, 'parse_mode': 'Markdown'}
        response = requests.post(url, data=data, timeout=10)
        log(f"Telegram testo - Status: {response.status_code} - {response.text[:100]}")
    except Exception as e:
        log(f"Errore invio testo Telegram: {e}")


def invia_notifica_telegram(foto_path, messaggio, reply_markup=None, chat_id=None):
    if not CONFIG_VALIDA:
        log(f"[SKIP Telegram] Config mancante: {messaggio[:50]}")
        return
    destinatario = chat_id or TELEGRAM_CHAT_ID
    try:
        if foto_path and os.path.exists(foto_path):
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            with open(foto_path, 'rb') as photo:
                files = {'photo': photo}
                data = {
                    'chat_id': destinatario,
                    'caption': messaggio,
                    'parse_mode': 'Markdown'
                }
                if reply_markup:
                    data['reply_markup'] = json.dumps(reply_markup)
                response = requests.post(url, data=data, files=files, timeout=10)
                log(f"Telegram foto - Status: {response.status_code}")
        else:
            invia_messaggio_telegram(messaggio, chat_id=destinatario)
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
        in_cache_dinamica = (league_country.lower(), nome) in LEGHE_ATTIVE_CACHE
        in_whitelist_statica = any(lega.lower() in nome for lega in LEGHE_CON_STATISTICHE)
        # Unione, non sostituzione: la whitelist statica resta una rete di sicurezza per i
        # campionati core anche quando l'API non li marca ancora come "coperti" nella stagione
        # corrente (es. a inizio stagione, prima che vengano giocate partite con statistiche reali).
        if not in_cache_dinamica and not in_whitelist_statica:
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


def estrai_valore_stat_raw(stats_team, nome_stat):
    """Come estrai_valore_stat ma distingue 'campo assente/valore null' (None) da un valore reale, anche 0."""
    nome_stat = nome_stat.lower()
    for stat in stats_team:
        if (stat.get("type") or "").lower() == nome_stat:
            val = stat.get("value")
            if val is None:
                return None
            try:
                return float(str(val).replace("%", "").strip())
            except (TypeError, ValueError):
                return None
    return None


# Alias in italiano -> nome esatto del campo "type" usato da API-Football
ALIAS_STATISTICHE = {
    "tiri": "Total Shots",
    "tiri totali": "Total Shots",
    "tiri in porta": "Shots on Goal",
    "tiri porta": "Shots on Goal",
    "tiri fuori": "Shots off Goal",
    "tiri fuori porta": "Shots off Goal",
    "tiri bloccati": "Blocked Shots",
    "tiri dentro area": "Shots insidebox",
    "tiri in area": "Shots insidebox",
    "tiri interni area": "Shots insidebox",
    "tiri fuori area": "Shots outsidebox",
    "corner": "Corner Kicks",
    "calci d'angolo": "Corner Kicks",
    "angoli": "Corner Kicks",
    "falli": "Fouls",
    "fuorigioco": "Offsides",
    "possesso": "Ball Possession",
    "possesso palla": "Ball Possession",
    "gialli": "Yellow Cards",
    "cartellini gialli": "Yellow Cards",
    "rossi": "Red Cards",
    "cartellini rossi": "Red Cards",
    "parate": "Goalkeeper Saves",
    "passaggi": "Total passes",
    "passaggi totali": "Total passes",
    "passaggi accurati": "Passes accurate",
    "precisione passaggi": "Passes %",
    "xg": "expected_goals",
    "gol attesi": "expected_goals",
    "expected goals": "expected_goals",
    "gol evitati": "goals_prevented",
}


def risolvi_nome_statistica(query):
    """Traduce un nome in italiano (o parziale) nel campo 'type' esatto dell'API. Fallback: usa la query com'è."""
    query = query.lower().strip()
    if query in ALIAS_STATISTICHE:
        return ALIAS_STATISTICHE[query]
    for alias, tipo_api in ALIAS_STATISTICHE.items():
        if query in alias or alias in query:
            return tipo_api
    return query


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
        "/statscoverage - Copertura statistiche su tutte le partite live (diagnostica)\n"
        "/cercastat <statistica> [soglia] - Cerca tra le partite live per statistica (es: tiri in porta 3, xg, possesso 60)\n"
        "/intensita - Classifica le partite live per probabilità di essere \"calde\" ora\n"
        "/analisi <squadra casa> - <squadra trasferta> - Distribuzione storica gol per fascia di minuto (es: /analisi Milan - Juventus)\n"
        "/aggiornastorico - Forza l'aggiornamento dello storico minutaggi usato da /analisi\n"
        "/quotebetfair <squadra casa> - <squadra trasferta> - Quote Betfair 1X2/Over-Under/Goal-NoGoal (es: /quotebetfair Milan - Juventus)\n"
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
    keyboard = {"inline_keyboard": []}
    for fid, info in SILENCED_MATCHES.items():
        stato = stato_partite.get(int(fid), {})
        home = stato.get("home", f"ID {fid}")
        away = stato.get("away", "")
        etichetta = f"{home} vs {away}" if away else home
        lines.append(f"- {etichetta} (silenziata al {info.get('muted_at_minute', '?')}')")
        keyboard["inline_keyboard"].append([{"text": f"Riattiva: {etichetta}", "callback_data": f"unmute:{fid}"}])
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "\n".join(lines), "reply_markup": json.dumps(keyboard)}, timeout=5)


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
            stats_home = stats[0].get("statistics", [])
            stats_away = stats[1].get("statistics", [])
            tipi_home = [s.get("type") for s in stats_home if s.get("type")]
            tipi_away = [s.get("type") for s in stats_away if s.get("type")]
            tipi = sorted(set(tipi_home) | set(tipi_away))

            def stato_campo(nome_parziale):
                """Presente e popolato / presente ma vuoto (null) / assente, cercando per sottostringa nel type."""
                trovato = False
                for s in stats_home + stats_away:
                    t = (s.get("type") or "").lower()
                    if nome_parziale in t:
                        trovato = True
                        if s.get("value") is not None:
                            return "SI (con dati)"
                return "PRESENTE MA VUOTO (null)" if trovato else "NO (campo assente)"

            testo = (
                f"{home} vs {away}\n"
                f"Tipi di statistiche restituiti dall'API:\n"
                + "\n".join(f"- {t}" for t in tipi)
                + f"\n\nShots insidebox: {stato_campo('insidebox')}"
                + f"\nexpected_goals (xG): {stato_campo('expected')}"
            )
        risposta = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": testo}, timeout=5)
        if risposta.status_code != 200:
            log(f"Errore invio /statstypes: HTTP {risposta.status_code} - {risposta.text[:300]}")


def cmd_statscoverage(chat_id):
    """Diagnostica: scansiona TUTTE le partite live in questo momento e calcola, per ogni tipo
    di statistica, su quante partite (con almeno dati disponibili) il valore è realmente popolato
    (non null). Serve a trovare una statistica "universale" utilizzabile su qualsiasi campionato
    coperto dall'API, invece di indovinare da pochi esempi."""
    partite_raw = get_partite_live()
    partite_cmd = [
        f for f in partite_raw
        if campionato_valido(
            f.get("league", {}).get("name", ""),
            f.get("league", {}).get("type", ""),
            f.get("league", {}).get("country", "")
        )
    ]
    if not partite_cmd:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Nessuna partita live al momento nei campionati con statistiche note."}, timeout=5)
        return

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": f"Scansione statistiche su {len(partite_cmd)} partite live nei campionati con statistiche note (su {len(partite_raw)} live totali), attendi..."}, timeout=5)

    conteggio_presente = {}
    totale_con_stats = 0
    for f in partite_cmd:
        fid = f["fixture"]["id"]
        stats = get_statistiche_partita(fid)
        if stats and len(stats) >= 2:
            stats_home = stats[0].get("statistics", [])
            stats_away = stats[1].get("statistics", [])
            if stats_home or stats_away:
                totale_con_stats += 1
                tipi_con_valore = set()
                for s in stats_home + stats_away:
                    t = s.get("type")
                    if t and s.get("value") is not None:
                        tipi_con_valore.add(t)
                for t in tipi_con_valore:
                    conteggio_presente[t] = conteggio_presente.get(t, 0) + 1
        time.sleep(0.3)

    if totale_con_stats == 0:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Nessuna partita ha statistiche disponibili in questo momento."}, timeout=5)
        return

    righe = sorted(conteggio_presente.items(), key=lambda kv: -kv[1])
    testo = (
        f"Copertura statistiche reali (non null) su {totale_con_stats} partite con dati "
        f"(su {len(partite_cmd)} live totali):\n\n"
    )
    for tipo, cnt in righe:
        pct = round(100 * cnt / totale_con_stats)
        testo += f"- {tipo}: {cnt}/{totale_con_stats} ({pct}%)\n"

    for i in range(0, len(testo), 3800):
        pezzo = testo[i:i + 3800]
        risposta = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": pezzo}, timeout=10)
        if risposta.status_code != 200:
            log(f"Errore invio /statscoverage: HTTP {risposta.status_code} - {risposta.text[:300]}")


def cmd_cercastat(chat_id, testo_richiesta):
    """Cerca tra tutte le partite live quelle dove una statistica scelta dall'utente è disponibile,
    opzionalmente sopra una soglia. Es: 'tiri in porta 3', 'xg', 'possesso 60'."""
    parole = testo_richiesta.strip().split()
    soglia = None
    if parole:
        try:
            soglia = float(parole[-1].replace(",", "."))
            parole = parole[:-1]
        except ValueError:
            soglia = None
    query_stat = " ".join(parole).strip()
    if not query_stat:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Usa: /cercastat <statistica> [soglia]\nEs: /cercastat tiri in porta 3"}, timeout=5)
        return

    tipo_api = risolvi_nome_statistica(query_stat)

    partite_raw = get_partite_live()
    partite_cmd = [
        f for f in partite_raw
        if campionato_valido(
            f.get("league", {}).get("name", ""),
            f.get("league", {}).get("type", ""),
            f.get("league", {}).get("country", "")
        )
    ]
    if not partite_cmd:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Nessuna partita live al momento nei campionati con statistiche note."}, timeout=5)
        return

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": f"Ricerca '{tipo_api}'" + (f" >= {soglia}" if soglia is not None else "") + f" su {len(partite_cmd)} partite live nei campionati con statistiche note (su {len(partite_raw)} live totali)..."}, timeout=5)

    risultati = []
    for f in partite_cmd:
        fid = f["fixture"]["id"]
        home = f["teams"]["home"]["name"]
        away = f["teams"]["away"]["name"]
        league = f["league"]["name"]
        minute = f["fixture"]["status"].get("elapsed", "?")
        stats = get_statistiche_partita(fid)
        if stats and len(stats) >= 2:
            sh = stats[0].get("statistics", [])
            sa = stats[1].get("statistics", [])
            vh = estrai_valore_stat_raw(sh, tipo_api)
            va = estrai_valore_stat_raw(sa, tipo_api)
            if vh is not None or va is not None:
                vh_num = vh if vh is not None else 0
                va_num = va if va is not None else 0
                if soglia is None or vh_num >= soglia or va_num >= soglia:
                    risultati.append((home, away, vh, va, league, minute))
        time.sleep(0.3)

    if not risultati:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": f"Nessuna partita trovata con '{tipo_api}' disponibile" + (f" >= {soglia}" if soglia is not None else "") + "."}, timeout=5)
        return

    righe = [f"Trovate {len(risultati)} partite con '{tipo_api}'" + (f" >= {soglia}" if soglia is not None else " disponibile") + ":\n"]
    for home, away, vh, va, league, minute in risultati[:20]:
        vh_txt = "?" if vh is None else vh
        va_txt = "?" if va is None else va
        righe.append(f"- {home} {vh_txt} - {va_txt} {away} ({league}, {minute}')")
    if len(risultati) > 20:
        righe.append(f"\n... e altre {len(risultati) - 20} partite non mostrate")

    testo = "\n".join(righe)
    for i in range(0, len(testo), 3800):
        pezzo = testo[i:i + 3800]
        risposta = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": pezzo}, timeout=10)
        if risposta.status_code != 200:
            log(f"Errore invio /cercastat: HTTP {risposta.status_code} - {risposta.text[:300]}")


def calcola_indice_intensita(delta_stats):
    """Punteggio pesato basato sul ritmo (delta ultimi 15 min) di tiri totali, tiri in porta e corner.
    Più alto = probabilità maggiore che la partita sia "calda" in questo momento."""
    d_tiri = delta_stats.get("Tiri totali", (0, 0))
    d_porta = delta_stats.get("Tiri in porta", (0, 0))
    d_corner = delta_stats.get("Corner", (0, 0))
    return (
        (d_tiri[0] + d_tiri[1]) * PESO_INTENSITA_TIRI
        + (d_porta[0] + d_porta[1]) * PESO_INTENSITA_PORTA
        + (d_corner[0] + d_corner[1]) * PESO_INTENSITA_CORNER
    )


def descrivi_motivazioni_intensita(delta_stats):
    """Elenco leggibile delle statistiche (ultimi 15 min) che contribuiscono al ritmo della
    partita, ordinate per contributo decrescente. Mostra solo le voci con variazione positiva."""
    pesi = {
        "Tiri totali": PESO_INTENSITA_TIRI,
        "Tiri in porta": PESO_INTENSITA_PORTA,
        "Corner": PESO_INTENSITA_CORNER,
    }
    etichette = {
        "Tiri totali": "tiri totali",
        "Tiri in porta": "tiri in porta",
        "Corner": "corner",
    }
    contributi = []
    for chiave, peso in pesi.items():
        d_home, d_away = delta_stats.get(chiave, (0, 0))
        totale = d_home + d_away
        if totale > 0:
            contributi.append((totale * peso, totale, etichette[chiave]))
    if not contributi:
        return "nessun aumento significativo di ritmo"
    contributi.sort(key=lambda c: -c[0])
    return ", ".join(f"+{totale} {etichetta}" for _, totale, etichetta in contributi)


def simbolo_fiamma_per_posizione(posizione):
    """Simboli fiamma solo per le prime 4 posizioni in classifica (1° = più fiamme)."""
    if posizione <= 2:
        return "🔥🔥🔥"
    if posizione <= 4:
        return "🔥🔥"
    return ""


def cmd_intensita(chat_id):
    """Classifica le partite live (nei campionati con statistiche note) per indice di intensità,
    calcolato sul ritmo recente (ultimi 15 min) invece che sui totali cumulativi di partita."""
    partite_raw = get_partite_live()
    partite_cmd = [
        f for f in partite_raw
        if campionato_valido(
            f.get("league", {}).get("name", ""),
            f.get("league", {}).get("type", ""),
            f.get("league", {}).get("country", "")
        )
    ]
    if not partite_cmd:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Nessuna partita live al momento nei campionati con statistiche note."}, timeout=5)
        return

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": f"Calcolo indice di intensità su {len(partite_cmd)} partite, attendi..."}, timeout=5)

    risultati = []
    for f in partite_cmd:
        fid = f["fixture"]["id"]
        home = f["teams"]["home"]["name"]
        away = f["teams"]["away"]["name"]
        league = f["league"]["name"]
        minute = f["fixture"]["status"].get("elapsed", "?")
        score_h = f["goals"]["home"] or 0
        score_a = f["goals"]["away"] or 0
        stats = get_statistiche_partita(fid)
        if stats and len(stats) >= 2:
            sh = stats[0].get("statistics", [])
            sa = stats[1].get("statistics", [])
            current_stats = {
                "Tiri totali": (estrai_valore_stat(sh, "Total Shots"), estrai_valore_stat(sa, "Total Shots")),
                "Tiri in porta": (estrai_valore_stat(sh, "Shots on Goal"), estrai_valore_stat(sa, "Shots on Goal")),
                "Corner": (estrai_valore_stat(sh, "Corner Kicks"), estrai_valore_stat(sa, "Corner Kicks")),
            }
            delta_stats, is_real = calcola_delta_15min(fid, current_stats)
            punteggio = calcola_indice_intensita(delta_stats)
            risultati.append((punteggio, home, away, league, minute, score_h, score_a, delta_stats, is_real))
        time.sleep(0.3)

    if not risultati:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Nessuna delle partite monitorate ha statistiche disponibili in questo momento."}, timeout=5)
        return

    risultati.sort(key=lambda r: -r[0])
    top = risultati[:7]
    righe = [f"Top {len(top)} partite più \"calde\" (ritmo ultimi 15 min):\n"]
    for i, (punteggio, home, away, league, minute, score_h, score_a, delta_stats, is_real) in enumerate(top, start=1):
        nota = " (primo rilevamento, dato non ancora affidabile)" if not is_real else ""
        fiamme = simbolo_fiamma_per_posizione(i)
        prefisso = f"{fiamme} " if fiamme else ""
        motivazioni = descrivi_motivazioni_intensita(delta_stats)
        righe.append(
            f"{prefisso}{home} {score_h}-{score_a} {away} ({league}, {minute}'){nota}\n"
            f"   {motivazioni}"
        )
    if len(risultati) > 7:
        righe.append(f"\n... e altre {len(risultati) - 7} partite con ritmo più basso")

    testo = "\n".join(righe)
    for i in range(0, len(testo), 3800):
        pezzo = testo[i:i + 3800]
        risposta = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": pezzo}, timeout=10)
        if risposta.status_code != 200:
            log(f"Errore invio /intensita: HTTP {risposta.status_code} - {risposta.text[:300]}")


def cmd_setup(chat_id):
    keyboard = {
        "inline_keyboard": [
            [{"text": "📡 Live", "callback_data": "cmd:live"}],
            [{"text": "⭐ Preferiti", "callback_data": "cmd:favorites"},
             {"text": "🗑 Svuota preferiti", "callback_data": "cmd:clearfavorites"}],
            [{"text": "🔇 Silenziate", "callback_data": "cmd:silenced"}],
            [{"text": "📊 Leghe con statistiche", "callback_data": "cmd:leghestats"}],
            [{"text": "🔥 Intensità partite live", "callback_data": "cmd:intensita"}],
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


def invia_report_intensita_automatico(partite_valide):
    """Chiamata una volta per ciclo dal loop principale. Non manda nulla finché lo storico
    (azzerato ad ogni riavvio) non ha almeno un delta reale su 15 minuti; da quel momento invia
    la classifica di intensità ogni INTERVALLO_REPORT_INTENSITA secondi, riusando i dati già
    scaricati in questo ciclo (nessuna chiamata API aggiuntiva)."""
    global ULTIMO_REPORT_INTENSITA
    now = time.time()
    if now - ULTIMO_REPORT_INTENSITA < INTERVALLO_REPORT_INTENSITA:
        return

    risultati = []
    for f in partite_valide:
        fixture_id = f.get("fixture", {}).get("id")
        if not fixture_id:
            continue
        stato = stato_partite.get(fixture_id, {})
        history = stato.get("history", [])
        if not history:
            continue
        current_stats = history[-1]["stats"]
        delta_stats, is_real = calcola_delta_15min(fixture_id, current_stats)
        if not is_real:
            continue
        punteggio = calcola_indice_intensita(delta_stats)
        home = stato.get("home") or f.get("teams", {}).get("home", {}).get("name", "?")
        away = stato.get("away") or f.get("teams", {}).get("away", {}).get("name", "?")
        league = stato.get("league") or f.get("league", {}).get("name", "?")
        minute = f.get("fixture", {}).get("status", {}).get("elapsed", "?")
        risultati.append((punteggio, home, away, league, minute))

    if not risultati:
        log("Report intensità automatico: dati non ancora pronti (storico insufficiente), skip.")
        return

    risultati.sort(key=lambda r: -r[0])
    righe = [f"Report automatico intensità (ultimi 15 min) - {len(risultati)} partite:\n"]
    for punteggio, home, away, league, minute in risultati[:15]:
        righe.append(f"- {punteggio:.1f} pt | {home} vs {away} ({league}, {minute}')")
    invia_messaggio_telegram("\n".join(righe))
    ULTIMO_REPORT_INTENSITA = now


# =============================================================================
# STORICO MINUTAGGI - backfill/aggiornamento e analisi pre-partita (/analisi)
# =============================================================================
LEGHE_ID_STAGIONE_CACHE = {}
LEGHE_ID_STAGIONE_TIMESTAMP = 0
LEGHE_ID_STAGIONE_TTL = 86400  # 24 ore


def fascia_minuto(elapsed):
    """Fascia di 15 minuti a cui appartiene un gol, in base al minuto regolamentare
    (i minuti di recupero contano nella fascia a cui appartengono: 45+2 -> '31-45', 90+3 -> '76-90')."""
    elapsed = elapsed or 0
    if elapsed <= 15:
        return "0-15"
    if elapsed <= 30:
        return "16-30"
    if elapsed <= 45:
        return "31-45"
    if elapsed <= 60:
        return "46-60"
    if elapsed <= 75:
        return "61-75"
    return "76-90"


def risolvi_leghe_whitelist():
    """Risolve (id, stagione) per ogni campionato in whitelist interrogando /leagues una sola
    volta (cache 24h), per costruire/aggiornare lo storico minutaggi senza dover indovinare gli
    ID numerici delle leghe usati dall'API."""
    global LEGHE_ID_STAGIONE_CACHE, LEGHE_ID_STAGIONE_TIMESTAMP
    now = time.time()
    if LEGHE_ID_STAGIONE_CACHE and (now - LEGHE_ID_STAGIONE_TIMESTAMP) < LEGHE_ID_STAGIONE_TTL:
        return LEGHE_ID_STAGIONE_CACHE
    if not API_FOOTBALL_KEY:
        return LEGHE_ID_STAGIONE_CACHE

    url = "https://v3.football.api-sports.io/leagues"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"current": "true"}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)
        if response.status_code != 200:
            log(f"Errore /leagues (risolvi_leghe_whitelist): HTTP {response.status_code}")
            return LEGHE_ID_STAGIONE_CACHE
        data = response.json()
        mappa = {}
        for item in data.get("response", []):
            league = item.get("league", {})
            nome = league.get("name", "")
            league_id = league.get("id")
            if not nome or not league_id:
                continue
            if not any(lega.lower() in nome.lower() or nome.lower() in lega.lower() for lega in LEGHE_CON_STATISTICHE):
                continue
            for season in item.get("seasons", []):
                if season.get("current"):
                    mappa[nome] = (league_id, season.get("year"))
                    break
        if mappa:
            LEGHE_ID_STAGIONE_CACHE = mappa
            LEGHE_ID_STAGIONE_TIMESTAMP = now
            log(f"Storico minutaggi: risolte {len(mappa)} leghe whitelist con ID e stagione")
        return LEGHE_ID_STAGIONE_CACHE
    except Exception as e:
        log(f"Errore risolvi_leghe_whitelist: {e}")
        return LEGHE_ID_STAGIONE_CACHE


def get_fixtures_terminati(league_id, season):
    if not API_FOOTBALL_KEY:
        return []
    url = "https://v3.football.api-sports.io/fixtures"
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    params = {"league": league_id, "season": season, "status": "FT"}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)
        if response.status_code != 200:
            log(f"Errore fixtures terminati lega {league_id}: HTTP {response.status_code}")
            return []
        return response.json().get("response", [])
    except Exception as e:
        log(f"Errore get_fixtures_terminati({league_id}): {e}")
        return []


def aggiorna_storico_minutaggi_lega(league_id, season, max_fixtures=None):
    """Scarica i gol delle partite terminate di una lega/stagione non ancora processate e
    aggiorna lo storico locale (gol fatti/subiti per fascia di minuto, separati casa/trasferta,
    per squadra). Elabora al massimo `max_fixtures` nuove partite per chiamata per non consumare
    troppe richieste API in un colpo solo: le partite restanti vengono rimandate alla prossima
    esecuzione (il progresso è tracciato su disco tramite fixture_ids_processati)."""
    global STORICO_MINUTAGGI
    max_fixtures = max_fixtures or STORICO_MAX_FIXTURES_PER_RUN
    league_key = str(league_id)

    lega_dati = STORICO_MINUTAGGI.get(league_key)
    if not lega_dati or lega_dati.get("stagione") != season:
        lega_dati = {"stagione": season, "fixture_ids_processati": [], "squadre": {}, "ultimo_aggiornamento": 0}

    fixtures = get_fixtures_terminati(league_id, season)
    if not fixtures:
        lega_dati["ultimo_aggiornamento"] = time.time()
        STORICO_MINUTAGGI[league_key] = lega_dati
        salva_storico_minutaggi(STORICO_MINUTAGGI)
        return 0

    processati = set(lega_dati["fixture_ids_processati"])
    nuove = [f for f in fixtures if f["fixture"]["id"] not in processati]
    da_processare = nuove[:max_fixtures]
    if da_processare:
        log(f"Storico minutaggi: lega {league_id}, {len(da_processare)} nuove partite da processare (su {len(nuove)} non ancora fatte)")

    for f in da_processare:
        fixture_id = f["fixture"]["id"]
        home_id = f["teams"]["home"]["id"]
        home_name = f["teams"]["home"]["name"]
        away_id = f["teams"]["away"]["id"]
        away_name = f["teams"]["away"]["name"]

        for team_id, nome in ((home_id, home_name), (away_id, away_name)):
            squadra = lega_dati["squadre"].setdefault(str(team_id), {
                "nome": nome,
                "casa": {"partite": 0, "fatti": {b: 0 for b in FASCE_MINUTO}, "subiti": {b: 0 for b in FASCE_MINUTO}},
                "trasferta": {"partite": 0, "fatti": {b: 0 for b in FASCE_MINUTO}, "subiti": {b: 0 for b in FASCE_MINUTO}},
            })
            squadra["nome"] = nome

        lega_dati["squadre"][str(home_id)]["casa"]["partite"] += 1
        lega_dati["squadre"][str(away_id)]["trasferta"]["partite"] += 1

        eventi = fetch_fixture_events(fixture_id)
        for ev in eventi:
            if ev.get("type") != "Goal":
                continue
            if (ev.get("detail") or "").lower() == "missed penalty":
                continue
            minuto = (ev.get("time") or {}).get("elapsed")
            if minuto is None:
                continue
            fascia = fascia_minuto(minuto)
            team_gol_id = (ev.get("team") or {}).get("id")
            if team_gol_id == home_id:
                lega_dati["squadre"][str(home_id)]["casa"]["fatti"][fascia] += 1
                lega_dati["squadre"][str(away_id)]["trasferta"]["subiti"][fascia] += 1
            elif team_gol_id == away_id:
                lega_dati["squadre"][str(away_id)]["trasferta"]["fatti"][fascia] += 1
                lega_dati["squadre"][str(home_id)]["casa"]["subiti"][fascia] += 1

        lega_dati["fixture_ids_processati"].append(fixture_id)
        time.sleep(0.3)

    lega_dati["ultimo_aggiornamento"] = time.time()
    STORICO_MINUTAGGI[league_key] = lega_dati
    salva_storico_minutaggi(STORICO_MINUTAGGI)
    return len(da_processare)


def aggiorna_storico_minutaggi_tutte_leghe():
    """Forza l'aggiornamento di tutte le leghe whitelist adesso (usato da /aggiornastorico)."""
    mappa = risolvi_leghe_whitelist()
    if not mappa:
        log("Storico minutaggi: nessuna lega whitelist risolta, skip aggiornamento")
        return 0
    totale = 0
    for nome, (league_id, season) in mappa.items():
        if not season:
            continue
        totale += aggiorna_storico_minutaggi_lega(league_id, season)
        time.sleep(1)
    log(f"Storico minutaggi: aggiornamento completato, {totale} nuove partite processate in totale")
    return totale


def aggiorna_storico_minutaggi_automatico():
    """Chiamata ad ogni ciclo del loop principale, ma fa qualcosa solo se
    STORICO_AGGIORNAMENTO_AUTOMATICO è attivo (spento di default, vedi config.json). Per ogni lega
    whitelist, se sono passati almeno INTERVALLO_AGGIORNAMENTO_STORICO secondi dall'ultimo
    aggiornamento (dato letto dallo storico su disco, quindi resta valido anche tra un riavvio e
    l'altro del bot), scarica le partite nuove. STORICO_MAX_FIXTURES_PER_RUN è qui un limite
    GLOBALE per l'intera esecuzione (su tutte le leghe insieme, non per singola lega): appena
    raggiunto si interrompe subito, anche prima di controllare le leghe restanti, per evitare che
    un riavvio con decine di leghe mai aggiornate consumi la quota API giornaliera in un colpo
    solo. Le leghe non ancora controllate in questo giro verranno riprese al prossimo ciclo."""
    if not STORICO_AGGIORNAMENTO_AUTOMATICO:
        return
    mappa = risolvi_leghe_whitelist()
    if not mappa:
        return
    now = time.time()
    processate_in_questo_giro = 0
    for nome, (league_id, season) in mappa.items():
        if processate_in_questo_giro >= STORICO_MAX_FIXTURES_PER_RUN:
            log(f"Storico minutaggi: raggiunto il limite di {STORICO_MAX_FIXTURES_PER_RUN} partite per questo ciclo, riprendo al prossimo")
            break
        if not season:
            continue
        lega_dati = STORICO_MINUTAGGI.get(str(league_id), {})
        ultimo = lega_dati.get("ultimo_aggiornamento", 0)
        if now - ultimo < INTERVALLO_AGGIORNAMENTO_STORICO:
            continue
        log(f"Storico minutaggi: aggiornamento automatico lega {nome} ({league_id})")
        processate_in_questo_giro += aggiorna_storico_minutaggi_lega(
            league_id, season, max_fixtures=STORICO_MAX_FIXTURES_PER_RUN - processate_in_questo_giro
        )
        time.sleep(1)


def trova_squadra_in_storico(nome_query):
    """Cerca una squadra per nome (case-insensitive, match parziale) tra tutte le leghe salvate
    nello storico. In caso di più corrispondenze sceglie quella con più partite giocate."""
    query = nome_query.lower().strip()
    candidati = []
    for lega_dati in STORICO_MINUTAGGI.values():
        for squadra in lega_dati.get("squadre", {}).values():
            nome = squadra.get("nome", "")
            if query in nome.lower() or nome.lower() in query:
                partite_totali = squadra["casa"]["partite"] + squadra["trasferta"]["partite"]
                candidati.append((partite_totali, squadra))
    if not candidati:
        return None
    candidati.sort(key=lambda c: -c[0])
    return candidati[0][1]


def genera_grafico_minutaggi(nome_casa, dati_casa, nome_trasferta, dati_trasferta):
    """Grafico con 2 pannelli: distribuzione gol fatti/subiti per fascia di 15 minuti,
    squadra di casa nelle sue partite in casa, squadra ospite nelle sue partite in trasferta."""
    try:
        fig, axes = plt.subplots(2, 1, figsize=(6.5, 6.5), dpi=150)
        fig.patch.set_facecolor('#1e1e1e')

        color_fatti = '#22c55e'
        color_subiti = '#ef4444'
        color_text = '#e5e5e5'
        color_muted = '#888888'

        pannelli = [
            (axes[0], f"{nome_casa} (in casa)", dati_casa),
            (axes[1], f"{nome_trasferta} (in trasferta)", dati_trasferta),
        ]

        x = np.arange(len(FASCE_MINUTO))
        larghezza = 0.35

        for ax, titolo, dati in pannelli:
            ax.set_facecolor('#1e1e1e')
            fatti = [dati["fatti"].get(b, 0) for b in FASCE_MINUTO]
            subiti = [dati["subiti"].get(b, 0) for b in FASCE_MINUTO]

            ax.bar(x - larghezza / 2, fatti, larghezza, color=color_fatti, label="Gol fatti")
            ax.bar(x + larghezza / 2, subiti, larghezza, color=color_subiti, label="Gol subiti")

            ax.set_xticks(x)
            ax.set_xticklabels([f"{b}'" for b in FASCE_MINUTO], fontsize=8, color=color_text)
            ax.tick_params(axis='y', colors=color_muted, labelsize=8)
            partite = dati.get("partite", 0)
            ax.set_title(f"{titolo} - {partite} partite", fontsize=10, color=color_text, loc='left')
            for spine in ax.spines.values():
                spine.set_visible(False)
            ax.legend(fontsize=8, labelcolor=color_text, frameon=False, loc='upper right')

        plt.tight_layout()
        foto_path = os.path.join(os.path.dirname(__file__), f'minutaggi_{int(time.time())}.png')
        plt.savefig(foto_path, format='png', bbox_inches='tight', facecolor='#1e1e1e', edgecolor='none', pad_inches=0.15)
        plt.close()
        return foto_path
    except Exception as e:
        log(f"Errore grafico minutaggi: {e}")
        return None


def cmd_analisi(chat_id, testo_richiesta):
    """/analisi <squadra in casa> - <squadra in trasferta>: mostra la distribuzione storica di
    gol fatti/subiti per fascia di 15 minuti delle due squadre nel proprio ruolo per la partita
    in arrivo, usando lo storico costruito da /aggiornastorico."""
    separatore = " - " if " - " in testo_richiesta else "-"
    if separatore not in testo_richiesta:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Usa: /analisi <squadra in casa> - <squadra in trasferta>\nEs: /analisi Milan - Juventus"}, timeout=5)
        return

    nome_casa_query, nome_trasferta_query = testo_richiesta.split(separatore, 1)
    nome_casa_query = nome_casa_query.strip()
    nome_trasferta_query = nome_trasferta_query.strip()

    squadra_casa = trova_squadra_in_storico(nome_casa_query)
    squadra_trasferta = trova_squadra_in_storico(nome_trasferta_query)

    mancanti = [q for q, s in ((nome_casa_query, squadra_casa), (nome_trasferta_query, squadra_trasferta)) if not s]
    if mancanti:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": f"Nessuno storico trovato per: {', '.join(mancanti)}.\nProva prima /aggiornastorico, oppure controlla il nome."}, timeout=5)
        return

    if squadra_casa["casa"]["partite"] == 0 or squadra_trasferta["trasferta"]["partite"] == 0:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Storico insufficiente per una o entrambe le squadre nel ruolo richiesto (casa/trasferta). Aspetta altre giornate o lancia /aggiornastorico."}, timeout=5)
        return

    foto_path = genera_grafico_minutaggi(
        squadra_casa["nome"], squadra_casa["casa"],
        squadra_trasferta["nome"], squadra_trasferta["trasferta"]
    )
    messaggio = (
        f"{squadra_casa['nome']} vs {squadra_trasferta['nome']}\n"
        f"Distribuzione storica gol per fascia di minuto (stagione corrente)\n"
        f"Verde = gol fatti, Rosso = gol subiti"
    )

    try:
        if foto_path and os.path.exists(foto_path):
            with open(foto_path, 'rb') as photo:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                    data={"chat_id": chat_id, "caption": messaggio},
                    files={"photo": photo}, timeout=15)
            os.remove(foto_path)
        else:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": messaggio}, timeout=5)
    except Exception as e:
        log(f"Errore invio /analisi: {e}")


def cmd_aggiornastorico(chat_id):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "Aggiornamento storico minutaggi in corso, può richiedere qualche minuto..."}, timeout=5)
    totale = aggiorna_storico_minutaggi_tutte_leghe()
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": f"Aggiornamento completato: {totale} nuove partite processate. Se erano tante, alcune potrebbero essere rimandate al prossimo aggiornamento per non sforare i limiti API."}, timeout=5)


# =============================================================================
# BETFAIR - quote 1X2 / Over-Under 2.5 / Goal-NoGoal (login non interattivo con certificato)
# =============================================================================
BETFAIR_RELAY_URL = os.environ.get("BETFAIR_RELAY_URL")
BETFAIR_RELAY_SECRET = os.environ.get("BETFAIR_RELAY_SECRET")
BETFAIR_EVENT_TYPE_CALCIO = "1"

BETFAIR_CONFIGURATO = bool(BETFAIR_RELAY_URL and BETFAIR_RELAY_SECRET)
print(f"Betfair configurato: {'SI' if BETFAIR_CONFIGURATO else 'NO (relay non configurato, funzioni quote disattivate)'}", flush=True)


def betfair_api_call(method, params=None):
    """Chiamata all'API Betfair tramite il relay che gira su un PC con IP italiano
    (richiesto dall'Exchange regolamentato ADM, non raggiungibile dai datacenter di Render).
    Login e sessione sono gestiti dal relay stesso."""
    if not BETFAIR_CONFIGURATO:
        return None
    try:
        response = requests.post(
            f"{BETFAIR_RELAY_URL.rstrip('/')}/betfair-call",
            json={"method": method, "params": params or {}},
            headers={"X-Relay-Secret": BETFAIR_RELAY_SECRET},
            timeout=20
        )
    except Exception as e:
        log(f"Errore rete verso il relay Betfair ({method}): {e}")
        return None

    if response.status_code != 200:
        log(f"Errore relay Betfair ({method}): HTTP {response.status_code} - body: {response.text[:500]!r}")
        return None

    try:
        risultato = response.json()
    except Exception as e:
        log(f"Errore parsing risposta relay Betfair ({method}): {e}")
        return None

    if risultato.get("error"):
        log(f"Errore API Betfair ({method}) dal relay: {risultato['error']}")
        return None
    return risultato.get("result")


def trova_mercati_betfair(home_team, away_team):
    """Cerca l'evento Betfair corrispondente a una partita (ricerca testuale per nome squadra di
    casa, poi verifica che compaia anche quella in trasferta) e restituisce i cataloghi dei
    mercati Match Odds, Over/Under 2.5 Goals e Both Teams To Score, se trovati."""
    eventi = betfair_api_call("listEvents", {
        "filter": {
            "eventTypeIds": [BETFAIR_EVENT_TYPE_CALCIO],
            "textQuery": home_team
        }
    })
    if not eventi:
        return None

    evento_scelto = None
    for e in eventi:
        nome_evento = (e.get("event") or {}).get("name", "").lower()
        if home_team.lower() in nome_evento and away_team.lower() in nome_evento:
            evento_scelto = e["event"]
            break
    if not evento_scelto:
        return None

    cataloghi = betfair_api_call("listMarketCatalogue", {
        "filter": {"eventIds": [evento_scelto["id"]]},
        "marketProjection": ["MARKET_START_TIME", "RUNNER_DESCRIPTION"],
        "maxResults": 50
    })
    if not cataloghi:
        return None

    mercati = {"1x2": None, "over_under_25": None, "goal_nogoal": None}
    for m in cataloghi:
        nome = (m.get("marketName") or "").lower()
        if nome == "match odds":
            mercati["1x2"] = m
        elif "over/under 2.5" in nome:
            mercati["over_under_25"] = m
        elif "both teams to score" in nome:
            mercati["goal_nogoal"] = m
    return mercati if any(mercati.values()) else None


def leggi_quote_mercato(market_id):
    """Legge la miglior quota back disponibile per ogni esito di un mercato Betfair."""
    libri = betfair_api_call("listMarketBook", {
        "marketIds": [market_id],
        "priceProjection": {"priceData": ["EX_BEST_OFFERS"]}
    })
    if not libri:
        return None
    libro = libri[0]
    quote = {}
    for runner in libro.get("runners", []):
        selection_id = runner.get("selectionId")
        prezzi = (runner.get("ex") or {}).get("availableToBack") or []
        quote[selection_id] = prezzi[0]["price"] if prezzi else None
    return quote


def cmd_quotebetfair(chat_id, testo_richiesta):
    """/quotebetfair <squadra casa> - <squadra trasferta>: diagnostica, mostra le quote Betfair
    trovate per la partita (1X2, Over/Under 2.5, Goal/No Goal)."""
    if not BETFAIR_CONFIGURATO:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Integrazione Betfair non configurata (variabili d'ambiente mancanti)."}, timeout=5)
        return

    separatore = " - " if " - " in testo_richiesta else "-"
    if separatore not in testo_richiesta:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Usa: /quotebetfair <squadra casa> - <squadra trasferta>"}, timeout=5)
        return

    home, away = [p.strip() for p in testo_richiesta.split(separatore, 1)]
    mercati = trova_mercati_betfair(home, away)
    if not mercati:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": f"Nessun mercato Betfair trovato per {home} vs {away}."}, timeout=5)
        return

    righe = [f"Quote Betfair {home} vs {away} (chiave Delayed, dati con ritardo):\n"]
    for chiave, etichetta in (("1x2", "1X2"), ("over_under_25", "Over/Under 2.5"), ("goal_nogoal", "Goal/No Goal")):
        mercato = mercati.get(chiave)
        if not mercato:
            righe.append(f"{etichetta}: mercato non trovato")
            continue
        quote = leggi_quote_mercato(mercato["marketId"])
        if not quote:
            righe.append(f"{etichetta}: quote non disponibili")
            continue
        dettagli = []
        for runner in mercato.get("runners", []):
            nome_esito = runner.get("runnerName", "?")
            prezzo = quote.get(runner.get("selectionId"))
            dettagli.append(f"{nome_esito}: {prezzo if prezzo else '?'}")
        righe.append(f"{etichetta}: " + " | ".join(dettagli))

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "\n".join(righe)}, timeout=10)


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

                chat_destinazione = TELEGRAM_CHAT_ID_PREFERITI if str(fixture_id) in FAVORITE_MATCHES else TELEGRAM_CHAT_ID
                invia_notifica_telegram(foto_path, messaggio, chat_id=chat_destinazione)

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
        chat_destinazione = TELEGRAM_CHAT_ID_PREFERITI if is_fav else TELEGRAM_CHAT_ID
        invia_notifica_telegram(foto_path, messaggio, reply_markup=keyboard, chat_id=chat_destinazione)

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
        {"command": "intensita", "description": "Classifica partite live per intensità"},
        {"command": "analisi", "description": "Distribuzione storica gol per fascia di minuto"},
        {"command": "aggiornastorico", "description": "Aggiorna lo storico minutaggi"},
        {"command": "quotebetfair", "description": "Quote Betfair 1X2/Over-Under/Goal-NoGoal"},
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
        invia_report_intensita_automatico(partite_valide)
        aggiorna_storico_minutaggi_automatico()
        log("Attesa 3 minuti...")
        time.sleep(180)