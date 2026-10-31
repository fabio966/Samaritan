"""
========================================================================================
BOT PULICHIMICA - Modulo Integrazione Shopify (api_shopify.py)
========================================================================================
Racchiude tutte le chiamate REST verso Shopify:
  - Recupero ultimi 3 ordini
  - Ricerca prodotti e giacenze sull'intero catalogo
  - Health Check API Shopify (shop.json)
  - Monitoraggio automatico nuovi ordini (07:00-20:00) con persistenza su last_order_id.txt
Importa esclusivamente 'config' per garantire assenza totale di dipendenze circolari.
========================================================================================
"""

import os
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
