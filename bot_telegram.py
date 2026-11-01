"""
========================================================================================
BOT PULICHIMICA - Modulo Interfaccia Telegram, OTA & Graceful Shutdown (bot_telegram.py)
========================================================================================
Gestisce:
  - Invio messaggi e tastiere interattive (/menu e /admin)
  - Handler delle callback_query e macchina a stati per ricerca prodotti e carico magazzino
  - Ricezione foto e PDF per estrazione dati fatture/bolle con Google Gemini (ai_vision)
  - Listener in background 24/7 (polling getUpdates non bloccante)
  - Auto-Update modulare (GitHub Raw / Telegram file) con riavvio su 'main.py'
  - Avviso sincrono e bloccante di disconnessione (Graceful Shutdown con requests)
========================================================================================
"""

import os
import sys
import time
import json
import ctypes
import asyncio
import threading
from pathlib import Path

import requests

from config import (
    BASE_DIR,
    ENTRY_POINT_FILE,
    GITHUB_RAW_BASE_URL,
    MODULI_PROGETTO,
    RESTART_LOCK,
    SHUTDOWN_LOCK,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    load_env,
    logger,
    stato_sistema,
    stato_utente,
)
from api_shopify import (
    cerca_prodotti_shopify,
    check_salute_shopify,
    recupera_ultimi_tre_ordini,
)
from api_packlink import (
    check_salute_packlink,
    controlla_spedizioni_packlink,
)
from ai_vision import analizza_documento_carico

# Riferimento globale per evitare che il Garbage Collector rimuova l'handler Win32
_WIN_CTRL_HANDLER_REF = None


# --------------------------------------------------------------------------------------
# 1. INVIO MESSAGGI E CHIAMATE API TELEGRAM
# --------------------------------------------------------------------------------------
def _risolvi_credenziali_telegram(cfg: dict = None) -> tuple:
    bot_token = (
        (cfg.get("TELEGRAM_BOT_TOKEN") if cfg else None)
        or os.getenv("TELEGRAM_BOT_TOKEN")
        or TELEGRAM_BOT_TOKEN
        or ""
    ).strip()
    chat_id = (
        (cfg.get("TELEGRAM_CHAT_ID") if cfg else None)
        or os.getenv("TELEGRAM_CHAT_ID")
        or TELEGRAM_CHAT_ID
        or ""
    ).strip()
    return bot_token, chat_id


def bocca_telegram(messaggio: str, cfg: dict = None) -> bool:
    """
    Invia un messaggio testuale alla chat Telegram configurata.
    Intercetta qualsiasi eccezione HTTP/rete per non bloccare mai il demone.
    """
    bot_token, chat_id = _risolvi_credenziali_telegram(cfg)
    if not bot_token or not chat_id:
        logger.error("[BOCCA TELEGRAM] TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID mancanti nel .env.")
        return False

    endpoint_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": messaggio}

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


def _telegram_api(metodo: str, payload: dict, cfg: dict = None, timeout: int = 12) -> bool:
    """Chiamata POST generica alle Telegram Bot API con gestione completa degli errori."""
    bot_token, _ = _risolvi_credenziali_telegram(cfg)
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


def invia_messaggio_lungo(testo: str, chat_id: str, cfg: dict = None):
    """Invia messaggi spezzandoli in blocchi se superano il limite Telegram di 4096 caratteri."""
    if not testo:
        return
    limite = 4000
    if len(testo) <= limite:
        _telegram_api("sendMessage", {"chat_id": chat_id, "text": testo}, cfg)
    else:
        parti = [testo[i : i + limite] for i in range(0, len(testo), limite)]
        for p in parti:
            _telegram_api("sendMessage", {"chat_id": chat_id, "text": p}, cfg)


# --------------------------------------------------------------------------------------
# 2. TASTIERE E SOTTOMENU (ReplyKeyboardMarkup e InlineKeyboardMarkup)
# --------------------------------------------------------------------------------------
REPLY_KEYBOARD_PRINCIPALE = {
    "keyboard": [
        [
            {"text": "📦 Ricerca Prodotto"},
            {"text": "📋 Ultimi 3 Ordini"},
        ],
        [
            {"text": "🚚 Monitoraggio Packlink"},
            {"text": "📥 Carico Magazzino"},
        ],
    ],
    "resize_keyboard": True,
    "one_time_keyboard": False,
}

