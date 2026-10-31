"""
========================================================================================
BOT PULICHIMICA - Autonomous E-Commerce Operating System
Lead Architect Core Module
========================================================================================
Modulo: samaritan_core.py
Descrizione: Demone autonomo non bloccante per la gestione e-commerce Pulichimica:
             - Canale "Orecchie" (24/7): ascolto continuo Telegram con due sottomenu
               separati:
                 * /menu  -> Menu Operativo (Ricerca Prodotto, Ultimi 3 Ordini,
                             Monitoraggio Packlink, Chiudi)
                 * /admin -> Menu Sviluppatore (Forza Aggiornamento GitHub, Health Check API)
             - Modulo Ordini (Heartbeat Orario 07:00-20:00): verifica nuovi ordini ogni
               3600s tramite delta temporale non bloccante, persistenza su
               'last_order_id.txt' e recupero automatico in blocco alle 07:00
               delle vendite notturne (20:01-06:59).
             - Modulo Packlink PRO: controllo anomalie spedizioni alle ore 07, 13 e 16
               e su richiesta dal menu operativo.
             - Graceful Shutdown: invio sincrono notifica di spegnimento su SIGINT/SIGTERM.
========================================================================================
"""

import os
import sys
import time
import json
import ctypes
import signal
import logging
import threading
from pathlib import Path
from datetime import datetime

import requests
from dotenv import load_dotenv

# --------------------------------------------------------------------------------------
# 1. CONFIGURAZIONE CONSOLE UTF-8 E LOGGING
# --------------------------------------------------------------------------------------
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [BOT PULICHIMICA] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE_PATH = BASE_DIR / "samaritan.log"
LAST_ORDER_ID_FILE = BASE_DIR / "last_order_id.txt"

logger = logging.getLogger("BotPulichimica")
logger.setLevel(logging.INFO)

if not logger.handlers:
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    logger.addHandler(file_handler)

# Costanti operative
SHOPIFY_REST_VERSION = "2024-01"
ORDER_CHECK_INTERVAL = 3600          # 1 ora (3600 secondi) durante la fascia attiva
ORDER_ERROR_RETRY_DELAY = 300        # Retry dopo 5 minuti in caso di errore di rete temporaneo
MAIN_LOOP_TICK_SECONDS = 1.0         # Tick non bloccante del loop principale
GITHUB_RAW_URL = "https://raw.githubusercontent.com/fabio966/Samaritan/main/samaritan_core.py"
PACKLINK_SHIPMENTS_URL = "https://api.packlink.com/v1/shipments"
PACKLINK_CHECK_HOURS = (7, 13, 16)
STATI_ANOMALIA_PACKLINK = {
    "INCIDENT",
    "PARCEL_INCIDENT",
    "EXCEPTION",
    "RETURNED_TO_SENDER",
    "DELIVERY_FAILED",
}

RESTART_LOCK = threading.Lock()
RESTART_IN_PROGRESS = False
SHUTDOWN_LOCK = threading.Lock()
SHUTDOWN_NOTIFIED = False

# Gestore di stato conversazionale per le chat Telegram (es. 'attesa_ricerca')
stato_utente = {}


# --------------------------------------------------------------------------------------
# 2. CARICAMENTO VARIABILI D'AMBIENTE (.env)
# --------------------------------------------------------------------------------------
def load_env(env_path: Path = None) -> dict:
    """Carica le credenziali dal file .env supportando Shopify, Telegram e Packlink PRO."""
    if env_path is None:
        env_path = BASE_DIR / ".env"

    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=True)
    else:
        logger.warning(f"File .env non trovato in: {env_path}")

    shop_domain = (os.getenv("SHOPIFY_SHOP") or os.getenv("SHOPIFY_STORE_URL") or "").strip()
    return {
        "SHOPIFY_SHOP": shop_domain,
        "SHOPIFY_STORE_URL": (os.getenv("SHOPIFY_STORE_URL") or shop_domain).strip(),
        "SHOPIFY_CLIENT_ID": os.getenv("SHOPIFY_CLIENT_ID", "").strip(),
        "SHOPIFY_CLIENT_SECRET": os.getenv("SHOPIFY_CLIENT_SECRET", "").strip(),
        "SHOPIFY_ACCESS_TOKEN": os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip(),
        "SHOPIFY_API_VERSION": os.getenv("SHOPIFY_API_VERSION", SHOPIFY_REST_VERSION).strip(),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        "PACKLINK_API_KEY": os.getenv("PACKLINK_API_KEY", "").strip(),
    }


def _normalizza_shop_domain(config: dict = None) -> str:
    """Restituisce il dominio myshopify pulito leggendo SHOPIFY_SHOP o SHOPIFY_STORE_URL."""
    raw_shop = (
        (config.get("SHOPIFY_SHOP") if config else None)
        or (config.get("SHOPIFY_STORE_URL") if config else None)
        or os.getenv("SHOPIFY_SHOP")
        or os.getenv("SHOPIFY_STORE_URL")
        or ""
    ).strip()
    return raw_shop.replace("https://", "").replace("http://", "").rstrip("/")


# --------------------------------------------------------------------------------------
# 3. MODULO BOCCA: COMUNICAZIONE E SOTTOMENU TELEGRAM (/menu e /admin)
# --------------------------------------------------------------------------------------
def bocca_telegram(messaggio: str, config: dict = None) -> bool:
    """
    Invia un messaggio testuale alla chat Telegram configurata.
    Intercetta qualsiasi eccezione HTTP/rete per non bloccare mai il demone.
    """
    bot_token = (config.get("TELEGRAM_BOT_TOKEN") if config else None) or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = (config.get("TELEGRAM_CHAT_ID") if config else None) or os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logger.error("[BOCCA TELEGRAM] TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID mancanti nel .env.")
        return False

    endpoint_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": messaggio,
    }

    try:
        response = requests.post(endpoint_url, data=payload, timeout=12)
        if response.status_code == 200:
            prima_riga = messaggio.splitlines()[0] if messaggio else ""
            logger.info(f"[BOCCA TELEGRAM] Messaggio inviato: \"{prima_riga}\"")
            return True
        logger.error(f"[BOCCA TELEGRAM] Errore API Telegram (HTTP {response.status_code}): {response.text}")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"[BOCCA TELEGRAM] Errore di rete verso Telegram: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"[BOCCA TELEGRAM] Errore imprevisto nell'invio: {str(e)}")
        return False


