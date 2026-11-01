"""
========================================================================================
BOT PULICHIMICA - Modulo Vision AI con Google Gemini (ai_vision.py)
========================================================================================
Gestisce l'analisi visiva e documentale di bolle di consegna (DDT), fatture
e documenti di trasporto merci ricevuti come foto o PDF.
Utilizza la libreria 'google.generativeai' con il modello 'gemini-1.5-flash'
(con fallback automatico a 'gemini-3.8-flash' se 1.5-flash non è disponibile
sull'endpoint attivo).
Importa unicamente 'config' per evitare qualsiasi circular import.
========================================================================================
"""

import os
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

PROMPT_CARICO_MAGAZZINO = """Sei l'assistente AI per la gestione di magazzino di "Pulichimica".
Analizza l'immagine o il documento allegato (Bolla di Consegna / DDT / Fattura / Documento di Trasporto).

Estrai con la massima accuratezza tutti i dati utili per registrare il carico merci a magazzino, formattandoli in modo chiaro e leggibile per Telegram (usa testo formattato in stile markdown con elenchi puntati):

🏢 **Fornitore / Mittente**: (Ragione sociale fornitore, P.IVA/CF se presente)
📄 **Documento**: (Tipo: es. DDT o Fattura, Numero documento, Data documento)

📦 **Articoli Ricevuti**:
Per ciascun articolo presente nel documento, crea una voce con:
- **Descrizione**: Nome completo del prodotto / articolo
- **Quantità**: Numero di pezzi / colli / litri / kg (specifica sempre l'unità di misura)
- **Codice / SKU**: Codice articolo del fornitore (se indicato)
- **Prezzo Unitario / Sconto**: (se visibile, altrimenti 'Non specificato')

💶 **Riepilogo Economico**: (Totale imponibile / Totale documento, se indicati)
📝 **Note e Discrepanze**: (Eventuali lotti di produzione, date di scadenza, colli mancanti o anomalie visibili nel documento)

Se il documento non è leggibile, è sfocato o non sembra una bolla/fattura, segnalalo chiaramente all'inizio del messaggio."""


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


async def analizza_documento_carico(file_path: str | Path, cfg: dict = None, api_key: str = None) -> str:
    """
    Funzione asincrona che riceve il percorso di un file locale (immagine o PDF).
    Carica i dati del file, invoca l'API di Gemini 1.5 Flash con il prompt per
    estrarre i dati utili per il carico magazzino e restituisce il testo generato.
    """
    path_obj = Path(file_path).resolve()
    if not path_obj.exists() or not path_obj.is_file():
        logger.error(f"[AI VISION] File non trovato: {path_obj}")
        return f"⚠️ Errore: File non trovato in `{path_obj}`."

    # Risoluzione chiave API Gemini
    chiave = (
        api_key
        or (cfg.get("GEMINI_API_KEY") if cfg else None)
        or os.getenv("GEMINI_API_KEY")
        or GEMINI_API_KEY
        or ""
    ).strip()

    if not chiave:
        logger.warning("[AI VISION] GEMINI_API_KEY non configurata.")
        return (
            "⚠️ **Chiave API Gemini mancante!**\n\n"
            "Per utilizzare la lettura automatica di bolle e fatture con l'AI, "
            "inserisci la variabile `GEMINI_API_KEY` nel file `.env` del bot."
        )

    # Assicura che google.generativeai sia configurato con la chiave attiva
    genai.configure(api_key=chiave)

    mime_type = _determina_mime_type(path_obj)
    logger.info(f"[AI VISION] Avvio analisi asincrona su '{path_obj.name}' (MIME: {mime_type})...")

    try:
        # Lettura del file in memoria (bytes)
        file_bytes = path_obj.read_bytes()
        if not file_bytes:
            return "⚠️ Il file ricevuto è vuoto (0 byte)."

        # Creazione del payload multimodale compatibile sia per immagini che per documenti PDF
        parte_documento = {
            "mime_type": mime_type,
            "data": file_bytes,
        }
        contenuti = [PROMPT_CARICO_MAGAZZINO, parte_documento]

        # 1. Tentativo con il modello richiesto: gemini-1.5-flash
        try:
            logger.info(f"[AI VISION] Invocazione modello '{MODELLO_PRINCIPALE}'...")
            modello = genai.GenerativeModel(MODELLO_PRINCIPALE)
            response = await modello.generate_content_async(contenuti)
        except (NotFound, GoogleAPIError, Exception) as mod_err:
            msg_err = str(mod_err).lower()
            if "not found" in msg_err or "no longer available" in msg_err or "404" in msg_err:
                logger.warning(
                    f"[AI VISION] '{MODELLO_PRINCIPALE}' non attivo o ritirato sull'endpoint. "
                    f"Esecuzione automatica con '{MODELLO_FALLBACK}'..."
                )
                modello = genai.GenerativeModel(MODELLO_FALLBACK)
                response = await modello.generate_content_async(contenuti)
            else:
                raise mod_err

        testo_risposta = (response.text or "").strip()
        if not testo_risposta:
            return "⚠️ Il modello Gemini non ha restituito alcun testo per questo documento."

        logger.info(f"[AI VISION] Analisi completata con successo per '{path_obj.name}'.")
        return f"📥 **Carico Magazzino Estratto:**\n\n{testo_risposta}"

    except Exception as e:
        logger.exception(f"[AI VISION] Errore durante l'elaborazione AI: {str(e)}")
        return f"⚠️ **Errore durante l'elaborazione AI del documento:**\n`{str(e)}`"


def analizza_documento_carico_sync(file_path: str | Path, cfg: dict = None, api_key: str = None) -> str:
    """Wrapper sincrono per invocare la funzione asincrona analizza_documento_carico()."""
    return asyncio.run(analizza_documento_carico(file_path=file_path, cfg=cfg, api_key=api_key))
