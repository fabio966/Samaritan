"""
========================================================================================
BOT PULICHIMICA - Modulo Task Automatici in Background (task_automatici.py)
========================================================================================
Gestisce i job pianificati in background:
  1. Avviso Sottoscorta Telegram (prodotti con giacenza tra 1 e 5 pz, escludendo <= 0)
  2. Report Serale Google Drive/Sheets (ordini pagati di oggi con costi, commissioni e netto)
Importa config, api_shopify e bot_telegram senza creare dipendenze circolari.
========================================================================================
"""

import os
import asyncio
from pathlib import Path
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from config import (
    BASE_DIR,
    TELEGRAM_CHAT_ID,
    load_env,
    logger,
)
from api_shopify import (
    recupera_ordini_del_giorno,
    recupera_prodotti_sottoscorta,
)
from bot_telegram import (
    bocca_telegram,
    invia_messaggio_lungo,
)

# --------------------------------------------------------------------------------------
# VARIABILI GLOBALI ECONOMIA E COMMISSIONI
# --------------------------------------------------------------------------------------
COMMISSIONE_PERCENTUALE = 0.11
FEE_FISSA_TRANSAZIONE = 0.35

# Scope richiesti per Google Drive e Google Sheets
SCOPES_GOOGLE = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

INTESTAZIONE_FOGLIO = [
    "Data",
    "Numero Ordine",
    "Cliente",
    "Articoli",
    "Prezzo Vendita Totale (€)",
    "Costo Acquisto Totale (€)",
    "Commissioni (€)",
    "Netto Reale (€)",
]


async def _invia_messaggio(testo: str, bot=None, chat_id: str = None, cfg: dict = None):
    """Supporta sia l'oggetto bot passato dallo scheduler sia le routine interne di Telegram."""
    target_chat = chat_id or (cfg.get("TELEGRAM_CHAT_ID") if cfg else None) or TELEGRAM_CHAT_ID
    if not target_chat:
        logger.error("[TASK AUTOMATICI] chat_id mancante per l'invio del messaggio.")
        return

    # Se bot è un'istanza con metodo send_message (es. telegram.Bot o aiogram)
    if bot and hasattr(bot, "send_message"):
        try:
            res = bot.send_message(chat_id=target_chat, text=testo)
            if asyncio.iscoroutine(res):
                await res
            return
        except Exception as e:
            logger.warning(f"[TASK AUTOMATICI] Invio tramite bot instance fallito ({e}), fallback su bocca_telegram...")

    # Fallback standard sull'infrastruttura HTTP nativa del bot
    invia_messaggio_lungo(testo, chat_id=target_chat, cfg=cfg)


def _trova_percorso_credenziali_google(cfg: dict = None) -> Path | None:
    """Individua il file JSON del Service Account Google nel progetto."""
    percorso_env = (
        (cfg.get("GOOGLE_SERVICE_ACCOUNT_JSON") if cfg else None)
        or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        or ""
    ).strip()

    candidati = [
        Path(percorso_env) if percorso_env else None,
        BASE_DIR / percorso_env if percorso_env else None,
        BASE_DIR / "google_credentials.json",
        BASE_DIR / "service_account.json",
        BASE_DIR / "credentials.json",
    ]

    for p in candidati:
        if p and p.exists() and p.is_file():
            return p
    return None


# --------------------------------------------------------------------------------------
# 1. CONTROLLO SOTTOSCORTA TELEGRAM
# --------------------------------------------------------------------------------------
async def controllo_sottoscorta(bot=None, chat_id: str = None):
    """
    Interroga Shopify, cerca tutti i prodotti con giacenza compresa tra 1 e 5 pezzi (inclusi)
    e invia un alert di sottoscorta su Telegram.
    Ignora categoricamente i prodotti a giacenza 0 o negativa.
    """
    cfg = load_env()
    target_chat = chat_id or cfg.get("TELEGRAM_CHAT_ID") or TELEGRAM_CHAT_ID
    logger.info("[SOTTOSCORTA] Avvio controllo prodotti con giacenza tra 1 e 5 pz...")

    try:
        # Esecuzione in thread per non bloccare l'event loop asyncio
        prodotti = await asyncio.to_thread(recupera_prodotti_sottoscorta, cfg=cfg, min_qty=1, max_qty=5)

        if not prodotti:
            logger.info("[SOTTOSCORTA] Nessun articolo sottoscorta rilevato (giacenze > 5 o esaurite).")
            return

        righe = [
            "⚠️ **AVVISO SOTTOSCORTA MAGAZZINO** ⚠️\n"
            f"I seguenti **{len(prodotti)}** articoli hanno giacenza critica (tra 1 e 5 pz):\n"
        ]

        for p in prodotti:
            nome = p.get("full_name") or p.get("title")
            sku = p.get("sku")
            sku_str = f" [SKU: {sku}]" if sku else ""
            qty = p.get("inventory_quantity", 0)
            righe.append(f"• **{nome}**{sku_str}: `{qty} pz`")

        righe.append("\n_Provvedere al riordino presso il fornitore._")
        messaggio = "\n".join(righe)

        await _invia_messaggio(messaggio, bot=bot, chat_id=target_chat, cfg=cfg)
        logger.info(f"[SOTTOSCORTA] Alert notificato con successo per {len(prodotti)} articoli.")

    except Exception as e:
        logger.exception(f"[SOTTOSCORTA] Errore durante l'esecuzione del task: {e}")