MENU_OPERATIVO_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "📦 Ricerca Prodotto", "callback_data": "cmd_cerca_prodotto"},
            {"text": "📋 Ultimi 3 Ordini", "callback_data": "cmd_ultimi_ordini"},
        ],
        [
            {"text": "🚚 Monitoraggio Packlink", "callback_data": "cmd_packlink"},
            {"text": "📥 Carico Magazzino", "callback_data": "cmd_carico_magazzino"},
        ],
        [
            {"text": "🔙 Chiudi", "callback_data": "cmd_chiudi"},
        ],
    ]
}

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


def invia_menu_operativo(cfg: dict = None, chat_id: str = None) -> bool:
    """Invia il Menu Operativo attivando la ReplyKeyboardMarkup principale."""
    _, default_chat_id = _risolvi_credenziali_telegram(cfg)
    target_chat = chat_id or default_chat_id
    payload = {
        "chat_id": target_chat,
        "text": "🧭 **Menu Operativo Bot Pulichimica**\nSeleziona un'operazione dai pulsanti in basso:",
        "reply_markup": REPLY_KEYBOARD_PRINCIPALE,
    }
    ok = _telegram_api("sendMessage", payload, cfg)
    if ok:
        logger.info("[UI TELEGRAM] Menu Operativo (ReplyKeyboardMarkup) inviato.")
    return ok


def invia_menu_admin(cfg: dict = None, chat_id: str = None) -> bool:
    """Invia il Menu Sviluppatore separato (attivato dal comando /admin)."""
    _, default_chat_id = _risolvi_credenziali_telegram(cfg)
    target_chat = chat_id or default_chat_id
    payload = {
        "chat_id": target_chat,
        "text": "🛠️ Menu Sviluppatore Bot Pulichimica:",
        "reply_markup": MENU_ADMIN_KEYBOARD,
    }
    ok = _telegram_api("sendMessage", payload, cfg)
    if ok:
        logger.info("[UI TELEGRAM] Menu Sviluppatore (/admin) inviato.")
    return ok


# --------------------------------------------------------------------------------------
# 3. HEALTH CHECK COMBINATO E CONTROLLO MANUALE PACKLINK
# --------------------------------------------------------------------------------------
def esegui_health_check_api(cfg: dict = None) -> str:
    """Combina il risultato dell'Health Check di Shopify e Packlink PRO."""
    stato_shopify = check_salute_shopify(cfg)
    stato_packlink = check_salute_packlink(cfg)

    # TODO [FUTURO - MODULO EBAY]: Inserire qui il blocco Health Check per eBay API

    return (
        "🛠 **Report Health Check API**\n\n"
        f"{stato_shopify}\n"
        f"{stato_packlink}"
    )


def avvia_controllo_packlink_manuale(cfg: dict = None, chat_id: str = None):
    """
    Gestisce la pressione del tasto '🚚 Monitoraggio Packlink' su Telegram:
      1. Risponde subito '⏳ Controllo spedizioni Packlink in corso...'
      2. Esegue controlla_spedizioni_packlink()
      3. Se l'elenco anomalie è vuoto, conferma che tutte le spedizioni sono in regola.
    """
    if cfg is None:
        cfg = load_env()
    _, default_chat_id = _risolvi_credenziali_telegram(cfg)
    target_chat = chat_id or default_chat_id

    _telegram_api(
        "sendMessage",
        {"chat_id": target_chat, "text": "⏳ Controllo spedizioni Packlink in corso..."},
        cfg,
    )

    anomalie = controlla_spedizioni_packlink(
        cfg=cfg,
        notifica_fn=lambda msg: bocca_telegram(msg, cfg=cfg),
    )

    if anomalie is None:
        _telegram_api(
            "sendMessage",
            {"chat_id": target_chat, "text": "⚠️ Errore di connessione alle API di Packlink PRO."},
            cfg,
        )
    elif len(anomalie) == 0:
        _telegram_api(
            "sendMessage",
            {"chat_id": target_chat, "text": "✅ Tutte le spedizioni sono in regola. Nessuna anomalia riscontrata."},
            cfg,
        )


