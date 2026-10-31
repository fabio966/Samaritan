"""
========================================================================================
PROJECT SAMARITAN - Autonomous E-Commerce Operating System
Lead Architect Core Module
========================================================================================
Modulo: samaritan_core.py
Descrizione: Loop autonomo di background con battito cardiaco (heartbeat) a intervalli
             regolari. Integra:
             - Sensori "Occhi": monitoraggio canali di vendita (Shopify attivo, eBay stub).
             - Canale "Bocca": sistema di notifica bidirezionale Telegram Bot.
             - Canale "Orecchie": ascolto messaggi Telegram in background (polling non bloccante).
             - Modulo OTA (Over-The-Air): aggiornamento automatico immediato via Telegram
               e notturno via repository GitHub (raw URL).
========================================================================================
"""

import os
import sys
import time
import json
import logging
import threading
from pathlib import Path
from datetime import datetime

import requests
from dotenv import load_dotenv

# --------------------------------------------------------------------------------------
# 1. CONFIGURAZIONE LOGGING
# --------------------------------------------------------------------------------------
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [SAMARITAN] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger("SamaritanCore")
logger.setLevel(logging.INFO)

# Evita duplicazione degli handler in caso di riavvio nello stesso runtime
if not logger.handlers:
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    logger.addHandler(console_handler)

    log_file_path = Path(__file__).parent / "samaritan.log"
    file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    logger.addHandler(file_handler)
else:
    log_file_path = Path(__file__).parent / "samaritan.log"

# Costanti di sistema
GITHUB_RAW_URL = "https://raw.githubusercontent.com/fabio966/Samaritan/main/samaritan_core.py"
RESTART_LOCK = threading.Lock()
RESTART_IN_PROGRESS = False

# --------------------------------------------------------------------------------------
# 2. CARICAMENTO VARIABILI D'AMBIENTE (.env)
# --------------------------------------------------------------------------------------
def load_env(env_path: Path = None) -> dict:
    """Carica le variabili d'ambiente dal file .env tramite python-dotenv."""
    if env_path is None:
        env_path = Path(__file__).parent / ".env"
    
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=True)
    else:
        logger.warning(f"File .env non trovato in: {env_path}")

    config = {
        "SHOPIFY_SHOP": os.getenv("SHOPIFY_SHOP", ""),
        "SHOPIFY_CLIENT_ID": os.getenv("SHOPIFY_CLIENT_ID", ""),
        "SHOPIFY_CLIENT_SECRET": os.getenv("SHOPIFY_CLIENT_SECRET", ""),
        "SHOPIFY_ACCESS_TOKEN": os.getenv("SHOPIFY_ACCESS_TOKEN", ""),
        "SHOPIFY_API_VERSION": os.getenv("SHOPIFY_API_VERSION", "2024-07"),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", "")
    }
    return config

# --------------------------------------------------------------------------------------
# 3. MODULO BOCCA: TELEGRAM NOTIFIER
# --------------------------------------------------------------------------------------
def bocca_telegram(messaggio: str, config: dict = None) -> bool:
    """
    Invia un messaggio testuale alla chat Telegram configurata tramite Telegram Bot API.
    Gestisce in modo sicuro le eccezioni di rete per garantire la continuità del loop.
    """
    bot_token = (config.get("TELEGRAM_BOT_TOKEN") if config else None) or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = (config.get("TELEGRAM_CHAT_ID") if config else None) or os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logger.error("[BOCCA TELEGRAM] Impossibile inviare: TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID mancanti nel file .env.")
        return False

    endpoint_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": messaggio
    }

    try:
        response = requests.post(endpoint_url, data=payload, timeout=12)
        if response.status_code == 200:
            logger.info(f"[BOCCA TELEGRAM] Messaggio inviato: \"{messaggio}\"")
            return True
        else:
            logger.error(f"[BOCCA TELEGRAM] Risposta API Telegram fallita (HTTP {response.status_code}): {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"[BOCCA TELEGRAM] Errore di rete durante la connessione a Telegram: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"[BOCCA TELEGRAM] Errore imprevisto nell'invio del messaggio: {str(e)}")
        return False

