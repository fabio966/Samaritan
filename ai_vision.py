"""
========================================================================================
BOT PULICHIMICA - Modulo Vision AI con Google Gemini (ai_vision.py)
========================================================================================
Gestisce l'analisi visiva di bolle di consegna (DDT), fatture e documenti di carico merci.
Utilizza 'google.generativeai' con il modello 'gemini-1.5-flash' (e fallback automatico
a 'gemini-3.8-flash' qualora la versione 1.5 risulti ritirata per le nuove chiavi API).
Restituisce ESCLUSIVAMENTE una stringa JSON pura rappresentante un array di oggetti:
[{"sku": "...", "nome_prodotto": "...", "quantita_aggiunta": <int>}, ...]
Importa unicamente 'config' per evitare dipendenze circolari.
========================================================================================
"""

import os
import re
import json
import asyncio
import mimetypes
import warnings
from pathlib import Path

# Soppressione avvisi informativi di obsolescenza SDK per mantenere i log puliti
warnings.filterwarnings("ignore", category=FutureWarning)

import google.generativeai as genai
from google.api_core.exceptions import NotFound, GoogleAPIError

from config import (
    GEMINI_API_KEY,
    logger,
)

# Configurazione globale dell'API Key da config.py
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# Modelli Gemini da utilizzare (1.5 Flash come richiesto, con fallback resiliente)
MODELLO_PRINCIPALE = "gemini-1.5-flash"
MODELLO_FALLBACK = "gemini-3.8-flash"

PROMPT_CARICO_MAGAZZINO = """Analizza con la massima accuratezza l'immagine o il documento allegato (Bolla di Consegna / DDT / Fattura / Documento di Trasporto).
Il tuo obiettivo è estrarre tutti i prodotti ricevuti per registrare il carico merci a magazzino su Shopify.

Devi restituire ESCLUSIVAMENTE un array JSON puro, senza testo introduttivo o conclusivo, senza commenti e senza formattazione markdown (NON usare i delimitatori ```json o ```).

Ogni elemento dell'array deve essere un oggetto JSON con le seguenti chiavi:
- "sku": codice articolo o SKU del fornitore/prodotto se leggibile nel documento (se assente o illeggibile, lascia una stringa vuota "")
- "nome_prodotto": denominazione o descrizione completa del prodotto (obbligatorio, es. "MA-FRA 8 PLUS 25KG")
- "quantita_aggiunta": numero intero di pezzi, colli o flaconi caricati (deve essere un valore intero, es. 20)

Se il documento non è leggibile, è sfocato o non contiene articoli di magazzino, restituisci semplicemente un array vuoto: []

Esempio esatto di output:
[
  {"sku": "P0210", "nome_prodotto": "MA-FRA 8 PLUS 25KG", "quantita_aggiunta": 10},
  {"sku": "", "nome_prodotto": "MA-FRA Fast Cleaner 500ml", "quantita_aggiunta": 24}
]"""


def _determina_mime_type(file_path: Path) -> str:
    """Riconosce il MIME type dal percorso del file o dalla sua estensione."""
    mime, _ = mimetypes.guess_type(str(file_path))
    if mime:
        return mime

    ext = file_path.suffix.lower()
    mapping = {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }
    return mapping.get(ext, "application/octet-stream")


def _pulisci_json_risposta(testo: str) -> str:
    """
    Rimuove eventuali blocchi markdown (```json ... ```) e testo spazzola,
    estraendo la sottostringa JSON compresa tra la prima '[' e l'ultima ']'.
    Verifica che il JSON sia formalmente valido prima di restituirlo.
    """
    if not testo:
        return "[]"

    pulito = testo.strip()

    # Rimozione eventuali fence markdown
    pulito = re.sub(r"^```(?:json)?\s*", "", pulito, flags=re.IGNORECASE)
    pulito = re.sub(r"\s*```$", "", pulito)
    pulito = pulito.strip()

    # Isolamento dell'array JSON racchiuso tra parentesi quadre
    start = pulito.find("[")
    end = pulito.rfind("]")
    if start != -1 and end != -1 and end >= start:
        pulito = pulito[start : end + 1].strip()

    # Validazione formale del JSON
    try:
        parsed = json.loads(pulito)
        if isinstance(parsed, list):
            return json.dumps(parsed, ensure_ascii=False)
        elif isinstance(parsed, dict):
            # Se per errore ha restituito un singolo oggetto o un wrapper {"prodotti": [...]}
            for val in parsed.values():
                if isinstance(val, list):
                    return json.dumps(val, ensure_ascii=False)
            return json.dumps([parsed], ensure_ascii=False)
    except Exception as e:
        logger.warning(f"[AI VISION] Parsing JSON fallito ({e}), raw output: {testo[:200]}")

    return "[]"