def _telegram_api(metodo: str, payload: dict, config: dict = None, timeout: int = 12) -> bool:
    """Chiamata POST generica alle Telegram Bot API con gestione completa degli errori."""
    bot_token = (config.get("TELEGRAM_BOT_TOKEN") if config else None) or os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.error(f"[TELEGRAM API] {metodo}: TELEGRAM_BOT_TOKEN mancante.")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/{metodo}",
            json=payload,
            timeout=timeout,
        )
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


# Sottomenu 1: Menu Operativo (/menu)
MENU_OPERATIVO_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "📦 Ricerca Prodotto", "callback_data": "cmd_cerca_prodotto"},
            {"text": "📋 Ultimi 3 Ordini", "callback_data": "cmd_ultimi_ordini"},
        ],
        [
            {"text": "🚚 Monitoraggio Packlink", "callback_data": "cmd_packlink"},
        ],
        [
            {"text": "🔙 Chiudi", "callback_data": "cmd_chiudi"},
        ],
    ]
}

# Sottomenu 2: Menu Sviluppatore (/admin)
MENU_ADMIN_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "🔄 Forza Aggiornamento GitHub", "callback_data": "cmd_force_ota"},
        ],
        [
            {"text": "🩺 Health Check API", "callback_data": "cmd_health_check"},
        ],
    ]
}


def invia_menu_operativo(config: dict, chat_id: str = None) -> bool:
    """Invia il Menu Operativo (attivato dal comando /menu)."""
    chat_id = chat_id or config.get("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    payload = {
        "chat_id": chat_id,
        "text": "🧭 Menu Operativo Bot Pulichimica:",
        "reply_markup": MENU_OPERATIVO_KEYBOARD,
    }
    ok = _telegram_api("sendMessage", payload, config)
    if ok:
        logger.info("[UI TELEGRAM] Menu Operativo (/menu) inviato.")
    return ok


def invia_menu_admin(config: dict, chat_id: str = None) -> bool:
    """Invia il Menu Sviluppatore separato (attivato dal comando /admin)."""
    chat_id = chat_id or config.get("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    payload = {
        "chat_id": chat_id,
        "text": "🛠️ Menu Sviluppatore Bot Pulichimica:",
        "reply_markup": MENU_ADMIN_KEYBOARD,
    }
    ok = _telegram_api("sendMessage", payload, config)
    if ok:
        logger.info("[UI TELEGRAM] Menu Sviluppatore (/admin) inviato.")
    return ok


# --------------------------------------------------------------------------------------
# 4. AZIONI CALLBACK UI TELEGRAM (ULTIMI 3 ORDINI, HEALTH CHECK, OTA, RICERCA, CHIUDI)
# --------------------------------------------------------------------------------------
def _estrai_nome_cliente(order: dict) -> str:
    """Estrae nome e cognome del cliente dall'ordine Shopify con fallback sicuri."""
    cust = order.get("customer") or {}
    ship = order.get("shipping_address") or {}
    bill = order.get("billing_address") or {}

    nome = (cust.get("first_name") or ship.get("first_name") or bill.get("first_name") or "").strip()
    cognome = (cust.get("last_name") or ship.get("last_name") or bill.get("last_name") or "").strip()
    nome_completo = f"{nome} {cognome}".strip()

    if nome_completo:
        return nome_completo
    if ship.get("name"):
        return str(ship["name"]).strip()
    if bill.get("name"):
        return str(bill["name"]).strip()
    if cust.get("email"):
        return str(cust["email"]).strip()
    return "Cliente Ospite"


def recupera_ultimi_tre_ordini(config: dict) -> str:
    """
    Interroga Shopify (GET /admin/api/2024-01/orders.json?status=any&limit=3)
    e restituisce un riepilogo leggibile degli ultimi 3 ordini ricevuti.
    """
    shop = _normalizza_shop_domain(config)
    token = ((config.get("SHOPIFY_ACCESS_TOKEN") if config else None) or os.getenv("SHOPIFY_ACCESS_TOKEN") or "").strip()

    if not shop or not token:
        logger.error("[ULTIMI ORDINI] Credenziali Shopify mancanti nel .env.")
        return "⚠️ Errore di connessione a Shopify."

    endpoint_url = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/orders.json"
    params = {"status": "any", "limit": 3, "order": "created_at desc"}
    headers = {
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
        "User-Agent": "BotPulichimica-Core/1.0",
    }

    try:
        response = requests.get(endpoint_url, params=params, headers=headers, timeout=15)
        if response.status_code != 200:
            logger.error(f"[ULTIMI ORDINI] Risposta HTTP {response.status_code}: {response.text}")
            return "⚠️ Errore nel recupero degli ultimi ordini da Shopify."

        orders = response.json().get("orders", [])
        if not orders:
            return "📋 **Ultimi 3 Ordini:**\nNessun ordine presente nello storico del negozio."

        righe = ["📋 **Ultimi 3 Ordini Shopify:**"]
        for idx, order in enumerate(orders, 1):
            numero = order.get("order_number") or order.get("name", "N/A")
            cliente = _estrai_nome_cliente(order)
            totale = order.get("total_price", "0.00")
            stato_pagamento = order.get("financial_status", "n/d")
            created_raw = order.get("created_at", "")
            data_str = created_raw[:16].replace("T", " ") if created_raw else "n/d"

            articoli = [
                f"{item.get('quantity', 1)}x {item.get('name', 'Articolo')}"
                for item in (order.get("line_items") or [])
            ]
            articoli_str = ", ".join(articoli) if articoli else "Nessun articolo"

            righe.append(
                f"\n{idx}. Ordine #{numero} ({data_str})\n"
                f"   👤 Cliente: {cliente}\n"
                f"   💶 Totale: {totale} € [{stato_pagamento}]\n"
                f"   📦 Articoli: {articoli_str}"
            )

        return "\n".join(righe)

    except requests.exceptions.RequestException as e:
        logger.error(f"[ULTIMI ORDINI] Errore di rete verso Shopify: {str(e)}")
        return "⚠️ Errore di connessione a Shopify."
    except Exception as e:
        logger.error(f"[ULTIMI ORDINI] Errore imprevisto: {str(e)}")
        return "⚠️ Errore nel recupero degli ultimi ordini da Shopify."


