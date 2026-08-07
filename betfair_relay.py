"""Relay locale per le chiamate Betfair.

Gira su un PC con IP italiano (richiesto dall'Exchange regolamentato ADM) ed espone
un endpoint HTTP che il bot su Render richiama al posto di contattare Betfair
direttamente. Da esporre su internet con un Cloudflare Tunnel (o simile) puntato a
questa porta locale.

Avvio: python betfair_relay.py
"""
import os
import time
from datetime import datetime

import requests
from flask import Flask, jsonify, request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BETFAIR_APP_KEY = os.environ.get("BETFAIR_APP_KEY")
BETFAIR_USERNAME = os.environ.get("BETFAIR_USERNAME")
BETFAIR_PASSWORD = os.environ.get("BETFAIR_PASSWORD")
BETFAIR_CERT_PATH = os.environ.get("BETFAIR_CERT_PATH", "certs/betfair-client.crt")
BETFAIR_KEY_PATH = os.environ.get("BETFAIR_KEY_PATH", "certs/betfair-client.key")
RELAY_SECRET = os.environ.get("RELAY_SECRET")
RELAY_PORT = int(os.environ.get("RELAY_PORT", "5001"))

BETFAIR_CONFIGURATO = all([BETFAIR_APP_KEY, BETFAIR_USERNAME, BETFAIR_PASSWORD, BETFAIR_CERT_PATH, BETFAIR_KEY_PATH])

BETFAIR_LOGIN_URL = "https://identitysso-cert.betfair.it/api/certlogin"
BETFAIR_API_URL = "https://api.betfair.it/exchange/betting/json-rpc/v1"

# User-Agent "da browser": senza, alcune richieste vengono bloccate dalla protezione
# anti-bot (Cloudflare) davanti alle API Betfair, che riconosce lo User-Agent di default
# della libreria requests come traffico automatizzato.
BETFAIR_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

BETFAIR_SESSION_TOKEN = None
BETFAIR_SESSION_TIMESTAMP = 0
BETFAIR_SESSION_TTL = 4 * 3600  # rinnova la sessione ogni 4 ore per sicurezza

app = Flask(__name__)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def betfair_login(force=False):
    """Login non interattivo (bot) con certificato client, per l'Exchange italiano.
    Riusa il token in cache se ancora valido, altrimenti rifà il login."""
    global BETFAIR_SESSION_TOKEN, BETFAIR_SESSION_TIMESTAMP
    if not BETFAIR_CONFIGURATO:
        return None
    now = time.time()
    if not force and BETFAIR_SESSION_TOKEN and (now - BETFAIR_SESSION_TIMESTAMP) < BETFAIR_SESSION_TTL:
        return BETFAIR_SESSION_TOKEN
    try:
        response = requests.post(
            BETFAIR_LOGIN_URL,
            data={"username": BETFAIR_USERNAME, "password": BETFAIR_PASSWORD},
            headers={
                "X-Application": BETFAIR_APP_KEY,
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": BETFAIR_USER_AGENT,
                "Accept": "application/json",
            },
            cert=(BETFAIR_CERT_PATH, BETFAIR_KEY_PATH),
            timeout=15
        )
        data = response.json()
        if data.get("loginStatus") == "SUCCESS":
            BETFAIR_SESSION_TOKEN = data.get("sessionToken")
            BETFAIR_SESSION_TIMESTAMP = now
            log("Betfair: login riuscito")
            return BETFAIR_SESSION_TOKEN
        log(f"Betfair: login fallito - {data.get('loginStatus')}")
        BETFAIR_SESSION_TOKEN = None
        return None
    except Exception as e:
        log(f"Errore login Betfair: {e}")
        return None


def betfair_api_call(method, params=None, retry=True):
    """Chiamata generica all'API Betting di Betfair (JSON-RPC). Se la sessione risulta scaduta,
    rifà il login una volta sola e ritenta."""
    token = betfair_login()
    if not token:
        return None, "Login Betfair non riuscito"
    payload = [{
        "jsonrpc": "2.0",
        "method": f"SportsAPING/v1.0/{method}",
        "params": params or {},
        "id": 1
    }]
    headers = {
        "X-Application": BETFAIR_APP_KEY,
        "X-Authentication": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": BETFAIR_USER_AGENT,
    }
    try:
        response = requests.post(BETFAIR_API_URL, json=payload, headers=headers, timeout=15)
    except Exception as e:
        log(f"Errore rete chiamata Betfair {method}: {e}")
        return None, f"Errore rete: {e}"

    if response.status_code != 200 or not response.text.strip():
        log(f"Errore chiamata Betfair ({method}): HTTP {response.status_code} - body: {response.text[:500]!r}")
        return None, f"HTTP {response.status_code}"

    try:
        risultato = response.json()
    except Exception as e:
        log(f"Errore parsing risposta Betfair ({method}): {e} - HTTP {response.status_code} - body: {response.text[:500]!r}")
        return None, f"Errore parsing risposta: {e}"

    if isinstance(risultato, list) and risultato:
        item = risultato[0]
        errore = item.get("error")
        if errore:
            codice = ((errore.get("data") or {}).get("APINGException") or {}).get("errorCode", "")
            if retry and codice in ("INVALID_SESSION_INFORMATION", "NO_SESSION"):
                log("Betfair: sessione scaduta, rifaccio login")
                betfair_login(force=True)
                return betfair_api_call(method, params, retry=False)
            log(f"Errore API Betfair ({method}): {errore}")
            return None, str(errore)
        return item.get("result"), None
    log(f"Risposta Betfair ({method}) inattesa: {str(risultato)[:500]}")
    return None, "Risposta inattesa"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "betfair_configurato": BETFAIR_CONFIGURATO})


@app.route("/betfair-call", methods=["POST"])
def betfair_call_endpoint():
    if not RELAY_SECRET or request.headers.get("X-Relay-Secret") != RELAY_SECRET:
        return jsonify({"error": "non autorizzato"}), 401
    if not BETFAIR_CONFIGURATO:
        return jsonify({"error": "Betfair non configurato sul relay"}), 503

    body = request.get_json(silent=True) or {}
    method = body.get("method")
    if not method:
        return jsonify({"error": "campo 'method' mancante"}), 400
    params = body.get("params") or {}

    risultato, errore = betfair_api_call(method, params)
    if errore:
        return jsonify({"error": errore}), 502
    return jsonify({"result": risultato})


if __name__ == "__main__":
    log(f"Relay Betfair avviato sulla porta {RELAY_PORT}")
    log(f"Betfair configurato: {'SI' if BETFAIR_CONFIGURATO else 'NO (variabili mancanti)'}")
    if not RELAY_SECRET:
        log("ATTENZIONE: RELAY_SECRET non impostato, l'endpoint rifiuterà tutte le richieste")
    app.run(host="127.0.0.1", port=RELAY_PORT)
