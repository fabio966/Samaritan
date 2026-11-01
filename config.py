"""
========================================================================================
BOT PULICHIMICA - Modulo Configurazione Centrale (config.py)
========================================================================================
Gestisce il caricamento del file .env, il sistema di logging condiviso, le costanti
operative e le variabili globali (Token Telegram, chiavi API Shopify, Packlink PRO,
Google Gemini e Chat ID amministratore).
Non importa alcun altro modulo del progetto per prevenire qualsiasi import circolare.
========================================================================================
"""

import os
import sys
import logging
import threading
from pathlib import Path

import dotenv
from dotenv import load_dotenv

# --------------------------------------------------------------------------------------
# 1. CONFIGURAZIONE UTF-8 E PERCORSI DI SISTEMA
# --------------------------------------------------------------------------------------
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE_PATH = BASE_DIR / ".env"
LOG_FILE_PATH = BASE_DIR / "samaritan.log"
LAST_ORDER_ID_FILE = BASE_DIR / "last_order_id.txt"
ENTRY_POINT_FILE = BASE_DIR / "main.py"

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [BOT PULICHIMICA] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger("BotPulichimica")
logger.setLevel(logging.INFO)

if not logger.handlers:
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    logger.addHandler(file_handler)

# --------------------------------------------------------------------------------------
# 2. COSTANTI OPERATIVE E PARAMETRI DI SCHEDULAZIONE
# --------------------------------------------------------------------------------------
SHOPIFY_REST_VERSION = "2024-01"
ORDER_CHECK_INTERVAL = 3600          # Controllo ordini ogni 3600s (1 ora) tra 07:00 e 20:00
ORDER_ERROR_RETRY_DELAY = 300        # Retry dopo 5 minuti in caso di errore di rete temporaneo
MAIN_LOOP_TICK_SECONDS = 1.0         # Tick non bloccante del loop principale

PACKLINK_SHIPMENTS_URL = "https://api.packlink.com/v1/shipments"
PACKLINK_CHECK_HOURS = (7, 13, 16)
STATI_ANOMALIA_PACKLINK = {
    "INCIDENT",
    "PARCEL_INCIDENT",
    "EXCEPTION",
    "RETURNED_TO_SENDER",
    "DELIVERY_FAILED",
}

GITHUB_RAW_BASE_URL = "https://raw.githubusercontent.com/fabio966/Samaritan/main"
MODULI_PROGETTO = (
    "config.py",
    "api_shopify.py",
    "api_packlink.py",
    "ai_vision.py",
    "task_automatici.py",
    "bot_telegram.py",
    "main.py",
)

USER_AGENT = "BotPulichimica-Core/1.0"

# Primitive di sincronizzazione e stato condiviso tra i moduli
RESTART_LOCK = threading.Lock()
SHUTDOWN_LOCK = threading.Lock()
stato_sistema = {
    "restart_in_progress": False,
    "shutdown_notified": False,
}
stato_utente = {}


# --------------------------------------------------------------------------------------
# 3. CARICAMENTO FILE .ENV E VARIABILI GLOBALI
# --------------------------------------------------------------------------------------
def load_env(env_path: Path = None) -> dict:
    """Carica il file .env e restituisce il dizionario delle credenziali aggiornate."""
    target_env = env_path or ENV_FILE_PATH
    if target_env.exists():
        load_dotenv(dotenv_path=target_env, override=True)
    else:
        logger.warning(f"File .env non trovato in: {target_env}")

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
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY", "").strip(),
        "GOOGLE_SERVICE_ACCOUNT_JSON": os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "google_credentials.json").strip(),
        "GOOGLE_SHEET_NAME": os.getenv("GOOGLE_SHEET_NAME", "Report Pulichimica").strip(),
        "GOOGLE_SHEET_ID": os.getenv("GOOGLE_SHEET_ID", "").strip(),
    }


def normalizza_shop_domain(cfg: dict = None) -> str:
    """Restituisce il dominio myshopify pulito leggendo SHOPIFY_SHOP o SHOPIFY_STORE_URL."""
    raw_shop = (
        (cfg.get("SHOPIFY_SHOP") if cfg else None)
        or (cfg.get("SHOPIFY_STORE_URL") if cfg else None)
        or os.getenv("SHOPIFY_SHOP")
        or os.getenv("SHOPIFY_STORE_URL")
        or ""
    ).strip()
    return raw_shop.replace("https://", "").replace("http://", "").rstrip("/")


# Esposizione immediata delle variabili globali all'importazione del modulo
_initial_cfg = load_env()

SHOPIFY_SHOP = _initial_cfg["SHOPIFY_SHOP"]
SHOPIFY_STORE_URL = _initial_cfg["SHOPIFY_STORE_URL"]
SHOPIFY_CLIENT_ID = _initial_cfg["SHOPIFY_CLIENT_ID"]
SHOPIFY_CLIENT_SECRET = _initial_cfg["SHOPIFY_CLIENT_SECRET"]
SHOPIFY_ACCESS_TOKEN = _initial_cfg["SHOPIFY_ACCESS_TOKEN"]
SHOPIFY_API_VERSION = _initial_cfg["SHOPIFY_API_VERSION"]
TELEGRAM_BOT_TOKEN = _initial_cfg["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = _initial_cfg["TELEGRAM_CHAT_ID"]
PACKLINK_API_KEY = _initial_cfg["PACKLINK_API_KEY"]
GEMINI_API_KEY = _initial_cfg["GEMINI_API_KEY"]
GOOGLE_SERVICE_ACCOUNT_JSON = _initial_cfg["GOOGLE_SERVICE_ACCOUNT_JSON"]
GOOGLE_SHEET_NAME = _initial_cfg["GOOGLE_SHEET_NAME"]
GOOGLE_SHEET_ID = _initial_cfg["GOOGLE_SHEET_ID"]
