"""
========================================================================================
BOT PULICHIMICA - Modulo Integrazione Packlink PRO (api_packlink.py)
========================================================================================
Racchiude le chiamate alle API di Packlink PRO:
  - Filtraggio e notifica delle spedizioni in stato di anomalia
  - Health Check API Packlink PRO
Importa esclusivamente 'config' per garantire assenza totale di dipendenze circolari.
========================================================================================
"""

import os
import requests

from config import (
    PACKLINK_API_KEY,
    PACKLINK_SHIPMENTS_URL,
    STATI_ANOMALIA_PACKLINK,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    USER_AGENT,
    load_env,
    logger,
)


def _risolvi_chiave_packlink(cfg: dict = None) -> str:
    """Recupera la chiave Packlink PRO dal dizionario di configurazione o dalle variabili globali."""
    return (
        os.getenv("PACKLINK_API_KEY")
        or (cfg.get("PACKLINK_API_KEY") if cfg else None)
        or PACKLINK_API_KEY
        or ""
    ).strip()


def _invia_notifica_fallback(messaggio: str, cfg: dict = None) -> bool:
    """Fallback HTTP diretto verso Telegram qualora non venga iniettata notifica_fn."""
    bot_token = ((cfg.get("TELEGRAM_BOT_TOKEN") if cfg else None) or os.getenv("TELEGRAM_BOT_TOKEN") or TELEGRAM_BOT_TOKEN).strip()
    chat_id = ((cfg.get("TELEGRAM_CHAT_ID") if cfg else None) or os.getenv("TELEGRAM_CHAT_ID") or TELEGRAM_CHAT_ID).strip()
    if not bot_token or not chat_id:
        return False
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": messaggio},
            timeout=12,
        )
        return res.status_code == 200
    except Exception as e:
        logger.error(f"[PACKLINK PRO] Errore invio notifica fallback: {e}")
        return False


def check_salute_packlink(cfg: dict = None) -> str:
    """
    Esegue una GET all'endpoint base delle spedizioni Packlink PRO.
    Restituisce '🟢 Packlink: Connesso' su HTTP 200, oppure il dettaglio errore specifico.
    """
    packlink_key = _risolvi_chiave_packlink(cfg)
    if not packlink_key:
        return "🔴 Packlink: DISCONNESSO\nDettaglio: PACKLINK_API_KEY mancante nel .env"

    packlink_headers = {
        "Authorization": packlink_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
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
            return "🟢 Packlink: Connesso"

        body_err = (res_packlink.text or "").strip()[:300]
        errore = f"HTTP {res_packlink.status_code} - {body_err}" if body_err else f"HTTP {res_packlink.status_code}"
        logger.warning(f"[HEALTH CHECK] Packlink DISCONNESSO: {errore}")
        return f"🔴 Packlink: DISCONNESSO\nDettaglio: {errore}"
    except Exception as e:
        errore = str(e)
        logger.error(f"[HEALTH CHECK] Eccezione Packlink API: {errore}")
        return f"🔴 Packlink: DISCONNESSO\nDettaglio: {errore}"


def controlla_spedizioni_packlink(cfg: dict = None, notifica_fn=None) -> list:
    """
    Funzione autonoma di controllo spedizioni Packlink PRO:
      - Esegue la chiamata GET alle API di Packlink PRO
      - Isola le spedizioni in stato di anomalia (INCIDENT, EXCEPTION, RETURNED_TO_SENDER, DELIVERY_FAILED)
      - Invia su Telegram un messaggio di alert per ciascuna anomalia rilevata
      - Restituisce la lista delle anomalie trovate ([] se tutto regolare, None se errore API)
    """
    if cfg is None:
        cfg = load_env()

    packlink_key = _risolvi_chiave_packlink(cfg)
    if not packlink_key:
        logger.warning("[PACKLINK PRO] PACKLINK_API_KEY mancante nel .env. Controllo saltato.")
        return None

    invia_notifica = notifica_fn or (lambda msg: _invia_notifica_fallback(msg, cfg))

    headers_bearer = {
        "Authorization": f"Bearer {packlink_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

    try:
        response = requests.get(PACKLINK_SHIPMENTS_URL, headers=headers_bearer, timeout=15)

        # Fallback automatico per Packlink PRO API v1 (accetta il token diretto in Authorization)
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
            invia_notifica(messaggio_anomalia)

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