def esegui_health_check_api(config: dict = None) -> str:
    """
    Esegue l'Health Check combinato su Shopify (shop.json) e Packlink PRO (shipments),
    catturando e mostrando eventuali errori specifici senza mai bloccare il main loop.
    """
    # 1. Test Shopify API
    shop = _normalizza_shop_domain(config)
    token = ((config.get("SHOPIFY_ACCESS_TOKEN") if config else None) or os.getenv("SHOPIFY_ACCESS_TOKEN") or "").strip()

    if not shop or not token:
        stato_shopify = "🔴 Shopify: DISCONNESSO\nDettaglio: Credenziali SHOPIFY_SHOP o SHOPIFY_ACCESS_TOKEN mancanti"
    else:
        shopify_url = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/shop.json"
        shopify_headers = {
            "X-Shopify-Access-Token": token,
            "Accept": "application/json",
            "User-Agent": "BotPulichimica-Core/1.0",
        }
        try:
            res_shopify = requests.get(shopify_url, headers=shopify_headers, timeout=12)
            if res_shopify.status_code == 200:
                stato_shopify = "🟢 Shopify: Connesso"
            else:
                body_err = (res_shopify.text or "").strip()[:300]
                errore = f"HTTP {res_shopify.status_code} - {body_err}" if body_err else f"HTTP {res_shopify.status_code}"
                logger.warning(f"[HEALTH CHECK] Shopify DISCONNESSO: {errore}")
                stato_shopify = f"🔴 Shopify: DISCONNESSO\nDettaglio: {errore}"
        except Exception as e:
            errore = str(e)
            logger.error(f"[HEALTH CHECK] Eccezione Shopify API: {errore}")
            stato_shopify = f"🔴 Shopify: DISCONNESSO\nDettaglio: {errore}"

    # 2. Test Packlink PRO API
    packlink_key = (
        os.getenv("PACKLINK_API_KEY")
        or (config.get("PACKLINK_API_KEY") if config else None)
        or ""
    ).strip()

    if not packlink_key:
        stato_packlink = "🔴 Packlink: DISCONNESSO\nDettaglio: PACKLINK_API_KEY mancante nel .env"
    else:
        packlink_headers = {
            "Authorization": packlink_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "BotPulichimica-Core/1.0",
        }
        try:
            res_packlink = requests.get(
                PACKLINK_SHIPMENTS_URL,
                params={"page": 1},
                headers=packlink_headers,
                timeout=12,
            )
            if res_packlink.status_code != 200:
                bearer_headers = dict(packlink_headers)
                bearer_headers["Authorization"] = f"Bearer {packlink_key}"
                res_bearer = requests.get(
                    PACKLINK_SHIPMENTS_URL,
                    params={"page": 1},
                    headers=bearer_headers,
                    timeout=12,
                )
                if res_bearer.status_code == 200:
                    res_packlink = res_bearer

            if res_packlink.status_code == 200:
                stato_packlink = "🟢 Packlink: Connesso"
            else:
                body_err = (res_packlink.text or "").strip()[:300]
                errore = f"HTTP {res_packlink.status_code} - {body_err}" if body_err else f"HTTP {res_packlink.status_code}"
                logger.warning(f"[HEALTH CHECK] Packlink DISCONNESSO: {errore}")
                stato_packlink = f"🔴 Packlink: DISCONNESSO\nDettaglio: {errore}"
        except Exception as e:
            errore = str(e)
            logger.error(f"[HEALTH CHECK] Eccezione Packlink API: {errore}")
            stato_packlink = f"🔴 Packlink: DISCONNESSO\nDettaglio: {errore}"

    # TODO [FUTURO - MODULO EBAY]: Inserire qui il blocco Health Check per eBay API

    return (
        "🛠 **Report Health Check API**\n\n"
        f"{stato_shopify}\n"
        f"{stato_packlink}"
    )


def cerca_prodotti_shopify(query_ricerca: str, config: dict, max_results: int = 30) -> str:
    """
    Interroga Shopify GET /admin/api/2024-01/products.json?fields=title,variants
    (seguendo l'eventuale paginazione cursore per coprire l'intero catalogo),
    filtra in modo case-insensitive sul title del prodotto e restituisce nome e quantità.
    """
    query_pulita = (query_ricerca or "").strip()
    if not query_pulita:
        return "Nessun prodotto trovato per ''."

    shop = _normalizza_shop_domain(config)
    token = ((config.get("SHOPIFY_ACCESS_TOKEN") if config else None) or os.getenv("SHOPIFY_ACCESS_TOKEN") or "").strip()

    if not shop or not token:
        logger.error("[RICERCA PRODOTTO] Credenziali Shopify mancanti nel .env.")
        return "⚠️ Errore di connessione a Shopify."

    next_url = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/products.json?fields=title,variants&limit=250"
    headers = {
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
        "User-Agent": "BotPulichimica-Core/1.0",
    }

    query_lower = query_pulita.lower()
    risultati = []
    pagine_lette = 0

    try:
        while next_url and pagine_lette < 10:
            response = requests.get(next_url, headers=headers, timeout=15)
            if response.status_code != 200:
                logger.error(f"[RICERCA PRODOTTO] Risposta HTTP {response.status_code}: {response.text}")
                return "⚠️ Errore di connessione a Shopify."

            products = response.json().get("products", [])
            pagine_lette += 1

            for product in products:
                p_title = (product.get("title") or "").strip()
                if query_lower not in p_title.lower():
                    continue

                variants = product.get("variants") or []
                multi_variant = len(variants) > 1

                for variant in variants:
                    v_title = (variant.get("title") or "").strip()
                    qty = variant.get("inventory_quantity")
                    qty_int = int(qty) if qty is not None else 0

                    if multi_variant and v_title and v_title.lower() != "default title":
                        nome_voce = f"{p_title} ({v_title})"
                    else:
                        nome_voce = p_title

                    risultati.append(f"- {nome_voce}: {qty_int} pz")

            next_url = response.links.get("next", {}).get("url")

        if not risultati:
            return f"Nessun prodotto trovato per '{query_pulita}'."

        righe = [f"🔍 Risultati per '{query_pulita}':"]
        righe.extend(risultati[:max_results])
        if len(risultati) > max_results:
            righe.append(f"...e altri {len(risultati) - max_results} risultati (affina la ricerca).")

        return "\n".join(righe)

    except requests.exceptions.RequestException as e:
        logger.error(f"[RICERCA PRODOTTO] Errore di rete verso Shopify: {str(e)}")
        return "⚠️ Errore di connessione a Shopify."
    except Exception as e:
        logger.error(f"[RICERCA PRODOTTO] Errore imprevisto durante la ricerca: {str(e)}")
        return "⚠️ Errore durante la ricerca del prodotto."


