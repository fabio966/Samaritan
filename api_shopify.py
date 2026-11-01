"""
========================================================================================
BOT PULICHIMICA - Modulo Integrazione Shopify (api_shopify.py)
========================================================================================
Racchiude tutte le chiamate REST verso Shopify:
  - Aggiornamento giacenze da array JSON (carico magazzino con Gemini)
  - Recupero ultimi 3 ordini
  - Ricerca prodotti e giacenze sull'intero catalogo
  - Health Check API Shopify (shop.json)
  - Monitoraggio automatico nuovi ordini (07:00-20:00) con persistenza su last_order_id.txt
Importa esclusivamente 'config' per garantire assenza totale di dipendenze circolari.
========================================================================================
"""

import os
import json
from pathlib import Path
from datetime import datetime

import requests

from config import (
    LAST_ORDER_ID_FILE,
    SHOPIFY_ACCESS_TOKEN,
    SHOPIFY_REST_VERSION,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    USER_AGENT,
    logger,
    normalizza_shop_domain,
)

# ID di fallback della sede principale del negozio Pulichimica
DEFAULT_PULICHIMICA_LOCATION_ID = 114360254847


def _risolvi_token_shopify(cfg: dict = None) -> str:
    """Recupera il token Shopify dal dizionario di configurazione o dalle variabili globali."""
    return (
        (cfg.get("SHOPIFY_ACCESS_TOKEN") if cfg else None)
        or os.getenv("SHOPIFY_ACCESS_TOKEN")
        or SHOPIFY_ACCESS_TOKEN
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
        logger.error(f"[ORDINI SHOPIFY] Errore invio notifica fallback: {e}")
        return False


def estrai_nome_cliente(order: dict) -> str:
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


def recupera_location_id_principale(shop: str, token: str, fallback_inventory_item_id: int = None) -> int:
    """
    Recupera il location_id principale del negozio con 4 livelli di sicurezza:
      1. Variabile d'ambiente SHOPIFY_LOCATION_ID se esplicitata
      2. Endpoint locations.json di Shopify (se il token ha scope read_locations)
      3. Endpoint inventory_levels.json tramite fallback_inventory_item_id (supportato con write_inventory)
      4. Fallback predefinito di magazzino Pulichimica (114360254847)
    """
    env_loc = os.getenv("SHOPIFY_LOCATION_ID", "").strip()
    if env_loc.isdigit():
        return int(env_loc)

    headers = {
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    # Tentativo via locations.json
    try:
        url_loc = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/locations.json"
        res = requests.get(url_loc, headers=headers, timeout=8)
        if res.status_code == 200:
            locations = res.json().get("locations", [])
            for loc in locations:
                if loc.get("active") is not False and loc.get("id"):
                    logger.info(f"[SHOPIFY LOCATION] Trovata location attiva: {loc.get('name')} (ID: {loc['id']})")
                    return int(loc["id"])
    except Exception as e:
        logger.debug(f"[SHOPIFY LOCATION] Fallita lettura locations.json: {e}")

    # Tentativo via inventory_levels con inventory_item_id noto
    if fallback_inventory_item_id:
        try:
            url_inv = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/inventory_levels.json"
            res = requests.get(url_inv, params={"inventory_item_ids": fallback_inventory_item_id}, headers=headers, timeout=8)
            if res.status_code == 200:
                inv_levels = res.json().get("inventory_levels", [])
                if inv_levels and inv_levels[0].get("location_id"):
                    loc_id = int(inv_levels[0]["location_id"])
                    logger.info(f"[SHOPIFY LOCATION] Trovata location da inventory_levels: {loc_id}")
                    return loc_id
        except Exception as e:
            logger.debug(f"[SHOPIFY LOCATION] Fallita lettura inventory_levels: {e}")

    logger.info(f"[SHOPIFY LOCATION] Utilizzo location predefinita: {DEFAULT_PULICHIMICA_LOCATION_ID}")
    return DEFAULT_PULICHIMICA_LOCATION_ID


def _costruisci_indice_catalogo(shop: str, token: str) -> tuple[dict, list]:
    """
    Scarica l'elenco dei prodotti con relative varianti paginando su cursori.
    Restituisce:
      - mappa_sku: dizionario {sku_lower: (product_title, variant_dict)}
      - lista_prodotti: lista di tuple (product_title_lower, product_title, variant_dict)
    """
    next_url = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/products.json?fields=id,title,variants&limit=250"
    headers = {
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    mappa_sku = {}
    lista_prodotti = []
    pagine_lette = 0

    while next_url and pagine_lette < 10:
        try:
            res = requests.get(next_url, headers=headers, timeout=15)
            if res.status_code != 200:
                logger.error(f"[INDICE CATALOGO] Errore HTTP {res.status_code} nel caricamento catalogo.")
                break

            data = res.json()
            products = data.get("products", [])
            pagine_lette += 1

            for p in products:
                p_title = (p.get("title") or "").strip()
                p_title_lower = p_title.lower()
                variants = p.get("variants") or []

                for v in variants:
                    sku = (v.get("sku") or "").strip().lower()
                    if sku:
                        mappa_sku[sku] = (p_title, v)

                    v_title = (v.get("title") or "").strip().lower()
                    full_name = f"{p_title_lower} {v_title}".strip()
                    lista_prodotti.append((p_title_lower, full_name, p_title, v))

            next_url = res.links.get("next", {}).get("url")

        except Exception as e:
            logger.error(f"[INDICE CATALOGO] Eccezione caricamento catalogo: {e}")
            break

    logger.info(f"[INDICE CATALOGO] Indicizzati {len(mappa_sku)} SKU e {len(lista_prodotti)} varianti ({pagine_lette} pagine).")
    return mappa_sku, lista_prodotti


def aggiorna_giacenza_da_json(dati_json, cfg: dict = None) -> list[str]:
    """
    Riceve l'array JSON prodotto da ai_vision.py (stringa o lista Python),
    individua ciascun articolo su Shopify tramite SKU o nome, recupera la location
    principale ed esegue la chiamata REST di incremento giacenza (inventory_levels/adjust.json).
    Restituisce una lista di stringhe con l'esito dettagliato per ciascuna voce.
    """
    # 1. Parsing sicuro dei dati in ingresso
    if isinstance(dati_json, str):
        try:
            prodotti = json.loads(dati_json)
        except Exception as e:
            logger.error(f"[CARICO MAGAZZINO] Parsing JSON in ingresso fallito: {e}")
            return [f"⚠️ Errore nel formato JSON restituito dall'IA: {e}"]
    elif isinstance(dati_json, list):
        prodotti = dati_json
    else:
        return ["⚠️ Formato dati non riconosciuto (attesa lista di articoli)."]

    if not prodotti or not isinstance(prodotti, list):
        return ["ℹ️ Nessun articolo utile per il carico a magazzino rilevato nel documento."]

    shop = normalizza_shop_domain(cfg)
    token = _risolvi_token_shopify(cfg)

    if not shop or not token:
        logger.error("[CARICO MAGAZZINO] Credenziali Shopify mancanti.")
        return ["⚠️ Errore: credenziali di connessione a Shopify non configurate."]

    # 2. Caricamento catalogo Shopify per risoluzione rapida SKU / Nome
    mappa_sku, lista_prodotti = _costruisci_indice_catalogo(shop, token)
    if not lista_prodotti:
        return ["⚠️ Impossibile accedere al catalogo prodotti di Shopify per verificare le giacenze."]

    location_id = None
    resoconto = []

    headers_adjust = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    # 3. Elaborazione e incremento di ciascun articolo
    for item in prodotti:
        if not isinstance(item, dict):
            continue

        sku_cercato = (item.get("sku") or "").strip()
        nome_cercato = (item.get("nome_prodotto") or "").strip()
        quantita_raw = item.get("quantita_aggiunta", 0)

        try:
            quantita_aggiunta = int(quantita_raw)
        except (ValueError, TypeError):
            quantita_aggiunta = 0

        riferimento_visivo = nome_cercato or sku_cercato or "Articolo non specificato"

        if quantita_aggiunta <= 0:
            resoconto.append(f"⚠️ Quantità non valida ({quantita_raw}) per: '{riferimento_visivo}'.")
            continue

        # Ricerca variante corrispondente: priorità a SKU, poi corrispondenza nome
        trovato_p_title = None
        trovato_variant = None

        if sku_cercato:
            sku_key = sku_cercato.lower()
            if sku_key in mappa_sku:
                trovato_p_title, trovato_variant = mappa_sku[sku_key]
            else:
                # Ricerca per sottostringa dello SKU
                for k, (p_t, v_d) in mappa_sku.items():
                    if sku_key in k or k in sku_key:
                        trovato_p_title, trovato_variant = p_t, v_d
                        break

        # Fallback ricerca per nome prodotto se non trovato per SKU
        if not trovato_variant and nome_cercato:
            nome_key = nome_cercato.lower()
            # 1. Match parziale sul titolo
            for p_title_lower, full_name, p_title, variant in lista_prodotti:
                if nome_key in p_title_lower or p_title_lower in nome_key or nome_key in full_name:
                    trovato_p_title = p_title
                    trovato_variant = variant
                    break

        if not trovato_variant:
            identificativo = f"SKU '{sku_cercato}'" if sku_cercato else f"'{nome_cercato}'"
            resoconto.append(f"⚠️ Prodotto non trovato su Shopify: {identificativo} (qtà: +{quantita_aggiunta}).")
            continue

        inventory_item_id = trovato_variant.get("inventory_item_id")
        if not inventory_item_id:
            resoconto.append(f"⚠️ Articolo '{trovato_p_title}' privo di inventory_item_id su Shopify.")
            continue

        # Risoluzione dinamica di location_id al primo match
        if location_id is None:
            location_id = recupera_location_id_principale(shop, token, fallback_inventory_item_id=inventory_item_id)

        # Chiamata REST di incremento giacenza: POST /admin/api/2024-01/inventory_levels/adjust.json
        payload_adjust = {
            "location_id": location_id,
            "inventory_item_id": inventory_item_id,
            "available_adjustment": quantita_aggiunta,
        }

        url_adjust = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/inventory_levels/adjust.json"

        try:
            res_adj = requests.post(url_adjust, json=payload_adjust, headers=headers_adjust, timeout=15)
            v_title = (trovato_variant.get("title") or "").strip()
            nome_completo = f"{trovato_p_title} ({v_title})" if v_title and v_title.lower() != "default title" else trovato_p_title

            if res_adj.status_code == 200:
                inv_data = res_adj.json().get("inventory_level", {})
                nuova_giacenza = inv_data.get("available", "N/D")
                logger.info(
                    f"[CARICO MAGAZZINO] Aggiornata giacenza '{nome_completo}': +{quantita_aggiunta} "
                    f"(Disponibile: {nuova_giacenza})."
                )
                resoconto.append(f"✅ Aggiunti {quantita_aggiunta} pezzi di {nome_completo} (Giacenza attuale: {nuova_giacenza} pz)")
            else:
                logger.error(f"[CARICO MAGAZZINO] Errore adjust per '{nome_completo}': HTTP {res_adj.status_code} - {res_adj.text}")
                resoconto.append(f"❌ Errore aggiornamento giacenza per '{nome_completo}' (HTTP {res_adj.status_code}).")

        except Exception as e:
            logger.exception(f"[CARICO MAGAZZINO] Eccezione adjust: {e}")
            resoconto.append(f"❌ Errore di connessione durante l'aggiornamento di '{trovato_p_title}': {str(e)}")

    return resoconto


def recupera_ultimi_tre_ordini(cfg: dict = None) -> str:
    """
    Interroga Shopify (GET /admin/api/2024-01/orders.json?status=any&limit=3)
    e restituisce un riepilogo leggibile degli ultimi 3 ordini ricevuti.
    """
    shop = normalizza_shop_domain(cfg)
    token = _risolvi_token_shopify(cfg)

    if not shop or not token:
        logger.error("[ULTIMI ORDINI] Credenziali Shopify mancanti nel .env.")
        return "⚠️ Errore di connessione a Shopify."

    endpoint_url = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/orders.json"
    params = {"status": "any", "limit": 3, "order": "created_at desc"}
    headers = {
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
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
            cliente = estrai_nome_cliente(order)
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


def check_salute_shopify(cfg: dict = None) -> str:
    """
    Esegue una GET all'endpoint base shop.json di Shopify.
    Restituisce '🟢 Shopify: Connesso' su HTTP 200, oppure il dettaglio errore specifico.
    """
    shop = normalizza_shop_domain(cfg)
    token = _risolvi_token_shopify(cfg)

    if not shop or not token:
        return "🔴 Shopify: DISCONNESSO\nDettaglio: Credenziali SHOPIFY_SHOP o SHOPIFY_ACCESS_TOKEN mancanti"

    shopify_url = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/shop.json"
    shopify_headers = {
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    try:
        res_shopify = requests.get(shopify_url, headers=shopify_headers, timeout=12)
        if res_shopify.status_code == 200:
            return "🟢 Shopify: Connesso"

        body_err = (res_shopify.text or "").strip()[:300]
        errore = f"HTTP {res_shopify.status_code} - {body_err}" if body_err else f"HTTP {res_shopify.status_code}"
        logger.warning(f"[HEALTH CHECK] Shopify DISCONNESSO: {errore}")
        return f"🔴 Shopify: DISCONNESSO\nDettaglio: {errore}"
    except Exception as e:
        errore = str(e)
        logger.error(f"[HEALTH CHECK] Eccezione Shopify API: {errore}")
        return f"🔴 Shopify: DISCONNESSO\nDettaglio: {errore}"


def cerca_prodotti_shopify(query_ricerca: str, cfg: dict = None, max_results: int = 30) -> str:
    """
    Interroga Shopify GET /admin/api/2024-01/products.json?fields=title,variants
    (seguendo la paginazione cursore per coprire l'intero catalogo),
    filtra in modo case-insensitive sul title del prodotto e restituisce nome e quantità.
    """
    query_pulita = (query_ricerca or "").strip()
    if not query_pulita:
        return "Nessun prodotto trovato per ''."

    shop = normalizza_shop_domain(cfg)
    token = _risolvi_token_shopify(cfg)

    if not shop or not token:
        logger.error("[RICERCA PRODOTTO] Credenziali Shopify mancanti nel .env.")
        return "⚠️ Errore di connessione a Shopify."

    next_url = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/products.json?fields=title,variants&limit=250"
    headers = {
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
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


def leggi_o_inizializza_last_order_id(cfg: dict = None, file_path: Path = LAST_ORDER_ID_FILE) -> str:
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

    shop = normalizza_shop_domain(cfg)
    token = _risolvi_token_shopify(cfg)
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


def controlla_nuovi_ordini_shopify(cfg: dict = None, notifica_fn=None) -> bool:
    """
    Interroga GET /admin/api/2024-01/orders.json?status=any&since_id={last_order_id}.
    Invia una notifica Telegram per ogni nuovo ordine (inclusi gli ordini notturni
    recuperati al primo ciclo delle 07:00) e aggiorna subito 'last_order_id.txt'.
    """
    shop = normalizza_shop_domain(cfg)
    token = _risolvi_token_shopify(cfg)

    if not shop or not token:
        logger.error("[ORDINI SHOPIFY] SHOPIFY_SHOP/SHOPIFY_STORE_URL o SHOPIFY_ACCESS_TOKEN mancanti.")
        return False

    invia_notifica = notifica_fn or (lambda msg: _invia_notifica_fallback(msg, cfg))
    last_order_id = leggi_o_inizializza_last_order_id(cfg)

    endpoint_url = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/orders.json"
    params = {
        "status": "any",
        "since_id": last_order_id,
        "limit": 250,
    }
    headers = {
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
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
            cliente_str = estrai_nome_cliente(order)
            total_price = order.get("total_price", "0.00")

            messaggio_ordine = (
                "🚨 **NUOVO ORDINE SHOPIFY!**\n"
                f"Numero: {order_number}\n"
                f"Cliente: {cliente_str}\n"
                f"Totale: {total_price} €"
            )

            if invia_notifica(messaggio_ordine):
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


def occhi_ebay(cfg: dict = None):
    """Predisposizione architetturale per futura integrazione ordini/giacenze eBay."""
    pass


# --------------------------------------------------------------------------------------
# 6. SOTTOSCORTA E REPORT ORDINI CON COSTO INVENTORY_ITEM
# --------------------------------------------------------------------------------------
def recupera_prodotti_sottoscorta(cfg: dict = None, min_qty: int = 1, max_qty: int = 5) -> list[dict]:
    """
    Scansiona il catalogo Shopify ed estrae i prodotti con giacenza compresa tra min_qty e max_qty (inclusi).
    Ignora categoricamente giacenze <= 0 (out of stock o non tracciate).
    """
    shop = normalizza_shop_domain(cfg)
    token = _risolvi_token_shopify(cfg)
    if not shop or not token:
        logger.error("[SOTTOSCORTA] Credenziali Shopify mancanti.")
        return []

    next_url = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/products.json?fields=id,title,variants&limit=250"
    headers = {
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    prodotti_sottoscorta = []
    pagine_lette = 0

    while next_url and pagine_lette < 10:
        try:
            res = requests.get(next_url, headers=headers, timeout=15)
            if res.status_code != 200:
                logger.error(f"[SOTTOSCORTA] Errore HTTP {res.status_code} da Shopify.")
                break

            data = res.json()
            products = data.get("products", [])
            pagine_lette += 1

            for p in products:
                p_title = (p.get("title") or "").strip()
                variants = p.get("variants") or []
                for v in variants:
                    qty = v.get("inventory_quantity")
                    if qty is None:
                        continue
                    try:
                        qty_int = int(qty)
                    except (ValueError, TypeError):
                        continue

                    # Filtro: giacenza compresa tra min_qty e max_qty (inclusi). Ignora <= 0!
                    if min_qty <= qty_int <= max_qty:
                        v_title = (v.get("title") or "").strip()
                        nome_completo = (
                            f"{p_title} ({v_title})"
                            if v_title and v_title.lower() != "default title"
                            else p_title
                        )
                        prodotti_sottoscorta.append({
                            "product_id": p.get("id"),
                            "variant_id": v.get("id"),
                            "inventory_item_id": v.get("inventory_item_id"),
                            "title": p_title,
                            "variant_title": v_title,
                            "full_name": nome_completo,
                            "sku": (v.get("sku") or "").strip(),
                            "inventory_quantity": qty_int,
                        })

            next_url = res.links.get("next", {}).get("url")

        except Exception as e:
            logger.error(f"[SOTTOSCORTA] Errore durante la scansione catalogo: {e}")
            break

    logger.info(f"[SOTTOSCORTA] Trovati {len(prodotti_sottoscorta)} articoli con giacenza tra {min_qty} e {max_qty} pz.")
    return prodotti_sottoscorta


def recupera_costi_inventory_items(inventory_item_ids: list, shop: str, token: str) -> dict:
    """
    Dato un elenco di inventory_item_id, interroga inventory_items.json a blocchi
    e restituisce un dizionario {str(inventory_item_id): cost_float}.
    Se il cost è None o assente, restituisce 0.0.
    """
    if not inventory_item_ids:
        return {}

    cost_map = {}
    headers = {
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }

    unique_ids = list({str(iid).strip() for iid in inventory_item_ids if iid})
    chunk_size = 50

    for i in range(0, len(unique_ids), chunk_size):
        chunk = unique_ids[i : i + chunk_size]
        ids_param = ",".join(chunk)
        url = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/inventory_items.json?ids={ids_param}"
        try:
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code == 200:
                items = res.json().get("inventory_items", [])
                for item in items:
                    item_id = str(item.get("id"))
                    cost_val = item.get("cost")
                    try:
                        cost_float = float(cost_val) if cost_val is not None else 0.0
                    except (ValueError, TypeError):
                        cost_float = 0.0
                    cost_map[item_id] = cost_float
        except Exception as e:
            logger.error(f"[COSTI INVENTORY] Errore recupero costi per chunk: {e}")

    return cost_map


def recupera_ordini_del_giorno(data_riferimento: str = None, cfg: dict = None) -> list:
    """
    Recupera tutti gli ordini pagati (financial_status == 'paid') della data indicata
    (default: oggi YYYY-MM-DD). Per ciascun articolo recupera il 'cost' dall'InventoryItem
    (oppure 0.0 se non impostato) e calcola il totale costo d'acquisto e prezzo vendita.
    """
    if not data_riferimento:
        data_riferimento = datetime.now().strftime("%Y-%m-%d")

    shop = normalizza_shop_domain(cfg)
    token = _risolvi_token_shopify(cfg)

    if not shop or not token:
        logger.error("[REPORT ORDINI] Credenziali Shopify mancanti.")
        return []

    url = f"https://{shop}/admin/api/{SHOPIFY_REST_VERSION}/orders.json"
    headers = {
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    params = {
        "status": "any",
        "financial_status": "paid",
        "created_at_min": f"{data_riferimento}T00:00:00",
        "limit": 250,
    }

    try:
        res = requests.get(url, params=params, headers=headers, timeout=15)
        if res.status_code != 200:
            logger.error(f"[REPORT ORDINI] Errore HTTP {res.status_code}: {res.text}")
            return []

        all_orders = res.json().get("orders", [])
    except Exception as e:
        logger.error(f"[REPORT ORDINI] Eccezione recupero ordini: {e}")
        return []

    ordini_oggi = []
    inventory_item_ids_needed = set()

    # Indicizzazione catalogo per risolvere variant_id -> inventory_item_id
    _, lista_prodotti = _costruisci_indice_catalogo(shop, token)
    variant_to_inventory_item = {}
    for _, _, _, var in lista_prodotti:
        v_id = var.get("id")
        inv_id = var.get("inventory_item_id")
        if v_id and inv_id:
            variant_to_inventory_item[str(v_id)] = str(inv_id)

    for o in all_orders:
        created_at = o.get("created_at", "")
        if not created_at.startswith(data_riferimento):
            continue

        if o.get("financial_status") != "paid":
            continue

        ordini_oggi.append(o)
        for li in o.get("line_items", []):
            v_id = str(li.get("variant_id") or "")
            inv_id = variant_to_inventory_item.get(v_id)
            if inv_id:
                inventory_item_ids_needed.add(inv_id)

    cost_map = recupera_costi_inventory_items(list(inventory_item_ids_needed), shop, token)

    ordini_elaborati = []
    for o in ordini_oggi:
        prezzo_vendita_totale = float(o.get("total_price") or 0.0)
        costo_acquisto_totale = 0.0
        dettagli_righe = []

        for li in o.get("line_items", []):
            qty = int(li.get("quantity") or 1)
            v_id = str(li.get("variant_id") or "")
            inv_id = variant_to_inventory_item.get(v_id)
            costo_unitario = cost_map.get(inv_id, 0.0)
            costo_linea = costo_unitario * qty
            costo_acquisto_totale += costo_linea

            dettagli_righe.append({
                "name": li.get("name") or li.get("title") or "Articolo",
                "sku": li.get("sku") or "",
                "quantity": qty,
                "price": float(li.get("price") or 0.0),
                "unit_cost": costo_unitario,
                "total_cost": costo_linea,
            })

        ordini_elaborati.append({
            "id": o.get("id"),
            "order_number": o.get("order_number") or o.get("name") or "N/D",
            "date": data_riferimento,
            "created_at": o.get("created_at"),
            "customer": estrai_nome_cliente(o),
            "total_price": round(prezzo_vendita_totale, 2),
            "total_cost": round(costo_acquisto_totale, 2),
            "line_items": dettagli_righe,
        })

    logger.info(f"[REPORT ORDINI] Elaborati {len(ordini_elaborati)} ordini pagati per la data {data_riferimento}.")
    return ordini_elaborati