# --------------------------------------------------------------------------------------
# 4. GESTIONE DOCUMENTI CARICO MAGAZZINO (DOWNLOAD & ELABORAZIONE CON GEMINI)
# --------------------------------------------------------------------------------------
def processa_documento_carico_telegram(file_id: str, file_name: str, chat_id: str, cfg: dict):
    """
    Scarica la foto o il documento PDF/immagine inviato dall'utente su Telegram,
    lo salva in una cartella temporanea locale, lo analizza tramite ai_vision (Gemini),
    invia il report dettagliato in chat ed elimina infine il file temporaneo.
    """
    bot_token, _ = _risolvi_credenziali_telegram(cfg)
    temp_dir = BASE_DIR / "temp_uploads"
    temp_dir.mkdir(parents=True, exist_ok=True)

    _telegram_api(
        "sendMessage",
        {"chat_id": chat_id, "text": "Sto analizzando il documento con Gemini..."},
        cfg,
    )

    temp_path = None
    try:
        # a) Scarica e salva in cartella temporanea
        info_res = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getFile",
            params={"file_id": file_id},
            timeout=15,
        )
        if info_res.status_code != 200 or not info_res.json().get("ok"):
            _telegram_api(
                "sendMessage",
                {"chat_id": chat_id, "text": "⚠️ Impossibile recuperare i metadati del file da Telegram."},
                cfg,
            )
            return

        file_path_remoto = info_res.json()["result"]["file_path"]
        ext = Path(file_path_remoto).suffix or Path(file_name).suffix or ".jpg"
        temp_path = temp_dir / f"carico_{int(time.time())}_{file_id[:8]}{ext}"

        # Download effettivo del file
        dl_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path_remoto}"
        dl_res = requests.get(dl_url, timeout=35)
        if dl_res.status_code != 200 or not dl_res.content:
            _telegram_api(
                "sendMessage",
                {"chat_id": chat_id, "text": "⚠️ Errore durante il download del documento dai server Telegram."},
                cfg,
            )
            return

        temp_path.write_bytes(dl_res.content)
        logger.info(f"[CARICO MAGAZZINO] Documento salvato temporaneamente in {temp_path.name} ({len(dl_res.content)} bytes).")

        # c) Passa il percorso del file alla funzione di ai_vision.py
        risultato_ai = asyncio.run(analizza_documento_carico(temp_path, cfg=cfg))

        # d) Restituisce la risposta testuale formattata all'utente in chat
        invia_messaggio_lungo(risultato_ai, chat_id=chat_id, cfg=cfg)

    except Exception as e:
        logger.exception(f"[CARICO MAGAZZINO] Errore durante l'elaborazione del documento: {e}")
        _telegram_api(
            "sendMessage",
            {"chat_id": chat_id, "text": f"⚠️ Si è verificato un errore durante l'elaborazione del file:\n`{e}`"},
            cfg,
        )
    finally:
        # e) Elimina il file temporaneo per non intasare il NAS
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink(missing_ok=True)
                logger.info(f"[CARICO MAGAZZINO] File temporaneo {temp_path.name} eliminato con successo.")
            except Exception as e:
                logger.warning(f"[CARICO MAGAZZINO] Impossibile eliminare {temp_path.name}: {e}")


