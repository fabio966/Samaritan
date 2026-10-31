"""
========================================================================================
BOT PULICHIMICA - Entry-Point Principale (main.py)
========================================================================================
Coordina tutti i moduli dell'architettura (config, api_shopify, api_packlink, bot_telegram),
avvia il listener Telegram 24/7, gestisce lo scheduler non bloccante (Shopify ordini,
Packlink PRO, Auto-Update notturno ore 03:00) e il Graceful Shutdown sincrono.
========================================================================================
"""

import sys
import time
import signal
import threading
from datetime import datetime

from config import (
    DATE_FORMAT,
    LAST_ORDER_ID_FILE,
    MAIN_LOOP_TICK_SECONDS,
    ORDER_CHECK_INTERVAL,
    ORDER_ERROR_RETRY_DELAY,
    PACKLINK_CHECK_HOURS,
    load_env,
    logger,
    normalizza_shop_domain,
    stato_sistema,
)
from api_shopify import (
    controlla_nuovi_ordini_shopify,
    in_fascia_attiva_ordini,
    leggi_o_inizializza_last_order_id,
)
from api_packlink import controlla_spedizioni_packlink
from bot_telegram import (
    avvia_thread_orecchie,
    bocca_telegram,
    cerca_aggiornamenti,
    invia_spegnimento_sincrono,
    registra_win32_close_handler,
)


def main():
    logger.info("==================================================================")
    logger.info("  BOT PULICHIMICA - Demone Autonomo Avviato [Modular Core]")
    logger.info("==================================================================")

    cfg = load_env()
    shop = normalizza_shop_domain(cfg)

    if not shop or not cfg.get("SHOPIFY_ACCESS_TOKEN"):
        logger.error("Credenziali Shopify mancanti nel file .env (SHOPIFY_SHOP/SHOPIFY_STORE_URL o SHOPIFY_ACCESS_TOKEN).")
        sys.exit(1)

    last_id_iniziale = leggi_o_inizializza_last_order_id(cfg)

    logger.info(f"Target Store Shopify : {shop}")
    logger.info(f"Telegram Chat ID     : {cfg.get('TELEGRAM_CHAT_ID', 'Non specificato')}")
    logger.info(f"Last Order ID locale : {last_id_iniziale} ({LAST_ORDER_ID_FILE.name})")
    logger.info(f"Fascia controllo ord.: 07:00 - 20:00 (ogni {ORDER_CHECK_INTERVAL}s / 1 ora)")
    logger.info(f"Check Packlink PRO   : ore {PACKLINK_CHECK_HOURS} (API key {'presente' if cfg.get('PACKLINK_API_KEY') else 'mancante'})")

    stop_event = threading.Event()
    registra_win32_close_handler(cfg, stop_event)

    def _gestisci_segnale_arresto(_signum, _frame):
        stop_event.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _gestisci_segnale_arresto)
    signal.signal(signal.SIGTERM, _gestisci_segnale_arresto)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _gestisci_segnale_arresto)

    # Notifica di avvio operativa
    bocca_telegram("🟢 Bot Pulichimica avviato. Inizio monitoraggio Shopify e Packlink.", cfg=cfg)

    # Avvio listener Telegram 24/7
    orecchie_thread = avvia_thread_orecchie(cfg, stop_event)

    last_order_check_ts = 0.0
    ultimo_check_packlink = ""
    last_nightly_ota_date = None
    in_pausa_notturna_loggato = False

    logger.info("Loop principale non bloccante avviato su main.py (tick 1s). Premi CTRL+C per arrestare.")
    print("-" * 66)

    try:
        while not stop_event.is_set():
            try:
                now = datetime.now()
                now_ts = time.time()

                # 1. Watchdog sul listener Telegram (garantisce operatività 24/7)
                if not orecchie_thread.is_alive() and not stato_sistema["restart_in_progress"]:
                    logger.warning("[WATCHDOG] Thread Orecchie Telegram fermo: riavvio immediato...")
                    orecchie_thread = avvia_thread_orecchie(cfg, stop_event)

                # 2. Controllo Nuovi Ordini Shopify (SOLO fascia 07:00 - 20:00, ogni 3600s)
                if in_fascia_attiva_ordini(now):
                    in_pausa_notturna_loggato = False
                    if (now_ts - last_order_check_ts) >= ORDER_CHECK_INTERVAL:
                        logger.info(f"[HEARTBEAT ORDINI] Esecuzione controllo orario ordini ({now.strftime(DATE_FORMAT)})...")
                        esito_ok = controlla_nuovi_ordini_shopify(
                            cfg=cfg,
                            notifica_fn=lambda msg: bocca_telegram(msg, cfg=cfg),
                        )
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
                        controlla_spedizioni_packlink(
                            cfg=cfg,
                            notifica_fn=lambda msg: bocca_telegram(msg, cfg=cfg),
                        )

                # 4. Controllo aggiornamenti OTA notturno (ore 03:00, una volta al giorno -> riavvia main.py)
                today_str = now.strftime("%Y-%m-%d")
                if now.hour == 3 and last_nightly_ota_date != today_str:
                    last_nightly_ota_date = today_str
                    cerca_aggiornamenti(cfg)

                stop_event.wait(timeout=MAIN_LOOP_TICK_SECONDS)

            except Exception as e:
                logger.exception(f"[CRITICAL ERROR] Eccezione intercettata nel loop principale: {str(e)}")
                stop_event.wait(timeout=5)

    except KeyboardInterrupt:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        stop_event.set()
        logger.warning("[STOP] Interruzione rilevata: invio sincrono avviso di spegnimento...")
        invia_spegnimento_sincrono(cfg=cfg, timeout=5)
        sys.exit(0)


if __name__ == "__main__":
    main()