# --------------------------------------------------------------------------------------
# 3b. MODULO BOCCA: MENU INTERATTIVO (INLINE KEYBOARD)
# --------------------------------------------------------------------------------------
def _telegram_api(metodo: str, payload: dict, config: dict = None, timeout: int = 12) -> bool:
    """Chiamata POST generica alle Telegram Bot API. Non solleva mai eccezioni."""
    bot_token = (config.get("TELEGRAM_BOT_TOKEN") if config else None) or os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.error(f"[TELEGRAM API] {metodo}: TELEGRAM_BOT_TOKEN mancante.")
        return False
    try:
        response = requests.post(f"https://api.telegram.org/bot{bot_token}/{metodo}", json=payload, timeout=timeout)
        if response.status_code == 200:
            return True
        logger.error(f"[TELEGRAM API] {metodo} fallita (HTTP {response.status_code}): {response.text}")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"[TELEGRAM API] {metodo}: errore di rete: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"[TELEGRAM API] {metodo}: errore imprevisto: {str(e)}")
        return False

def invia_menu_telegram(config: dict, chat_id: str = None) -> bool:
    """Invia il Pannello di Controllo con InlineKeyboardMarkup su 2 righe."""
    chat_id = chat_id or config.get("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    payload = {
        "chat_id": chat_id,
        "text": "Pannello di Controllo Samaritan:",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "📦 Controlla Giacenze", "callback_data": "cmd_giacenze"},
                    {"text": "🛒 Stato Shopify", "callback_data": "cmd_shopify"},
                ],
                [
                    {"text": "⚙️ Stato Sistema", "callback_data": "cmd_sistema"},
                ],
            ]
        },
    }
    ok = _telegram_api("sendMessage", payload, config)
    if ok:
        logger.info("[MENU TELEGRAM] Pannello di Controllo inviato.")
    return ok

# Risposte fittizie associate ai pulsanti (da collegare alle API reali nel prossimo step)
RISPOSTE_CALLBACK = {
    "cmd_giacenze": "Controllo giacenze in corso...",
    "cmd_shopify": "Controllo stato Shopify in corso...",
    "cmd_sistema": "Controllo stato del sistema in corso...",
}

def gestisci_callback_query(callback_query: dict, config: dict, authorized_chat_id: str):
    """
    Gestisce il click su un pulsante inline:
    1. answerCallbackQuery (obbligatorio: fa sparire l'icona di caricamento dal pulsante)
    2. invio di un normale messaggio di testo di risposta.
    """
    callback_id = callback_query.get("id")
    data = callback_query.get("data", "")
    sender_id = str(callback_query.get("from", {}).get("id", "")).strip()
    chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", sender_id)).strip()

    # Risponde SEMPRE alla callback per togliere l'orologio, anche se non autorizzato
    autorizzato = sender_id == authorized_chat_id
    _telegram_api("answerCallbackQuery", {"callback_query_id": callback_id}, config, timeout=8)

    if not autorizzato:
        logger.warning(f"[MENU TELEGRAM] Callback '{data}' da ID non autorizzato '{sender_id}'. Ignorata.")
        return

    logger.info(f"[MENU TELEGRAM] Click ricevuto: '{data}'")
    testo = RISPOSTE_CALLBACK.get(data)
    if testo:
        _telegram_api("sendMessage", {"chat_id": chat_id, "text": testo}, config)
    else:
        logger.warning(f"[MENU TELEGRAM] callback_data sconosciuto: '{data}'")
        _telegram_api("sendMessage", {"chat_id": chat_id, "text": "Comando non riconosciuto."}, config)

