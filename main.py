================================================================================
                    TRADING LIVE BOT - VERSIONE CORRETTA
                        Changelog & Anteprima Funzioni
================================================================================

DATA: 3 Agosto 2026
STATO: Pronto per deploy su Render

================================================================================
1. PROBLEMI RISOLTI (dai log)
================================================================================

[ERRORE 1] Telegram 404 "Not Found"
  Causa: TELEGRAM_BOT_TOKEN assente o errato. Il bot tentava chiamate HTTP
         con token vuoto/invalido.
  Fix:   Aggiunto check CONFIG_VALIDA all'avvio. Se manca una credenziale,
         il bot logga l'errore in modo chiaro e NON invia richieste inutili.

[ERRORE 2] API-Football 403 Forbidden
  Causa: API_FOOTBALL_KEY assente (stesso problema di configurazione).
  Fix:   Stesso check sopra: nessuna chiamata API senza key valida.

[ERRORE 3] Comandi Telegram non rispondono
  Causa: Il thread poll_callbacks falliva silenziosamente perché il token
         era None; inoltre la variabile locale 'partite' ombreggiava quella
         globale creando confusione nei log.
  Fix:   - Aggiunto 'if not TELEGRAM_BOT_TOKEN: continue' nel loop di polling
         - Rinominata variabile locale da 'partite' a 'partite_cmd'

[ERRORE 4] Preferiti bloccano tutte le altre partite (BUG LOGICO)
  Causa: In deve_notificare(), se esisteva anche solo un preferito salvato,
         il metodo ritornava False per TUTTE le altre partite.
  Fix:   Rimosso il filtro esclusivo. Ora i preferiti BYPASSANO le soglie
         (notifica immediata se stats cambiate) ma non escludono le altre
         partite che seguono le regole normali.

[ERRORE 5] Notifiche finali indesiderate
  Causa: pulisci_partite_terminate() inviava un messaggio anche per partite
         mai entrate nelle regole di notifica (es. filtrate dal campionato).
  Fix:   Aggiunto check 'timestamp_notifica > 0': notifica finale solo se
         la partita era stata effettivamente monitorata in precedenza.

================================================================================
2. ANTEPRIMA FUNZIONI
================================================================================

2.1 VALIDAZIONE CONFIGURAZIONE
--------------------------------
All'avvio il bot verifica che tutte le credenziali siano presenti:
  - TELEGRAM_BOT_TOKEN
  - TELEGRAM_CHAT_ID
  - API_FOOTBALL_KEY

Se manca anche solo una:
  -> Stampa: "CONFIGURAZIONE INCOMPLETA - Impossibile avviare il bot"
  -> Il ciclo principale entra in attesa (sleep 30s) senza spammare errori
  -> Nessuna chiamata HTTP a Telegram o API-Football

2.2 CICLO PRINCIPALE
--------------------
Ogni 3 minuti:
  1. Recupera partite live da API-Football
  2. Filtra campionati validi (esclude: femminili, U15-U20, amichevoli,
     dilettanti, riserve)
  3. Per ogni partita valida:
     - Recupera statistiche (tiri, tiri in porta, corner)
     - Recupera eventi e gol
     - Calcola delta ultimi 15 minuti
     - Applica regole di notifica
     - Genera grafico a barre orizzontali
     - Invia notifica Telegram con tastiera inline
  4. Pulisce partite terminate

2.3 REGOLE DI NOTIFICA LIVE
----------------------------
Una partita viene notificata se:

  A) DIFFERENZA TIRI >= 3
     Le squadre hanno almeno 3 tiri di differenza (dominanza offensiva)

  B) PARTITA MOLTO ATTIVA (primi 25 min)
     Almeno 6 tiri totali entro il 25° minuto

  C) FORZATA OGNI 30 MINUTI
     Se sono passati 30 minuti dall'ultima notifica E ci sono almeno 4 tiri

  D) PREFERITI (BOOST)
     Se la partita è nei preferiti, viene notificata SEMPRE che le
     statistiche siano cambiate rispetto al ciclo precedente

  E) ANTI-SPAM
     Se le statistiche totali non sono cambiate rispetto al ciclo
     precedente, NON notifica (evita duplicati)