# --------------------------------------------------------------------------------------
# 5. AUTO-UPDATE MODULARE & RIAVVIO SU 'main.py'
# --------------------------------------------------------------------------------------
def applica_aggiornamenti_e_riavvia(modifiche: dict, cfg: dict = None, sorgente: str = "OTA"):
    """
    Valida la sintassi Python di tutti i moduli aggiornati, crea backup '.bak',
    sovrascrive i file modificati e riavvia il processo puntando al nuovo
    entry-point 'main.py' (sostituendo il processo corrente tramite os.execv).
    """
    if not modifiche:
        return

    with RESTART_LOCK:
        if stato_sistema["restart_in_progress"]:
            return
        stato_sistema["restart_in_progress"] = True

    logger.info(f"[OTA RESTART] Validazione aggiornamenti ({list(modifiche.keys())}) da: {sorgente}")

    # 1. Validazione sintattica preventiva su ciascun file prima della scrittura
    for nome_file, nuovo_codice in modifiche.items():
        try:
            compile(nuovo_codice, nome_file, "exec")
        except Exception as e:
            logger.error(f"[OTA RESTART] Errore di sintassi in '{nome_file}' ({e}). Aggiornamento annullato.")
            bocca_telegram(
                f"ERRORE OTA: Il file '{nome_file}' ricevuto da {sorgente} contiene errori di sintassi. Aggiornamento annullato.",
                cfg=cfg,
            )
            stato_sistema["restart_in_progress"] = False
            return

    # 2. Backup e scrittura dei nuovi file su disco
    try:
        for nome_file, nuovo_codice in modifiche.items():
            target_path = (BASE_DIR / nome_file).resolve()
            if target_path.exists():
                backup_path = target_path.with_suffix(".py.bak")
                backup_path.write_text(target_path.read_text(encoding="utf-8"), encoding="utf-8")
            target_path.write_text(nuovo_codice, encoding="utf-8")
            logger.info(f"[OTA RESTART] Modulo aggiornato: {target_path.name}")
    except Exception as e:
        logger.error(f"[OTA RESTART] Errore scrittura su disco: {str(e)}")
        bocca_telegram(f"ERRORE OTA: Impossibile sovrascrivere i file locali: {str(e)}", cfg=cfg)
        stato_sistema["restart_in_progress"] = False
        return

    # 3. Notifica Telegram e riavvio pulito sul nuovo entry-point main.py
    bocca_telegram("Aggiornamento ricevuto. Installazione e riavvio in corso...", cfg=cfg)
    time.sleep(1.5)

    entry_point = str(ENTRY_POINT_FILE.resolve())
    logger.info(f"[OTA RESTART] Riavvio autonomo del processo su entry-point '{ENTRY_POINT_FILE.name}' (os.execv)...")
    sys.stdout.flush()
    sys.stderr.flush()

    for h in list(logger.handlers):
        h.flush()
        h.close()

    python_bin = sys.executable
    args = [python_bin, entry_point] + sys.argv[1:]
    os.execv(python_bin, args)


def cerca_aggiornamenti(cfg: dict = None) -> bool:
    """
    Controllo aggiornamenti (notturno alle ore 03:00 o forzato da /admin) su tutti
    i moduli dell'architettura. Se rileva modifiche su GitHub, aggiorna e riavvia main.py.
    """
    logger.info(f"[OTA GITHUB] Verifica aggiornamenti modulari su: {GITHUB_RAW_BASE_URL}")
    modifiche_rilevate = {}
    ts = int(time.time())

    try:
        for nome_modulo in MODULI_PROGETTO:
            raw_url = f"{GITHUB_RAW_BASE_URL}/{nome_modulo}?_ts={ts}"
            response = requests.get(raw_url, timeout=15)
            if response.status_code != 200:
                logger.warning(
                    f"[OTA GITHUB] Modulo '{nome_modulo}' non raggiungibile su GitHub (HTTP {response.status_code})."
                )
                continue

            remote_code = response.text
            if not remote_code or len(remote_code.strip()) < 20:
                continue

            local_path = BASE_DIR / nome_modulo
            local_code = local_path.read_text(encoding="utf-8") if local_path.exists() else ""

            if remote_code.replace("\r\n", "\n").strip() != local_code.replace("\r\n", "\n").strip():
                logger.info(f"[OTA GITHUB] Rilevata nuova versione per '{nome_modulo}'.")
                modifiche_rilevate[nome_modulo] = remote_code

        if modifiche_rilevate:
            applica_aggiornamenti_e_riavvia(modifiche_rilevate, cfg=cfg, sorgente="GitHub Web OTA")
            return True

        logger.info("[OTA GITHUB] Tutti i moduli locali sono allineati a GitHub (nessun aggiornamento disponibile).")
        return False

    except Exception as e:
        logger.warning(f"[OTA GITHUB] Impossibile completare verifica GitHub: {str(e)}")
        return False