async def analizza_documento_carico(file_path: str | Path, cfg: dict = None, api_key: str = None) -> str:
    """
    Funzione asincrona che riceve il percorso di un file locale (immagine o PDF).
    Carica i dati del file, invoca l'API di Gemini richiedendo un array JSON puro
    con i prodotti da caricare a magazzino, e restituisce la stringa JSON validata.
    """
    path_obj = Path(file_path).resolve()
    if not path_obj.exists() or not path_obj.is_file():
        logger.error(f"[AI VISION] File non trovato: {path_obj}")
        return "[]"

    # Risoluzione chiave API Gemini
    chiave = (
        api_key
        or (cfg.get("GEMINI_API_KEY") if cfg else None)
        or os.getenv("GEMINI_API_KEY")
        or GEMINI_API_KEY
        or ""
    ).strip()

    if not chiave:
        logger.warning("[AI VISION] GEMINI_API_KEY non configurata nel .env.")
        return "[]"

    genai.configure(api_key=chiave)
    mime_type = _determina_mime_type(path_obj)
    logger.info(f"[AI VISION] Avvio analisi JSON su '{path_obj.name}' (MIME: {mime_type})...")

    try:
        file_bytes = path_obj.read_bytes()
        if not file_bytes:
            logger.warning(f"[AI VISION] File '{path_obj.name}' vuoto (0 byte).")
            return "[]"

        parte_documento = {
            "mime_type": mime_type,
            "data": file_bytes,
        }
        contenuti = [PROMPT_CARICO_MAGAZZINO, parte_documento]

        # Configurazione per forzare l'output JSON nativo
        generation_config = {"response_mime_type": "application/json"}

        # 1. Tentativo con il modello richiesto: gemini-1.5-flash
        try:
            logger.info(f"[AI VISION] Invocazione modello '{MODELLO_PRINCIPALE}' con response_mime_type=application/json...")
            modello = genai.GenerativeModel(MODELLO_PRINCIPALE, generation_config=generation_config)
            response = await modello.generate_content_async(contenuti)
        except (NotFound, GoogleAPIError, Exception) as mod_err:
            msg_err = str(mod_err).lower()
            if "not found" in msg_err or "no longer available" in msg_err or "404" in msg_err:
                logger.warning(
                    f"[AI VISION] '{MODELLO_PRINCIPALE}' non attivo o ritirato sull'endpoint. "
                    f"Esecuzione automatica con '{MODELLO_FALLBACK}'..."
                )
                modello = genai.GenerativeModel(MODELLO_FALLBACK, generation_config=generation_config)
                response = await modello.generate_content_async(contenuti)
            else:
                raise mod_err

        testo_grezzo = (response.text or "").strip()
        json_pulito = _pulisci_json_risposta(testo_grezzo)
        logger.info(f"[AI VISION] Estrazione JSON completata per '{path_obj.name}': {json_pulito[:120]}...")
        return json_pulito

    except Exception as e:
        logger.exception(f"[AI VISION] Errore durante l'elaborazione AI del documento: {e}")
        return "[]"


def analizza_documento_carico_sync(file_path: str | Path, cfg: dict = None, api_key: str = None) -> str:
    """Wrapper sincrono per invocare la funzione asincrona analizza_documento_carico()."""
    return asyncio.run(analizza_documento_carico(file_path=file_path, cfg=cfg, api_key=api_key))
