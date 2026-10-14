#!/usr/bin/env python3
"""Pokéfada : surveille des fiches produit et prévient sur Discord au réassort.

Usage :
    python monitor.py            passage normal (alerte + mise à jour de etat.json)
    python monitor.py --test     envoie un récap de ce que le script lit sur chaque page
    python monitor.py --dry-run  affiche tout dans le terminal, n'envoie rien, n'écrit rien

L'URL du webhook Discord se lit dans la variable d'environnement DISCORD_WEBHOOK_URL
(Secret GitHub). Elle n'est jamais écrite dans le code ni dans les logs.
"""
import argparse
import json
import os
import random
import re
import sys
import time
from html import unescape
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
PRODUCTS_FILE = BASE / "products.json"
STATE_FILE = BASE / "etat.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
}

IN_STATUSES = {"in_stock", "preorder"}
PROBLEM_STATUSES = {"blocked", "error", "unknown"}
PROBLEM_THRESHOLD = 3  # passages consécutifs en échec avant de te prévenir (~30 min)

OUT_MARKERS = [
    "rupture de stock", "indisponible", "épuisé", "epuise", "out of stock",
    "sold out", "bientôt disponible", "prévenez-moi", "me prévenir", "être alerté",
]
IN_MARKERS = [
    "ajouter au panier", "ajouter à mon panier", "add to basket", "add to cart",
    "acheter maintenant",
]
BLOCK_MARKERS = [
    "captcha", "access denied", "just a moment", "robot check", "are you a human",
    "accès refusé", "verify you are human", "cf-chl",
]

LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)


# ---------------------------------------------------------------- analyse

def parse_price(value):
    """'35,94 €' / '35.94' / 35.94 -> 35.94 (ou None)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^\d,.\-]", "", str(value))
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def map_availability(value):
    """Normalise une disponibilité (URL schema.org ou texte) en statut."""
    if not value:
        return None
    v = re.sub(r"[\s_\-]", "", str(value).lower().rsplit("/", 1)[-1])
    if v in ("instock", "limitedavailability", "onlineonly", "available"):
        return "in_stock"
    if v in ("preorder", "presale", "availableforpreorder"):
        return "preorder"
    if v in ("outofstock", "oos", "soldout", "discontinued", "backorder",
             "instoreonly", "unavailable", "pending"):
        return "out_of_stock"
    return None


def iter_nodes(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_nodes(v)


def from_json_ld(html):
    """Lit les offres schema.org. Garde l'offre en stock la moins chère, sinon 'out_of_stock'."""
    offers = []
    for m in LD_RE.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for node in iter_nodes(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Offer" in types or "AggregateOffer" in types:
                status = map_availability(node.get("availability"))
                if status:
                    price = parse_price(node.get("price", node.get("lowPrice")))
                    offers.append((status, price))
    if not offers:
        return None
    available = [o for o in offers if o[0] in IN_STATUSES]
    if available:
        priced = [o for o in available if o[1] is not None]
        return min(priced, key=lambda o: o[1]) if priced else available[0]
    return offers[0]


def meta_content(html, *names):
    pattern = r'(?:property|name|itemprop)=["\'](?:%s)["\']' % "|".join(map(re.escape, names))
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I):
        if re.search(pattern, tag, re.I):
            m = re.search(r'content=["\']([^"\']*)["\']', tag, re.I)
            if m:
                return unescape(m.group(1))
    return None


def from_meta(html):
    status = map_availability(meta_content(html, "og:availability", "product:availability"))
    if not status:
        return None
    price = parse_price(meta_content(html, "product:price:amount", "og:price:amount"))
    return status, price


def visible_text(html):
    html = re.sub(r"<(script|style|noscript)[\s\S]*?</\1>", " ", html, flags=re.I)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    return re.sub(r"\s+", " ", text).lower()


def analyse(html, product):
    """Retourne (statut, prix, source)."""
    text = None

    # 0. règles personnalisées (si la détection auto échoue sur un site)
    if product.get("out_of_stock_regex") or product.get("in_stock_regex"):
        text = visible_text(html)
        if product.get("out_of_stock_regex") and re.search(product["out_of_stock_regex"], text, re.I):
            return "out_of_stock", None, "regex"
        if product.get("in_stock_regex") and re.search(product["in_stock_regex"], text, re.I):
            return "in_stock", None, "regex"

    # 1. données structurées schema.org
    found = from_json_ld(html)
    if found:
        return found[0], found[1], "json-ld"

    # 2. balises meta
    found = from_meta(html)
    if found:
        return found[0], found[1], "meta"

    # 3. texte de la page (moins fiable)
    text = text or visible_text(html)
    if any(k in text for k in OUT_MARKERS):
        return "out_of_stock", None, "texte"
    if any(k in text for k in IN_MARKERS):
        return "in_stock", None, "texte"

    if any(k in text or k in html.lower() for k in BLOCK_MARKERS):
        return "blocked", None, "page de blocage"
    return "unknown", None, "rien de lisible"