def gestisci_callback_query(callback_query: dict, config: dict, authorized_chat_id: str):
    """
    Intercetta e gestisce esclusivamente le nuove callback_query dei sottomenu /menu e /admin:
      - cmd_ultimi_ordini  -> Recupera e stampa gli ultimi 3 ordini Shopify
      - cmd_health_check   -> Verifica /admin/api/2024-01/shop.json
      - cmd_force_ota      -> Forza cerca_aggiornamenti() su GitHub
      - cmd_cerca_prodotto -> Imposta stato 'attesa_ricerca' e chiede il prodotto da cercare
      - cmd_chiudi         -> Elimina il messaggio della pulsantiera via deleteMessage
    """
    callback_id = callback_query.get("id")
    data = callback_query.get("data", "")
    sender_id = str(callback_query.get("from", {}).get("id", "")).strip()
    message_obj = callback_query.get("message") or {}
    chat_id = str(message_obj.get("chat", {}).get("id", sender_id)).strip()
    message_id = message_obj.get("message_id")

    autorizzato = sender_id == authorized_chat_id
    _telegram_api("answerCallbackQuery", {"callback_query_id": callback_id}, config, timeout=8)

    if not autorizzato:
        logger.warning(f"[UI TELEGRAM] Callback '{data}' da ID non autorizzato '{sender_id}'. Ignorata.")
        return

    logger.info(f"[UI TELEGRAM] Azione pulsante ricevuta: '{data}'")

    if data == "cmd_ultimi_ordini":
        report_ordini = recupera_ultimi_tre_ordini(config)
        _telegram_api("sendMessage", {"chat_id": chat_id, "text": report_ordini}, config)

    elif data == "cmd_health_check":
        esito_health = esegui_health_check_api(config)
        _telegram_api("sendMessage", {"chat_id": chat_id, "text": esito_health}, config)

    elif data == "cmd_force_ota":
        _telegram_api(
            "sendMessage",
            {"chat_id": chat_id, "text": "🔄 Controllo forzato aggiornamenti su GitHub in corso..."},
            config,
        )
        aggiornato = cerca_aggiornamenti(config)
        if not aggiornato:
            _telegram_api(
                "sendMessage",
                {"chat_id": chat_id, "text": "✅ Nessun nuovo aggiornamento disponibile su GitHub (codice già allineato o remoto non raggiungibile)."},
                config,
            )

    elif data == "cmd_cerca_prodotto":
        stato_utente[chat_id] = "attesa_ricerca"
        logger.info(f"[STATO CHAT] stato_utente['{chat_id}'] impostato su 'attesa_ricerca'.")
        _telegram_api(
            "sendMessage",
            {"chat_id": chat_id, "text": "Scrivi il nome del prodotto (o una parte di esso) da cercare:"},
            config,
        )

    elif data == "cmd_packlink":
        stato_utente[chat_id] = None
        avvia_controllo_packlink_manuale(config=config, chat_id=chat_id)

    elif data == "cmd_chiudi":
        stato_utente[chat_id] = None
        if message_id is not None:
            _telegram_api(
                "deleteMessage",
                {"chat_id": chat_id, "message_id": message_id},
                config,
            )
            logger.info(f"[UI TELEGRAM] Menu chiuso (deleteMessage message_id={message_id}).")

    else:
        logger.warning(f"[UI TELEGRAM] callback_data non riconosciuto: '{data}'")
        _telegram_api(
            "sendMessage",
            {"chat_id": chat_id, "text": "⚠️ Pulsante obsoleto o non riconosciuto. Usa /menu o /admin."},
            config,
        )


# --------------------------------------------------------------------------------------
# 5. MONITORAGGIO AUTOMATICO NUOVI ORDINI (PERSISTENZA + FASCIA 07:00-20:00)
# --------------------------------------------------------------------------------------
def in_fascia_attiva_ordini(now: datetime = None) -> bool:
    """
    Verifica se l'orario corrente rientra nella finestra attiva 07:00 - 20:00.
    Tra le 20:01 e le 06:59 restituisce False (nessuna chiamata API per gli ordini).
    """
    if now is None:
        now = datetime.now()
    if now.hour < 7 or now.hour > 20:
        return False
    if now.hour == 20 and now.minute >= 1:
        return False
    return True


def salva_last_order_id(order_id, file_path: Path = LAST_ORDER_ID_FILE) -> bool:
    """Salva immediatamente il last_order_id su disco in modo persistente."""
    try:
        file_path.write_text(str(order_id).strip(), encoding="utf-8")
        return True
    except Exception as e:
        logger.error(f"[ORDINI SHOPIFY] Impossibile aggiornare {file_path.name}: {str(e)}")
        return False


def leggi_o_inizializza_last_order_id(config: dict, file_path: Path = LAST_ORDER_ID_FILE) -> str:
    """
    Legge l'ultimo ID ordine notificato da 'last_order_id.txt'.
    Se il file non esiste ancora, allinea l'ID all'ultimo ordine attualmente presente
    su Shopify (oppure '0' se il negozio non ha ancora ordini) e lo salva su disco.
    """
    if file_path.exists():
        try:
            contenuto = file_path.read_text(encoding="utf-8").strip()
            if contenuto.isdigit():
                return contenuto
        except Exception as e:
            logger.error(f"[ORDINI SHOPIFY] Errore lettura {file_path.name}: {str(e)}")

    shop = _normalizza_shop_domain(config)
    token = ((config.get("SHOPIFY_ACCESS_TOKEN") if config else None) or os.getenv("SHOPIFY_ACCESS_TOKEN") or "").strip()
    initial_id = "0"

    if shop and token:
        try:
            url = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/orders.json"
            headers = {"X-Shopify-Access-Token": token, "Accept": "application/json"}
            res = requests.get(url, params={"status": "any", "limit": 1}, headers=headers, timeout=15)
            if res.status_code == 200:
                orders = res.json().get("orders", [])
                if orders and orders[0].get("id"):
                    initial_id = str(orders[0]["id"])
        except Exception as e:
            logger.warning(f"[ORDINI SHOPIFY] Impossibile pre-allineare last_order_id da remoto, uso '0': {str(e)}")

    salva_last_order_id(initial_id, file_path=file_path)
    logger.info(f"[ORDINI SHOPIFY] File {file_path.name} inizializzato con last_order_id={initial_id}")
    return initial_id