# --------------------------------------------------------------------------------------
# 4. MODULO RIAVVIO & OTA (OVER-THE-AIR)
# --------------------------------------------------------------------------------------
def applica_aggiornamento_e_riavvia(nuovo_codice: str, config: dict, sorgente: str):
    """
    Verifica l'integrità sintattica del nuovo codice, crea un backup del file attuale,
    sovrascrive samaritan_core.py, invia la notifica su Telegram e riavvia il processo.
    """
    global RESTART_IN_PROGRESS
    with RESTART_LOCK:
        if RESTART_IN_PROGRESS:
            return
        RESTART_IN_PROGRESS = True

    logger.info(f"[OTA RESTART] Validazione e applicazione aggiornamento ricevuto da: {sorgente}")

    # Validazione preventiva del codice Python per evitare brick dello script
    try:
        compile(nuovo_codice, "samaritan_core.py", "exec")
        logger.info("[OTA RESTART] Validazione sintassi Python completata con successo.")
    except Exception as e:
        logger.error(f"[OTA RESTART] Codice ricevuto non valido o corrotto ({e}). Aggiornamento annullato per sicurezza.")
        bocca_telegram(f"ERRORE OTA: Il codice ricevuto da {sorgente} contiene errori di sintassi. Aggiornamento annullato.", config=config)
        RESTART_IN_PROGRESS = False
        return

    target_file = Path(__file__).resolve()
    backup_file = target_file.with_suffix(".py.bak")

    try:
        # Backup del file corrente
        backup_file.write_text(target_file.read_text(encoding="utf-8"), encoding="utf-8")
        # Sovrascrittura con la nuova versione
        target_file.write_text(nuovo_codice, encoding="utf-8")
        logger.info(f"[OTA RESTART] File {target_file.name} sovrascritto con successo (backup salvato in {backup_file.name}).")
    except Exception as e:
        logger.error(f"[OTA RESTART] Errore durante la scrittura su disco: {str(e)}")
        bocca_telegram(f"ERRORE OTA: Impossibile sovrascrivere il file locale: {str(e)}", config=config)
        RESTART_IN_PROGRESS = False
        return

    # Notifica Telegram OBBLIGATORIA prima dell'esecuzione del riavvio
    messaggio_restart = "Aggiornamento ricevuto. Installazione e riavvio in corso..."
    bocca_telegram(messaggio_restart, config=config)

    # Breve pausa tecnica per permettere a Telegram di finalizzare la richiesta di rete
    time.sleep(1.5)

    logger.info("[OTA RESTART] Esecuzione riavvio autonomo del processo (os.execv)...")
    sys.stdout.flush()
    sys.stderr.flush()

    for h in list(logger.handlers):
        h.flush()
        h.close()

    # Riavvio trasparente del processo Python preservando gli argomenti di avvio
    python_bin = sys.executable
    args = [python_bin, str(target_file)] + sys.argv[1:]
    os.execv(python_bin, args)