def check_product(product, session):
    try:
        r = session.get(product["url"], headers=HEADERS, timeout=25, allow_redirects=True)
    except requests.RequestException as e:
        return {"status": "error", "price": None, "source": type(e).__name__}
    if r.status_code in (401, 403, 429, 503):
        return {"status": "blocked", "price": None, "source": f"HTTP {r.status_code}"}
    if r.status_code == 404:
        return {"status": "error", "price": None, "source": "HTTP 404 (lien mort ?)"}
    if r.status_code >= 400:
        return {"status": "error", "price": None, "source": f"HTTP {r.status_code}"}
    status, price, source = analyse(r.text, product)
    return {"status": status, "price": price, "source": source}


# ---------------------------------------------------------------- Discord

def send_discord(webhook, content):
    """Envoie un message. Retourne True si Discord a accepté."""
    payload = {"content": content[:1990], "allowed_mentions": {"parse": ["everyone"]}}
    for _ in range(3):
        try:
            r = requests.post(webhook, json=payload, timeout=15)
        except requests.RequestException as e:
            print(f"  ! envoi Discord impossible ({type(e).__name__})")
            return False
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except ValueError:
                wait = 2
            time.sleep(min(wait, 10))
            continue
        return r.ok
    return False


def fmt_price(price):
    return f"{price:.2f} €".replace(".", ",") if price is not None else "prix non lu"


# ---------------------------------------------------------------- état

def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return default


def save_state(state):
    text = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if not STATE_FILE.exists() or STATE_FILE.read_text(encoding="utf-8") != text:
        STATE_FILE.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- exécution

PROBLEM_LABEL = {
    "blocked": "le site bloque les requêtes du script",
    "error": "le site ne répond pas correctement",
    "unknown": "la page se charge mais je n'y lis pas le stock",
}


def run(args):
    products = load_json(PRODUCTS_FILE, [])
    if not products:
        print("products.json est vide ou illisible.")
        return 1

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    sending = not args.dry_run
    if sending:
        if not re.match(r"^https://(?:\w+\.)?(?:discord|discordapp)\.com/api/webhooks/", webhook):
            print("DISCORD_WEBHOOK_URL absent ou invalide (Secret GitHub à créer).")
            return 1

    def notify(text):
        print("DISCORD>", text.replace("\n", " | "))
        return send_discord(webhook, text) if sending else True

    state = load_json(STATE_FILE, {})
    session = requests.Session()
    recap = []

    for i, product in enumerate(products):
        if i:
            time.sleep(random.uniform(2, 6))
        name, url = product["name"], product["url"]
        store = product.get("store", "")
        max_price = product.get("max_price")
        res = check_product(product, session)
        status, price, source = res["status"], res["price"], res["source"]
        print(f"- {name} [{store}] -> {status} / {fmt_price(price)} ({source})")
        recap.append((name, store, status, price, source))

        if args.test:
            continue

        st = state.setdefault(url, {"status": None, "price": None, "alerted": False,
                                    "problems": 0, "problem_notified": False})

        # --- site illisible / bloqué : ne rien conclure, mais prévenir si ça dure
        if status in PROBLEM_STATUSES:
            st["problems"] += 1
            if st["problems"] >= PROBLEM_THRESHOLD and not st["problem_notified"]:
                if notify(f"⚠️ **{name}** ({store}) : {PROBLEM_LABEL[status]} "
                          f"({source}) depuis plusieurs passages. "
                          f"Je ne peux pas le surveiller tant que ça dure."):
                    st["problem_notified"] = True
            continue

        if st["problem_notified"]:
            notify(f"✅ **{name}** ({store}) : la surveillance fonctionne de nouveau.")
            st["problem_notified"] = False
        st["problems"] = 0

        # --- décision d'alerte
        price_ok = max_price is None or price is None or price <= max_price
        alertable = status in IN_STATUSES and price_ok
        if alertable and not st["alerted"]:
            kind = "PRÉCOMMANDE" if status == "preorder" else "EN STOCK"
            lines = [f"🚨 **{kind}** : {name}", f"🏬 {store}  •  💶 {fmt_price(price)}"]
            if price is None and max_price is not None:
                lines.append(f"⚠️ prix non lu, vérifie qu'il est ≤ {max_price:.2f} €".replace(".", ",", 1))
            if source in ("texte", "regex"):
                lines.append(f"ℹ️ détection par {source} (moins fiable)")
            lines.append(url)
            if notify("@everyone " + "\n".join(lines)):
                st["alerted"] = True
        elif not alertable:
            if status in IN_STATUSES:
                print(f"  (en stock mais {fmt_price(price)} > {max_price} €, pas d'alerte)")
            st["alerted"] = False
        st["status"], st["price"] = status, price

    if args.test:
        icons = {"in_stock": "🟢", "preorder": "🟡", "out_of_stock": "🔴",
                 "blocked": "⛔", "error": "⛔", "unknown": "❓"}
        lines = ["🧪 **Test Pokéfada** : voilà ce que le script lit sur chaque page"]
        for name, store, status, price, source in recap:
            lines.append(f"{icons.get(status, '❓')} {name} ({store}) : {status}, "
                         f"{fmt_price(price)}, via {source}")
        lines.append("🟢 en stock • 🔴 rupture • ⛔ bloqué/erreur • ❓ page illisible")
        notify("\n".join(lines))
        return 0

    if not args.dry_run:
        save_state(state)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--test", action="store_true", help="récap Discord, sans toucher à l'état")
    parser.add_argument("--dry-run", action="store_true", help="aucun envoi Discord, aucune écriture")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