# --------------------------------------------------------------------------------------
# 6. CALLBACK HANDLER & LISTENER TELEGRAM 24/7
# --------------------------------------------------------------------------------------
def gestisci_callback_query(callback_query: dict, cfg: dict, authorized_chat_id: str):
    """Gestisce tutte le callback_query dei sottomenu /menu e /admin."""
    callback_id = callback_query.get("id")
    data = callback_query.get("data", "")
    sender_id = str(callback_query.get("from", {}).get("id", "")).strip()
    message_obj = callback_query.get("message") or {}
    chat_id = str(message_obj.get("chat", {}).get("id", sender_id)).strip()
    message_id = message_obj.get("message_id")

    autorizzato = sender_id == authorized_chat_id
    _telegram_api("answerCallbackQuery", {"callback_query_id": callback_id}, cfg, timeout=8)

    if not autorizzato:
        logger.warning(f"[UI TELEGRAM] Callback '{data}' da ID non autorizzato '{sender_id}'. Ignorata.")
        return

    logger.info(f"[UI TELEGRAM] Azione pulsante ricevuta: '{data}'")

    if data == "cmd_ultimi_ordini":
        report_ordini = recupera_ultimi_tre_ordini(cfg)
        _telegram_api("sendMessage", {"chat_id": chat_id, "text": report_ordini}, cfg)

    elif data == "cmd_health_check":
        esito_health = esegui_health_check_api(cfg)
        _telegram_api("sendMessage", {"chat_id": chat_id, "text": esito_health}, cfg)

    elif data == "cmd_force_ota":
        _telegram_api(
            "sendMessage",
            {"chat_id": chat_id, "text": "🔄 Controllo forzato aggiornamenti su GitHub in corso..."},
            cfg,
        )
        aggiornato = cerca_aggiornamenti(cfg)
        if not aggiornato:
            _telegram_api(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "✅ Nessun nuovo aggiornamento disponibile su GitHub (codice già allineato o remoto non raggiungibile).",
                },
                cfg,
            )

    elif data == "cmd_cerca_prodotto":
        stato_utente[chat_id] = "attesa_ricerca"
        logger.info(f"[STATO CHAT] stato_utente['{chat_id}'] impostato su 'attesa_ricerca'.")
        _telegram_api(
            "sendMessage",
            {"chat_id": chat_id, "text": "Scrivi il nome del prodotto (o una parte di esso) da cercare:"},
            cfg,
        )

    elif data == "cmd_packlink":
        stato_utente[chat_id] = None
        avvia_controllo_packlink_manuale(cfg=cfg, chat_id=chat_id)

    elif data == "cmd_carico_magazzino":
        stato_utente[chat_id] = "attesa_carico"
        logger.info(f"[STATO CHAT] stato_utente['{chat_id}'] impostato su 'attesa_carico'.")
        _telegram_api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    "📥 **Carico Magazzino con AI (Google Gemini)**\n\n"
                    "Invia una **foto** o un **file PDF** della bolla di consegna (DDT) o fattura da caricare.\n\n"
                    "_(Per annullare in qualsiasi momento digita /menu o /annulla)_"
                ),
            },
            cfg,
        )

    elif data == "cmd_chiudi":
        stato_utente[chat_id] = None
        if message_id is not None:
            _telegram_api(
                "deleteMessage",
                {"chat_id": chat_id, "message_id": message_id},
                cfg,
            )
            logger.info(f"[UI TELEGRAM] Menu chiuso (deleteMessage message_id={message_id}).")

    else:
        logger.warning(f"[UI TELEGRAM] callback_data non riconosciuto: '{data}'")
        _telegram_api(
            "sendMessage",
            {"chat_id": chat_id, "text": "⚠️ Pulsante obsoleto o non riconosciuto. Usa /menu o /admin."},
            cfg,
        )