2.4 STATISTICHE DELTA 15 MINUTI
--------------------------------
Nelle notifiche live, il testo mostra l'incremento degli ultimi 15 min:

  Esempio:
  • Tiri totali: 2🔥 (+2) - 1 (+1) 🏠
  • Tiri in porta: 1🔥 (+1) - 0 (+0)
  • Corner: 1 (+1) - 0 (+0)

  Legenda:
  - Numeri grandi = valori cumulativi reali dalla partita
  - Numeri in parentesi (+X) = incremento negli ultimi 15 min
  - 🔥 = soglia di attenzione superata (2+ tiri totali, 2+ tiri in porta)
  - 🏠 / ✈️ / ⚖️ = freccia direzionale (casa / ospite / equilibrato)

2.5 GRAFICO A BARRE ORIZZONTALI
--------------------------------
Allegato ad ogni notifica live:
  - Verde = squadra di casa
  - Rosso = squadra ospite
  - Sfondo scuro (#1e1e1e)
  - Barre proporzionali ai totali cumulativi
  - Nessun dato = "Nessun dato"

2.6 GOL
--------
Nelle notifiche live:
  🥇 Primo gol: 25' (M. Meerdink)
  ⚡ Ultimo gol: 66' (R. Daal)

Nelle notifiche finali (non silenziate):
  Stesse informazioni + statistiche finali complete

2.7 TASTIERA INLINE
--------------------
Ogni notifica live include due bottoni:

  [⭐ Aggiungi ai preferiti]  -> toggle preferito
  [🔕 Silenzia questa partita] -> stop notifiche live fino al fischio finale

  Se già preferita: [❌ Rimuovi dai preferiti]
  Se già silenziata: nessun bottone (notifica finale arriva comunque)

2.8 SILENZIA PARTITA
--------------------
- Click su "🔕 Silenzia questa partita"
- Salva score attuale e minuto su file JSON (persistenza)
- Nessuna notifica live fino alla fine
- Notifica finale ARRIVA COMUNQUE in formato minimal:

  Esempio:
  🏁 PSV Eindhoven vs AZ Alkmaar
  🏆 Super Cup
  🏁 Risultato finale: 0 - 5 +1✈️
  🔕 Silenziato al 89'
  ⏱️ Gol dopo: 92'✈️

2.9 COMANDI TELEGRAM
--------------------
/help              -> Lista comandi disponibili
/status <squadra>  -> Info live su una partita specifica
/favorites         -> Lista partite preferite
/clearfavorites    -> Svuota lista preferiti
/silenced          -> Lista partite silenziate
/live              -> Mostra tutte le partite live

2.10 PERSISTENZA
-----------------
- silenced_matches.json -> Partite silenziate (sopravvive ai restart)
- favorite_matches.json -> Partite preferite (sopravvive ai restart)
- Entrambi in .gitignore (non vanno su GitHub)

================================================================================
3. VARIABILI D'AMBIENTE (Render)
================================================================================

TELEGRAM_BOT_TOKEN    -> Token del bot Telegram (@BotFather)
TELEGRAM_CHAT_ID      -> ID della chat dove inviare le notifiche
API_FOOTBALL_KEY      -> Chiave API-Football (piano €20/mese)

Fallback: config.json nella stessa cartella di main.py

================================================================================
4. DIPENDENZE
================================================================================

requests>=2.31.0
matplotlib>=3.7.0

================================================================================
5. NOTE TECNICHE
================================================================================

- Backend matplotlib: 'Agg' (headless, funziona su Render)
- Grafico PNG temporaneo, cancellato dopo invio
- Server HTTP interno porta 10000 per health check Render
- Polling Telegram callback in thread separato daemon
- WEB_CONCURRENCY=1 (default su Render free tier)

================================================================================
                            FINE DOCUMENTO
================================================================================