def cerca_aggiornamenti(config: dict) -> bool:
    """
    Funzione di aggiornamento notturno via Web.
    Interroga il raw URL di GitHub, confronta il codice con la versione locale in esecuzione
    e, se rileva modifiche, applica l'aggiornamento e riavvia Samaritan.
    """
    logger.info(f"[OTA NOTTURNO] Controllo aggiornamenti su GitHub: {GITHUB_RAW_URL}")
    try:
        # Aggiunge un timestamp casuale in query per bypassare la cache di GitHub raw
        cache_buster_url = f"{GITHUB_RAW_URL}?_ts={int(time.time())}"
        response = requests.get(cache_buster_url, timeout=15)
        
        if response.status_code != 200:
            logger.warning(f"[OTA NOTTURNO] Risposta GitHub inattesa: HTTP {response.status_code}")
            return False

        remote_code = response.text
        if not remote_code or len(remote_code.strip()) < 50:
            logger.warning("[OTA NOTTURNO] Contenuto remoto vuoto o non conforme. Controllo ignorato.")
            return False

        local_file = Path(__file__).resolve()
        local_code = local_file.read_text(encoding="utf-8")

        # Confronto normalizzando i ritorni a capo (CRLF vs LF)
        clean_remote = remote_code.replace("\r\n", "\n").strip()
        clean_local = local_code.replace("\r\n", "\n").strip()

        if clean_remote != clean_local:
            logger.info("[OTA NOTTURNO] Rilevata nuova versione su GitHub (codice differente).")
            applica_aggiornamento_e_riavvia(remote_code, config, sorgente="GitHub Web OTA")
            return True
        else:
            logger.info("[OTA NOTTURNO] Codice locale già allineato all'ultima versione GitHub.")
            return False

    except requests.exceptions.RequestException as e:
        logger.warning(f"[OTA NOTTURNO] Errore di connessione a GitHub durante la verifica: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"[OTA NOTTURNO] Errore imprevisto durante il controllo notturno: {str(e)}")
        return False

# --------------------------------------------------------------------------------------
# 5. MODULO ORECCHIE: TELEGRAM LISTENER (POLLING NON BLOCCANTE IN BACKGROUND)
# --------------------------------------------------------------------------------------
def orecchie_telegram_worker(config: dict, stop_event: threading.Event):
    """
    Thread daemon in background dedicato all'ascolto (polling) di Telegram via getUpdates.
    Non blocca in alcun modo il ciclo principale di Samaritan.
    Se riceve un file 'samaritan_core.py' dall'ID autorizzato, lo scarica, lo sovrascrive e si riavvia.
    """
    bot_token = config.get("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    authorized_chat_id = str(config.get("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "")).strip()

    if not bot_token or not authorized_chat_id:
        logger.error("[ORECCHIE TELEGRAM] Impossibile avviare il listener: token o chat ID mancanti.")
        return

    logger.info(f"[ORECCHIE TELEGRAM] Listener avviato in background per l'utente autorizzato ID: {authorized_chat_id}")

    # Sincronizzazione iniziale dell'offset per scartare vecchi messaggi pregressi
    offset = None
    try:
        init_res = requests.get(f"https://api.telegram.org/bot{bot_token}/getUpdates", params={"offset": -1}, timeout=10)
        if init_res.status_code == 200:
            res_json = init_res.json()
            if res_json.get("ok") and res_json.get("result"):
                offset = res_json["result"][-1]["update_id"] + 1
                logger.info(f"[ORECCHIE TELEGRAM] Offset iniziale sincronizzato: {offset}")
    except Exception as e:
        logger.warning(f"[ORECCHIE TELEGRAM] Errore sincronizzazione offset iniziale: {e}")

    while not stop_event.is_set():
        try:
            params = {"timeout": 5, "allowed_updates": json.dumps(["message", "edited_message", "callback_query"])}
            if offset is not None:
                params["offset"] = offset

            response = requests.get(f"https://api.telegram.org/bot{bot_token}/getUpdates", params=params, timeout=12)
            
            if response.status_code != 200:
                time.sleep(4)
                continue

            data = response.json()
            if not data.get("ok"):
                time.sleep(4)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1

                # Click su pulsante inline: gestito in un try dedicato per non perdere il resto del batch
                callback_query = update.get("callback_query")
                if callback_query:
                    try:
                        gestisci_callback_query(callback_query, config, authorized_chat_id)
                    except Exception as e:
                        logger.error(f"[ORECCHIE TELEGRAM] Errore gestione callback_query: {str(e)}")
                    continue

                # Estrazione messaggio (supporta sia messaggi standard che modificati)
                msg = update.get("message") or update.get("edited_message")
                if not msg:
                    continue

                sender_id = str(msg.get("from", {}).get("id", "")).strip()
                chat_id = str(msg.get("chat", {}).get("id", "")).strip()

                # Verifica di sicurezza: processa solo se proviene dall'ID autorizzato
                if sender_id != authorized_chat_id and chat_id != authorized_chat_id:
                    logger.warning(f"[ORECCHIE TELEGRAM] Ricevuta interazione non autorizzata da ID '{sender_id or chat_id}'. Ignorata.")
                    continue

                # Controllo 1: Presenza documento allegato
                doc = msg.get("document")
                if doc:
                    file_name = doc.get("file_name", "")
                    logger.info(f"[ORECCHIE TELEGRAM] Ricevuto documento allegato: '{file_name}' da utente autorizzato.")

                    if file_name == "samaritan_core.py":
                        file_id = doc.get("file_id")
                        logger.info(f"[ORECCHIE TELEGRAM] Aggiornamento OTA immediato rilevato. Recupero file_id: {file_id}")

                        # 1. Recupera il file_path da Telegram
                        info_res = requests.get(f"https://api.telegram.org/bot{bot_token}/getFile", params={"file_id": file_id}, timeout=10)
                        if info_res.status_code == 200 and info_res.json().get("ok"):
                            file_path = info_res.json()["result"]["file_path"]
                            download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
                            
                            # 2. Scarica il codice sorgente
                            dl_res = requests.get(download_url, timeout=20)
                            if dl_res.status_code == 200 and len(dl_res.text.strip()) > 0:
                                nuovo_codice = dl_res.text
                                
                                # 3. Conferma ricezione aggiornamento ed esegue riavvio
                                applica_aggiornamento_e_riavvia(nuovo_codice, config, sorgente="Telegram Document OTA")
                                return
                            else:
                                logger.error(f"[ORECCHIE TELEGRAM] Download del file fallito (HTTP {dl_res.status_code}).")
                                bocca_telegram("ERRORE: Impossibile scaricare il file allegato da Telegram.", config=config)
                        else:
                            logger.error("[ORECCHIE TELEGRAM] Chiamata getFile fallita per il documento.")
                            bocca_telegram("ERRORE: Impossibile recuperare i metadati del file da Telegram.", config=config)
                    else:
                        logger.info(f"[ORECCHIE TELEGRAM] Documento '{file_name}' non è 'samaritan_core.py'. Ignorato.")

                # Controllo 2: Messaggi testuali (logging informativo)
                elif msg.get("text"):
                    testo = msg.get("text").strip()
                    logger.info(f"[ORECCHIE TELEGRAM] Ricevuto messaggio testuale: \"{testo}\"")
                    comando = testo.lower().split("@")[0]
                    if comando in ["/menu", "/start"]:
                        invia_menu_telegram(config, chat_id=chat_id)
                    elif comando in ["/aggiorna", "aggiorna", "/update"]:
                        bocca_telegram("Comando manuale ricevuto: avvio verifica aggiornamenti notturni su GitHub...", config=config)
                        cerca_aggiornamenti(config)

        except requests.exceptions.RequestException:
            # Sbalzo di rete: attesa prima del nuovo polling
            time.sleep(3)
        except Exception as e:
            logger.error(f"[ORECCHIE TELEGRAM] Errore imprevisto nel listener: {str(e)}")
            time.sleep(3)