def controlla_nuovi_ordini_shopify(config: dict) -> bool:
    """
    Interroga GET /admin/api/2024-01/orders.json?status=any&since_id={last_order_id}.
    Invia una notifica Telegram per ogni nuovo ordine (inclusi tutti gli ordini
    accumulati nella fascia notturna 20:01-06:59 al primo controllo delle 07:00)
    e aggiorna immediatamente 'last_order_id.txt'.
    """
    shop = _normalizza_shop_domain(config)
    token = ((config.get("SHOPIFY_ACCESS_TOKEN") if config else None) or os.getenv("SHOPIFY_ACCESS_TOKEN") or "").strip()

    if not shop or not token:
        logger.error("[ORDINI SHOPIFY] SHOPIFY_SHOP/SHOPIFY_STORE_URL o SHOPIFY_ACCESS_TOKEN mancanti.")
        return False

    last_order_id = leggi_o_inizializza_last_order_id(config)
    endpoint_url = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/orders.json"
    params = {
        "status": "any",
        "since_id": last_order_id,
        "limit": 250,
    }
    headers = {
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
        "User-Agent": "BotPulichimica-Core/1.0",
    }

    try:
        response = requests.get(endpoint_url, params=params, headers=headers, timeout=15)
        if response.status_code != 200:
            logger.error(f"[ORDINI SHOPIFY] Risposta inattesa HTTP {response.status_code}: {response.text}")
            return False

        orders = response.json().get("orders", [])
        logger.info(
            f"[ORDINI SHOPIFY] Check completato su '{shop}' (since_id={last_order_id}): "
            f"{len(orders)} nuovi ordini trovati."
        )

        if not orders:
            return True

        orders.sort(key=lambda o: int(o.get("id", 0)))

        for order in orders:
            order_id = order.get("id")
            if not order_id:
                continue

            order_number = order.get("order_number") or order.get("name", "N/A")
            cliente_str = _estrai_nome_cliente(order)
            total_price = order.get("total_price", "0.00")

            messaggio_ordine = (
                "🚨 **NUOVO ORDINE SHOPIFY!**\n"
                f"Numero: {order_number}\n"
                f"Cliente: {cliente_str}\n"
                f"Totale: {total_price} €"
            )

            if bocca_telegram(messaggio_ordine, config=config):
                salva_last_order_id(order_id)
                logger.info(f"[ORDINI SHOPIFY] Ordine #{order_number} (ID {order_id}) notificato e salvato.")
            else:
                logger.warning(
                    f"[ORDINI SHOPIFY] Invio Telegram fallito per ordine #{order_number} (ID {order_id}). "
                    "Verrà ritentato al prossimo ciclo."
                )
                break

        return True

    except requests.exceptions.RequestException as e:
        logger.error(f"[ORDINI SHOPIFY] Errore di rete verso Shopify: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"[ORDINI SHOPIFY] Errore imprevisto nel controllo ordini: {str(e)}")
        return False


# --------------------------------------------------------------------------------------
# 6. MODULO RIAVVIO & OTA (OVER-THE-AIR)
# --------------------------------------------------------------------------------------
def applica_aggiornamento_e_riavvia(nuovo_codice: str, config: dict, sorgente: str):
    """
    Valida la sintassi del nuovo codice, salva un backup, sovrascrive samaritan_core.py,
    notifica su Telegram e riavvia il processo Python in modo pulito (os.execv).
    """
    global RESTART_IN_PROGRESS
    with RESTART_LOCK:
        if RESTART_IN_PROGRESS:
            return
        RESTART_IN_PROGRESS = True

    logger.info(f"[OTA RESTART] Validazione aggiornamento ricevuto da: {sorgente}")

    try:
        compile(nuovo_codice, "samaritan_core.py", "exec")
    except Exception as e:
        logger.error(f"[OTA RESTART] Codice non valido ({e}). Aggiornamento annullato.")
        bocca_telegram(
            f"ERRORE OTA: Il codice ricevuto da {sorgente} contiene errori di sintassi. Aggiornamento annullato.",
            config=config,
        )
        RESTART_IN_PROGRESS = False
        return

    target_file = Path(__file__).resolve()
    backup_file = target_file.with_suffix(".py.bak")

    try:
        backup_file.write_text(target_file.read_text(encoding="utf-8"), encoding="utf-8")
        target_file.write_text(nuovo_codice, encoding="utf-8")
        logger.info(f"[OTA RESTART] {target_file.name} aggiornato (backup in {backup_file.name}).")
    except Exception as e:
        logger.error(f"[OTA RESTART] Errore scrittura file: {str(e)}")
        bocca_telegram(f"ERRORE OTA: Impossibile sovrascrivere il file locale: {str(e)}", config=config)
        RESTART_IN_PROGRESS = False
        return

    bocca_telegram("Aggiornamento ricevuto. Installazione e riavvio in corso...", config=config)
    time.sleep(1.5)

    logger.info("[OTA RESTART] Riavvio autonomo del processo (os.execv)...")
    sys.stdout.flush()
    sys.stderr.flush()

    for h in list(logger.handlers):
        h.flush()
        h.close()

    python_bin = sys.executable
    args = [python_bin, str(target_file)] + sys.argv[1:]
    os.execv(python_bin, args)


def cerca_aggiornamenti(config: dict) -> bool:
    """Controlla il repository GitHub Raw, scarica eventuale nuovo codice e riavvia."""
    logger.info(f"[OTA GITHUB] Controllo aggiornamenti su GitHub: {GITHUB_RAW_URL}")
    try:
        cache_buster_url = f"{GITHUB_RAW_URL}?_ts={int(time.time())}"
        response = requests.get(cache_buster_url, timeout=15)
        if response.status_code != 200:
            logger.warning(f"[OTA GITHUB] Risposta GitHub HTTP {response.status_code}")
            return False

        remote_code = response.text
        if not remote_code or len(remote_code.strip()) < 50:
            return False

        local_code = Path(__file__).resolve().read_text(encoding="utf-8")
        if remote_code.replace("\r\n", "\n").strip() != local_code.replace("\r\n", "\n").strip():
            logger.info("[OTA GITHUB] Nuova versione rilevata su GitHub.")
            applica_aggiornamento_e_riavvia(remote_code, config, sorgente="GitHub Web OTA")
            return True

        logger.info("[OTA GITHUB] Codice locale già allineato a GitHub.")
        return False
    except Exception as e:
        logger.warning(f"[OTA GITHUB] Impossibile completare verifica GitHub: {str(e)}")
        return False


# --------------------------------------------------------------------------------------
# 7. MODULO ORECCHIE: LISTENER TELEGRAM 24/7 (THREAD DAEMON NON BLOCCANTE)
# --------------------------------------------------------------------------------------
def orecchie_telegram_worker(config: dict, stop_event: threading.Event):
    """
    Thread daemon attivo 24 ore su 24 per gestire:
      - /menu  (e /start) -> Menu Operativo
      - /admin            -> Menu Sviluppatore
      - callback_query    -> Azioni dei nuovi pulsanti inline
      - documenti .py     -> Aggiornamento OTA immediato via Telegram
    """
    bot_token = config.get("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    authorized_chat_id = str(config.get("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "")).strip()

    if not bot_token or not authorized_chat_id:
        logger.error("[ORECCHIE TELEGRAM] Token o Chat ID mancanti: listener non avviabile.")
        return

    logger.info(f"[ORECCHIE TELEGRAM] Listener attivo 24/7 per Chat ID autorizzato: {authorized_chat_id}")

    offset = None
    try:
        init_res = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getUpdates",
            params={"offset": -1},
            timeout=10,
        )
        if init_res.status_code == 200:
            res_json = init_res.json()
            if res_json.get("ok") and res_json.get("result"):
                offset = res_json["result"][-1]["update_id"] + 1
                logger.info(f"[ORECCHIE TELEGRAM] Offset sincronizzato: {offset}")
    except Exception as e:
        logger.warning(f"[ORECCHIE TELEGRAM] Sincronizzazione offset iniziale fallita: {e}")

    while not stop_event.is_set():
        try:
            params = {
                "timeout": 5,
                "allowed_updates": json.dumps(["message", "edited_message", "callback_query"]),
            }
            if offset is not None:
                params["offset"] = offset

            response = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getUpdates",
                params=params,
                timeout=12,
            )
            if response.status_code != 200:
                time.sleep(3)
                continue

            data = response.json()
            if not data.get("ok"):
                time.sleep(3)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1

                # 1. Gestione click pulsanti Inline Keyboard
                callback_query = update.get("callback_query")
                if callback_query:
                    try:
                        gestisci_callback_query(callback_query, config, authorized_chat_id)
                    except Exception as e:
                        logger.error(f"[ORECCHIE TELEGRAM] Errore in gestisci_callback_query: {str(e)}")
                    continue

                # 2. Gestione comandi testuali e documenti OTA
                msg = update.get("message") or update.get("edited_message")
                if not msg:
                    continue

                sender_id = str(msg.get("from", {}).get("id", "")).strip()
                chat_id = str(msg.get("chat", {}).get("id", "")).strip()

                if sender_id != authorized_chat_id and chat_id != authorized_chat_id:
                    logger.warning(f"[ORECCHIE TELEGRAM] Interazione ignorata da ID non autorizzato '{sender_id or chat_id}'.")
                    continue

                doc = msg.get("document")
                if doc:
                    file_name = doc.get("file_name", "")
                    if file_name == "samaritan_core.py":
                        file_id = doc.get("file_id")
                        logger.info(f"[ORECCHIE TELEGRAM] Ricevuto update OTA via Telegram (file_id={file_id}).")
                        info_res = requests.get(
                            f"https://api.telegram.org/bot{bot_token}/getFile",
                            params={"file_id": file_id},
                            timeout=10,
                        )
                        if info_res.status_code == 200 and info_res.json().get("ok"):
                            file_path = info_res.json()["result"]["file_path"]
                            dl_res = requests.get(
                                f"https://api.telegram.org/file/bot{bot_token}/{file_path}",
                                timeout=20,
                            )
                            if dl_res.status_code == 200 and dl_res.text.strip():
                                applica_aggiornamento_e_riavvia(dl_res.text, config, sorgente="Telegram Document OTA")
                                return
                        bocca_telegram("ERRORE: Impossibile scaricare il file di aggiornamento da Telegram.", config=config)
                elif msg.get("text"):
                    testo = msg.get("text").strip()
                    logger.info(f"[ORECCHIE TELEGRAM] Messaggio ricevuto: \"{testo}\"")
                    comando = testo.lower().split("@")[0]

                    if comando in ["/menu", "/start"]:
                        stato_utente[chat_id] = None
                        invia_menu_operativo(config, chat_id=chat_id)
                    elif comando == "/admin":
                        stato_utente[chat_id] = None
                        invia_menu_admin(config, chat_id=chat_id)
                    elif testo == "🚚 Monitoraggio Packlink" or comando == "/packlink":
                        stato_utente[chat_id] = None
                        avvia_controllo_packlink_manuale(config=config, chat_id=chat_id)
                    elif stato_utente.get(chat_id) == "attesa_ricerca":
                        stato_utente[chat_id] = None
                        query_ricerca = testo
                        logger.info(f"[RICERCA PRODOTTO] Esecuzione ricerca per query: '{query_ricerca}'")
                        messaggio_risultati = cerca_prodotti_shopify(query_ricerca, config)
                        _telegram_api("sendMessage", {"chat_id": chat_id, "text": messaggio_risultati}, config)

        except requests.exceptions.RequestException:
            time.sleep(3)
        except Exception as e:
            logger.error(f"[ORECCHIE TELEGRAM] Errore nel loop listener: {str(e)}")
            time.sleep(3)


