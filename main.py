"""
========================================================================================
BOT PULICHIMICA - Entry-Point Principale (main.py)
========================================================================================
Coordina tutti i moduli dell'architettura:
  - Scheduler asincrono (AsyncIOScheduler) per Sottoscorta (07:00, 13:00, 16:00)
    e Report Serale Google Drive (20:00)
  - Listener Telegram non bloccante 24/7
  - Heartbeat orario ordini Shopify (07:00 - 20:00)
  - Controllo spedizioni Packlink PRO (07:00, 13:00, 16:00)
  - Auto-Update notturno GitHub OTA (03:00)
  - Graceful Shutdown sincrono e bloccante
========================================================================================
"""

import sys
import time
import signal
import asyncio
import threading
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

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
from task_automatici import (
    aggiorna_report_drive,
    controllo_sottoscorta,
)


async def main():
    logger.info("==================================================================")
    logger.info("  BOT PULICHIMICA - Demone Autonomo Avviato [Modular Core]")
    logger.info("==================================================================")

    cfg = load_env()
    shop = normalizza_shop_domain(cfg)

    if not shop or not cfg.get("SHOPIFY_ACCESS_TOKEN"):
        logger.error("Credenziali Shopify mancanti nel file .env (SHOPIFY_SHOP/SHOPIFY_STORE_URL o SHOPIFY_ACCESS_TOKEN).")
        sys.exit(1)

    last_id_iniziale = leggi_o_inizializza_last_order_id(cfg)
    chat_id_target = cfg.get("TELEGRAM_CHAT_ID")

    logger.info(f"Target Store Shopify : {shop}")
    logger.info(f"Telegram Chat ID     : {chat_id_target or 'Non specificato'}")
    logger.info(f"Last Order ID locale : {last_id_iniziale} ({LAST_ORDER_ID_FILE.name})")
    logger.info(f"Fascia controllo ord.: 07:00 - 20:00 (ogni {ORDER_CHECK_INTERVAL}s / 1 ora)")
    logger.info(f"Check Packlink PRO   : ore {PACKLINK_CHECK_HOURS}")

    stop_event = threading.Event()
    registra_win32_close_handler(cfg, stop_event)

    def _gestisci_segnale_arresto(_signum, _frame):
        stop_event.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _gestisci_segnale_arresto)
    signal.signal(signal.SIGTERM, _gestisci_segnale_arresto)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _gestisci_segnale_arresto)

    # 1. Configurazione e avvio AsyncIOScheduler in background
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        controllo_sottoscorta,
        "cron",
        hour="7,13,16",
        minute=0,
        args=[None, chat_id_target],
        id="job_sottoscorta",
        name="Controllo Sottoscorta (07:00, 13:00, 16:00)",
    )
    scheduler.add_job(
        aggiorna_report_drive,
        "cron",
        hour=20,
        minute=0,
        args=[chat_id_target],
        id="job_report_drive",
        name="Report Serale Google Drive (20:00)",
    )
    scheduler.start()
    logger.info("[SCHEDULER] AsyncIOScheduler attivo: Sottoscorta (07:00, 13:00, 16:00) | Report Drive (20:00)")

    # 2. Notifica di avvio operativa
    bocca_telegram("🟢 Bot Pulichimica avviato. Inizio monitoraggio Shopify, Packlink e Scheduler attivo.", cfg=cfg)

    # 3. Avvio listener Telegram 24/7 (long polling thread non bloccante)
    orecchie_thread = avvia_thread_orecchie(cfg, stop_event)

    last_order_check_ts = 0.0
    ultimo_check_packlink = ""
    last_nightly_ota_date = None
    in_pausa_notturna_loggato = False

    logger.info("Loop principale asincrono avviato su main.py (tick 1s). Premi CTRL+C per arrestare.")
    print("-" * 66)

    try:
        while not stop_event.is_set():
            try:
                now = datetime.now()
                now_ts = time.time()

                # Watchdog sul listener Telegram
                if not orecchie_thread.is_alive() and not stato_sistema["restart_in_progress"]:
                    logger.warning("[WATCHDOG] Thread Orecchie Telegram fermo: riavvio immediato...")
                    orecchie_thread = avvia_thread_orecchie(cfg, stop_event)

                # Controllo Nuovi Ordini Shopify (fascia 07:00 - 20:00, ogni 3600s)
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
                            "[STANDBY NOTTURNO] Fascia 20:01-06:59 attiva: ordini Shopify sospesi. "
                            "Scheduler, Drive e Telegram attivi."
                        )
                        in_pausa_notturna_loggato = True

                # Monitoraggio Spedizioni Packlink PRO (ore 7, 13, 16)
                if now.hour in PACKLINK_CHECK_HOURS:
                    slot_packlink = f"{now.strftime('%Y-%m-%d')}_{now.hour:02d}"
                    if ultimo_check_packlink != slot_packlink:
                        ultimo_check_packlink = slot_packlink
                        logger.info(f"[PACKLINK PRO] Avvio controllo spedizioni fascia oraria {slot_packlink}...")
                        controlla_spedizioni_packlink(
                            cfg=cfg,
                            notifica_fn=lambda msg: bocca_telegram(msg, cfg=cfg),
                        )

                # Controllo aggiornamenti OTA notturno (ore 03:00)
                today_str = now.strftime("%Y-%m-%d")
                if now.hour == 3 and last_nightly_ota_date != today_str:
                    last_nightly_ota_date = today_str
                    cerca_aggiornamenti(cfg)

                await asyncio.sleep(MAIN_LOOP_TICK_SECONDS)

            except Exception as e:
                logger.exception(f"[CRITICAL ERROR] Eccezione intercettata nel loop principale: {str(e)}")
                await asyncio.sleep(5)

    except (KeyboardInterrupt, asyncio.CancelledError):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        stop_event.set()
        logger.warning("[STOP] Interruzione rilevata: arresto scheduler e invio avviso di spegnimento...")
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        invia_spegnimento_sincrono(cfg=cfg, timeout=5)
        sys.exit(0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