# --------------------------------------------------------------------------------------
# 6. MODULO OCCHI: SHOPIFY
# --------------------------------------------------------------------------------------
def occhi_shopify(config: dict, limit: int = 3) -> list:
    """
    Interroga le Admin REST API di Shopify per estrarre e mostrare gli ultimi ordini ricevuti.
    Utilizza l'access token permanente memorizzato nel file .env.
    """
    shop = config.get("SHOPIFY_SHOP") or os.getenv("SHOPIFY_SHOP")
    token = config.get("SHOPIFY_ACCESS_TOKEN") or os.getenv("SHOPIFY_ACCESS_TOKEN")
    api_version = config.get("SHOPIFY_API_VERSION") or os.getenv("SHOPIFY_API_VERSION", "2024-07")

    if not shop or not token:
        logger.error("Impossibile eseguire 'occhi_shopify': SHOPIFY_SHOP o SHOPIFY_ACCESS_TOKEN mancanti nel file .env.")
        return []

    endpoint_url = f"https://{shop}/admin/api/{api_version}/orders.json?status=any&limit={limit}"
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Samaritan-AgenticCore/1.0"
    }

    try:
        response = requests.get(endpoint_url, headers=headers, timeout=15)
        if response.status_code == 200:
            data = response.json()
            orders = data.get("orders", [])
            
            logger.info(f"--- [OCCHI SHOPIFY] Controllo eseguito su '{shop}' ---")
            if not orders:
                logger.info("Nessun ordine presente nello storico del negozio (0 ordini trovati).")
            else:
                logger.info(f"Trovati {len(orders)} ordini recenti (richiesti max {limit}):")
                for idx, o in enumerate(orders, 1):
                    order_num = o.get("name", "N/A")
                    created = o.get("created_at", "N/A")
                    total = f"{o.get('total_price', '0.00')} {o.get('currency', 'EUR')}"
                    financial = o.get("financial_status", "sconosciuto")
                    fulfillment = o.get("fulfillment_status") or "unfulfilled"
                    
                    customer_info = "Ospite"
                    if o.get("customer"):
                        cust = o["customer"]
                        first = cust.get("first_name", "")
                        last = cust.get("last_name", "")
                        customer_info = f"{first} {last}".strip() or cust.get("email", "Cliente")

                    items_summary = []
                    for item in o.get("line_items", []):
                        items_summary.append(f"{item.get('quantity')}x {item.get('name')}")
                    items_str = ", ".join(items_summary) if items_summary else "Nessun articolo"

                    print(f"   [{idx}] Ordine: {order_num} | Data: {created} | Cliente: {customer_info}")
                    print(f"       Totale: {total} | Pagamento: {financial} | Spedizione: {fulfillment}")
                    print(f"       Articoli: {items_str}")
            
            return orders
        else:
            logger.error(f"Risposta Shopify inattesa: HTTP {response.status_code} - {response.text}")
            return []

    except requests.exceptions.RequestException as e:
        logger.error(f"Errore di rete verso Shopify API: {str(e)}")
        return []
    except Exception as e:
        logger.error(f"Errore imprevisto durante l'ispezione Shopify: {str(e)}")
        return []

