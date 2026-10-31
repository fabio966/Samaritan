"""
========================================================================================
PROJECT SAMARITAN - Autonomous E-Commerce Operating System
Lead Architect Core Module
========================================================================================
Modulo: samaritan_core.py
Descrizione: Loop autonomo di background con battito cardiaco (heartbeat) a intervalli
             regolari. Integra:
             - Sensori "Occhi": monitoraggio canali di vendita (Shopify; futuro eBay).
             - Canale "Bocca": sistema di notifica bidirezionale Telegram Bot.
========================================================================================
"""

import os
import sys
import time
import json
import logging
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

# Evita duplicazione degli handler se ricaricato
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

    # Ritorna un dizionario con tutte le configurazioni rilevanti di Samaritan
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
    Gestisce in modo sicuro le eccezioni di rete in modo da non interrompere mai il loop primario.
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
# 4. MODULO OCCHI: SHOPIFY
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
# 5. MODULO OCCHI: EBAY (Predisposizione architetturale futura)
# --------------------------------------------------------------------------------------
def occhi_ebay(config: dict):
    """Placeholder architetturale: modulo sensore ordini e notifiche eBay."""
    pass

# --------------------------------------------------------------------------------------
# 6. CORE LOOP (BATTITO CARDIACO)
# --------------------------------------------------------------------------------------
def main():
    logger.info("==================================================================")
    logger.info("  PROJECT SAMARITAN - Core Autonomo Avviato [Lead Architect Mode]")
    logger.info("==================================================================")
    
    # Caricamento credenziali
    env_file = Path(__file__).parent / ".env"
    config = load_env(env_file)
    
    if not config.get("SHOPIFY_ACCESS_TOKEN"):
        logger.error(f"Credenziali non trovate in {env_file}. Assicurarsi che il file .env esista.")
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

    logger.info("Avvio del ciclo operativo continuo. Premi CTRL+C per arrestare Samaritan.")
    print("-" * 66)

    cycle_count = 0

    while True:
        try:
            cycle_count += 1
            now_str = datetime.now().strftime(DATE_FORMAT)
            logger.info(f"[PULSE #{cycle_count}] Battito cardiaco Samaritan alle ore {now_str}")

            # Esecuzione modulo occhi
            occhi_shopify(config, limit=3)

            # Futuro: occhi_ebay(config)

            logger.info(f"[STANDBY] In attesa del prossimo battito tra {interval_seconds} secondi...")
            print("-" * 66)
            time.sleep(interval_seconds)

        except KeyboardInterrupt:
            logger.warning("\n[STOP] Rilevata interruzione da tastiera (CTRL+C). Arresto sicuro di Samaritan in corso...")
            break
        except Exception as e:
            logger.exception(f"[CRITICAL ERROR] Errore imprevisto nel loop principale: {str(e)}")
            logger.info("Ripristino in corso: attesa di 60 secondi prima del nuovo tentativo...")
            time.sleep(60)

    logger.info("Samaritan Core disattivato. Sessione terminata.")

if __name__ == "__main__":
    main()