# --------------------------------------------------------------------------------------
# 8. MODULO SPEDIZIONI: PACKLINK PRO (UI MANUALE + SCHEDULER ORE 07, 13, 16) + EBAY STUB
# --------------------------------------------------------------------------------------
def controlla_spedizioni_packlink(config: dict = None) -> list:
    """
    Funzione autonoma di controllo spedizioni Packlink PRO:
      - Esegue la chiamata GET alle API di Packlink PRO
      - Isola le spedizioni in stato di anomalia (INCIDENT, EXCEPTION, RETURNED_TO_SENDER, DELIVERY_FAILED)
      - Invia su Telegram un messaggio di alert per ciascuna anomalia rilevata
      - Restituisce la lista delle anomalie trovate (lista vuota [] se tutto regolare, None se errore API)
    """
    if config is None:
        config = load_env()

    packlink_key = (
        (config.get("PACKLINK_API_KEY") if config else None)
        or os.getenv("PACKLINK_API_KEY")
        or ""
    ).strip()

    if not packlink_key:
        logger.warning("[PACKLINK PRO] PACKLINK_API_KEY mancante nel .env. Controllo saltato.")
        return None

    headers_bearer = {
        "Authorization": f"Bearer {packlink_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "BotPulichimica-Core/1.0",
    }

    try:
        response = requests.get(PACKLINK_SHIPMENTS_URL, headers=headers_bearer, timeout=15)

        # Fallback automatico per Packlink PRO API v1 (che accetta il token diretto in Authorization)
        if response.status_code != 200:
            headers_direct = dict(headers_bearer)
            headers_direct["Authorization"] = packlink_key
            response = requests.get(PACKLINK_SHIPMENTS_URL, headers=headers_direct, timeout=15)

        if response.status_code != 200:
            logger.error(f"[PACKLINK PRO] Risposta API non valida (HTTP {response.status_code}): {response.text}")
            return None

        payload = response.json()
        if isinstance(payload, dict):
            spedizioni = payload.get("shipments")
            if spedizioni is None:
                spedizioni = payload.get("data", [])
        elif isinstance(payload, list):
            spedizioni = payload
        else:
            spedizioni = []

        anomalie = []
        for spedizione in spedizioni:
            if not isinstance(spedizione, dict):
                continue

            state_raw = spedizione.get("state") or spedizione.get("status") or ""
            state = str(state_raw).strip().upper()

            if state not in STATI_ANOMALIA_PACKLINK:
                continue

            reference = (
                spedizione.get("reference")
                or spedizione.get("shipment_reference")
                or spedizione.get("id")
                or "N/D"
            )
            carrier = (
                spedizione.get("carrier")
                or spedizione.get("carrier_name")
                or spedizione.get("service")
                or "N/D"
            )

            anomalia_info = {"reference": reference, "state": state, "carrier": carrier}
            anomalie.append(anomalia_info)

            messaggio_anomalia = (
                "📦 ⚠️ **ANOMALIA SPEDIZIONE PACKLINK!**\n"
                f"Riferimento: {reference}\n"
                f"Stato: {state}\n"
                f"Vettore: {carrier}"
            )
            bocca_telegram(messaggio_anomalia, config=config)

        logger.info(
            f"[PACKLINK PRO] Controllo completato ({len(spedizioni)} spedizioni lette, "
            f"{len(anomalie)} anomalie rilevate)."
        )
        return anomalie

    except requests.exceptions.RequestException as e:
        logger.error(f"[PACKLINK PRO] Errore di rete / timeout verso Packlink PRO: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"[PACKLINK PRO] Errore imprevisto durante l'analisi spedizioni: {str(e)}")
        return None


def avvia_controllo_packlink_manuale(config: dict = None, chat_id: str = None):
    """
    Gestisce la pressione del tasto '🚚 Monitoraggio Packlink' su Telegram:
      1. Risponde subito '⏳ Controllo spedizioni Packlink in corso...'
      2. Esegue controlla_spedizioni_packlink()
      3. Se l'elenco anomalie è vuoto, conferma che tutte le spedizioni sono in regola.
    """
    if config is None:
        config = load_env()
    target_chat = chat_id or config.get("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

    _telegram_api(
        "sendMessage",
        {"chat_id": target_chat, "text": "⏳ Controllo spedizioni Packlink in corso..."},
        config,
    )

    anomalie = controlla_spedizioni_packlink(config=config)

    if anomalie is None:
        _telegram_api(
            "sendMessage",
            {"chat_id": target_chat, "text": "⚠️ Errore di connessione alle API di Packlink PRO."},
            config,
        )
    elif len(anomalie) == 0:
        _telegram_api(
            "sendMessage",
            {"chat_id": target_chat, "text": "✅ Tutte le spedizioni sono in regola. Nessuna anomalia riscontrata."},
            config,
        )


def occhi_ebay(config: dict):
    """Predisposizione architetturale per futura integrazione ordini/giacenze eBay."""
    pass


# --------------------------------------------------------------------------------------
# 9. CORE LOOP NON BLOCCANTE & GRACEFUL SHUTDOWN
# --------------------------------------------------------------------------------------
def avvia_thread_orecchie(config: dict, stop_event: threading.Event) -> threading.Thread:
    """Crea e avvia il thread daemon di ascolto Telegram."""
    t = threading.Thread(
        target=orecchie_telegram_worker,
        args=(config, stop_event),
        name="BotPulichimicaOrecchieTelegram",
        daemon=True,
    )
    t.start()
    return t


# Riferimento globale per impedire al Garbage Collector di rimuovere l'handler Win32
_WIN_CTRL_HANDLER_REF = None


def _invia_spegnimento_http_diretto(config: dict = None, timeout: int = 5):
    """
    Invia la notifica di spegnimento con una POST HTTP sincrona diretta via requests.post
    (senza passare per thread o wrapper del bot), garantendo un singolo invio.
    """
    global SHUTDOWN_NOTIFIED
    with SHUTDOWN_LOCK:
        if SHUTDOWN_NOTIFIED or RESTART_IN_PROGRESS:
            return
        SHUTDOWN_NOTIFIED = True

    token_del_bot = (
        (config.get("TELEGRAM_BOT_TOKEN") if config else None)
        or os.getenv("TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()
    id_amministratore = (
        (config.get("TELEGRAM_CHAT_ID") if config else None)
        or os.getenv("TELEGRAM_CHAT_ID")
        or ""
    ).strip()

    if not token_del_bot or not id_amministratore:
        return

    url = f"https://api.telegram.org/bot{token_del_bot}/sendMessage"
    payload = {
        "chat_id": id_amministratore,
        "text": "🔴 Bot Pulichimica disconnesso. Monitoraggio interrotto.",
    }
    try:
        requests.post(url, data=payload, timeout=timeout)
    except Exception as e:
        logger.error(f"[SHUTDOWN] Impossibile inviare l'avviso di spegnimento: {e}")


def _registra_win32_close_handler(config: dict, stop_event: threading.Event):
    """
    Su Windows intercetta anche la chiusura della finestra del terminale (X / CTRL_CLOSE_EVENT)
    inviando sincronamente il messaggio prima che il sistema chiuda la console.
    """
    global _WIN_CTRL_HANDLER_REF
    if os.name != "nt":
        return
    try:
        HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def _ctrl_handler(ctrl_type):
            # 2 = CTRL_CLOSE_EVENT, 5 = CTRL_LOGOFF_EVENT, 6 = CTRL_SHUTDOWN_EVENT
            if ctrl_type in (2, 5, 6):
                stop_event.set()
                _invia_spegnimento_http_diretto(config=config, timeout=4)
                return True
            return False

        _WIN_CTRL_HANDLER_REF = HandlerRoutine(_ctrl_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_WIN_CTRL_HANDLER_REF, True)
    except Exception as e:
        logger.debug(f"[SHUTDOWN] Registrazione handler console Windows ignorata: {e}")


def main():
    logger.info("==================================================================")
    logger.info("  BOT PULICHIMICA - Demone Autonomo Avviato [Lead Architect]")
    logger.info("==================================================================")

    config = load_env()
    shop = _normalizza_shop_domain(config)

    if not shop or not config.get("SHOPIFY_ACCESS_TOKEN"):
        logger.error("Credenziali Shopify mancanti nel file .env (SHOPIFY_SHOP/SHOPIFY_STORE_URL o SHOPIFY_ACCESS_TOKEN).")
        sys.exit(1)

    last_id_iniziale = leggi_o_inizializza_last_order_id(config)

    logger.info(f"Target Store Shopify : {shop}")
    logger.info(f"Telegram Chat ID     : {config.get('TELEGRAM_CHAT_ID', 'Non specificato')}")
    logger.info(f"Last Order ID locale : {last_id_iniziale} ({LAST_ORDER_ID_FILE.name})")
    logger.info(f"Fascia controllo ord.: 07:00 - 20:00 (ogni {ORDER_CHECK_INTERVAL}s / 1 ora)")
    logger.info(f"Check Packlink PRO   : ore {PACKLINK_CHECK_HOURS} (API key {'presente' if config.get('PACKLINK_API_KEY') else 'mancante'})")

    stop_event = threading.Event()
    _registra_win32_close_handler(config, stop_event)

    def _gestisci_segnale_arresto(signum, _frame):
        stop_event.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _gestisci_segnale_arresto)
    signal.signal(signal.SIGTERM, _gestisci_segnale_arresto)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _gestisci_segnale_arresto)

    # Notifica di avvio operativa (Rebranding Bot Pulichimica)
    bocca_telegram("🟢 Bot Pulichimica avviato. Inizio monitoraggio Shopify e Packlink.", config=config)

    orecchie_thread = avvia_thread_orecchie(config, stop_event)

    last_order_check_ts = 0.0
    ultimo_check_packlink = ""
    last_nightly_ota_date = None
    in_pausa_notturna_loggato = False

    logger.info("Loop principale non bloccante avviato (tick 1s). Premi CTRL+C per arrestare.")
    print("-" * 66)

    try:
        while not stop_event.is_set():
            try:
                now = datetime.now()
                now_ts = time.time()

                # 1. Watchdog sul listener Telegram (garantisce operatività 24/7)
                if not orecchie_thread.is_alive() and not RESTART_IN_PROGRESS:
                    logger.warning("[WATCHDOG] Thread Orecchie Telegram fermo: riavvio immediato...")
                    orecchie_thread = avvia_thread_orecchie(config, stop_event)

                # 2. Controllo Nuovi Ordini Shopify (SOLO fascia 07:00 - 20:00, ogni 3600s)
                if in_fascia_attiva_ordini(now):
                    in_pausa_notturna_loggato = False
                    if (now_ts - last_order_check_ts) >= ORDER_CHECK_INTERVAL:
                        logger.info(f"[HEARTBEAT ORDINI] Esecuzione controllo orario ordini ({now.strftime(DATE_FORMAT)})...")
                        esito_ok = controlla_nuovi_ordini_shopify(config)
                        if esito_ok:
                            last_order_check_ts = time.time()
                        else:
                            last_order_check_ts = time.time() - ORDER_CHECK_INTERVAL + ORDER_ERROR_RETRY_DELAY
                else:
                    if not in_pausa_notturna_loggato:
                        logger.info(
                            "[STANDBY NOTTURNO] Fascia 20:01-06:59 attiva: chiamate API ordini sospese. "
                            "Recupero automatico programmato alle 07:00. Ascolto Telegram attivo 24/7."
                        )
                        in_pausa_notturna_loggato = True

                # 3. Monitoraggio Spedizioni Packlink PRO (SOLO ore 7, 13, 16 - una sola volta per fascia)
                if now.hour in PACKLINK_CHECK_HOURS:
                    slot_packlink = f"{now.strftime('%Y-%m-%d')}_{now.hour:02d}"
                    if ultimo_check_packlink != slot_packlink:
                        ultimo_check_packlink = slot_packlink
                        logger.info(f"[PACKLINK PRO] Avvio controllo spedizioni fascia oraria {slot_packlink}...")
                        controlla_spedizioni_packlink(config)

                # 4. Controllo aggiornamenti OTA notturno (ore 03:00, una volta al giorno)
                today_str = now.strftime("%Y-%m-%d")
                if now.hour == 3 and last_nightly_ota_date != today_str:
                    last_nightly_ota_date = today_str
                    cerca_aggiornamenti(config)

                stop_event.wait(timeout=MAIN_LOOP_TICK_SECONDS)

            except Exception as e:
                logger.exception(f"[CRITICAL ERROR] Eccezione intercettata nel loop principale: {str(e)}")
                stop_event.wait(timeout=5)

    except KeyboardInterrupt:
        # Maschera subito eventuali SIGINT multipli propagati dalla console Windows
        # affinché la chiamata sincrona requests.post arrivi sempre a compimento
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        stop_event.set()
        logger.warning("[STOP] Interruzione rilevata: invio sincrono avviso di spegnimento...")

        TOKEN_DEL_BOT = (config.get("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        ID_AMMINISTRATORE = (config.get("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()

        if TOKEN_DEL_BOT and ID_AMMINISTRATORE and not SHUTDOWN_NOTIFIED and not RESTART_IN_PROGRESS:
            with SHUTDOWN_LOCK:
                globals()["SHUTDOWN_NOTIFIED"] = True
            url = f"https://api.telegram.org/bot{TOKEN_DEL_BOT}/sendMessage"
            payload = {
                "chat_id": ID_AMMINISTRATORE,
                "text": "🔴 Bot Pulichimica disconnesso. Monitoraggio interrotto.",
            }
            try:
                requests.post(url, data=payload, timeout=5)
                logger.info("[SHUTDOWN] Avviso di spegnimento consegnato a Telegram.")
            except Exception as e:
                logger.error(f"[SHUTDOWN] Errore durante l'invio dell'avviso di spegnimento: {e}")

        sys.exit(0)


if __name__ == "__main__":
    main()