# --------------------------------------------------------------------------------------
# 7. MODULO OCCHI: EBAY (Predisposizione architetturale futura)
# --------------------------------------------------------------------------------------
def occhi_ebay(config: dict):
    """Placeholder architetturale: modulo sensore ordini e notifiche eBay."""
    pass

# --------------------------------------------------------------------------------------
# 8. CORE LOOP (BATTITO CARDIACO)
# --------------------------------------------------------------------------------------
def main():
    logger.info("==================================================================")
    logger.info("  PROJECT SAMARITAN - Core Autonomo Avviato [Lead Architect Mode]")
    logger.info("==================================================================")
    
    # Caricamento credenziali
    env_file = Path(__file__).parent / ".env"
    config = load_env(env_file)
    
    if not config.get("SHOPIFY_ACCESS_TOKEN"):
        logger.error(f"Credenziali Shopify non trovate in {env_file}.")
        sys.exit(1)

    logger.info(f"Target Store: {config.get('SHOPIFY_SHOP', 'Non specificato')}")
    logger.info(f"Telegram Chat ID: {config.get('TELEGRAM_CHAT_ID', 'Non specificato')}")
    logger.info(f"File di log persistente: {log_file_path}")

    # Intervallo predefinito: 10 minuti (600 secondi)
    interval_seconds = int(os.environ.get("SAMARITAN_HEARTBEAT_INTERVAL", 600))
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        interval_seconds = int(sys.argv[1])

    logger.info(f"Battito cardiaco impostato: ogni {interval_seconds} secondi ({interval_seconds / 60:.1f} minuti).")

    # Invio messaggio di boot tramite Bocca Telegram
    logger.info("Trasmissione messaggio di avvio operativo su Telegram...")
    messaggio_avvio = "SISTEMA OPERATIVO. Samaritan avviato. Inizio monitoraggio Shopify."
    bocca_telegram(messaggio_avvio, config=config)

    # Avvio del thread in background "Orecchie Telegram" (polling non bloccante)
    stop_event = threading.Event()
    orecchie_thread = threading.Thread(
        target=orecchie_telegram_worker,
        args=(config, stop_event),
        name="SamaritanOrecchieTelegram",
        daemon=True
    )
    orecchie_thread.start()

    logger.info("Avvio del ciclo operativo continuo. Premi CTRL+C per arrestare Samaritan.")
    print("-" * 66)

    cycle_count = 0
    last_nightly_check_date = None

    while True:
        try:
            cycle_count += 1
            now = datetime.now()
            now_str = now.strftime(DATE_FORMAT)
            today_str = now.strftime("%Y-%m-%d")

            logger.info(f"[PULSE #{cycle_count}] Battito cardiaco Samaritan alle ore {now_str}")

            # 1. Esecuzione modulo occhi Shopify
            occhi_shopify(config, limit=3)

            # 2. Controllo notturno programmato (ore 03:00, una volta al giorno)
            if now.hour == 3 and last_nightly_check_date != today_str:
                logger.info(f"[TIMER NOTTURNO] Finestra oraria 03:00 attiva ({now.strftime('%H:%M:%S')}). Esecuzione cerca_aggiornamenti()...")
                last_nightly_check_date = today_str
                cerca_aggiornamenti(config)

            # Futuro: occhi_ebay(config)

            logger.info(f"[STANDBY] In attesa del prossimo battito tra {interval_seconds} secondi...")
            print("-" * 66)
            
            # Sleep a micro-step per interruzione rapida in caso di arresto o segnale
            sleep_chunk = 1.0
            elapsed = 0.0
            while elapsed < interval_seconds and not stop_event.is_set():
                time.sleep(sleep_chunk)
                elapsed += sleep_chunk

        except KeyboardInterrupt:
            logger.warning("\n[STOP] Rilevata interruzione da tastiera (CTRL+C). Arresto sicuro di Samaritan in corso...")
            stop_event.set()
            break
        except Exception as e:
            logger.exception(f"[CRITICAL ERROR] Errore imprevisto nel loop principale: {str(e)}")
            logger.info("Ripristino in corso: attesa di 60 secondi prima del nuovo tentativo...")
            time.sleep(60)

    logger.info("Samaritan Core disattivato. Sessione terminata.")

if __name__ == "__main__":
    main()