# --------------------------------------------------------------------------------------
# 2. REPORT SERALE GOOGLE DRIVE / GOOGLE SHEETS
# --------------------------------------------------------------------------------------
async def aggiorna_report_drive(chat_id: str = None):
    """
    Recupera tutti gli ordini pagati di oggi su Shopify.
    Calcola per ciascuno:
      - Prezzo Vendita Totale
      - Costo Acquisto Totale (da InventoryItem.cost o 0.0)
      - Commissioni (11% + 0.35€)
      - Netto Reale (Vendita - Costo - Commissioni)
    Autentica il Service Account, apre il foglio di calcolo preesistente su Drive
    e accoda le nuove righe di oggi in fondo, preservando lo storico precedente.
    Invia '✅ Report Drive aggiornato' su Telegram.
    """
    cfg = load_env()
    target_chat = chat_id or cfg.get("TELEGRAM_CHAT_ID") or TELEGRAM_CHAT_ID
    oggi_str = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"[REPORT DRIVE] Avvio compilazione report serale per la data {oggi_str}...")

    try:
        # 1. Recupero ordini pagati odierni con relativi costi d'acquisto
        ordini = await asyncio.to_thread(recupera_ordini_del_giorno, data_riferimento=oggi_str, cfg=cfg)

        if not ordini:
            logger.info(f"[REPORT DRIVE] Nessun ordine pagato registrato per oggi ({oggi_str}).")
            await _invia_messaggio(
                f"ℹ️ **Report Drive ({oggi_str})**: nessun nuovo ordine pagato da registrare oggi.",
                chat_id=target_chat,
                cfg=cfg,
            )
            return

        # 2. Calcolo metriche economiche per ciascuna riga da scrivere
        righe_da_accodare = []
        for o in ordini:
            prezzo_vendita = round(float(o.get("total_price") or 0.0), 2)
            costo_acquisto = round(float(o.get("total_cost") or 0.0), 2)

            # Calcolo commissioni: 11% + 0.35€
            commissione = round((prezzo_vendita * COMMISSIONE_PERCENTUALE) + FEE_FISSA_TRANSAZIONE, 2)
            netto_reale = round(prezzo_vendita - costo_acquisto - commissione, 2)

            # Riepilogo stringa articoli
            articoli_list = [
                f"{item.get('quantity', 1)}x {item.get('name')}" + (f" ({item.get('sku')})" if item.get('sku') else "")
                for item in o.get("line_items", [])
            ]
            articoli_str = ", ".join(articoli_list) if articoli_list else "Articoli non specificati"

            righe_da_accodare.append([
                oggi_str,
                str(o.get("order_number") or ""),
                str(o.get("customer") or ""),
                articoli_str,
                prezzo_vendita,
                costo_acquisto,
                commissione,
                netto_reale,
            ])

        # 3. Autenticazione Google Service Account tramite file JSON
        creds_path = _trova_percorso_credenziali_google(cfg)
        if not creds_path:
            err_msg = (
                "⚠️ **Errore Report Drive:** file credenziali Service Account non trovato.\n"
                "Posiziona `google_credentials.json` nella cartella principale del bot o imposta "
                "`GOOGLE_SERVICE_ACCOUNT_JSON` nel file `.env`."
            )
            logger.error(err_msg)
            await _invia_messaggio(err_msg, chat_id=target_chat, cfg=cfg)
            return

        # 4. Connessione a Google Sheets tramite gspread
        creds = Credentials.from_service_account_file(str(creds_path), scopes=SCOPES_GOOGLE)
        gc = gspread.authorize(creds)

        nome_foglio = (
            (cfg.get("GOOGLE_SHEET_NAME") if cfg else None)
            or os.getenv("GOOGLE_SHEET_NAME")
            or "Report Pulichimica"
        ).strip()

        id_foglio = (
            (cfg.get("GOOGLE_SHEET_ID") if cfg else None)
            or os.getenv("GOOGLE_SHEET_ID")
            or ""
        ).strip()

        if id_foglio:
            spreadsheet = await asyncio.to_thread(gc.open_by_key, id_foglio)
        else:
            spreadsheet = await asyncio.to_thread(gc.open, nome_foglio)

        worksheet = spreadsheet.sheet1

        # Se il foglio è completamente vuoto, inserisce l'intestazione
        valori_esistenti = await asyncio.to_thread(worksheet.get_all_values)
        if not valori_esistenti:
            await asyncio.to_thread(worksheet.append_row, INTESTAZIONE_FOGLIO, value_input_option="USER_ENTERED")
            logger.info(f"[REPORT DRIVE] Inserita intestazione colonne nel foglio '{spreadsheet.title}'.")

        # 5. Accodamento righe senza cancellare lo storico precedente
        await asyncio.to_thread(worksheet.append_rows, righe_da_accodare, value_input_option="USER_ENTERED")
        logger.info(f"[REPORT DRIVE] Accodate {len(righe_da_accodare)} nuove righe nel foglio '{spreadsheet.title}'.")

        # 6. Notifica Telegram di successo
        await _invia_messaggio("✅ Report Drive aggiornato", chat_id=target_chat, cfg=cfg)

    except Exception as e:
        logger.exception(f"[REPORT DRIVE] Errore durante l'aggiornamento del foglio Google Drive: {e}")
        await _invia_messaggio(
            f"❌ **Errore aggiornamento Report Drive:**\n`{str(e)}`",
            chat_id=target_chat,
            cfg=cfg,
        )
