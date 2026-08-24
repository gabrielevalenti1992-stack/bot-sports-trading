import collections
import copy
import hashlib
import json
import re
import traceback
import unicodedata
import time
import datetime
from zoneinfo import ZoneInfo
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
# PERSISTENZA SU DISCO: tutti i file di stato vivono in DATA_DIR invece che nella
# cartella del progetto. Su Render, senza un Persistent Disk collegato, il filesystem del
# container (compresa la cartella del progetto) viene ricreato da zero ad ogni riavvio/redeploy -
# quindi PRIMA di collegare un disco, DATA_DIR resta la cartella dello script (stesso
# comportamento di sempre, niente di nuovo si rompe). DOPO aver collegato un Persistent Disk
# Render e impostato la env var DATA_DIR sul suo mount path (es. /var/data), questi stessi file
# sopravvivono ai riavvii per davvero.
# =============================================================================
DATA_DIR = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
os.makedirs(DATA_DIR, exist_ok=True)
print(f"DATA_DIR: {DATA_DIR} ({'da env var DATA_DIR' if os.environ.get('DATA_DIR') else 'fallback, nessuna env var DATA_DIR impostata'})", flush=True)


def data_path(nome_file):
    return os.path.join(DATA_DIR, nome_file)


def salva_json_atomico(path, obj):
    """Scrive prima su un file temporaneo e poi rinomina, cosi' un crash a metà scrittura
    (es. il processo ucciso durante un redeploy) non lascia un JSON troncato/corrotto sul disco
    persistente - os.replace() è atomico sullo stesso filesystem. Il nome del file temporaneo
    include pid+thread id: più thread (loop live, worker quote, comandi Telegram) possono
    chiamare questa funzione sullo stesso path nello stesso momento, e con un nome fisso
    un thread può rinominare via il tmp file di un altro thread ancora in scrittura,
    causando un FileNotFoundError su os.replace(). Snapshot deepcopy prima del dump: json.dump
    itera l'oggetto e, se un altro thread muta un dict/list annidato durante la serializzazione,
    salta un RuntimeError o scrive dati incoerenti. deepcopy sotto il GIL è atomica rispetto
    alle strutture Python."""
    snapshot = copy.deepcopy(obj)
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp_path, 'w') as f:
        json.dump(snapshot, f)
    os.replace(tmp_path, path)


def verifica_disco_scrivibile():
    """Scrive e rilegge un file di prova in DATA_DIR all'avvio: conferma che la scrittura
    funziona DAVVERO (permessi ok, spazio disponibile), non solo che la cartella esiste - così
    un problema col disco persistente (es. montato ma non scrivibile) si vede subito nei log
    invece di scoprirlo indirettamente da un file di stato mancante dopo un riavvio."""
    test_path = data_path(".disco_test")
    marcatore = str(time.time())
    try:
        with open(test_path, 'w') as f:
            f.write(marcatore)
        with open(test_path, 'r') as f:
            letto = f.read()
        os.remove(test_path)
        if letto != marcatore:
            print(f"DISCO: scrittura/lettura in {DATA_DIR} inconsistente (letto diverso da scritto)", flush=True)
            return False
        print(f"DISCO: scrittura/lettura OK in {DATA_DIR}", flush=True)
        return True
    except Exception as e:
        print(f"DISCO: ERRORE scrittura/lettura in {DATA_DIR}: {e}", flush=True)
        return False


DISCO_SCRIVIBILE = verifica_disco_scrivibile()


# =============================================================================
# SERVER HTTP PER RENDER FREE (fix 501 HEAD) + BATTITO DEL CICLO PRINCIPALE
# =============================================================================
# Il server HTTP vive in un thread daemon separato dal ciclo principale: finché il processo Python
# è vivo risponde, anche se il ciclo che fa il lavoro vero è morto o bloccato. Rispondere sempre
# "200 Bot is running!" rendeva quindi l'endpoint inutile come controllo di salute - un monitor
# esterno lo avrebbe visto verde con il bot fermo da ore.
#
# Il ciclo principale aggiorna questo battito ad ogni giro (vedi segna_battito): se l'ultimo
# aggiornamento è più vecchio della soglia qui sotto, il ciclo non sta più girando e l'endpoint
# risponde 503, che è quello che un monitor esterno sa leggere.
#
# La pausa manuale (/stop) è uno stato SANO: il ciclo non lavora perché gliel'è stato chiesto, e
# svegliare qualcuno di notte per una pausa voluta sarebbe rumore. Aggiorna il battito anche lei,
# dichiarando lo stato, così l'endpoint distingue "fermo apposta" da "fermo e basta".
# Due tempi distinti, perché "vivo" e "funzionante" non sono la stessa cosa: il ciclo principale
# cattura qualunque eccezione e riprova dopo 30s, quindi un bot che fallisce ad ogni giro continua
# a passare dall'inizio del ciclo per sempre. Col solo battito d'inizio sembrerebbe sano.
#   timestamp        -> l'ultima volta che il ciclo è passato di lì (thread vivo)
#   giro_completato  -> l'ultima volta che un giro è arrivato in fondo senza eccezioni
BATTITO_CICLO = {
    "timestamp": time.time(),
    "giro_completato": time.time(),
    "stato": "avvio",
    "ciclo": 0,
    "lavora": False,
}

# Margine sopra l'attesa più lunga possibile tra due giri (INTERVALLO_CICLO_MORTO, 30 min di
# default): un ciclo lento non è un ciclo morto. 15 minuti coprono l'elaborazione di una serata
# piena (due chiamate API per partita, più le attese del limitatore globale quando è saturo)
# senza far scattare falsi allarmi.
MARGINE_BATTITO_SECONDI = 900


def segna_battito(stato, ciclo=None, lavora=True):
    """Registra che il ciclo principale è passato di qui. Chiamata ad ogni giro, compresi quelli
    che non fanno lavoro (pausa manuale, configurazione incompleta): è la prova che il thread è
    vivo, non che abbia trovato qualcosa da fare.

    lavora=False per gli stati in cui NON completare giri è normale (pausa manuale, token
    mancante): lì pretendere un giro completo darebbe un allarme per una situazione voluta."""
    BATTITO_CICLO["timestamp"] = time.time()
    BATTITO_CICLO["stato"] = stato
    BATTITO_CICLO["lavora"] = lavora
    if ciclo is not None:
        BATTITO_CICLO["ciclo"] = ciclo


def segna_giro_completato():
    """Fine di un giro arrivato in fondo senza eccezioni: è questo, non l'inizio, a dire che il
    bot sta davvero lavorando."""
    BATTITO_CICLO["giro_completato"] = time.time()


def stato_salute():
    """(codice_http, testo) per l'endpoint di salute.

    200 = il ciclo principale gira, o è fermo per un motivo voluto (pausa manuale).
    503 = il ciclo non passa più (bloccato/morto), oppure passa ma non completa un giro da troppo
          tempo (eccezione ad ogni ciclo). Sono i due casi che devono svegliare qualcuno."""
    # Letto dal modulo al momento della richiesta, non all'import: il valore vero arriva da
    # config.json, che viene caricato parecchie righe più sotto di qui. Il default copre la
    # finestra di pochi millisecondi tra l'avvio del server e il caricamento della configurazione.
    intervallo_massimo = globals().get("INTERVALLO_CICLO_MORTO", 1800)
    soglia = intervallo_massimo + MARGINE_BATTITO_SECONDI
    adesso = time.time()
    eta_battito = adesso - BATTITO_CICLO["timestamp"]
    eta_giro = adesso - BATTITO_CICLO["giro_completato"]
    righe = [
        f"stato: {BATTITO_CICLO['stato']}",
        f"ultimo passaggio del ciclo: {int(eta_battito)}s fa (soglia {soglia}s)",
        f"ultimo giro completato: {int(eta_giro)}s fa",
        f"ciclo numero: {BATTITO_CICLO['ciclo']}",
    ]
    if eta_battito > soglia:
        righe.insert(0, "BOT FERMO: il ciclo principale non gira più")
        return 503, "\n".join(righe)
    if BATTITO_CICLO["lavora"] and eta_giro > soglia:
        righe.insert(0, "BOT INCEPPATO: i cicli partono ma nessuno arriva in fondo")
        return 503, "\n".join(righe)
    righe.insert(0, "Bot is running!")
    return 200, "\n".join(righe)


class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        codice, testo = stato_salute()
        self.send_response(codice)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(testo.encode("utf-8"))

    def do_HEAD(self):
        # Stesso codice del GET: un monitor esterno può usare HEAD, e rispondere sempre 200 qui
        # vanificherebbe tutto il controllo qui sopra.
        codice, _ = stato_salute()
        self.send_response(codice)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
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

# UptimeRobot (opzionale): serve solo al comando /uptime, che chiede quanto e' stato raggiungibile
# l'endpoint di salute visto da fuori. Senza questa variabile il bot funziona esattamente come
# prima e /uptime spiega come impostarla, invece di fallire con un errore.
# Basta una chiave READ-ONLY: il bot legge lo stato dei monitor e non deve poterli modificare.
UPTIMEROBOT_API_KEY = os.environ.get("UPTIMEROBOT_API_KEY")

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

# Preferiti: bypassano le soglie normali (molto più reattivi), ma non per il minimo indivisibile
# (un singolo tiro non sul bersaglio non merita una notifica intera con foto). Serve un cambiamento
# comunque percepibile dall'ultimo invio: almeno questi tiri totali in più, oppure almeno 1 tiro in
# porta o 1 corner in più negli ultimi 15 min.
SOGLIA_MIN_CAMBIO_PREFERITI = 2

# Salto di ritmo: il criterio sopra misura quanto è cambiato DALL'ULTIMA NOTIFICA, quindi in una
# fase concitata fatta di tanti piccoli incrementi (un tiro per ciclo) non scatta mai, pur essendo
# esattamente il momento in cui la partita merita attenzione. Questo secondo criterio guarda invece
# il totale del blocco di 15 minuti corrente: se il blocco è caldo la notifica parte lo stesso.
# Scatta al massimo una volta per blocco (vedi blocco_ultima_notifica in processa_partita), quindi
# aggiunge al massimo ~6 messaggi in una partita intera e non può degenerare.
SOGLIA_RITMO_NOTIFICA_PREFERITI = 4       # tiri combinati nel blocco
SOGLIA_PORTA_RITMO_NOTIFICA_PREFERITI = 2  # oppure tiri in porta combinati nel blocco

# Preferiti "raffreddati": se passano questi secondi senza che parta nessuna notifica (nessun
# evento abbastanza rilevante), vuol dire che la partita si è spenta - si rimuove automaticamente
# dai preferiti e torna alle regole normali, invece di restare agganciata per sempre a soglie più
# permissive senza motivo.
DURATA_MAX_SENZA_NOTIFICA_PREFERITI = 900  # 15 minuti

# Goleada: oltre questo scarto gol la partita perde valore per il trading e si smette di
# notificarla del tutto (preferiti compresi). Il pareggio invece (scarto 0) è sempre rilevante e
# bypassa qualunque soglia sotto - a differenza di uno scarto "normale" di 1-3 gol, che segue le
# regole standard.
SOGLIA_GOLEADA_STOP_NOTIFICHE = 3

# Auto-preferiti: una partita che si accende merita di essere seguita dal canale preferiti senza
# aspettare che venga cliccata a mano tra le tante notifiche normali.
#
# La regola precedente chiedeva 6 tiri combinati O 2 gol ENTRO IL 12', e chiudeva per sempre la
# finestra subito dopo. Misurata sui log di produzione del 16/08 (12:00-17:31): ~100 valutazioni,
# ~30 finestre chiuse, ZERO partite promosse. I valori migliori visti sono stati "12' tiri=5",
# "12' tiri=4 gol=1", "10' tiri=4": la soglia non è stata raggiunta nemmeno una volta. Tre motivi,
# tutti strutturali e non risolvibili abbassando il numero:
#  - i primi 12 minuti sono la parte meno informativa della partita, ed è anche la finestra in cui
#    l'API spesso non ha ancora pubblicato le statistiche (il 16/08 Beveren e Bielefeld sono
#    rimaste vuote fino oltre il 40'): una partita veniva valutata "tiri=0" perché il dato non
#    c'era, non perché fosse bloccata;
#  - una partita con 1 tiro al 12' può diventare la più interessante della giornata al 60', ma
#    veniva già scartata per sempre ("non verrà più rivalutata");
#  - contava il VOLUME dall'inizio gara, non il RITMO: 6 tiri fra il 60' e il 72' sul pareggio
#    valgono molto più di 6 tiri sparsi nei primi 12 minuti, e non venivano mai visti.
#
# Ora si valuta per tutta la partita, sul ritmo del blocco di 15 minuti corrente (lo stesso delta
# già calcolato per le notifiche e per /intensita, quindi nessuna chiamata API in più), con un
# filtro di contesto e un tetto ai preferiti simultanei.
AUTO_PREFERITI_ATTIVO = True
# --- Rotta 1: i GOL. Non tocca le statistiche, quindi funziona anche quando l'API non le
# pubblica - ed è il motivo per cui è la rotta principale. Il 16/08 l'endpoint statistiche è
# rimasto muto per ore su mezzo mondo (Belgio, Germania, Corea, Giappone, Svezia) mentre i gol
# continuavano ad arrivare regolarmente, col nome del marcatore: una regola che dipende solo dai
# gol non si spegne insieme al feed.
#
# Due gol presto dicono che la partita è viva, ma solo se il punteggio la lascia APERTA: un 2-0 al
# 20' è una partita che si sta chiudendo, un 1-1 è una partita da seguire. Per questo non basta
# contare i gol, serve anche lo scarto (SCARTO_MAX_AUTO_PREFERITI qui sotto): con 2 gol passa solo
# l'1-1, con 3 gol passa il 2-1.
SOGLIA_GOL_AUTO_PREFERITI = 2
MINUTO_GOL_AUTO_PREFERITI = 25
SCARTO_MAX_AUTO_PREFERITI = 1  # pareggio o un gol di scarto
#
# È l'UNICA porta d'ingresso, su richiesta esplicita. Era stata affiancata da una seconda regola
# sul ritmo (tiri nel blocco di 15 minuti) per prendere anche le partite che si accendono senza
# segnare: tolta, perché dipendeva dalle statistiche - cioè proprio dal dato che manca più spesso -
# e perché una porta sola rende prevedibile cosa arriva nel canale. Un 0-0 con assedio quindi non
# entra: resta seguito dalle notifiche normali nella chat principale.
# Tetto ai preferiti simultanei (manuali inclusi): ogni preferito viene ricontrollato ogni
# INTERVALLO_CICLO_MOMENTUM (60s) invece di ogni INTERVALLO_CICLO_ATTIVO (180s), cioè costa il
# triplo di chiamate API. Allentare le soglie senza un tetto porta dritti ai rate-limit.
MAX_PREFERITI_SIMULTANEI = 4
# Verdetto negativo per lo shadow-log: prima veniva registrato alla chiusura della finestra (~13'),
# che ora non esiste più. Si registra una riga sola per partita a questo minuto, così il file resta
# confrontabile (un campione per partita) invece di crescere ad ogni ciclo.
MINUTO_VERDETTO_SHADOW_AUTO_PREFERITI = 75

# --- Rotta 2: il DOMINIO. Si affianca alla rotta gol, non la sostituisce.
#
# La rotta gol qui sopra prende le partite che si sbloccano presto e restano aperte, ed e' cieca
# per costruzione a un 0-0 con assedio: una squadra puo' comandare la partita all'80% senza mai
# segnare e non entra da nessuna parte. E' esattamente il caso che questa seconda porta copre,
# senza toccare la prima - se l'API smette di pubblicare statistiche (16/08, mezzo mondo muto per
# ore) questa rotta non promuove niente, ma la rotta gol continua a funzionare da sola come oggi.
#
# Non e' il ritorno della vecchia regola "ritmo" tolta in precedenza. Quella guardava il VOLUME nel
# blocco di 15 minuti (quanto si gioca), questa guarda lo SQUILIBRIO sui totali di partita (chi la
# sta facendo), e soprattutto calcola_dominio() ritorna None quando le statistiche mancano o sono
# troppo poche: non puo' promuovere per errore su dati assenti, cosa che alla regola ritmo non era
# garantita. Il motivo dell'ingresso resta scritto per esteso nel messaggio del canale, quindi
# resta sempre leggibile a colpo d'occhio se una partita e' entrata per gol o per dominio.
#
# SPENTA DI DEFAULT, ed e' il punto del disegno: da spenta la rotta gira lo stesso ad ogni ciclo e
# scrive tutto nello shadow-log dedicato senza promuovere nulla (vedi
# SHADOW_LOG_AUTO_PREFERITI_DOMINIO_FILE). Le due soglie qui sotto sono stime a occhio, non
# misure: si accende dopo aver guardato i percentili veri raccolti dal log, con lo stesso codice
# che ha prodotto quei dati - non con un ramo diverso mai girato in produzione.
AUTO_PREFERITI_DOMINIO_ATTIVO = False
# Molto piu' alta della soglia con cui si dichiara un dominio in notifica (SOGLIA_QUOTA_DOMINIO,
# 65%): li' basta dire "comanda"; qui si occupa uno dei quattro posti nei preferiti e si triplica
# il costo API di quella partita, quindi serve uno squilibrio netto, non una prevalenza.
SOGLIA_QUOTA_DOMINIO_AUTO_PREFERITI = 78
# I due assi sono accoppiati e vanno alzati insieme: su poco materiale la percentuale impazzisce
# (4 tiri a 1 fa gia' 80%), quindi una quota piu' alta senza un volume piu' alto seleziona
# soprattutto rumore. Doppio del minimo con cui calcola_dominio accetta di dare un verdetto.
VOLUME_MINIMO_DOMINIO_AUTO_PREFERITI = 16
# Isteresi in ingresso: la rotta gol valuta un evento discreto ("e' arrivato il secondo gol") e un
# colpo solo basta. Il dominio e' invece un valore continuo, che puo' toccare la soglia per un
# ciclo e ridiscendere: si promuove solo dopo questi cicli CONSECUTIVI sopra soglia (il contatore
# si azzera appena scende), cioe' ~9 minuti di gioco a ciclo attivo da 180s.
CICLI_DOMINIO_PER_AUTO_PREFERITI = 3

# Gate dominio sulle notifiche generali (chat principale, partite NON preferite): prima di
# valutare le regole a volume grezzo (differenza tiri, partita attiva, refresh forzato, momentum
# 15 min) si chiede se in questa partita STIA COMANDANDO QUALCUNO. Le regole restano identiche, ma
# smettono di essere il criterio d'ingresso e diventano solo la cadenza con cui si aggiorna una
# partita gia' qualificata: senza gate arrivavano in chat anche i 10-9 di tiri, tantissimo gioco
# distribuito equamente, che e' il tipo di partita da cui non si ricava una direzione.
#
# Costante separata da SOGLIA_QUOTA_DOMINIO apposta: quella decide quando SCRIVERE la riga
# "X comanda 70%" dentro una notifica, questa decide se la notifica parta del tutto - due domande
# diverse, che devono poter essere tarate una senza l'altra. Attenzione pero': abbassarla sotto
# SOGLIA_QUOTA_DOMINIO non allarga il gate, perche' sotto quella quota calcola_dominio() non
# dichiara alcun dominio e ritorna None comunque. Alzarla stringe.
DOMINIO_GATE_NOTIFICHE_ATTIVO = True
SOGLIA_QUOTA_DOMINIO_NOTIFICA = 65

# LA FAVORITA CHE NON STA VINCENDO: seconda porta accanto al gate del dominio.
#
# Il gate chiede una cosa sola - chi tira di piu' - e non chiede mai chi DOVEVA vincere. Il 23/08
# Manchester City-Bournemouth e' finito 0-1 all'intervallo senza mai arrivare in chat: il bot
# vedeva Tiri 5-2, Porta 2-2, Area 4-2, cioe' dominio 62%, sotto la soglia. E aveva ragione sui
# tiri: gli xG dicevano 0.49 City contro 0.53 Bournemouth, il dominio del City era tutto possesso
# (68%) e passaggi (330 contro 157), che di proposito qui non contano perche' non anticipano i gol.
#
# Solo che una super-favorita in casa sotto all'intervallo e' esattamente la divergenza che
# interessa: il campo e il tabellone dicono cose diverse da quello che il mercato si aspettava.
# Quella domanda il dominio non la pone, perche' guarda la partita e non il pronostico.
#
# Il dato serve gia' ed e' gia' in casa: le quote 1X2 pre-match sono scaricate col piano giornata e
# calcola_probabilita_no_vig() toglie il margine del bookmaker. Zero chiamate API in piu'.
#
# "Non sta vincendo" comprende il pareggio, non solo la sconfitta: per una squadra data al 75%+ un
# pareggio e' gia' un risultato che il mercato non aveva messo in conto.
FAVORITA_IN_DIFFICOLTA_ATTIVO = True

# Silenzio finche' l'API non pubblica le statistiche di una partita (motivazione estesa in testa a
# deve_notificare). La partita resta seguita e continua ad alimentare gli shadow-log: sparisce solo
# dalla chat, e ci torna da sola appena arrivano i primi dati.
SILENZIO_SENZA_STATISTICHE_ATTIVO = True

# Feed statistiche bloccato: l'API continua a rispondere, ma con la STESSA IDENTICA risposta di
# prima mentre la partita va avanti (motivazione estesa in impronta_statistiche). Visto il 23/08 su
# Venezia-Lecce: ferma su "Tiri 3-0, Corner 0-0" dal 24' al 44', mentre la partita era davvero a
# 7-1 di tiri e 3-0 di corner. Il bot la spegneva con "nessun tiro cambiato" e non arrivava mai in
# chat - proprio una di quelle che vanno viste (75% di possesso, xG 0.59 contro 0.04).
FEED_CONGELATO_ATTIVO = True
# Le partite congelate trovate nel ciclo corrente, raccolte qui e mandate in UN messaggio solo da
# invia_riepilogo_feed_congelati(). Un avviso per partita si e' rivelato insostenibile: il 23/08 ne
# sono partiti otto in due minuti, perche' il blocco del feed non e' l'eccezione che sembrava -
# Trabzonspor-Basaksehir e' rimasta ferma su "Tiri 14-7 | Porta 7-2 | Corner 5-1" per 17 minuti, e
# non era sola.
FEED_CONGELATI_CICLO = []
MINUTI_FEED_CONGELATO = 25  # minuti di GIOCO con la risposta identica prima di dichiararlo bloccato
# Ogni buco di almeno tanti minuti viene loggato quando si RICHIUDE (non in chat, solo nei log):
# serve a misurare la distribuzione vera dei buchi invece di tarare la soglia sopra a occhio.
MINUTI_GAP_FEED_DA_MISURARE = 5

# Lo scarto goleada blocca anche i gol (motivazione estesa in testa a deve_notificare): a
# SOGLIA_GOLEADA_STOP_NOTIFICHE+1 gol di distanza la partita e' decisa, e sapere quale gol l'ha
# decisa non cambia niente di operativo.
GOLEADA_BLOCCA_ANCHE_I_GOL = True
SOGLIA_PROB_FAVORITA = 0.75   # probabilita' no-vig pre-match per considerarla favorita netta
MINUTO_MINIMO_FAVORITA_IN_DIFFICOLTA = 30

# Un aggiornamento di routine per blocco di 15 minuti e per partita, nella chat principale (vedi il
# commento esteso dentro deve_notificare). Serve perche' le quattro regole generali guardano lo
# stato assoluto e non il cambiamento: una volta che una partita e' sbilanciata restano vere per
# sempre, e la stessa partita tornava in chat ad ogni ciclo. Gli eventi forzati - gol, rosso,
# rigore, recupero - non sono toccati: passano molto prima di questo controllo.
UN_AGGIORNAMENTO_PER_BLOCCO_ATTIVO = True

# Messaggio live: nel canale preferiti una partita viene ricontrollata ogni 60s per tutta la gara,
# e ogni aggiornamento era finora un messaggio nuovo - decine di foto quasi identiche impilate, in
# cui l'ultima riga utile e' sempre in fondo e le precedenti sono gia' scadute. Qui invece gli
# aggiornamenti di routine RISCRIVONO il messaggio precedente (editMessageMedia: foto e didascalia
# insieme), cosi' in cima al canale c'e' sempre una sola scheda viva per partita.
#
# Due cose NON vengono mai riscritte, e sono il motivo per cui questo non spegne le notifiche:
# Telegram non suona su un edit, quindi un evento che deve farsi sentire (gol, rosso, rigore,
# recupero appena concluso) manda sempre un messaggio NUOVO - che diventa poi la scheda viva da
# aggiornare. E ad ogni blocco di 15 minuti si ricomincia da un messaggio nuovo, cosi' il canale
# conserva comunque un filo storico leggibile (~6 schede a partita invece di ~1 sola per 90
# minuti, o delle ~40-90 di prima) invece di una sola riga che cambia di nascosto.
MESSAGGIO_LIVE_PREFERITI_ATTIVO = True

# Una scheda sola per partita: quando ne parte una nuova, quella vecchia viene CANCELLATA.
#
# L'edit sopra tiene viva una sola scheda finche' si resta nello stesso blocco di 15 minuti, ma
# appena si cambia blocco - o arriva un gol, o si e' cliccato Momentum - nasce un messaggio nuovo e
# il precedente resta li' sotto, fermo a dati ormai scaduti. In chat si finisce per leggere una
# scheda vecchia credendola attuale, che e' peggio del non averla.
#
# Niente va perso: il testo della scheda porta con se' tutto lo storico che conta (marcatori con
# il minuto, cartellini, rigori, confronto 1°T/2°T), e il grafico viene rigenerato ogni volta sui
# dati aggiornati. La scheda cancellata era una fotografia vecchia della stessa partita, non un
# pezzo di informazione che esiste solo li'.
ELIMINA_SCHEDA_PRECEDENTE_ATTIVO = True

# Momentum "appiccicato" alla partita: cliccare il bottone su una notifica dice che di QUESTA
# partita si vuole vedere l'andamento, non che lo si vuole vedere una volta sola.
#
# Prima il grafico viveva solo dentro il messaggio su cui si era cliccato: la notifica successiva
# tornava alle sole barre, e per rivedere il momentum bisognava ricliccare ogni volta. Ora la
# richiesta resta memorizzata per il resto della partita e ogni notifica successiva nasce gia'
# combinata (barre + momentum), esattamente come succede da sempre per i preferiti.
MOMENTUM_PERSISTENTE_ATTIVO = True

# Backoff sulle statistiche che non arrivano mai per una singola partita (soglie e motivazione
# estesa accanto a SOGLIA_SENZA_STATISTICHE, dove sta il resto della copertura statistiche).
BACKOFF_STATISTICHE_ASSENTI_ATTIVO = True

# Shadow-log auto-preferiti: registra su disco le statistiche reali di ogni partita al momento
# della valutazione (sia che scatti l'auto-preferito sia che la finestra si chiuda senza
# scattare), senza cambiare alcun comportamento. Serve a raccogliere dati reali per calibrare le
# soglie sopra con percentili veri invece che a occhio, una volta accumulate abbastanza partite.
SHADOW_LOG_AUTO_PREFERITI_FILE = data_path("shadow_log_auto_preferiti.jsonl")

# Shadow-log della rotta dominio, in un file suo e non mescolato a quello sopra: le due rotte
# vanno lette separatamente (una si misura in gol e minuti, l'altra in quota e volume pesato), e
# tenerle in un solo file costringerebbe a filtrarle ad ogni analisi - stesso motivo per cui
# valore, strategie e auto-preferiti hanno gia' tre file distinti.
#
# Registra il PICCO di dominio della partita, non solo il valore all'istante del verdetto: la
# domanda a cui questi dati devono rispondere e' "con soglia X e N cicli consecutivi, quante
# partite sarebbero entrate e quali", e serve sapere quanto in alto e' arrivata ciascuna, non
# quanto valeva al 75'. Con il picco, la quota al picco, il volume in quel momento e la striscia
# consecutiva piu' lunga si puo' ricalcolare a tavolino l'esito di qualunque coppia di soglie
# senza dover rigirare una stagione.
SHADOW_LOG_AUTO_PREFERITI_DOMINIO_FILE = data_path("shadow_log_auto_preferiti_dominio.jsonl")

# Shadow-log valore: stesso principio (silenzioso, nessun comportamento visibile) ma per validare
# in futuro se la probabilità no-vig del mercato pre-match, incrociata con le statistiche live,
# avrebbe previsto meglio l'esito reale - una riga "snapshot" ad ogni notifica live e una riga
# "risultato_finale" a fine partita, da incrociare offline per fixture_id. Nessuna soglia o
# semaforo finché non ci sono abbastanza partite reali per calibrarli (vedi Fase 2).
SHADOW_LOG_VALORE_FILE = data_path("shadow_log_valore.jsonl")

# Chiusura degli shadow-log delle partite sparite dal feed live (motivazione estesa in
# chiudi_shadow_log_partite_sparite): senza, gli snapshot raccolti durante la partita restano
# orfani per sempre, ed e' esattamente cio' che era successo - 642 partite con snapshot e ZERO
# risultati finali.
CHIUSURA_SHADOW_LOG_PARTITE_SPARITE_ATTIVA = True
MAX_CHIUSURE_SHADOW_LOG_PER_CICLO = 10  # tetto: quando finisce un turno intero non si ammassano chiamate
TENTATIVI_MAX_CHIUSURA_SHADOW_LOG = 3   # oltre si rinuncia: meglio perdere una partita che riprovare per sempre

# Shadow-log strategie: stesso principio, ma per le sei strategie (Assedio, Fascia calda,
# Rimonta, Concretezza, xG per tiro, Qualità - non più comandi Telegram, solo logica interna).
# Ad ogni ciclo (stesso ritmo dello snapshot valore sopra, dati
# già scaricati per le notifiche normali, zero chiamate API in più) si valutano tutte e sei sulla
# partita corrente e si registra quali scattano - anche quando NESSUNA scatta, altrimenti si
# misurerebbe solo "cosa succede quando scatta" senza sapere cosa succede quando non scatta (lo
# stesso bias di selezione visto per il valore). A fine partita una riga "risultato_finale" con
# punteggio e gol (minuto + squadra) per poter incrociare offline: quando una strategia scatta,
# il gol che "promette" arriva davvero, e più spesso di quando non scatta?
SHADOW_LOG_STRATEGIE_FILE = data_path("shadow_log_strategie.jsonl")

# Momentum: rilevazioni minime nello storico prima di generare il grafico (2 punti = 1 sola
# barra, che riempie tutto lo spazio e sembra un blocco pieno invece di un andamento leggibile -
# es. dopo un riavvio del bot, che azzera lo storico in memoria). Sotto questa soglia si preferisce
# non mandare nessun grafico piuttosto che mandarne uno inutile/fuorviante.
MOMENTUM_MIN_STORICO = 6

# Quante didascalie di notifica tenere per partita (vedi "didascalie_notifiche" in processa_partita):
# servono a far ritrovare al bottone Momentum il testo esatto gia' mandato con quella notifica. Un
# preferito viene notificato ogni INTERVALLO_CICLO_MOMENTUM (60s) per tutta la gara, quindi senza un
# tetto se ne accumulerebbe un centinaio per fixture, tutte riscritte su disco ad ogni ciclo.
# Momentum si clicca sulle notifiche recenti: le piu' vecchie sono peso morto.
MAX_DIDASCALIE_RICORDATE = 30

# Pesi per l'indice di intensità (usato dal comando /intensita e dal report automatico)
PESO_INTENSITA_TIRI = 1
PESO_INTENSITA_PORTA = 2
PESO_INTENSITA_CORNER = 1

# Peso per il delta di xG (expected_goals) nel grafico /momentum: un tiro pesa ~0.05-0.3 xG,
# quindi il peso è più alto degli altri per rendere il contributo comparabile (es. 0.2 xG in un
# intervallo pesa quanto ~2 tiri in porta). NOTA: xGOT (expected goals on target) non è un campo
# fornito da API-Football (verificato) - solo xG "semplice" è disponibile, quindi è l'unica
# metrica di qualità del tiro che possiamo usare, non xGOT.
PESO_MOMENTUM_XG = 10

# Report automatico di intensità: ogni quanto (secondi) inviarlo, una volta che i dati sono pronti.
# Disattivato di default su richiesta esplicita: resta comunque disponibile a mano con /intensita.
REPORT_INTENSITA_AUTOMATICO_ATTIVO = False
INTERVALLO_REPORT_INTENSITA = 900  # 15 minuti
ULTIMO_REPORT_INTENSITA = 0

# Diagnostica automatica pipeline dati: ogni quanto (secondi) ricontrollare da soli, dentro il
# ciclo principale, se tracciamento/statistiche/quote/shadow-log delle partite live stanno
# funzionando. Attiva di default (a differenza del report intensità sopra) perché qui l'obiettivo
# è accorgersi da soli di un problema, non richiedere all'utente di lanciare un comando per
# controllare - vedi esegui_diagnostica_automatica().
DIAGNOSTICA_AUTOMATICA_ATTIVA = True
INTERVALLO_DIAGNOSTICA_AUTOMATICA = 1800  # 30 minuti
ULTIMA_DIAGNOSTICA_AUTOMATICA = 0
# Da quanto tempo l'ultimo punto statistiche raccolto rende una partita "ferma". Serve perché tutti
# i controlli sulle statistiche guardavano se lo storico fosse VUOTO, e lo storico è cumulativo:
# bastava un solo punto raccolto al 5' perché una partita risultasse sana per il resto della gara,
# anche con le statistiche ferme da un'ora. È esattamente il buco che il guasto del feed del 16/08
# avrebbe reso invisibile su tutte le partite che avevano già raccolto qualcosa prima che l'API
# smettesse di pubblicare. Il /diagnostica manuale l'età dell'ultimo punto la calcolava già
# ("ultimo aggiornamento Ns fa"): mancava solo a quello automatico, che è quello che avvisa da solo.
# 900s = 15 minuti, cioè cinque cicli attivi saltati di fila: abbastanza per non segnalare un
# rate-limit passeggero, poco per accorgersi di uno stallo vero mentre la partita è ancora in corso.
SOGLIA_STATISTICHE_FERME = 900
# Anomalie già mandate in chat, per partita: {fixture_id: {"STATISTICHE", ...}}. Serve a NON
# ripetere ogni 30 minuti la stessa identica anomalia sulla stessa partita (una partita senza
# statistiche generava lo stesso avviso, legenda inclusa, per tutti i 90 minuti). Si notifica al
# primo rilevamento; se l'anomalia rientra, la voce viene tolta e un'eventuale ricomparsa torna a
# essere notificabile. Ripulita a fine partita da pulisci_partite_terminate().
# Persistita su disco (stesso motivo del resto dello stato partite): tenerla solo in memoria
# significava che ad ogni riavvio del bot (redeploy, crash) la deduplica si azzerava, e la STESSA
# anomalia - condizione mai cambiata sulla partita - veniva rimandata in chat come se fosse nuova.
ANOMALIE_DIAGNOSTICA_FILE = data_path("anomalie_diagnostica_notificate.json")


def carica_anomalie_diagnostica_notificate():
    if os.path.exists(ANOMALIE_DIAGNOSTICA_FILE):
        try:
            with open(ANOMALIE_DIAGNOSTICA_FILE, 'r') as f:
                dati = json.load(f)
            return {int(fid): set(categorie) for fid, categorie in dati.items()}
        except Exception as e:
            print(f"Errore lettura {ANOMALIE_DIAGNOSTICA_FILE}: {e}", flush=True)
    return {}


def salva_anomalie_diagnostica_notificate(dati):
    salva_json_atomico(ANOMALIE_DIAGNOSTICA_FILE, {str(fid): sorted(categorie) for fid, categorie in dati.items()})


ANOMALIE_DIAGNOSTICA_NOTIFICATE = carica_anomalie_diagnostica_notificate()

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

# Piano giornata: una volta al giorno (ora locale Italia) si scarica con UNA chiamata /fixtures?date=
# l'elenco di tutte le partite del giorno, si filtrano quelle nei campionati whitelist e si
# costruiscono le "finestre attive" (kickoff -> kickoff+durata stimata) in cui vale la pena
# interrogare live=all a ritmo sostenuto. Fuori da queste finestre il ciclo rallenta (vedi
# INTERVALLO_CICLO_MORTO) invece di continuare a interrogare l'API ogni 3 minuti per niente.
ORA_GENERAZIONE_PIANO_GIORNATA = 12  # ora locale Italia in cui (ri)generare il piano
DURATA_STIMATA_PARTITA_MINUTI = 130  # 90' + recupero + intervallo + margine di sicurezza
MARGINE_PRE_KICKOFF_MINUTI = 10  # anticipo con cui il ciclo torna "attivo" prima del kickoff previsto
INTERVALLO_CICLO_ATTIVO = 180  # secondi tra un ciclo e l'altro dentro una finestra attiva (come oggi)
INTERVALLO_CICLO_MORTO = 1800  # secondi tra un ciclo e l'altro fuori da ogni finestra attiva (30 min)
INTERVALLO_CICLO_MOMENTUM = 60  # secondi tra un controllo e l'altro per i preferiti (grafico momentum più denso)

# Quote 1X2 pre-partita: un solo bookmaker fisso (non "il primo che risponde", altrimenti il
# numero mostrato non è confrontabile da una partita all'altra) e un solo mercato (Match Winner),
# risolti per nome sui riferimenti reali dell'API invece di un ID hardcoded indovinato.
ODDS_BOOKMAKER_NOME = "Bet365"
ODDS_BET_NOME = "Match Winner"
ODDS_REFRESH_MINUTI_PRIMA_KICKOFF = 90  # rifà la chiamata quote quando manca meno di così al kickoff

# Pausa automatica notturna: fuori da questa fascia (ora locale Italia) il bot NON manda
# notifiche Telegram (proattive: notifiche live, risultato finale, auto-preferiti) - ma il
# monitoraggio (statistiche, quote, shadow-log) resta attivo 24 ore su 24, per non perdere dati
# utili alla validazione futura e per non lasciare orfane le partite che finiscono proprio a
# cavallo dell'orario di stop. Le risposte a comandi manuali (es. /live a qualsiasi ora) non sono
# toccate: girano su un thread separato (poll_callbacks) che non passa da questo controllo.
# È un meccanismo indipendente dalla pausa manuale /stop (nessuno stato salvato su disco, si
# ricalcola ogni ciclo dall'orologio): un /stop per un weekend intero non viene "riattivato" da
# questo alle 12, e viceversa questo non manda notifiche mentre l'utente ha messo /stop.
ORARIO_ATTIVO_INIZIO_ORA = 12
ORARIO_ATTIVO_INIZIO_MINUTO = 0
ORARIO_ATTIVO_FINE_ORA = 23
ORARIO_ATTIVO_FINE_MINUTO = 30

# Shadow-log valore: snapshot periodico per partita monitorata, indipendente dal fatto che scatti
# o meno una notifica - registrare solo nei momenti "notevoli" (soglie superate) introdurrebbe un
# bias di selezione documentato nella letteratura sulla calibrazione delle previsioni (si
# valuterebbe il modello solo nei momenti ad alta attività, non su un quadro rappresentativo
# dell'intera partita). 15 minuti è un compromesso tra granularità e dimensione del file di log.
INTERVALLO_SNAPSHOT_VALORE = 900

# Minuti di recupero: se superano questa soglia in un tempo (1° o 2°), la partita merita una
# notifica dedicata anche se le altre soglie (tiri, momentum...) non sono soddisfatte, perché più
# recupero significa più tempo utile per operare su una squadra in attacco. Per i preferiti la
# soglia non si applica: qualsiasi recupero (anche solo 1') viene comunque segnalato.
SOGLIA_RECUPERO_LUNGO_MINUTI = 3

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
    # Brasile e Argentina (Brasile già coperto da "Serie A"/"Serie B"), Colombia, Uruguay, Bolivia
    "Liga Profesional Argentina", "Copa de la Liga Profesional", "Primera A", "Primera División",
    "Division Profesional", "División Profesional",
    # Nome ESATTO restituito da API-Football (confermato da una notifica reale: "UEFA Europa
    # Conference League", non solo "Conference League") - tenute anche le forme brevi come
    # ulteriore match esatto, nel dubbio che qualche endpoint le usi ancora così.
    "UEFA Champions League", "UEFA Europa League", "UEFA Europa Conference League",
    "Champions League", "Europa League", "Conference League", "UEFA Super Cup",
    # Rete di sicurezza per i turni di qualificazione: da conferma empirica (una notifica reale
    # su una partita del 3° turno di qualificazione) l'API-Football usa lo STESSO nome della fase
    # a gironi/knockout anche per le qualificazioni (es. "UEFA Europa Conference League" anche per
    # Drita-Tre Fiori, 3° turno). Queste varianti "Qualifying"/"Qualification" restano qui per
    # coprire l'eventualità che l'API le usi in qualche caso - se non vengono mai restituite non
    # cambia nulla, è un'aggiunta innocua.
    "UEFA Champions League Qualifying", "UEFA Europa League Qualifying",
    "UEFA Europa Conference League Qualifying",
    "UEFA Champions League Qualification", "UEFA Europa League Qualification",
    "UEFA Europa Conference League Qualification",
    "World Cup", "Euro Championship", "Copa America", "Copa Libertadores",
    # Supercoppe di lega nazionali (l'equivalente locale della Community Shield inglese)
    "Community Shield", "Supercoppa Italiana", "Supercopa de Espana", "DFL-Supercup",
    "Trophee des Champions", "Johan Cruijff Schaal", "Supertaca",
    # Coppe nazionali seguite (vedi COPPE_NAZIONALI_SEGUITE per la deroga sulle esclusioni)
    "Coppa Italia", "FA Cup", "League Cup", "Copa del Rey", "DFB Pokal", "Coupe de France", "KNVB Beker", "Taca de Portugal", "Scottish Cup", "Scottish League Cup",
]

# Nomi di campionato IDENTICI usati da paesi diversi nell'API (l'API-Football non li distingue
# nel nome, solo nel campo "country"): senza un controllo aggiuntivo sul paese, es. il "Premier
# League" del Kazakistan o il "Segunda División" dell'Uruguay (nessuna statistica reale)
# passerebbero il filtro pensato per Inghilterra/Spagna, dato lo stesso nome esatto.
PAESE_ATTESO_LEGA_AMBIGUA = {
    # Coppe con nome condiviso da piu' federazioni: "FA Cup" e "League Cup" esistono con lo stesso
    # identico nome in piu' paesi, e senza il vincolo passerebbero tutte.
    "fa cup": "england",
    "league cup": "england",
    "premier league": "england",
    "championship": "england",
    "league one": "england",
    "league two": "england",
    "super league": "switzerland",
    "super liga": "serbia",
    "premiership": "scotland",
    "first division a": "belgium",
    "pro league": "belgium",
    "segunda división": "spain",
    "segunda division": "spain",
    "nb i": "hungary",
}

# Competizioni internazionali: richiesto un match ESATTO del nome (non una sottostringa), per non
# intercettare le versioni continentali di altre confederazioni con nome simile (es. "AFC
# Champions League", "CAF Champions League" contengono "Champions League" come sottostringa ma
# non hanno statistiche reali).
COMPETIZIONI_INTERNAZIONALI_MATCH_ESATTO = {
    "uefa champions league", "uefa europa league", "uefa europa conference league",
    "champions league", "europa league", "conference league", "uefa super cup",
    "world cup", "euro championship", "copa america", "copa libertadores",
    "uefa champions league qualifying", "uefa europa league qualifying",
    "uefa europa conference league qualifying",
    "uefa champions league qualification", "uefa europa league qualification",
    "uefa europa conference league qualification",
}

# Coppe nazionali seguite. Sono state riammesse su richiesta, ma non basta toglierle dall'elenco
# delle escluse: senza la deroga qui sotto il meccanismo delle esclusioni per mancanza statistiche
# le spegnerebbe quasi subito, ed e' il motivo per cui erano state tenute fuori.
#
# In coppa la copertura cambia PARTITA per partita, non a livello di competizione: una squadra di
# Serie A che gioca in casa di una di Serie C gioca su un campo senza rilevazione, e nello stesso
# turno un'altra partita fra due squadre di A ha i dati completi. Bastavano tre partite di
# contorno nei primi turni per far sparire l'INTERA coppa per 24h - semifinali e finale comprese -
# e dopo cinque turni per toglierla dalla whitelist per sempre.
COPPE_NAZIONALI_SEGUITE = {
    "coppa italia", "fa cup", "league cup", "copa del rey", "dfb-pokal", "dfb pokal",
    "coupe de france", "knvb beker", "taca de portugal",
    "scottish cup", "scottish league cup",
}

# Competizioni per cui le esclusioni per mancanza statistiche NON si applicano, perche' li' la
# copertura e' una proprieta' della singola partita e non della competizione: le coppe nazionali
# per il motivo sopra, le internazionali perche' nei turni di qualificazione mescolano federazioni
# con copertura molto diversa (Kosovo-San Marino con dati parziali accanto a partite complete).
COMPETIZIONI_COPERTURA_VARIABILE = COMPETIZIONI_INTERNAZIONALI_MATCH_ESATTO | COPPE_NAZIONALI_SEGUITE

# Sottoinsieme delle competizioni UEFA sopra per cui ha senso cercare l'andata (fase di
# qualificazione/playoff andata-ritorno delle 3 coppe per club): esclude Mondiali/Europei/Copa
# America/Libertadores/Supercoppe, che non sono andata-ritorno tra le stesse due squadre.
COMPETIZIONI_UEFA_ANDATA_RITORNO = {
    "uefa champions league", "champions league",
    "uefa champions league qualifying", "uefa champions league qualification",
    "uefa europa league", "europa league",
    "uefa europa league qualifying", "uefa europa league qualification",
    "uefa europa conference league", "conference league",
    "uefa europa conference league qualifying", "uefa europa conference league qualification",
}

# Controllo largo (non un match esatto) sul campo "round" restituito dall'API per capire se il
# turno è di quelli giocati andata/ritorno: meglio provare la ricerca dell'andata anche quando la
# dicitura esatta del round non è quella prevista (rischio: una chiamata H2H in più, a vuoto) che
# non provarla mai per una dicitura leggermente diversa da quella immaginata.
def _e_round_andata_ritorno(round_str):
    r = (round_str or "").lower()
    return any(parola in r for parola in ("leg", "qualif", "play-off", "playoff"))

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
    SOGLIA_MIN_CAMBIO_PREFERITI = config.get("soglia_min_cambio_preferiti", SOGLIA_MIN_CAMBIO_PREFERITI)
    SOGLIA_RITMO_NOTIFICA_PREFERITI = config.get("soglia_ritmo_notifica_preferiti", SOGLIA_RITMO_NOTIFICA_PREFERITI)
    SOGLIA_PORTA_RITMO_NOTIFICA_PREFERITI = config.get("soglia_porta_ritmo_notifica_preferiti", SOGLIA_PORTA_RITMO_NOTIFICA_PREFERITI)
    DURATA_MAX_SENZA_NOTIFICA_PREFERITI = config.get("durata_max_senza_notifica_preferiti", DURATA_MAX_SENZA_NOTIFICA_PREFERITI)
    SOGLIA_GOLEADA_STOP_NOTIFICHE = config.get("soglia_goleada_stop_notifiche", SOGLIA_GOLEADA_STOP_NOTIFICHE)
    AUTO_PREFERITI_ATTIVO = config.get("auto_preferiti_attivo", AUTO_PREFERITI_ATTIVO)
    SOGLIA_GOL_AUTO_PREFERITI = config.get("soglia_gol_auto_preferiti", SOGLIA_GOL_AUTO_PREFERITI)
    MINUTO_GOL_AUTO_PREFERITI = config.get("minuto_gol_auto_preferiti", MINUTO_GOL_AUTO_PREFERITI)
    SCARTO_MAX_AUTO_PREFERITI = config.get("scarto_max_auto_preferiti", SCARTO_MAX_AUTO_PREFERITI)
    MAX_PREFERITI_SIMULTANEI = config.get("max_preferiti_simultanei", MAX_PREFERITI_SIMULTANEI)
    AUTO_PREFERITI_DOMINIO_ATTIVO = config.get("auto_preferiti_dominio_attivo", AUTO_PREFERITI_DOMINIO_ATTIVO)
    SOGLIA_QUOTA_DOMINIO_AUTO_PREFERITI = config.get("soglia_quota_dominio_auto_preferiti", SOGLIA_QUOTA_DOMINIO_AUTO_PREFERITI)
    VOLUME_MINIMO_DOMINIO_AUTO_PREFERITI = config.get("volume_minimo_dominio_auto_preferiti", VOLUME_MINIMO_DOMINIO_AUTO_PREFERITI)
    CICLI_DOMINIO_PER_AUTO_PREFERITI = config.get("cicli_dominio_per_auto_preferiti", CICLI_DOMINIO_PER_AUTO_PREFERITI)
    DOMINIO_GATE_NOTIFICHE_ATTIVO = config.get("dominio_gate_notifiche_attivo", DOMINIO_GATE_NOTIFICHE_ATTIVO)
    SOGLIA_QUOTA_DOMINIO_NOTIFICA = config.get("soglia_quota_dominio_notifica", SOGLIA_QUOTA_DOMINIO_NOTIFICA)
    UN_AGGIORNAMENTO_PER_BLOCCO_ATTIVO = config.get("un_aggiornamento_per_blocco_attivo", UN_AGGIORNAMENTO_PER_BLOCCO_ATTIVO)
    BACKOFF_STATISTICHE_ASSENTI_ATTIVO = config.get("backoff_statistiche_assenti_attivo", BACKOFF_STATISTICHE_ASSENTI_ATTIVO)
    FAVORITA_IN_DIFFICOLTA_ATTIVO = config.get("favorita_in_difficolta_attivo", FAVORITA_IN_DIFFICOLTA_ATTIVO)
    SILENZIO_SENZA_STATISTICHE_ATTIVO = config.get("silenzio_senza_statistiche_attivo", SILENZIO_SENZA_STATISTICHE_ATTIVO)
    CHIUSURA_SHADOW_LOG_PARTITE_SPARITE_ATTIVA = config.get("chiusura_shadow_log_partite_sparite_attiva", CHIUSURA_SHADOW_LOG_PARTITE_SPARITE_ATTIVA)
    FEED_CONGELATO_ATTIVO = config.get("feed_congelato_attivo", FEED_CONGELATO_ATTIVO)
    MINUTI_FEED_CONGELATO = config.get("minuti_feed_congelato", MINUTI_FEED_CONGELATO)
    GOLEADA_BLOCCA_ANCHE_I_GOL = config.get("goleada_blocca_anche_i_gol", GOLEADA_BLOCCA_ANCHE_I_GOL)
    SOGLIA_PROB_FAVORITA = config.get("soglia_prob_favorita", SOGLIA_PROB_FAVORITA)
    MINUTO_MINIMO_FAVORITA_IN_DIFFICOLTA = config.get("minuto_minimo_favorita_in_difficolta", MINUTO_MINIMO_FAVORITA_IN_DIFFICOLTA)
    MESSAGGIO_LIVE_PREFERITI_ATTIVO = config.get("messaggio_live_preferiti_attivo", MESSAGGIO_LIVE_PREFERITI_ATTIVO)
    ELIMINA_SCHEDA_PRECEDENTE_ATTIVO = config.get("elimina_scheda_precedente_attivo", ELIMINA_SCHEDA_PRECEDENTE_ATTIVO)
    MOMENTUM_PERSISTENTE_ATTIVO = config.get("momentum_persistente_attivo", MOMENTUM_PERSISTENTE_ATTIVO)
    SOLO_LEGHE_CON_STATISTICHE = config.get("solo_leghe_con_statistiche", SOLO_LEGHE_CON_STATISTICHE)
    LEGHE_CON_STATISTICHE = config.get("leghe_con_statistiche", LEGHE_CON_STATISTICHE)
    PESO_INTENSITA_TIRI = config.get("peso_intensita_tiri", PESO_INTENSITA_TIRI)
    PESO_INTENSITA_PORTA = config.get("peso_intensita_porta", PESO_INTENSITA_PORTA)
    PESO_INTENSITA_CORNER = config.get("peso_intensita_corner", PESO_INTENSITA_CORNER)
    REPORT_INTENSITA_AUTOMATICO_ATTIVO = config.get("report_intensita_automatico_attivo", REPORT_INTENSITA_AUTOMATICO_ATTIVO)
    INTERVALLO_REPORT_INTENSITA = config.get("intervallo_report_intensita", INTERVALLO_REPORT_INTENSITA)
    DIAGNOSTICA_AUTOMATICA_ATTIVA = config.get("diagnostica_automatica_attiva", DIAGNOSTICA_AUTOMATICA_ATTIVA)
    INTERVALLO_DIAGNOSTICA_AUTOMATICA = config.get("intervallo_diagnostica_automatica", INTERVALLO_DIAGNOSTICA_AUTOMATICA)
    INTERVALLO_AGGIORNAMENTO_STORICO = config.get("intervallo_aggiornamento_storico", INTERVALLO_AGGIORNAMENTO_STORICO)
    STORICO_MAX_FIXTURES_PER_RUN = config.get("storico_max_fixtures_per_run", STORICO_MAX_FIXTURES_PER_RUN)
    STORICO_AGGIORNAMENTO_AUTOMATICO = config.get("storico_aggiornamento_automatico", STORICO_AGGIORNAMENTO_AUTOMATICO)
    ORA_GENERAZIONE_PIANO_GIORNATA = config.get("ora_generazione_piano_giornata", ORA_GENERAZIONE_PIANO_GIORNATA)
    DURATA_STIMATA_PARTITA_MINUTI = config.get("durata_stimata_partita_minuti", DURATA_STIMATA_PARTITA_MINUTI)
    MARGINE_PRE_KICKOFF_MINUTI = config.get("margine_pre_kickoff_minuti", MARGINE_PRE_KICKOFF_MINUTI)
    INTERVALLO_CICLO_ATTIVO = config.get("intervallo_ciclo_attivo", INTERVALLO_CICLO_ATTIVO)
    INTERVALLO_CICLO_MORTO = config.get("intervallo_ciclo_morto", INTERVALLO_CICLO_MORTO)
    INTERVALLO_CICLO_MOMENTUM = config.get("intervallo_ciclo_momentum", INTERVALLO_CICLO_MOMENTUM)
    SOGLIA_RECUPERO_LUNGO_MINUTI = config.get("soglia_recupero_lungo_minuti", SOGLIA_RECUPERO_LUNGO_MINUTI)
    ODDS_BOOKMAKER_NOME = config.get("odds_bookmaker_nome", ODDS_BOOKMAKER_NOME)
    ODDS_BET_NOME = config.get("odds_bet_nome", ODDS_BET_NOME)
    ODDS_REFRESH_MINUTI_PRIMA_KICKOFF = config.get("odds_refresh_minuti_prima_kickoff", ODDS_REFRESH_MINUTI_PRIMA_KICKOFF)
    ORARIO_ATTIVO_INIZIO_ORA = config.get("orario_attivo_inizio_ora", ORARIO_ATTIVO_INIZIO_ORA)
    ORARIO_ATTIVO_INIZIO_MINUTO = config.get("orario_attivo_inizio_minuto", ORARIO_ATTIVO_INIZIO_MINUTO)
    ORARIO_ATTIVO_FINE_ORA = config.get("orario_attivo_fine_ora", ORARIO_ATTIVO_FINE_ORA)
    ORARIO_ATTIVO_FINE_MINUTO = config.get("orario_attivo_fine_minuto", ORARIO_ATTIVO_FINE_MINUTO)
    INTERVALLO_SNAPSHOT_VALORE = config.get("intervallo_snapshot_valore", INTERVALLO_SNAPSHOT_VALORE)
    print(f"Soglie caricate da config.json: diff={DIFF_TIRI_SOGLIA}, tot={TIRI_TOTALI_ATTIVA}, min={MINUTI_ATTIVA}, int={INTERVALLO_FORZATO}", flush=True)
    print(f"Piano giornata: generazione alle {ORA_GENERAZIONE_PIANO_GIORNATA}:00 (Italia), ciclo attivo {INTERVALLO_CICLO_ATTIVO}s / morto {INTERVALLO_CICLO_MORTO}s / preferiti {INTERVALLO_CICLO_MOMENTUM}s", flush=True)
    print(f"Filtro leghe con statistiche: {'ATTIVO' if SOLO_LEGHE_CON_STATISTICHE else 'disattivo'} ({len(LEGHE_CON_STATISTICHE)} leghe in whitelist)", flush=True)
    print(f"Auto-preferiti: {'ATTIVO' if AUTO_PREFERITI_ATTIVO else 'disattivo'} "
          f"({SOGLIA_GOL_AUTO_PREFERITI} gol entro il {MINUTO_GOL_AUTO_PREFERITI}' con max "
          f"{SCARTO_MAX_AUTO_PREFERITI} gol di scarto; max {MAX_PREFERITI_SIMULTANEI} preferiti insieme)",
          flush=True)
    print(f"Rotta dominio auto-preferiti: {'ATTIVA' if AUTO_PREFERITI_DOMINIO_ATTIVO else 'solo shadow-log (non promuove)'} "
          f"(quota >= {SOGLIA_QUOTA_DOMINIO_AUTO_PREFERITI}%, volume >= {VOLUME_MINIMO_DOMINIO_AUTO_PREFERITI}, "
          f"{CICLI_DOMINIO_PER_AUTO_PREFERITI} cicli consecutivi)", flush=True)
    print(f"Gate dominio notifiche generali: {'ATTIVO' if DOMINIO_GATE_NOTIFICHE_ATTIVO else 'disattivo'} "
          f"(quota >= {SOGLIA_QUOTA_DOMINIO_NOTIFICA}%) | messaggio live preferiti: "
          f"{'ATTIVO' if MESSAGGIO_LIVE_PREFERITI_ATTIVO else 'disattivo'}", flush=True)
    print(f"Silenzio senza statistiche: "
          f"{'ATTIVO' if SILENZIO_SENZA_STATISTICHE_ATTIVO else 'disattivo'} | goleada oltre "
          f"{SOGLIA_GOLEADA_STOP_NOTIFICHE} gol di scarto: "
          f"{'blocca anche i gol' if GOLEADA_BLOCCA_ANCHE_I_GOL else 'i gol passano lo stesso'}",
          flush=True)
    print(f"Favorita che non vince: {'ATTIVA' if FAVORITA_IN_DIFFICOLTA_ATTIVO else 'disattiva'} "
          f"(favorita dal {SOGLIA_PROB_FAVORITA * 100:.0f}% no-vig, dal {MINUTO_MINIMO_FAVORITA_IN_DIFFICOLTA}')", flush=True)
    print(f"Rilevatore feed statistiche bloccato: "
          f"{'ATTIVO' if FEED_CONGELATO_ATTIVO else 'disattivo'} "
          f"(avviso dopo {MINUTI_FEED_CONGELATO}' di gioco con risposta identica)", flush=True)
    print(f"Chiusura shadow-log a fine partita: "
          f"{'ATTIVA' if CHIUSURA_SHADOW_LOG_PARTITE_SPARITE_ATTIVA else 'disattiva'} "
          f"(max {MAX_CHIUSURE_SHADOW_LOG_PER_CICLO} partite per ciclo)", flush=True)
    print(f"Backoff statistiche assenti: "
          f"{'ATTIVO' if BACKOFF_STATISTICHE_ASSENTI_ATTIVO else 'disattivo'}", flush=True)
    print(f"Un aggiornamento per blocco di 15 min (chat principale): "
          f"{'ATTIVO' if UN_AGGIORNAMENTO_PER_BLOCCO_ATTIVO else 'disattivo'} | "
          f"UptimeRobot: {'collegato' if UPTIMEROBOT_API_KEY else 'non collegato'}", flush=True)
    print(f"Scheda precedente eliminata: {'SI' if ELIMINA_SCHEDA_PRECEDENTE_ATTIVO else 'no'} | "
          f"momentum persistente per partita: "
          f"{'SI' if MOMENTUM_PERSISTENTE_ATTIVO else 'no'}", flush=True)
except Exception as e:
    print(f"Soglie default (config.json non trovato o errore): {e}", flush=True)

PAROLE_ESCLUSE = [
    "women", "femminile", "female", "u21", "u20", "u19", "u18", "u17", "u16", "u15",
    "under-21", "under-20", "under-19", "under-18", "under-17", "under 21", "under 20",
    "under 19", "under 18", "under 17", "youth", "amateur", "dilettanti", "regional",
    "reserves", "riserve",
    # Campionati di sviluppo/riserve il cui nome inizia come un campionato whitelist (es. "Premier
    # League 2" contiene "Premier League" e passerebbe il match a sottostringa della whitelist):
    # vanno esclusi esplicitamente qui, non li ferma il controllo U21 perché non contengono "u21".
    "premier league 2", "professional development league",
    # Stesso motivo, dall'altro lato: "National League Cup" CONTIENE "League Cup" (in whitelist per
    # la Carabao Cup inglese) e la superava a confine di parola. E' la coppa in cui i club non-league
    # affrontano le U21 dei club di Premier: il 18/08 ne erano live dodici in contemporanea su 21
    # partite valide totali - Halifax-Derby U21, Gateshead-Nottingham Forest U21, Tamworth-Newcastle
    # U21 e le altre - e la diagnostica ripeteva per ognuna "l'API risponde ma non pubblica
    # statistiche per questa partita". Ventiquattro chiamate per ciclo che non producono mai un
    # dato, ma consumano il limite per-minuto: quella sera Dinamo Zagreb-Viking e Fenerbahce-Lyon,
    # Champions League, restavano a "Statistiche: N/D" con nei log "Rate-limit ancora in
    # raffreddamento, chiamata saltata". Le partite vere perdevano la corsa contro le U21.
    "national league cup"
    # TEST TEMPORANEO: "friendlies", "amichevoli", "friendly" rimossi per verificare grafici/notifiche
    # Ripristinare dopo il test!
]

# Formazioni giovanili e riserve riconosciute dal NOME DELLA SQUADRA, non da quello della lega.
#
# Escludere i campionati uno per uno non bastava, ed e' stato dimostrato due volte nella stessa
# sera: tolta "National League Cup" e' arrivata "Premier League Cup" (Huddersfield Town U21 -
# Gillingham FC U21, statistiche N/D), che passava per la stessa strada - contiene "Premier League"
# e "League Cup", entrambe in whitelist. Dietro c'e' sempre "Professional Development League" e
# "Premier League International Cup", e il prossimo torneo giovanile con un nome che assomiglia a
# quello di un campionato vero rifarebbe lo stesso giro.
#
# Il segnale comune non e' il nome della competizione: sono le squadre. "Newcastle United U21" e'
# una squadra di sviluppo comunque si chiami il torneo in cui gioca, e l'API non pubblica quasi mai
# le sue statistiche. Basta UNA delle due: in queste coppe un club non-league affronta la U21 di un
# club di Premier (Tamworth - Newcastle United U21), quindi chiedere che siano giovanili entrambe
# lascerebbe passare meta' delle partite.
PAROLE_ESCLUSE_SQUADRE = [
    "u23", "u21", "u20", "u19", "u18", "u17", "u16", "u15",
    "under-23", "under-21", "under-20", "under-19", "under-18", "under-17",
    "under 23", "under 21", "under 20", "under 19", "under 18", "under 17",
    "youth", "reserves", "riserve",
]


def squadra_giovanile(nome_squadra):
    """True se il nome e' quello di una formazione giovanile o di riserve.

    Confine di parola, non sottostringa: "u20" non deve intercettare un club che ha quelle tre
    lettere dentro un nome piu' lungo."""
    nome = _senza_accenti(nome_squadra or "")
    return any(re.search(rf"\b{re.escape(parola)}\b", nome) for parola in PAROLE_ESCLUSE_SQUADRE)


def fixture_in_whitelist(fixture):
    """campionato_valido() letto direttamente da una partita dell'API.

    Gemella di partita_tra_giovanili(): stessa forma, stesso uso, cosi' i due filtri si leggono
    insieme dove servono. Prima lo scavo dentro "league" era ricopiato in quattro punti - ciclo
    principale, /live, /intensita e la diagnostica - e bastava aggiungerne un quinto con una chiave
    scritta storta per avere un filtro che si comporta diversamente dagli altri."""
    lega = fixture.get("league") or {}
    return campionato_valido(lega.get("name", ""), lega.get("type", ""), lega.get("country", ""))


def partita_tra_giovanili(fixture):
    """True se almeno una delle due squadre della partita e' giovanile/riserve.

    Si applica al tracciamento automatico (ciclo principale e piano giornata), non ai comandi:
    se l'utente cerca esplicitamente una partita con /status deve poterla vedere lo stesso."""
    squadre = fixture.get("teams", {}) or {}
    return any(squadra_giovanile((squadre.get(lato) or {}).get("name", ""))
               for lato in ("home", "away"))

# Coppe nazionali (non UEFA) sempre escluse. Da quando campionato_valido() usa solo la whitelist
# statica (niente più cache dinamica dell'API, vedi commento lì) questo elenco è ridondante in
# pratica - nessuna di queste competizioni è comunque in LEGHE_CON_STATISTICHE, quindi verrebbe
# già bloccata dalla whitelist da sola - ma resta come rete di sicurezza esplicita e documentata:
# se in futuro cambia la logica del filtro, queste restano escluse di proposito, non per omissione.
# Le coppe nazionali dei campionati principali sono state riammesse (vedi COPPE_NAZIONALI_SEGUITE):
# qui restano solo le competizioni che NON vogliamo comunque, e non perche' siano coppe ma perche'
# mettono in campo squadre riserve e club non professionistici - EFL Trophy e FA Trophy schierano
# le U21 dei club di Premier League, la Challenge Cup scozzese squadre di leghe inferiori e
# straniere. La Coupe de la Ligue non esiste piu' dal 2020 e resta come rete di sicurezza.
COPPE_NAZIONALI_ESCLUSE = [
    "efl trophy", "fa trophy",
    "coupe de la ligue",
    "scottish challenge cup",
]

# Leghe che, empiricamente, non restituiscono mai statistiche reali dall'API pur superando il
# filtro whitelist (tipicamente perché l'API le marca "coperte" a livello di stagione ma poi
# questa specifica partita non ha dati, o competizioni minori di un paese omonime a un campionato
# whitelist: es. "League Two" scozzese, stesso nome della quarta serie inglese che è in whitelist).
# Dopo SOGLIA_SENZA_STATISTICHE controlli consecutivi senza dati, la lega viene esclusa per
# DURATA_ESCLUSIONE_SENZA_STATISTICHE secondi, per non continuare a pagare 2 chiamate a vuoto ad
# ogni ciclo su partite che non produrranno mai una notifica utile. Persistito su disco: senza,
# ogni riavvio del bot (frequente in fase di sviluppo, con un redeploy per ogni modifica) azzerava
# il contatore prima che arrivasse mai a 3, rendendo l'esclusione di fatto inefficace.
LEGHE_SENZA_STATISTICHE_FILE = data_path("leghe_senza_statistiche.json")
SOGLIA_SENZA_STATISTICHE = 3

# BACKOFF SULLE STATISTICHE CHE NON ARRIVANO MAI PER QUELLA PARTITA.
#
# L'esclusione per lega qui sopra non copre le coppe nazionali e le internazionali
# (COMPETIZIONI_COPERTURA_VARIABILE), ed e' giusto cosi': in DFB Pokal il Bayern-Dortmund le
# statistiche le ha, il Bahlinger SC-Magdeburg no. La copertura e' una proprieta' della singola
# partita, non del torneo. Il risultato pero' era che quelle partite venivano richieste ad ogni
# ciclo per tutti i 90 minuti, sapendo gia' dal decimo che non avrebbero mai risposto.
#
# Log del 23/08, primo turno di DFB Pokal: sei partite in parallelo che contano insieme
# "2 volte di fila", "3 volte", "4", "5", "6", "7"... una chiamata sprecata a testa ad ogni giro.
# Su una giornata di coppa con trenta partite di questo tipo sono centinaia di chiamate che non
# produrranno mai un dato - e il 22/08 la quota giornaliera si e' esaurita davvero, lasciando il
# bot cieco dalle 23:30 alle 02:00.
#
# Non uno stop netto, pero': una partita muta puo' ancora svegliarsi. Il 20/08 Fenerbahce-Lyon era
# vuota al 13' e aveva le statistiche al 17'; il 16/08 Beveren e Bielefeld sono rimaste vuote oltre
# il 40'. Smettere di chiedere per sempre farebbe perdere quelle partite. Si continua a chiedere,
# solo piu' di rado: un tentativo ogni CICLI_* invece che ad ogni ciclo, e la prima risposta con
# dati azzera tutto e riporta al ritmo normale.
CICLI_BACKOFF_STATISTICHE = 3        # da SOGLIA_SENZA_STATISTICHE vuote in poi: 1 tentativo ogni 3 cicli
CICLI_BACKOFF_STATISTICHE_LUNGO = 6  # da SOGLIA_BACKOFF_LUNGO in poi: 1 ogni 6
SOGLIA_BACKOFF_LUNGO = 6
DURATA_ESCLUSIONE_SENZA_STATISTICHE = 24 * 3600  # 24 ore (era 6h)
# Prima di questo minuto una risposta senza statistiche non dice niente sulla copertura della
# lega: nei primissimi minuti l'API spesso non ha ancora pubblicato nulla anche per campionati
# perfettamente coperti. Senza questa soglia bastava una partita sola, controllata 3 volte tra il
# 1' e il 9', per escludere per 24h un intero campionato che le statistiche le pubblica eccome.
#
# Era 15, e 15 non bastava. La regola chiede tre PARTITE diverse, cioè tre prove indipendenti, ma
# in un turno di campionato le partite iniziano tutte insieme: arrivano al minuto di soglia nello
# stesso ciclo, con lo stesso contatore, e i tre verdetti cadono nel giro di pochi secondi. Non
# sono tre prove, è la stessa osservazione ripetuta - e presa mentre l'API può ancora non aver
# pubblicato. Visto in produzione il 15/08 sulla MLS: cinque partite live insieme dal 1', tutte
# vuote fino al 18', tre verdetti fra le 23:58:13 e le 23:58:16 e campionato spento per 24 ore.
# Una partita che al 60' non ha ancora UNA statistica è invece una prova vera: il ritardo di
# pubblicazione è un fenomeno di inizio gara, non dura un'ora. Il prezzo è continuare a chiedere
# statistiche per una mezz'ora in più sulle leghe davvero scoperte, prima di escluderle.
MINUTO_MINIMO_VERDETTO_STATISTICHE = 60


# I contatori salvati prima di questa versione contavano CICLI (una risposta vuota qualunque),
# non PARTITE che non pubblicano statistiche: valori accumulati con la vecchia regola sono molto
# più alti a parità di situazione reale, e riusarli farebbe ri-escludere subito leghe coperte
# appena scade l'esclusione in corso. Alla prima esecuzione con la regola nuova il file viene
# quindi ricostruito da zero: si perdono le esclusioni in essere (comprese quelle sbagliate) e le
# leghe davvero senza statistiche vengono re-imparate in poche partite.
#
# Portata a 3 insieme al minuto minimo qui sopra: le esclusioni decise con la soglia del 15' sono
# state prese su prove troppo deboli (vedi la MLS), e senza questo giro di versione resterebbero
# valide su disco fino alla loro scadenza naturale, cioè fino a 24h dopo il deploy della
# correzione. Azzerando il file, al primo avvio la lega esclusa per sbaglio torna subito visibile.
#
# Portata a 4 insieme al controllo sull'avaria diffusa (vedi avaria_statistiche_diffusa): le
# esclusioni decise durante il guasto del feed del 16/08 - K League 1 e K League 2 alle 11:58, con
# 12 partite vuote su 13 in sei campionati e quattro paesi - sono state prese su prove che il
# controllo nuovo avrebbe scartato in blocco. Restando su disco resterebbero valide fino a 24h
# dopo il deploy della correzione, cioè per quasi tutta la loro durata: tanto varrebbe non aver
# corretto niente. Con questo giro di versione, al primo avvio i campionati esclusi per sbaglio
# tornano subito visibili.
VERSIONE_REGOLA_SENZA_STATISTICHE = 4


def carica_leghe_senza_statistiche():
    if os.path.exists(LEGHE_SENZA_STATISTICHE_FILE):
        try:
            with open(LEGHE_SENZA_STATISTICHE_FILE, 'r') as f:
                contenuto = json.load(f)
            if isinstance(contenuto, dict):
                if contenuto.get("versione") == VERSIONE_REGOLA_SENZA_STATISTICHE:
                    return {(v["country"], v["name"]): v["stato"] for v in contenuto.get("voci", [])}
                print("leghe_senza_statistiche: formato di una versione diversa, riparto da zero", flush=True)
                return {}
            print(f"leghe_senza_statistiche: {len(contenuto)} voci con la vecchia regola "
                  f"(contava i cicli invece delle partite), scartate e riparto da zero", flush=True)
            return {}
        except Exception as e:
            print(f"Errore lettura {LEGHE_SENZA_STATISTICHE_FILE}: {e}", flush=True)
    return {}


def salva_leghe_senza_statistiche(dati):
    voci = [{"country": paese, "name": nome, "stato": stato} for (paese, nome), stato in dati.items()]
    salva_json_atomico(LEGHE_SENZA_STATISTICHE_FILE,
                       {"versione": VERSIONE_REGOLA_SENZA_STATISTICHE, "voci": voci})


LEGHE_SENZA_STATISTICHE = carica_leghe_senza_statistiche()
print(f"leghe_senza_statistiche recuperate da disco: {len(LEGHE_SENZA_STATISTICHE)}", flush=True)


def registra_esito_statistiche(league_country, league_name, disponibili):
    """Va chiamata SOLO quando l'API ha davvero risposto (dati presenti o risposta vuota):
    disponibili=False significa "l'API ha risposto e per questa partita non ci sono statistiche",
    non "la chiamata è fallita". Passare qui anche i fallimenti (rate-limit, timeout, errori di
    rete) faceva escludere per 24h campionati coperti dopo 3 cicli sfortunati di seguito.

    disponibili=False va passato UNA SOLA VOLTA per partita, e solo da una partita che ha già
    accumulato da sola SOGLIA_SENZA_STATISTICHE risposte vuote consecutive (vedi il chiamante):
    qui ogni chiamata vale come "una partita intera di questa lega non ha statistiche", non come
    "un singolo controllo andato a vuoto"."""
    # Nelle competizioni a copertura variabile il verdetto non viene nemmeno registrato: non
    # avrebbe effetto (campionato_valido lo ignora, vedi COMPETIZIONI_COPERTURA_VARIABILE) e
    # produrrebbe un log "Lega X esclusa per 24h" che descrive qualcosa che non succede.
    if _senza_accenti(league_name) in COMPETIZIONI_COPERTURA_VARIABILE:
        return
    chiave = (league_country.lower(), league_name.lower())
    stato = LEGHE_SENZA_STATISTICHE.get(chiave, {"senza_stats_consecutive": 0, "esclusa_fino": 0})
    if disponibili:
        # Nessun motivo di tenere in memoria (e su disco) una lega che le statistiche le pubblica:
        # lasciarci una voce azzerata faceva crescere il file all'infinito, una riga per ogni lega
        # mai vista almeno una volta.
        if chiave in LEGHE_SENZA_STATISTICHE:
            del LEGHE_SENZA_STATISTICHE[chiave]
            salva_leghe_senza_statistiche(LEGHE_SENZA_STATISTICHE)
        return
    stato["senza_stats_consecutive"] += 1
    # esclusa_fino già passato = esclusione vecchia e scaduta, si può riarmare. Il vecchio
    # "not stato['esclusa_fino']" guardava solo se il campo fosse valorizzato, quindi dopo la prima
    # esclusione una lega non veniva MAI più esclusa (il timestamp restava lì, scaduto ma non nullo)
    # finché non tornava a pubblicare statistiche.
    if stato["senza_stats_consecutive"] >= SOGLIA_SENZA_STATISTICHE and stato.get("esclusa_fino", 0) <= time.time():
        stato["esclusa_fino"] = time.time() + DURATA_ESCLUSIONE_SENZA_STATISTICHE
        log(f"  Lega '{league_name}' ({league_country}) esclusa per {DURATA_ESCLUSIONE_SENZA_STATISTICHE // 3600}h: "
            f"{stato['senza_stats_consecutive']} partite senza statistiche")
    LEGHE_SENZA_STATISTICHE[chiave] = stato
    salva_leghe_senza_statistiche(LEGHE_SENZA_STATISTICHE)


def deve_chiedere_statistiche(fixture_id):
    """False quando conviene saltare la chiamata statistiche di questo ciclo per QUESTA partita.

    Vale solo dopo SOGLIA_SENZA_STATISTICHE risposte vuote di fila, cioe' quando l'API ha gia'
    detto tre volte che per questa partita non pubblica niente. Da li' in poi si continua a
    chiedere - una partita muta puo' svegliarsi - ma a intervalli, e l'intervallo si allarga se il
    silenzio continua.

    Chi chiama azzera cicli_saltati_statistiche quando la chiamata viene fatta davvero, e
    stats_vuote_consecutive quando arrivano dati: una risposta buona riporta tutto al ritmo pieno."""
    if not BACKOFF_STATISTICHE_ASSENTI_ATTIVO:
        return True
    stato = stato_partite.get(fixture_id, {})
    vuote = stato.get("stats_vuote_consecutive", 0)
    if vuote < SOGLIA_SENZA_STATISTICHE:
        return True
    ogni = CICLI_BACKOFF_STATISTICHE if vuote < SOGLIA_BACKOFF_LUNGO else CICLI_BACKOFF_STATISTICHE_LUNGO
    # "ogni 3 cicli" = due saltati e poi uno buono.
    return stato.get("cicli_saltati_statistiche", 0) >= ogni - 1


def lega_esclusa_per_mancanza_statistiche(league_country, league_name):
    stato = LEGHE_SENZA_STATISTICHE.get((league_country.lower(), league_name.lower()))
    if not stato:
        return False
    return time.time() < stato.get("esclusa_fino", 0)


# Una risposta vuota non distingue da sola due situazioni molto diverse: "questa lega non ha
# statistiche" (verdetto giusto, la lega va esclusa) e "in questo momento l'API non sta pubblicando
# statistiche per nessuno" (guasto esterno e temporaneo, escludere sarebbe un errore). Il segnale
# che le separa il bot ce l'ha già e finora lo buttava via: quante ALTRE leghe, nello stesso
# momento, stanno rispondendo vuote. Una lacuna di copertura riguarda una lega per volta; un
# guasto del feed le spegne tutte insieme.
#
# Caso reale del 16/08 (log Render, istanza ...-bbwtp, ciclo delle 11:51:49): 12 partite su 13
# vuote nello stesso ciclo, su 6 campionati in whitelist e 4 paesi diversi - Jupiler Pro League
# (Belgio), 2. Bundesliga x3 (Germania), K League 1 x3 e K League 2 x4 (Corea del Sud), J League 2
# (Giappone). L'unica a pubblicare era ADO Den Haag-Groningen (Olanda). Alle 11:58, sette minuti
# dopo il deploy della soglia dei 60', K League 1 e K League 2 sono state comunque escluse per 24h
# con le partite al 61', 62', 62' e 64': il minuto minimo non protegge da un guasto che dura piu'
# di un'ora, perche' le partite ci arrivano lo stesso, al minuto giusto e ancora vuote.
#
# Le osservazioni si tengono su una finestra scorrevole invece che sul singolo ciclo: dentro un
# ciclo le partite sono processate in fila, quindi la prima che vota vedrebbe un quadro ancora
# quasi vuoto e il guasto risulterebbe invisibile proprio a chi decide.
FINESTRA_OSSERVAZIONI_STATISTICHE = 900  # 15 min: ~5 cicli attivi
# Sotto le 3 leghe osservate il campione e' troppo piccolo per parlare di "diffuso" (di notte
# possono esserci due sole leghe live, entrambe davvero scoperte): in quel caso si lascia decidere
# la regola normale. Sopra, si chiede che le leghe vuote siano almeno il doppio di quelle che
# pubblicano - con 12 partite vuote su 13 il rapporto reale era 5 a 1.
LEGHE_VUOTE_MINIME_PER_AVARIA = 3
RAPPORTO_VUOTE_SU_OK_PER_AVARIA = 2
OSSERVAZIONI_STATISTICHE = []  # [(timestamp, (paese, lega), disponibili)]


def registra_osservazione_statistiche(league_country, league_name, disponibili, giornata=""):
    """Annota l'esito statistiche di UNA partita, per avere il quadro d'insieme del momento.

    Diversa da registra_esito_statistiche(): quella riceve un verdetto per lega (raro, pesato,
    persistito su disco), questa riceve ogni singola risposta dell'API (frequente, solo in memoria)
    e serve unicamente a capire se il feed statistiche sta funzionando in generale."""
    adesso = time.time()
    OSSERVAZIONI_STATISTICHE.append((adesso, (league_country.lower(), league_name.lower()), disponibili))
    taglio = adesso - FINESTRA_OSSERVAZIONI_STATISTICHE
    while OSSERVAZIONI_STATISTICHE and OSSERVAZIONI_STATISTICHE[0][0] < taglio:
        OSSERVAZIONI_STATISTICHE.pop(0)
    # Dopo la potatura: registra_giornata_statistiche() interroga avaria_statistiche_diffusa(),
    # che deve vedere anche l'osservazione appena aggiunta.
    registra_giornata_statistiche(league_country, league_name, giornata, disponibili)


def avaria_statistiche_diffusa():
    """True se nella finestra recente le statistiche mancano su troppe leghe diverse insieme.

    Una lega conta come "che pubblica" se ha dato statistiche almeno una volta nella finestra: le
    partite appena iniziate sono vuote anche nei campionati coperti, e non devono far sembrare
    guasto un feed sano."""
    taglio = time.time() - FINESTRA_OSSERVAZIONI_STATISTICHE
    leghe_ok, leghe_viste = set(), set()
    for quando, chiave, disponibili in OSSERVAZIONI_STATISTICHE:
        if quando < taglio:
            continue
        leghe_viste.add(chiave)
        if disponibili:
            leghe_ok.add(chiave)
    leghe_vuote = leghe_viste - leghe_ok
    return (len(leghe_vuote) >= LEGHE_VUOTE_MINIME_PER_AVARIA
            and len(leghe_vuote) >= RAPPORTO_VUOTE_SU_OK_PER_AVARIA * len(leghe_ok))


# =============================================================================
# ESCLUSIONE DEFINITIVA DALLA WHITELIST: leghe che non pubblicano MAI statistiche reali
# =============================================================================
# L'esclusione a 24h qui sopra è una misura di risparmio: smette di pagare due chiamate a vuoto per
# una lega che oggi non dà dati, ma la riammette il giorno dopo. Serve anche un verdetto
# definitivo, perché il bot non serve a sapere il risultato - quello si trova ovunque - ma a
# leggere tiri, tiri in porta, corner e tiri in area mentre la partita è in corso. Un campionato
# che quei quattro numeri non li pubblica mai occupa quota API e spazio in chat senza poter far
# scattare una sola strategia: tanto vale toglierlo dalla whitelist e liberare le chiamate per i
# campionati che i dati li danno.
#
# "Statistiche reali" = le stesse quattro di STATISTICHE_USATE, cioè esattamente ciò che
# ha_statistiche_disponibili() controlla: una risposta con solo possesso palla e falli non conta
# come copertura (vedi il caso Djurgardens del 16/08).
#
# Si contano GIORNATE DI CAMPIONATO, non giorni di calendario né partite né cicli, ed è la
# differenza che rende la regola solida:
#  - contare le PARTITE farebbe arrivare cinque "prove" in un pomeriggio solo, che è la stessa
#    osservazione ripetuta - l'errore già costato le esclusioni sbagliate di MLS e K League;
#  - contare i GIORNI di calendario non è la stessa cosa di contare i turni: una giornata di Serie
#    A si gioca fra sabato, domenica e lunedì, quindi cinque giorni solari sono meno di due turni
#    veri. Cinque giornate di campionato sono invece cinque turni, tipicamente più di un mese.
# La giornata è quella dichiarata dall'API nel campo "round" del fixture (es. "Regular Season -
# 1"), quindi è il turno vero del campionato e non una nostra approssimazione. Il conteggio parte
# da quando il bot comincia a osservare quella lega: le giornate giocate prima non esistono.
GIORNATE_SENZA_STATISTICHE_FILE = data_path("giornate_senza_statistiche.json")
SOGLIA_GIORNATE_SENZA_STATISTICHE = 5
# Le giornate osservate si tengono in memoria per lega: oltre questo numero le più vecchie si
# buttano, tanto il verdetto guarda solo se si arriva a SOGLIA_GIORNATE_SENZA_STATISTICHE.
MAX_GIORNATE_RICORDATE = 30
VERSIONE_REGOLA_GIORNATE_SENZA_STATISTICHE = 1


def carica_giornate_senza_statistiche():
    if os.path.exists(GIORNATE_SENZA_STATISTICHE_FILE):
        try:
            with open(GIORNATE_SENZA_STATISTICHE_FILE, 'r') as f:
                contenuto = json.load(f)
            if (isinstance(contenuto, dict)
                    and contenuto.get("versione") == VERSIONE_REGOLA_GIORNATE_SENZA_STATISTICHE):
                return {(v["country"], v["name"]): v["stato"] for v in contenuto.get("voci", [])}
            print("giornate_senza_statistiche: formato di una versione diversa, riparto da zero", flush=True)
            return {}
        except Exception as e:
            print(f"Errore lettura {GIORNATE_SENZA_STATISTICHE_FILE}: {e}", flush=True)
    return {}


def salva_giornate_senza_statistiche(dati):
    voci = [{"country": paese, "name": nome, "stato": stato} for (paese, nome), stato in dati.items()]
    salva_json_atomico(GIORNATE_SENZA_STATISTICHE_FILE,
                       {"versione": VERSIONE_REGOLA_GIORNATE_SENZA_STATISTICHE, "voci": voci})


GIORNATE_SENZA_STATISTICHE = carica_giornate_senza_statistiche()
print(f"giornate_senza_statistiche recuperate da disco: {len(GIORNATE_SENZA_STATISTICHE)} leghe "
      f"({sum(1 for s in GIORNATE_SENZA_STATISTICHE.values() if s.get('esclusa_definitivamente'))} "
      f"escluse dalla whitelist)", flush=True)


def _giornate_senza_statistiche_contate(stato):
    """Giornate CHIUSE in cui la lega non ha pubblicato niente e il dato è attendibile.

    Una giornata ancora aperta non conta: il 16/08 diverse leghe sono rimaste vuote per oltre 40
    minuti di gioco e poi hanno pubblicato tutto (Beveren, Bielefeld). Finché il turno è in corso
    non si sa ancora com'è andato, e aspettare non costa niente."""
    return [nome for nome, g in stato.get("giornate", {}).items()
            if g.get("chiusa") and not g.get("pubblicato") and not g.get("inattendibile")]


def registra_giornata_statistiche(league_country, league_name, giornata, disponibili):
    """Tiene il conto delle giornate di campionato senza una sola statistica reale.

    `giornata` è il campo "round" del fixture. Se l'API non lo fornisce si ripiega sulla data: la
    regola resta valida, solo un po' più severa (più giornate a parità di turni)."""
    # Stesso motivo del gemello sopra: una coppa non va mai tolta dalla whitelist per le partite
    # dei primi turni, quindi non si conta nemmeno.
    if _senza_accenti(league_name) in COMPETIZIONI_COPERTURA_VARIABILE:
        return
    chiave = (league_country.lower(), league_name.lower())
    stato = GIORNATE_SENZA_STATISTICHE.get(
        chiave, {"giornate": {}, "esclusa_definitivamente": 0})
    if stato.get("esclusa_definitivamente"):
        return

    giornata = (giornata or "").strip()
    if not giornata:
        giornata = "data " + datetime.datetime.now(ZoneInfo("Europe/Rome")).strftime("%Y-%m-%d")

    # Ancoraggio esplicito: il conteggio parte da qui, non da inizio stagione. La prima giornata
    # vista dopo l'attivazione della regola è la "1ª giornata di test" di quel campionato, anche
    # se il campionato è in corso da mesi - delle giornate precedenti non sappiamo niente e non
    # devono pesare. Resta scritto su disco per poter sempre dire da quando lo si sta valutando.
    if not stato.get("prima_giornata"):
        stato["prima_giornata"] = giornata
        stato["iniziato_il"] = time.time()
        log(f"  Lega '{league_name}' ({league_country}): inizio verifica copertura statistiche, "
            f"'{giornata}' conta come 1ª giornata di test")

    giornate = stato.setdefault("giornate", {})
    corrente = giornate.setdefault(giornata, {"pubblicato": False, "inattendibile": False,
                                              "chiusa": False})
    # Vedere una giornata diversa significa che le altre sono finite: da quel momento possono
    # essere giudicate. Chi è già chiusa resta chiusa, così un recupero giocato fuori turno non
    # riapre (né fa ri-contare) un turno già archiviato.
    for nome, altra in giornate.items():
        if nome != giornata:
            altra["chiusa"] = True

    if disponibili:
        # Un solo tiro pubblicato basta: la lega i dati li dà. Si azzera tutto lo storico, non solo
        # la giornata in corso - le giornate vuote di prima erano evidentemente un problema
        # passeggero dell'API, non una lacuna di copertura.
        stato["giornate"] = {giornata: {"pubblicato": True, "inattendibile": False, "chiusa": False}}
        GIORNATE_SENZA_STATISTICHE[chiave] = stato
        salva_giornate_senza_statistiche(GIORNATE_SENZA_STATISTICHE)
        return

    if avaria_statistiche_diffusa():
        corrente["inattendibile"] = True
        # L'avaria si riconosce solo dopo aver visto abbastanza leghe: le prime osservate a
        # finestra fredda (primo ciclo dopo un riavvio) passerebbero di qui con l'avaria ancora
        # invisibile, e quella giornata risulterebbe una prova valida contro la loro lega.
        # Siccome un guasto riguarda tutti, si marcano inattendibili tutte le giornate aperte.
        for altro in GIORNATE_SENZA_STATISTICHE.values():
            for altra in altro.get("giornate", {}).values():
                if not altra.get("chiusa"):
                    altra["inattendibile"] = True

    senza = _giornate_senza_statistiche_contate(stato)
    if len(senza) >= SOGLIA_GIORNATE_SENZA_STATISTICHE:
        stato["esclusa_definitivamente"] = time.time()
        log(f"  Lega '{league_name}' ({league_country}) ESCLUSA DALLA WHITELIST: "
            f"{len(senza)} giornate di campionato senza statistiche reali ({', '.join(sorted(senza))})")
        invia_messaggio_telegram(
            f"🚫 Campionato tolto dalla whitelist\n\n"
            f"{league_name} ({league_country})\n\n"
            f"In {len(senza)} giornate di campionato non ha mai pubblicato una statistica reale "
            f"(tiri, tiri in porta, corner, tiri in area): solo risultati. Non verrà più seguito, "
            f"così la quota API resta ai campionati che i dati li danno.\n\n"
            f"Giornate: {', '.join(sorted(senza))}\n\n"
            f"Le giornate in cui l'API era guasta su molti campionati insieme non sono state "
            f"conteggiate.")
    elif len(giornate) > MAX_GIORNATE_RICORDATE:
        # Si buttano le più vecchie fra quelle che non fanno testo (pubblicate o inattendibili):
        # quelle che contano vanno tenute tutte, sono al massimo SOGLIA_GIORNATE_SENZA_STATISTICHE.
        da_tenere = set(senza) | {giornata}
        for nome in list(giornate)[:-MAX_GIORNATE_RICORDATE]:
            if nome not in da_tenere:
                del giornate[nome]

    GIORNATE_SENZA_STATISTICHE[chiave] = stato
    salva_giornate_senza_statistiche(GIORNATE_SENZA_STATISTICHE)


def lega_esclusa_definitivamente(league_country, league_name):
    stato = GIORNATE_SENZA_STATISTICHE.get((league_country.lower(), league_name.lower()))
    return bool(stato and stato.get("esclusa_definitivamente"))


# =============================================================================
# STATO PARTITE LIVE: punteggi, storico statistiche (usato da /momentum e dal delta 15 min a
# blocchi), snapshot di fine 1°T, cartellini/rigori già notificati. Persistito su disco (a
# differenza di prima, quando era puro dizionario in memoria): un riavvio del bot nel bel mezzo
# di una partita non perde più nulla, riprende esattamente da dov'era. Le chiavi sono fixture_id
# interi lato Python, ma JSON le forza a stringhe: vanno riconvertite al caricamento.
# =============================================================================
STATO_PARTITE_FILE = data_path("stato_partite.json")


def carica_stato_partite():
    if os.path.exists(STATO_PARTITE_FILE):
        try:
            with open(STATO_PARTITE_FILE, 'r') as f:
                dati = json.load(f)
            return {int(k): v for k, v in dati.items()}
        except Exception as e:
            print(f"Errore lettura {STATO_PARTITE_FILE}: {e}", flush=True)
    return {}


def salva_stato_partite(dati):
    salva_json_atomico(STATO_PARTITE_FILE, dati)


stato_partite = carica_stato_partite()
print(f"stato_partite recuperato da disco: {len(stato_partite)} partite (0 = primo avvio o disco non persistente)", flush=True)
ciclo_numero = 0

# =============================================================================
# BACKUP DELLO STORICO MOMENTUM: copia indipendente di stato_partite[fixture_id]["history"], in un
# file separato che NESSUN reset di stato_partite (es. il reset per regresso minuto, vedi
# processa_partita) può cancellare. Nato da un caso reale: una partita preferita con un grafico
# momentum già completo (dal calcio d'inizio) l'ha perso del tutto dopo un riavvio del bot,
# ripartendo "dal 45'" - i punti storici raccolti fino a quel momento non erano recuperabili da
# nessun'altra parte (l'API-Football espone solo i totali CORRENTI, non uno storico minuto per
# minuto: una volta persa la lista di stato_partite non c'è modo di ricostruirla chiedendola di
# nuovo all'API). Con questa copia indipendente, se stato_partite[fixture_id]["history"] risulta
# vuota ma esiste un backup più lungo per lo stesso fixture_id, il grafico momentum può ripartire
# da dove era rimasto invece che da zero.
# =============================================================================
BACKUP_HISTORY_MOMENTUM_FILE = data_path("backup_history_momentum.json")


def carica_backup_history_momentum():
    if os.path.exists(BACKUP_HISTORY_MOMENTUM_FILE):
        try:
            with open(BACKUP_HISTORY_MOMENTUM_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Errore lettura {BACKUP_HISTORY_MOMENTUM_FILE}: {e}", flush=True)
    return {}


def salva_backup_history_momentum(dati):
    salva_json_atomico(BACKUP_HISTORY_MOMENTUM_FILE, dati)


BACKUP_HISTORY_MOMENTUM = carica_backup_history_momentum()

# Storico dei 15 minuti usato da /status, separato da stato_partite: quest'ultimo viene ripulito
# ad ogni ciclo per le partite non più whitelist (pulisci_partite_terminate), quindi una partita
# fuori whitelist (es. una coppa) controllata a mano con /status perderebbe subito lo storico se
# usasse stato_partite. In memoria soltanto, non persistito: non è un problema se si azzera ad un
# riavvio del bot, l'utente lo ricostruisce controllando di nuovo la partita.
STATUS_HISTORY = {}

# =============================================================================
# STATO SILENZIATI (dict con score al momento del silenzio)
# =============================================================================
SILENCED_FILE = data_path("silenced_matches.json")

def load_silenced():
    # try/except come tutti gli altri caricatori da disco: questa funzione gira a livello di
    # modulo, quindi un file illeggibile o troncato non farebbe partire il bot per niente
    # (il processo muore prima ancora di collegarsi a Telegram, e su Render riparte in loop).
    # Meglio ripartire con la lista vuota che restare giù.
    if os.path.exists(SILENCED_FILE):
        try:
            with open(SILENCED_FILE, 'r') as f:
                data = json.load(f)
            if isinstance(data, list):
                return {str(fid): {"score_home": 0, "score_away": 0} for fid in data}
            if isinstance(data, dict):
                return data
            return {}
        except Exception as e:
            print(f"Errore lettura {SILENCED_FILE}: {e}", flush=True)
    return {}

def save_silenced(silenced):
    salva_json_atomico(SILENCED_FILE, silenced)

SILENCED_MATCHES = load_silenced()

# =============================================================================
# STATO PREFERITI
# =============================================================================
FAVORITES_FILE = data_path("favorite_matches.json")

def load_favorites():
    # Stesso motivo di load_silenced(): gira a livello di modulo, un file illeggibile bloccherebbe
    # l'avvio del bot invece di far perdere solo la lista dei preferiti.
    if os.path.exists(FAVORITES_FILE):
        try:
            with open(FAVORITES_FILE, 'r') as f:
                data = json.load(f)
            if isinstance(data, list):
                return set(str(x) for x in data)
            return set()
        except Exception as e:
            print(f"Errore lettura {FAVORITES_FILE}: {e}", flush=True)
    return set()

def save_favorites(favs):
    salva_json_atomico(FAVORITES_FILE, list(favs))

FAVORITE_MATCHES = load_favorites()

# =============================================================================
# OFFSET GETUPDATES TELEGRAM: deve sopravvivere ai riavvii, altrimenti dopo ogni
# restart (redeploy, crash, riavvio Render) poll_callbacks() ripartiva da offset=0
# e Telegram (che tiene in coda gli update non confermati fino a 24h) rimandava
# indietro vecchi callback_query/comandi gia' gestiti in una vita precedente del
# processo - rieseguendo bottoni (es. "Momentum") o comandi MAI ricliccati/inviati
# in quel momento, a insaputa dell'utente.
# =============================================================================
TELEGRAM_OFFSET_FILE = data_path("telegram_offset.json")

def carica_telegram_offset():
    if os.path.exists(TELEGRAM_OFFSET_FILE):
        try:
            with open(TELEGRAM_OFFSET_FILE, 'r') as f:
                return json.load(f).get("offset", 0)
        except Exception as e:
            print(f"Errore lettura {TELEGRAM_OFFSET_FILE}: {e}", flush=True)
    return 0

def salva_telegram_offset(offset):
    salva_json_atomico(TELEGRAM_OFFSET_FILE, {"offset": offset})

# =============================================================================
# STORICO MINUTAGGI (analisi pre-partita /analisi)
# =============================================================================
STORICO_MINUTAGGI_FILE = data_path("storico_minutaggi.json")

def carica_storico_minutaggi():
    if os.path.exists(STORICO_MINUTAGGI_FILE):
        try:
            with open(STORICO_MINUTAGGI_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Errore lettura {STORICO_MINUTAGGI_FILE}: {e}", flush=True)
    return {}

def salva_storico_minutaggi(dati):
    salva_json_atomico(STORICO_MINUTAGGI_FILE, dati)

STORICO_MINUTAGGI = carica_storico_minutaggi()

# =============================================================================
# PIANO GIORNATA (snapshot giornaliero partite whitelist + finestre orarie attive)
# =============================================================================
PIANO_GIORNATA_FILE = data_path("piano_giornata.json")


def carica_piano_giornata():
    if os.path.exists(PIANO_GIORNATA_FILE):
        try:
            with open(PIANO_GIORNATA_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Errore lettura {PIANO_GIORNATA_FILE}: {e}", flush=True)
    return {"data": None, "generato_alle": 0, "partite": [], "finestre_attive": []}


def salva_piano_giornata(piano):
    salva_json_atomico(PIANO_GIORNATA_FILE, piano)


PIANO_GIORNATA = carica_piano_giornata()

# =============================================================================
# PAUSA MANUALE (/stop, /riprendi): quando l'utente sa che non sta seguendo il trading (es. sta
# per disconnettersi per un paio d'ore), può mettere il bot in pausa esplicitamente invece di
# aspettare che lo scheduler adattivo rallenti da solo in base al piano giornata. In pausa il ciclo
# non chiama affatto l'API e non manda notifiche, finché non arriva /riprendi. Persistita su disco
# apposta: un riavvio del bot (es. redeploy) mentre l'utente è offline non deve far ripartire le
# chiamate a sua insaputa.
# =============================================================================
PAUSA_FILE = data_path("bot_pausa.json")
INTERVALLO_PROMEMORIA_PAUSA = 6 * 3600  # 6 ore: ogni quanto ricordare che il bot è ancora in pausa


def carica_pausa():
    if os.path.exists(PAUSA_FILE):
        try:
            with open(PAUSA_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Errore lettura {PAUSA_FILE}: {e}", flush=True)
    return {"in_pausa": False, "dal": 0, "ultimo_promemoria": 0}


def salva_pausa(stato):
    salva_json_atomico(PAUSA_FILE, stato)


STATO_PAUSA = carica_pausa()

# =============================================================================
# MODALITA' ESSENZIALE (/modalitaessenziale, /modalitacompleta): quando attiva, deve_notificare()
# lascia passare SOLO gli eventi forzati (gol, cartellino rosso, rigore, recupero lungo),
# sopprimendo le notifiche di soglia (differenza tiri, momentum, refresh ogni 30 min) anche per i
# preferiti. Persistita su disco come /stop, cosi' un riavvio del bot non la resetta a sua insaputa.
# =============================================================================
MODALITA_FILE = data_path("modalita_notifiche.json")


def carica_modalita():
    if os.path.exists(MODALITA_FILE):
        try:
            with open(MODALITA_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Errore lettura {MODALITA_FILE}: {e}", flush=True)
    return {"essenziale": False}


def salva_modalita(stato):
    salva_json_atomico(MODALITA_FILE, stato)


MODALITA_NOTIFICHE = carica_modalita()

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

def _esegui_comando(chat_id, funzione, args):
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


def togli_bottoni(chat_id, msg_id):
    """Toglie la tastiera da un messaggio gia' inviato: dopo aver silenziato o riattivato una
    partita i bottoni non hanno piu' senso, e lasciarli invita a ricliccarli."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
            json={"chat_id": chat_id, "message_id": msg_id,
                  "reply_markup": json.dumps({"inline_keyboard": []})}, timeout=5)
    except Exception as e:
        log(f"Bottoni non rimossi (message_id {msg_id}): {e}")


def elimina_messaggio(chat_id, message_id):
    """Cancella una scheda superata. Ritorna True se e' stata davvero rimossa.

    Un fallimento non e' un problema da propagare, ed e' anzi il caso normale col passare del
    tempo: Telegram lascia cancellare i propri messaggi solo entro 48 ore, e una scheda che
    l'utente ha gia' cancellato a mano non c'e' piu'. In entrambi i casi il risultato voluto -
    "quella vecchia non deve restare in giro" - e' comunque ottenuto, quindi si logga e basta."""
    if not message_id:
        return False
    try:
        risposta = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id}, timeout=5)
        if risposta.status_code == 200:
            return True
        log(f"Scheda precedente non cancellata (message_id {message_id}): "
            f"HTTP {risposta.status_code} - {risposta.text[:150]}")
    except Exception as e:
        log(f"Scheda precedente non cancellata (message_id {message_id}): {e}")
    return False


def esegui_comando_sicuro(chat_id, funzione, *args):
    """Lancia il comando in un thread separato invece di eseguirlo nel thread che fa polling di
    Telegram: alcuni comandi (es. /intensita) possono girare per decine di secondi o
    più se ci sono molte partite live, e senza questo il polling di /stop e di qualsiasi altro
    comando resterebbe bloccato fino al termine di quello in corso."""
    threading.Thread(target=_esegui_comando, args=(chat_id, funzione, args), daemon=True).start()

# =============================================================================
# THREAD: ASCOLTA CLICK SUI BOTTONI + COMANDI MANUALI
# =============================================================================
def poll_callbacks():
    offset = carica_telegram_offset()
    while True:
        if not TELEGRAM_BOT_TOKEN:
            time.sleep(10)
            continue
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            r = requests.get(url, params={"offset": offset, "limit": 10}, timeout=10)
            # Se Telegram restituisce 5xx o 429 (rate-limit), il corpo e' HTML/testo
            # e r.json() lancia ValueError. Meglio distinguere HTTP errore da JSON
            # malformato: cosi' il log dice cosa e' successo davvero, invece di
            # "JSONDecodeError" opaco.
            if not r.ok:
                log(f"getUpdates HTTP {r.status_code}: {r.text[:200]}")
                updates = []
            else:
                try:
                    updates = r.json().get("result", [])
                except ValueError:
                    log(f"getUpdates risposta non-JSON: {r.text[:200]}")
                    updates = []
            for upd in updates:
                offset = upd["update_id"] + 1
                # Salvato SUBITO, prima di eseguire l'azione: se il processo viene ucciso a
                # metà (redeploy, OOM) l'update risulta gia' "consumato" e non verra' rimandato
                # indietro da Telegram al prossimo avvio - meglio perdere un'azione rara che
                # rieseguirla a sorpresa.
                salva_telegram_offset(offset)

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
                        togli_bottoni(chat_id, msg_id)
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
                        togli_bottoni(chat_id, msg_id)
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": "\U0001F514 Partita riattivata. Torneranno gli alert live."}, timeout=5)

                    elif data.startswith("fav:"):
                        fid = str(int(data.split(":")[1]))
                        if fid in FAVORITE_MATCHES:
                            # Rimozione: stesso trattamento del "silenzia" (bottoni tolti +
                            # messaggio di conferma), invece di lasciare i bottoni lì a fare
                            # domande su un messaggio che ormai non e' piu' un preferito.
                            FAVORITE_MATCHES.discard(fid)
                            save_favorites(FAVORITE_MATCHES)
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                                json={"callback_query_id": cq["id"], "text": "Rimossa dai preferiti"}, timeout=5)
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
                                    "text": "⭐ Partita rimossa dai preferiti. Torna alle notifiche normali nella chat principale (niente più canale dedicato)."
                                }, timeout=5)
                        else:
                            FAVORITE_MATCHES.add(fid)
                            save_favorites(FAVORITE_MATCHES)
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                                json={"callback_query_id": cq["id"], "text": "Aggiunta ai preferiti"}, timeout=5)
                            is_sil = fid in SILENCED_MATCHES
                            mostra_momentum = len(stato_partite.get(int(fid), {}).get("history", [])) >= MOMENTUM_MIN_STORICO
                            keyboard = get_notification_keyboard(int(fid), True, is_sil, mostra_momentum)
                            if keyboard:
                                requests.post(
                                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
                                    json={
                                        "chat_id": chat_id,
                                        "message_id": msg_id,
                                        "reply_markup": json.dumps(keyboard)
                                    }, timeout=5)

                    elif data.startswith("momentum:"):
                        fid_bottone = int(data.split(":")[1])
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                            json={"callback_query_id": cq["id"], "text": "Genero il grafico momentum..."}, timeout=5)
                        # msg_id: cmd_momentum_da_bottone sostituisce la foto di QUESTA notifica
                        # (editMessageMedia) invece di mandare un grafico come messaggio a parte,
                        # cosi' compare esattamente dove si e' cliccato, non altrove in chat.
                        esegui_comando_sicuro(chat_id, cmd_momentum_da_bottone, fid_bottone, msg_id)

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
                        elif azione == "piano":
                            esegui_comando_sicuro(chat_id, cmd_piano)
                        elif azione == "stop":
                            esegui_comando_sicuro(chat_id, cmd_stop)
                        elif azione == "riprendi":
                            esegui_comando_sicuro(chat_id, cmd_riprendi)
                        elif azione == "modalitaessenziale":
                            esegui_comando_sicuro(chat_id, cmd_modalitaessenziale)
                        elif azione == "modalitacompleta":
                            esegui_comando_sicuro(chat_id, cmd_modalitacompleta)
                        elif azione == "testpreferiti":
                            esegui_comando_sicuro(chat_id, cmd_testpreferiti)
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

                    elif cmd == "/momentum":
                        if not args:
                            requests.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={"chat_id": chat_id, "text": "Usa: /momentum <nome squadra>", "parse_mode": "Markdown"}, timeout=5)
                            continue
                        esegui_comando_sicuro(chat_id, cmd_momentum, " ".join(args).lower().strip("<>").strip())

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

                    elif cmd == "/piano":
                        esegui_comando_sicuro(chat_id, cmd_piano)

                    elif cmd == "/stop":
                        esegui_comando_sicuro(chat_id, cmd_stop)

                    elif cmd == "/riprendi":
                        esegui_comando_sicuro(chat_id, cmd_riprendi)

                    elif cmd == "/modalitaessenziale":
                        esegui_comando_sicuro(chat_id, cmd_modalitaessenziale)

                    elif cmd == "/modalitacompleta":
                        esegui_comando_sicuro(chat_id, cmd_modalitacompleta)

                    elif cmd == "/testpreferiti":
                        esegui_comando_sicuro(chat_id, cmd_testpreferiti)

                    elif cmd == "/shadowlog":
                        esegui_comando_sicuro(chat_id, cmd_shadowlog)

                    elif cmd == "/shadowlogstrategie":
                        esegui_comando_sicuro(chat_id, cmd_shadowlogstrategie)

                    elif cmd == "/shadowlogdominio":
                        esegui_comando_sicuro(chat_id, cmd_shadowlogdominio)

                    elif cmd == "/diagnostica":
                        esegui_comando_sicuro(chat_id, cmd_diagnostica)

                    elif cmd == "/coperturaleghe":
                        esegui_comando_sicuro(chat_id, cmd_coperturaleghe)

                    elif cmd == "/dominio":
                        esegui_comando_sicuro(chat_id, cmd_dominio)

                    elif cmd == "/funzioni":
                        esegui_comando_sicuro(chat_id, cmd_funzioni)

                    elif cmd == "/apiusage":
                        esegui_comando_sicuro(chat_id, cmd_apiusage)

                    elif cmd == "/uptime":
                        esegui_comando_sicuro(chat_id, cmd_uptime)
        except Exception as e:
            log(f"Errore poll callback: {e}\n{traceback.format_exc()}")
        time.sleep(5)

callback_thread = threading.Thread(target=poll_callbacks, daemon=True)
callback_thread.start()

# =============================================================================
# FUNZIONI UTILITY
# =============================================================================
def log(msg):
    print(msg, flush=True)


ULTIMO_AVVISO_CANALE_PREFERITI = 0
INTERVALLO_AVVISO_CANALE_PREFERITI = 3600  # 1 ora: non ripetere l'avviso troppo spesso


def _e_canale_preferiti_dedicato(destinatario):
    """True se il destinatario è il canale preferiti E questo è davvero diverso dalla chat
    principale (cioè TELEGRAM_CHAT_ID_PREFERITI è stata impostata a un valore diverso)."""
    return destinatario == TELEGRAM_CHAT_ID_PREFERITI and TELEGRAM_CHAT_ID_PREFERITI != TELEGRAM_CHAT_ID


def _avvisa_e_fallback_canale_preferiti(dettaglio):
    """Se l'invio al canale preferiti dedicato fallisce, avvisa nella chat principale (al massimo
    una volta ogni INTERVALLO_AVVISO_CANALE_PREFERITI) invece di scoprirlo solo scavando nei log
    di Render, e ritorna True se va ritentato l'invio nella chat principale come ripiego, per non
    perdere la notifica."""
    global ULTIMO_AVVISO_CANALE_PREFERITI
    log(f"Invio al canale preferiti fallito: {dettaglio}")
    now = time.time()
    if now - ULTIMO_AVVISO_CANALE_PREFERITI >= INTERVALLO_AVVISO_CANALE_PREFERITI:
        ULTIMO_AVVISO_CANALE_PREFERITI = now
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={
                    'chat_id': TELEGRAM_CHAT_ID,
                    'text': (
                        "⚠️ Non riesco a mandare messaggi al canale preferiti dedicato "
                        f"(TELEGRAM_CHAT_ID_PREFERITI): {dettaglio}\n"
                        "Controlla che il bot sia amministratore di quel canale/gruppo e che l'ID sia corretto "
                        "(usa /testpreferiti per riprovare). Nel frattempo le notifiche dei preferiti arrivano qui."
                    )
                }, timeout=10)
        except Exception:
            pass
    return True


def invia_messaggio_telegram(testo, chat_id=None):
    if not CONFIG_VALIDA:
        log(f"[SKIP Telegram] Config mancante: {testo[:50]}")
        return
    destinatario = chat_id or TELEGRAM_CHAT_ID
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {'chat_id': destinatario, 'text': testo, 'parse_mode': 'Markdown'}
        response = requests.post(url, data=data, timeout=10)
        log(f"Telegram testo -> {destinatario} - Status: {response.status_code} - {response.text[:200]}")
        if response.status_code != 200 and _e_canale_preferiti_dedicato(destinatario):
            if _avvisa_e_fallback_canale_preferiti(f"HTTP {response.status_code} - {response.text[:200]}"):
                data['chat_id'] = TELEGRAM_CHAT_ID
                requests.post(url, data=data, timeout=10)
    except Exception as e:
        log(f"Errore invio testo Telegram: {e}")


def invia_notifica_telegram(foto_path, messaggio, reply_markup=None, chat_id=None):
    """Ritorna il message_id del messaggio inviato (o None se non disponibile/fallito) - usato
    dal chiamante per ricordare quale didascalia è stata mandata a quale messaggio, così il
    bottone "📈 Momentum" può in seguito sostituire solo la foto senza perdere il testo con tutti
    i dati (quote, statistiche, gol...) già inviato in quella notifica."""
    if not CONFIG_VALIDA:
        log(f"[SKIP Telegram] Config mancante: {messaggio[:50]}")
        return None
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
                log(f"Telegram foto -> {destinatario} - Status: {response.status_code} - {response.text[:200]}")
            if response.status_code != 200 and _e_canale_preferiti_dedicato(destinatario):
                if _avvisa_e_fallback_canale_preferiti(f"HTTP {response.status_code} - {response.text[:200]}"):
                    return invia_notifica_telegram(foto_path, messaggio, reply_markup=reply_markup, chat_id=TELEGRAM_CHAT_ID)
            if response.status_code == 200:
                return response.json().get("result", {}).get("message_id")
            return None
        else:
            invia_messaggio_telegram(messaggio, chat_id=destinatario)
            return None
    except Exception as e:
        log(f"Errore invio Telegram: {e}")
        return None


def aggiorna_notifica_telegram(message_id, foto_path, messaggio, reply_markup=None, chat_id=None):
    """Riscrive una notifica gia' mandata (foto e didascalia insieme, editMessageMedia) invece di
    aggiungerne una nuova. Ritorna True se l'aggiornamento e' andato a buon fine.

    Un False non e' un errore da propagare: e' semplicemente "questa scheda non si puo' piu'
    aggiornare" (cancellata a mano, troppo vecchia, rate-limit passeggero), e il chiamante ha
    sempre la strada del messaggio nuovo. Per questo qui non si avvisa e non si fa fallback sul
    canale: una notifica non viene mai persa per colpa di un edit fallito."""
    if not CONFIG_VALIDA:
        return False
    if not message_id or not foto_path or not os.path.exists(foto_path):
        return False
    destinatario = chat_id or TELEGRAM_CHAT_ID
    try:
        media = {"type": "photo", "media": "attach://photo",
                 "caption": messaggio, "parse_mode": "Markdown"}
        dati = {"chat_id": destinatario, "message_id": message_id, "media": json.dumps(media)}
        if reply_markup:
            dati["reply_markup"] = json.dumps(reply_markup)
        with open(foto_path, 'rb') as photo:
            response = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageMedia",
                data=dati, files={"photo": photo}, timeout=15)
        if response.status_code == 200:
            log(f"Telegram scheda live aggiornata -> {destinatario} (message_id {message_id})")
            return True
        # "message is not modified": l'edit non serviva perche' il contenuto era gia' quello. Non
        # e' un fallimento, e rimandare un messaggio nuovo identico sarebbe il contrario di quello
        # che questa funzione esiste per evitare.
        if "not modified" in response.text:
            return True
        log(f"Scheda live non aggiornabile (message_id {message_id}): "
            f"HTTP {response.status_code} - {response.text[:200]}")
        return False
    except Exception as e:
        log(f"Errore aggiornamento scheda live Telegram: {e}")
        return False


def _senza_accenti(testo):
    """Nome confrontabile: minuscolo e senza segni diacritici.

    API-Football restituisce i nomi accentati ("Trophée des Champions", "Supercopa de España",
    "Supertaça Cândido de Oliveira") mentre in whitelist alcune voci erano scritte in ASCII: il
    confronto a stringa grezza non le faceva mai combaciare, e quelle competizioni non comparivano
    fra le partite live pur essendo state messe in elenco apposta. Si vedeva anche il rattoppo:
    per Süper Lig e Primera División erano state aggiunte a mano ENTRAMBE le forme, accentata e no.
    Normalizzando qui il problema sparisce per tutte, comprese quelle che verranno aggiunte dopo."""
    return "".join(c for c in unicodedata.normalize("NFD", (testo or "").lower())
                   if unicodedata.category(c) != "Mn")


# Chiavi normalizzate anch'esse: "segunda división" nella mappa sopra è accentata, e senza questo
# la guardia sul paese per quella lega smetterebbe di trovarla appena il nome viene normalizzato.
PAESE_ATTESO_LEGA_AMBIGUA_NORM = {_senza_accenti(k): v for k, v in PAESE_ATTESO_LEGA_AMBIGUA.items()}


def _lega_in_whitelist_statica(nome, league_country):
    """True se 'nome' (già lowercase) corrisponde a una voce della whitelist statica.
    Match a confine di parola (non sottostringa grezza): "NB I" non deve intercettare "NB III"
    solo perché ne è un prefisso. Per le competizioni internazionali serve un match esatto (non
    parziale, per non intercettare le versioni continentali di altre confederazioni). Per i nomi
    ambigui condivisi da più paesi (es. "Premier League" Inghilterra/Kazakistan) serve anche il
    paese giusto, altrimenti passerebbero tutte le leghe omonime prive di statistiche reali."""
    paese = (league_country or "").lower()
    nome = _senza_accenti(nome)
    for lega in LEGHE_CON_STATISTICHE:
        lega_lower = _senza_accenti(lega)
        if lega_lower in COMPETIZIONI_INTERNAZIONALI_MATCH_ESATTO:
            if nome == lega_lower:
                return True
            continue
        if not re.search(rf"\b{re.escape(lega_lower)}\b", nome):
            continue
        paese_atteso = PAESE_ATTESO_LEGA_AMBIGUA_NORM.get(lega_lower)
        if paese_atteso and paese != paese_atteso:
            continue
        return True
    return False


def campionato_valido(league_name, league_type, league_country=""):
    # Normalizzato su ENTRAMBI i lati del confronto: l'API manda i nomi accentati e alcune voci
    # degli elenchi sono scritte in ASCII (vedi _senza_accenti). Normalizzare solo il nome della
    # lega romperebbe le voci accentate degli elenchi, per esempio "taça de portugal".
    nome = _senza_accenti(league_name)
    for parola in PAROLE_ESCLUSE:
        if _senza_accenti(parola) in nome:
            return False
    for coppa in COPPE_NAZIONALI_ESCLUSE:
        # Confine di parola, non sottostringa grezza: "coppa italia" non deve intercettare
        # "Supercoppa Italiana" (whitelistata a parte) solo perché ne è una sottostringa - stesso
        # criterio già usato da _lega_in_whitelist_statica per lo stesso identico motivo.
        if re.search(rf"\b{re.escape(_senza_accenti(coppa))}\b", nome):
            return False
    if league_type and league_type.lower() not in ["league", "cup", "championship"]:
        return False
    # Le competizioni internazionali a match esatto (Champions/Europa/Conference League, Mondiali,
    # ecc.) NON sono soggette a questa esclusione globale per lega: sono un pugno di voci scelte a
    # mano che vogliamo sempre tracciare, e nei turni di qualificazione mescolano federazioni con
    # copertura statistiche molto diversa partita per partita (es. Conference League 3° turno:
    # Kosovo-San Marino con dati parziali, altre partite dello stesso turno magari complete) - non
    # è un problema "a livello di intera competizione". Se una singola partita poco coperta facesse
    # scattare l'esclusione, TUTTA la competizione sparirebbe per 24h (anche le partite ben coperte
    # di squadre più note), senza nessuna indicazione visibile del motivo - nemmeno /diagnostica lo
    # segnala, perché filtra anch'essa tramite campionato_valido().
    if nome not in COMPETIZIONI_COPERTURA_VARIABILE and lega_esclusa_per_mancanza_statistiche(league_country, league_name):
        return False
    # Esclusione definitiva: la lega ha attraversato SOGLIA_GIORNATE_SENZA_STATISTICHE giornate di
    # campionato senza pubblicare un solo tiro. Vale la stessa deroga per le competizioni
    # internazionali a match esatto spiegata qui sopra, e per lo stesso motivo: lì la copertura
    # cambia partita per partita a seconda delle federazioni in campo, non è una proprietà della
    # competizione. Per tornare a seguire una lega tolta di qui basta cancellare la sua voce da
    # giornate_senza_statistiche.json (o il file intero).
    if nome not in COMPETIZIONI_COPERTURA_VARIABILE and lega_esclusa_definitivamente(league_country, league_name):
        return False
    if SOLO_LEGHE_CON_STATISTICHE:
        # Solo whitelist statica curata: fino a poco fa si passava anche qualunque campionato
        # marcato "statistiche coperte" a livello di stagione dalla cache dinamica dell'API
        # (aggiorna_leghe_attive), come rete di sicurezza per i campionati core prima che
        # vengano giocate partite con statistiche reali. Tolta su richiesta: quel flag e' un
        # dettaglio tecnico dell'API, non una selezione per rilevanza, e lasciava passare
        # campionati minori mai scelti di proposito (es. terze serie russe/georgiane) - partite
        # ininfluenti per il trading che pero' pesano sulla stessa quota/rate-limit API delle
        # partite vere, con anche il rischio di far fallire le chiamate statistiche su quelle.
        if not _lega_in_whitelist_statica(nome, league_country):
            return False
    return True


def formatta_lega(nome, paese):
    """Nome campionato con il paese tra parentesi (es. 'Serie A (Italy)'), per capire a colpo
    d'occhio di che campionato/nazione si tratta nelle notifiche senza doverlo cercare."""
    if paese and paese.lower() not in ("world", "n/a", ""):
        return f"{nome} ({paese})"
    return nome


# =============================================================================
# CHIAMATE API-FOOTBALL: helper condiviso con diagnostica e notifiche throttled
# =============================================================================
ULTIMO_ERRORE_API = {"tipo": None, "timestamp": 0}
INTERVALLO_NOTIFICA_ERRORE_API = 1800  # 30 minuti: non ripetere la stessa notifica più spesso

# Raffreddamento dopo un rate-limit: il limite di API-Football è "richieste al minuto", quindi
# insistere subito peggiora solo le cose (altre chiamate respinte, magari da più punti del bot
# nello stesso momento: ciclo principale, comandi manuali, refresh quote in background). Un solo
# rate-limit rilevato blocca TUTTE le chiamate API-Football per un minuto abbondante, poi si
# riprova - molto più semplice ed efficace che stimare quante richieste restano.
RATE_LIMIT_COOLDOWN_SECONDI = 65
PROSSIMA_CHIAMATA_API_CONSENTITA = 0

# =============================================================================
# LIMITATORE GLOBALE PROATTIVO (finestra scorrevole): il raffreddamento sopra è REATTIVO, scatta
# SOLO dopo che un rate-limit è già stato rifiutato dall'API. Non basta più con tanti campionati
# attivi insieme: il time.sleep(1) tra le due chiamate di ogni singola partita (vedi
# processa_partita) è un ritmo LOCALE a quella funzione, ma non tiene conto di cosa fanno
# CONTEMPORANEAMENTE gli altri thread del bot (ciclo principale, comandi Telegram manuali, refresh
# quote in background) - ognuno rispetta il proprio ritmo locale, ma nessuno guardava il totale
# reale di chiamate/minuto su TUTTO il bot insieme. Con una trentina di partite live in
# contemporanea (sabato con più campionati minori tutti in corso) la somma tra i thread può
# comunque superare le 100/minuto pur restando ognuno "educato" per conto proprio - infatti
# succede anche con 0 partite live nel ciclo, segno che il picco arriva da altrove (refresh quote,
# comandi manuali, cambio di finestra col piano giornata).
# Questo limitatore mette in coda (aspetta, non salta) la chiamata se negli ultimi 60s il bot ne ha
# già fatte LIMITE_CHIAMATE_AL_MINUTO_SICUREZZA - un margine di sicurezza sotto le 100/minuto reali
# dell'abbonamento, per lasciare spazio a chiamate concorrenti di altri thread nello stesso istante.
# Il margine è stato abbassato da 90 a 70 dopo aver verificato sui log Render che con 90 il bot
# toccava comunque "Too many requests" dall'API: soprattutto durante i redeploy (Render tiene per
# qualche secondo DUE istanze vive in parallelo durante il rolling restart, ognuna col proprio
# limitatore indipendente in memoria - la somma delle due può superare il limite reale anche se
# ciascuna resta sotto 90), ma anche a bot fermo su una sola istanza nelle ore di punta.
LIMITE_CHIAMATE_AL_MINUTO_SICUREZZA = 70
_LOCK_RATE_LIMIT_GLOBALE = threading.Lock()
_TIMESTAMP_CHIAMATE_RECENTI = collections.deque()


def _attendi_slot_rate_limit_globale():
    while True:
        with _LOCK_RATE_LIMIT_GLOBALE:
            ora = time.time()
            while _TIMESTAMP_CHIAMATE_RECENTI and ora - _TIMESTAMP_CHIAMATE_RECENTI[0] >= 60:
                _TIMESTAMP_CHIAMATE_RECENTI.popleft()
            if len(_TIMESTAMP_CHIAMATE_RECENTI) < LIMITE_CHIAMATE_AL_MINUTO_SICUREZZA:
                _TIMESTAMP_CHIAMATE_RECENTI.append(ora)
                return
            attesa = 60 - (ora - _TIMESTAMP_CHIAMATE_RECENTI[0]) + 0.05
        time.sleep(max(attesa, 0.05))

# =============================================================================
# CONTATORE CHIAMATE API-FOOTBALL: quante richieste il bot fa davvero, per farsi un'idea concreta
# della quota usata al giorno e valutare se il piano attivo (limite giornaliero/al minuto) è
# adeguato - vedi /apiusage. Persistito su disco (stesso pattern del resto dello stato) per poter
# calcolare una media sugli ultimi giorni, non solo sulla giornata in corso che si azzera a
# mezzanotte. Tenute solo le ultime CHIAMATE_API_GIORNI_STORICO giornate, altrimenti crescerebbe
# per sempre.
# =============================================================================
CHIAMATE_API_FILE = data_path("chiamate_api_giornaliere.json")
CHIAMATE_API_GIORNI_STORICO = 30
# Quota residua vista nell'header dell'ultima risposta API (x-ratelimit-requests-*): un dato più
# autorevole del nostro conteggio per "quanto mi resta OGGI", perché viene da API-Football stessa.
ULTIMA_QUOTA_API = {"limite": None, "residuo": None, "aggiornata": 0}


def carica_chiamate_api():
    if os.path.exists(CHIAMATE_API_FILE):
        try:
            with open(CHIAMATE_API_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Errore lettura {CHIAMATE_API_FILE}: {e}", flush=True)
    return {}


def salva_chiamate_api(dati):
    salva_json_atomico(CHIAMATE_API_FILE, dati)


CHIAMATE_API_PER_GIORNO = carica_chiamate_api()
_LOCK_CHIAMATE_API = threading.Lock()


def registra_chiamata_api():
    """Incrementa il contatore per la data odierna (fuso Italia, coerente col resto del bot) e
    tiene solo le ultime CHIAMATE_API_GIORNI_STORICO giornate. Va chiamata una volta per ogni
    chiamata di rete davvero effettuata verso API-Football (non per quelle saltate dal
    raffreddamento rate-limit, che non consumano quota). Il lock serve perché più thread (loop
    live, worker quote iniziali, comandi Telegram) chiamano questa funzione in parallelo:
    senza lock l'incremento letto-modificato-scritto sul dict condiviso può perdere aggiornamenti."""
    with _LOCK_CHIAMATE_API:
        oggi = datetime.datetime.now(ZoneInfo("Europe/Rome")).strftime("%Y-%m-%d")
        CHIAMATE_API_PER_GIORNO[oggi] = CHIAMATE_API_PER_GIORNO.get(oggi, 0) + 1
        if len(CHIAMATE_API_PER_GIORNO) > CHIAMATE_API_GIORNI_STORICO:
            for vecchia in sorted(CHIAMATE_API_PER_GIORNO)[:-CHIAMATE_API_GIORNI_STORICO]:
                del CHIAMATE_API_PER_GIORNO[vecchia]
        salva_chiamate_api(CHIAMATE_API_PER_GIORNO)


def _e_errore_rate_limit(errori):
    """True se 'errori' (il campo "errors" della risposta API-Football, dict o lista a seconda
    dell'endpoint) segnala un rate-limit - non un errore applicativo qualunque."""
    testo = str(errori).lower()
    return "ratelimit" in testo.replace(" ", "") or "too many requests" in testo


def _log_quota_headers(response):
    """Logga la quota residua che l'API restituisce già negli header di ogni risposta, e la tiene
    anche in ULTIMA_QUOTA_API (usata da /apiusage) - senza bisogno di una chiamata dedicata per
    controllarla."""
    limite = response.headers.get("x-ratelimit-requests-limit")
    residuo = response.headers.get("x-ratelimit-requests-remaining")
    if limite or residuo:
        log(f"    [quota API-Football] {residuo}/{limite} richieste rimaste oggi")
        ULTIMA_QUOTA_API["limite"] = limite
        ULTIMA_QUOTA_API["residuo"] = residuo
        ULTIMA_QUOTA_API["aggiornata"] = time.time()


def _classifica_errore_http(status_code):
    if status_code == 429:
        return "rate_limit", "limite di richieste (al minuto o giornaliero) superato"
    if status_code in (401, 403):
        return "auth", "chiave API non valida o piano non abilitato per questa risorsa"
    return f"http_{status_code}", f"HTTP {status_code}"


def get_api_football(url, params, timeout, contesto):
    """GET verso API-Football con diagnostica sempre loggata (status, header di quota residua,
    corpo dell'errore troncato), usando solo i dati già presenti nella risposta ricevuta: nessuna
    chiamata aggiuntiva viene fatta per il debug. Ritorna (json_o_None, tipo_errore_o_None,
    dettaglio_errore_o_None).

    Rispetta il raffreddamento globale dopo un rate-limit (vedi PROSSIMA_CHIAMATA_API_CONSENTITA):
    se ancora attivo, salta del tutto la chiamata di rete invece di rischiare di allungare il
    rate-limit stesso - il limite è "al minuto", quindi ogni chiamata in più mentre è già superato
    non fa che ritardare quando tornerà a funzionare.

    Rispetta ANCHE il limitatore proattivo (_attendi_slot_rate_limit_globale): a differenza del
    raffreddamento sopra, questo non salta la chiamata ma la mette in coda - conta le chiamate di
    TUTTI i thread del bot insieme, non solo quelle di chi chiama in questo momento, per evitare di
    arrivare al rate-limit prima ancora che scatti la protezione reattiva."""
    global PROSSIMA_CHIAMATA_API_CONSENTITA
    if not API_FOOTBALL_KEY:
        return None, "config", "API_FOOTBALL_KEY mancante"
    now = time.time()
    if now < PROSSIMA_CHIAMATA_API_CONSENTITA:
        attesa = int(PROSSIMA_CHIAMATA_API_CONSENTITA - now)
        log(f"[{contesto}] Rate-limit ancora in raffreddamento, chiamata saltata (riprova tra {attesa}s)")
        return None, "rate_limit", "raffreddamento dopo un rate-limit recente, chiamata saltata"
    _attendi_slot_rate_limit_globale()
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    # Contata qui, non dopo: la chiamata sta per partire davvero (passato il controllo del
    # raffreddamento sopra), quindi consuma quota a prescindere dall'esito - successo, errore
    # applicativo o addirittura un'eccezione di rete (la richiesta può comunque essere arrivata al
    # server anche se la risposta non torna indietro in tempo).
    registra_chiamata_api()
    try:
        response = requests.get(url, headers=headers, params=params, timeout=timeout)
    except Exception as e:
        log(f"[{contesto}] Eccezione di rete: {e}")
        return None, "rete", f"eccezione di rete ({e})"

    _log_quota_headers(response)

    if response.status_code != 200:
        tipo, motivo = _classifica_errore_http(response.status_code)
        log(f"[{contesto}] {motivo} - corpo: {response.text[:500]}")
        if tipo == "rate_limit":
            PROSSIMA_CHIAMATA_API_CONSENTITA = time.time() + RATE_LIMIT_COOLDOWN_SECONDI
            log(f"[{contesto}] Raffreddamento API-Football attivato per {RATE_LIMIT_COOLDOWN_SECONDI}s")
        return None, tipo, motivo

    data = response.json()
    errori = data.get("errors")
    if errori:
        log(f"[{contesto}] Errore applicativo API: {errori}")
        if _e_errore_rate_limit(errori):
            PROSSIMA_CHIAMATA_API_CONSENTITA = time.time() + RATE_LIMIT_COOLDOWN_SECONDI
            log(f"[{contesto}] Raffreddamento API-Football attivato per {RATE_LIMIT_COOLDOWN_SECONDI}s")
            return None, "rate_limit", str(errori)
        return None, "api_errors", str(errori)

    return data, None, None


def notifica_errore_api_throttled(tipo, dettaglio, contesto):
    """Manda un messaggio Telegram sull'errore API solo se lo stesso tipo di errore non è già
    stato notificato negli ultimi INTERVALLO_NOTIFICA_ERRORE_API secondi, per non spammare la
    chat ad ogni ciclo mentre l'errore persiste (es. quota esaurita per ore)."""
    global ULTIMO_ERRORE_API
    now = time.time()
    if ULTIMO_ERRORE_API["tipo"] == tipo and (now - ULTIMO_ERRORE_API["timestamp"]) < INTERVALLO_NOTIFICA_ERRORE_API:
        log(f"[{contesto}] Errore '{tipo}' ripetuto, notifica Telegram soppressa (ancora in cooldown)")
        return
    invia_messaggio_telegram(f"Errore API ({contesto})\n{dettaglio}")
    ULTIMO_ERRORE_API = {"tipo": tipo, "timestamp": now}


ULTIMO_ERRORE_GET_PARTITE_LIVE = 0


def get_partite_live():
    global ULTIMO_ERRORE_GET_PARTITE_LIVE
    if not API_FOOTBALL_KEY:
        log("API_FOOTBALL_KEY mancante, skip get_partite_live")
        return []
    url = "https://v3.football.api-sports.io/fixtures"
    data, tipo_errore, dettaglio = get_api_football(url, {"live": "all"}, timeout=15, contesto="get_partite_live")
    if data is None:
        if tipo_errore and tipo_errore != "config":
            notifica_errore_api_throttled(tipo_errore, dettaglio, "get_partite_live")
            ULTIMO_ERRORE_GET_PARTITE_LIVE = time.time()
        return []
    return data.get("response", [])


# =============================================================================
# PIANO GIORNATA: snapshot giornaliero (1 chiamata) delle partite whitelist di oggi, usato per
# sapere in anticipo in quali fasce orarie vale la pena interrogare live=all a ritmo sostenuto.
# =============================================================================
STATUS_NON_LIVE_FUTURI = {"PST", "CANC", "ABD", "AWD", "WO"}  # rinviata/annullata/a tavolino: mai live

# Stati in cui la partita e' davvero finita. AWD/WO restano fuori di proposito (sono sopra, fra i
# "mai live"): un risultato assegnato a tavolino non e' una partita giocata, e non e' un esito da
# cui lo shadow-log possa imparare qualcosa.
STATI_PARTITA_CONCLUSA = {"FT", "AET", "PEN"}

# Tempi regolamentari (90') finiti in parità, partita di coppa proseguita a supplementari/rigori:
# BT = pausa tra 2°T e supplementari (o tra i due tempi supplementari), ET = supplementari in
# corso, P = rigori in corso. Su richiesta esplicita: da qui in poi nessuna notifica per quella
# partita (vedi processa_partita) - una partita di campionato non passa mai da questi stati,
# finisce direttamente in FT, quindi in pratica questo riguarda solo le coppe.
STATUS_OLTRE_TEMPI_REGOLAMENTARI = {"BT", "ET", "P"}


def costruisci_finestre_attive(partite):
    """Unisce gli intervalli [kickoff, kickoff+durata_stimata] delle partite whitelist in finestre
    orarie senza sovrapposizioni: fuori da queste finestre nessuna partita whitelist dovrebbe
    essere in corso."""
    durata_sec = DURATA_STIMATA_PARTITA_MINUTI * 60
    intervalli = sorted(
        (p["kickoff_ts"], p["kickoff_ts"] + durata_sec) for p in partite if p.get("kickoff_ts")
    )
    finestre = []
    for inizio, fine in intervalli:
        if finestre and inizio <= finestre[-1][1]:
            finestre[-1][1] = max(finestre[-1][1], fine)
        else:
            finestre.append([inizio, fine])
    return finestre


def costruisci_piano_giornata(data_str):
    """Scarica con UNA chiamata /fixtures?date=YYYY-MM-DD tutte le partite del giorno (tutte le
    leghe del mondo) e filtra in locale con campionato_valido() (nessuna chiamata aggiuntiva) per
    ottenere l'elenco delle partite whitelist di oggi e le finestre orarie in cui sono attese live.
    Ritorna None in caso di errore API, cosi il chiamante puo' riprovare al ciclo successivo senza
    sovrascrivere un piano precedente ancora valido."""
    url = "https://v3.football.api-sports.io/fixtures"
    data, tipo_errore, dettaglio = get_api_football(url, {"date": data_str}, timeout=25, contesto="costruisci_piano_giornata")
    if data is None:
        if tipo_errore and tipo_errore != "config":
            notifica_errore_api_throttled(tipo_errore, dettaglio, "costruisci_piano_giornata")
        return None

    partite = []
    for item in data.get("response", []):
        fixture_info = item.get("fixture", {})
        status_short = fixture_info.get("status", {}).get("short", "")
        if status_short in STATUS_NON_LIVE_FUTURI:
            continue
        league = item.get("league", {})
        if not campionato_valido(league.get("name", ""), league.get("type", ""), league.get("country", "")):
            continue
        # Stesso filtro del ciclo principale: una partita giovanile non deve nemmeno entrare nel
        # piano della giornata, altrimenti rientrerebbe dalla finestra oraria che il piano genera.
        if partita_tra_giovanili(item):
            continue
        kickoff_ts = fixture_info.get("timestamp")
        if not kickoff_ts:
            continue
        partite.append({
            "fixture_id": fixture_info.get("id"),
            "lega": league.get("name", ""),
            "kickoff_ts": kickoff_ts,
            "home": item.get("teams", {}).get("home", {}).get("name", "?"),
            "away": item.get("teams", {}).get("away", {}).get("name", "?"),
        })

    finestre = costruisci_finestre_attive(partite)
    log(f"Piano giornata {data_str}: {len(partite)} partite whitelist, {len(finestre)} finestre attive")
    return {
        "data": data_str,
        "generato_alle": time.time(),
        "partite": partite,
        "finestre_attive": finestre,
    }


def dentro_finestra_attiva(piano, now_ts=None):
    """True se now_ts (default: adesso) cade dentro una finestra attiva del piano, con un margine
    di anticipo (MARGINE_PRE_KICKOFF_MINUTI) per non perdere l'inizio di una partita in leggero
    ritardo sull'orario previsto."""
    now_ts = now_ts if now_ts is not None else time.time()
    margine_sec = MARGINE_PRE_KICKOFF_MINUTI * 60
    for inizio, fine in piano.get("finestre_attive", []):
        if inizio - margine_sec <= now_ts <= fine:
            return True
    return False


def dentro_orario_attivo(now_it=None):
    """True se l'ora locale italiana corrente cade nella fascia ORARIO_ATTIVO_* (default
    12:00-23:30). Nessuna eccezione per partite già in corso a cavallo del limite. NOTA: fuori
    fascia il bot NON si ferma più - il monitoraggio (statistiche, quote, shadow-log) resta
    attivo 24/7, solo l'invio delle notifiche Telegram viene saltato (vedi notifiche_attive nel
    loop principale e in processa_partita)."""
    now_it = now_it if now_it is not None else datetime.datetime.now(ZoneInfo("Europe/Rome"))
    inizio = now_it.replace(hour=ORARIO_ATTIVO_INIZIO_ORA, minute=ORARIO_ATTIVO_INIZIO_MINUTO, second=0, microsecond=0)
    fine = now_it.replace(hour=ORARIO_ATTIVO_FINE_ORA, minute=ORARIO_ATTIVO_FINE_MINUTO, second=0, microsecond=0)
    return inizio <= now_it < fine


def aggiorna_piano_giornata_se_serve():
    """Chiamata ad ogni ciclo del loop principale (nessun costo se non serve rigenerare). Rigenera
    il piano una volta al giorno, alla prima occasione utile da quando è scattata l'ora
    configurata (ora locale Italia) in poi - non solo durante quell'ora esatta: se il bot è in
    pausa (manuale o per la fascia oraria notturna) proprio durante l'ora di generazione, la
    finestra non va "persa" fino al giorno dopo, ma recuperata al primo ciclo utile dopo la
    ripresa, qualunque sia l'ora nel frattempo. Prima dell'ora configurata non genera comunque
    nulla: un riavvio mattutino (il filesystem di Render non e' persistente tra un riavvio e
    l'altro, quindi il piano risulta sempre "mai generato" dopo un riavvio) non deve scatenare
    una chiamata /fixtures?date= prima che serva davvero. In attesa del piano il ciclo principale
    tratta comunque ogni momento come finestra attiva (fail-safe), quindi non si perde nessuna
    partita nel frattempo - solo l'ottimizzazione del ritmo dei cicli resta meno efficiente."""
    global PIANO_GIORNATA
    now_it = datetime.datetime.now(ZoneInfo("Europe/Rome"))
    oggi_str = now_it.strftime("%Y-%m-%d")

    if PIANO_GIORNATA.get("data") == oggi_str:
        return
    if now_it.hour < ORA_GENERAZIONE_PIANO_GIORNATA:
        return

    log(f"Generazione piano partite per {oggi_str} (ore {now_it.strftime('%H:%M')} orario italiano)...")
    nuovo_piano = costruisci_piano_giornata(oggi_str)
    if nuovo_piano is not None:
        PIANO_GIORNATA = nuovo_piano
        salva_piano_giornata(PIANO_GIORNATA)
        avvia_recupero_quote_iniziali(PIANO_GIORNATA)
    else:
        log("Generazione piano giornata fallita (errore API), riprovo al prossimo ciclo")


# =============================================================================
# QUOTE 1X2 PRE-PARTITA: valore testuale (non un grafico) delle quote di apertura, per farsi
# un'idea di come il mercato valuta la partita prima che inizi. Recuperate in due passaggi
# separati dal loop live (mai dentro il ciclo di monitoraggio a 60-180s):
#   1) al momento del piano giornata (una volta al giorno) - un primo tentativo, in un thread a
#      parte per non bloccare la generazione del piano stesso;
#   2) un refresh mirato quando manca meno di ODDS_REFRESH_MINUTI_PRIMA_KICKOFF al calcio
#      d'inizio, così la quota mostrata è quella più vicina al closing (il riferimento più
#      indicativo per il "valore") invece di quella di ore/giorni prima.
# Bookmaker e mercato sono fissi (un solo bookmaker sempre uguale, altrimenti il numero mostrato
# non è confrontabile da una partita all'altra) ma risolti per NOME sui riferimenti reali
# dell'API (/odds/bookmakers, /odds/bets) invece di un ID hardcoded indovinato.
# =============================================================================
RIFERIMENTI_ODDS_TTL = 86400  # 24 ore: bookmaker/mercati non cambiano ID durante la giornata
RIFERIMENTI_ODDS_RETRY_FALLIMENTO = 900  # 15 minuti prima di ritentare dopo un fallimento (vedi sotto)
_RIFERIMENTI_ODDS_CACHE = {"bookmaker_id": None, "bet_id": None, "timestamp": 0}


def _trova_id_per_nome(elementi, nome_cercato):
    nome_cercato = nome_cercato.strip().lower()
    for el in elementi:
        if (el.get("name") or "").strip().lower() == nome_cercato:
            return el.get("id")
    return None


def risolvi_riferimenti_odds():
    """Risolve l'ID del bookmaker e del mercato configurati per nome, interrogando
    /odds/bookmakers e /odds/bets. Cache 24h in caso di successo; in caso di fallimento (nome non
    trovato, errore di rete, rate-limit) mette comunque in "raffreddamento" il tentativo per
    RIFERIMENTI_ODDS_RETRY_FALLIMENTO secondi invece di ritentare subito - altrimenti, chiamata da
    dentro il ciclo di recupero quote (una volta per ogni partita del piano), un singolo fallimento
    scatenerebbe 2 chiamate extra ad ogni partita del batch invece di una sola in tutto il giorno,
    rischiando di far scattare proprio il rate-limit per-minuto che si vuole evitare."""
    now = time.time()
    if _RIFERIMENTI_ODDS_CACHE["bookmaker_id"] and _RIFERIMENTI_ODDS_CACHE["bet_id"] and \
            (now - _RIFERIMENTI_ODDS_CACHE["timestamp"]) < RIFERIMENTI_ODDS_TTL:
        return _RIFERIMENTI_ODDS_CACHE["bookmaker_id"], _RIFERIMENTI_ODDS_CACHE["bet_id"]
    if not _RIFERIMENTI_ODDS_CACHE["bookmaker_id"] and \
            (now - _RIFERIMENTI_ODDS_CACHE["timestamp"]) < RIFERIMENTI_ODDS_RETRY_FALLIMENTO:
        return None, None

    data_bm, _, _ = get_api_football(
        "https://v3.football.api-sports.io/odds/bookmakers", {}, timeout=15, contesto="risolvi_riferimenti_odds(bookmakers)")
    data_bet, _, _ = get_api_football(
        "https://v3.football.api-sports.io/odds/bets", {}, timeout=15, contesto="risolvi_riferimenti_odds(bets)")

    bookmaker_id = _trova_id_per_nome(data_bm.get("response", []), ODDS_BOOKMAKER_NOME) if data_bm else None
    bet_id = _trova_id_per_nome(data_bet.get("response", []), ODDS_BET_NOME) if data_bet else None

    if bookmaker_id and bet_id:
        _RIFERIMENTI_ODDS_CACHE["bookmaker_id"] = bookmaker_id
        _RIFERIMENTI_ODDS_CACHE["bet_id"] = bet_id
        _RIFERIMENTI_ODDS_CACHE["timestamp"] = now
        log(f"Riferimenti quote risolti: bookmaker '{ODDS_BOOKMAKER_NOME}'={bookmaker_id}, mercato '{ODDS_BET_NOME}'={bet_id}")
    else:
        _RIFERIMENTI_ODDS_CACHE["timestamp"] = now
        log(f"Riferimenti quote non risolti (bookmaker={bookmaker_id}, bet={bet_id}) - "
            f"ritento tra {RIFERIMENTI_ODDS_RETRY_FALLIMENTO // 60} min, verificare i nomi in config.json")

    return _RIFERIMENTI_ODDS_CACHE["bookmaker_id"], _RIFERIMENTI_ODDS_CACHE["bet_id"]


def recupera_quote_1x2(fixture_id):
    """Quote 1X2 pre-match per una singola fixture dal bookmaker/mercato configurati. Ritorna un
    dict {'casa','pareggio','ospite','bookmaker'} se trovate, altrimenti None (nessuna quota
    pubblicata per ora, oppure errore/lega non coperta): non distingue i due casi qui, ci pensa
    il chiamante (Passo A le lascia "da ritentare", Passo B le marca definitive)."""
    bookmaker_id, bet_id = risolvi_riferimenti_odds()
    if not bookmaker_id or not bet_id:
        return None

    data, _, _ = get_api_football(
        "https://v3.football.api-sports.io/odds",
        {"fixture": fixture_id, "bookmaker": bookmaker_id, "bet": bet_id},
        timeout=15, contesto=f"recupera_quote_1x2({fixture_id})")
    if not data:
        return None

    response = data.get("response") or []
    if not response:
        return None
    bookmakers = response[0].get("bookmakers") or []
    if not bookmakers:
        return None
    bets = bookmakers[0].get("bets") or []
    if not bets:
        return None
    valori = bets[0].get("values") or []

    quote = {}
    etichette = {"home": "casa", "draw": "pareggio", "away": "ospite"}
    for v in valori:
        chiave = etichette.get((v.get("value") or "").strip().lower())
        if not chiave:
            continue
        try:
            quote[chiave] = float(v.get("odd"))
        except (TypeError, ValueError):
            continue

    if not all(k in quote for k in ("casa", "pareggio", "ospite")):
        return None
    quote["bookmaker"] = bookmakers[0].get("name") or ODDS_BOOKMAKER_NOME
    return quote


def _recupero_quote_iniziali_worker(piano):
    """Thread separato (non blocca la generazione del piano né il loop live): primo tentativo di
    quota per ogni partita del giorno, con uno sleep tra una chiamata e l'altra per restare ben
    sotto il rate-limit per-minuto anche con piani da decine di partite. Stesso ritmo (1s) usato
    dal loop live tra una partita e l'altra: questo thread gira in parallelo a quel loop, quindi
    le loro chiamate si sommano nella stessa finestra di tempo - un ritmo più aggressivo qui
    rischierebbe di far scattare il rate-limit anche quando il loop live da solo resterebbe sotto
    soglia."""
    trovate = 0
    partite = piano.get("partite", [])
    for partita in partite:
        quote = recupera_quote_1x2(partita["fixture_id"])
        if quote:
            partita["quote_1x2"] = quote
            trovate += 1
        time.sleep(1)
    salva_piano_giornata(piano)
    log(f"Quote 1X2 iniziali: {trovate}/{len(partite)} partite con quota trovata al primo tentativo")


def avvia_recupero_quote_iniziali(piano):
    threading.Thread(target=_recupero_quote_iniziali_worker, args=(piano,), daemon=True).start()


def aggiorna_quote_prepartita_imminenti():
    """Chiamata ad ogni ciclo del loop principale (nessuna chiamata API se nessuna partita è
    vicina al kickoff): quando manca meno di ODDS_REFRESH_MINUTI_PRIMA_KICKOFF minuti, rifà la
    quota una seconda volta (più vicina al closing) e la marca definitiva - se anche qui non
    c'è nulla, smette di ritentare e lo mostra esplicitamente come "non pubblicate" invece di
    lasciare un campo vuoto senza spiegazione.

    time.sleep(1) tra una chiamata e l'altra quando più partite entrano nella finestra nello
    stesso ciclo (stesso motivo/ritmo di _recupero_quote_iniziali_worker, che questa funzione
    non aveva mai avuto: prima le chiamate partivano una via l'altra senza pausa, e con più
    partite vicine al kickoff nello stesso ciclo bastava a far scattare il rate-limit "al
    minuto" - visto nei log con tre chiamate nello stesso secondo, due respinte subito)."""
    now_ts = time.time()
    finestra_sec = ODDS_REFRESH_MINUTI_PRIMA_KICKOFF * 60
    modificato = False
    prima_chiamata = True
    for partita in PIANO_GIORNATA.get("partite", []):
        kickoff_ts = partita.get("kickoff_ts")
        if not kickoff_ts or partita.get("quote_refresh_fatto"):
            continue
        if not (0 <= kickoff_ts - now_ts <= finestra_sec):
            continue
        if not prima_chiamata:
            time.sleep(1)
        prima_chiamata = False
        quote = recupera_quote_1x2(partita["fixture_id"])
        partita["quote_1x2"] = quote if quote else False
        partita["quote_refresh_fatto"] = True
        modificato = True
        log(f"Quote 1X2 refresh pre-kickoff: {partita.get('home', '?')} vs {partita.get('away', '?')} "
            f"-> {'trovate' if quote else 'non disponibili'}")
    if modificato:
        salva_piano_giornata(PIANO_GIORNATA)


def quote_1x2_per_fixture(fixture_id):
    for partita in PIANO_GIORNATA.get("partite", []):
        if partita.get("fixture_id") == fixture_id:
            return partita.get("quote_1x2")
    return None


def calcola_probabilita_no_vig(quote):
    """Toglie il margine del bookmaker (l'overround: 1/quota_casa + 1/quota_pareggio +
    1/quota_ospite somma sempre più di 100%) dalle 3 quote, normalizzando alla probabilità "vera"
    secondo il mercato. Pura aritmetica sui dati già scaricati, nessuna stima: a differenza di
    qualunque punteggio "momentum", questo numero è sempre corretto per definizione."""
    if not quote:
        return None
    try:
        implicite = {
            "casa": 1 / quote["casa"],
            "pareggio": 1 / quote["pareggio"],
            "ospite": 1 / quote["ospite"],
        }
    except (KeyError, ZeroDivisionError, TypeError):
        return None
    overround = sum(implicite.values())
    if overround <= 0:
        return None
    return {chiave: valore / overround for chiave, valore in implicite.items()}


def testo_quote_1x2(quote):
    """Solo la quota grezza nel messaggio: la probabilità no-vig si continua a calcolare e
    registrare nello shadow-log (vedi registra_shadow_log_valore_snapshot) per la validazione
    futura, ma non si mostra ancora in chat finché non è stata verificata su dati reali (Fase 2)."""
    if quote is False:
        return "\nQuote 1X2 iniziali: non pubblicate\n"
    if not quote:
        return ""
    return (f"\nQuote 1X2 iniziali ({quote['bookmaker']}): "
            f"1 {quote['casa']:.2f} - X {quote['pareggio']:.2f} - 2 {quote['ospite']:.2f}\n")


def recupera_andata_precedente(fixture_id, team_home_id, team_away_id, league_id, ts_ritorno):
    """Cerca la partita di ANDATA di un turno di qualificazione UEFA andata-ritorno, tramite
    l'endpoint head-to-head: l'API non lega esplicitamente i due fixture_id di andata e ritorno
    con un riferimento diretto, quindi va cercata tra i precedenti recenti delle stesse due
    squadre. Filtra per stessa competizione (league_id) e giocata negli ultimi 20 giorni prima del
    ritorno, per non prendere per sbaglio un vecchio precedente in un'altra stagione/torneo.
    Va chiamata UNA sola volta per fixture_id, MA SOLO SE LA CHIAMATA RIESCE (il chiamante mette
    in cache il risultato): ritorna (chiamata_riuscita, andata_info) - chiamata_riuscita è False
    se la chiamata API è fallita (rate-limit/timeout/rete), cosa ben diversa da "chiamata riuscita
    ma nessuna andata trovata". Confondere le due cose (com'era prima) faceva sì che una singola
    chiamata sfortunata - probabile proprio nel momento in cui iniziano insieme tante partite di
    ritorno - disattivasse per sempre la ricerca per quella partita, facendola risultare come se
    non avesse un'andata invece di riprovare al ciclo successivo."""
    if not API_FOOTBALL_KEY:
        return False, None
    url = "https://v3.football.api-sports.io/fixtures/headtohead"
    data, _, _ = get_api_football(
        url, {"h2h": f"{team_home_id}-{team_away_id}", "last": 5}, timeout=10,
        contesto=f"recupera_andata_precedente({fixture_id})")
    if data is None:
        return False, None
    for f in data.get("response", []):
        if f.get("fixture", {}).get("id") == fixture_id:
            continue
        if (f.get("league", {}) or {}).get("id") != league_id:
            continue
        status_short = ((f.get("fixture", {}) or {}).get("status", {}) or {}).get("short")
        if status_short not in ("FT", "AET", "PEN"):
            continue
        ts_partita = (f.get("fixture", {}) or {}).get("timestamp")
        if not ts_partita or not (0 < ts_ritorno - ts_partita <= 20 * 24 * 3600):
            continue
        return True, {
            "home": f["teams"]["home"]["name"],
            "away": f["teams"]["away"]["name"],
            "score_home": f["goals"]["home"],
            "score_away": f["goals"]["away"],
        }
    return True, None


# Statistiche ed eventi non supportano un fetch "bulk" su più partite (a differenza di /fixtures,
# solo /fixtures/statistics e /fixtures/events restano per-singola-partita), quindi non si può
# ridurre il numero di chiamate raggruppandole. Quello che invece si può evitare è la chiamata
# DUPLICATA quando la stessa partita viene richiesta più volte a distanza di pochi secondi: il
# loop principale la interroga già ogni 60-180s, ma /live, /intensita e /status (comandi manuali)
# rifacevano la stessa identica chiamata da capo ogni volta che l'utente li lanciava, anche a
# pochi secondi da un ciclo del loop o da un altro comando. Una
# cache condivisa (per fixture) con TTL breve rende gratuite queste richieste duplicate senza
# cambiare la frequenza/freschezza dei dati usati dal loop principale (i suoi cicli sono comunque
# distanziati almeno 60s, oltre il TTL della cache, quindi per lui è sempre un cache-miss).
CACHE_TTL_STATS_EVENTI = 50  # secondi, appena sotto il ciclo più stretto (60s dei preferiti)
_CACHE_STATISTICHE_PARTITA = {}  # fixture_id -> (timestamp, risposta)
_CACHE_EVENTI_PARTITA = {}  # fixture_id -> (timestamp, risposta)


def get_statistiche_partita(fixture_id, debug=False):
    now = time.time()
    voce_cache = _CACHE_STATISTICHE_PARTITA.get(fixture_id)
    if voce_cache and (now - voce_cache[0]) < CACHE_TTL_STATS_EVENTI:
        return voce_cache[1]
    if not API_FOOTBALL_KEY:
        return None
    url = "https://v3.football.api-sports.io/fixtures/statistics"
    data, tipo_errore, dettaglio = get_api_football(
        url, {"fixture": fixture_id}, timeout=15, contesto=f"get_statistiche_partita({fixture_id})")
    if debug:
        if data is not None:
            log(f"    [DEBUG stats {fixture_id}] risposta OK - {json.dumps(data)[:1500]}")
        else:
            log(f"    [DEBUG stats {fixture_id}] errore: {tipo_errore} - {dettaglio}")
    if data is None:
        # Le chiamate FALLITE non si mettono in cache: mettercele (com'era prima) rendeva cieca
        # per altri 50s anche la richiesta successiva sulla stessa partita, quindi un /diagnostica
        # o un /live lanciati subito dopo un ciclo sfortunato riportavano "statistiche assenti"
        # senza nemmeno riprovare. Restituire None (invece di []) resta il segnale di errore:
        # chi chiama distingue "chiamata fallita" da "l'API ha risposto senza dati".
        return None
    risultato = data.get("response", [])
    _CACHE_STATISTICHE_PARTITA[fixture_id] = (now, risultato)
    return risultato


def fetch_fixture_events(fixture_id):
    now = time.time()
    voce_cache = _CACHE_EVENTI_PARTITA.get(fixture_id)
    if voce_cache and (now - voce_cache[0]) < CACHE_TTL_STATS_EVENTI:
        return voce_cache[1]
    if not API_FOOTBALL_KEY:
        return []
    url = "https://v3.football.api-sports.io/fixtures/events"
    data, _, _ = get_api_football(url, {"fixture": fixture_id}, timeout=10, contesto=f"fetch_fixture_events({fixture_id})")
    risultato = [] if data is None else data.get("response", [])
    _CACHE_EVENTI_PARTITA[fixture_id] = (now, risultato)
    return risultato


def extract_goals(events):
    """Gol veri e propri dagli eventi della partita. NOTA: API-Football marca un rigore
    sbagliato/parato con type="Goal" comunque (lo distingue solo nel campo "detail" =
    "Missed Penalty") - va escluso esplicitamente, altrimenti risulta come un gol mai segnato,
    con un punteggio "Primo/Ultimo gol" che non corrisponde al risultato reale della partita."""
    goals = []
    for ev in events:
        if ev.get("type") != "Goal":
            continue
        if (ev.get("detail") or "").lower() == "missed penalty":
            continue
        goals.append({
            "minute": ev["time"]["elapsed"],
            "player": (ev.get("player") or {}).get("name") or "Sconosciuto",
            "team": ev["team"]["name"]
        })
    goals.sort(key=lambda g: g["minute"])
    return goals


def goals_coerenti_con_risultato(goals, home, away, score_home, score_away):
    """L'endpoint eventi a volte continua a includere un gol poi annullato dal VAR (o corretto
    per un altro motivo) anche quando il risultato ufficiale della partita non lo conta più -
    visto in produzione: "Primo gol... -> 1-0" nel testo con il risultato reale tornato a 0-0.
    Tiene solo tanti gol per squadra quanti ne mostra il risultato reale, scartando gli eventuali
    gol "in eccesso" più recenti (il più plausibile ad essere quello poi annullato)."""
    gol_casa = sorted([g for g in goals if g["team"] == home], key=lambda g: g["minute"])[:score_home]
    gol_ospite = sorted([g for g in goals if g["team"] == away], key=lambda g: g["minute"])[:score_away]
    return sorted(gol_casa + gol_ospite, key=lambda g: g["minute"])


def calcola_punteggio_ai_gol(goals, home, away):
    """Per ogni gol (in ordine cronologico) il risultato ESATTO subito dopo quel gol, non il
    punteggio finale - serve a mostrare "chi ha fatto il 2-1" invece di dover risalire alle
    notifiche precedenti per capirlo."""
    risultati = []
    h = a = 0
    for g in goals:
        if g["team"] == home:
            h += 1
        elif g["team"] == away:
            a += 1
        risultati.append((h, a))
    return risultati


def testo_primo_ultimo_gol(goals, home, away):
    """Riga "Primo gol"/"Ultimo gol" con squadra e risultato a quel punto. Se c'è un solo gol,
    "Ultimo gol" non compare (sarebbe ridondante col "Primo gol")."""
    if not goals:
        return ""
    punteggi = calcola_punteggio_ai_gol(goals, home, away)
    h0, a0 = punteggi[0]
    testo = f"\nPrimo gol: {goals[0]['minute']}' ({goals[0]['player']}, {goals[0]['team']}) → {h0}-{a0}\n"
    if len(goals) > 1:
        h1, a1 = punteggi[-1]
        testo += f"Ultimo gol: {goals[-1]['minute']}' ({goals[-1]['player']}, {goals[-1]['team']}) → {h1}-{a1}\n"
    return testo


def extract_cartellini_rossi(events):
    """Cartellini rossi (diretti o secondo giallo) dagli eventi della partita."""
    rossi = []
    for ev in events:
        if ev.get("type") != "Card":
            continue
        dettaglio = (ev.get("detail") or "").lower()
        if "red" not in dettaglio:
            continue
        rossi.append({
            "minute": ev["time"]["elapsed"],
            "player": (ev.get("player") or {}).get("name") or "Sconosciuto",
            "team": ev["team"]["name"],
            "dettaglio": ev.get("detail") or "Red Card",
        })
    rossi.sort(key=lambda c: c["minute"])
    return rossi


def extract_rigori(events):
    """Rigori segnati o sbagliati dagli eventi della partita (esclude quelli concessi ma non
    ancora battuti: l'API non li espone come evento a sé finché non vengono calciati)."""
    rigori = []
    for ev in events:
        dettaglio = (ev.get("detail") or "").lower()
        if dettaglio == "penalty" and ev.get("type") == "Goal":
            esito = "segnato"
        elif dettaglio == "missed penalty":
            esito = "sbagliato"
        else:
            continue
        rigori.append({
            "minute": ev["time"]["elapsed"],
            "player": (ev.get("player") or {}).get("name") or "Sconosciuto",
            "team": ev["team"]["name"],
            "esito": esito,
        })
    rigori.sort(key=lambda r: r["minute"])
    return rigori


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


# Le uniche statistiche che il bot legge davvero (vedi estrai_current_stats): la disponibilità va
# giudicata su queste, non su tutta la risposta.
STATISTICHE_USATE = ("total shots", "shots on goal", "corner kicks", "shots insidebox")


def ha_statistiche_disponibili(stats):
    """True se l'API ha restituito dati statistici reali (non solo liste vuote/nulle) per entrambe le squadre.

    Guarda SOLO le statistiche che il bot usa davvero. Prima bastava che una voce QUALUNQUE della
    risposta fosse valorizzata - anche possesso palla, falli o cartellini, che il bot non legge mai
    - per prendere per buona tutta la risposta. Ma l'API pubblica i dati generali prima di tiri e
    corner: la risposta passava il controllo, poi estrai_valore_stat() traduceva in 0 i quattro
    valori davvero mancanti (None -> 0), e in notifica finiva "Tiri totali: 0 - 0" presentato come
    dato reale, sotto l'intestazione "Statistiche ultimi 15 min", invece del "N/D" previsto per il
    dato mancante.

    Visto il 16/08 su Djurgardens-AIK (Allsvenskan): gol al 10' e "0 - 0" tiri al 12'. Le due cose
    insieme non possono stare, un gol un tiro lo richiede. Lo zero era un None travestito."""
    if not stats or len(stats) < 2:
        return False
    stats_home = stats[0].get("statistics", []) or []
    stats_away = stats[1].get("statistics", []) or []
    if not stats_home or not stats_away:
        return False
    return any(s.get("value") is not None
               for s in stats_home + stats_away
               if s.get("type", "").lower() in STATISTICHE_USATE)


def impronta_statistiche(stats):
    """Impronta dell'INTERA risposta statistiche, non solo delle quattro voci che il bot usa.

    E' il punto su cui regge tutto il rilevatore di feed bloccato. Tiri e corner possono benissimo
    restare fermi per qualche minuto in una partita vera: con il ciclo da 3 minuti succede di
    continuo, e infatti "nessun tiro cambiato" e' lo skip piu' frequente dei log. Possesso palla,
    passaggi e falli invece non stanno mai fermi - cambiano ogni manciata di secondi.

    Quindi: se e' identica anche quella parte, la risposta non descrive una partita ferma, e' la
    stessa identica risposta di prima. Cioe' un feed che non si aggiorna. Che quei campi ci siano e
    si muovano per conto loro e' gia' documentato in ha_statistiche_disponibili ("l'API pubblica i
    dati generali prima di tiri e corner"), che proprio per questo li esclude dal suo controllo.

    md5 e non hash(): l'impronta finisce in stato_partite, che viene salvato su disco e riletto
    dopo un riavvio, e hash() sulle stringhe cambia ad ogni processo."""
    if not stats or len(stats) < 2:
        return None
    parti = []
    for lato in stats[:2]:
        for voce in (lato.get("statistics") or []):
            parti.append(f"{voce.get('type')}={voce.get('value')}")
    if not parti:
        return None
    return hashlib.md5("|".join(parti).encode("utf-8")).hexdigest()


def aggiorna_feed_congelato(fixture_id, stats, minuto, registra=True):
    """Tiene il conto di da quanti minuti di gioco la risposta statistiche non cambia di una virgola.

    Ritorna (congelato, minuti_fermo). Il conto e' in minuti di PARTITA, non di orologio: cosi'
    l'intervallo non lo fa crescere (a meta' tempo il minuto non avanza) e il numero detto
    all'utente e' quello che gli serve davvero.

    registra=False per chi legge soltanto (i comandi come /status): non tocca stato_partite, cosi'
    una consultazione non sposta il conteggio del ciclo live ne' crea uno stato per una partita che
    il loop non segue nemmeno."""
    if not FEED_CONGELATO_ATTIVO or minuto is None:
        return False, 0
    impronta = impronta_statistiche(stats)
    if impronta is None:
        return False, 0
    stato = stato_partite.setdefault(fixture_id, {}) if registra else stato_partite.get(fixture_id, {})
    if stato.get("impronta_stats") != impronta:
        # Risposta nuova: il feed si e' mosso. Si riparte da qui, e un blocco successivo potra'
        # essere segnalato di nuovo.
        if registra:
            # Il buco appena chiuso finisce nei log (mai in chat): e' l'unico modo per sapere
            # quanto durano DAVVERO, invece di tarare MINUTI_FEED_CONGELATO a occhio. Il 23/08 il
            # confronto fra l'avviso delle 20:27 e un /status di quattro minuti dopo ha mostrato
            # che due partite su tre erano gia' ripartite: l'API non si blocca, pubblica a
            # raffiche - Verona-Ascoli e' saltata da 10-4 a 13-9 in un colpo solo.
            fermo_precedente = stato.get("impronta_minuto")
            if (fermo_precedente is not None
                    and minuto - fermo_precedente >= MINUTI_GAP_FEED_DA_MISURARE):
                log(f"    📈 Feed ripartito dopo {minuto - fermo_precedente}' di gioco fermi")
            stato["impronta_stats"] = impronta
            stato["impronta_minuto"] = minuto
            stato["feed_congelato_segnalato"] = False
        return False, 0
    fermo_dal_minuto = stato.get("impronta_minuto")
    if fermo_dal_minuto is None:
        if registra:
            stato["impronta_minuto"] = minuto
        return False, 0
    minuti_fermo = max(0, minuto - fermo_dal_minuto)
    return minuti_fermo >= MINUTI_FEED_CONGELATO, minuti_fermo


def testo_feed_congelato(minuti_fermo, current_stats):
    """Riga di avviso da mettere dove si mostrano statistiche che potrebbero essere ferme."""
    tiri = current_stats.get("Tiri totali", (0, 0)) if current_stats else (0, 0)
    return (f"\n\n🧊 ATTENZIONE: l'API non aggiorna queste statistiche da {minuti_fermo}' di gioco "
            f"(ferme su Tiri {tiri[0]}-{tiri[1]}). I numeri qui sopra sono probabilmente vecchi.")


def _testo_riepilogo_feed_congelati(partite):
    """Una riga per partita, le piu' ferme in cima."""
    plurale = "partita" if len(partite) == 1 else "partite"
    righe = [f"🧊 STATISTICHE FERME · {len(partite)} {plurale}",
             "L'API ha smesso di aggiornare i numeri: finché non riparte il bot non può "
             "valutarle. Se una ti interessa, controllala a mano.", ""]
    for p in sorted(partite, key=lambda x: -x["fermo_da"]):
        righe.append(f"{p['home']} vs {p['away']} · {p['lega']}")
        righe.append(f"   {p['minuto']}' | {p['score']} — ferme da {p['fermo_da']}' "
                     f"su Tiri {p['tiri']}")
    return "\n".join(righe)


def invia_riepilogo_feed_congelati(notifiche_attive=True):
    """UN messaggio per ciclo con tutte le partite congelate, invece di uno per partita.

    Il 23/08 la prima versione ne ha mandati otto in due minuti. Non erano falsi allarmi - i log
    confermano che erano ferme davvero - ma e' proprio questo il punto: se il fenomeno e' comune,
    un messaggio per partita e' una raffica, e una raffica non si legge. Raggruppare tiene la
    stessa informazione in una riga per partita.

    I preferiti restano nel LORO canale: un riepilogo unico li porterebbe nella chat principale,
    riaprendo li' il flusso che il canale dedicato serve proprio a tenere separato. Se il canale
    dedicato non e' configurato le due destinazioni coincidono, e il messaggio torna uno solo."""
    partite = list(FEED_CONGELATI_CICLO)
    FEED_CONGELATI_CICLO.clear()
    if not partite or not notifiche_attive:
        return
    per_chat = {}
    for p in partite:
        chat = TELEGRAM_CHAT_ID_PREFERITI if p["preferita"] else TELEGRAM_CHAT_ID
        per_chat.setdefault(chat, []).append(p)
    for chat, elenco in per_chat.items():
        invia_messaggio_telegram(_testo_riepilogo_feed_congelati(elenco), chat_id=chat)


def conta_autogol(events, home, away):
    """Autogol accreditati a ciascuna squadra, come (pro_casa, pro_ospite).

    API-Football attribuisce l'evento alla squadra del GIOCATORE che ha segnato, cioe' a quella
    che subisce: un autogol di un difensore di casa vale un gol per gli ospiti. Serve saperlo qui
    perche' e' l'unico modo lecito in cui una squadra puo' avere piu' gol che tiri in porta."""
    pro_casa = pro_ospite = 0
    for ev in events or []:
        if ev.get("type") != "Goal":
            continue
        if (ev.get("detail") or "").lower() != "own goal":
            continue
        squadra = ((ev.get("team") or {}).get("name") or "")
        if squadra == home:
            pro_ospite += 1
        elif squadra == away:
            pro_casa += 1
    return pro_casa, pro_ospite


def statistiche_indietro_sul_punteggio(current_stats, score_home, score_away, events, home, away):
    """True se i numeri contraddicono il risultato: piu' gol che tiri in porta.

    Un gol richiede un tiro in porta - l'unica eccezione e' l'autogol, che si conta a parte e si
    sottrae. Quando la disuguaglianza salta, le statistiche non sono "poche": sono INDIETRO.

    Serve perche' gli altri due controlli non prendono questo caso. Il gate sulle statistiche
    assenti vede dei numeri e li accetta; il rilevatore di feed bloccato aspetta che la risposta
    resti identica per minuti, e qui invece la risposta cambia - solo che arriva in ritardo.
    Questo e' un errore visibile SUBITO, da una risposta sola, perche' e' una contraddizione
    logica e non una misura di tempo.

    Il caso: Pogon Szczecin-Wisla Krakow, 23/08. Al 23' il bot annunciava "1-1" con "Tiri 1-0",
    cioe' una squadra che aveva segnato senza aver tirato; al 34' era 2-2 con "Porta 2-1". Nessun
    autogol nella partita. E' lo stesso difetto gia' visto il 16/08 su Djurgardens-AIK ("gol al
    10' e 0-0 tiri al 12'"), che allora era stato affrontato solo per i valori nulli.

    Ritorna (indietro, dettaglio) - il dettaglio e' la riga da mostrare, vuota se tutto torna."""
    if not current_stats or score_home is None or score_away is None:
        return False, ""
    porta_casa, porta_ospite = current_stats.get("Tiri in porta", (0, 0))
    autogol_casa, autogol_ospite = conta_autogol(events, home, away)
    gol_casa = max(0, score_home - autogol_casa)
    gol_ospite = max(0, score_away - autogol_ospite)
    mancanti = []
    if gol_casa > porta_casa:
        mancanti.append(f"{home}: {gol_casa} gol ma {porta_casa} tiri in porta")
    if gol_ospite > porta_ospite:
        mancanti.append(f"{away}: {gol_ospite} gol ma {porta_ospite} tiri in porta")
    if not mancanti:
        return False, ""
    return True, ("\n⏳ Statistiche in ritardo sul risultato (" + "; ".join(mancanti)
                  + "): i numeri qui sopra non sono ancora aggiornati.\n")


def estrai_current_stats(stats_home, stats_away):
    """Le 4 statistiche (casa, trasferta) usate ovunque nel bot: tiri totali, tiri in porta,
    corner, tiri in area. Chi non usa "Tiri in area" può semplicemente ignorare quella chiave."""
    return {
        "Tiri totali": (estrai_valore_stat(stats_home, "Total Shots"), estrai_valore_stat(stats_away, "Total Shots")),
        "Tiri in porta": (estrai_valore_stat(stats_home, "Shots on Goal"), estrai_valore_stat(stats_away, "Shots on Goal")),
        "Corner": (estrai_valore_stat(stats_home, "Corner Kicks"), estrai_valore_stat(stats_away, "Corner Kicks")),
        "Tiri in area": (estrai_valore_stat(stats_home, "Shots insidebox"), estrai_valore_stat(stats_away, "Shots insidebox")),
    }


def testo_confronto_tempi(stats_fine_1h, current_stats):
    """Riga di riepilogo 1° tempo vs 2° tempo (il 2°T è la differenza tra il cumulativo attuale
    e lo snapshot salvato a fine 1°T). Serve a vedere chi ha dominato sull'intero 2° tempo, non
    solo il delta ultimi 15 min che può capovolgersi più volte nello stesso tempo."""
    etichette = {"Tiri totali": "Tiri", "Tiri in porta": "Porta", "Corner": "Corner"}
    parti_1t, parti_2t = [], []
    for chiave, label in etichette.items():
        h1, a1 = stats_fine_1h.get(chiave, (0, 0))
        hc, ac = current_stats.get(chiave, (0, 0))
        h2, a2 = max(0, hc - h1), max(0, ac - a1)
        parti_1t.append(f"{label} {h1}-{a1}")
        parti_2t.append(f"{label} {h2}-{a2}")
    return f"\n1° tempo: {' | '.join(parti_1t)}\n2° tempo: {' | '.join(parti_2t)}\n"


def testo_confronto_tempi_parziale(history, current_stats):
    """Come testo_confronto_tempi, ma per quando manca lo snapshot di fine 1°T (il bot ha
    iniziato a monitorare la partita a metà, es. per un riavvio durante l'intervallo): invece di
    non mostrare nulla, usa come base il primo dato che il bot ha davvero visto (history[0]) e
    mostra il delta da lì, dicendo esplicitamente da che minuto parte - non è "tutto il 2°
    tempo", ma è comunque meglio di niente, ed è onesto su cosa manca."""
    primo = history[0]
    minuto_base = primo.get("minuto") or 0
    stats_base = primo.get("stats", {}) or {}
    etichette = {"Tiri totali": "Tiri", "Tiri in porta": "Porta", "Corner": "Corner"}
    parti = []
    for chiave, label in etichette.items():
        h0, a0 = stats_base.get(chiave, (0, 0))
        hc, ac = current_stats.get(chiave, (0, 0))
        h, a = max(0, hc - h0), max(0, ac - a0)
        parti.append(f"{label} {h}-{a}")
    return f"\n(1°T/2°T non completo, il bot segue questa partita solo dal {minuto_base}')\nDal {minuto_base}': {' | '.join(parti)}\n"


# =============================================================================
# COMANDI TELEGRAM (funzioni riutilizzabili da testo e da bottoni inline)
# =============================================================================
def cmd_help(chat_id):
    help_text = (
        "Comandi disponibili:\n"
        "/help - Mostra questo messaggio\n"
        "/status <squadra> - Info live su una partita\n"
        "/momentum <squadra> - Grafico dell'andamento pressione durante la partita (solo partite monitorate)\n"
        "/intensita - Classifica le partite live per probabilità di essere \"calde\" ora\n"
        "/analisi <squadra casa> - <squadra trasferta> - Distribuzione storica gol per fascia di minuto (es: /analisi Milan - Juventus)\n"
        "/aggiornastorico - Forza l'aggiornamento dello storico minutaggi usato da /analisi\n"
        "/favorites - Lista partite preferite\n"
        "/clearfavorites - Svuota lista preferiti\n"
        "/silenced - Lista partite silenziate\n"
        "/live - Mostra tutte le partite live (con conteggio di quante hanno statistiche disponibili)\n"
        "/piano - Piano giornata: partite whitelist previste oggi e finestre orarie attive\n"
        "/stop - Metti il bot in pausa (nessuna chiamata API, nessuna notifica)\n"
        "/riprendi - Riattiva il bot dopo /stop\n"
        "/modalitaessenziale - Solo gol/rossi/rigori/recupero lungo, sospende le altre notifiche\n"
        "/modalitacompleta - Torna alle notifiche di soglia normali\n"
        "/testpreferiti - Verifica se il canale preferiti dedicato è raggiungibile\n"
        "/shadowlog - Riepilogo e file dei dati raccolti per la validazione (quote vs risultati)\n"
        "/shadowlogstrategie - Riepilogo e file dei dati raccolti in background sull'efficacia "
        "di sei condizioni di gioco (non più esposte come comandi live)\n"
        "/shadowlogdominio - A che quota di dominio arrivano davvero le partite, e quante "
        "entrerebbero nei preferiti con le soglie attuali\n"
        "/diagnostica - Controllo dal vivo di ogni partita live: dati arrivati, quota, shadow-log, "
        "eventuali anomalie\n"
        "/dominio - Cruscotto immediato: chi sta facendo la partita e dove il risultato non lo "
        "rispecchia ancora (in cima le partite che dominano e perdono). Nessuna chiamata API\n"
        "/coperturaleghe - Quali campionati pubblicano statistiche reali e quante giornate senza "
        "dati ha accumulato ciascuno (da quale giornata è partita la verifica)\n"
        "/funzioni - Cosa fa il bot: funzioni stabili, in validazione, novità recenti\n"
        "/apiusage - Quante chiamate API-Football il bot fa al giorno (storico e quota residua)\n"
        "/uptime - Quanto è stato raggiungibile il bot visto da fuori (UptimeRobot): "
        "disponibilità 24h/7g/30g e ultimo disservizio\n"
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
    for fid in list(FAVORITE_MATCHES):
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
    for fid, info in list(SILENCED_MATCHES.items()):
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
        if fixture_in_whitelist(f)
    ]
    if not partite_cmd:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Nessuna partita live monitorata al momento.", "parse_mode": "Markdown"}, timeout=5)
        return
    MAX_PARTITE_MOSTRATE = 20
    header = f"Partite live monitorate: {len(partite_cmd)}"
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
        if dati_ok:
            n_con_dati += 1
        log(f"  /live check: {home} vs {away} (id {fid}) - statistiche {'DISPONIBILI' if dati_ok else 'assenti'}")

        match_lines.append(f"- {home} {score_h}-{score_a} {away} ({league}, {minute}')")
        time.sleep(0.3)

    n_mostrate = len(match_lines)
    lines = [header] + match_lines
    if len(partite_cmd) > n_mostrate:
        lines.append(f"\n... e altre {len(partite_cmd) - n_mostrate} partite non mostrate")
    lines.append(f"\nStatistiche disponibili: {n_con_dati}/{n_mostrate} mostrate")
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "Markdown"}, timeout=5)


def cmd_piano(chat_id):
    """Mostra il piano giornata corrente: partite whitelist previste oggi, orari di kickoff e
    finestre orarie attive usate dallo scheduler adattivo per decidere il ritmo dei cicli."""
    if not PIANO_GIORNATA.get("data"):
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Nessun piano giornata generato ancora. Verrà creato al prossimo ciclo."}, timeout=5)
        return

    tz_italia = ZoneInfo("Europe/Rome")
    generato = datetime.datetime.fromtimestamp(PIANO_GIORNATA.get("generato_alle", 0), tz_italia)
    partite = sorted(PIANO_GIORNATA.get("partite", []), key=lambda p: p.get("kickoff_ts", 0))
    finestre = PIANO_GIORNATA.get("finestre_attive", [])

    now_ts = time.time()
    durata = DURATA_STIMATA_PARTITA_MINUTI * 60

    # Il piano copre l'intera giornata, che comincia a notte fonda: mostrarlo sempre dall'inizio
    # significava che dal pomeriggio in poi le prime 40 righe erano tutte partite finite da ore, e
    # quelle in corso finivano nel "...e altre N". Letto a metà giornata deve rispondere a "cosa si
    # gioca adesso e cosa viene dopo", non ripetere la notte appena passata.
    in_corso, prossime, concluse = [], [], []
    for p in partite:
        kickoff = p.get("kickoff_ts", 0)
        if kickoff > now_ts:
            prossime.append(p)
        elif now_ts <= kickoff + durata:
            in_corso.append(p)
        else:
            concluse.append(p)

    def riga_partita(p, suffisso=""):
        ora = datetime.datetime.fromtimestamp(p["kickoff_ts"], tz_italia).strftime("%H:%M")
        return f"- {ora} {p['home']} - {p['away']} ({p['lega']}){suffisso}"

    adesso_txt = datetime.datetime.fromtimestamp(now_ts, tz_italia).strftime("%H:%M")
    righe = [
        f"Piano giornata {PIANO_GIORNATA.get('data')} (generato alle {generato.strftime('%H:%M')})\n"
        f"{len(partite)} partite whitelist previste, {len(finestre)} finestre attive\n"
        f"Sono le {adesso_txt}: {len(in_corso)} in corso, {len(prossime)} ancora da giocare, "
        f"{len(concluse)} concluse\n"
    ]

    if in_corso:
        righe.append(f"IN CORSO ({len(in_corso)}):")
        for p in in_corso:
            da_quanto = int((now_ts - p["kickoff_ts"]) // 60)
            righe.append(riga_partita(p, f" - iniziata {da_quanto} min fa"))
        righe.append("")

    if prossime:
        MAX_PROSSIME = 30
        righe.append(f"PROSSIME ({len(prossime)}):")
        for p in prossime[:MAX_PROSSIME]:
            manca = int((p["kickoff_ts"] - now_ts) // 60)
            attesa = f" - fra {manca} min" if manca < 120 else ""
            righe.append(riga_partita(p, attesa))
        if len(prossime) > MAX_PROSSIME:
            righe.append(f"... e altre {len(prossime) - MAX_PROSSIME} più tardi")
        righe.append("")
    elif not in_corso:
        righe.append("Nessuna partita in corso né in programma per il resto della giornata.\n")

    if finestre:
        righe.append("Finestre attive (ciclo veloce):")
        giorno_piano = datetime.datetime.fromtimestamp(
            partite[0]["kickoff_ts"] if partite else now_ts, tz_italia).date()
        for inizio, fine in finestre:
            dt_i = datetime.datetime.fromtimestamp(inizio, tz_italia)
            dt_f = datetime.datetime.fromtimestamp(fine, tz_italia)
            # Le finestre di una giornata che comincia a notte fonda possono chiudersi dopo la
            # mezzanotte: con il solo orario risultavano a rovescio ("11:00 - 03:40", cioè una fine
            # prima dell'inizio), che sembra un errore invece di essere il giorno dopo.
            suffisso_i = " (ieri)" if dt_i.date() < giorno_piano else ""
            suffisso_f = " (domani)" if dt_f.date() > dt_i.date() else ""
            marcatore = " ← adesso" if inizio <= now_ts <= fine else ""
            righe.append(f"- {dt_i.strftime('%H:%M')}{suffisso_i} - "
                         f"{dt_f.strftime('%H:%M')}{suffisso_f}{marcatore}")

    if STATO_PAUSA.get("in_pausa"):
        stato = "IN PAUSA MANUALE (nessuna chiamata API, invia /riprendi per riattivare)"
    elif dentro_finestra_attiva(PIANO_GIORNATA, now_ts):
        stato = "ATTIVA (ciclo veloce)"
    else:
        stato = "fuori finestra (ciclo rallentato)"
    righe.append(f"\nAdesso: {stato}")

    # "Ciclo veloce/rallentato" sopra riguarda solo il ritmo del monitoraggio (sempre attivo
    # 24/7), non se le notifiche arrivano davvero: senza questa riga il comando potrebbe dire
    # "ATTIVA" anche fuori dalla fascia oraria configurata, facendo pensare che una notifica
    # stia per arrivare quando in realtà è soppressa (vedi notifiche_attive nel loop principale).
    if not STATO_PAUSA.get("in_pausa"):
        fascia = f"{ORARIO_ATTIVO_INIZIO_ORA:02d}:{ORARIO_ATTIVO_INIZIO_MINUTO:02d}-{ORARIO_ATTIVO_FINE_ORA:02d}:{ORARIO_ATTIVO_FINE_MINUTO:02d}"
        if dentro_orario_attivo():
            righe.append(f"Notifiche: ATTIVE (fascia oraria {fascia})")
        else:
            righe.append(f"Notifiche: in pausa fuori fascia oraria ({fascia}) - monitoraggio e raccolta dati comunque attivi")

    testo = "\n".join(righe)
    for i in range(0, len(testo), 3800):
        pezzo = testo[i:i + 3800]
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": pezzo}, timeout=10)


def cmd_stop(chat_id):
    """Mette il bot in pausa manuale: il ciclo principale smette di chiamare l'API e di mandare
    notifiche finché non arriva /riprendi. Utile quando l'utente sa già che non seguirà il trading
    per un po' (es. si sta disconnettendo), indipendentemente da cosa dice il piano giornata."""
    global STATO_PAUSA
    if STATO_PAUSA.get("in_pausa"):
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Il bot è già in pausa. Invia /riprendi per riattivarlo."}, timeout=5)
        return
    STATO_PAUSA = {"in_pausa": True, "dal": time.time(), "ultimo_promemoria": time.time()}
    salva_pausa(STATO_PAUSA)
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": "⏸ Bot in pausa. Nessuna chiamata API e nessuna notifica finché non invii /riprendi.\n"
                    "Ogni 6 ore ti mando un promemoria se resta in pausa, per non farti dimenticare di riattivarlo."
        }, timeout=5)


def cmd_riprendi(chat_id):
    """Toglie la pausa manuale e fa ripartire subito il ciclo (invece di aspettare il prossimo
    giro), così le partite in corso vengono recuperate senza ritardo."""
    global STATO_PAUSA
    if not STATO_PAUSA.get("in_pausa"):
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Il bot non è in pausa."}, timeout=5)
        return
    STATO_PAUSA = {"in_pausa": False, "dal": 0, "ultimo_promemoria": 0}
    salva_pausa(STATO_PAUSA)
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "▶️ Bot riattivato. Riprendo subito a controllare le partite live."}, timeout=5)


def cmd_modalitaessenziale(chat_id):
    """Attiva la modalità essenziale: da qui in poi solo gol, cartellini rossi, rigori e recupero
    lungo generano una notifica, per ridurre il rumore nelle serate con tante partite insieme."""
    global MODALITA_NOTIFICHE
    if MODALITA_NOTIFICHE.get("essenziale"):
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "La modalità essenziale è già attiva."}, timeout=5)
        return
    MODALITA_NOTIFICHE = {"essenziale": True}
    salva_modalita(MODALITA_NOTIFICHE)
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": "🔕 Modalità essenziale attiva: da ora solo gol, cartellini rossi, rigori e "
                    "recupero lungo. Le notifiche di soglia (tiri, momentum) sono sospese, anche "
                    "per i preferiti. Invia /modalitacompleta per tornare come prima."
        }, timeout=5)


def cmd_modalitacompleta(chat_id):
    """Disattiva la modalità essenziale, tornando alle soglie normali di notifica."""
    global MODALITA_NOTIFICHE
    if not MODALITA_NOTIFICHE.get("essenziale"):
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "La modalità completa è già attiva."}, timeout=5)
        return
    MODALITA_NOTIFICHE = {"essenziale": False}
    salva_modalita(MODALITA_NOTIFICHE)
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "🔔 Modalità completa ripristinata: tornano le notifiche di soglia normali."}, timeout=5)


def cmd_testpreferiti(chat_id):
    """Manda un messaggio di prova al canale preferiti dedicato (TELEGRAM_CHAT_ID_PREFERITI) e
    riporta subito l'esito in questa chat, per diagnosticare se il canale è raggiungibile senza
    dover scavare nei log di Render."""
    if TELEGRAM_CHAT_ID_PREFERITI == TELEGRAM_CHAT_ID:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "Nessun canale preferiti dedicato configurato (TELEGRAM_CHAT_ID_PREFERITI non impostata): "
                        "le notifiche dei preferiti arrivano in questa stessa chat."
            }, timeout=5)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        risposta = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID_PREFERITI,
                "text": "✅ Test canale preferiti: se leggi questo messaggio qui, il canale dedicato funziona."
            }, timeout=10)
        if risposta.status_code == 200:
            esito = f"✅ Messaggio di test inviato con successo al canale preferiti (chat_id {TELEGRAM_CHAT_ID_PREFERITI}). Controlla che sia arrivato lì."
        else:
            esito = (
                f"❌ Invio al canale preferiti fallito: HTTP {risposta.status_code} - {risposta.text[:300]}\n"
                "Controlla che il bot sia amministratore di quel canale/gruppo e che l'ID sia corretto."
            )
    except Exception as e:
        esito = f"❌ Eccezione inviando al canale preferiti: {e}"
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": esito}, timeout=5)


def cmd_shadowlog(chat_id):
    """Manda un riepilogo numerico + il file grezzo di shadow_log_valore.jsonl via Telegram.
    Serve perché non esiste nessun altro modo per leggere questo file da fuori Render (nessun
    accesso diretto al filesystem/ai log del servizio da una sessione Claude Code) - pensato
    apposta per il checkpoint di validazione (verificare quante partite hanno sia lo snapshot sia
    il risultato finale, il campione utilizzabile per un'analisi di calibrazione)."""
    if not os.path.exists(SHADOW_LOG_VALORE_FILE):
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "Nessun dato ancora: shadow_log_valore.jsonl non esiste (il bot non ha ancora registrato nessuno snapshot)."
            }, timeout=5)
        return

    snapshot_fixtures, risultato_fixtures = set(), set()
    try:
        dati, totale_righe, righe_malformate = leggi_shadow_log(SHADOW_LOG_VALORE_FILE)
        for dato in dati:
            fid = dato.get("fixture_id")
            if dato.get("tipo") == "snapshot":
                snapshot_fixtures.add(fid)
            elif dato.get("tipo") == "risultato_finale":
                risultato_fixtures.add(fid)
    except Exception as e:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": f"Errore leggendo shadow_log_valore.jsonl: {e}"}, timeout=5)
        return

    partite_complete = snapshot_fixtures & risultato_fixtures
    testo = (
        "Shadow-log valore - riepilogo:\n"
        f"- Righe totali: {totale_righe}" + (f" ({righe_malformate} malformate)" if righe_malformate else "") + "\n"
        f"- Partite con almeno uno snapshot: {len(snapshot_fixtures)}\n"
        f"- Partite con risultato finale registrato: {len(risultato_fixtures)}\n"
        f"- Partite complete (snapshot + risultato, utilizzabili per la validazione): {len(partite_complete)}\n\n"
        "Servono circa 200-500 partite complete per una validazione statisticamente sensata "
        "(minimo), idealmente 1000+."
    )
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": testo}, timeout=5)

    try:
        with open(SHADOW_LOG_VALORE_FILE, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                data={"chat_id": chat_id},
                files={"document": f}, timeout=30)
    except Exception as e:
        log(f"Errore invio file shadow_log_valore.jsonl: {e}")


def _percentile(valori_ordinati, percentuale):
    """Percentile su una lista gia' ordinata, senza numpy (non e' fra le dipendenze del bot)."""
    if not valori_ordinati:
        return None
    posizione = (len(valori_ordinati) - 1) * percentuale / 100
    basso, alto = int(posizione), min(int(posizione) + 1, len(valori_ordinati) - 1)
    return round(valori_ordinati[basso] + (valori_ordinati[alto] - valori_ordinati[basso]) * (posizione - basso), 1)


def _cascata_soglie_dominio(osservate, quota, volume, cicli):
    """Quante partite superano quota, poi quota+volume, poi tutti e tre i filtri."""
    passa_q = [o for o in osservate if o["quota"] >= quota]
    passa_qv = [o for o in passa_q if (o["volume"] or 0) >= volume]
    passa_qvc = [o for o in passa_qv if (o["cicli"] or 0) >= cicli]
    return len(passa_q), len(passa_qv), len(passa_qvc)


def cmd_shadowlogdominio(chat_id):
    """Riepilogo + file di shadow_log_auto_preferiti_dominio.jsonl: quante partite entrerebbero
    davvero nei preferiti dalla rotta dominio, con le soglie attuali e con altre.

    E' il comando che serve per decidere se accendere la rotta (AUTO_PREFERITI_DOMINIO_ATTIVO):
    finche' e' spenta il bot registra tutto senza promuovere nulla, e le soglie 78%/16 restano
    stime a occhio.

    La prima versione riportava solo quante partite superavano la soglia di QUOTA, avvertendo che
    era "prima di applicare volume e cicli" - cioe' il piu' permissivo dei tre filtri, quello che
    da solo non decide niente. Con 255 partite osservate diceva "57%", un numero che sembrava
    condannare la soglia mentre non stava misurando la regola vera. Ora si mostra la cascata
    completa e il ritmo che ne uscirebbe in partite al giorno, che e' la cosa da guardare per
    capire se il canale resterebbe leggibile.

    Approssimazione dichiarata: quota_max e volume_al_max sono la coppia dell'istante di picco,
    mentre cicli_consecutivi_max e' la striscia piu' lunga della partita, che puo' non coincidere
    col picco. Va letta come stima del limite superiore, non come conteggio esatto."""
    if not os.path.exists(SHADOW_LOG_AUTO_PREFERITI_DOMINIO_FILE):
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id,
                  "text": ("Nessun dato ancora: shadow_log_auto_preferiti_dominio.jsonl non "
                           "esiste. Il primo verdetto per partita viene scritto al "
                           f"{MINUTO_VERDETTO_SHADOW_AUTO_PREFERITI}', quindi serve almeno una "
                           "partita seguita fin li'.")}, timeout=5)
        return

    osservate, scattate, righe_totali, malformate = [], 0, 0, 0
    primo_ts = ultimo_ts = None
    try:
        with open(SHADOW_LOG_AUTO_PREFERITI_DOMINIO_FILE, "r") as f:
            for riga in f:
                riga = riga.strip()
                if not riga:
                    continue
                righe_totali += 1
                try:
                    dato = json.loads(riga)
                except Exception:
                    malformate += 1
                    continue
                if dato.get("auto_preferiti_dominio_scattato"):
                    scattate += 1
                ts = dato.get("timestamp")
                if ts:
                    primo_ts = ts if primo_ts is None else min(primo_ts, ts)
                    ultimo_ts = ts if ultimo_ts is None else max(ultimo_ts, ts)
                quota_max = dato.get("quota_max")
                if quota_max is not None:
                    osservate.append({
                        "quota": quota_max,
                        "volume": dato.get("volume_al_max"),
                        "cicli": dato.get("cicli_consecutivi_max", 0),
                    })
    except Exception as e:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id,
                  "text": f"Errore leggendo shadow_log_auto_preferiti_dominio.jsonl: {e}"}, timeout=5)
        return

    picchi = sorted(o["quota"] for o in osservate)
    giorni = max(1.0, ((ultimo_ts - primo_ts) / 86400) if (primo_ts and ultimo_ts) else 1.0)
    righe = [
        "Shadow-log rotta dominio - riepilogo:",
        f"- Partite osservate: {righe_totali}"
        + (f" ({malformate} riga malformata)" if malformate == 1
           else f" ({malformate} righe malformate)" if malformate else ""),
        f"- Con dominio misurabile: {len(osservate)}",
        f"- Promosse davvero dalla rotta dominio: {scattate}",
        f"- Promozione: {'ATTIVA' if AUTO_PREFERITI_DOMINIO_ATTIVO else 'spenta (sola osservazione)'}",
        f"- Giorni di raccolta: {giorni:.1f}",
        "",
        f"Soglie attuali: quota {SOGLIA_QUOTA_DOMINIO_AUTO_PREFERITI}%, volume "
        f"{VOLUME_MINIMO_DOMINIO_AUTO_PREFERITI}, {CICLI_DOMINIO_PER_AUTO_PREFERITI} cicli di fila",
    ]
    if osservate:
        q, v, c = (SOGLIA_QUOTA_DOMINIO_AUTO_PREFERITI, VOLUME_MINIMO_DOMINIO_AUTO_PREFERITI,
                   CICLI_DOMINIO_PER_AUTO_PREFERITI)
        solo_q, con_v, con_c = _cascata_soglie_dominio(osservate, q, v, c)
        tot = len(osservate)
        righe += [
            "",
            f"QUANTE ENTREREBBERO DAVVERO (su {tot} partite):",
            f"- solo quota >= {q}%: {solo_q} ({round(solo_q / tot * 100)}%)",
            f"- + volume >= {v}: {con_v} ({round(con_v / tot * 100)}%)",
            f"- + {c} cicli di fila: {con_c} ({round(con_c / tot * 100)}%)",
            f"=> circa {con_c / giorni:.1f} partite al giorno nel canale preferiti",
            "",
            "Picco di dominio raggiunto (percentili):",
            f"- mediana: {_percentile(picchi, 50)}%",
            f"- 75°: {_percentile(picchi, 75)}%   90°: {_percentile(picchi, 90)}%   "
            f"95°: {_percentile(picchi, 95)}%",
            f"- massimo visto: {picchi[-1]}%",
            "",
            "E SE SI STRINGESSE (quota / volume, stessi cicli):",
        ]
        for qq, vv in ((q, v), (q, v + 8), (88, v), (88, v + 8), (95, v + 8)):
            _, _, passate = _cascata_soglie_dominio(osservate, qq, vv, c)
            marca = "  <- attuale" if (qq, vv) == (q, v) else ""
            righe.append(f"- {qq}% / vol {vv}: {passate} partite "
                         f"({passate / giorni:.1f} al giorno){marca}")
        righe += [
            "",
            "Nota: quota e volume sono presi all'istante di picco, i cicli sono la striscia piu'",
            "lunga della partita, che puo' non coincidere col picco. E' una stima del limite",
            "superiore: le partite vere saranno queste o meno.",
        ]
    else:
        righe.append("\nNessuna partita ha ancora raggiunto un dominio misurabile "
                     "(statistiche assenti o squadre pari).")
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "\n".join(righe)}, timeout=5)

    try:
        with open(SHADOW_LOG_AUTO_PREFERITI_DOMINIO_FILE, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                data={"chat_id": chat_id}, files={"document": f}, timeout=30)
    except Exception as e:
        log(f"Errore invio file shadow_log_auto_preferiti_dominio.jsonl: {e}")


def cmd_shadowlogstrategie(chat_id):
    """Manda un riepilogo numerico + il file grezzo di shadow_log_strategie.jsonl via Telegram,
    stesso principio di /shadowlog ma per le sei strategie: quante volte scatta ciascuna e su
    quante partite diverse, più il file grezzo (segnali + gol reali) per un'analisi offline più
    fine. Nessuna soglia o classifica costruita su questi numeri finché non ce ne sono abbastanza."""
    if not os.path.exists(SHADOW_LOG_STRATEGIE_FILE):
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "Nessun dato ancora: shadow_log_strategie.jsonl non esiste (il bot non ha ancora registrato nessuno snapshot)."
            }, timeout=5)
        return

    snapshot_fixtures, risultato_fixtures = set(), set()
    conteggio_strategie = {nome: 0 for nome, _e, _f, _d in STRATEGIE}
    fixtures_per_strategia = {nome: set() for nome, _e, _f, _d in STRATEGIE}
    try:
        dati, totale_righe, righe_malformate = leggi_shadow_log(SHADOW_LOG_STRATEGIE_FILE)
        for dato in dati:
            fid = dato.get("fixture_id")
            if dato.get("tipo") == "snapshot":
                snapshot_fixtures.add(fid)
                for segnale in dato.get("segnali", []):
                    nome_strat = segnale.get("strategia")
                    if nome_strat in conteggio_strategie:
                        conteggio_strategie[nome_strat] += 1
                        fixtures_per_strategia[nome_strat].add(fid)
            elif dato.get("tipo") == "risultato_finale":
                risultato_fixtures.add(fid)
    except Exception as e:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": f"Errore leggendo shadow_log_strategie.jsonl: {e}"}, timeout=5)
        return

    partite_complete = snapshot_fixtures & risultato_fixtures
    righe_strategie = "\n".join(
        f"  {nome}: {conteggio_strategie[nome]} volte scattata, su {len(fixtures_per_strategia[nome])} partite diverse"
        for nome, _e, _f, _d in STRATEGIE
    )
    testo = (
        "Shadow-log strategie - riepilogo:\n"
        f"- Righe totali: {totale_righe}" + (f" ({righe_malformate} malformate)" if righe_malformate else "") + "\n"
        f"- Partite con almeno uno snapshot: {len(snapshot_fixtures)}\n"
        f"- Partite con risultato finale registrato: {len(risultato_fixtures)}\n"
        f"- Partite complete (snapshot + risultato, utilizzabili per l'analisi): {len(partite_complete)}\n\n"
        f"Quante volte è scattata ciascuna strategia finora:\n{righe_strategie}\n\n"
        "Ancora nessuna analisi fatta su questi numeri: servono abbastanza partite complete "
        "prima di poter dire se scattare anticipa davvero qualcosa o no."
    )
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": testo}, timeout=5)

    try:
        with open(SHADOW_LOG_STRATEGIE_FILE, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                data={"chat_id": chat_id},
                files={"document": f}, timeout=30)
    except Exception as e:
        log(f"Errore invio file shadow_log_strategie.jsonl: {e}")


def cmd_dominio(chat_id):
    """Cruscotto: tutte le partite seguite, ordinate per quanto il risultato tradisce il campo.

    Zero chiamate API - legge solo stato_partite, già aggiornato dal ciclo principale (a differenza
    di /intensita, che ne fa una per partita e va aspettata). Serve a rispondere in un colpo
    d'occhio a "dove sta succedendo qualcosa che il punteggio non dice ancora"."""
    righe_sotto, righe_bloccate, righe_avanti, senza_dominio = [], [], [], 0
    for fid, stato in list(stato_partite.items()):
        history = stato.get("history", [])
        if not history:
            continue
        current_stats = history[-1].get("stats")
        score_h = stato.get("score_home", 0)
        score_a = stato.get("score_away", 0)
        dominio = calcola_dominio(current_stats, score_h, score_a)
        if not dominio:
            senza_dominio += 1
            continue
        home = stato.get("home", "?")
        away = stato.get("away", "?")
        chi = home if dominio["lato"] == 0 else away
        minuto = stato.get("last_minute", "?")
        tiri = current_stats.get("Tiri totali", (0, 0))
        porta = current_stats.get("Tiri in porta", (0, 0))
        if dominio["lato"] == 1:
            tiri, porta = (tiri[1], tiri[0]), (porta[1], porta[0])
        # Due righe secche, senza ripetere quello che dice già l'intestazione del gruppo: la prima
        # inquadra la partita, la seconda dice chi comanda e con quali numeri.
        blocco = (
            f"{home} {score_h}-{score_a} {away} · {minuto}'\n"
            f"{barra_dominio(dominio['quota'])} {dominio['quota']}% {chi} · "
            f"{tiri[0]}-{tiri[1]} tiri, {porta[0]}-{porta[1]} in porta"
        )
        destinazione = {0: righe_sotto, 1: righe_bloccate, 2: righe_avanti}[dominio["priorita"]]
        destinazione.append((dominio["quota"], blocco))

    if not (righe_sotto or righe_bloccate or righe_avanti):
        invia_messaggio_telegram(
            "Nessuna partita con un dominio netto in questo momento.\n\n"
            f"{senza_dominio} partite seguite sono equilibrate, o non hanno ancora abbastanza "
            "gioco per dare un verdetto.", chat_id=chat_id)
        return

    def ordina(voci):
        return [b for _, b in sorted(voci, key=lambda v: -v[0])]

    parti = ["⚡ *DOMINIO* — chi fa la partita, e cosa dice il risultato"]
    if righe_sotto:
        parti.append("🔥 *DOMINA E PERDE*\n" + "\n\n".join(ordina(righe_sotto)))
    if righe_bloccate:
        parti.append("⚡ *DOMINA E NON SEGNA*\n" + "\n\n".join(ordina(righe_bloccate)))
    if righe_avanti:
        parti.append("▪️ *DOMINA ED È AVANTI* (il risultato rispecchia)\n" + "\n\n".join(ordina(righe_avanti)))
    if senza_dominio:
        parti.append(f"_Altre {senza_dominio} partite seguite: equilibrate o con troppo poco gioco._"
                     if senza_dominio > 1 else
                     "_Un'altra partita seguita: equilibrata o con troppo poco gioco._")

    invia_messaggio_telegram("\n\n".join(parti), chat_id=chat_id)


def cmd_coperturaleghe(chat_id):
    """A che punto è ogni campionato nella verifica "pubblica statistiche reali o no".

    Serve a vedere il conteggio mentre matura, invece di scoprire l'esito solo quando arriva la
    notifica di esclusione: da quale giornata è cominciata la verifica di ciascun campionato e
    quante giornate senza dati ha accumulato finora."""
    if not GIORNATE_SENZA_STATISTICHE:
        invia_messaggio_telegram(
            "Nessun campionato ancora osservato.\n\n"
            "Il conteggio parte dalla prima giornata vista dopo l'attivazione della regola: "
            "appena passano delle partite live, quella giornata diventa la 1ª di test.",
            chat_id=chat_id)
        return

    escluse, sorvegliate, pulite = [], [], []
    for (paese, nome), stato in sorted(GIORNATE_SENZA_STATISTICHE.items()):
        senza = _giornate_senza_statistiche_contate(stato)
        etichetta = f"{nome.title()} ({paese.title()})"
        prima = stato.get("prima_giornata") or "?"
        if stato.get("esclusa_definitivamente"):
            escluse.append(f"🚫 {etichetta} - fuori dalla whitelist ({len(senza)} giornate)")
        elif senza:
            sorvegliate.append(
                f"⚠️ {etichetta} - {len(senza)}/{SOGLIA_GIORNATE_SENZA_STATISTICHE}"
                f" - verifica dalla \"{prima}\"")
        else:
            pulite.append(f"✅ {etichetta} - verifica dalla \"{prima}\"")

    righe = ["📊 *Copertura statistiche per campionato*\n",
             f"Un campionato esce dalla whitelist dopo {SOGLIA_GIORNATE_SENZA_STATISTICHE} "
             f"giornate di campionato senza mai pubblicare tiri, tiri in porta, corner e tiri in "
             f"area. Le giornate in cui l'API è guasta su molti campionati insieme non contano.\n"]
    if escluse:
        righe.append("\n".join(escluse) + "\n")
    if sorvegliate:
        righe.append("*Con giornate senza dati:*\n" + "\n".join(sorvegliate) + "\n")
    if pulite:
        # Le leghe a posto sono la maggioranza e non aggiungono informazione: se ne mostra un
        # campione, il totale basta a sapere che sono seguite.
        mostrate = pulite[:15]
        coda = f"\n…e altre {len(pulite) - len(mostrate)}" if len(pulite) > len(mostrate) else ""
        righe.append(f"*Regolari ({len(pulite)}):*\n" + "\n".join(mostrate) + coda)

    invia_messaggio_telegram("\n".join(righe), chat_id=chat_id)


def cmd_diagnostica(chat_id):
    """Controllo dal vivo, partita per partita, di ogni passaggio della pipeline dati (tracciamento,
    statistiche, xG, quota 1X2, shadow-log valore, shadow-log strategie, quali strategie
    scatterebbero adesso) - pensato per verificare in tempo reale, mentre le partite sono in corso,
    se i dati vengono davvero raccolti e analizzati, invece di scoprirlo solo a fine giornata.

    Usa lo stato già in memoria (stato_partite, PIANO_GIORNATA) più UNA sola chiamata fresca a
    get_partite_live() per sapere cosa è live adesso: nessuna chiamata aggiuntiva alle statistiche,
    per non consumare quota extra proprio mentre ci sono più partite in corso.

    Le anomalie trovate vengono sia scritte nel messaggio Telegram sia loggate con log() (visibili
    nei log di Render), cosi' risultano in entrambi i posti come chiesto."""
    partite_raw = get_partite_live()
    if not partite_raw:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "🔍 Diagnostica: nessuna partita live in questo momento secondo l'API."}, timeout=5)
        return

    partite_valide = [
        f for f in partite_raw
        if fixture_in_whitelist(f)
    ]
    ora = time.time()
    righe = [
        f"🔍 Diagnostica pipeline dati\n"
        f"{len(partite_raw)} partite live dall'API, {len(partite_valide)} in campionati con statistiche note."
    ]
    anomalie = []

    for f in partite_valide[:12]:
        fid = f["fixture"]["id"]
        home = f["teams"]["home"]["name"]
        away = f["teams"]["away"]["name"]
        minuto_api = f["fixture"]["status"].get("elapsed") or 0
        league_name = f.get("league", {}).get("name", "")
        league_country = f.get("league", {}).get("country", "")
        stato = stato_partite.get(fid)

        blocco = [f"{home} vs {away} ({formatta_lega(league_name, league_country)}, {minuto_api}' da API)"]

        if not stato:
            blocco.append("  ⚠️ Non ancora tracciata dal bot (nessun ciclo l'ha ancora processata)")
            anomalie.append(f"{home}-{away}: mai tracciata pur essendo live e in un campionato valido")
            righe.append("\n".join(blocco))
            continue

        minuto_bot = stato.get("last_minute")
        if minuto_bot is not None and abs(minuto_api - minuto_bot) > 5:
            blocco.append(f"  ⚠️ Minuto bot fermo a {minuto_bot}' contro {minuto_api}' dell'API (ciclo lento o bloccato)")
            anomalie.append(f"{home}-{away}: minuto bot {minuto_bot}' vs API {minuto_api}'")
        else:
            blocco.append(f"  ✅ Tracciata, ultimo minuto visto: {minuto_bot}'")

        history = stato.get("history", [])
        if not history:
            esito_stats = stato.get("stats_ultimo_esito")
            vuote = stato.get("stats_vuote_consecutive", 0)
            if esito_stats == "vuote":
                blocco.append(f"  ➖ Statistiche: l'API risponde ma non ne ha per questa partita ({vuote} risposte vuote di fila)")
            elif esito_stats == "errore":
                blocco.append("  ⚠️ Statistiche: ultima chiamata fallita (rate-limit/timeout/rete)")
            else:
                blocco.append("  ⚠️ Statistiche: mai arrivate (nessuna riga nello storico)")
            if minuto_api > 10 and esito_stats != "vuote":
                anomalie.append(f"{home}-{away}: statistiche mai arrivate al {minuto_api}'")
        else:
            eta_stats = ora - history[-1]["timestamp"]
            blocco.append(f"  ✅ Statistiche: ultimo aggiornamento {int(eta_stats)}s fa")
            xg = history[-1].get("xg", [None, None])
            if xg[0] is not None or xg[1] is not None:
                blocco.append(f"  ✅ xG: {xg[0]} - {xg[1]}")
            else:
                blocco.append("  ➖ xG: non disponibile per questa lega/partita")

        quote = quote_1x2_per_fixture(fid)
        if isinstance(quote, dict):
            blocco.append(f"  ✅ Quota 1X2: 1 {quote['casa']:.2f} - X {quote['pareggio']:.2f} - 2 {quote['ospite']:.2f}")
        elif quote is False:
            blocco.append("  ➖ Quota 1X2: controllata, non disponibile per questa partita")
        else:
            blocco.append("  ➖ Quota 1X2: non ancora recuperata")

        ultimo_val = stato.get("ultimo_snapshot_valore")
        if isinstance(quote, dict):
            if ultimo_val:
                blocco.append(f"  ✅ Shadow-log valore: ultimo snapshot {int(ora - ultimo_val)}s fa")
            elif minuto_api > 16:
                blocco.append("  ⚠️ Shadow-log valore: quota presente ma nessuno snapshot ancora scritto")
                anomalie.append(f"{home}-{away}: quota presente ma shadow-log valore vuoto al {minuto_api}'")
            else:
                blocco.append("  ⏳ Shadow-log valore: quota presente, primo snapshot non ancora dovuto (< 15 min)")
        else:
            blocco.append("  ➖ Shadow-log valore: nessuna quota, nessuno snapshot atteso")

        ultimo_strat = stato.get("ultimo_snapshot_strategie")
        if ultimo_strat:
            blocco.append(f"  ✅ Shadow-log strategie: ultimo snapshot {int(ora - ultimo_strat)}s fa")
        elif history and minuto_api > 16:
            blocco.append("  ⚠️ Shadow-log strategie: statistiche presenti ma nessuno snapshot ancora scritto")
            anomalie.append(f"{home}-{away}: statistiche presenti ma shadow-log strategie vuoto al {minuto_api}'")
        elif history:
            blocco.append("  ⏳ Shadow-log strategie: primo snapshot non ancora dovuto (< 15 min)")
        else:
            blocco.append("  ⏳ Shadow-log strategie: in attesa delle prime statistiche")

        if history:
            current_stats_diag = history[-1]["stats"]
            xg_diag = history[-1].get("xg", [None, None])
            minuto_calc = minuto_bot if minuto_bot is not None else minuto_api
            delta_diag, delta_reale_diag = calcola_delta_15min(fid, current_stats_diag, minuto_calc)
            p_diag = {
                "home": home, "away": away, "minute": minuto_calc,
                "score_h": stato.get("score_home", 0), "score_a": stato.get("score_away", 0),
                "stats": current_stats_diag, "delta": delta_diag, "delta_reale": delta_reale_diag,
                "xg_home": xg_diag[0], "xg_away": xg_diag[1],
                "stato_precedente": stato,
            }
            scattate = [emoji for _n, emoji, valuta_fn, _d in STRATEGIE if valuta_fn(p_diag) is not None]
            blocco.append(f"  Strategie che scatterebbero ora: {' '.join(scattate) if scattate else 'nessuna'}")

        righe.append("\n".join(blocco))

    testo = "\n\n".join(righe)
    if len(partite_valide) > 12:
        testo += f"\n\n(mostrate le prime 12 di {len(partite_valide)} partite valide)"

    if anomalie:
        testo += "\n\n⚠️ Anomalie rilevate:\n" + "\n".join(f"- {a}" for a in anomalie)
        log("Diagnostica pipeline: " + " | ".join(anomalie))
    else:
        testo += "\n\n✅ Nessuna anomalia rilevata in questo controllo."

    for i in range(0, len(testo), 3800):
        pezzo = testo[i:i + 3800]
        risposta = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": pezzo}, timeout=10)
        if risposta.status_code != 200:
            log(f"Errore invio diagnostica: HTTP {risposta.status_code} - {risposta.text[:300]}")


def cmd_apiusage(chat_id):
    """Quante chiamate il bot fa davvero verso API-Football, per farsi un'idea concreta della
    quota usata al giorno e valutare se il piano attivo è adeguato. Combina il conteggio del bot
    (storico su più giorni, persistito - vedi registra_chiamata_api) con la quota residua vista
    nell'header dell'ultima risposta API (dato più autorevole per "quanto resta OGGI", perché
    viene direttamente da API-Football, non da un nostro conteggio parallelo)."""
    if not CHIAMATE_API_PER_GIORNO:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Nessuna chiamata API ancora registrata."}, timeout=5)
        return

    tz_italia = ZoneInfo("Europe/Rome")
    adesso = datetime.datetime.now(tz_italia)
    oggi = adesso.strftime("%Y-%m-%d")
    ieri = (adesso - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    righe = ["📊 Utilizzo API-Football\n", f"Oggi ({oggi}): {CHIAMATE_API_PER_GIORNO.get(oggi, 0)} chiamate"]
    if ieri in CHIAMATE_API_PER_GIORNO:
        righe.append(f"Ieri ({ieri}): {CHIAMATE_API_PER_GIORNO[ieri]} chiamate")

    # Media calcolata SOLO sui giorni completi (esclude oggi, ancora in corso): includerlo
    # abbasserebbe artificialmente la media nelle prime ore della giornata.
    giorni_completi = sorted((d, n) for d, n in CHIAMATE_API_PER_GIORNO.items() if d != oggi)
    if giorni_completi:
        media_tutti = sum(n for _, n in giorni_completi) / len(giorni_completi)
        righe.append(f"\nMedia sugli ultimi {len(giorni_completi)} giorni completi: {media_tutti:.0f} chiamate/giorno")

        ultimi_7 = giorni_completi[-7:]
        if len(ultimi_7) >= 2:
            media_7 = sum(n for _, n in ultimi_7) / len(ultimi_7)
            righe.append(f"Media ultimi {len(ultimi_7)} giorni: {media_7:.0f} chiamate/giorno")

        giorno_picco, chiamate_picco = max(giorni_completi, key=lambda x: x[1])
        righe.append(f"Giorno di picco: {giorno_picco} con {chiamate_picco} chiamate")

    if ULTIMA_QUOTA_API["residuo"] is not None:
        eta = int(time.time() - ULTIMA_QUOTA_API["aggiornata"])
        righe.append(
            f"\nQuota residua secondo API-Football ({eta}s fa): "
            f"{ULTIMA_QUOTA_API['residuo']}/{ULTIMA_QUOTA_API['limite']} oggi")

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": "\n".join(righe)}, timeout=5)


# Stati dei monitor UptimeRobot (campo "status" della API v2).
STATI_UPTIMEROBOT = {
    0: ("⏸️", "in pausa"),
    1: ("⏳", "mai controllato finora"),
    2: ("🟢", "raggiungibile"),
    8: ("🟠", "sembra irraggiungibile"),
    9: ("🔴", "irraggiungibile"),
}


def _durata_leggibile(secondi):
    """"3h 12m" invece di "11520": una durata di disservizio si legge, non si converte a mente."""
    try:
        secondi = int(secondi)
    except (TypeError, ValueError):
        return "durata sconosciuta"
    if secondi < 60:
        return f"{secondi}s"
    minuti, sec = divmod(secondi, 60)
    if minuti < 60:
        return f"{minuti}m" if not sec else f"{minuti}m {sec}s"
    ore, minuti = divmod(minuti, 60)
    if ore < 24:
        return f"{ore}h" if not minuti else f"{ore}h {minuti}m"
    giorni, ore = divmod(ore, 24)
    return f"{giorni}g" if not ore else f"{giorni}g {ore}h"


def cmd_uptime(chat_id):
    """Quanto e' stato raggiungibile il bot VISTO DA FUORI, secondo UptimeRobot.

    Risponde a una domanda che il bot da solo non puo' porsi in modo credibile: mentre era giu',
    non stava girando per accorgersene. L'unico giudice sensato e' un osservatore esterno, ed e'
    esattamente quello che UptimeRobot fa ogni 5 minuti sull'endpoint di salute.

    Sola lettura, su richiesta: nessuna chiamata finche' non si digita il comando, e la chiave
    read-only non puo' modificare i monitor nemmeno per sbaglio."""
    if not UPTIMEROBOT_API_KEY:
        invia_messaggio_telegram(
            "UptimeRobot non è collegato.\n\n"
            "Serve la variabile d'ambiente UPTIMEROBOT_API_KEY su Render "
            "(Environment → Add Environment Variable), con una chiave *read-only* presa da "
            "UptimeRobot → Integrations & API → API.\n\n"
            "Senza, il resto del bot funziona normalmente: cambia solo che questo comando non ha "
            "niente da leggere.", chat_id=chat_id)
        return

    try:
        # API v2: form-encoded, non JSON. custom_uptime_ratios chiede le percentuali di 1, 7 e 30
        # giorni in un colpo solo; logs=1 porta gli ultimi eventi, da cui si ricava l'ultimo
        # disservizio senza una seconda chiamata.
        risposta = requests.post(
            "https://api.uptimerobot.com/v2/getMonitors",
            data={"api_key": UPTIMEROBOT_API_KEY, "format": "json",
                  "custom_uptime_ratios": "1-7-30", "logs": "1", "logs_limit": "5"},
            timeout=15)
        dati = risposta.json()
    except Exception as e:
        log(f"Errore chiamata UptimeRobot: {e}")
        invia_messaggio_telegram(
            f"UptimeRobot non ha risposto: {e}\n\n"
            "È un problema di questa chiamata, non del bot: il monitoraggio esterno continua "
            "comunque a girare e ad avvisare via email.", chat_id=chat_id)
        return

    # "stat" e' il verdetto della API: qualunque cosa diversa da "ok" porta con se' il motivo,
    # e riportarlo com'e' evita di far indovinare se sia la chiave sbagliata o altro.
    if dati.get("stat") != "ok":
        errore = (dati.get("error") or {}).get("message", "motivo non specificato")
        invia_messaggio_telegram(
            f"UptimeRobot ha rifiutato la richiesta: {errore}\n\n"
            "Se parla della chiave, va rigenerata da UptimeRobot → Integrations & API e "
            "riaggiornata su Render.", chat_id=chat_id)
        return

    monitor = dati.get("monitors") or []
    if not monitor:
        invia_messaggio_telegram(
            "UptimeRobot risponde, ma su questo account non c'è nessun monitor configurato.",
            chat_id=chat_id)
        return

    righe = ["📡 *Disponibilità vista da fuori* (UptimeRobot)"]
    for m in monitor:
        emoji, descrizione = STATI_UPTIMEROBOT.get(m.get("status"), ("❔", "stato sconosciuto"))
        righe.append(f"\n{emoji} *{m.get('friendly_name', 'senza nome')}* — {descrizione}")

        # Le tre percentuali arrivano in un'unica stringa "99.9-99.8-99.7" nell'ordine chiesto
        # sopra (1-7-30 giorni). Se l'account non le fornisce si salta la riga invece di stampare
        # numeri inventati.
        percentuali = str(m.get("custom_uptime_ratio", "")).split("-")
        if len(percentuali) == 3:
            try:
                g1, g7, g30 = (float(p) for p in percentuali)
                righe.append(f"   24h {g1:.2f}% · 7g {g7:.2f}% · 30g {g30:.2f}%")
            except ValueError:
                pass

        # Ultimo disservizio: nei log type=1 significa "down". Serve a distinguere "100% da
        # sempre" da "100% nelle ultime 24h ma ieri e' stato giu' un'ora".
        cadute = [l for l in (m.get("logs") or []) if l.get("type") == 1]
        if cadute:
            ultima = cadute[0]
            quando = datetime.datetime.fromtimestamp(
                ultima.get("datetime", 0), ZoneInfo("Europe/Rome")).strftime("%d/%m %H:%M")
            righe.append(f"   Ultimo disservizio: {quando} per {_durata_leggibile(ultima.get('duration'))}")
        else:
            righe.append("   Nessun disservizio negli ultimi eventi registrati")

    righe.append("\n_Controllo ogni 5 minuti dall'esterno: è il solo modo per sapere se il bot è "
                 "stato irraggiungibile: mentre era giù non stava girando per accorgersene._")
    invia_messaggio_telegram("\n".join(righe), chat_id=chat_id)


def cmd_funzioni(chat_id):
    """Panoramica del bot, ma spiegata (non solo un elenco di nomi con un'etichetta
    stabile/in validazione): per ogni voce, cosa fa concretamente e perché. Mandata in 2
    messaggi separati per stare comodamente sotto il limite di 4096 caratteri di Telegram.
    Testo statico, da aggiornare a mano quando cambia qualcosa di rilevante."""
    parte1 = (
        "🟢 COSA FA IL BOT OGGI, DAVVERO ATTIVO\n\n"
        "Monitoraggio live\n"
        "Segue le partite dei campionati che segui e ti avvisa quando succede qualcosa che "
        "conta (un gol, un cartellino rosso, un rigore, un recupero lungo) - così non devi "
        "controllare tu ogni partita a mano.\n\n"
        "Solo le partite in cui comanda qualcuno\n"
        "Per gli aggiornamenti che non sono gol o eventi, in chat arrivano ora solo le partite "
        "in cui una squadra sta davvero facendo la partita (almeno il 65% dell'azione: tiri, "
        "tiri in porta, corner, tiri in area, pesati). Una partita combattuta con tanti tiri da "
        "una parte e dall'altra non arriva più: c'è tanto gioco ma non dice da che parte stare, "
        "ed era il grosso del rumore. I gol restano notificati sempre, in qualunque partita.\n\n"
        "Grafico momentum\n"
        "Il bottone \"📈 Momentum\" sotto ogni notifica ti fa vedere come sta andando la "
        "pressione della partita minuto per minuto (tiri, tiri in porta, corner, xG) - utile "
        "per capire se una squadra sta davvero spingendo adesso o se il momento buono è già "
        "passato.\n\n"
        "Quote 1X2\n"
        "Ogni notifica include la quota di apertura (1-X-2) di Bet365, presa prima dell'inizio "
        "della partita - vedi subito come il mercato valutava la partita, senza cercarla altrove.\n\n"
        "Preferiti\n"
        "Puoi seguire una partita con più attenzione (notifiche più frequenti, grafico più "
        "ricco) aggiungendola ai preferiti a mano, oppure ci pensa da solo il bot quando una "
        "partita si sblocca presto restando aperta (due gol entro il 25' con al massimo un gol "
        "di scarto). Le notifiche dei preferiti arrivano in un canale Telegram separato, se lo "
        "hai configurato.\n\n"
        "Una scheda sola per partita nel canale preferiti\n"
        "Un preferito veniva ricontrollato ogni minuto e ogni volta era un messaggio nuovo: "
        "decine di foto quasi identiche impilate. Ora gli aggiornamenti di routine riscrivono "
        "la scheda precedente, così in cima al canale c'è sempre una sola scheda aggiornata per "
        "partita. Restano messaggi nuovi - quelli che fanno suonare il telefono - i gol, i "
        "rossi, i rigori e i recuperi, e si riparte da una scheda nuova ad ogni quarto d'ora, "
        "così il filo della partita resta comunque leggibile a ritroso.\n\n"
        "Controllo del bot\n"
        "/stop ferma tutto (nessuna chiamata, nessuna notifica) quando sai che non stai "
        "seguendo il trading, /riprendi lo riattiva. C'è anche una pausa automatica nelle ore "
        "in cui hai detto di non essere operativo (12:00-23:30 di default): in quelle ore il "
        "bot continua a raccogliere dati dietro le quinte ma non ti manda notifiche.\n\n"
        "Comandi di analisi manuale\n"
        "/live (partite live), /piano (programma di oggi), /status <squadra> e "
        "/momentum <squadra> (info su una partita specifica)."
    )
    parte2 = (
        "🟡 COSA STA SUCCEDENDO DIETRO LE QUINTE (non lo vedi ancora in chat)\n\n"
        "Il bot sta silenziosamente raccogliendo dati per rispondere a una domanda precisa: "
        "\"le statistiche in diretta di una partita (tiri, pressione) dicono qualcosa di "
        "utile che la quota del bookmaker già non dica da sola?\" Per ogni partita seguita, "
        "ogni 15 minuti salva la quota (già ripulita dal margine del bookmaker) insieme alle "
        "statistiche del momento, e a fine partita salva anche il risultato vero. Zero "
        "output visibile per te oggi: è solo raccolta prove.\n\n"
        "Cosa succede dopo:\n"
        "- 21 agosto: controllo quanti dati si sono accumulati finora, solo per vedere se "
        "siamo in linea con i tempi - nessuna decisione presa in quel momento.\n"
        "- 31 agosto: se ci sono abbastanza partite raccolte, faccio un test statistico vero "
        "per rispondere alla domanda sopra. Se il test conferma che le statistiche live "
        "aggiungono davvero qualcosa, si passa a costruire un indicatore visibile in chat. "
        "Se il test dice che non aggiungono nulla, si scarta questa idea specifica e si "
        "guarda tra le altre già valutate (arbitraggio, confronto multi-bookmaker, modelli "
        "xG, e più di dieci altre).\n\n"
        "/shadowlog ti fa vedere in ogni momento a che punto è la raccolta.\n\n"
        "Stessa idea per sei condizioni di gioco (assedio, fascia calda, rimonta, concretezza, "
        "xG per tiro, qualità): non sono più comandi da lanciare a mano, ma il bot continua a "
        "valutarle da solo ogni 15 minuti su ogni partita seguita e a registrare quali scattano, "
        "così più avanti si può controllare con dati reali se scattare anticipa davvero un gol. "
        "/shadowlogstrategie mostra a che punto è questa raccolta.\n\n"
        "Terza raccolta, sempre silenziosa: una seconda porta d'ingresso ai preferiti basata sul "
        "dominio, per prendere anche le partite in cui una squadra assedia senza segnare (un "
        "assedio sullo 0-0 oggi non entra nei preferiti da nessuna parte). La regola è già "
        "scritta e gira ad ogni ciclo, ma NON promuove niente: registra soltanto a che quota di "
        "dominio arriva ogni partita, perché le soglie di partenza (78% dell'azione, con almeno "
        "il doppio del gioco minimo, per tre controlli di fila) sono stime a occhio. Quando i "
        "dati raccolti diranno quali sono i valori veri, si accende con un interruttore in "
        "configurazione. /shadowlogdominio mostra i numeri raccolti finora.\n\n"
        "Il bot ora si controlla anche da solo: ogni 30 minuti verifica che partite "
        "tracciate, statistiche e i due shadow-log sopra stiano davvero funzionando, e se "
        "trova un problema te lo scrive qui in chat da solo (oltre che nei log) - non devi "
        "controllare nulla a mano né lanciare comandi apposta.\n\n"
        "📋 ULTIME NOVITÀ (ultimi giorni)\n"
        "Le notifiche generali passano solo per le partite in cui comanda qualcuno, il canale "
        "preferiti aggiorna una scheda sola per partita invece di impilare messaggi, raccolta "
        "dati sulla rotta dominio verso i preferiti (spenta, sola osservazione). "
        "Quote 1X2 nelle notifiche, pausa automatica per fascia oraria, monitoraggio 24/7 "
        "anche fuori orario, il bottone Momentum ora aggiorna la notifica esistente invece "
        "di mandarne una nuova, corretto un bug che perdeva il risultato di partite finite "
        "durante la pausa, raccolta dati automatica in background sull'efficacia di sei "
        "condizioni di gioco (tolte come comandi live), controllo automatico della pipeline "
        "dati con avviso in chat se qualcosa si inceppa."
    )
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": parte1}, timeout=5)
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": parte2}, timeout=5)


# Abbreviazioni comuni delle squadre di calcio: la chiave e' la forma corta (lowercase,
# senza accenti), il valore e' l'espansione. Serve a matchare "Man Utd" con "Manchester
# United", "Ath Bilbao" con "Athletic Bilbao", ecc. I prefissi/suffissi societari
# (FC, AC, SC, ...) sono mappati a stringa vuota cosi' vengono ignorati nel match:
# l'utente puo' chiedere "milan" e trovare "AC Milan".
ABBREVIAZIONI_SQUADRE = {
    "utd": "united",
    "ath": "athletic",
    "atl": "atletico",
    "wolves": "wolverhampton",
    "brgh": "brighton",
    "psg": "paris saint germain",
    "juve": "juventus",
    "fc": "",
    "ac": "",
    "cf": "",
    "sc": "",
    "sk": "",
    "afc": "",
    "cfc": "",
    "fk": "",
}


def _normalizza_nome_squadra(testo):
    """Toglie accenti (unicode NFKD), converte in lowercase e comprime gli spazi.
    Cosi' "Málaga" e "malaga" diventano la stessa stringa, e la query utente non deve
    replicare esattamente i caratteri accentati che l'API restituisce."""
    if not testo:
        return ""
    testo_norm = unicodedata.normalize("NFKD", testo)
    testo_norm = "".join(c for c in testo_norm if not unicodedata.combining(c))
    return " ".join(testo_norm.lower().split())


def _sigla_squadra(nome_normalizzato):
    """Prime lettere di ogni parola (split su spazi/trattini/punti). 'paris saint-germain'
    -> 'psg'. Serve a matchare la sigla comune (PSG, RCD, ecc.) col nome per esteso."""
    parole = re.split(r"[\s\-\.]+", nome_normalizzato)
    return "".join(p[0] for p in parole if p)


def _nomi_squadra_matchano(query, nome_squadra):
    """Ritorna True se la query dell'utente identifica la squadra. Quattro strategie
    provate in cascata (dalla piu' precisa alla piu' fuzzy):
    1) sottostringa in entrambe le direzioni sui nomi normalizzati (accenti eliminati)
    2) sigla: query == prime lettere delle parole del nome (es. 'psg' vs 'Paris Saint-Germain')
    3) token-based: ogni token della query (>=2 char) e' prefisso di un token del nome
       (es. 'man city' vs 'manchester city')
    4) alias noti: sostituisce 'utd'->'united', 'ath'->'athletic', droppa 'fc'/'ac',
       poi ritenta il substring match

    Le strategie 2 e 3 richiedono almeno 2 caratteri per token per evitare match troppo
    permissivi (una singola lettera matcherebbe qualunque cosa). Il comportamento della
    strategia 1 e' invece identico a prima (case-insensitive) piu' l'accento-agnosticita'."""
    if not query or not nome_squadra:
        return False
    q = _normalizza_nome_squadra(query)
    n = _normalizza_nome_squadra(nome_squadra)
    if not q or not n:
        return False
    if q in n or n in q:
        return True
    if len(q.replace(" ", "")) >= 2 and q.replace(" ", "") == _sigla_squadra(n):
        return True
    tokens_q = [t for t in q.split() if len(t) >= 2]
    tokens_n = n.split()
    if tokens_q and all(
        any(tn.startswith(tq) for tn in tokens_n)
        for tq in tokens_q
    ):
        return True
    espansi_q = " ".join(
        ABBREVIAZIONI_SQUADRE.get(t, t) for t in q.split()
    )
    espansi_q = " ".join(espansi_q.split())
    if espansi_q and espansi_q != q:
        if espansi_q in n or n in espansi_q:
            return True
        # Riprova anche il token-prefix sull'espansione: "man utd" -> "man united",
        # cosi' matcha "Manchester United" (dove "united" == "united" e "man" prefisso di
        # "manchester"). Senza questo, l'alias 'utd'->'united' fallirebbe perche' "man
        # united" non e' sottostringa di "manchester united".
        tokens_esp = [t for t in espansi_q.split() if len(t) >= 2]
        if tokens_esp and all(
            any(tn.startswith(te) for tn in tokens_n)
            for te in tokens_esp
        ):
            return True
    return False


def cmd_status(chat_id, query):
    """/status <squadra>: info live sulla partita trovata, statistiche totali casa/trasferta,
    intensità (ultimi 15 min) calcolata solo per questa partita — funziona anche su partite fuori
    whitelist (es. una coppa) accumulando uno storico dedicato (STATUS_HISTORY) ad ogni chiamata —
    e, se disponibile, la distribuzione storica gol per fascia di minuto delle due squadre."""
    partite_cmd = get_partite_live()
    trovate = []
    for f in partite_cmd:
        home = f.get("teams", {}).get("home", {}).get("name", "")
        away = f.get("teams", {}).get("away", {}).get("name", "")
        if _nomi_squadra_matchano(query, home) or _nomi_squadra_matchano(query, away):
            trovate.append(f)
    if not trovate:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": f"Nessuna partita live trovata per '{query}'", "parse_mode": "Markdown"}, timeout=5)
        return
    for f in trovate:
        # Isolamento errori per partita: se cerchi "man" e ci sono sia City che United,
        # un fallimento su una (API 5xx, timeout del grafico, sendPhoto rifiutato) non
        # deve piu' interrompere il ciclo prima che l'altra venga inviata. In caso di
        # errore mando un avviso breve identificando la partita coinvolta, cosi' chi
        # ha chiesto /status vede che una e' saltata invece di ricevere una risposta
        # muta senza sapere perche' manca.
        try:
            fid = f["fixture"]["id"]
            home = f["teams"]["home"]["name"]
            away = f["teams"]["away"]["name"]
            league = f.get("league", {}).get("name", "")
            minuto = f["fixture"]["status"].get("elapsed") or 0
            score_h = f["goals"]["home"] or 0
            score_a = f["goals"]["away"] or 0

            stats = get_statistiche_partita(fid)
            stats_text = ""
            current_stats = None
            if stats and len(stats) >= 2:
                sh = stats[0].get("statistics", [])
                sa = stats[1].get("statistics", [])
                current_stats = estrai_current_stats(sh, sa)
                tc, to = current_stats["Tiri totali"]
                tp, tpo = current_stats["Tiri in porta"]
                cc, co = current_stats["Corner"]
                ta, tao = current_stats["Tiri in area"]
                stats_text = f"\nStats totali: Tiri {tc}-{to} | Porta {tp}-{tpo} | Corner {cc}-{co} | Area {ta}-{tao}"
                # Chiedere /status su una partita col feed bloccato dava i numeri vecchi senza dirlo:
                # e' come il bot ha risposto "Tiri 3-0" su Venezia-Lecce mentre erano 7-1. Sola
                # lettura (registra=False): consultare non deve spostare il conteggio del ciclo live.
                congelato_status, minuti_fermo_status = aggiorna_feed_congelato(
                    fid, stats, minuto, registra=False)
                if congelato_status:
                    stats_text += testo_feed_congelato(minuti_fermo_status, current_stats)

            intensita_text = ""
            if current_stats:
                history = STATUS_HISTORY.get(fid, [])
                history.append({"timestamp": time.time(), "minuto": minuto, "stats": current_stats})
                history = [h for h in history if time.time() - h["timestamp"] <= 1200]
                STATUS_HISTORY[fid] = history

                delta_stats, is_real = _calcola_delta_15min_da_storico(history, current_stats, minuto)
                if is_real:
                    punteggio = calcola_indice_intensita(delta_stats)
                    motivazioni = descrivi_motivazioni_intensita(delta_stats)
                    d_tiri = delta_stats.get("Tiri totali", (0, 0))
                    intensita_text = (
                        f"\n\nIntensità (ultimi 15 min) di questa partita: {punteggio:.1f} pt\n"
                        f"Casa {d_tiri[0]} - {d_tiri[1]} Fuori | {motivazioni}"
                    )
                else:
                    intensita_text = "\n\nIntensità: primo rilevamento per questa partita, richiama /status tra qualche minuto per un dato reale sul ritmo."

            events = fetch_fixture_events(fid)
            goals = extract_goals(events)
            goals = goals_coerenti_con_risultato(goals, home, away, score_h, score_a)
            # Stesso avviso della notifica: se i gol superano i tiri in porta, i numeri mostrati sopra
            # sono indietro sul risultato, e chiedere /status deve dirlo invece di darli per buoni.
            # Qui gli eventi arrivano dopo stats_text, quindi la riga si aggiunge in coda.
            _indietro_status, riga_ritardo_status = statistiche_indietro_sul_punteggio(
                current_stats, score_h, score_a, events, home, away)
            if _indietro_status:
                stats_text += riga_ritardo_status.rstrip()
            last_text = ""
            if goals:
                last_text = f"\nUltimo gol: {goals[-1]['minute']}' ({goals[-1]['player']})"

            msg_text = f"{home} vs {away}\n{league}\n{minuto}' | {score_h}-{score_a}{last_text}{stats_text}{intensita_text}"

            squadra_casa = trova_squadra_in_storico(home)
            squadra_trasferta = trova_squadra_in_storico(away)
            foto_path = None
            if (squadra_casa and squadra_casa["casa"]["partite"] > 0
                    and squadra_trasferta and squadra_trasferta["trasferta"]["partite"] > 0):
                foto_path = genera_grafico_minutaggi(
                    squadra_casa["nome"], squadra_casa["casa"],
                    squadra_trasferta["nome"], squadra_trasferta["trasferta"]
                )

            if foto_path and os.path.exists(foto_path):
                try:
                    with open(foto_path, 'rb') as photo:
                        requests.post(
                            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                            data={"chat_id": chat_id, "caption": msg_text},
                            files={"photo": photo}, timeout=15)
                except Exception as e:
                    log(f"Errore invio grafico /status: {e}")
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={"chat_id": chat_id, "text": msg_text, "parse_mode": "Markdown"}, timeout=5)
                finally:
                    try:
                        os.remove(foto_path)
                    except Exception:
                        pass
            else:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": msg_text, "parse_mode": "Markdown"}, timeout=5)
        except Exception as e:
            squadre_id = (
                f"{f.get('teams', {}).get('home', {}).get('name', '?')} vs "
                f"{f.get('teams', {}).get('away', {}).get('name', '?')}"
            )
            log(f"Errore /status per {squadre_id}: {e}\n{traceback.format_exc()}")
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": f"Errore nel recupero di {squadre_id}. Le altre partite trovate proseguono."
                    }, timeout=5)
            except Exception:
                pass


def spiega_momentum_insufficiente(history):
    """Motivo specifico per cui genera_grafico_momentum non ha prodotto un grafico, invece del
    messaggio generico che elencava tutte le cause possibili senza dire quale si applica
    davvero in questo caso - così chi legge sa se deve aspettare, e quanto, oppure se quella
    lega/partita semplicemente non ha statistiche."""
    n = len(history)
    if n == 0:
        return ("Il bot non ha ancora nessuna statistica per questa partita: monitoraggio appena "
                "iniziato, oppure questa lega/incontro non ha statistiche disponibili dall'API "
                "(succede per alcuni campionati minori - è lo stesso motivo per cui una partita "
                "così non arriva in chat).")
    if n < MOMENTUM_MIN_STORICO:
        mancanti = MOMENTUM_MIN_STORICO - n
        return (f"Solo {n} rilevazion{'e' if n == 1 else 'i'} su {MOMENTUM_MIN_STORICO} necessarie: "
                f"mancano circa {mancanti * 3} minuti (se il bot resta acceso senza riavvii nel mezzo). Riprova più tardi.")
    return ("Le statistiche non sono mai cambiate da quando il bot la monitora (partita ferma, "
            "o l'API non aggiorna i dati per questa lega).")


def invia_momentum_partita(chat_id, fid, home, away, league, minuto, score_h, score_a):
    """Genera e invia il grafico momentum per una partita già identificata (fixture_id noto),
    usato da /momentum <squadra> (dopo la ricerca per nome) - come messaggio nuovo, perché qui
    non c'è nessuna notifica precedente a cui agganciarlo (vedi invece cmd_momentum_da_bottone,
    che modifica sul posto la notifica da cui si è cliccato)."""
    stato = stato_partite.get(fid, {})
    history = stato.get("history", [])
    foto_path = genera_grafico_momentum(fid, home, away, history, stato.get("goals"), stato.get("rigori"), stato.get("cartellini_rossi"))
    msg_text = f"{home} {score_h}-{score_a} {away}\n{league} | {minuto}'{nota_copertura_momentum(history)}"

    if foto_path and os.path.exists(foto_path):
        try:
            with open(foto_path, 'rb') as photo:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                    data={"chat_id": chat_id, "caption": msg_text},
                    files={"photo": photo}, timeout=15)
        except Exception as e:
            log(f"Errore invio grafico momentum: {e}")
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": msg_text, "parse_mode": "Markdown"}, timeout=5)
        finally:
            try:
                os.remove(foto_path)
            except Exception:
                pass
    else:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": f"{msg_text}\n\nMomentum non disponibile: {spiega_momentum_insufficiente(history)}"
            }, timeout=5)


def cmd_momentum(chat_id, query):
    """/momentum <squadra>: grafico dell'andamento della pressione durante la partita, calcolato
    sullo storico accumulato dal bot per quella partita. A differenza di /status, funziona solo
    sulle partite monitorate automaticamente (whitelist), perché serve lo storico dell'intera
    partita che solo il ciclo principale accumula in stato_partite."""
    partite_cmd = get_partite_live()
    trovate = []
    for f in partite_cmd:
        home = f.get("teams", {}).get("home", {}).get("name", "")
        away = f.get("teams", {}).get("away", {}).get("name", "")
        if _nomi_squadra_matchano(query, home) or _nomi_squadra_matchano(query, away):
            trovate.append(f)
    if not trovate:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": f"Nessuna partita live trovata per '{query}'", "parse_mode": "Markdown"}, timeout=5)
        return

    for f in trovate:
        # Stesso isolamento errori di cmd_status: se cerchi "man" e ci sono sia City
        # che United, un fallimento su una (API stats vuote, sendPhoto rifiutato,
        # grafico non generato) non deve interrompere il ciclo prima che l'altra
        # venga inviata.
        try:
            fid = f["fixture"]["id"]
            home = f["teams"]["home"]["name"]
            away = f["teams"]["away"]["name"]
            league = f.get("league", {}).get("name", "")
            minuto = f["fixture"]["status"].get("elapsed") or 0
            score_h = f["goals"]["home"] or 0
            score_a = f["goals"]["away"] or 0
            invia_momentum_partita(chat_id, fid, home, away, league, minuto, score_h, score_a)
        except Exception as e:
            squadre_id = (
                f"{f.get('teams', {}).get('home', {}).get('name', '?')} vs "
                f"{f.get('teams', {}).get('away', {}).get('name', '?')}"
            )
            log(f"Errore /momentum per {squadre_id}: {e}\n{traceback.format_exc()}")
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": f"Errore nel momentum di {squadre_id}. Le altre partite trovate proseguono."
                    }, timeout=5)
            except Exception:
                pass


def _invia_reply_con_fallback(chat_id, text, reply_to_message_id, contesto):
    """Manda `text` in risposta (reply_parameters) a reply_to_message_id; se Telegram rifiuta il
    collegamento (messaggio troppo vecchio, cancellato, o un altro limite dell'API) la sendMessage
    fallisce PER INTERO - non solo la parte "in risposta a" - e senza controllare lo status code
    il messaggio va perso in silenzio: nessuna eccezione, nessun log, niente in chat (esattamente
    il sintomo "clicco Momentum e non succede assolutamente nulla"). Qui si ricontrolla e, se il
    collegamento fallisce, si rimanda lo stesso testo come messaggio normale invece di perderlo."""
    risposta = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text, "reply_parameters": {"message_id": reply_to_message_id}},
        timeout=5)
    if risposta.status_code == 200:
        return
    log(f"[{contesto}] sendMessage con reply_parameters fallita (HTTP {risposta.status_code}): "
        f"{risposta.text[:300]} - reinvio senza collegamento")
    risposta2 = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text}, timeout=5)
    if risposta2.status_code != 200:
        log(f"[{contesto}] sendMessage anche senza reply_parameters fallita (HTTP {risposta2.status_code}): {risposta2.text[:300]}")


def cmd_momentum_da_bottone(chat_id, fixture_id, message_id):
    """Bottone "📈 Momentum" cliccato su una notifica: invece di mandare un grafico come messaggio
    a parte, sostituisce la FOTO DELLA NOTIFICA STESSA (editMessageMedia) con la versione
    combinata barre+momentum in un'unica immagine (genera_grafico_combinato) - così il grafico
    compare esattamente nel messaggio su cui si è cliccato, non altrove in chat, e le barre con
    il totale cumulativo restano visibili invece di sparire sostituite dal solo andamento momentum.
    I totali per le barre riusano l'ultimo snapshot già in history (stato_partite), NON una nuova
    chiamata a get_statistiche_partita: la versione combinata era stata tolta da qui proprio per
    quella chiamata extra, ma il dato è già in memoria (al più qualche minuto vecchio, come ovunque
    altrove nel bot) quindi si evita il costo mantenendo comunque le barre. Se lo storico non
    basta ancora, lascia la notifica invariata (niente da mostrare di meglio) e risponde solo con
    la spiegazione.

    Su una partita con tante notifiche ravvicinate (preferiti, partite movimentate) l'edit da solo
    è facile da perdere in mezzo a messaggi quasi identici: in coda si manda anche una piccola
    conferma IN RISPOSTA (reply_parameters) a questa stessa notifica, cosi' Telegram mostra la
    citazione/collegamento e un tap ci salta dritto sopra, anche se è più in alto nella chat."""
    stato = stato_partite.get(fixture_id)
    if not stato:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Questa partita non è più monitorata (probabilmente è terminata)."}, timeout=5)
        return

    history = stato.get("history", [])
    home, away = stato.get("home", "?"), stato.get("away", "?")
    stats_totali = history[-1]["stats"] if history else {
        "Tiri totali": (0, 0), "Tiri in porta": (0, 0), "Corner": (0, 0), "Tiri in area": (0, 0)}

    foto_path = genera_grafico_combinato(
        fixture_id, home, away, stats_totali, history,
        stato.get("goals"), stato.get("rigori"), stato.get("cartellini_rossi"),
        stato.get("recupero_1h"), stato.get("recupero_2h"))

    if not foto_path:
        _invia_reply_con_fallback(
            chat_id, f"Momentum non disponibile: {spiega_momentum_insufficiente(history)}",
            message_id, "momentum non disponibile")
        return

    try:
        with open(foto_path, 'rb') as photo:
            # Riusa la didascalia esatta già mandata con questa notifica (quote, statistiche,
            # gol, tutto quello che c'era) - si perde solo se il bot è stato riavviato tra
            # l'invio e il click (stato in memoria perso), nel qual caso si ricostruisce una
            # versione minima piuttosto che lasciare senza didascalia.
            # Chiave stringa: stato_partite finisce su disco in JSON, che le chiavi le forza a
            # stringa, e carica_stato_partite() riconverte solo quelle di primo livello (i
            # fixture_id). Scritta e letta come intero, la didascalia risultava introvabile dopo
            # ogni riavvio del bot e la notifica perdeva quote, statistiche e gol appena si
            # cliccava Momentum. Si vedeva soprattutto nel canale preferiti, dove le notifiche
            # arrivano ogni 60s ed e' li' che si clicca Momentum piu' spesso.
            caption = stato.get("didascalie_notifiche", {}).get(str(message_id))
            if not caption:
                caption = (f"{home} {stato.get('score_home', '?')}-{stato.get('score_away', '?')} {away}\n"
                           f"{formatta_lega(stato.get('league', '?'), stato.get('league_country', ''))} | "
                           f"{stato.get('last_minute', '?')}'{nota_copertura_momentum(history)}")
            media = {"type": "photo", "media": "attach://photo", "caption": caption}
            risposta_edit = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageMedia",
                data={"chat_id": chat_id, "message_id": message_id, "media": json.dumps(media)},
                files={"photo": photo}, timeout=15)
    except Exception as e:
        log(f"Errore aggiornamento notifica con grafico momentum: {e}")
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Errore nel generare il grafico momentum, riprova."}, timeout=5)
        return
    finally:
        try:
            os.remove(foto_path)
        except Exception:
            pass

    if risposta_edit.status_code != 200:
        # Non si tocca il bottone se l'edit non è davvero riuscito (es. notifica troppo vecchia,
        # rate-limit): altrimenti il bottone sparirebbe senza che il grafico sia mai comparso.
        log(f"editMessageMedia fallita per la notifica momentum: HTTP {risposta_edit.status_code} - {risposta_edit.text[:300]}")
        _invia_reply_con_fallback(
            chat_id, "Errore nell'aggiornare la notifica con il grafico momentum, riprova.",
            message_id, "editMessageMedia fallita")
        return

    # Questa notifica esce dal giro degli aggiornamenti in place (vedi message_id_live in
    # processa_partita): l'utente ha appena chiesto di vederci dentro il grafico momentum, e il
    # ciclo successivo - se cadesse ancora nello stesso blocco di 15 minuti - la riscriverebbe
    # sopra facendo sparire proprio quello che era stato chiesto. Il prossimo aggiornamento apre
    # una scheda nuova e lascia intatta questa.
    if stato.get("message_id_live") == message_id:
        stato.pop("message_id_live", None)
        stato.pop("blocco_messaggio_live", None)
        stato.pop("chat_messaggio_live", None)

    # Da qui in avanti questa partita mostra sempre il momentum: il click dice "di questa partita
    # voglio vedere l'andamento", non "voglio vederlo una volta sola". Senza, la notifica
    # successiva tornava alle sole barre e il grafico andava richiesto da capo ogni volta.
    stato["momentum_richiesto"] = True

    # Il bottone non serve più: il grafico è già agganciato alla notifica, ricliccarlo
    # rigenererebbe la stessa immagine inutilmente.
    is_fav = str(fixture_id) in FAVORITE_MATCHES
    is_sil = str(fixture_id) in SILENCED_MATCHES
    nuova_keyboard = get_notification_keyboard(fixture_id, is_fav, is_sil, mostra_momentum=False)
    if nuova_keyboard:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
            json={"chat_id": chat_id, "message_id": message_id, "reply_markup": json.dumps(nuova_keyboard)}, timeout=5)

    # Collegamento visibile: su una partita con tante notifiche simili impilate, questa citazione
    # (Telegram mostra un'anteprima della notifica originale, tap per saltarci sopra) è l'unico
    # modo per far capire subito QUALE messaggio è stato appena aggiornato con il grafico.
    _invia_reply_con_fallback(
        chat_id, "📈 Grafico momentum aggiornato qui sopra ⬆️", message_id, "conferma momentum")


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


# =============================================================================
# DOMINIO: chi sta facendo la partita, e se il risultato lo rispecchia
# =============================================================================
# L'indice di intensità qui sopra SOMMA le due squadre: dice quanto si gioca, non chi comanda.
# Un 10-9 di tiri è intensissimo ma equilibrato, e non indica da che parte stare; un 9-1 è la
# stessa quantità di gioco ma tutta da una parte sola. Per il trading serve il secondo.
#
# Il segnale che conta davvero non è però il dominio in sé: è la DIVERGENZA fra dominio e
# risultato. Una squadra che domina e vince 3-0 non offre niente, il mercato l'ha già capito.
# Una che domina e non segna, o peggio sta perdendo, è la situazione in cui il punteggio racconta
# una partita diversa da quella che si sta giocando.
PESO_DOMINIO_TIRI = 1
PESO_DOMINIO_PORTA = 3    # un tiro in porta pesa quanto tre tiri qualunque
PESO_DOMINIO_CORNER = 1
PESO_DOMINIO_AREA = 2     # tiri dentro l'area: la statistica più vicina a un'occasione vera
# Sotto questa quota le due squadre si equivalgono abbastanza da non parlare di dominio.
SOGLIA_QUOTA_DOMINIO = 65
# Con pochissimo gioco le percentuali impazziscono (1 tiro a 0 fa "100%"): serve un minimo di
# materiale prima di dare un verdetto.
VOLUME_MINIMO_DOMINIO = 8


def _peso_offensivo(stats, lato):
    def v(chiave):
        return stats.get(chiave, (0, 0))[lato] or 0
    return (v("Tiri totali") * PESO_DOMINIO_TIRI
            + v("Tiri in porta") * PESO_DOMINIO_PORTA
            + v("Corner") * PESO_DOMINIO_CORNER
            + v("Tiri in area") * PESO_DOMINIO_AREA)


def calcola_dominio(current_stats, score_home, score_away):
    """Ritorna chi domina, con quanta parte dell'azione e cosa dice il risultato.

    None quando non c'è un dominio da dichiarare: statistiche assenti, troppo poco gioco per
    giudicare, oppure squadre sostanzialmente pari."""
    if not current_stats:
        return None
    peso_casa = _peso_offensivo(current_stats, 0)
    peso_ospite = _peso_offensivo(current_stats, 1)
    totale = peso_casa + peso_ospite
    if totale < VOLUME_MINIMO_DOMINIO:
        return None

    lato = 0 if peso_casa >= peso_ospite else 1
    quota = round(max(peso_casa, peso_ospite) / totale * 100)
    if quota < SOGLIA_QUOTA_DOMINIO:
        return None

    gol_pro = score_home if lato == 0 else score_away
    gol_contro = score_away if lato == 0 else score_home
    if gol_pro > gol_contro:
        situazione, priorita = "avanti", 2      # il risultato rispecchia il campo: niente da dire
    elif gol_pro == gol_contro:
        situazione, priorita = "bloccata", 1    # domina e non segna
    else:
        situazione, priorita = "sotto", 0       # domina e sta perdendo: divergenza massima
    return {"lato": lato, "quota": quota, "situazione": situazione, "priorita": priorita}


def favorita_che_non_vince(fixture_id, score_home, score_away, minuto):
    """La squadra data favorita dal mercato pre-partita non sta vincendo.

    Ritorna (lato, probabilita) - es. ("casa", 0.78) - oppure None. Il lato serve solo al testo del
    motivo: chi chiama deve sapere DI CHI si parla, altrimenti "favorita al 78%" non dice quale.

    Guarda solo le quote iniziali, mai quelle live: il punto e' il confronto fra quello che il
    mercato si aspettava PRIMA e quello che sta succedendo adesso. Una quota live si e' gia'
    aggiornata al risultato, e confrontarla col risultato non direbbe piu' niente."""
    if not FAVORITA_IN_DIFFICOLTA_ATTIVO:
        return None
    if minuto is None or minuto < MINUTO_MINIMO_FAVORITA_IN_DIFFICOLTA:
        return None
    if score_home is None or score_away is None:
        return None
    # quote_1x2_per_fixture ritorna il dizionario, oppure False quando il bookmaker non le ha
    # pubblicate, oppure None se la partita non e' nel piano: solo il primo caso e' utilizzabile.
    quote = quote_1x2_per_fixture(fixture_id)
    if not isinstance(quote, dict):
        return None
    probabilita = calcola_probabilita_no_vig(quote)
    if not probabilita:
        return None
    if probabilita["casa"] >= SOGLIA_PROB_FAVORITA and score_home <= score_away:
        return "casa", probabilita["casa"]
    if probabilita["ospite"] >= SOGLIA_PROB_FAVORITA and score_away <= score_home:
        return "ospite", probabilita["ospite"]
    return None


def motivo_assenza_dominio(current_stats):
    """Perche' calcola_dominio() non ha dichiarato un dominio: il numero che e' mancato.

    calcola_dominio ritorna None per tre ragioni diverse - statistiche assenti, troppo poco gioco,
    squadre pari - e nei log finivano tutte e tre nella stessa riga. E' il motivo di skip piu'
    frequente da quando il gate e' attivo, quindi era anche quello che conveniva meno lasciare
    muto: "squadre pari al 58%" e "volume 5, ne servono 8" portano a due decisioni diverse.

    Usa lo stesso _peso_offensivo di calcola_dominio, non un conteggio parallelo: due definizioni
    di "quanto si e' giocato" finirebbero prima o poi per divergere."""
    if not current_stats:
        return "statistiche assenti"
    peso_casa = _peso_offensivo(current_stats, 0)
    peso_ospite = _peso_offensivo(current_stats, 1)
    totale = peso_casa + peso_ospite
    if totale < VOLUME_MINIMO_DOMINIO:
        return f"troppo poco gioco (volume {totale}, ne servono {VOLUME_MINIMO_DOMINIO})"
    quota = round(max(peso_casa, peso_ospite) / totale * 100)
    return f"squadre pari, {quota}% (serve {SOGLIA_QUOTA_DOMINIO})"


def barra_dominio(quota):
    """Barra a dieci tacche: la quota si legge prima del numero."""
    pieni = max(0, min(10, round(quota / 10)))
    return "▓" * pieni + "░" * (10 - pieni)


def riga_dominio(dominio, home, away, current_stats):
    """Una riga sola che risponde a 'chi comanda e conviene guardarla?'."""
    if not dominio:
        return ""
    chi = home if dominio["lato"] == 0 else away
    tiri = current_stats.get("Tiri totali", (0, 0))
    porta = current_stats.get("Tiri in porta", (0, 0))
    if dominio["lato"] == 1:
        tiri, porta = (tiri[1], tiri[0]), (porta[1], porta[0])
    coda = {
        "sotto": "e sta perdendo",
        "bloccata": "e non segna",
        "avanti": "ed è avanti",
    }[dominio["situazione"]]
    emoji = {"sotto": "🔥", "bloccata": "⚡", "avanti": "▪️"}[dominio["situazione"]]
    return (f"{emoji} {chi} comanda {dominio['quota']}% {coda} "
            f"({tiri[0]}-{tiri[1]} tiri, {porta[0]}-{porta[1]} in porta)")


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
        if fixture_in_whitelist(f)
    ]
    if not partite_cmd:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": "Nessuna partita live al momento nei campionati con statistiche note."}, timeout=5)
        return

    MAX_PARTITE_SCANDITE = 40
    da_scandire = partite_cmd[:MAX_PARTITE_SCANDITE]
    avviso_limite = f" (limitata alle prime {MAX_PARTITE_SCANDITE} su {len(partite_cmd)})" if len(partite_cmd) > MAX_PARTITE_SCANDITE else ""
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": f"Calcolo indice di intensità su {len(da_scandire)} partite{avviso_limite}, attendi..."}, timeout=5)

    risultati = []
    for f in da_scandire:
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
            current_stats = estrai_current_stats(sh, sa)
            delta_stats, is_real = calcola_delta_15min(fid, current_stats, f["fixture"]["status"].get("elapsed") or 0)
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


# =============================================================================
# SEI STRATEGIE - logica di valutazione, tenuta per lo shadow-log e per /diagnostica
# (vedi registra_shadow_log_strategie_snapshot dentro processa_partita). Non sono più esposte
# come comandi Telegram (/assedio /fasciacalda /rimonta /concretezza /xgtiro /qualita /scanner
# /strategie sono stati tolti su richiesta): il bot le valuta comunque in background su ogni
# partita seguita e registra quali scattano, per validarle più avanti con dati reali - senza
# mai mandarle in chat come notifica o risposta a un comando.
# =============================================================================
# Soglie di partenza per le sei strategie: numeri ragionevoli scelti da zero (le soglie esatte
# discusse in sessioni precedenti non sono state salvate nel codice), pensati per essere
# facilmente ritoccati qui se in pratica risultano troppo permissivi o troppo restrittivi.
SOGLIA_ASSEDIO_MINUTO = 20
SOGLIA_ASSEDIO_GOL_MAX = 1
SOGLIA_ASSEDIO_RITMO_MIN = 4
PESO_ASSEDIO_XG = 3

SOGLIA_FASCIACALDA_MEDIA = 0.30
SOGLIA_FASCIACALDA_PARTITE_MIN = 9
SOGLIA_FASCIACALDA_GOLEADA = 3

SOGLIA_RIMONTA_MIN = 4

SOGLIA_CONCRETEZZA_TIRI_MIN = 3
SOGLIA_CONCRETEZZA_MIN = 0.5

SOGLIA_XGTIRO_TIRI_MIN = 2
SOGLIA_XGTIRO_TIRI_MAX = 6
SOGLIA_XGTIRO_MIN = 0.15

SOGLIA_QUALITA_DIFF_TIRI_MAX = 2
SOGLIA_QUALITA_DIFF_INDICE_MIN = 0.25


def estrai_xg(stats_team):
    """xG (expected_goals) della squadra, o None se il campo non è presente/valorizzato
    (distingue "dato assente" da "0.0 reale", a differenza di estrai_valore_stat)."""
    for stat in stats_team:
        if (stat.get("type") or "").lower() == "expected_goals":
            val = stat.get("value")
            if val is None:
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                return None
    return None


def valuta_assedio(p):
    """1. Assedio senza gol: partita ancora bloccata (0-1 gol totali) dopo il 20', ma con ritmo
    alto negli ultimi 15' e xG cumulativo molto superiore ai gol realmente segnati (squadra
    "sfortunata" o portiere in giornata, probabile sblocco vicino)."""
    if p["minute"] < SOGLIA_ASSEDIO_MINUTO or not p["delta_reale"]:
        return None
    gol_totali = p["score_h"] + p["score_a"]
    if gol_totali > SOGLIA_ASSEDIO_GOL_MAX:
        return None
    ritmo = calcola_indice_intensita(p["delta"])
    if ritmo < SOGLIA_ASSEDIO_RITMO_MIN:
        return None
    xg_tot = (p["xg_home"] or 0) + (p["xg_away"] or 0)
    bonus_xg = max(0, xg_tot - gol_totali) * PESO_ASSEDIO_XG
    dettaglio = f"ritmo {ritmo:.1f}pt ultimi 15'"
    if xg_tot > 0:
        dettaglio += f", xG {xg_tot:.2f} vs {gol_totali} gol"
    return ritmo + bonus_xg, dettaglio


def valuta_fasciacalda(p):
    """2. Pattern orario storico: il minuto attuale è dentro una fascia di 15' in cui una delle
    due squadre segna o subisce, storicamente e nel proprio ruolo, molto sopra la media — solo se
    la partita non è già in goleada."""
    if abs(p["score_h"] - p["score_a"]) >= SOGLIA_FASCIACALDA_GOLEADA:
        return None
    fascia = fascia_minuto(p["minute"])
    candidati = []
    squadra_casa = trova_squadra_in_storico(p["home"])
    if squadra_casa and squadra_casa["casa"]["partite"] >= SOGLIA_FASCIACALDA_PARTITE_MIN:
        partite = squadra_casa["casa"]["partite"]
        fatti = squadra_casa["casa"]["fatti"].get(fascia, 0)
        subiti = squadra_casa["casa"]["subiti"].get(fascia, 0)
        if fatti / partite >= SOGLIA_FASCIACALDA_MEDIA:
            candidati.append((fatti / partite, f"{p['home']} segna spesso in casa in questa fascia ({fatti}/{partite} partite)"))
        if subiti / partite >= SOGLIA_FASCIACALDA_MEDIA:
            candidati.append((subiti / partite, f"{p['home']} subisce spesso in casa in questa fascia ({subiti}/{partite} partite)"))
    squadra_trasferta = trova_squadra_in_storico(p["away"])
    if squadra_trasferta and squadra_trasferta["trasferta"]["partite"] >= SOGLIA_FASCIACALDA_PARTITE_MIN:
        partite = squadra_trasferta["trasferta"]["partite"]
        fatti = squadra_trasferta["trasferta"]["fatti"].get(fascia, 0)
        subiti = squadra_trasferta["trasferta"]["subiti"].get(fascia, 0)
        if fatti / partite >= SOGLIA_FASCIACALDA_MEDIA:
            candidati.append((fatti / partite, f"{p['away']} segna spesso in trasferta in questa fascia ({fatti}/{partite} partite)"))
        if subiti / partite >= SOGLIA_FASCIACALDA_MEDIA:
            candidati.append((subiti / partite, f"{p['away']} subisce spesso in trasferta in questa fascia ({subiti}/{partite} partite)"))
    if not candidati:
        return None
    candidati.sort(key=lambda c: -c[0])
    media, dettaglio = candidati[0]
    return media * 10, f"fascia {fascia}': {dettaglio}"


def valuta_rimonta(p):
    """3. Rimonta in atto: la squadra in svantaggio mostra un'impennata di ritmo nel 2° tempo
    rispetto alla propria prima parte (confronto con se stessa nel tempo, non con l'avversaria)."""
    if p["score_h"] == p["score_a"]:
        return None
    stats_1h = p["stato_precedente"].get("stats_fine_1h")
    if not stats_1h:
        return None
    idx = 0 if p["score_h"] < p["score_a"] else 1
    nome_squadra = p["home"] if idx == 0 else p["away"]
    d_tiri = p["stats"]["Tiri totali"][idx] - stats_1h["Tiri totali"][idx]
    d_porta = p["stats"]["Tiri in porta"][idx] - stats_1h["Tiri in porta"][idx]
    d_corner = p["stats"]["Corner"][idx] - stats_1h["Corner"][idx]
    punteggio = d_tiri * PESO_INTENSITA_TIRI + d_porta * PESO_INTENSITA_PORTA + d_corner * PESO_INTENSITA_CORNER
    if punteggio < SOGLIA_RIMONTA_MIN:
        return None
    # Stesso ordine casa-trasferta della riga sopra (non "sotto X-Y" con punteggio proprio-avversario
    # in un ordine diverso): altrimenti bisogna ricalcolare a mente chi è chi tra le due righe.
    return punteggio, (
        f"{nome_squadra} insegue ({p['home']} {p['score_h']}-{p['score_a']} {p['away']}): "
        f"nel 2° tempo, rispetto al proprio 1° tempo, sta facendo +{d_tiri} tiri, "
        f"+{d_porta} in porta, +{d_corner} corner"
    )


def _indice_concretezza(p, idx):
    """Combina Tiri in area/Tiri totali (quanto tira da posizione pericolosa) e Tiri in
    porta/Tiri totali (quanto è preciso): alto su entrambi = squadra che attacca davvero."""
    tiri_tot = p["stats"]["Tiri totali"][idx]
    if tiri_tot < SOGLIA_CONCRETEZZA_TIRI_MIN:
        return None
    rapporto_area = p["stats"]["Tiri in area"][idx] / tiri_tot
    rapporto_porta = p["stats"]["Tiri in porta"][idx] / tiri_tot
    return (rapporto_area + rapporto_porta) / 2, rapporto_area, rapporto_porta, tiri_tot


def valuta_concretezza(p):
    """4. Indice di concretezza offensiva: quale squadra trasforma meglio i tiri in occasioni
    vere, non solo per volume."""
    candidati = []
    for idx, nome in ((0, p["home"]), (1, p["away"])):
        esito = _indice_concretezza(p, idx)
        if esito is None:
            continue
        indice, rapporto_area, rapporto_porta, tiri_tot = esito
        candidati.append((indice, nome, rapporto_area, rapporto_porta, tiri_tot))
    if not candidati:
        return None
    candidati.sort(key=lambda c: -c[0])
    indice, nome, rapporto_area, rapporto_porta, tiri_tot = candidati[0]
    if indice < SOGLIA_CONCRETEZZA_MIN:
        return None
    return indice * 10, f"{nome}: {round(rapporto_area * 100)}% tiri da area, {round(rapporto_porta * 100)}% in porta (su {tiri_tot} tiri)"


def valuta_xgtiro(p):
    """5. xG per tiro: poche occasioni ma di alta qualità (expected_goals / tiri totali alto)."""
    candidati = []
    for idx, nome, xg in ((0, p["home"], p["xg_home"]), (1, p["away"], p["xg_away"])):
        if xg is None:
            continue
        tiri_tot = p["stats"]["Tiri totali"][idx]
        if not (SOGLIA_XGTIRO_TIRI_MIN <= tiri_tot <= SOGLIA_XGTIRO_TIRI_MAX):
            continue
        candidati.append((xg / tiri_tot, nome, xg, tiri_tot))
    if not candidati:
        return None
    candidati.sort(key=lambda c: -c[0])
    xg_per_tiro, nome, xg, tiri_tot = candidati[0]
    if xg_per_tiro < SOGLIA_XGTIRO_MIN:
        return None
    return xg_per_tiro * 10, f"{nome}: xG {xg:.2f} su {tiri_tot} tiri ({xg_per_tiro:.2f} xG/tiro)"


def valuta_qualita(p):
    """6. Confronto di qualità (non di volume): tiri quasi pari tra le due squadre, ma l'indice di
    concretezza (punto 4) di una è nettamente superiore all'altra."""
    tiri_h = p["stats"]["Tiri totali"][0]
    tiri_a = p["stats"]["Tiri totali"][1]
    if abs(tiri_h - tiri_a) > SOGLIA_QUALITA_DIFF_TIRI_MAX:
        return None
    esito_h = _indice_concretezza(p, 0)
    esito_a = _indice_concretezza(p, 1)
    if esito_h is None or esito_a is None:
        return None
    indice_h = esito_h[0]
    indice_a = esito_a[0]
    diff = indice_h - indice_a
    if abs(diff) < SOGLIA_QUALITA_DIFF_INDICE_MIN:
        return None
    migliore = p["home"] if diff > 0 else p["away"]
    return abs(diff) * 10, f"{migliore} molto più concreta a parità di tiri ({tiri_h}-{tiri_a}): indice {max(indice_h, indice_a):.2f} vs {min(indice_h, indice_a):.2f}"


STRATEGIE = [
    ("Assedio", "🏰", valuta_assedio, "match fermi ma con pressione alta"),
    ("Fascia calda", "⏰", valuta_fasciacalda, "squadra storicamente pericolosa in questa fascia oraria"),
    ("Rimonta", "🔄", valuta_rimonta, "squadra in svantaggio che spinge più che nel 1° tempo"),
    ("Concretezza", "🎯", valuta_concretezza, "squadra che trasforma bene i tiri in occasioni vere"),
    ("xG per tiro", "💎", valuta_xgtiro, "poche occasioni ma di alta qualità"),
    ("Qualità", "⚖️", valuta_qualita, "tiri quasi pari ma una squadra molto più concreta"),
]


def cmd_setup(chat_id):
    keyboard = {
        "inline_keyboard": [
            [{"text": "📡 Live", "callback_data": "cmd:live"}],
            [{"text": "⭐ Preferiti", "callback_data": "cmd:favorites"},
             {"text": "🗑 Svuota preferiti", "callback_data": "cmd:clearfavorites"}],
            [{"text": "🔇 Silenziate", "callback_data": "cmd:silenced"}],
            [{"text": "🗓 Piano giornata", "callback_data": "cmd:piano"}],
            [{"text": "⏸ Pausa", "callback_data": "cmd:stop"},
             {"text": "▶️ Riprendi", "callback_data": "cmd:riprendi"}],
            [{"text": "🔕 Modalità essenziale", "callback_data": "cmd:modalitaessenziale"},
             {"text": "🔔 Modalità completa", "callback_data": "cmd:modalitacompleta"}],
            [{"text": "🧪 Test canale preferiti", "callback_data": "cmd:testpreferiti"}],
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
def _disegna_grafico_barre(ax, home_name, away_name, stats):
    """Disegna il grafico a barre proporzionale (totale cumulativo della partita) sull'ax
    passato, cosi' puo' essere usato sia da solo (genera_grafico_barre) sia impilato insieme
    al grafico momentum in un'unica immagine (genera_grafico_combinato)."""
    metrics = list(stats.keys())
    home_vals = [stats[m][0] for m in metrics]
    away_vals = [stats[m][1] for m in metrics]

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
    ax.set_title(f"Totale partita: {home_name} vs {away_name}",
                 fontsize=9, color=color_text, pad=10)

    home_patch = mpatches.Patch(color=color_home, label=home_name)
    away_patch = mpatches.Patch(color=color_away, label=away_name)
    ax.legend(handles=[home_patch, away_patch], loc='lower center',
              bbox_to_anchor=(0.5, -0.20), ncol=2, frameon=False,
              fontsize=9, labelcolor=color_text)


def genera_grafico_barre(fixture_id, home_name, away_name, stats):
    # fig chiusa in un finally (non solo sul percorso di successo, prima del return): un'eccezione
    # tra plt.subplots() e plt.close() - rendering, savefig, disco pieno - lasciava altrimenti la
    # figura per sempre nel registro globale di matplotlib, mai liberata. Con un grafico generato
    # ad ogni notifica su ogni partita monitorata, bastava un'eccezione occasionale per accumulare
    # lentamente memoria fino a far sforare il limite del processo (causa più probabile dei riavvii
    # per out-of-memory segnalati da Render). plt.close(fig) invece di plt.close() bare: il bot è
    # multi-thread (ogni comando manuale gira nel suo thread), e pyplot tiene uno stato globale
    # "figura corrente" non thread-safe - chiudere per riferimento esplicito evita di chiudere la
    # figura sbagliata se un altro thread ne ha creata una nel frattempo.
    fig = None
    try:
        fig, ax = plt.subplots(figsize=(5.0, 2.6), dpi=150)
        fig.patch.set_facecolor('#1e1e1e')
        _disegna_grafico_barre(ax, home_name, away_name, stats)
        ax.set_title("")  # titolo ridondante quando il grafico è da solo (già nel testo/legenda)

        plt.tight_layout(rect=[0, 0.06, 1, 1])

        foto_path = os.path.join(os.path.dirname(__file__), f'chart_{fixture_id}.png')
        plt.savefig(foto_path, format='png', bbox_inches='tight',
                    facecolor='#1e1e1e', edgecolor='none', pad_inches=0.1)
        return foto_path
    except Exception as e:
        log(f"Errore grafico barre: {e}")
        return None
    finally:
        if fig is not None:
            plt.close(fig)


def _calcola_punteggi_momentum(history):
    """Calcola i punteggi per barra a partire dallo storico. Punteggio = tiri totali + tiri in
    porta (pesati) + corner + delta di xG (se disponibile) - la qualità del tiro (xG), non solo
    la quantità, quindi un singolo tiro pericoloso può pesare quanto una raffica di tiri innocui.
    NOTA: xGOT (expected goals on target) non è un dato fornito da API-Football, solo xG
    "semplice" - non è nel calcolo perché non esiste nella fonte dati, non per una scelta di
    design. Ritorna None se lo storico è troppo corto o completamente piatto."""
    if len(history) < MOMENTUM_MIN_STORICO:
        return None

    storico = sorted(history, key=lambda h: h["timestamp"])
    punteggi_casa, punteggi_ospite = [], []
    for prec, corr in zip(storico, storico[1:]):
        s_prec, s_corr = prec["stats"], corr["stats"]
        d_tiri_h = max(0, s_corr["Tiri totali"][0] - s_prec["Tiri totali"][0])
        d_tiri_a = max(0, s_corr["Tiri totali"][1] - s_prec["Tiri totali"][1])
        d_porta_h = max(0, s_corr["Tiri in porta"][0] - s_prec["Tiri in porta"][0])
        d_porta_a = max(0, s_corr["Tiri in porta"][1] - s_prec["Tiri in porta"][1])
        d_corner_h = max(0, s_corr["Corner"][0] - s_prec["Corner"][0])
        d_corner_a = max(0, s_corr["Corner"][1] - s_prec["Corner"][1])

        xg_prec = prec.get("xg") or [None, None]
        xg_corr = corr.get("xg") or [None, None]
        d_xg_h = d_xg_a = 0
        if xg_prec[0] is not None and xg_corr[0] is not None:
            d_xg_h = max(0, xg_corr[0] - xg_prec[0])
        if xg_prec[1] is not None and xg_corr[1] is not None:
            d_xg_a = max(0, xg_corr[1] - xg_prec[1])

        punteggi_casa.append(
            d_tiri_h * PESO_INTENSITA_TIRI + d_porta_h * PESO_INTENSITA_PORTA
            + d_corner_h * PESO_INTENSITA_CORNER + d_xg_h * PESO_MOMENTUM_XG
        )
        punteggi_ospite.append(
            d_tiri_a * PESO_INTENSITA_TIRI + d_porta_a * PESO_INTENSITA_PORTA
            + d_corner_a * PESO_INTENSITA_CORNER + d_xg_a * PESO_MOMENTUM_XG
        )

    if not any(punteggi_casa) and not any(punteggi_ospite):
        return None
    return storico, punteggi_casa, punteggi_ospite


def _larghezza_grafico_momentum(ultimo_minuto):
    # Larghezza proporzionale ai minuti effettivamente giocati (non al numero di barre): con una
    # partita appena iniziata il grafico resta compatto invece di allargarsi a vuoto verso il 90'.
    frazione = min(1.0, (ultimo_minuto or 0) / 90)
    return max(3.5, min(6.5, 2.0 + 4.5 * frazione))


def _disegna_marcatori_evento(ax, eventi, y_riga):
    """Disegna, su una singola riga orizzontale fissa (y_riga: positiva sopra per la casa,
    negativa sotto per la trasferta), un marcatore per ogni evento nella lista, al suo minuto
    reale sull'asse x. 'eventi' è una lista di tuple (minuto, simbolo, colore). Eventi troppo
    vicini nel tempo (entro 3 minuti l'uno dall'altro, raro) vengono raggruppati e distanziati
    leggermente sull'asse x, altrimenti si sovrapporrebbero."""
    if not eventi:
        return
    eventi = sorted(eventi, key=lambda e: e[0])
    gruppi = [[eventi[0]]]
    for ev in eventi[1:]:
        if ev[0] - gruppi[-1][-1][0] <= 3:
            gruppi[-1].append(ev)
        else:
            gruppi.append([ev])
    for gruppo in gruppi:
        n = len(gruppo)
        centro = sum(ev[0] for ev in gruppo) / n
        fontsize = 13 if n <= 2 else max(7, 13 - (n - 2) * 3)
        passo = 4.5 if n > 2 else 2.5  # in minuti (unità dell'asse x)
        offset_iniziale = -(n - 1) * passo / 2
        for k, (_minuto, simbolo, colore) in enumerate(gruppo):
            ax.text(centro + offset_iniziale + k * passo, y_riga, simbolo,
                    ha='center', va='center', fontsize=fontsize, fontweight='bold',
                    color=colore, zorder=4)


def _ticks_fissi_grafico_momentum(ultimo_minuto, recupero_1h=None, recupero_2h=None):
    """Traguardi fissi sull'asse x (0, 15, 30, HT, 60, 75, FT), posizionati al loro minuto reale
    su un asse temporale continuo: essendo tutti a distanza di 15' l'uno dall'altro sono quindi
    automaticamente equidistanti tra loro (0' a sinistra, FT/90' a destra), invece di dipendere
    da dove capitano le barre con dati. Non mostra un traguardo se la partita non ci è ancora
    arrivata. Se noto (solo per i preferiti, via genera_grafico_combinato) il recupero di 1°/2°
    tempo viene aggiunto direttamente all'etichetta HT/FT (es. "HT +5'"), senza spostarne la
    posizione (resta comunque a 45'/90': il recupero è testo, non un nuovo minuto sull'asse)."""
    obiettivi = [
        (0, "0'"), (15, "15'"), (30, "30'"),
        (45, f"HT +{recupero_1h}'" if recupero_1h else "HT"),
        (60, "60'"), (75, "75'"),
        (90, f"FT +{recupero_2h}'" if recupero_2h else "FT"),
    ]
    return [(m, e) for m, e in obiettivi if m <= (ultimo_minuto or 0) + 5]


def _disegna_grafico_momentum(ax, home_name, away_name, storico, punteggi_casa, punteggi_ospite,
                               eventi_casa=None, eventi_ospite=None,
                               recupero_1h=None, recupero_2h=None):
    """Disegna il grafico momentum (andamento a intervalli) sull'ax passato, cosi' puo' essere
    usato sia da solo (genera_grafico_momentum) sia impilato insieme al grafico a barre
    proporzionale in un'unica immagine (genera_grafico_combinato). Asse x continuo in minuti reali
    (non indici di barra): ogni barra è larga quanto il suo intervallo effettivo e posizionata al
    minuto giusto, cosi' 0'/15'/30'/HT/60'/75'/FT risultano davvero equidistanti (stessa distanza
    reale in minuti), con 0' all'inizio del grafico e l'ultimo minuto disponibile alla fine. I
    marcatori seguono la stessa convenzione delle barre: eventi della casa sopra la linea dello
    zero, della trasferta sotto. Niente emoji (⚽/❌🟥): il font di sistema usato da matplotlib in
    produzione non ha i glifi e mostrerebbe un quadratino vuoto, quindi '+' per un gol, 'X' per un
    rigore sbagliato/parato, '▮' per un'espulsione."""
    color_home = '#22c55e'
    color_away = '#ef4444'
    color_text = '#e5e5e5'
    color_muted = '#888888'

    ax.set_facecolor('#1e1e1e')

    centri, larghezze = [], []
    for prec, corr in zip(storico, storico[1:]):
        m0 = prec.get("minuto") or 0
        m1 = corr.get("minuto") or 0
        centri.append((m0 + m1) / 2)
        larghezze.append(max(1.0, (m1 - m0) * 0.85))

    ax.bar(centri, punteggi_casa, width=larghezze, color=color_home, zorder=2, edgecolor='none')
    ax.bar(centri, [-v for v in punteggi_ospite], width=larghezze, color=color_away, zorder=2, edgecolor='none')
    ax.axhline(0, color=color_muted, linewidth=1, zorder=1)

    # Nessuna etichetta numerica sopra/sotto le barre (tolta su richiesta esplicita): il colore
    # e l'altezza della barra bastano a leggere l'andamento, senza numerini che affollano il
    # grafico. Restano solo le etichette dei minuti in basso sull'asse.
    picco = max([abs(v) for v in punteggi_casa + punteggi_ospite] or [1])
    ha_eventi = bool(eventi_casa) or bool(eventi_ospite)
    # Margine ben più largo del punto in cui vengono disegnati i marcatori (non solo di poco):
    # con un picco piccolo un margine stretto schiaccia i marcatori quasi a ridosso delle
    # etichette dell'asse x, rendendoli illeggibili/tagliati.
    margine = picco * 2.2 if ha_eventi else picco * 1.1
    ax.set_ylim(-margine, margine)

    if ha_eventi:
        # Riga fissa sopra/sotto lo zero, indipendente dall'altezza delle barre: cosi' i
        # marcatori sono sempre visibili anche quando la barra dell'intervallo è bassa o assente.
        _disegna_marcatori_evento(ax, eventi_casa, picco * 1.4)
        _disegna_marcatori_evento(ax, eventi_ospite, -picco * 1.4)

    ultimo_minuto = storico[-1].get("minuto") or 0
    traguardi = _ticks_fissi_grafico_momentum(ultimo_minuto, recupero_1h, recupero_2h)
    ax.set_xticks([m for m, _ in traguardi])
    ax.set_xticklabels([e for _, e in traguardi], fontsize=8, color=color_text)
    xlim_max = max(ultimo_minuto, traguardi[-1][0] if traguardi else ultimo_minuto)
    ax.set_xlim(0, xlim_max)
    ax.set_yticks([])
    for spine in ['top', 'right', 'bottom', 'left']:
        ax.spines[spine].set_visible(False)

    ax.set_title(f"Momentum: {home_name} vs {away_name} (tiri, porta, corner, xG)",
                 fontsize=9, color=color_text, pad=10)

    home_patch = mpatches.Patch(color=color_home, label=home_name)
    away_patch = mpatches.Patch(color=color_away, label=away_name)
    # -0.32 (non -0.22): con l'asse a minuti reali le etichette "30'"/"HT" cadono spesso proprio
    # sopra la legenda, e a -0.22 le due righe erano quasi a contatto.
    ax.legend(handles=[home_patch, away_patch], loc='lower center',
              bbox_to_anchor=(0.5, -0.32), ncol=2, frameon=False,
              fontsize=9, labelcolor=color_text)


def _eventi_marcatori_per_squadra(home_name, away_name, goals, rigori, cartellini_rossi=None):
    """Divide gol, rigori sbagliati/parati e cartellini rossi per squadra (casa/trasferta), come
    tuple (minuto, simbolo, colore) pronte per _disegna_marcatori_evento - casa sopra la linea
    dello zero, trasferta sotto (stessa convenzione delle barre)."""
    goals = goals or []
    rigori = rigori or []
    cartellini_rossi = cartellini_rossi or []
    eventi_casa, eventi_ospite = [], []

    def _aggiungi(minuto, squadra, simbolo, colore):
        if squadra == home_name:
            eventi_casa.append((minuto, simbolo, colore))
        elif squadra == away_name:
            eventi_ospite.append((minuto, simbolo, colore))

    for g in goals:
        _aggiungi(g["minute"], g.get("team"), '+', '#facc15')
    for r in rigori:
        if r.get("esito") == "sbagliato":
            _aggiungi(r["minute"], r.get("team"), 'X', '#ef4444')
    for c in cartellini_rossi:
        _aggiungi(c["minute"], c.get("team"), '▮', '#ef4444')
    return eventi_casa, eventi_ospite


def genera_grafico_momentum(fixture_id, home_name, away_name, history, goals=None, rigori=None, cartellini_rossi=None):
    """Grafico "momentum": una barra per ogni intervallo tra due rilevazioni consecutive, verde
    verso l'alto quando spinge la casa in quell'intervallo, rossa verso il basso quando spinge la
    trasferta. Risoluzione onesta: un punto ogni ciclo (~3 min quando la partita è "attiva"), non
    al minuto - non è quindi immediato/fluido come i widget con feed dati proprietario, ma usa
    dati reali. goals/rigori/cartellini_rossi (liste opzionali, formato extract_goals/
    extract_rigori/extract_cartellini_rossi) aggiungono un marcatore '+' dorato sui gol, 'X' rossa
    sui rigori sbagliati/parati e '▮' rossa sulle espulsioni, sopra lo zero per la casa e sotto
    per la trasferta (stessa convenzione delle barre)."""
    fig = None  # chiusa in finally, vedi commento in genera_grafico_barre
    try:
        dati = _calcola_punteggi_momentum(history)
        if not dati:
            return None
        storico, punteggi_casa, punteggi_ospite = dati
        eventi_casa, eventi_ospite = _eventi_marcatori_per_squadra(
            home_name, away_name, goals, rigori, cartellini_rossi)

        ultimo_minuto = storico[-1].get("minuto") or 0
        fig, ax = plt.subplots(figsize=(_larghezza_grafico_momentum(ultimo_minuto), 3.2), dpi=150)
        fig.patch.set_facecolor('#1e1e1e')
        _disegna_grafico_momentum(ax, home_name, away_name, storico, punteggi_casa, punteggi_ospite,
                                   eventi_casa, eventi_ospite)
        ax.set_title(f"{home_name} vs {away_name} - momentum (tiri, porta, corner, xG)",
                     fontsize=9, color='#e5e5e5', pad=10)

        plt.tight_layout(rect=[0, 0.12, 1, 1])

        foto_path = os.path.join(os.path.dirname(__file__), f'momentum_{fixture_id}.png')
        plt.savefig(foto_path, format='png', bbox_inches='tight',
                    facecolor='#1e1e1e', edgecolor='none', pad_inches=0.1)
        return foto_path
    except Exception as e:
        log(f"Errore grafico momentum: {e}")
        return None
    finally:
        if fig is not None:
            plt.close(fig)


def genera_grafico_combinato(fixture_id, home_name, away_name, stats_totali, history, goals=None, rigori=None,
                              cartellini_rossi=None, recupero_1h=None, recupero_2h=None):
    """Un'unica immagine con due grafici impilati (uno sopra l'altro): in alto il totale
    cumulativo di tutta la partita (barre proporzionali, come prima), in basso l'andamento
    momentum a intervalli (con marcatori '+'/'X'/'▮' su gol/rigori sbagliati/espulsioni, vedi
    genera_grafico_momentum). recupero_1h/recupero_2h (solo qui, non nel grafico standalone: sono
    passati solo dalla notifica live dei preferiti) aggiungono il recupero direttamente
    all'etichetta HT/FT dell'asse (es. "HT +5'"), nel punto esatto invece che come nota a parte.
    Cosi' i preferiti hanno il quadro d'insieme, il dettaglio temporale e gli eventi chiave in
    una notifica sola, senza dover scegliere tra due grafici o mandarne due separati (che su
    Telegram si aprono uno alla volta, esperienza confusa)."""
    fig = None  # chiusa in finally, vedi commento in genera_grafico_barre
    try:
        dati = _calcola_punteggi_momentum(history)
        if not dati:
            return None
        storico, punteggi_casa, punteggi_ospite = dati
        eventi_casa, eventi_ospite = _eventi_marcatori_per_squadra(
            home_name, away_name, goals, rigori, cartellini_rossi)

        ultimo_minuto = storico[-1].get("minuto") or 0
        larghezza = max(5.0, _larghezza_grafico_momentum(ultimo_minuto))
        fig, (ax_barre, ax_momentum) = plt.subplots(
            2, 1, figsize=(larghezza, 5.6), dpi=150,
            gridspec_kw={'height_ratios': [2.6, 3.0]}
        )
        fig.patch.set_facecolor('#1e1e1e')
        _disegna_grafico_barre(ax_barre, home_name, away_name, stats_totali)
        _disegna_grafico_momentum(ax_momentum, home_name, away_name, storico, punteggi_casa, punteggi_ospite,
                                   eventi_casa, eventi_ospite, recupero_1h, recupero_2h)

        plt.tight_layout(rect=[0, 0.05, 1, 1], h_pad=3.0)

        foto_path = os.path.join(os.path.dirname(__file__), f'combinato_{fixture_id}.png')
        plt.savefig(foto_path, format='png', bbox_inches='tight',
                    facecolor='#1e1e1e', edgecolor='none', pad_inches=0.15)
        return foto_path
    except Exception as e:
        log(f"Errore grafico combinato: {e}")
        return None
    finally:
        if fig is not None:
            plt.close(fig)


def nota_copertura_momentum(history):
    """Se lo storico non parte da vicino al calcio d'inizio (es. il bot ha iniziato a monitorare
    la partita a metà, o un riavvio prima della persistenza ha perso i dati precedenti), lo dice
    esplicitamente - altrimenti il grafico sembra "rotto"/incompleto invece che semplicemente
    privo di dati per la parte iniziale che non abbiamo mai visto."""
    if not history:
        return ""
    primo_minuto = min(h.get("minuto") or 0 for h in history)
    if primo_minuto <= 10:
        return ""
    return f"\n(dati disponibili da {primo_minuto}' in poi)"


# =============================================================================
# TASTIERA INLINE
# =============================================================================
def get_notification_keyboard(fixture_id, is_favorite=False, is_silenced=False, mostra_momentum=False):
    if is_silenced:
        return None
    buttons = []
    fav_text = "Rimuovi dai preferiti" if is_favorite else "Aggiungi ai preferiti"
    buttons.append([{"text": fav_text, "callback_data": f"fav:{fixture_id}"}])
    buttons.append([{"text": "Silenzia questa partita", "callback_data": f"mute:{fixture_id}"}])
    if mostra_momentum:
        buttons.append([{"text": "📈 Momentum", "callback_data": f"momentum:{fixture_id}"}])
    return {"inline_keyboard": buttons}


# =============================================================================
# DELTA 15 MINUTI - a blocchi fissi (0-15', 15-30', 30-45', 45-60', 60-75', 75-90'...): si azzera
# esattamente ad ogni multiplo di 15 minuti di gioco, invece di scorrere con una finestra reale
# calcolata da "adesso". Stessa risorsa (stato_partite[fid]["history"]) usata da /momentum.
# =============================================================================
def _blocco_minuto(minuto):
    return (minuto or 0) // 15


def _calcola_delta_15min_da_storico(history, current_stats, minuto_corrente):
    blocco_corrente = _blocco_minuto(minuto_corrente)
    punti_blocco = [h for h in history if _blocco_minuto(h.get("minuto")) == blocco_corrente]

    # Serve ALMENO 2 punti nel blocco corrente (non solo "il blocco esiste"): con un solo punto -
    # tipicamente lo snapshot appena preso in questo stesso ciclo, che è già dentro "history" -
    # inizio_blocco coinciderebbe con current_stats e il delta sarebbe sempre (0,0) MA marcato
    # come "reale" invece che "primo rilevamento". Questo è esattamente il bug che faceva apparire
    # decine di partite con "0.0 pt" tutte uguali nel report automatico appena dopo un riavvio (o
    # ad ogni cambio di blocco, per un ciclo).
    if len(punti_blocco) < 2:
        return {k: (0, 0) for k in current_stats}, False

    inizio_blocco = min(punti_blocco, key=lambda h: h["timestamp"])
    delta = {}
    for key in current_stats:
        curr_h, curr_a = current_stats[key]
        old_h, old_a = inizio_blocco["stats"].get(key, (0, 0))
        delta[key] = (max(0, curr_h - old_h), max(0, curr_a - old_a))
    return delta, True


def calcola_delta_15min(fixture_id, current_stats, minuto_corrente):
    stato = stato_partite.get(fixture_id, {})
    history = stato.get("history", [])
    return _calcola_delta_15min_da_storico(history, current_stats, minuto_corrente)


def invia_report_intensita_automatico(partite_valide, notifiche_attive=True):
    """Chiamata una volta per ciclo dal loop principale. Non manda nulla finché lo storico
    (azzerato ad ogni riavvio) non ha almeno un delta reale su 15 minuti; da quel momento invia
    la classifica di intensità ogni INTERVALLO_REPORT_INTENSITA secondi, riusando i dati già
    scaricati in questo ciclo (nessuna chiamata API aggiuntiva). notifiche_attive=False (fuori
    dalla fascia oraria configurata) salta solo l'invio, non il calcolo/timer."""
    global ULTIMO_REPORT_INTENSITA
    if not REPORT_INTENSITA_AUTOMATICO_ATTIVO:
        return
    now = time.time()
    if now - ULTIMO_REPORT_INTENSITA < INTERVALLO_REPORT_INTENSITA:
        return

    risultati = []
    for f in partite_valide:
        fixture_id = f.get("fixture", {}).get("id")
        if not fixture_id:
            continue
        if str(fixture_id) in FAVORITE_MATCHES:
            continue  # già seguite con notifiche dedicate più frequenti, non serve duplicarle qui
        stato = stato_partite.get(fixture_id, {})
        history = stato.get("history", [])
        if not history:
            continue
        current_stats = history[-1]["stats"]
        delta_stats, is_real = calcola_delta_15min(fixture_id, current_stats, history[-1].get("minuto") or 0)
        if not is_real:
            continue
        punteggio = calcola_indice_intensita(delta_stats)
        home = stato.get("home") or f.get("teams", {}).get("home", {}).get("name", "?")
        away = stato.get("away") or f.get("teams", {}).get("away", {}).get("name", "?")
        league = stato.get("league") or f.get("league", {}).get("name", "?")
        league_country = stato.get("league_country") or f.get("league", {}).get("country", "")
        minute = f.get("fixture", {}).get("status", {}).get("elapsed", "?")
        score_h = stato.get("score_home", f.get("goals", {}).get("home") or 0)
        score_a = stato.get("score_away", f.get("goals", {}).get("away") or 0)
        risultati.append((punteggio, home, away, league, league_country, minute, score_h, score_a, delta_stats))

    if not risultati:
        log("Report intensità automatico: dati non ancora pronti (storico insufficiente), skip.")
        return

    risultati.sort(key=lambda r: -r[0])
    top = risultati[:7]
    righe = [f"Report automatico intensità (ultimi 15 min) - top {len(top)} di {len(risultati)} partite:\n"]
    for i, (punteggio, home, away, league, league_country, minute, score_h, score_a, delta_stats) in enumerate(top, start=1):
        fiamme = simbolo_fiamma_per_posizione(i)
        prefisso = f"{fiamme} " if fiamme else ""
        d_tiri = delta_stats.get("Tiri totali", (0, 0))
        d_porta = delta_stats.get("Tiri in porta", (0, 0))
        d_area = delta_stats.get("Tiri in area", (0, 0))
        righe.append(
            f"{prefisso}{punteggio:.1f} pt | {home} {score_h}-{score_a} {away} "
            f"({formatta_lega(league, league_country)}, {minute}')\n"
            f"   Tiri totali: {d_tiri[0]} - {d_tiri[1]} | Tiri in porta: {d_porta[0]} - {d_porta[1]} | Tiri in area: {d_area[0]} - {d_area[1]}"
        )
    if notifiche_attive:
        invia_messaggio_telegram("\n".join(righe))
    ULTIMO_REPORT_INTENSITA = now


# Legenda usata dalla diagnostica automatica sotto: ogni anomalia viene taggata con una di queste
# etichette, così chi legge (in chat o nei log di Render) sa subito a quale passaggio della
# pipeline risale e cosa controllare - senza dover rifare il ragionamento da capo ogni volta.
#
# Versione tecnica (nomi di funzioni/variabili reali, per chi legge il codice):
# TRACCIAMENTO: la partita deve comparire tra quelle live e passare campionato_valido() prima che
#   processa_partita() la veda. Se manca: controllare il filtro lega/whitelist, o se il ciclo
#   principale è più lento delle partite che iniziano (troppe live insieme).
# STATISTICHE: get_statistiche_partita() ha restituito None, cioè la chiamata è fallita
#   (rate-limit, timeout, rete) o non è mai stata fatta. Vero problema di pipeline.
# COPERTURA STATISTICHE: get_statistiche_partita() risponde ma ha_statistiche_disponibili() è
#   False da almeno SOGLIA_SENZA_STATISTICHE cicli: l'API non ha dati per quella partita (vedi
#   leghe_senza_statistiche.json per l'esclusione lega, che scatta solo dal
#   MINUTO_MINIMO_VERDETTO_STATISTICHE in poi e solo dopo che SOGLIA_SENZA_STATISTICHE PARTITE
#   diverse della stessa lega hanno ognuna accumulato SOGLIA_SENZA_STATISTICHE risposte vuote
#   consecutive - una singola partita sfortunata, o tante partite nello stesso ciclo a inizio
#   turno, non bastano più a escludere il campionato). Non è un bug del bot.
# xG e QUOTA: non generano anomalie automatiche (mancano spesso per motivi normali: lega non
#   coperta per l'xG, bookmaker senza quota su quella partita) - restano visibili solo lanciando
#   /diagnostica a mano.
# SHADOW-LOG VALORE: richiede la quota 1X2 già risolta, scrive ogni INTERVALLO_SNAPSHOT_VALORE
#   secondi. Se la quota c'è ma non scrive mai: controllare registra_shadow_log_valore_snapshot()
#   e il timer ultimo_snapshot_valore dentro processa_partita().
# SHADOW-LOG STRATEGIE: richiede solo le statistiche (non la quota), stesso ritmo di sopra. Se le
#   statistiche ci sono ma non scrive mai: controllare il blocco corrispondente dentro
#   processa_partita() (dentro l'if current_stats).
# STRATEGIE: se una strategia non scatta mai per ore su nessuna partita non è automaticamente un
#   bug - può essere normale (soglie strette) o mancare un dato a monte (es. Fascia calda resta
#   vuota se storico_aggiornamento_automatico è spento, xG per tiro se l'xG non è disponibile per
#   quelle leghe). Non sono più comandi Telegram: le soglie si controllano nel codice (SOGLIA_*
#   vicino a STRATEGIE).
#
# Versione mandata su Telegram sotto (parse_mode Markdown): niente underscore o parentesi quadre,
# altrimenti Telegram prova a interpretarli come corsivo/link e può rifiutare il messaggio intero
# con un errore di parsing invece di consegnarlo.
LEGENDA_DIAGNOSTICA = (
    "Legenda passaggi pipeline (dove può fermarsi, come intervenire):\n"
    "TRACCIAMENTO: la partita deve comparire tra quelle live e in un campionato supportato prima "
    "che il bot inizi a seguirla. Se manca: controllare il filtro lega/whitelist, o se il ciclo è "
    "più lento delle partite che iniziano (troppe partite live insieme).\n"
    "STATISTICHE: la chiamata API alle statistiche è fallita o non è mai arrivata a destinazione "
    "(rate-limit, timeout, errore di rete). È l'unico caso in cui le statistiche mancanti sono "
    "davvero un problema di pipeline: di solito rientra da solo al ciclo dopo, se persiste "
    "controllare quota giornaliera e stato dell'API.\n"
    "STATISTICHE FERME: la partita le statistiche le aveva, e poi ha smesso di riceverne per un "
    "pezzo (15 minuti o più). Diverso da STATISTICHE, che riguarda le partite che non ne hanno mai "
    "avute: qui la pipeline ha funzionato e si è interrotta a metà, quindi i dati mostrati sono "
    "vecchi anche se ci sono. Di solito è l'API che smette di pubblicare per un po'.\n"
    "COPERTURA STATISTICHE: l'API risponde regolarmente ma per quella partita non pubblica "
    "statistiche. Non c'è niente da riparare nel bot: la partita resta seguita e continua ad "
    "alimentare gli shadow-log, ma non manda notifiche - gol e cartellini compresi - finché "
    "l'API non pubblica i primi dati, e non può far scattare nessuna strategia. Torna a parlare "
    "da sola appena i dati arrivano. Capita anche a partite della stessa lega in cui invece le "
    "statistiche arrivano.\n"
    "Ogni anomalia viene segnalata una volta sola per partita: se resta uguale non viene "
    "ripetuta ad ogni controllo, e ricompare in chat solo se rientra e si ripresenta.\n"
    "(xG e quota 1X2 non generano anomalie automatiche: mancano spesso per motivi normali - lega "
    "non coperta per l'xG, bookmaker senza quota su quella partita - restano visibili solo "
    "lanciando /diagnostica a mano.)\n"
    "SHADOW-LOG VALORE: richiede la quota 1X2 già risolta, scrive ogni 15 minuti circa. Se la "
    "quota c'è ma non scrive mai: bug nella scrittura dello snapshot valore o nel suo timer.\n"
    "SHADOW-LOG STRATEGIE: richiede solo le statistiche (non la quota), stesso ritmo di sopra. Se "
    "le statistiche ci sono ma non scrive mai: bug nel blocco corrispondente dentro l'elaborazione "
    "della partita.\n"
    "STRATEGIE: se una strategia non scatta mai per ore su nessuna partita non è automaticamente "
    "un bug - può essere normale (soglie strette) oppure mancare un dato a monte (es. fascia calda "
    "resta vuota se l'aggiornamento storico automatico è spento, xG per tiro se l'xG non è "
    "disponibile per quelle leghe). Non sono più comandi Telegram, girano solo in background."
)


def _anomalie_nuove(fixture_id, trovate, registra=True):
    """Tiene solo le anomalie di questa partita non ancora mandate in chat, e aggiorna lo storico
    per la prossima passata: le categorie rientrate vengono dimenticate, così se lo stesso
    problema si ripresenta più tardi torna a essere notificato (una volta sola, di nuovo).
    Con registra=False (notifiche spente fuori orario) nulla viene marcato come notificato:
    l'anomalia resta in coda e verrà segnalata al primo controllo dentro l'orario attivo."""
    gia_note = ANOMALIE_DIAGNOSTICA_NOTIFICATE.get(fixture_id, set())
    nuove = [testo for categoria, testo in trovate.items() if categoria not in gia_note]
    if registra:
        if trovate:
            ANOMALIE_DIAGNOSTICA_NOTIFICATE[fixture_id] = set(trovate.keys())
        else:
            ANOMALIE_DIAGNOSTICA_NOTIFICATE.pop(fixture_id, None)
        salva_anomalie_diagnostica_notificate(ANOMALIE_DIAGNOSTICA_NOTIFICATE)
    return nuove


def esegui_diagnostica_automatica(partite_valide, notifiche_attive=True):
    """Diagnostica automatica della pipeline dati: gira da sola dentro il ciclo principale ogni
    INTERVALLO_DIAGNOSTICA_AUTOMATICA secondi, riusando partite_valide/stato_partite già
    aggiornati in questo stesso ciclo (nessuna chiamata API in più). Logga sempre il dettaglio
    passo-passo su Render (visibile anche quando tutto va bene, per un controllo a ritroso) e
    manda un messaggio Telegram automatico SOLO se trova almeno un'anomalia - così non serve
    lanciare nessun comando a mano per accorgersi di un problema mentre le partite sono in corso.

    In chat ogni anomalia va una volta sola per partita (vedi _anomalie_nuove): i log di Render
    continuano invece a riportarle tutte ad ogni passata, per poter ricostruire quanto è durata."""
    global ULTIMA_DIAGNOSTICA_AUTOMATICA
    if not DIAGNOSTICA_AUTOMATICA_ATTIVA:
        return
    ora = time.time()
    if ora - ULTIMA_DIAGNOSTICA_AUTOMATICA < INTERVALLO_DIAGNOSTICA_AUTOMATICA:
        return
    ULTIMA_DIAGNOSTICA_AUTOMATICA = ora

    if not partite_valide:
        log("Diagnostica automatica: nessuna partita live valida in questo momento, skip.")
        return

    righe_log = []
    anomalie = []       # tutto ciò che risulta anomalo adesso (finisce nei log di Render)
    anomalie_nuove = [] # solo ciò che non era già stato notificato (finisce in chat)
    for f in partite_valide:
        fid = f.get("fixture", {}).get("id")
        if not fid:
            continue
        home = f.get("teams", {}).get("home", {}).get("name", "?")
        away = f.get("teams", {}).get("away", {}).get("name", "?")
        minuto_api = f.get("fixture", {}).get("status", {}).get("elapsed") or 0
        stato = stato_partite.get(fid)
        # Anomalie di QUESTO giro per questa partita, indicizzate per categoria: sotto si notifica
        # solo ciò che non era già stato notificato prima (vedi ANOMALIE_DIAGNOSTICA_NOTIFICATE).
        trovate = {}

        if not stato:
            trovate["TRACCIAMENTO"] = (
                f"TRACCIAMENTO - {home}-{away}: live e in campionato valido ma mai tracciata dal bot")
            anomalie.extend(trovate.values())
            anomalie_nuove.extend(_anomalie_nuove(fid, trovate, registra=notifiche_attive))
            continue

        minuto_bot = stato.get("last_minute")
        if minuto_bot is not None and abs(minuto_api - minuto_bot) > 5:
            trovate["TRACCIAMENTO"] = (
                f"TRACCIAMENTO - {home}-{away}: minuto bot fermo a {minuto_bot}' contro {minuto_api}' dell'API")

        history = stato.get("history", [])
        esito_stats = stato.get("stats_ultimo_esito")
        # Età dell'ultimo punto raccolto: "ci sono statistiche" non vuol dire "ne stanno arrivando".
        eta_stats = (ora - history[-1]["timestamp"]) if history else None
        stats_fresche = eta_stats is not None and eta_stats <= SOGLIA_STATISTICHE_FERME
        if history and not stats_fresche and minuto_api > 10:
            # La partita ha raccolto statistiche e poi si è fermata: non lo vedeva nessun controllo,
            # perché tutti guardavano solo se lo storico fosse vuoto (vedi SOGLIA_STATISTICHE_FERME).
            dettaglio_fermo = {
                "vuote": f"l'API risponde ma non pubblica più statistiche "
                         f"({stato.get('stats_vuote_consecutive', 0)} risposte vuote di fila)",
                "errore": "le chiamate alle statistiche stanno fallendo (rate-limit/timeout/rete)",
            }.get(esito_stats, "nessun nuovo dato dall'ultima raccolta")
            trovate["STATISTICHE FERME"] = (
                f"STATISTICHE FERME - {home}-{away}: ultime statistiche raccolte "
                f"{int(eta_stats // 60)} min fa (al {minuto_api}' di gioco) - {dettaglio_fermo}")
        if not history and minuto_api > 10:
            # Due cose diverse che prima venivano dette con la stessa frase: se l'API risponde
            # regolarmente ma per questa partita non ha statistiche, non c'è niente da riparare
            # nel bot (stessa natura di xG e quota mancanti); se invece la chiamata fallisce, o
            # non è mai stata fatta, quello sì è un problema di pipeline.
            # Il verdetto "l'API non copre questa partita" lo dà stats_vuote_consecutive, che si
            # accumula solo su risposte vuote vere e si azzera al primo esito buono. Un fallimento
            # TRANSITORIO (rate-limit, timeout, rete) non lo azzera - apposta, vedi processa_partita
            # - quindi non deve nemmeno annullarlo qui: altrimenti una partita già classificata
            # COPERTURA STATISTICHE tornava a STATISTICHE al primo skip da raffreddamento, e siccome
            # il dedup di _anomalie_nuove() lavora per categoria, il cambio di categoria la faceva
            # risegnalare in chat come se fosse un'anomalia nuova. Con un raffreddamento che salta
            # tutte le chiamate rimaste nel ciclo, il rimbalzo colpiva molte partite insieme.
            evidenza_non_copertura = stato.get("stats_vuote_consecutive", 0) >= SOGLIA_SENZA_STATISTICHE
            if esito_stats in ("vuote", "errore") and evidenza_non_copertura:
                trovate["COPERTURA STATISTICHE"] = (
                    f"COPERTURA STATISTICHE - {home}-{away}: l'API risponde ma non pubblica statistiche "
                    f"per questa partita (al {minuto_api}'). Non è un blocco del bot: la partita resta "
                    f"seguita e negli shadow-log, ma non manda notifiche - gol compresi - finché "
                    f"l'API non pubblica i primi dati.")
            else:
                dettaglio = "chiamata alle statistiche fallita (rate-limit/timeout/rete)" if esito_stats == "errore" \
                    else "nessuna risposta utile alle statistiche"
                trovate["STATISTICHE"] = (
                    f"STATISTICHE - {home}-{away}: nessuna statistica arrivata al {minuto_api}' ({dettaglio})")

        quote = quote_1x2_per_fixture(fid)
        ultimo_val = stato.get("ultimo_snapshot_valore")
        if isinstance(quote, dict) and not ultimo_val and minuto_api > 16:
            trovate["SHADOW-LOG VALORE"] = (
                f"SHADOW-LOG VALORE - {home}-{away}: quota presente ma nessuno snapshot scritto al {minuto_api}'")

        ultimo_strat = stato.get("ultimo_snapshot_strategie")
        # Su stats_fresche e non su history: con lo storico ripristinato dal backup dopo un riavvio
        # (vedi BACKUP_HISTORY_MOMENTUM) e le statistiche che nel frattempo falliscono, la condizione
        # su history diceva "statistiche presenti ma nessuno snapshot" - una frase falsa, perché le
        # statistiche in quel momento non ci sono affatto. Lo snapshot manca come conseguenza, non
        # come causa: il problema vero è già segnalato da STATISTICHE FERME.
        if stats_fresche and not ultimo_strat and minuto_api > 16:
            trovate["SHADOW-LOG STRATEGIE"] = (
                f"SHADOW-LOG STRATEGIE - {home}-{away}: statistiche presenti ma nessuno snapshot scritto al {minuto_api}'")

        anomalie.extend(trovate.values())
        anomalie_nuove.extend(_anomalie_nuove(fid, trovate, registra=notifiche_attive))

        # L'età dell'ultimo punto va scritta anche quando è tutto a posto: è il dato che rende la
        # riga verificabile a ritroso, invece di un "si" che non distingue "aggiornata adesso" da
        # "ferma da un'ora".
        if history:
            stats_txt = f"si ({int(eta_stats // 60)} min fa)" if not stats_fresche else "si (fresche)"
        else:
            stats_txt = f"no (esito API: {esito_stats or 'mai chiamata'})"
        righe_log.append(
            f"{home}-{away} {minuto_api}': tracciata=si, stats={stats_txt}, "
            f"quota={'si' if isinstance(quote, dict) else 'no'}, "
            f"snap_valore={'si' if ultimo_val else 'no'}, snap_strategie={'si' if ultimo_strat else 'no'}"
        )

    log("Diagnostica automatica - dettaglio: " + (" | ".join(righe_log) if righe_log else "nessuna partita tracciabile"))

    if not anomalie:
        log("Diagnostica automatica: nessuna anomalia rilevata.")
        return

    log("Diagnostica automatica - ANOMALIE: " + " | ".join(anomalie))
    if not notifiche_attive:
        log("Diagnostica automatica: notifiche spente (fuori orario), anomalie solo loggate su Render.")
        return

    if not anomalie_nuove:
        # Le stesse anomalie di prima, sulle stesse partite: restano nei log ma non si rimanda lo
        # stesso messaggio (con legenda annessa) ogni 30 minuti per tutta la durata della partita.
        log(f"Diagnostica automatica: {len(anomalie)} anomalie già notificate in precedenza, nessun nuovo messaggio in chat.")
        return

    testo = (
        "🔍 Diagnostica automatica - trovate anomalie nella pipeline dati:\n\n"
        + "\n".join(f"- {a}" for a in anomalie_nuove)
        + "\n\n" + LEGENDA_DIAGNOSTICA
    )
    for i in range(0, len(testo), 3800):
        invia_messaggio_telegram(testo[i:i + 3800])


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
    data, _, _ = get_api_football(url, {"current": "true"}, timeout=20, contesto="risolvi_leghe_whitelist")
    if data is None:
        return LEGHE_ID_STAGIONE_CACHE
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


def get_fixtures_terminati(league_id, season):
    if not API_FOOTBALL_KEY:
        return []
    url = "https://v3.football.api-sports.io/fixtures"
    data, _, _ = get_api_football(
        url, {"league": league_id, "season": season, "status": "FT"}, timeout=20,
        contesto=f"get_fixtures_terminati({league_id})")
    if data is None:
        return []
    return data.get("response", [])


def aggiorna_storico_minutaggi_lega(league_id, season, max_fixtures=None):
    """Scarica i gol delle partite terminate di una lega/stagione non ancora processate e
    aggiorna lo storico locale (gol fatti/subiti per fascia di minuto, separati casa/trasferta,
    per squadra). Elabora al massimo `max_fixtures` nuove partite per chiamata per non consumare
    troppe richieste API in un colpo solo: le partite restanti vengono rimandate alla prossima
    esecuzione (il progresso è tracciato su disco tramite fixture_ids_processati)."""
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
    """Cerca una squadra per nome tra tutte le leghe salvate nello storico. Usa lo stesso
    matching di /status (accenti, sigle, abbreviazioni, alias noti) via _nomi_squadra_matchano,
    cosi' /analisi Milan - Juve e la sezione grafico di /status accettano le stesse forme brevi.
    In caso di piu' corrispondenze sceglie quella con piu' partite giocate."""
    if not nome_query or not nome_query.strip():
        return None
    candidati = []
    for lega_dati in STORICO_MINUTAGGI.values():
        for squadra in lega_dati.get("squadre", {}).values():
            nome = squadra.get("nome", "")
            if _nomi_squadra_matchano(nome_query, nome):
                partite_totali = squadra["casa"]["partite"] + squadra["trasferta"]["partite"]
                candidati.append((partite_totali, squadra))
    if not candidati:
        return None
    candidati.sort(key=lambda c: -c[0])
    return candidati[0][1]


def genera_grafico_minutaggi(nome_casa, dati_casa, nome_trasferta, dati_trasferta):
    """Grafico con 2 pannelli: distribuzione gol fatti/subiti per fascia di 15 minuti,
    squadra di casa nelle sue partite in casa, squadra ospite nelle sue partite in trasferta."""
    fig = None  # chiusa in finally, vedi commento in genera_grafico_barre
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
        return foto_path
    except Exception as e:
        log(f"Errore grafico minutaggi: {e}")
        return None
    finally:
        if fig is not None:
            plt.close(fig)


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
# REGOLE DI NOTIFICA
# =============================================================================
def invia_messaggio_uscita_preferiti(home, away, minuto, score_home, score_away, motivo, stato):
    """Chiude il filo di una partita nel canale preferiti, dicendo perché non se ne parla più.

    Senza, il canale resta pieno di conversazioni senza finale: gli aggiornamenti si interrompono
    e basta, e a distanza di ore non si capisce se la partita è finita, si è spenta o è stata
    tolta. Va nel CANALE, non nella chat principale: è lì che la partita è vissuta."""
    righe = [
        f"🏁 ESCE DAI PREFERITI · {minuto}'",
        f"{home} {score_home}-{score_away} {away}",
        "",
        motivo,
    ]
    # Confronto 1°T/2°T se disponibile: è il riepilogo che rende utile rileggere il filo dopo.
    stats_1h = stato.get("stats_fine_1h")
    history = stato.get("history", [])
    if stats_1h and history:
        righe.append("\n" + testo_confronto_tempi(stats_1h, history[-1]["stats"]))
    invia_messaggio_telegram("\n".join(righe), chat_id=TELEGRAM_CHAT_ID_PREFERITI)


def deve_aggiungere_automaticamente_ai_preferiti(minuto, score_home, score_away):
    """Partita che si sblocca presto restando aperta: due gol entro il 25' con al massimo un gol
    di scarto.

    Ritorna (True, motivo) oppure (False, motivo): il motivo finisce nel log e nella notifica, per
    capire a posteriori perché una partita è stata promossa o scartata senza dover indovinare.

    Non legge le statistiche, ed è deliberato. Il 16/08 l'endpoint statistiche è rimasto muto per
    ore su Belgio, Germania, Corea, Giappone e Svezia mentre i gol continuavano ad arrivare
    regolarmente, col nome del marcatore: una regola che guarda solo il punteggio non si spegne
    insieme al feed, e non ha bisogno né della guardia sull'avaria diffusa né di aspettare che il
    delta 15 minuti diventi misurabile."""
    if not AUTO_PREFERITI_ATTIVO:
        return False, "auto-preferiti disattivati"
    if minuto is None:
        return False, "minuto non disponibile"
    if len(FAVORITE_MATCHES) >= MAX_PREFERITI_SIMULTANEI:
        # Non si marca la partita come già valutata: appena si libera un posto torna in gioco.
        return False, f"già {len(FAVORITE_MATCHES)} preferiti attivi (max {MAX_PREFERITI_SIMULTANEI})"

    gol_totali = score_home + score_away
    scarto = abs(score_home - score_away)
    # I motivi non ripetono punteggio e minuto: chi chiama li ha già e li stampa accanto, e nel
    # messaggio di ingresso finivano scritti due volte nella stessa riga.
    if gol_totali < SOGLIA_GOL_AUTO_PREFERITI:
        return False, f"{gol_totali} gol (ne servono {SOGLIA_GOL_AUTO_PREFERITI})"
    if minuto > MINUTO_GOL_AUTO_PREFERITI:
        return False, f"i {gol_totali} gol sono arrivati dopo il {MINUTO_GOL_AUTO_PREFERITI}'"
    # Lo scarto decide se la partita è ancora aperta: con 2 gol passa l'1-1 ma non il 2-0, con 3
    # gol passa il 2-1. Un 2-0 al 20' non è una partita viva, è una partita che si sta chiudendo.
    # Copre da sé anche la goleada: con al massimo un gol di scarto non ci si arriva mai.
    if scarto > SCARTO_MAX_AUTO_PREFERITI:
        return False, f"{scarto} gol di scarto, partita già indirizzata"
    return True, (f"{gol_totali} gol entro il {MINUTO_GOL_AUTO_PREFERITI}' "
                  f"con la partita ancora aperta")


def _dominio_per_auto_preferiti(current_stats, score_home, score_away):
    """Quota e volume pesato della partita, letti con gli occhi della rotta dominio.

    Ritorna (quota, volume, dominio) con quota/volume a None quando non c'e' abbastanza materiale
    per dire alcunche' - cioe' esattamente quando calcola_dominio() ritorna None: statistiche
    assenti, troppo poco gioco, oppure squadre sostanzialmente pari. Il volume e' lo stesso peso
    offensivo combinato su cui calcola_dominio calcola la percentuale, riusato qui invece di
    ricontarlo per non avere due definizioni di "quanto si e' giocato" che possono divergere."""
    dominio = calcola_dominio(current_stats, score_home, score_away)
    if not dominio:
        return None, None, None
    volume = _peso_offensivo(current_stats, 0) + _peso_offensivo(current_stats, 1)
    return dominio["quota"], volume, dominio


def deve_aggiungere_automaticamente_ai_preferiti_per_dominio(fixture_id, current_stats, score_home,
                                                             score_away, minuto):
    """Rotta 2: una squadra che sta facendo la partita da sola, con abbastanza gioco alle spalle
    perche' la percentuale voglia dire qualcosa, e per piu' cicli di fila.

    Ritorna (True/False, motivo), stessa forma della rotta gol: il motivo finisce nel log e nel
    messaggio di ingresso del canale, cosi' resta sempre scritto PERCHE' una partita e' entrata.

    Aggiorna anche il contatore di isteresi e il picco di dominio dentro stato_partite: va chiamata
    ad ogni ciclo, anche quando la promozione e' spenta (AUTO_PREFERITI_DOMINIO_ATTIVO=False), che
    e' il modo in cui lo shadow-log raccoglie i dati per tarare le soglie."""
    stato = stato_partite.setdefault(fixture_id, {})
    quota, volume, dominio = _dominio_per_auto_preferiti(current_stats, score_home, score_away)

    # Il picco si aggiorna sempre, promozione accesa o spenta e soglie a parte: e' il dato che
    # serve all'analisi offline per rispondere a "con quale soglia questa partita sarebbe entrata".
    if quota is not None and quota > stato.get("dominio_quota_max", 0):
        stato["dominio_quota_max"] = quota
        stato["dominio_volume_al_max"] = volume
        stato["dominio_minuto_al_max"] = minuto
        stato["dominio_situazione_al_max"] = dominio["situazione"]

    sopra_soglia = (quota is not None
                    and quota >= SOGLIA_QUOTA_DOMINIO_AUTO_PREFERITI
                    and volume >= VOLUME_MINIMO_DOMINIO_AUTO_PREFERITI)
    if sopra_soglia:
        cicli = stato.get("cicli_dominio_sopra_soglia", 0) + 1
    else:
        # Azzeramento, non decremento: l'isteresi deve chiedere cicli CONSECUTIVI, altrimenti una
        # partita che oscilla intorno alla soglia accumulerebbe lo stesso il credito per entrare.
        cicli = 0
    stato["cicli_dominio_sopra_soglia"] = cicli
    stato["cicli_dominio_sopra_soglia_max"] = max(stato.get("cicli_dominio_sopra_soglia_max", 0), cicli)

    if not AUTO_PREFERITI_DOMINIO_ATTIVO:
        return False, "rotta dominio in sola osservazione (non promuove)"
    if not AUTO_PREFERITI_ATTIVO:
        return False, "auto-preferiti disattivati"
    if minuto is None:
        return False, "minuto non disponibile"
    if len(FAVORITE_MATCHES) >= MAX_PREFERITI_SIMULTANEI:
        # Come la rotta gol: non si marca la partita come gia' valutata, appena si libera un posto
        # torna in gioco. Il tetto e' lo stesso identico dei preferiti manuali e della rotta gol,
        # non uno dedicato: e' li' per proteggere il numero di chiamate API, e le chiamate non
        # sanno da quale porta sia entrata la partita.
        return False, f"gia' {len(FAVORITE_MATCHES)} preferiti attivi (max {MAX_PREFERITI_SIMULTANEI})"
    # Goleada: qui serve un controllo esplicito, mentre la rotta gol la evita da sola (con al
    # massimo un gol di scarto non ci si arriva mai). Una squadra puo' benissimo dominare all'85%
    # mentre e' gia' avanti 4-0, ed e' proprio la partita che il resto del bot smette di notificare.
    if abs(score_home - score_away) > SOGLIA_GOLEADA_STOP_NOTIFICHE:
        return False, f"{abs(score_home - score_away)} gol di scarto, partita gia' decisa"
    if quota is None:
        return False, "nessun dominio misurabile (statistiche assenti o troppo poco gioco)"
    if not sopra_soglia:
        return False, (f"dominio {quota}% su volume {volume} "
                       f"(servono {SOGLIA_QUOTA_DOMINIO_AUTO_PREFERITI}% e volume "
                       f"{VOLUME_MINIMO_DOMINIO_AUTO_PREFERITI})")
    if cicli < CICLI_DOMINIO_PER_AUTO_PREFERITI:
        return False, (f"dominio {quota}% da {cicli} cicli "
                       f"(ne servono {CICLI_DOMINIO_PER_AUTO_PREFERITI} di fila)")

    chi = "la squadra di casa" if dominio["lato"] == 0 else "la squadra ospite"
    coda = {"sotto": "e sta perdendo", "bloccata": "e non segna", "avanti": "ed e' avanti"}[dominio["situazione"]]
    return True, f"dominio: {chi} comanda {quota}% {coda}, stabile da {cicli} cicli"


def leggi_shadow_log(percorso_file):
    """Legge uno shadow-log (una riga JSON per evento) contando anche le righe illeggibili.

    Ritorna (dati, totale_righe, righe_malformate). Una riga corrotta non fa fallire la lettura -
    si conta e si tira dritto - perche' il file viene appeso in produzione e un crash a meta'
    scrittura non deve rendere illeggibile tutto lo storico. Gli errori di apertura invece si
    propagano: il chiamante li mostra in chat col nome del file."""
    dati = []
    totale = malformate = 0
    with open(percorso_file, "r") as f:
        for riga in f:
            riga = riga.strip()
            if not riga:
                continue
            totale += 1
            try:
                dati.append(json.loads(riga))
            except Exception:
                malformate += 1
    return dati, totale, malformate


def _appendi_shadow_log(percorso_file, riga):
    """Appende una riga JSON a un file di shadow-log (raccolta dati per analisi offline, nessun
    effetto sul comportamento del bot). Helper condiviso da tutti gli shadow-log del bot, per non
    duplicare lo stesso apri-scrivi-gestisci-errore in ognuno."""
    try:
        with open(percorso_file, "a") as f:
            f.write(json.dumps(riga) + "\n")
    except Exception as e:
        print(f"Errore scrittura shadow log ({percorso_file}): {e}", flush=True)


def registra_shadow_log_auto_preferiti(fixture_id, home, away, league_name, league_country, minuto,
                                        tiri_totali, tiri_porta, corner, tiri_area, xg_casa, xg_ospite,
                                        gol_totali, scattato):
    """Registra le statistiche reali osservate al momento della valutazione auto-preferiti
    (scattata o finestra chiusa senza scattare). Puramente per analisi offline successiva."""
    _appendi_shadow_log(SHADOW_LOG_AUTO_PREFERITI_FILE, {
        "timestamp": time.time(),
        "fixture_id": fixture_id,
        "home": home,
        "away": away,
        "league": league_name,
        "league_country": league_country,
        "minuto": minuto,
        "tiri_totali": tiri_totali,
        "tiri_porta": tiri_porta,
        "corner": corner,
        "tiri_area": tiri_area,
        "xg_casa": xg_casa,
        "xg_ospite": xg_ospite,
        "gol_totali": gol_totali,
        "auto_preferiti_scattato": scattato,
    })


def registra_shadow_log_auto_preferiti_dominio(fixture_id, home, away, league_name, league_country,
                                               minuto, score_home, score_away, quota, volume,
                                               situazione, cicli, scattato, motivo):
    """Una riga per la rotta dominio: com'era la partita al momento del verdetto E quanto in alto
    era arrivato il dominio nel corso della gara (il picco tenuto in stato_partite).

    Il picco e' la parte che serve davvero a tarare: le soglie 78%/16 sono stime a occhio, e per
    sostituirle con percentili veri bisogna sapere a che quota e' arrivata ogni partita, non a che
    quota si trovava all'istante in cui si e' guardato. Con quota_max, il volume in quel momento e
    la striscia consecutiva piu' lunga si ricalcola offline l'esito di qualunque coppia di soglie."""
    stato = stato_partite.get(fixture_id, {})
    _appendi_shadow_log(SHADOW_LOG_AUTO_PREFERITI_DOMINIO_FILE, {
        "timestamp": time.time(),
        "fixture_id": fixture_id,
        "home": home,
        "away": away,
        "league": league_name,
        "league_country": league_country,
        "minuto": minuto,
        "score_home": score_home,
        "score_away": score_away,
        "quota": quota,
        "volume": volume,
        "situazione": situazione,
        "cicli_consecutivi": cicli,
        "quota_max": stato.get("dominio_quota_max"),
        "volume_al_max": stato.get("dominio_volume_al_max"),
        "minuto_al_max": stato.get("dominio_minuto_al_max"),
        "situazione_al_max": stato.get("dominio_situazione_al_max"),
        "cicli_consecutivi_max": stato.get("cicli_dominio_sopra_soglia_max", 0),
        "soglia_quota": SOGLIA_QUOTA_DOMINIO_AUTO_PREFERITI,
        "soglia_volume": VOLUME_MINIMO_DOMINIO_AUTO_PREFERITI,
        "soglia_cicli": CICLI_DOMINIO_PER_AUTO_PREFERITI,
        "promozione_attiva": AUTO_PREFERITI_DOMINIO_ATTIVO,
        "auto_preferiti_dominio_scattato": scattato,
        "motivo": motivo,
    })


def registra_shadow_log_valore_snapshot(fixture_id, home, away, minuto, score_home, score_away,
                                         probabilita_no_vig, stats_15min):
    """Snapshot ad una notifica live: probabilità no-vig del mercato pre-match + statistiche
    ultimi 15 min già calcolate altrove (nessun ricalcolo). Da incrociare offline col risultato
    finale (registra_shadow_log_valore_risultato) per capire, con dati reali, se le statistiche
    live aggiungono potere predittivo alla sola quota pre-match - prima di costruire qualunque
    soglia o semaforo su questo (vedi Fase 2)."""
    _appendi_shadow_log(SHADOW_LOG_VALORE_FILE, {
        "tipo": "snapshot",
        "timestamp": time.time(),
        "fixture_id": fixture_id,
        "home": home,
        "away": away,
        "minuto": minuto,
        "score_home": score_home,
        "score_away": score_away,
        "probabilita_no_vig": probabilita_no_vig,
        "stats_15min": stats_15min,
    })


def registra_shadow_log_valore_risultato(fixture_id, score_home, score_away):
    """Riga "risultato_finale" per lo stesso fixture_id degli snapshot sopra, scritta una sola
    volta quando la partita termina (stesso punto in cui il bot manda già la notifica di
    risultato finale)."""
    esito = "1" if score_home > score_away else ("2" if score_away > score_home else "X")
    _appendi_shadow_log(SHADOW_LOG_VALORE_FILE, {
        "tipo": "risultato_finale",
        "timestamp": time.time(),
        "fixture_id": fixture_id,
        "score_home": score_home,
        "score_away": score_away,
        "esito": esito,
    })


def registra_shadow_log_strategie_snapshot(fixture_id, home, away, minuto, score_home, score_away, segnali):
    """Fotografia periodica di quali delle sei strategie scattano in questo momento sulla
    partita (lista vuota se nessuna) - vedi commento su SHADOW_LOG_STRATEGIE_FILE per il perché
    si registra anche quando non scatta nulla."""
    _appendi_shadow_log(SHADOW_LOG_STRATEGIE_FILE, {
        "tipo": "snapshot",
        "timestamp": time.time(),
        "fixture_id": fixture_id,
        "home": home,
        "away": away,
        "minuto": minuto,
        "score_home": score_home,
        "score_away": score_away,
        "segnali": segnali,
    })


def registra_shadow_log_strategie_risultato(fixture_id, score_home, score_away, goals):
    """Riga "risultato_finale" per lo stesso fixture_id degli snapshot sopra, con anche i gol
    della partita (minuto + squadra) così l'analisi offline può controllare, per ogni segnale
    registrato, se e quando è arrivato il gol successivo - non solo il risultato finale."""
    _appendi_shadow_log(SHADOW_LOG_STRATEGIE_FILE, {
        "tipo": "risultato_finale",
        "timestamp": time.time(),
        "fixture_id": fixture_id,
        "score_home": score_home,
        "score_away": score_away,
        "goals": goals,
    })


def recupera_esito_finale_fixture(fixture_id):
    """Stato e punteggio finale di UNA partita, con una chiamata mirata /fixtures?id=X.

    Serve per le partite sparite dall'elenco live: quell'endpoint smette di restituirle appena
    finiscono, quindi chiederlo esplicitamente e' l'unico modo per sapere com'e' andata.

    Ritorna (conclusa, score_home, score_away), oppure None se la CHIAMATA e' fallita. I due casi
    vanno tenuti distinti: "non conclusa" e' una risposta valida (partita sospesa, rinviata,
    fixture sconosciuto) e non c'e' niente da registrare, mentre una chiamata fallita merita un
    altro tentativo - confonderle butterebbe via il campione al primo rate-limit."""
    if not API_FOOTBALL_KEY:
        return None
    data, _, _ = get_api_football(
        "https://v3.football.api-sports.io/fixtures", {"id": fixture_id}, timeout=15,
        contesto=f"recupera_esito_finale_fixture({fixture_id})")
    if data is None:
        return None
    risposta = data.get("response") or []
    if not risposta:
        return False, None, None
    fixture_info = risposta[0].get("fixture") or {}
    if (fixture_info.get("status") or {}).get("short") not in STATI_PARTITA_CONCLUSA:
        return False, None, None
    goals = risposta[0].get("goals") or {}
    score_home, score_away = goals.get("home"), goals.get("away")
    if score_home is None or score_away is None:
        return False, None, None
    return True, score_home, score_away


def _shadow_log_ha_snapshot_aperti(fixture_id):
    """True se questa partita ha snapshot registrati ma non ancora il suo risultato finale."""
    stato = stato_partite.get(fixture_id, {})
    if stato.get("notified_final"):
        return False  # gia' chiusa dal ramo di fine partita dentro processa_partita
    return bool(stato.get("ultimo_snapshot_valore") or stato.get("ultimo_snapshot_strategie"))


def chiudi_shadow_log_partite_sparite(fixture_ids):
    """Scrive il "risultato_finale" delle partite appena sparite dal feed live.

    L'esito veniva registrato SOLO dentro processa_partita, nel ramo
    `status_short in STATI_PARTITA_CONCLUSA`. Ma processa_partita vede soltanto cio' che
    l'endpoint live restituisce, e una partita finita da quell'elenco sparisce e basta: quel ramo
    in produzione non scattava quasi mai. I numeri del 23/08 non lasciano dubbi - 642 partite con
    snapshot e ZERO risultati finali nello shadow-log valore, con "RISULTATO FINALE" mai comparso
    in due giorni interi di log mentre "Partite terminate rimosse" compariva decine di volte.

    Senza l'esito gli snapshot non valgono nulla: esistono proprio per essere incrociati con come
    la partita e' finita davvero. Qui il cerchio si chiude, subito prima che
    pulisci_partite_terminate cancelli lo stato, e solo per le partite che hanno snapshot aperti.

    Ritorna gli id da NON cancellare in questo giro: quelli oltre il tetto di chiamate e quelli la
    cui chiamata e' fallita ma ha ancora tentativi disponibili. Restano in stato_partite e si
    riprovano al ciclo dopo, invece di sparire portandosi via il campione."""
    if not CHIUSURA_SHADOW_LOG_PARTITE_SPARITE_ATTIVA:
        return set()

    da_chiudere = [fid for fid in fixture_ids if _shadow_log_ha_snapshot_aperti(fid)]
    if not da_chiudere:
        return set()

    rimandate, chiuse = set(), 0
    for indice, fid in enumerate(da_chiudere):
        if indice >= MAX_CHIUSURE_SHADOW_LOG_PER_CICLO:
            rimandate.update(da_chiudere[indice:])
            break
        if indice:
            time.sleep(1)  # stesso ritmo del loop live: mai piu' chiamate nello stesso secondo
        esito = recupera_esito_finale_fixture(fid)
        if esito is None:
            stato = stato_partite.setdefault(fid, {})
            tentativi = stato.get("tentativi_chiusura_shadow_log", 0) + 1
            stato["tentativi_chiusura_shadow_log"] = tentativi
            if tentativi < TENTATIVI_MAX_CHIUSURA_SHADOW_LOG:
                rimandate.add(fid)
            else:
                log(f"    Shadow-log: rinuncio a chiudere la partita {fid} dopo {tentativi} tentativi falliti")
            continue
        conclusa, score_home, score_away = esito
        if not conclusa:
            continue  # sospesa, rinviata o sconosciuta: nessun esito reale da registrare
        registra_shadow_log_valore_risultato(fid, score_home, score_away)
        # I gol servono allo shadow-log strategie per sapere QUANDO e' arrivato il gol dopo un
        # segnale, non solo com'e' finita: e' il dato per cui quel file esiste, e vale la seconda
        # chiamata. Se gli eventi non arrivano si registra lo stesso con la lista vuota - il
        # risultato finale e' comunque meglio di un altro snapshot orfano.
        eventi = fetch_fixture_events(fid)
        registra_shadow_log_strategie_risultato(
            fid, score_home, score_away, extract_goals(eventi) if eventi else [])
        chiuse += 1

    if chiuse or rimandate:
        log(f"Shadow-log chiusi a fine partita: {chiuse}"
            + (f" ({len(rimandate)} rimandati al prossimo ciclo)" if rimandate else ""))
    return rimandate


def classifica_cambio_punteggio(fixture_id, score_home, score_away):
    """Confronta il punteggio appena letto dall'API con quello dell'ultimo ciclo per questa
    partita, e ritorna (gol_appena_segnato, punteggio_corretto_al_ribasso).

    Un punteggio può cambiare anche ALL'INDIETRO: gol annullato al VAR, oppure una correzione
    dell'API che aveva attribuito un gol per sbaglio. Prima qui bastava che il punteggio fosse
    "diverso" da quello di prima, quindi anche togliere un gol veniva trattato come segnarlo - e
    un gol è un evento forzato, che scavalca modalità essenziale e filtro goleada e manda una
    notifica intera. Visto in produzione il 17/08 su due partite: "1-0 -> 0-0" alle 16:15 (lo
    stesso gol dato alle 16:11 e poi tolto) e "0-2 -> 0-1" alle 17:12.

    Un gol c'è stato solo se il punteggio di una delle due squadre è SALITO. Se è solo sceso è una
    correzione: lo stato viene aggiornato lo stesso dal chiamante (il risultato mostrato resta
    quello vero), ma non parte nessuna notifica di gol.

    Alla prima lettura di una partita non c'è niente da confrontare: nessun gol, nessuna
    correzione - altrimenti ogni partita già in corso quando il bot la vede per la prima volta
    (o dopo un riavvio, che azzera stato_partite) genererebbe un gol inventato."""
    stato_precedente = stato_partite.get(fixture_id, {})
    if not stato_precedente:
        return False, False
    prev_score_home = stato_precedente.get("score_home", score_home)
    prev_score_away = stato_precedente.get("score_away", score_away)
    gol_appena_segnato = score_home > prev_score_home or score_away > prev_score_away
    corretto_al_ribasso = (not gol_appena_segnato
                           and (score_home < prev_score_home or score_away < prev_score_away))
    return gol_appena_segnato, corretto_al_ribasso


# Perche' l'ultima valutazione di deve_notificare() e' finita come e' finita, per fixture.
#
# Serve perche' il log diceva solo "-> Skip": con gate del dominio, freno per blocco, goleada,
# modalita' essenziale, soglie dei preferiti e "niente e' cambiato", quel Skip puo' voler dire sei
# cose diverse e da fuori sono indistinguibili. Con le notifiche diventate selettive la domanda
# "perche' questa partita non e' arrivata in chat?" e' quella che si fa piu' spesso, e la risposta
# doveva essere leggibile nei log invece di richiedere di rileggere il codice.
#
# Registrato anche quando la notifica PARTE: sapere quale regola l'ha fatta scattare e' il dato che
# serve per tarare le soglie (quale regola lavora davvero, quale non scatta mai).
MOTIVO_VALUTAZIONE_NOTIFICA = {}


def _verdetto_notifica(fixture_id, esito, motivo):
    """Registra il perche' e restituisce l'esito, cosi' ogni uscita di deve_notificare() resta una
    riga sola e non si puo' aggiungere un ramo dimenticando di spiegarlo."""
    MOTIVO_VALUTAZIONE_NOTIFICA[fixture_id] = motivo
    return esito


def motivo_valutazione_notifica(fixture_id):
    return MOTIVO_VALUTAZIONE_NOTIFICA.get(fixture_id, "motivo non registrato")


def deve_notificare(fixture_id, tiri_casa, tiri_ospite, minuto, delta_stats=None, gol_appena_segnato=False, recupero_lungo=False, score_home=None, score_away=None,
                    current_stats=None):
    # SILENZIO FINCHE' L'API NON PUBBLICA LE STATISTICHE DI QUESTA PARTITA.
    #
    # Sta prima di tutto il resto, gol compresi, ed e' l'unica cosa che passa davanti a un evento
    # forzato: senza tiri, tiri in porta, corner e area la notifica dice "Statistiche: N/D - N/D" e
    # non puo' far scattare nessuna strategia. E' una riga di risultato, che si trova ovunque, non
    # il motivo per cui questo bot esiste.
    #
    # Il 23/08 la chat si e' riempita cosi': Bahlinger SC-Magdeburg 0-1 all'8', BSC Young Boys-Vaduz
    # 4-2 all'89', tutte le partite di DFB Pokal fra dilettanti e squadre di Bundesliga - notifiche
    # arrivate solo perche' un gol passa sempre, e tutte senza un solo dato utile dentro.
    #
    # "Nascosta" non vuol dire "persa": la partita resta seguita e continua ad alimentare gli
    # shadow-log (valore, strategie, auto-preferiti), che girano piu' in alto in processa_partita e
    # non passano di qui. Appena l'API pubblica le prime statistiche la partita torna a notificare
    # normalmente, e i gol nel frattempo non sono spariti - sono nel testo della prima notifica
    # utile, con il minuto e il marcatore.
    if SILENZIO_SENZA_STATISTICHE_ATTIVO and not current_stats:
        return _verdetto_notifica(
            fixture_id, False,
            "statistiche non pubblicate: partita seguita in silenzio, solo shadow-log")

    # GOLEADA, PRIMA ANCORA DEL GOL.
    #
    # Oltre SOGLIA_GOLEADA_STOP_NOTIFICHE gol di scarto la partita perde valore per il trading e si
    # smette di notificarla del tutto, preferiti compresi.
    #
    # Il 23/08 PSV Eindhoven-Groningen e' arrivata in chat sul 5-1: il gol del 56' aveva appena
    # portato lo scarto da 3 a 4, e i gol passavano davanti a questo controllo. La regola scritta
    # allora diceva che quel gol e' "l'evento che ha creato la goleada" e quindi meritava di
    # passare. Per un bot di trading non regge: a quattro gol di scarto la partita e' decisa, e
    # sapere quale gol l'ha decisa non cambia niente di quello che si puo' fare. Con
    # GOLEADA_BLOCCA_ANCHE_I_GOL spento si torna al comportamento di prima e il gol passa.
    #
    # Il pareggio NON ha un trattamento speciale: segue le regole di sempre piu' sotto. Il risultato
    # finale arriva comunque a fine partita: e' un messaggio a parte e non passa di qui.
    if score_home is not None and score_away is not None:
        diff_gol = abs(score_home - score_away)
        evento_forzato = gol_appena_segnato or recupero_lungo
        if diff_gol > SOGLIA_GOLEADA_STOP_NOTIFICHE and (GOLEADA_BLOCCA_ANCHE_I_GOL
                                                         or not evento_forzato):
            return _verdetto_notifica(
                fixture_id, False,
                f"goleada: {diff_gol} gol di scarto (oltre {SOGLIA_GOLEADA_STOP_NOTIFICHE})"
                + (", gol compresi" if evento_forzato else ""))

    # PRIORITÀ MASSIMA: gol appena segnato o recupero lungo appena concluso -> notifica sempre,
    # anche in modalità essenziale (sono esattamente gli eventi che quella modalità vuole lasciar
    # passare).
    if gol_appena_segnato or recupero_lungo:
        return _verdetto_notifica(
            fixture_id, True,
            "gol o evento forzato" if gol_appena_segnato else "recupero appena concluso")

    # Modalità essenziale: tutto il resto (soglie tiri, momentum, refresh forzato, preferiti) è
    # sospeso finché non torna la modalità completa.
    if MODALITA_NOTIFICHE.get("essenziale"):
        return _verdetto_notifica(fixture_id, False, "modalità essenziale attiva")

    stato = stato_partite.get(fixture_id, {})
    ultima_casa = stato.get("tiri_casa", -1)
    ultima_ospite = stato.get("tiri_ospite", -1)
    ultimo_invio = stato.get("timestamp_notifica", 0)

    if tiri_casa == ultima_casa and tiri_ospite == ultima_ospite:
        return _verdetto_notifica(
            fixture_id, False,
            f"nessun tiro cambiato dall'ultimo controllo (fermi a {tiri_casa}-{tiri_ospite})")

    # Preferiti: molto più reattivi delle altre partite (bypassano le soglie sotto), ma non per il
    # minimo indivisibile: serve un cambiamento comunque percepibile dall'ultimo invio. Con il
    # controllo ogni 60s (INTERVALLO_CICLO_MOMENTUM) un singolo tiro isolato non deve più bastare
    # da solo a generare una notifica ogni minuto: la stessa soglia SOGLIA_MIN_CAMBIO_PREFERITI si
    # applica in modo uniforme a tiri totali, tiri in porta e corner.
    if str(fixture_id) in FAVORITE_MATCHES:
        if ultima_casa < 0:
            # prima notifica per questa partita preferita: sempre subito
            return _verdetto_notifica(fixture_id, True, "preferita: prima notifica")
        cambio_tiri_totali = (tiri_casa + tiri_ospite) - (ultima_casa + ultima_ospite)
        if cambio_tiri_totali >= SOGLIA_MIN_CAMBIO_PREFERITI:
            return _verdetto_notifica(
                fixture_id, True, f"preferita: +{cambio_tiri_totali} tiri dall'ultimo invio")
        if delta_stats:
            d_porta = delta_stats.get("Tiri in porta", (0, 0))
            d_corner = delta_stats.get("Corner", (0, 0))
            if (d_porta[0] + d_porta[1]) >= SOGLIA_MIN_CAMBIO_PREFERITI:
                return _verdetto_notifica(
                    fixture_id, True, f"preferita: +{d_porta[0] + d_porta[1]} tiri in porta")
            if (d_corner[0] + d_corner[1]) >= SOGLIA_MIN_CAMBIO_PREFERITI:
                return _verdetto_notifica(
                    fixture_id, True, f"preferita: +{d_corner[0] + d_corner[1]} corner")
            # Salto di ritmo: i controlli qui sopra misurano il cambiamento DALL'ULTIMA NOTIFICA,
            # quindi una fase concitata fatta di tanti piccoli incrementi (un tiro per ciclo) non
            # li supera mai, pur essendo il momento in cui la partita merita attenzione. Questo
            # guarda il totale del blocco di 15 minuti, e scatta al massimo una volta per blocco:
            # se in questo blocco una notifica è già partita - per un gol, per le soglie sopra, per
            # qualunque motivo - non se ne aggiunge una seconda.
            if stato.get("blocco_ultima_notifica") != _blocco_minuto(minuto):
                tiri_blocco = sum(delta_stats.get("Tiri totali", (0, 0)))
                porta_blocco = d_porta[0] + d_porta[1]
                if (tiri_blocco >= SOGLIA_RITMO_NOTIFICA_PREFERITI
                        or porta_blocco >= SOGLIA_PORTA_RITMO_NOTIFICA_PREFERITI):
                    return _verdetto_notifica(
                        fixture_id, True,
                        f"preferita: salto di ritmo nel blocco ({tiri_blocco} tiri, "
                        f"{porta_blocco} in porta)")
        return _verdetto_notifica(
            fixture_id, False, "preferita: cambiamento sotto le soglie reattive")

    # GATE DOMINIO (solo partite NON preferite: sopra si e' gia' tornati per quelle).
    #
    # Le quattro regole qui sotto guardano il VOLUME: quanti tiri in tutto, quanta differenza,
    # quanto ritmo negli ultimi 15 minuti. Prese da sole rispondono a "si sta giocando?", che non e'
    # la domanda utile: un 10-9 di tiri le supera tutte pur essendo la partita da cui si ricava
    # meno di ogni altra, perche' il gioco e' distribuito equamente e non indica da che parte
    # stare. Il gate chiede prima "sta comandando qualcuno?", e solo dopo lascia che le regole
    # decidano QUANDO aggiornare - da criterio d'ingresso diventano cadenza.
    #
    # Posizione deliberata, non casuale: sta DOPO il gol/evento forzato (che passa sempre, gate o
    # meno), DOPO la modalita' essenziale e DOPO la goleada, cosi' eredita quelle tre sospensioni
    # senza riscriverle, e DOPO il ramo dei preferiti, che resta intoccato con le sue soglie
    # reattive - una partita entrata nei preferiti e' gia' stata giudicata degna, non deve
    # ripresentare le credenziali ad ogni ciclo.
    #
    # Quando le statistiche mancano del tutto calcola_dominio() ritorna None e il gate chiude, ma
    # non e' un buco nuovo: senza statistiche tiri e delta valgono zero, e le quattro regole qui
    # sotto non scattavano gia' prima (diff 0, totali 0, delta 0). I gol continuano ad arrivare
    # comunque, perche' passano molto piu' in alto.
    # Due strade per qualificare la partita, non una: il dominio sul campo (sotto) oppure la
    # favorita di mercato che non sta vincendo (vedi favorita_che_non_vince). La seconda scavalca il
    # gate perche' risponde a una domanda diversa - non "chi comanda" ma "chi doveva vincere" - e
    # una partita come City-Bournemouth 0-1, dominio 62%, non passerebbe mai dalla prima.
    favorita_ko = favorita_che_non_vince(fixture_id, score_home, score_away, minuto)

    if DOMINIO_GATE_NOTIFICHE_ATTIVO and not favorita_ko:
        dominio = calcola_dominio(current_stats, score_home, score_away) if current_stats else None
        if not dominio:
            return _verdetto_notifica(
                fixture_id, False,
                f"gate dominio: {motivo_assenza_dominio(current_stats)}")
        if dominio["quota"] < SOGLIA_QUOTA_DOMINIO_NOTIFICA:
            return _verdetto_notifica(
                fixture_id, False,
                f"gate dominio: {dominio['quota']}% sotto la soglia "
                f"{SOGLIA_QUOTA_DOMINIO_NOTIFICA}%")

    # UN AGGIORNAMENTO DI ROUTINE PER BLOCCO DI 15 MINUTI.
    #
    # Le quattro regole qui sotto guardano lo stato ASSOLUTO, non il cambiamento: la Regola 1
    # ("differenza tiri >= 3") e' vera per sempre appena una partita si sbilancia. Dinamo
    # Zagreb-Viking il 18/08 era 19-5 di tiri, differenza 14: superava la soglia ad ogni singolo
    # ciclo, e la stessa partita tornava in chat ogni 3 minuti (22:41, 22:44, 22:47...).
    #
    # Il gate del dominio ha reso la cosa piu' evidente invece di causarla: filtrando alle sole
    # partite dominate, lascia passare esattamente quelle in cui la differenza tiri e' alta - cioe'
    # quelle che la Regola 1 ripresenta all'infinito. Prima il rumore era distribuito su piu'
    # partite, ora si concentra sulle stesse due o tre.
    #
    # Il freno riusa il blocco di 15 minuti gia' esistente per il salto di ritmo dei preferiti: un
    # aggiornamento di routine per blocco e per partita. Gli eventi che devono farsi sentire - gol,
    # rosso, rigore, recupero - non passano di qui: hanno gia' restituito True molto piu' in alto.
    if UN_AGGIORNAMENTO_PER_BLOCCO_ATTIVO and stato.get("blocco_ultima_notifica") == _blocco_minuto(minuto):
        blocco = _blocco_minuto(minuto)
        return _verdetto_notifica(
            fixture_id, False,
            f"già inviato un aggiornamento nel blocco {blocco}-{blocco + 14}'")

    # La favorita che non vince notifica da sola, senza passare dalle quattro regole qui sotto:
    # quelle misurano il ritmo, e il punto qui non e' il ritmo. City-Bournemouth al 38' aveva
    # differenza tiri 2 e delta quasi piatto - nessuna delle quattro sarebbe scattata, e la
    # notizia ("la favorita e' sotto") sarebbe andata persa lo stesso.
    #
    # Passa comunque dal freno per blocco qui sopra: la notizia e' una, non va ripetuta ogni tre
    # minuti per il resto della partita.
    if favorita_ko:
        lato, probabilita = favorita_ko
        chi = "in casa" if lato == "casa" else "in trasferta"
        situazione = "sotto" if (score_home < score_away if lato == "casa" else score_away < score_home) else "in parità"
        return _verdetto_notifica(
            fixture_id, True,
            f"favorita {chi} al {probabilita * 100:.0f}% ma {situazione} al {minuto}'")

    tiri_totali = tiri_casa + tiri_ospite
    diff = abs(tiri_casa - tiri_ospite)
    tempo_passato = time.time() - ultimo_invio

    # Regola 1: Differenza tiri significativa
    if diff >= DIFF_TIRI_SOGLIA:
        return _verdetto_notifica(
            fixture_id, True, f"Regola 1: differenza tiri {diff} (soglia {DIFF_TIRI_SOGLIA})")

    # Regola 2: Partita molto attiva nei primi 25 min
    if minuto <= MINUTI_ATTIVA and tiri_totali >= TIRI_TOTALI_ATTIVA:
        return _verdetto_notifica(
            fixture_id, True, f"Regola 2: {tiri_totali} tiri entro il {MINUTI_ATTIVA}'")

    # Regola 3: Forzata ogni 30 min se abbastanza tiri.
    #
    # Alla prima notifica di una partita, "timestamp_notifica" non e' mai stato scritto e il default
    # e' 0 (epoch 1970). Senza la guardia esplicita "ultimo_invio > 0", tempo_passato diventa
    # time.time() - 0 = ~1.7 mld di secondi e la Regola 3 scatta sempre con un messaggio nonsense
    # tipo "refresh dopo 29793349 min dall'ultimo invio" (visto in produzione). Se non c'e' un
    # invio precedente non c'e' nemmeno un "refresh": la prima notifica per una partita non-
    # preferita deve arrivare da Regola 1/2/4 (differenza tiri, prima fase attiva, momentum).
    if ultimo_invio > 0 and tempo_passato >= INTERVALLO_FORZATO and tiri_totali >= 4:
        return _verdetto_notifica(
            fixture_id, True,
            f"Regola 3: refresh dopo {int(tempo_passato / 60)} min dall'ultimo invio")

    # Regola 4: MOMENTUM - ritmo recente negli ultimi 15 min
    # Cattura partite che si svegliano nel secondo tempo anche se totali bassi
    if delta_stats:
        d_tiri = delta_stats.get("Tiri totali", (0, 0))
        d_porta = delta_stats.get("Tiri in porta", (0, 0))
        d_corner = delta_stats.get("Corner", (0, 0))
        if (d_porta[0] + d_porta[1]) >= MOMENTUM_TIRI_IN_PORTA:
            return _verdetto_notifica(
                fixture_id, True,
                f"Regola 4: +{d_porta[0] + d_porta[1]} tiri in porta negli ultimi 15 min")
        if (d_tiri[0] + d_tiri[1]) >= MOMENTUM_TIRI_TOTALI:
            return _verdetto_notifica(
                fixture_id, True,
                f"Regola 4: +{d_tiri[0] + d_tiri[1]} tiri negli ultimi 15 min")
        if (d_corner[0] + d_corner[1]) >= MOMENTUM_CORNER:
            return _verdetto_notifica(
                fixture_id, True,
                f"Regola 4: +{d_corner[0] + d_corner[1]} corner negli ultimi 15 min")

    return _verdetto_notifica(
        fixture_id, False,
        f"nessuna regola soddisfatta (differenza tiri {diff}, totali {tiri_totali})")


# =============================================================================
# PROCESSA SINGOLA PARTITA
# =============================================================================
def processa_partita(fixture, notifiche_attive=True):
    """Elabora una partita live già filtrata dal chiamante (main loop): qui non si ricontrolla più
    campionato_valido(), il filtro è fatto una sola volta a monte per evitare il doppio controllo
    e per far sì che lo sleep tra una partita e l'altra scatti solo per partite valide.

    notifiche_attive: False fuori dalla fascia oraria configurata (vedi dentro_orario_attivo) -
    statistiche, quote e shadow-log continuano comunque ad essere raccolti, solo l'invio delle
    notifiche Telegram viene saltato."""
    try:
        fixture_id = fixture["fixture"]["id"]
        league = fixture.get("league", {})
        league_name = league.get("name", "")
        league_country = league.get("country", "")
        # Giornata di campionato dichiarata dall'API (es. "Regular Season - 1"): è il turno vero,
        # usato per contare le giornate senza statistiche (vedi registra_giornata_statistiche).
        league_round = league.get("round", "")

        home = fixture["teams"]["home"]["name"]
        away = fixture["teams"]["away"]["name"]
        score_home = fixture["goals"]["home"] or 0
        score_away = fixture["goals"]["away"] or 0
        elapsed_raw = fixture["fixture"]["status"].get("elapsed")
        minuto = elapsed_raw or 0
        status_short = fixture["fixture"]["status"].get("short", "LIVE")
        extra_corrente = fixture["fixture"]["status"].get("extra")

        log(f"  ✅ {home} vs {away} - {minuto}' ({league_name})")

        # Se il minuto segnalato ora dall'API è drasticamente PIÙ BASSO dell'ultimo minuto visto per
        # questo stesso fixture_id (es. 124' -> 0', visto in produzione su una partita verosimilmente
        # già conclusa ai supplementari/rigori), è un segnale che l'API ha corretto/resettato i dati
        # di questa partita, non un normale ritardo del ciclo (quello sposta il minuto di poco, non di
        # decine di minuti all'indietro). Continuare ad accumulare sullo stato vecchio (history,
        # snapshot, cartellini già notificati...) produrrebbe confronti e notifiche senza senso - es.
        # un "recupero concluso" fasullo o un grafico che mischia dati di due fasi diverse della
        # partita. Si azzera lo stato per questo fixture_id e si riparte puliti, come se fosse la
        # prima volta che lo si vede (stessa logica poco più sotto per un fixture_id mai visto).
        #
        # IMPORTANTE: il confronto usa elapsed_raw (non "minuto", già convertito con "or 0"), perché
        # l'API a volte non riporta affatto un "elapsed" numerico - es. status "HT", dove il tempo è
        # fermo - e in quel caso "elapsed" è None, non un vero 0. Confondere i due casi (bug trovato
        # in produzione dopo un Manual Deploy) faceva scattare il reset SEMPRE che una partita fosse
        # ferma all'intervallo proprio nel momento di un riavvio del bot, cancellando storico e
        # confronto 1°T/2°T di partite perfettamente in corso, non solo il caso reale originale
        # (un vero "0" esplicito restituito dall'API su una partita già conclusa).
        stato_esistente = stato_partite.get(fixture_id)
        reset_appena_avvenuto = False
        if stato_esistente is not None:
            minuto_precedente = stato_esistente.get("last_minute")
            if elapsed_raw is not None and minuto_precedente is not None and elapsed_raw < minuto_precedente - 20:
                log(f"    ⚠️ Minuto retrocesso da {minuto_precedente}' a {elapsed_raw}' per {home}-{away}: "
                    f"reset dello stato accumulato (probabile correzione dati dell'API)")
                stato_partite[fixture_id] = {}
                reset_appena_avvenuto = True
                # Il backup (vedi sotto) deve sparire insieme allo stato: contiene lo storico della
                # partita PRIMA della correzione dati, esattamente quello che il reset vuole
                # abbandonare. Senza questo svuotamento, il ripristino da backup qui sotto
                # rimetterebbe subito lo storico vecchio, vanificando il reset appena fatto (bug
                # trovato con un controllo incrociato tra le due fix, mai capitato finora in
                # produzione solo perché non si erano ancora sovrapposte).
                if str(fixture_id) in BACKUP_HISTORY_MOMENTUM:
                    del BACKUP_HISTORY_MOMENTUM[str(fixture_id)]
                    salva_backup_history_momentum(BACKUP_HISTORY_MOMENTUM)

        # Ripristino da backup: se lo storico momentum di questa partita risulta vuoto/più corto di
        # quello salvato nel backup indipendente (vedi BACKUP_HISTORY_MOMENTUM sopra) - tipicamente
        # dopo un riavvio che ha perso stato_partite ma non il backup - lo si ripristina da lì invece
        # di ripartire da zero. MAI subito dopo un reset per regresso minuto appena avvenuto in
        # questo stesso ciclo (vedi sopra): quel reset esiste apposta per abbandonare uno storico che
        # non corrisponde più alla partita corretta, e il backup è stato svuotato insieme per lo
        # stesso motivo. Non fa mai perdere punti: si applica solo quando il backup è STRETTAMENTE
        # più lungo dello storico attuale.
        if not reset_appena_avvenuto:
            backup_fixture = BACKUP_HISTORY_MOMENTUM.get(str(fixture_id))
            if backup_fixture:
                history_attuale = stato_partite.get(fixture_id, {}).get("history", [])
                if len(backup_fixture) > len(history_attuale):
                    stato_partite.setdefault(fixture_id, {})["history"] = list(backup_fixture)
                    log(f"    ♻️ Storico momentum ripristinato dal backup per {home}-{away}: "
                        f"{len(backup_fixture)} punti recuperati")

        stato_precedente = stato_partite.get(fixture_id, {})
        prev_score_home = stato_precedente.get("score_home", score_home)
        prev_score_away = stato_precedente.get("score_away", score_away)
        gol_appena_segnato, punteggio_corretto_al_ribasso = classifica_cambio_punteggio(
            fixture_id, score_home, score_away)
        if gol_appena_segnato:
            log(f"    ⚽🚨 GOL RILEVATO! Punteggio cambiato: {prev_score_home}-{prev_score_away} -> {score_home}-{score_away}")
        elif punteggio_corretto_al_ribasso:
            # Nessuna notifica: il risultato mostrato resta comunque aggiornato (lo stato viene
            # riscritto poco più sotto), ma dire "gol" per un gol tolto è il contrario di quello
            # che è successo.
            log(f"    ↩️ Punteggio corretto all'indietro: {prev_score_home}-{prev_score_away} -> "
                f"{score_home}-{score_away} (gol annullato o correzione dell'API, nessuna notifica)")

        # Minuti di recupero: si tiene il valore più recente annunciato dall'API per il tempo in
        # corso, e si rileva il momento in cui il tempo finisce (1H->altro, 2H->altro) per poterlo
        # segnalare come "appena concluso" con una notifica dedicata se è abbastanza lungo.
        prev_status_short = stato_precedente.get("last_status_short")
        recupero_1h = stato_precedente.get("recupero_1h")
        recupero_2h = stato_precedente.get("recupero_2h")
        if status_short == "1H" and extra_corrente is not None:
            recupero_1h = extra_corrente
        elif status_short == "2H" and extra_corrente is not None:
            recupero_2h = extra_corrente

        recupero_appena_concluso = None
        if prev_status_short == "1H" and status_short != "1H" and recupero_1h is not None:
            recupero_appena_concluso = ("1° tempo", recupero_1h)
        elif prev_status_short == "2H" and status_short != "2H" and recupero_2h is not None:
            recupero_appena_concluso = ("2° tempo", recupero_2h)

        # Serve al confronto 1°T/2°T in notifica e alla strategia Rimonta (shadow-log): alla fine
        # del 1° tempo si fotografano le statistiche per poterle confrontare con quelle del 2°
        # tempo (la squadra contro se stessa, non contro l'avversaria).
        fine_1h_appena_avvenuta = prev_status_short == "1H" and status_short != "1H"

        recupero_da_segnalare = None
        if recupero_appena_concluso:
            fase_recupero, minuti_recupero = recupero_appena_concluso
            e_preferita = str(fixture_id) in FAVORITE_MATCHES
            if minuti_recupero > SOGLIA_RECUPERO_LUNGO_MINUTI or (e_preferita and minuti_recupero >= 1):
                recupero_da_segnalare = (fase_recupero, minuti_recupero)
                log(f"    ⏱ Recupero {fase_recupero} concluso: +{minuti_recupero}' (da segnalare)")

        if fixture_id not in stato_partite:
            stato_partite[fixture_id] = {}
        stato_partite[fixture_id].update({
            "score_home": score_home,
            "score_away": score_away,
            "last_minute": minuto,
            "last_status_short": status_short,
            "recupero_1h": recupero_1h,
            "recupero_2h": recupero_2h,
            "home": home,
            "away": away,
            "league": league_name,
            "league_country": league_country,
        })

        # Andata (solo qualificazioni/playoff UEFA andata-ritorno): cercata UNA sola volta per
        # fixture_id e salvata in cache, non ripetuta ad ogni ciclo - vedi
        # recupera_andata_precedente(). Serve ad avere subito il quadro aggregato (chi era in casa
        # all'andata, che risultato c'è stato) senza doverlo cercare altrove.
        # "andata_controllata" scatta a True SOLO se la chiamata è andata a buon fine (trovata o
        # no): se fallisce (rate-limit/timeout/rete) resta False apposta, per riprovare al ciclo
        # successivo invece di rinunciare per sempre alla prima chiamata sfortunata.
        if not stato_partite[fixture_id].get("andata_controllata"):
            league_id = league.get("id")
            round_corrente = league.get("round", "")
            if league_id and league_name.lower() in COMPETIZIONI_UEFA_ANDATA_RITORNO \
                    and _e_round_andata_ritorno(round_corrente):
                home_id = fixture["teams"]["home"].get("id")
                away_id = fixture["teams"]["away"].get("id")
                if home_id and away_id:
                    ts_ritorno = fixture["fixture"].get("timestamp") or time.time()
                    chiamata_riuscita, andata_info = recupera_andata_precedente(
                        fixture_id, home_id, away_id, league_id, ts_ritorno)
                    time.sleep(1)
                    stato_partite[fixture_id]["andata_controllata"] = chiamata_riuscita
                    stato_partite[fixture_id]["andata_info"] = andata_info
                else:
                    # Niente id squadra, non c'è nulla da riprovare: non ha senso ritentare.
                    stato_partite[fixture_id]["andata_controllata"] = True
                    stato_partite[fixture_id]["andata_info"] = None
            else:
                # Non è un turno andata-ritorno UEFA: non ha senso ritentare ad ogni ciclo.
                stato_partite[fixture_id]["andata_controllata"] = True
                stato_partite[fixture_id]["andata_info"] = None

        events = fetch_fixture_events(fixture_id)
        goals = extract_goals(events)
        goals = goals_coerenti_con_risultato(goals, home, away, score_home, score_away)
        if goals:
            log(f"    ⚽ Gol trovati: {len(goals)}")
        else:
            log("    ⚽ Nessun gol registrato")

        # Cartellini rossi e rigori: stessa lista fresca ad ogni ciclo (nessuna chiamata in più,
        # è già dentro "events"), confrontata con quella salvata al ciclo precedente per capire
        # quali sono nuovi. Se stato_precedente è vuoto (prima volta che vediamo la partita) il
        # default fa combaciare le due liste, cosi' non si notifica un cartellino/rigore già
        # avvenuto prima che il bot iniziasse a monitorarla (stesso criterio usato per i gol).
        #
        # Il confronto usa una CHIAVE STABILE (minuto, squadra, dettaglio/esito) invece
        # dell'uguaglianza sull'intero dizionario: il campo "player" può essere "Sconosciuto" nei
        # primi cicli e risolto dall'API in un nome vero più tardi (visto in produzione), e prima
        # confrontare i dizionari interi faceva risultare lo STESSO cartellino "diverso" da quello
        # già notificato, rimandandolo una seconda volta solo perché nel frattempo era comparso il
        # nome del giocatore - un duplicato, non una notizia nuova.
        cartellini_rossi = extract_cartellini_rossi(events)
        rigori = extract_rigori(events)
        prev_cartellini_rossi = stato_precedente.get("cartellini_rossi", cartellini_rossi)
        prev_rigori = stato_precedente.get("rigori", rigori)
        chiavi_prev_cartellini = {(c["minute"], c["team"], c.get("dettaglio")) for c in prev_cartellini_rossi}
        chiavi_prev_rigori = {(r["minute"], r["team"], r.get("esito")) for r in prev_rigori}
        nuovi_cartellini_rossi = [
            c for c in cartellini_rossi if (c["minute"], c["team"], c.get("dettaglio")) not in chiavi_prev_cartellini]
        nuovi_rigori = [
            r for r in rigori if (r["minute"], r["team"], r.get("esito")) not in chiavi_prev_rigori]
        if nuovi_cartellini_rossi:
            log(f"    🟥 Nuovo cartellino rosso: {nuovi_cartellini_rossi}")
        if nuovi_rigori:
            log(f"    ⚠️ Nuovo rigore: {nuovi_rigori}")
        stato_partite[fixture_id]["cartellini_rossi"] = cartellini_rossi
        stato_partite[fixture_id]["rigori"] = rigori
        stato_partite[fixture_id]["goals"] = goals

        if status_short in STATUS_OLTRE_TEMPI_REGOLAMENTARI:
            # Tempi regolamentari finiti in parità: supplementari/rigori in corso. Su richiesta,
            # da qui in poi nessuna notifica per questa partita - niente statistiche da recuperare
            # (nessuna notifica le userebbe), e quando finirà davvero (AET/PEN, più sotto) non
            # partirà nemmeno il recap finale. I gol restano comunque tracciati sopra (stessa
            # chiamata "events" di sempre, nessun costo aggiuntivo), così un gol ai supplementari
            # non manca allo shadow-log quando la partita si chiude per davvero.
            log(f"  -> Tempi regolamentari finiti ({status_short}, {minuto}'): supplementari/rigori in corso, notifiche sospese")
            return

        # Pausa tra le due chiamate di questa stessa partita (eventi appena fatta, statistiche
        # tra un attimo): senza, con molte partite live nello stesso ciclo (es. più gironi di
        # qualificazione in contemporanea) il ritmo reale era di 2 chiamate quasi consecutive per
        # partita e solo 1s di pausa PRIMA della partita successiva - con una decina di partite
        # live si arriva facilmente a 100+ richieste/minuto, il limite per-minuto dell'abbonamento
        # API-Football (visto coi rate-limit sporadici anche a traffico medio basso). Ora ogni
        # partita costa 2 chiamate ben distanziate invece di una raffica.
        # Backoff: se questa partita ha gia' dimostrato di non avere statistiche, si salta il giro -
        # e con esso anche la pausa qui sotto, che serve a distanziare le chiamate che non facciamo.
        chiamata_saltata = not deve_chiedere_statistiche(fixture_id)
        if chiamata_saltata:
            stato_partite[fixture_id]["cicli_saltati_statistiche"] = (
                stato_partite[fixture_id].get("cicli_saltati_statistiche", 0) + 1)
            stats = None
        else:
            stato_partite[fixture_id]["cicli_saltati_statistiche"] = 0
            time.sleep(1)

        # Tre esiti diversi, che prima finivano tutti e tre in due soli rami:
        #  - stats is None            -> la CHIAMATA è fallita (rate-limit, timeout, rete)
        #  - risposta senza dati veri -> l'API ha risposto, ma per questa partita non pubblica stats
        #  - dati veri                -> caso normale
        # La distinzione conta in due punti: solo il secondo caso dice qualcosa sulla copertura
        # della lega (vedi registra_esito_statistiche) e solo il primo è un vero problema di
        # pipeline da segnalare nella diagnostica. Si usa ha_statistiche_disponibili() come già
        # fa /live: il vecchio "len(stats) >= 2" considerava buona anche la risposta con le due
        # squadre ma tutti i valori a null (tipica dei primi minuti), che estrai_current_stats
        # traduce in zeri - finivano nello storico come punti finti e in notifica come "0 - 0"
        # invece che "N/D".
        if not chiamata_saltata:
            stats = get_statistiche_partita(fixture_id)
        if ha_statistiche_disponibili(stats):
            stats_home = stats[0].get("statistics", [])
            stats_away = stats[1].get("statistics", [])
            current_stats = estrai_current_stats(stats_home, stats_away)
            tiri_casa, tiri_ospite = current_stats["Tiri totali"]
            tiri_p_casa, tiri_p_ospite = current_stats["Tiri in porta"]
            corner_casa, corner_ospite = current_stats["Corner"]
            tiri_area_casa, tiri_area_ospite = current_stats["Tiri in area"]
            xg_casa, xg_ospite = estrai_xg(stats_home), estrai_xg(stats_away)
            log(f"    📊 Statistiche: Tiri {tiri_casa}-{tiri_ospite} | Porta {tiri_p_casa}-{tiri_p_ospite} | Corner {corner_casa}-{corner_ospite} | Area {tiri_area_casa}-{tiri_area_ospite}")

            # Feed bloccato (vedi impronta_statistiche): l'API risponde, ma con la stessa identica
            # risposta di prima mentre la partita va avanti. Senza dirlo, la partita si spegne da
            # sola con "nessun tiro cambiato" e non arriva mai in chat: e' quello che il 23/08 ha
            # tenuto fuori Venezia-Lecce, ferma su 3-0 dal 24' al 44' mentre era davvero 7-1.
            # Un avviso solo per blocco, non ad ogni ciclo.
            feed_congelato, minuti_feed_fermo = aggiorna_feed_congelato(fixture_id, stats, minuto)
            if feed_congelato and not stato_partite[fixture_id].get("feed_congelato_segnalato"):
                stato_partite[fixture_id]["feed_congelato_segnalato"] = True
                log(f"    🧊 Feed statistiche bloccato da {minuti_feed_fermo}' di gioco "
                    f"(fermo su Tiri {tiri_casa}-{tiri_ospite})")
                # Una partita gia' decisa non serve segnalarla: con le statistiche ferme non
                # si puo' leggere niente, e a quel punto non c'e' nemmeno niente da leggere.
                scarto_gol = abs((score_home or 0) - (score_away or 0))
                if (str(fixture_id) not in SILENCED_MATCHES
                        and scarto_gol <= SOGLIA_GOLEADA_STOP_NOTIFICHE):
                    FEED_CONGELATI_CICLO.append({
                        "home": home, "away": away,
                        "lega": formatta_lega(league_name, league_country),
                        "minuto": minuto, "score": f"{score_home}-{score_away}",
                        "fermo_da": minuti_feed_fermo,
                        "tiri": f"{tiri_casa}-{tiri_ospite}",
                        "preferita": str(fixture_id) in FAVORITE_MATCHES,
                    })

            # Storico NON potato (a differenza di STATUS_HISTORY in cmd_status): serve per intero a
            # /momentum per disegnare l'andamento su tutta la partita, non solo il blocco di 15 min
            # corrente. _calcola_delta_15min_da_storico filtra già da sé i punti del blocco che le
            # servono, quindi tenerlo tutto non cambia il delta 15 min. Costo trascurabile: ~1
            # snapshot a ciclo (~3 min), ripulito comunque a fine partita da pulisci_partite_terminate().
            history = stato_partite[fixture_id].get("history", [])
            history.append({
                "timestamp": time.time(), "minuto": minuto, "stats": current_stats,
                "xg": [estrai_xg(stats_home), estrai_xg(stats_away)],
            })
            stato_partite[fixture_id]["history"] = history
            stato_partite[fixture_id]["stats_ultimo_esito"] = "ok"
            stato_partite[fixture_id]["stats_vuote_consecutive"] = 0
            # La partita è tornata a dare statistiche: se più avanti dovesse tornare vuota per
            # altri 3 cicli di fila, potrà di nuovo contribuire al verdetto sulla lega.
            stato_partite[fixture_id]["verdetto_lega_registrato"] = False
            # Copia nel backup indipendente (vedi BACKUP_HISTORY_MOMENTUM) ad ogni nuovo punto, cosi'
            # un eventuale reset di stato_partite più avanti non fa perdere quanto già raccolto.
            BACKUP_HISTORY_MOMENTUM[str(fixture_id)] = history
            salva_backup_history_momentum(BACKUP_HISTORY_MOMENTUM)
            registra_esito_statistiche(league_country, league_name, True)
            registra_osservazione_statistiche(league_country, league_name, True, league_round)
            if fine_1h_appena_avvenuta:
                stato_partite[fixture_id]["stats_fine_1h"] = current_stats
                log("    📸 Statistiche di fine 1° tempo salvate (confronto 1°T/2°T, strategia Rimonta)")
        else:
            current_stats = None
            tiri_casa = tiri_ospite = tiri_p_casa = tiri_p_ospite = corner_casa = corner_ospite = 0
            tiri_area_casa = tiri_area_ospite = 0
            xg_casa = xg_ospite = None
            if chiamata_saltata:
                # Nessuna notizia nuova, perche' non abbiamo chiesto: si lascia intatto tutto lo
                # stato precedente (esito, contatore delle vuote) invece di inventare un esito che
                # non c'e' stato. La partita resta seguita: gol, cartellini e rigori arrivano dalla
                # chiamata eventi, che non e' toccata dal backoff.
                vuote_note = stato_partite[fixture_id].get("stats_vuote_consecutive", 0)
                saltati = stato_partite[fixture_id].get("cicli_saltati_statistiche", 0)
                log(f"    ⏭️ Statistiche non richieste: assenti {vuote_note} volte di fila, "
                    f"si riprova tra {max(0, (CICLI_BACKOFF_STATISTICHE if vuote_note < SOGLIA_BACKOFF_LUNGO else CICLI_BACKOFF_STATISTICHE_LUNGO) - 1 - saltati)} cicli")
            elif stats is None:
                # Chiamata fallita: non dice nulla sulla copertura della lega, si riprova al ciclo
                # dopo. Il contatore delle risposte vuote NON va azzerato (non abbiamo notizie
                # nuove) ma nemmeno incrementato.
                stato_partite[fixture_id]["stats_ultimo_esito"] = "errore"
                log("    ⚠️ Statistiche non recuperate: chiamata API fallita (rate-limit/timeout/rete), riprovo al prossimo ciclo")
            else:
                vuote = stato_partite[fixture_id].get("stats_vuote_consecutive", 0) + 1
                stato_partite[fixture_id]["stats_ultimo_esito"] = "vuote"
                stato_partite[fixture_id]["stats_vuote_consecutive"] = vuote
                log(f"    ⚠️ Statistiche assenti: l'API ha risposto senza dati per questa partita ({vuote} volte di fila)")
                registra_osservazione_statistiche(league_country, league_name, False, league_round)
                # Il verdetto sulla LEGA lo dà solo una partita che da sola ha già collezionato
                # SOGLIA_SENZA_STATISTICHE risposte vuote di fila (~3 cicli, diversi minuti di
                # tempo dato all'API), e conta una volta sola per partita. Prima bastava una
                # risposta vuota qualsiasi: con più partite della stessa lega live insieme
                # (sabato di campionato, 10+ gare in contemporanea) la soglia dei "3 controlli
                # consecutivi" veniva raggiunta da 3 PARTITE DIVERSE nello stesso ciclo, cioè in
                # pochi secondi, e bastava il normale ritardo con cui l'API pubblica le
                # statistiche a inizio turno per escludere per 24h campionati perfettamente
                # coperti (visto in produzione su League One/League Two inglesi, Ekstraklasa,
                # K League, J League, Liga I romena).
                if (minuto >= MINUTO_MINIMO_VERDETTO_STATISTICHE
                        and vuote >= SOGLIA_SENZA_STATISTICHE
                        and not stato_partite[fixture_id].get("verdetto_lega_registrato")):
                    # Ultimo filtro prima di condannare la lega: se in questo momento le
                    # statistiche mancano su molte leghe diverse insieme, questa partita non sta
                    # dimostrando niente sulla SUA lega - sta solo osservando un feed guasto (vedi
                    # avaria_statistiche_diffusa). Il verdetto non viene registrato e nemmeno
                    # marcato come dato: la partita potra' rivotare piu' avanti, quando il quadro
                    # sara' tornato leggibile.
                    if avaria_statistiche_diffusa():
                        log("    ⏸️ Verdetto sulla lega sospeso: statistiche assenti su molte leghe "
                            "insieme, sembra un guasto del feed e non una lega scoperta")
                    else:
                        stato_partite[fixture_id]["verdetto_lega_registrato"] = True
                        registra_esito_statistiche(league_country, league_name, False)

        if status_short in STATI_PARTITA_CONCLUSA:
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
                        f"{formatta_lega(league_name, league_country)}\n"
                        f"Risultato finale: {score_home} - {score_away}{after_text}\n"
                        f"Silenziato al {muted_minute}'\n"
                        f"Gol dopo:{minutes_text}"
                    )
                    foto_path = None
                else:
                    # Il grafico serve solo per l'invio Telegram più sotto: se le notifiche sono
                    # spente (fuori orario), o se questa partita è finita ai supplementari/rigori
                    # (status AET/PEN, niente notifica - vedi il gate più sotto), generarlo
                    # comunque sarebbe lavoro sprecato (rendering matplotlib + scrittura file) per
                    # un'immagine che verrebbe subito cancellata senza mai essere usata.
                    if current_stats and notifiche_attive and status_short == "FT":
                        foto_path = genera_grafico_barre(fixture_id, home, away, current_stats)
                    else:
                        foto_path = None

                    goals_text = testo_primo_ultimo_gol(goals, home, away)

                    recupero_parti = []
                    if recupero_1h:
                        recupero_parti.append(f"1° tempo +{recupero_1h}'")
                    if recupero_2h:
                        recupero_parti.append(f"2° tempo +{recupero_2h}'")
                    recupero_finale_text = f"Recupero: {', '.join(recupero_parti)}\n" if recupero_parti else ""

                    cartellini_finale_text = ""
                    if cartellini_rossi:
                        righe = [f"🟥 {c['minute']}' {c['player']} ({c['team']})" for c in cartellini_rossi]
                        cartellini_finale_text = "Cartellini rossi:\n" + "\n".join(righe) + "\n"

                    rigori_finale_text = ""
                    if rigori:
                        righe = []
                        for r in rigori:
                            esito_emoji = "⚽" if r["esito"] == "segnato" else "❌"
                            righe.append(f"{esito_emoji} {r['minute']}' {r['player']} ({r['team']}) - {r['esito']}")
                        rigori_finale_text = "Rigori:\n" + "\n".join(righe) + "\n"

                    tempi_finale_text = ""
                    stats_1h_salvate = stato.get("stats_fine_1h")
                    if current_stats:
                        if stats_1h_salvate:
                            tempi_finale_text = testo_confronto_tempi(stats_1h_salvate, current_stats)
                        elif history:
                            tempi_finale_text = testo_confronto_tempi_parziale(history, current_stats)
                        else:
                            tempi_finale_text = "(1°T/2°T non disponibile: nessun dato raccolto per questa partita)\n"

                    messaggio = (
                        f"{home} vs {away}\n"
                        f"{formatta_lega(league_name, league_country)}\n"
                        f"RISULTATO FINALE\n\n"
                        f"{score_home} - {score_away}\n"
                        f"{goals_text}\n"
                        f"{recupero_finale_text}"
                        f"{cartellini_finale_text}"
                        f"{rigori_finale_text}"
                        f"Statistiche finali:\n"
                        f"- Tiri totali: {tiri_casa if current_stats else '?'} - {tiri_ospite if current_stats else '?'}\n"
                        f"- Tiri in porta: {tiri_p_casa if current_stats else '?'} - {tiri_p_ospite if current_stats else '?'}\n"
                        f"- Corner: {corner_casa if current_stats else '?'} - {corner_ospite if current_stats else '?'}\n"
                        f"{tempi_finale_text}"
                    )

                # L'esito va registrato sempre, notifica o no: è il dato che chiude lo shadow-log
                # di questa partita (senza, gli snapshot già raccolti restano orfani per sempre -
                # bug scoperto proprio perché prima la pausa fermava tutto, notifica inclusa).
                registra_shadow_log_valore_risultato(fixture_id, score_home, score_away)
                registra_shadow_log_strategie_risultato(fixture_id, score_home, score_away, goals)
                # AET/PEN = partita finita ai supplementari o ai rigori: su richiesta, stesso
                # trattamento di STATUS_OLTRE_TEMPI_REGOLAMENTARI più sopra, nessuna notifica
                # nemmeno per il recap finale. Solo FT (decisa nei 90' regolamentari) la manda.
                if notifiche_attive and status_short == "FT":
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

        if current_stats:
            delta_stats, is_real_delta = calcola_delta_15min(fixture_id, current_stats, minuto)
            stats_dict = delta_stats
            header_stats = "Statistiche ultimi 15 min" if is_real_delta else "Primo rilevamento"

            # Shadow-log strategie: stesso ritmo (15 min) e stessa logica indipendente da
            # deve_notificare() dello snapshot valore più sotto - vedi commento su
            # SHADOW_LOG_STRATEGIE_FILE. Riusa dati già calcolati sopra, nessuna chiamata in più.
            ultimo_snapshot_strategie = stato_partite[fixture_id].get("ultimo_snapshot_strategie", 0)
            if (time.time() - ultimo_snapshot_strategie) >= INTERVALLO_SNAPSHOT_VALORE:
                p_strategie = {
                    "home": home, "away": away, "minute": minuto,
                    "score_h": score_home, "score_a": score_away,
                    "stats": current_stats, "delta": delta_stats, "delta_reale": is_real_delta,
                    "xg_home": xg_casa, "xg_away": xg_ospite,
                    "stato_precedente": stato_partite.get(fixture_id, {}),
                }
                segnali = []
                for nome_strat, _emoji_strat, valuta_fn, _descr_strat in STRATEGIE:
                    esito_strat = valuta_fn(p_strategie)
                    if esito_strat is not None:
                        punteggio_strat, dettaglio_strat = esito_strat
                        segnali.append({
                            "strategia": nome_strat,
                            "punteggio": round(punteggio_strat, 2),
                            "dettaglio": dettaglio_strat,
                        })
                registra_shadow_log_strategie_snapshot(
                    fixture_id, home, away, minuto, score_home, score_away, segnali)
                stato_partite[fixture_id]["ultimo_snapshot_strategie"] = time.time()
        else:
            stats_dict = {"Tiri totali": (0, 0), "Tiri in porta": (0, 0), "Corner": (0, 0)}
            header_stats = "Statistiche"

        log(f"  Tiri: {tiri_casa}-{tiri_ospite} | Porta: {tiri_p_casa}-{tiri_p_ospite} | Corner: {corner_casa}-{corner_ospite}")
        log(f"  Delta 15min: {stats_dict}")

        # Shadow-log valore: snapshot ogni INTERVALLO_SNAPSHOT_VALORE, indipendente da
        # deve_notificare() più sotto - girare solo quando scatta una notifica campionerebbe solo
        # i momenti "ad alta attività", introducendo il bias di selezione visto nella ricerca.
        quote_iniziali_snapshot = quote_1x2_per_fixture(fixture_id)
        probabilita_no_vig_snapshot = calcola_probabilita_no_vig(quote_iniziali_snapshot) if quote_iniziali_snapshot else None
        if probabilita_no_vig_snapshot:
            ultimo_snapshot = stato_partite[fixture_id].get("ultimo_snapshot_valore", 0)
            if (time.time() - ultimo_snapshot) >= INTERVALLO_SNAPSHOT_VALORE:
                registra_shadow_log_valore_snapshot(
                    fixture_id, home, away, minuto, score_home, score_away,
                    probabilita_no_vig_snapshot, stats_dict)
                stato_partite[fixture_id]["ultimo_snapshot_valore"] = time.time()

        # Silenziare una partita è una scelta sulle NOTIFICHE, non sulla raccolta dati: da qui in
        # giù si decide solo cosa mandare in chat, quindi il taglio va fatto qui e non prima.
        # Stava sopra, prima dei due shadow-log, e li saltava entrambi - stesso identico problema
        # già corretto per l'esito finale poco più sopra ("l'esito va registrato sempre, notifica o
        # no: senza, gli snapshot già raccolti restano orfani per sempre"), che però era rimasto
        # valido solo per il risultato e non per gli snapshot che lo precedono.
        #
        # Lo shadow-log serve a calibrare le strategie e nasce apposta per non avere bias di
        # selezione (vedi INTERVALLO_SNAPSHOT_VALORE): perdere le partite silenziate lo reintroduce
        # dalla porta di servizio, e per giunta filtrato da una scelta manuale.
        #
        # Visto in produzione il 16/08, ciclo #10 delle 12:22: Odense-AC Horsens al 22',
        # SK Beveren-Anderlecht al 45' e Arminia Bielefeld-Energie Cottbus al 45' avevano tutte
        # statistiche piene ("Tiri 3-2", "Tiri 1-6", "Tiri 8-2") e finivano su "-> Silenziata,
        # skip" senza scrivere nulla, mentre Hannover 96-VfL Wolfsburg - stessa lega, stesso
        # minuto, stesso ciclo, ma non silenziata - lo scriveva regolarmente. La diagnostica
        # automatica le segnalava come anomalia "statistiche presenti ma nessuno snapshot scritto":
        # aveva ragione, ed era anche l'unico modo in cui il buco si vedeva da fuori.
        if str(fixture_id) in SILENCED_MATCHES:
            muted_data = SILENCED_MATCHES[str(fixture_id)]
            if "muted_at_minute" not in muted_data:
                muted_data["muted_at_minute"] = minuto
                save_silenced(SILENCED_MATCHES)
            log("  -> Silenziata, skip")
            return

        # Preferito "raffreddato": se sono passati troppi minuti dall'ultima notifica inviata,
        # la partita si è spenta - si rimuove dai preferiti PRIMA di valutare deve_notificare(),
        # cosi' questo stesso ciclo segue già le regole normali invece di restare agganciato a
        # soglie più permissive per sempre.
        if str(fixture_id) in FAVORITE_MATCHES:
            stato_fav = stato_partite.setdefault(fixture_id, {})
            # Preferito senza statistiche: esce. La regola d'ingresso guarda solo il punteggio,
            # apposta per funzionare quando l'API tace, ma il canale serve a leggere tiri, tiri in
            # porta e corner mentre si gioca - e senza quelli non può far scattare nessuna
            # strategia, pur costando il triplo di chiamate (60s invece di 180s). Meglio liberare
            # il posto per una partita che i dati li ha.
            # Si guarda l'assenza CONFERMATA, non un buco passeggero: esito "vuote" ripetuto
            # SOGLIA_SENZA_STATISTICHE volte, la stessa prova che usa la diagnostica per dire
            # "l'API risponde ma non pubblica statistiche per questa partita". Un rate-limit o un
            # timeout lasciano esito "errore" e non contano.
            if (stato_fav.get("stats_ultimo_esito") == "vuote"
                    and stato_fav.get("stats_vuote_consecutive", 0) >= SOGLIA_SENZA_STATISTICHE):
                FAVORITE_MATCHES.discard(str(fixture_id))
                save_favorites(FAVORITE_MATCHES)
                # Non riproponibile: senza questo, la regola sui gol la rimetterebbe dentro al
                # ciclo successivo, visto che il punteggio non è cambiato.
                stato_fav["auto_preferito_processato"] = True
                log("    ⭐➡️ Preferito rimosso: l'API non pubblica statistiche per questa partita")
                if notifiche_attive:
                    invia_messaggio_uscita_preferiti(
                        home, away, minuto, score_home, score_away,
                        "l'API non pubblica statistiche per questa partita",
                        stato_fav)
                return
            ultimo_invio_fav = stato_fav.get("timestamp_notifica", 0)
            # Il tempo di silenzio va misurato in minuti GIOCATI, non di orologio: durante
            # l'intervallo (status "HT", elapsed assente) non arrivano notifiche semplicemente
            # perché non si sta giocando, e DURATA_MAX_SENZA_NOTIFICA_PREFERITI vale esattamente
            # 15 minuti - quanto dura l'intervallo. Col solo orologio, un preferito la cui ultima
            # notifica cadeva poco prima del 45' veniva rimosso durante l'intervallo o al primo
            # ciclo del secondo tempo, cioè proprio quando serve continuare a seguirlo. Stessa
            # logica già applicata al caso "fuori orario" qui sotto, dove lo stato viene aggiornato
            # come se avessimo notificato per non far scadere il preferito di notte.
            minuto_ultima_notifica = stato_fav.get("minuto_ultima_notifica")
            if ultimo_invio_fav and minuto_ultima_notifica is None and elapsed_raw is not None:
                # Stato salvato da una versione precedente (senza questo campo): si riparte da ora
                # invece di rimuovere subito il preferito.
                minuto_ultima_notifica = elapsed_raw
                stato_fav["minuto_ultima_notifica"] = elapsed_raw
            minuti_giocati_da_notifica = (
                elapsed_raw - minuto_ultima_notifica
                if elapsed_raw is not None and minuto_ultima_notifica is not None else None
            )
            if (ultimo_invio_fav
                    and elapsed_raw is not None
                    and minuti_giocati_da_notifica is not None
                    and minuti_giocati_da_notifica >= DURATA_MAX_SENZA_NOTIFICA_PREFERITI // 60
                    and (time.time() - ultimo_invio_fav) > DURATA_MAX_SENZA_NOTIFICA_PREFERITI):
                FAVORITE_MATCHES.discard(str(fixture_id))
                save_favorites(FAVORITE_MATCHES)
                minuti_senza_notifica = DURATA_MAX_SENZA_NOTIFICA_PREFERITI // 60
                log(f"    ⭐➡️ Preferito rimosso automaticamente: nessuna notifica da oltre {minuti_senza_notifica} min")
                if notifiche_attive:
                    invia_messaggio_uscita_preferiti(
                        home, away, minuto, score_home, score_away,
                        f"si è spenta, nessun cambiamento da {minuti_senza_notifica} minuti giocati",
                        stato_fav)
        elif not stato_partite.get(fixture_id, {}).get("auto_preferito_processato"):
            # Partita che parte già "a razzo" (tanti tiri o gol nei primissimi minuti): valutata
            # una sola volta per partita, cosi' se l'utente la rimuove in seguito non viene
            # riproposta di nuovo entro la stessa finestra di minuti.
            gol_totali = score_home + score_away
            tiri_totali_partita = (tiri_casa + tiri_ospite) if current_stats else 0
            promuovi, motivo = deve_aggiungere_automaticamente_ai_preferiti(
                minuto, score_home, score_away)
            # Rotta 2 (dominio), valutata solo se la prima non ha gia' promosso: da qui in poi la
            # partita e' comunque nei preferiti, e continuare a misurarne il dominio non servirebbe
            # a nessuno dei due scopi (ne' promuovere ne' tarare le soglie per promuovere).
            # Va chiamata anche quando la promozione e' spenta: e' lei ad aggiornare contatore di
            # isteresi e picco, cioe' i dati con cui poi si decide se accenderla.
            promuovi_dominio = False
            motivo_dominio = ""
            quota_dominio = volume_dominio = None
            situazione_dominio = None
            if not promuovi:
                quota_dominio, volume_dominio, dominio_valutato = _dominio_per_auto_preferiti(
                    current_stats, score_home, score_away)
                situazione_dominio = dominio_valutato["situazione"] if dominio_valutato else None
                promuovi_dominio, motivo_dominio = deve_aggiungere_automaticamente_ai_preferiti_per_dominio(
                    fixture_id, current_stats, score_home, score_away, minuto)
                if promuovi_dominio:
                    promuovi, motivo = True, motivo_dominio
            # Il silenzio senza statistiche vale anche qui. Una partita di cui l'API non pubblica i
            # numeri non manda NESSUNA notifica (vedi deve_notificare): promuoverla vorrebbe dire
            # occupare uno dei MAX_PREFERITI_SIMULTANEI posti e pagare il triplo di chiamate (60s
            # invece di 180s) per una partita che nel canale resta muta - annunciandola per giunta
            # con un "Statistiche: N/D", cioe' proprio il messaggio che non si vuole piu' vedere.
            #
            # E' lo stesso motivo per cui un preferito che smette di dare statistiche viene tolto
            # poco piu' sopra ("meglio liberare il posto per una partita che i dati li ha"): qui si
            # evita di farlo entrare, invece di annunciarlo e poi cacciarlo con un secondo
            # messaggio. Vale solo per la rotta gol: quella dominio le statistiche le richiede gia'
            # per definizione, senza non calcola niente.
            #
            # Non si marca "auto_preferito_processato": se le statistiche arrivano prima del
            # MINUTO_GOL_AUTO_PREFERITI la partita puo' ancora entrare al ciclo successivo.
            if promuovi and SILENZIO_SENZA_STATISTICHE_ATTIVO and not current_stats:
                promuovi = False
                motivo = "statistiche non pubblicate: nel canale resterebbe muta"
            if promuovi:
                FAVORITE_MATCHES.add(str(fixture_id))
                save_favorites(FAVORITE_MATCHES)
                # Marcata come già valutata SOLO quando viene davvero promossa: così, se in seguito
                # la si toglie a mano dai preferiti, il bot non la ripropone. Una partita mai
                # promossa resta invece valutabile per tutto il resto della gara - è il punto del
                # cambio: prima la finestra si chiudeva al 12' e non si riapriva più.
                stato_partite[fixture_id]["auto_preferito_processato"] = True
                log(f"    ⭐ Aggiunta automaticamente ai preferiti al {minuto}': {motivo}")
                if notifiche_attive:
                    # Nel CANALE preferiti, non nella chat principale: è il messaggio che apre la
                    # partita là dove poi vivrà. Prima finiva nella chat principale (chat_id
                    # omesso), e il canale si ritrovava un flusso di aggiornamenti senza inizio.
                    quote_ingresso = quote_1x2_per_fixture(fixture_id)
                    righe_ingresso = [
                        f"⭐ ENTRA NEI PREFERITI · {minuto}'",
                        f"{home} {score_home}-{score_away} {away}",
                        f"{formatta_lega(league_name, league_country)}",
                        "",
                        motivo,
                    ]
                    if goals:
                        marcatori = [f"{g['minute']}' {g['player']} ({g['team']})" for g in goals]
                        righe_ingresso.append("\nGol: " + " · ".join(marcatori))
                    if isinstance(quote_ingresso, dict):
                        righe_ingresso.append(
                            f"Quote 1X2 iniziali: {quote_ingresso['casa']:.2f} - "
                            f"{quote_ingresso['pareggio']:.2f} - {quote_ingresso['ospite']:.2f}")
                    if current_stats:
                        righe_ingresso.append(
                            f"Statistiche: Tiri {tiri_casa}-{tiri_ospite} · "
                            f"Porta {tiri_p_casa}-{tiri_p_ospite} · Corner {corner_casa}-{corner_ospite}")
                    else:
                        righe_ingresso.append("Statistiche: N/D (l'API non le pubblica per questa partita)")
                    invia_messaggio_telegram("\n".join(righe_ingresso),
                                             chat_id=TELEGRAM_CHAT_ID_PREFERITI)
                registra_shadow_log_auto_preferiti(
                    fixture_id, home, away, league_name, league_country, minuto,
                    tiri_totali_partita, (tiri_p_casa + tiri_p_ospite) if current_stats else 0,
                    (corner_casa + corner_ospite) if current_stats else 0,
                    (tiri_area_casa + tiri_area_ospite) if current_stats else 0,
                    xg_casa, xg_ospite, gol_totali, True,
                )
                if promuovi_dominio:
                    registra_shadow_log_auto_preferiti_dominio(
                        fixture_id, home, away, league_name, league_country, minuto,
                        score_home, score_away, quota_dominio, volume_dominio, situazione_dominio,
                        stato_partite[fixture_id].get("cicli_dominio_sopra_soglia", 0), True, motivo)
            else:
                # Log solo per le partite che hanno segnato: sono le uniche che possono entrare,
                # quindi le uniche il cui "no" dice qualcosa. Loggare ad ogni ciclo anche gli 0-0
                # riempirebbe i log senza aggiungere niente.
                if gol_totali >= SOGLIA_GOL_AUTO_PREFERITI and minuto is not None:
                    log(f"    ⭐? Auto-preferiti {minuto}': non promossa - {motivo}")
                # Verdetto negativo per lo shadow-log: una riga sola per partita, così il file
                # resta confrontabile (un campione per partita) invece di crescere ad ogni ciclo.
                if (minuto is not None and minuto >= MINUTO_VERDETTO_SHADOW_AUTO_PREFERITI
                        and not stato_partite[fixture_id].get("verdetto_shadow_auto_preferiti")):
                    stato_partite[fixture_id]["verdetto_shadow_auto_preferiti"] = True
                    log(f"    ⭐✖️ Auto-preferiti: mai promossa entro il {minuto}' - {motivo}")
                    registra_shadow_log_auto_preferiti(
                        fixture_id, home, away, league_name, league_country, minuto,
                        tiri_totali_partita, (tiri_p_casa + tiri_p_ospite) if current_stats else 0,
                        (corner_casa + corner_ospite) if current_stats else 0,
                        (tiri_area_casa + tiri_area_ospite) if current_stats else 0,
                        xg_casa, xg_ospite, gol_totali, False,
                    )
                # Verdetto della rotta dominio: contatore suo, non quello della rotta gol qui
                # sopra. Le due rotte scartano per motivi diversi e in momenti diversi, e con un
                # flag condiviso il "no" di una avrebbe zittito per sempre quello dell'altra. La
                # riga porta con se' il picco raggiunto in tutta la partita, non solo il valore di
                # questo istante: e' quello il dato con cui si tarano le soglie.
                if (minuto is not None and minuto >= MINUTO_VERDETTO_SHADOW_AUTO_PREFERITI
                        and not stato_partite[fixture_id].get("verdetto_shadow_dominio")):
                    stato_partite[fixture_id]["verdetto_shadow_dominio"] = True
                    quota_max_vista = stato_partite[fixture_id].get("dominio_quota_max")
                    picco_txt = f" (picco {quota_max_vista}%)" if quota_max_vista else ""
                    log(f"    ⭐✖️ Rotta dominio: mai promossa entro il {minuto}' - "
                        f"{motivo_dominio}{picco_txt}")
                    registra_shadow_log_auto_preferiti_dominio(
                        fixture_id, home, away, league_name, league_country, minuto,
                        score_home, score_away, quota_dominio, volume_dominio, situazione_dominio,
                        stato_partite[fixture_id].get("cicli_dominio_sopra_soglia", 0), False,
                        motivo_dominio)

        evento_forzato = gol_appena_segnato or bool(nuovi_cartellini_rossi) or bool(nuovi_rigori)
        if not deve_notificare(fixture_id, tiri_casa, tiri_ospite, minuto, delta_stats=stats_dict,
                               gol_appena_segnato=evento_forzato,
                               recupero_lungo=recupero_da_segnalare is not None,
                               score_home=score_home, score_away=score_away,
                               current_stats=current_stats):
            prev_notified = stato_partite.get(fixture_id, {}).get("notified_final", False)
            stato_partite[fixture_id].update({
                "tiri_casa": tiri_casa,
                "tiri_ospite": tiri_ospite,
                "timestamp_notifica": stato_partite[fixture_id].get("timestamp_notifica", 0),
                "notified_final": prev_notified,
            })
            log(f"  -> Skip: {motivo_valutazione_notifica(fixture_id)}")
            return

        log(f"  -> Notifica: {motivo_valutazione_notifica(fixture_id)}")

        # Preferiti: un'unica immagine con il totale cumulativo (barre proporzionali, come per
        # tutte le altre partite) impilato sopra il grafico momentum (andamento a intervalli, per
        # decidere se entrare) - se lo storico non è ancora abbastanza lungo per il momentum si usa
        # comunque il solo grafico a barre, per non restare senza foto.
        is_fav = str(fixture_id) in FAVORITE_MATCHES
        foto_path = None
        nota_momentum = ""
        # Il grafico (barre o combinato) serve solo per l'invio Telegram più sotto: se le
        # notifiche sono spente (fuori orario) generarlo comunque sarebbe lavoro sprecato
        # (rendering matplotlib + scrittura file) per un'immagine mai inviata e subito cancellata.
        # Il combinato (barre + momentum) spetta ai preferiti e a chi ha chiesto il momentum su
        # questa partita: la richiesta vale per il resto della gara, non solo per il messaggio su
        # cui si e' cliccato (vedi MOMENTUM_PERSISTENTE_ATTIVO).
        momentum_richiesto = bool(
            MOMENTUM_PERSISTENTE_ATTIVO
            and stato_partite.get(fixture_id, {}).get("momentum_richiesto"))
        if notifiche_attive:
            if is_fav or momentum_richiesto:
                history_completo = stato_partite.get(fixture_id, {}).get("history", [])
                foto_path = genera_grafico_combinato(
                    fixture_id, home, away, current_stats if current_stats else stats_dict, history_completo,
                    goals, rigori, cartellini_rossi, recupero_1h, recupero_2h)
                if foto_path:
                    nota_momentum = nota_copertura_momentum(history_completo)
                else:
                    # Il grafico combinato manca solo la parte momentum (le barre da sole si generano
                    # comunque sotto): senza questa nota il momentum spariva senza spiegazione, dando
                    # l'impressione di un bug invece che di storico ancora insufficiente.
                    nota_momentum = f"\n(grafico momentum non disponibile: {spiega_momentum_insufficiente(history_completo)})"
            if not foto_path:
                foto_path = genera_grafico_barre(fixture_id, home, away, current_stats if current_stats else stats_dict)

        diff = stats_dict["Tiri totali"][0] - stats_dict["Tiri totali"][1]
        # Nessun indicatore quando sono pari (EQ non è utile): solo chi è avanti nel delta 15 min.
        freccia = " 🏡 CASA" if diff > 0 else " ✈️ OSP" if diff < 0 else ""

        if current_stats:
            d_tiri_c = stats_dict["Tiri totali"][0]
            d_tiri_o = stats_dict["Tiri totali"][1]
            d_porta_c = stats_dict["Tiri in porta"][0]
            d_porta_o = stats_dict["Tiri in porta"][1]

            fire_t_c = get_fire_suffix(d_tiri_c)
            fire_t_o = get_fire_suffix(d_tiri_o)
            fire_p_c = get_fire_suffix_shots(d_porta_c)
            fire_p_o = get_fire_suffix_shots(d_porta_o)

            # Spazio tra il valore e la fiamma, così non restano attaccati (es. "5 🔥" non "5🔥").
            tot_c_txt = f"{d_tiri_c} {fire_t_c}" if fire_t_c else str(d_tiri_c)
            tot_o_txt = f"{d_tiri_o} {fire_t_o}" if fire_t_o else str(d_tiri_o)
            porta_c_txt = f"{d_porta_c} {fire_p_c}" if fire_p_c else str(d_porta_c)
            porta_o_txt = f"{d_porta_o} {fire_p_o}" if fire_p_o else str(d_porta_o)
            corner_line = f"{stats_dict['Corner'][0]} - {stats_dict['Corner'][1]}"
        else:
            # Nessuna statistica reale disponibile per questa lega/partita (non uno "0 - 0" vero):
            # va detto esplicitamente, altrimenti sembra un dato reale invece che dato mancante
            # (il grafico allegato in questo caso mostra già "Nessun dato" per coerenza).
            tot_c_txt = tot_o_txt = porta_c_txt = porta_o_txt = "N/D"
            corner_line = "N/D - N/D"
            freccia = ""

        goals_text = testo_primo_ultimo_gol(goals, home, away)

        recupero_text = ""
        if recupero_da_segnalare:
            fase_recupero, minuti_recupero = recupero_da_segnalare
            lungo_txt = " (lungo)" if minuti_recupero > SOGLIA_RECUPERO_LUNGO_MINUTI else ""
            recupero_text = f"\n⏱ Recupero {fase_recupero}: +{minuti_recupero}'{lungo_txt}\n"
        elif status_short in ("1H", "2H") and extra_corrente is not None:
            fase_corrente = "1° tempo" if status_short == "1H" else "2° tempo"
            recupero_text = f"\n⏱ Recupero {fase_corrente} annunciato: +{extra_corrente}'\n"

        cartellini_text = ""
        for c in nuovi_cartellini_rossi:
            cartellini_text += f"\n🟥 Rosso al {c['minute']}': {c['player']} ({c['team']})\n"

        rigori_text = ""
        for r in nuovi_rigori:
            if r["esito"] == "segnato":
                rigori_text += f"\n⚽ Rigore segnato al {r['minute']}': {r['player']} ({r['team']})\n"
            else:
                rigori_text += f"\n❌ Rigore sbagliato/parato al {r['minute']}': {r['player']} ({r['team']})\n"

        # Confronto 1°T/2°T: solo nel 2° tempo. Se manca lo snapshot di fine 1°T (il bot ha
        # iniziato a monitorare la partita dopo l'intervallo, es. per un riavvio nel mezzo) lo
        # dice esplicitamente, invece di lasciare intuire una sezione sparita per errore.
        # Dominio sui totali di partita, non sul blocco 15 min: la domanda è "chi sta facendo la
        # partita", che è una cosa cumulativa. Si calcola una volta e finisce sia in notifica sia
        # nel log, così a colpo d'occhio si legge chi comanda senza confrontare due colonne.
        dominio_partita = calcola_dominio(current_stats, score_home, score_away) if current_stats else None
        # Statistiche che contraddicono il risultato (piu' gol che tiri in porta): non sono poche,
        # sono indietro. Va detto accanto ai numeri, altrimenti un "Tiri 1-0" su un 1-1 si legge
        # come un dato vero. Vedi statistiche_indietro_sul_punteggio.
        _indietro, riga_ritardo_stats = statistiche_indietro_sul_punteggio(
            current_stats, score_home, score_away, events, home, away)
        if _indietro:
            log(f"  ⏳ Statistiche indietro sul risultato ({score_home}-{score_away} con "
                f"{current_stats.get('Tiri in porta', (0, 0))} in porta)")
        riga_dominio_notifica = ""
        if dominio_partita:
            riga_dominio_notifica = riga_dominio(dominio_partita, home, away, current_stats) + "\n"
            log(f"  {riga_dominio(dominio_partita, home, away, current_stats)}")

        tempi_text = ""
        stats_1h_salvate = stato_partite.get(fixture_id, {}).get("stats_fine_1h")
        if status_short == "2H" and current_stats:
            if stats_1h_salvate:
                tempi_text = testo_confronto_tempi(stats_1h_salvate, current_stats)
            elif history:
                tempi_text = testo_confronto_tempi_parziale(history, current_stats)
            else:
                tempi_text = "\n(1°T/2°T non disponibile: nessun dato raccolto per questa partita)\n"

        # Il totale cumulativo della partita (tiri, porta, corner, area) si vede ora nel grafico
        # allegato (barre proporzionali, sempre presenti: da sole per le non preferite, impilate
        # sopra il momentum per i preferiti) - non serve ripeterlo anche in testo.
        quote_iniziali = quote_1x2_per_fixture(fixture_id)
        quote_text = testo_quote_1x2(quote_iniziali)

        # Andata (se trovata, vedi il blocco di ricerca più sopra): mostrata subito in cima, prima
        # ancora del risultato del ritorno, per avere subito il quadro aggregato della qualificazione.
        # Se è stata trovata un'andata, PER DEFINIZIONE la partita di QUESTA notifica è il ritorno
        # (altrimenti la ricerca non l'avrebbe trovata) - va marcato esplicitamente nel titolo,
        # altrimenti la riga "Andata: ..." da sola può far pensare che sia l'andata quella
        # descritta nel resto del messaggio, invece del ritorno in corso.
        andata_info = stato_partite.get(fixture_id, {}).get("andata_info")
        andata_text = ""
        titolo_ritorno = ""
        if andata_info:
            titolo_ritorno = " (RITORNO)"
            andata_text = (
                f"🔄 Andata: {andata_info['home']} {andata_info['score_home']} - "
                f"{andata_info['score_away']} {andata_info['away']}\n\n"
            )

        messaggio = (
            f"{home} vs {away}{titolo_ritorno}\n"
            f"{formatta_lega(league_name, league_country)}\n"
            f"Minuto: {minuto}' | Stato: {status_short}\n\n"
            f"{andata_text}"
            f"Risultato: {score_home} - {score_away}\n"
            f"{quote_text}"
            f"{goals_text}"
            f"{cartellini_text}"
            f"{rigori_text}"
            f"{recupero_text}\n"
            f"{header_stats}:\n"
            f"- Tiri totali: {tot_c_txt} - {tot_o_txt}{freccia}\n"
            f"- Tiri in porta: {porta_c_txt} - {porta_o_txt}\n"
            f"- Corner: {corner_line}\n"
            f"{riga_ritardo_stats}"
            f"{riga_dominio_notifica}"
            f"{tempi_text}"
            f"{nota_momentum}"
        ).rstrip()

        is_sil = str(fixture_id) in SILENCED_MATCHES
        history_per_bottone = stato_partite.get(fixture_id, {}).get("history", [])
        mostra_momentum = len(history_per_bottone) >= MOMENTUM_MIN_STORICO
        keyboard = get_notification_keyboard(fixture_id, is_fav, is_sil, mostra_momentum)
        chat_destinazione = TELEGRAM_CHAT_ID_PREFERITI if is_fav else TELEGRAM_CHAT_ID
        if notifiche_attive:
            # Scheda viva del canale preferiti (vedi MESSAGGIO_LIVE_PREFERITI_ATTIVO): dentro lo
            # stesso blocco di 15 minuti gli aggiornamenti di routine riscrivono il messaggio
            # precedente, invece di impilarne uno nuovo ogni 60 secondi.
            #
            # Tre cose aprono sempre un messaggio NUOVO, e sono il motivo per cui questo non
            # trasforma le notifiche in aggiornamenti silenziosi:
            #  - un evento forzato (gol, rosso, rigore) o un recupero appena concluso: Telegram non
            #    suona sugli edit, quindi quello che deve farsi sentire non puo' essere un edit;
            #  - il cambio di blocco di 15 minuti: il canale conserva un filo storico leggibile
            #    invece di un'unica riga che cambia di nascosto per 90 minuti;
            #  - la chat diversa da quella della scheda: un id di messaggio vale solo nella sua
            #    chat, e una partita che entra o esce dai preferiti cambia destinazione.
            blocco_corrente = _blocco_minuto(minuto)
            stato_live = stato_partite.get(fixture_id, {})
            message_id_live = stato_live.get("message_id_live")
            scheda_aggiornabile = (
                MESSAGGIO_LIVE_PREFERITI_ATTIVO
                and is_fav
                and message_id_live
                and stato_live.get("blocco_messaggio_live") == blocco_corrente
                and stato_live.get("chat_messaggio_live") == chat_destinazione
                and not evento_forzato
                and not recupero_da_segnalare)
            message_id_inviato = None
            if scheda_aggiornabile and aggiorna_notifica_telegram(
                    message_id_live, foto_path, messaggio, reply_markup=keyboard,
                    chat_id=chat_destinazione):
                message_id_inviato = message_id_live
            if message_id_inviato is None:
                # La scheda precedente si cancella solo DOPO che la nuova e' partita davvero: se
                # l'invio fallisce (rete, rate-limit) si resta con quella vecchia, che e' scaduta
                # ma esiste, invece che senza niente.
                scheda_da_eliminare = stato_partite.get(fixture_id, {}).get("scheda_visibile")
                message_id_inviato = invia_notifica_telegram(foto_path, messaggio, reply_markup=keyboard, chat_id=chat_destinazione)
                if message_id_inviato:
                    if (ELIMINA_SCHEDA_PRECEDENTE_ATTIVO and scheda_da_eliminare
                            and scheda_da_eliminare.get("message_id") != message_id_inviato):
                        elimina_messaggio(scheda_da_eliminare.get("chat_id"),
                                          scheda_da_eliminare.get("message_id"))
                    stato_partite[fixture_id]["scheda_visibile"] = {
                        "message_id": message_id_inviato, "chat_id": chat_destinazione}
                if message_id_inviato and MESSAGGIO_LIVE_PREFERITI_ATTIVO and is_fav:
                    stato_partite[fixture_id].update({
                        "message_id_live": message_id_inviato,
                        "blocco_messaggio_live": blocco_corrente,
                        "chat_messaggio_live": chat_destinazione,
                    })
            if message_id_inviato and mostra_momentum:
                # Ricorda la didascalia esatta di questo messaggio: se poi si clicca "Momentum", la
                # foto viene sostituita ma il testo con tutti i dati (quote, statistiche, gol...)
                # resta quello originale, invece di essere rimpiazzato da un testo minimo.
                # Chiave stringa, la stessa forma con cui JSON la riscrive su disco: vedi il
                # commento in cmd_momentum_da_bottone. Scritta come intero, dopo un riavvio non
                # veniva piu' ritrovata.
                didascalie = stato_partite[fixture_id].setdefault("didascalie_notifiche", {})
                didascalie[str(message_id_inviato)] = messaggio
                # Un preferito viene notificato ogni 60s per tutta la partita: senza un tetto qui
                # si accumulano un centinaio di didascalie intere per fixture, riscritte su disco ad
                # ogni ciclo. Momentum si clicca sulle notifiche recenti, le piu' vecchie non
                # servono piu'.
                if len(didascalie) > MAX_DIDASCALIE_RICORDATE:
                    for vecchia in list(didascalie)[:-MAX_DIDASCALIE_RICORDATE]:
                        del didascalie[vecchia]
        # Fuori orario: niente invio, ma lo stato sotto viene comunque aggiornato come se avessimo
        # notificato (timestamp_notifica azzerato, notified_final marcato) - altrimenti un
        # preferito verrebbe rimosso automaticamente solo perché è notte, non perché si è spento
        # davvero (vedi DURATA_MAX_SENZA_NOTIFICA_PREFERITI più sopra).

        prev_notified = stato_partite.get(fixture_id, {}).get("notified_final", False)
        aggiornamento_notifica = {
            "tiri_casa": tiri_casa,
            "tiri_ospite": tiri_ospite,
            "timestamp_notifica": time.time(),
            "notified_final": prev_notified,
            # Blocco di 15 minuti in cui è caduta questa notifica: il salto di ritmo dei preferiti
            # (vedi deve_notificare) lo usa per non mandare un secondo messaggio nello stesso
            # blocco, qualunque fosse il motivo del primo.
            "blocco_ultima_notifica": _blocco_minuto(minuto),
        }
        # Minuto di gioco dell'ultima notifica: serve alla rimozione automatica dei preferiti per
        # misurare il silenzio in minuti giocati invece che di orologio (vedi sopra). Non si
        # sovrascrive con None quando l'API non espone il minuto (pausa), altrimenti si perderebbe
        # il riferimento buono registrato poco prima.
        if elapsed_raw is not None:
            aggiornamento_notifica["minuto_ultima_notifica"] = elapsed_raw
        stato_partite[fixture_id].update(aggiornamento_notifica)

        if foto_path and os.path.exists(foto_path):
            try:
                os.remove(foto_path)
            except:
                pass

    except Exception as e:
        log(f"Errore processa_partita: {e}")


def pulisci_partite_terminate(fixture_ids_live):
    ids_da_rimuovere = [fid for fid in stato_partite if fid not in fixture_ids_live]
    # Prima di cancellare lo stato: chi ha snapshot aperti va chiuso con il suo risultato finale,
    # altrimenti tutto il campione raccolto durante la partita resta orfano per sempre. Le partite
    # rimandate (tetto di chiamate raggiunto, o chiamata fallita) NON si cancellano: restano qui e
    # si riprovano al giro dopo.
    rimandate = chiudi_shadow_log_partite_sparite(ids_da_rimuovere)
    if rimandate:
        ids_da_rimuovere = [fid for fid in ids_da_rimuovere if fid not in rimandate]
    for fid in ids_da_rimuovere:
        # Nessun messaggio di fine partita
        SILENCED_MATCHES.pop(str(fid), None)
        # Il motivo dell'ultima valutazione vive quanto la partita: senza questa riga il dizionario
        # crescerebbe per tutta la vita del processo, una voce per ogni partita mai vista.
        MOTIVO_VALUTAZIONE_NOTIFICA.pop(fid, None)
        del stato_partite[fid]

    if ids_da_rimuovere:
        save_silenced(SILENCED_MATCHES)
        log(f"Partite terminate rimosse: {len(ids_da_rimuovere)}")

    # La cache statistiche/eventi copre anche partite mai entrate in stato_partite (es. richieste
    # da /status su una partita fuori whitelist, o da un comando prima che il loop la processi):
    # va ripulita controllando fixture_ids_live direttamente, non solo le chiavi di stato_partite.
    for cache in (_CACHE_STATISTICHE_PARTITA, _CACHE_EVENTI_PARTITA):
        for fid in [f for f in cache if f not in fixture_ids_live]:
            del cache[fid]

    # Stesso motivo per lo storico delle anomalie già notificate: senza pulizia crescerebbe per
    # tutta la vita del processo e, se lo stesso fixture_id tornasse live (partita sospesa e
    # ripresa), si porterebbe dietro anomalie vecchie facendole considerare "già viste".
    fid_da_rimuovere = [f for f in ANOMALIE_DIAGNOSTICA_NOTIFICATE if f not in fixture_ids_live]
    if fid_da_rimuovere:
        for fid in fid_da_rimuovere:
            del ANOMALIE_DIAGNOSTICA_NOTIFICATE[fid]
        salva_anomalie_diagnostica_notificate(ANOMALIE_DIAGNOSTICA_NOTIFICATE)

    # Stesso motivo per il backup dello storico momentum (vedi BACKUP_HISTORY_MOMENTUM): a fine
    # partita non serve più, e senza pulizia crescerebbe per sempre. Chiavi stringa (JSON), da
    # confrontare con fixture_ids_live (interi) convertendo questi ultimi.
    fixture_ids_live_str = {str(f) for f in fixture_ids_live}
    backup_da_rimuovere = [f for f in BACKUP_HISTORY_MOMENTUM if f not in fixture_ids_live_str]
    if backup_da_rimuovere:
        for f in backup_da_rimuovere:
            del BACKUP_HISTORY_MOMENTUM[f]
        salva_backup_history_momentum(BACKUP_HISTORY_MOMENTUM)

    # Anche i preferiti vanno ripuliti a fine partita: restavano dentro per sempre, quindi
    # favorite_matches.json cresceva ad ogni partita mai messa nei preferiti (a mano o
    # dall'auto-preferiti) e /favorites si riempiva di righe "ID 12345 (non live)" che non
    # sparivano più. Le partite non ancora iniziate non passano di qui (i preferiti si aggiungono
    # solo da una notifica live), quindi non si rischia di rimuovere un preferito "in attesa".
    preferiti_da_rimuovere = [f for f in FAVORITE_MATCHES if f not in fixture_ids_live_str]
    if preferiti_da_rimuovere:
        for f in preferiti_da_rimuovere:
            FAVORITE_MATCHES.discard(f)
        save_favorites(FAVORITE_MATCHES)
        log(f"Preferiti di partite terminate rimossi: {len(preferiti_da_rimuovere)}")


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
        {"command": "momentum", "description": "Grafico andamento pressione partita"},
        {"command": "favorites", "description": "Lista partite preferite"},
        {"command": "clearfavorites", "description": "Svuota lista preferiti"},
        {"command": "silenced", "description": "Lista partite silenziate"},
        {"command": "piano", "description": "Piano giornata e finestre orarie attive"},
        {"command": "stop", "description": "Metti il bot in pausa"},
        {"command": "riprendi", "description": "Riattiva il bot dopo /stop"},
        {"command": "modalitaessenziale", "description": "Solo gol/rossi/rigori/recupero lungo"},
        {"command": "modalitacompleta", "description": "Torna alle notifiche normali"},
        {"command": "testpreferiti", "description": "Verifica il canale preferiti dedicato"},
        {"command": "shadowlog", "description": "Riepilogo dati raccolti per la validazione"},
        {"command": "shadowlogstrategie", "description": "Dati raccolti in background sull'efficacia delle strategie"},
        {"command": "shadowlogdominio", "description": "A che quota di dominio arrivano le partite osservate"},
        {"command": "diagnostica", "description": "Controllo dal vivo di ogni partita live"},
        {"command": "funzioni", "description": "Funzioni stabili, in validazione, novità"},
        {"command": "apiusage", "description": "Chiamate API-Football fatte al giorno"},
        {"command": "intensita", "description": "Classifica partite live per intensità"},
        {"command": "analisi", "description": "Distribuzione storica gol per fascia di minuto"},
        {"command": "aggiornastorico", "description": "Aggiorna lo storico minutaggi"},
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
        # Tutto il corpo del ciclo è protetto da questo try/except: prima non lo era, quindi
        # un'eccezione imprevista in QUALSIASI punto (dato dell'API fuori formato, edge case mai
        # visto in una delle funzioni chiamate qui sotto) usciva dal while, terminava il processo
        # Python e Render lo faceva ripartire da capo - il pattern "Bot avviato" a raffica visto
        # in chat senza nessun deploy manuale di mezzo. processa_partita() e i comandi Telegram
        # (_esegui_comando) erano già protetti così; mancava solo il ciclo principale che li
        # richiama insieme alle funzioni automatiche (diagnostica, pulizia partite terminate,
        # salvataggio stato, piano giornata...). Un errore qui viene ora loggato e segnalato in
        # chat (se possibile), poi il ciclo riprende dopo una breve pausa invece di far morire
        # tutto il bot.
        try:
            if not CONFIG_VALIDA:
                # Battito comunque: il thread è vivo. Lo stato dichiarato dice che manca un token,
                # così l'endpoint di salute distingue questo caso da un ciclo bloccato.
                segna_battito("configurazione incompleta (manca un token)", lavora=False)
                log("CONFIGURAZIONE INCOMPLETA - Attendo 30 secondi...")
                time.sleep(30)
                continue

            if STATO_PAUSA.get("in_pausa"):
                ora = time.time()
                da_quanto = ora - STATO_PAUSA.get("dal", ora)
                if ora - STATO_PAUSA.get("ultimo_promemoria", 0) >= INTERVALLO_PROMEMORIA_PAUSA:
                    ore = round(da_quanto / 3600, 1)
                    invia_messaggio_telegram(f"⏸ Bot ancora in pausa da {ore}h. Invia /riprendi per riattivarlo.")
                    STATO_PAUSA["ultimo_promemoria"] = ora
                    salva_pausa(STATO_PAUSA)
                segna_battito(f"in pausa manuale da {round(da_quanto / 60)} min", lavora=False)
                log(f"In pausa manuale da {round(da_quanto / 60)} min, nessuna chiamata API. Attesa {INTERVALLO_CICLO_MORTO}s...")
                time.sleep(INTERVALLO_CICLO_MORTO)
                continue

            # Fuori dalla fascia oraria configurata: il monitoraggio (statistiche, quote, shadow-log)
            # resta comunque attivo 24/7 - notifiche_attive spegne solo l'invio delle notifiche più
            # sotto (vedi processa_partita), non il ciclo in sé. Questo evita sia di perdere dati utili
            # alla validazione futura, sia il bug delle partite che finiscono a cavallo dell'orario di
            # stop restando orfane nello shadow-log (nessun risultato finale mai registrato).
            notifiche_attive = dentro_orario_attivo()
            if not notifiche_attive:
                log(f"Fuori dall'orario attivo ({ORARIO_ATTIVO_INIZIO_ORA:02d}:{ORARIO_ATTIVO_INIZIO_MINUTO:02d}-"
                    f"{ORARIO_ATTIVO_FINE_ORA:02d}:{ORARIO_ATTIVO_FINE_MINUTO:02d}): monitoraggio silenzioso, nessuna notifica.")

            aggiorna_piano_giornata_se_serve()
            aggiorna_quote_prepartita_imminenti()

            ciclo_numero += 1
            segna_battito("ciclo in corso", ciclo_numero)
            log(f"\n=== Ciclo #{ciclo_numero} - {time.strftime('%H:%M:%S')} ===")

            partite = get_partite_live()
            chiamata_partite_live_fallita = (time.time() - ULTIMO_ERRORE_GET_PARTITE_LIVE) < 20
            # Deduplicazione per fixture_id: l'API a volte restituisce la stessa partita due volte
            # nello stesso payload live (osservato 3 volte in 48h di log produzione), e senza questa
            # guardia processa_partita() gira due volte a ~2s di distanza sullo stesso fixture. Il
            # rilevamento gol confronta lo score con quello dell'ultimo ciclo (vedi
            # classifica_cambio_punteggio): finche' il primo giro non aggiorna score_home/away in
            # stato_partite, il secondo giro rilegge il prev_score vecchio e rigenera lo stesso
            # "GOL RILEVATO! 0-0 -> 0-1" (con la relativa notifica gol duplicata in chat). Si tiene
            # la prima occorrenza per fixture_id, in ordine di apparizione.
            visti = set()
            partite_deduplicate = []
            duplicati_scartati = 0
            for f in partite:
                fid = f.get("fixture", {}).get("id")
                if fid is None:
                    partite_deduplicate.append(f)
                    continue
                if fid in visti:
                    duplicati_scartati += 1
                    continue
                visti.add(fid)
                partite_deduplicate.append(f)
            if duplicati_scartati:
                log(f"⚠️ API-Football ha restituito {duplicati_scartati} fixture duplicati nello stesso payload live: scartati")
            partite = partite_deduplicate
            in_whitelist = [
                f for f in partite
                if fixture_in_whitelist(f)
            ]
            partite_valide = [f for f in in_whitelist if not partita_tra_giovanili(f)]
            # Le giovanili si contano a parte invece di sparire dentro il totale: se un giorno il
            # filtro dovesse escludere una partita vera, il numero lo mostra subito invece di
            # lasciar credere che quella partita non fosse live.
            escluse_giovanili = len(in_whitelist) - len(partite_valide)
            log(f"Partite live: {len(partite)} totali, {len(partite_valide)} valide"
                + (f" ({escluse_giovanili} escluse: squadre giovanili)" if escluse_giovanili else ""))

            if notifiche_attive and (ciclo_numero == 1 or ciclo_numero % 10 == 0):
                if chiamata_partite_live_fallita:
                    invia_messaggio_telegram(
                        f"Bot attivo - Ciclo #{ciclo_numero}\n"
                        f"Ultima chiamata API fallita (vedi errore sopra): dato partite live non affidabile"
                    )
                else:
                    invia_messaggio_telegram(
                        f"Bot attivo - Ciclo #{ciclo_numero}\n"
                        f"Partite live monitorate: {len(partite_valide)}"
                    )

            # Si itera solo sulle partite valide (non su tutte le partite live del mondo): lo sleep(1)
            # tra una chiamata e l'altra serve a distanziare le chiamate API fatte da processa_partita
            # (statistiche + eventi), quindi non ha senso pagarlo anche per le migliaia di partite
            # scartate (dilettanti, giovanili, campionati fuori whitelist) su cui non viene fatta
            # nessuna chiamata. stato_partite contiene solo partite valide, quindi fixture_ids_live
            # può essere costruito direttamente da partite_valide.
            fixture_ids_live = {
                f["fixture"]["id"] for f in partite_valide if f.get("fixture", {}).get("id")
            }
            # I preferiti vengono ricontrollati ogni INTERVALLO_CICLO_MOMENTUM (60s) invece che ogni
            # INTERVALLO_CICLO_ATTIVO (180s): più punti storici in meno tempo = grafico momentum più
            # denso. Le altre partite valide restano al ritmo normale. Il ciclo esterno (sleep finale)
            # viene comunque accorciato quando c'è almeno un preferito live, altrimenti il gate qui
            # sotto non verrebbe mai ricontrollato abbastanza spesso da avere effetto.
            preferito_live = False
            for fixture in partite_valide:
                fid = fixture.get("fixture", {}).get("id")
                e_preferita = fid is not None and str(fid) in FAVORITE_MATCHES
                if e_preferita:
                    preferito_live = True
                intervallo_minimo = INTERVALLO_CICLO_MOMENTUM if e_preferita else INTERVALLO_CICLO_ATTIVO
                ultimo_controllo = stato_partite.get(fid, {}).get("ultimo_controllo", 0) if fid is not None else 0
                # "ultimo_controllo" è persistito su disco: sopravvive ai riavvii. Se un redeploy è
                # più veloce di intervallo_minimo (180s), al primo ciclo dopo il riavvio quel
                # timestamp risulta ancora "recente" per l'orologio reale, e la partita verrebbe
                # SALTATA proprio nel giro in cui più serve un controllo fresco - lasciando
                # last_minute fermo al valore pre-riavvio mentre l'API è già andata avanti (visto in
                # produzione: la diagnostica lo segnalava come falsa anomalia "TRACCIAMENTO", perché
                # confronta lo stesso minuto fresco dell'API con uno stato_partite non ancora
                # aggiornato). Il primo ciclo dopo l'avvio del processo ignora sempre questa soglia,
                # per ricontrollare subito ogni partita live indipendentemente da quanto tempo reale
                # sia passato dal riavvio.
                if ciclo_numero > 1 and time.time() - ultimo_controllo < intervallo_minimo:
                    continue
                processa_partita(fixture, notifiche_attive)
                if fid is not None:
                    stato_partite.setdefault(fid, {})["ultimo_controllo"] = time.time()
                time.sleep(1)

            # La pulizia si basa su "questa partita non è più tra le live", ma get_partite_live()
            # restituisce una lista vuota SIA quando non c'è nessuna partita in corso SIA quando la
            # chiamata è fallita (rate-limit, quota giornaliera esaurita, timeout): i due casi sono
            # indistinguibili dal valore di ritorno. Senza questa guardia, un singolo rate-limit
            # sulla chiamata live faceva passare un set vuoto e cancellava in un colpo solo lo stato
            # di TUTTE le partite in corso - storico momentum compreso, backup indipendente
            # incluso (BACKUP_HISTORY_MOMENTUM viene ripulito qui dentro), che era stato aggiunto
            # proprio per sopravvivere ai reset. I rate-limit su get_partite_live sono documentati
            # nei log di produzione, quindi non è un caso teorico.
            if chiamata_partite_live_fallita:
                log("Chiamata partite live fallita: salto la pulizia delle partite terminate "
                    "(un elenco vuoto qui non significa che le partite siano finite)")
            else:
                pulisci_partite_terminate(fixture_ids_live)
            salva_stato_partite(stato_partite)
            # Un solo messaggio per ciclo con tutte le partite dal feed congelato, invece
            # di uno per partita mentre si scorre l'elenco.
            invia_riepilogo_feed_congelati(notifiche_attive)
            invia_report_intensita_automatico(partite_valide, notifiche_attive)
            esegui_diagnostica_automatica(partite_valide, notifiche_attive)
            aggiorna_storico_minutaggi_automatico()

            # Scheduler adattivo (piano giornata): fuori da ogni finestra attiva prevista e senza
            # partite valide effettivamente in corso in questo momento, il ciclo rallenta invece di
            # continuare a interrogare l'API ogni pochi minuti per niente. Se il piano non è ancora
            # disponibile (es. primo avvio prima che la generazione vada a buon fine) ci si comporta
            # come se si fosse sempre in finestra attiva, per non restare ciechi.
            piano_disponibile = PIANO_GIORNATA.get("data") is not None
            in_finestra_attiva = dentro_finestra_attiva(PIANO_GIORNATA) if piano_disponibile else True
            ciclo_attivo = in_finestra_attiva or bool(partite_valide)
            prossimo_intervallo = INTERVALLO_CICLO_ATTIVO if ciclo_attivo else INTERVALLO_CICLO_MORTO
            if preferito_live:
                prossimo_intervallo = min(prossimo_intervallo, INTERVALLO_CICLO_MOMENTUM)
            log(f"Attesa {prossimo_intervallo}s ({'finestra attiva' if ciclo_attivo else 'nessuna finestra attiva, ciclo rallentato'}{', preferito live: ciclo accelerato' if preferito_live else ''})...")
            # Ultima riga prima dell'attesa: il giro è arrivato in fondo senza eccezioni.
            segna_giro_completato()
            time.sleep(prossimo_intervallo)
        except Exception as e:
            log(f"Errore imprevisto nel ciclo principale (il bot NON si riavvia, riprende dopo una pausa): {e}")
            try:
                invia_messaggio_telegram("⚠️ Errore imprevisto nel ciclo principale (vedi log Render per il dettaglio). Il bot continua, riprende tra poco.")
            except Exception:
                pass
            time.sleep(30)