def orecchie_telegram_worker(cfg: dict, stop_event: threading.Event):
    """
    Thread daemon attivo 24 ore su 24 per gestire:
      - comandi (/menu, /start, /admin, /annulla)
      - ricerca prodotti testuale (stato 'attesa_ricerca')
      - ricezione foto e PDF per carico magazzino AI (stato 'attesa_carico')
      - pulsanti inline e aggiornamenti OTA via file
    """
    bot_token, authorized_chat_id = _risolvi_credenziali_telegram(cfg)

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
                        gestisci_callback_query(callback_query, cfg, authorized_chat_id)
                    except Exception as e:
                        logger.error(f"[ORECCHIE TELEGRAM] Errore in gestisci_callback_query: {str(e)}")
                    continue

                msg = update.get("message") or update.get("edited_message")
                if not msg:
                    continue

                sender_id = str(msg.get("from", {}).get("id", "")).strip()
                chat_id = str(msg.get("chat", {}).get("id", "")).strip()

                if sender_id != authorized_chat_id and chat_id != authorized_chat_id:
                    logger.warning(f"[ORECCHIE TELEGRAM] Interazione ignorata da ID non autorizzato '{sender_id or chat_id}'.")
                    continue

                in_attesa_carico = (stato_utente.get(chat_id) == "attesa_carico")

                # 2. Gestione Foto inviata dall'utente
                photo_list = msg.get("photo")
                if photo_list:
                    if in_attesa_carico:
                        stato_utente[chat_id] = None
                        best_photo = photo_list[-1]
                        file_id = best_photo.get("file_id")
                        logger.info(f"[ORECCHIE TELEGRAM] Ricevuta foto per carico magazzino (file_id={file_id}).")
                        processa_documento_carico_telegram(file_id, "bolla.jpg", chat_id, cfg)
                        continue
                    else:
                        logger.info("[ORECCHIE TELEGRAM] Foto ricevuta fuori dal flusso di carico magazzino.")
                        _telegram_api(
                            "sendMessage",
                            {
                                "chat_id": chat_id,
                                "text": "ℹ️ Foto ricevuta. Per avviare l'analisi del carico merci premi prima il tasto **📥 Carico Magazzino** nel /menu.",
                            },
                            cfg,
                        )
                        continue

                # 3. Gestione Documento (PDF, Immagine come file o Modulo OTA)
                doc = msg.get("document")
                if doc:
                    file_name = doc.get("file_name", "")
                    file_id = doc.get("file_id")

                    if in_attesa_carico:
                        stato_utente[chat_id] = None
                        logger.info(f"[ORECCHIE TELEGRAM] Ricevuto documento '{file_name}' per carico magazzino.")
                        processa_documento_carico_telegram(file_id, file_name, chat_id, cfg)
                        continue

                    # Se non è in attesa carico ed è un file di codice del progetto, gestisce OTA
                    if file_name in MODULI_PROGETTO:
                        logger.info(f"[ORECCHIE TELEGRAM] Ricevuto modulo OTA '{file_name}' via Telegram (file_id={file_id}).")
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
                                applica_aggiornamenti_e_riavvia(
                                    {file_name: dl_res.text},
                                    cfg=cfg,
                                    sorgente=f"Telegram Document OTA ({file_name})",
                                )
                                return
                        bocca_telegram("ERRORE: Impossibile scaricare il file di aggiornamento da Telegram.", cfg=cfg)
                        continue

                    # Documento generico ricevuto senza essere in attesa carico
                    _telegram_api(
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": f"ℹ️ Ricevuto '{file_name}'. Per analizzarlo con Gemini come carico magazzino, premi prima **📥 Carico Magazzino** dal /menu.",
                        },
                        cfg,
                    )
                    continue

                # 4. Gestione Messaggi Testuali e Comandi
                if msg.get("text"):
                    testo = msg.get("text").strip()
                    logger.info(f"[ORECCHIE TELEGRAM] Messaggio ricevuto: \"{testo}\"")
                    comando = testo.lower().split("@")[0]

                    if comando in ["/annulla", "annulla", "/cancel"]:
                        stato_utente[chat_id] = None
                        _telegram_api("sendMessage", {"chat_id": chat_id, "text": "✅ Operazione annullata."}, cfg)
                    elif comando in ["/menu", "/start"]:
                        stato_utente[chat_id] = None
                        invia_menu_operativo(cfg, chat_id=chat_id)
                    elif comando == "/admin":
                        stato_utente[chat_id] = None
                        invia_menu_admin(cfg, chat_id=chat_id)
                    elif testo == "🚚 Monitoraggio Packlink" or comando == "/packlink":
                        stato_utente[chat_id] = None
                        avvia_controllo_packlink_manuale(cfg=cfg, chat_id=chat_id)
                    elif testo == "📥 Carico Magazzino" or comando == "/carico":
                        stato_utente[chat_id] = "attesa_carico"
                        _telegram_api(
                            "sendMessage",
                            {
                                "chat_id": chat_id,
                                "text": (
                                    "📥 **Carico Magazzino con AI (Google Gemini)**\n\n"
                                    "Invia ora una **foto** o un file **PDF** della bolla/fattura (oppure digita /annulla)."
                                ),
                            },
                            cfg,
                        )
                    elif testo == "📦 Ricerca Prodotto":
                        stato_utente[chat_id] = "attesa_ricerca"
                        _telegram_api(
                            "sendMessage",
                            {
                                "chat_id": chat_id,
                                "text": "🔍 Scrivi il nome del prodotto (o una parte di esso) da cercare:",
                            },
                            cfg,
                        )
                    elif testo == "📋 Ultimi 3 Ordini":
                        stato_utente[chat_id] = None
                        _telegram_api(
                            "sendMessage",
                            {"chat_id": chat_id, "text": "⏳ Recupero ultimi 3 ordini da Shopify..."},
                            cfg,
                        )
                        report_ordini = recupera_ultimi_tre_ordini(cfg)
                        invia_messaggio_lungo(report_ordini, chat_id=chat_id, cfg=cfg)
                    elif stato_utente.get(chat_id) == "attesa_ricerca":
                        stato_utente[chat_id] = None
                        query_ricerca = testo
                        logger.info(f"[RICERCA PRODOTTO] Esecuzione ricerca per query: '{query_ricerca}'")
                        messaggio_risultati = cerca_prodotti_shopify(query_ricerca, cfg)
                        _telegram_api("sendMessage", {"chat_id": chat_id, "text": messaggio_risultati}, cfg)
                    elif in_attesa_carico:
                        _telegram_api(
                            "sendMessage",
                            {
                                "chat_id": chat_id,
                                "text": (
                                    "ℹ️ Sono in attesa di un'**immagine** o di un file **PDF** della bolla o fattura.\n"
                                    "Allega il file in chat oppure digita /annulla per uscire."
                                ),
                            },
                            cfg,
                        )

        except requests.exceptions.RequestException:
            time.sleep(3)
        except Exception as e:
            logger.error(f"[ORECCHIE TELEGRAM] Errore nel loop listener: {str(e)}")
            time.sleep(3)


