import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests

# 1. Mini Web Server per ingannare Render e tenere aperta la porta
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Online!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()




def run_web_server():
  port = int(os.environ.get("PORT", 8080))
  server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
  print(f"Web server avviato sulla porta {port}")
  server.serve_forever()


# 2. Funzione per inviare il messaggio Telegram
def send_telegram_message():
  with open("config.json", "r") as f:
    config = json.load(f)

  bot_token = config["telegram_bot_token"]
  chat_id = config["telegram_chat_id"]

  url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
  payload = {
      "chat_id": chat_id,
      "text": (
          "🚀 *Bot Sports Trading avviato con successo!*\nIl sistema è attivo"
          " sul piano Free di Render."
      ),
      "parse_mode": "Markdown",
  }

  response = requests.post(url, json=payload)
  if response.status_code == 200:
    print("Messaggio Telegram inviato con successo!")
  else:
    print(f"Errore invio Telegram: {response.text}")


# 3. Esecuzione principale
if __name__ == "__main__":
  # Avvia il server HTTP in background
  server_thread = threading.Thread(target=run_web_server)
  server_thread.daemon = True
  server_thread.start()

  # Invia la notifica Telegram
  send_telegram_message()

  # Mantiene lo script in esecuzione
  server_thread.join()