def avvia_thread_orecchie(cfg: dict, stop_event: threading.Event) -> threading.Thread:
    """Crea e avvia il thread daemon di ascolto Telegram."""
    t = threading.Thread(
        target=orecchie_telegram_worker,
        args=(cfg, stop_event),
        name="BotPulichimicaOrecchieTelegram",
        daemon=True,
    )
    t.start()
    return t


# --------------------------------------------------------------------------------------
# 7. GRACEFUL SHUTDOWN SINCRONO E BLOCCANTE (REQUESTS POST DIRETTA)
# --------------------------------------------------------------------------------------
def invia_spegnimento_sincrono(cfg: dict = None, timeout: int = 5):
    """
    Invia in modo sincrono e bloccante l'avviso di disconnessione con una POST HTTP
    diretta alle API di Telegram tramite requests.post (senza passare per thread o wrapper),
    garantendo un singolo invio prima della chiusura del processo.
    """
    with SHUTDOWN_LOCK:
        if stato_sistema["shutdown_notified"] or stato_sistema["restart_in_progress"]:
            return
        stato_sistema["shutdown_notified"] = True

    token_del_bot, id_amministratore = _risolvi_credenziali_telegram(cfg)
    if not token_del_bot or not id_amministratore:
        return

    url = f"https://api.telegram.org/bot{token_del_bot}/sendMessage"
    payload = {
        "chat_id": id_amministratore,
        "text": "🔴 Bot Pulichimica disconnesso. Monitoraggio interrotto.",
    }
    try:
        requests.post(url, data=payload, timeout=timeout)
        logger.info("[SHUTDOWN] Avviso di spegnimento consegnato a Telegram.")
    except Exception as e:
        logger.error(f"[SHUTDOWN] Errore durante l'invio dell'avviso di spegnimento: {e}")


def registra_win32_close_handler(cfg: dict, stop_event: threading.Event):
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
            if ctrl_type in (2, 5, 6):
                stop_event.set()
                invia_spegnimento_sincrono(cfg=cfg, timeout=4)
                return True
            return False

        _WIN_CTRL_HANDLER_REF = HandlerRoutine(_ctrl_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_WIN_CTRL_HANDLER_REF, True)
    except Exception as e:
        logger.debug(f"[SHUTDOWN] Registrazione handler console Windows ignorata: {e}")
