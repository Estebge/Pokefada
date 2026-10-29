#!/usr/bin/env python3
"""Pokéfada : surveille des fiches produit et prévient sur Discord au réassort.

Usage :
    python monitor.py            passage normal (alertes + mise à jour de etat.json)
    python monitor.py --test     récap sur Discord de ce que le script lit sur chaque page
                                 (offres "actif" ET "test"), sans toucher à etat.json
    python monitor.py --dry-run  comme un passage normal, mais n'envoie rien et n'écrit rien

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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

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

# Domaines des magasins : un lien raccourci est "résolu" dès qu'il atteint l'un d'eux.
MERCHANT_DOMAINS = [
    "amazon.fr", "e.leclerc", "fnac.com", "carrefour.fr", "cdiscount.com",
    "micromania.fr", "1001hobbies.fr", "bcd-jeux.fr", "figurines-goodies.com",
    "cultura.com", "auchan.fr", "rakuten.com", "joueclub.fr", "lagranderecre.fr",
    "king-jouet.com", "smythstoys.com",
]

IN_STATUSES = {"in_stock", "preorder"}
PROBLEM_STATUSES = {"blocked", "error", "unknown", "lien", "liste"}
# seules ces lectures déclenchent une alerte (sauf "trust_text": true dans l'offre)
RELIABLE_SOURCES = {"json-ld", "meta", "regex"}
LISTING_TITLE = re.compile(r"r[ée]sultat|recherche|vente termin|cat[ée]gorie", re.I)
PROBLEM_THRESHOLD = 3            # passages en échec d'affilée avant de te prévenir (~30 min)
LINK_RETRY_SECONDS = 6 * 3600    # un lien raccourci non résolu est retenté toutes les 6 h

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
DIAG_WORDS = ["panier", "stock", "disponible", "indisponible", "précommande",
              "bientôt", "épuisé", "javascript", "captcha", "cookies"]

LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)

PROBLEM_LABEL = {
    "blocked": "bloqué par le site",
    "error": "erreur du site",
    "unknown": "stock illisible",
    "lien": "lien non résolu",
    "liste": "pas une fiche produit",
}
STATUS_LABEL = {
    "in_stock": "en stock", "preorder": "précommande", "out_of_stock": "rupture",
    **PROBLEM_LABEL,
}
ICONS = {"in_stock": "🟢", "preorder": "🟡", "out_of_stock": "🔴",
         "blocked": "⛔", "error": "⛔", "unknown": "❓", "lien": "🔗", "liste": "📋"}


# ================================================================ liens

def host_of(url):
    return (urlsplit(url).hostname or "").lower()


def is_merchant(host):
    return any(host == d or host.endswith("." + d) for d in MERCHANT_DOMAINS)


def clean_url(url):
    """Retire le suivi d'affiliation (paramètres) d'une URL de magasin.
    Retourne None si l'URL n'est que la page d'accueil."""
    p = urlsplit(url)
    path = p.path or "/"
    if path == "/":
        return None
    query = ""
    if re.search(r"search|recherche|listing|/s$", path, re.I):  # page de résultats : garder la requête
        keep = [(k, v) for k, v in parse_qsl(p.query)
                if not re.match(r"(utm_|awc|tag$|ref|aff|cmp|xtor|gclid|ae$)", k, re.I)]
        query = "&".join(f"{k}={v}" for k, v in keep)
    return urlunsplit((p.scheme or "https", p.netloc, path, query, ""))


def embedded_merchant_url(url):
    """Les liens d'affiliation contiennent souvent l'adresse finale en paramètre."""
    for _, value in parse_qsl(urlsplit(url).query):
        for candidate in (value, unquote(value)):
            if candidate.startswith("http") and is_merchant(host_of(candidate)):
                return candidate
    return None


def resolve_link(link, session):
    """Suit un lien raccourci (bit.ly, tidd.ly, ...) jusqu'au magasin, sans charger
    la page du magasin. Retourne (url_propre, None) ou (None, raison)."""
    url = link
    for _ in range(10):
        host = host_of(url)
        target = embedded_merchant_url(url)
        if target and target != url:
            url = target
            continue
        if is_merchant(host):
            clean = clean_url(url)
            if clean:
                return clean, None
            if not urlsplit(url).query:
                return None, f"renvoie vers l'accueil de {host}"
            # racine d'un sous-domaine du magasin avec paramètres (ex. xht.micromania.fr/?...) :
            # c'est un redirecteur de suivi, on continue à le suivre
        try:
            r = session.get(url, headers=HEADERS, timeout=20, allow_redirects=False)
        except requests.RequestException as e:
            return None, f"{type(e).__name__} sur {host}"
        location = r.headers.get("Location")
        if location:
            url = urljoin(url, location)
            continue
        m = (re.search(r'http-equiv=["\']refresh["\'][^>]*?url=([^"\'>\s]+)', r.text, re.I)
             or re.search(r'location(?:\.href)?\s*=\s*["\'](https?://[^"\']+)', r.text))
        if m:
            url = urljoin(url, unescape(m.group(1)))
            continue
        if is_merchant(host):
            return None, f"renvoie vers l'accueil de {host}"
        return None, f"bloqué sur {host} (HTTP {r.status_code})"
    return None, "trop de redirections"


def resolve_all(offers, state, force):
    """Résout les liens raccourcis une seule fois et garde le résultat dans etat.json."""
    links = state["liens"]
    now = int(time.time())
    todo = []
    for _, o in offers:
        link = o.get("link")
        if o.get("url") or not link or link in todo:
            continue
        entry = links.get(link)
        if entry and entry.get("url"):
            continue
        if entry and not force and now - entry.get("ts", 0) < LINK_RETRY_SECONDS:
            continue
        todo.append(link)
    if not todo:
        return

    def work(link):
        time.sleep(random.uniform(0, 1.5))
        return (link, *resolve_link(link, requests.Session()))

    with ThreadPoolExecutor(max_workers=6) as ex:
        for link, url, err in ex.map(work, todo):
            links[link] = {"url": url, "err": err, "ts": now}
            print(f"lien {link} -> {url or 'ÉCHEC : ' + err}")


def offer_url(offer, state):
    if offer.get("url"):
        return offer["url"]
    return state["liens"].get(offer.get("link") or "", {}).get("url")


# ================================================================ analyse des pages

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
    if v in ("instock", "limitedavailability", "onlineonly", "available",
             "lowstock", "limitedstock"):
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


RAW_PATTERNS = [
    # microdata : <link itemprop="availability" href="https://schema.org/InStock">
    r'itemprop=["\']availability["\'][^>]*?(?:href|content)=["\']([^"\']+)["\']',
    r'(?:href|content)=["\']([^"\']+)["\'][^>]*?itemprop=["\']availability["\']',
    # JSON embarqué dans la page (état initial d'une appli JavaScript)
    r'"availability"\s*:\s*"([^"]+)"',
    r'"stockLevelStatus"\s*:\s*"(\w+)"',
    r'"stockStatus"\s*:\s*"(\w+)"',
    r'"stock_status"\s*:\s*"(\w+)"',
]


def from_raw(html):
    """Cherche un motif de stock n'importe où dans le code. Garde la 1re occurrence."""
    best = None
    for pattern in RAW_PATTERNS:
        m = re.search(pattern, html, re.I)
        if m:
            status = map_availability(m.group(1))
            if status and (best is None or m.start() < best[0]):
                best = (m.start(), status)
    m = re.search(r'"inStock"\s*:\s*(true|false)', html, re.I)
    if m and (best is None or m.start() < best[0]):
        best = (m.start(), "in_stock" if m.group(1).lower() == "true" else "out_of_stock")
    return best[1] if best else None


def visible_text(html):
    html = re.sub(r"<(script|style|noscript)[\s\S]*?</\1>", " ", html, flags=re.I)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    return re.sub(r"\s+", " ", text).lower()


def analyse(html, offer):
    """Retourne (statut, prix, source)."""
    text = None

    # 0. règles personnalisées (si la détection auto échoue sur un site)
    if offer.get("out_of_stock_regex") or offer.get("in_stock_regex"):
        text = visible_text(html)
        if offer.get("out_of_stock_regex") and re.search(offer["out_of_stock_regex"], text, re.I):
            return "out_of_stock", None, "regex"
        if offer.get("in_stock_regex") and re.search(offer["in_stock_regex"], text, re.I):
            return "in_stock", None, "regex"

    # 1. données structurées schema.org
    found = from_json_ld(html)
    if found:
        return found[0], found[1], "json-ld"

    # 2. balises meta
    found = from_meta(html)
    if found:
        return found[0], found[1], "meta"

    # 3. motifs de stock dans le code de la page (moins fiable : peut viser un autre produit)
    status = from_raw(html)
    if status:
        return status, None, "motif brut"

    # 4. texte de la page (moins fiable)
    text = text or visible_text(html)
    if any(k in text for k in OUT_MARKERS):
        return "out_of_stock", None, "texte"
    if any(k in text for k in IN_MARKERS):
        return "in_stock", None, "texte"

    if any(k in text or k in html.lower() for k in BLOCK_MARKERS):
        return "blocked", None, "page anti-robot"
    return "unknown", None, "rien de lisible"


def diagnose(r):
    """Résumé court de ce que la page contient, pour comprendre un échec de lecture."""
    html = r.text
    text = visible_text(html)
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title_txt = re.sub(r"\s+", " ", unescape(title.group(1))).strip()[:70] if title else "(aucun)"
    words = ", ".join(w for w in DIAG_WORDS if w in text) or "aucun"
    return {
        "court": f"HTTP {r.status_code}, {len(html) // 1000} ko, titre « {title_txt} », mots : {words}",
        "texte": text[:200],
    }


def top_level_products(html):
    """Nombre de produits décrits en JSON-LD au premier niveau (une fiche n'en a qu'un)."""
    count = 0
    for m in LD_RE.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if "ItemList" in types and len(node.get("itemListElement") or []) > 1:
                return 99
            if "Product" in types:
                count += 1
    return count


def listing_reason(r):
    """Raison de penser que la page n'est pas une fiche produit (recherche, catégorie...)."""
    title = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.I | re.S)
    if title:
        t = re.sub(r"\s+", " ", unescape(title.group(1))).strip()
        if LISTING_TITLE.search(t):
            return f"titre « {t[:60]} »"
    if re.search(r"recherche|search|resultat", urlsplit(r.url).path, re.I):
        return "adresse de recherche"
    n = top_level_products(r.text)
    if n > 1:
        return "plusieurs produits sur la page"
    return None


def check_page(url, offer, session):
    try:
        r = session.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
    except requests.RequestException as e:
        return {"status": "error", "price": None, "source": type(e).__name__, "diag": None}
    diag = diagnose(r)
    if r.status_code in (401, 403, 429, 503):
        return {"status": "blocked", "price": None, "source": f"HTTP {r.status_code}", "diag": diag}
    if r.status_code == 404:
        return {"status": "error", "price": None, "source": "HTTP 404 (lien mort ?)", "diag": diag}
    if r.status_code >= 400:
        return {"status": "error", "price": None, "source": f"HTTP {r.status_code}", "diag": diag}
    reason = listing_reason(r)
    if reason and not offer.get("allow_listing"):
        return {"status": "liste", "price": None, "source": reason, "diag": diag}
    status, price, source = analyse(r.text, offer)
    return {"status": status, "price": price, "source": source, "diag": diag}


def check_group(items):
    """Vérifie les pages d'un même magasin une par une, avec une pause entre chaque."""
    session = requests.Session()
    out = {}
    for i, (oid, offer, url) in enumerate(items):
        if i:
            time.sleep(random.uniform(2, 5))
        res = check_page(url, offer, session)
        res["url"] = url
        out[oid] = res
    return out


# ================================================================ Discord

def send_discord(webhook, content):
    """Envoie un message. Retourne True si Discord a accepté."""
    payload = {"content": content[:1990], "allowed_mentions": {"parse": ["everyone"]}}
    for _ in range(4):
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
            time.sleep(min(wait, 15))
            continue
        return r.ok
    return False


def chunks(lines, limit=1900):
    """Regroupe des lignes en messages de moins de `limit` caractères."""
    out, cur = [], ""
    for line in lines:
        line = line[:limit]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def no_ping(text):
    return text.replace("@", "@​")


def fmt_price(price):
    return f"{price:.2f} €".replace(".", ",") if price is not None else "prix non lu"


# ================================================================ état

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


def load_products():
    cfg = load_json(PRODUCTS_FILE, None)
    products = cfg.get("products") if isinstance(cfg, dict) else None
    if not products:
        return None
    ids = [p["id"] for p in products]
    if len(ids) != len(set(ids)):
        raise SystemExit("products.json : deux produits ont le même id.")
    return products


def offer_id(product, offer):
    return f"{product['id']}|{offer['store']}"


# ================================================================ exécution

def write_github_summary(products, offers, results):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["# Pokéfada : détail du passage", ""]
    by_product = defaultdict(list)
    for p, o in offers:
        by_product[p["id"]].append(o)
    for p in products:
        if p["id"] not in by_product:
            continue
        lines += [f"## {p['name']}", "",
                  "| Magasin | Statut | Prix | Lu via | Page | Diagnostic |",
                  "|---|---|---|---|---|---|"]
        for o in by_product[p["id"]]:
            r = results[offer_id(p, o)]
            diag = r.get("diag") or {}
            cell = (diag.get("court", "") + " — " + diag.get("texte", "")[:120]).strip(" —")
            cell = cell.replace("|", "/")
            page = r.get("url") or ""
            lines.append(f"| {o['store']} ({o.get('status', 'actif')}) | "
                         f"{STATUS_LABEL.get(r['status'], r['status'])} | {fmt_price(r['price'])} | "
                         f"{r['source']} | {page} | {cell} |")
        lines.append("")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def run(args):
    products = load_products()
    if not products:
        print("products.json est vide ou illisible.")
        return 1

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    sending = not args.dry_run
    if sending and not re.match(r"^https://(?:\w+\.)?(?:discord|discordapp)\.com/api/webhooks/", webhook):
        print("DISCORD_WEBHOOK_URL absent ou invalide (Secret GitHub à créer).")
        return 1

    def notify(text):
        print("DISCORD>", text.replace("\n", " | "))
        return send_discord(webhook, text) if sending else True

    state = load_json(STATE_FILE, {})
    if not isinstance(state, dict) or "offres" not in state:
        state = {"offres": {}, "liens": {}}  # ancien format : on repart de zéro
    state.setdefault("liens", {})

    # offres à vérifier ce passage-ci
    offers = []
    for p in products:
        for o in p.get("offers", []):
            mode = o.get("status", "actif")
            if mode == "actif" or (mode == "test" and args.test):
                offers.append((p, o))

    # 1. liens raccourcis -> adresses directes des magasins
    resolve_all(offers, state, force=args.test)

    # 2. pages groupées par magasin ; les magasins sont vérifiés en parallèle
    results, groups = {}, defaultdict(list)
    for p, o in offers:
        oid, url = offer_id(p, o), offer_url(o, state)
        if url:
            groups[host_of(url)].append((oid, o, url))
        else:
            err = state["liens"].get(o.get("link") or "", {}).get("err") or "aucun lien"
            results[oid] = {"status": "lien", "price": None, "source": err,
                            "diag": None, "url": o.get("link")}
    if groups:
        with ThreadPoolExecutor(max_workers=min(20, len(groups))) as ex:
            for res in ex.map(check_group, groups.values()):
                results.update(res)

    for p, o in offers:
        r = results[offer_id(p, o)]
        print(f"- {p['name']} [{o['store']}] -> {r['status']} / {fmt_price(r['price'])} ({r['source']})")
    write_github_summary(products, offers, results)

    if args.test:
        return send_test_report(products, offers, results, notify)

    # 3. décisions d'alerte
    new_problems, recovered = [], []
    seen = set()
    for p, o in offers:
        oid = offer_id(p, o)
        seen.add(oid)
        r = results[oid]
        status, price, source = r["status"], r["price"], r["source"]
        max_price = p.get("max_price")
        st = state["offres"].setdefault(oid, {"status": None, "price": None, "alerted": False,
                                              "problems": 0, "problem_notified": False})

        if status in PROBLEM_STATUSES:
            st["problems"] += 1
            if st["problems"] >= PROBLEM_THRESHOLD and not st["problem_notified"]:
                new_problems.append(f"• {p['name']} — {o['store']} : "
                                    f"{PROBLEM_LABEL[status]} ({no_ping(source)})")
                st["problem_notified"] = True
            continue
        if st["problem_notified"]:
            recovered.append(f"• {p['name']} — {o['store']}")
            st["problem_notified"] = False
        st["problems"] = 0

        price_ok = max_price is None or price is None or price <= max_price
        reliable = source in RELIABLE_SOURCES or o.get("trust_text")
        alertable = status in IN_STATUSES and price_ok and reliable
        if alertable and not st["alerted"]:
            kind = "PRÉCOMMANDE" if status == "preorder" else "EN STOCK"
            lines = [f"🚨 **{kind}** : {p['name']}", f"🏬 {o['store']}  •  💶 {fmt_price(price)}"]
            if price is None and max_price is not None:
                lines.append(f"⚠️ prix non lu, vérifie qu'il est ≤ {fmt_price(max_price)}")
            if source not in ("json-ld", "meta"):
                lines.append(f"ℹ️ détection par {source} (moins fiable)")
            lines.append(r["url"])
            if notify("@everyone " + "\n".join(lines)):
                st["alerted"] = True
        elif not alertable:
            if status in IN_STATUSES and not reliable:
                print(f"  (en stock lu via {source}, peu fiable : pas d'alerte)")
            elif status in IN_STATUSES:
                print(f"  (en stock mais {fmt_price(price)} > {fmt_price(max_price)}, pas d'alerte)")
            st["alerted"] = False
        st["status"], st["price"] = status, price

    if new_problems:
        head = (f"⚠️ **Pages que je ne peux plus surveiller** (depuis {PROBLEM_THRESHOLD} passages) "
                f"— aucune alerte possible pour elles tant que ça dure :")
        for msg in chunks([head] + new_problems):
            notify(msg)
    if recovered:
        for msg in chunks(["✅ **Surveillance rétablie** :"] + recovered):
            notify(msg)

    # oublie les offres retirées de products.json
    state["offres"] = {k: v for k, v in state["offres"].items() if k in seen}
    if not args.dry_run:
        save_state(state)
    return 0


def send_test_report(products, offers, results, notify):
    by_product = defaultdict(list)
    for p, o in offers:
        by_product[p["id"]].append(o)

    notify("🧪 **Test Pokéfada** : ce que le script lit sur chaque page\n"
           "🟢 en stock • 🟡 précommande • 🔴 rupture • ⛔ bloqué/erreur • ❓ illisible • 🔗 lien non résolu • 📋 pas une fiche produit\n"
           "*(test)* = offre désactivée en temps normal • lu via json-ld/meta = fiable, "
           "via texte/motif brut = pas d'alerte")
    for p in products:
        if p["id"] not in by_product:
            continue
        cap = f" (alerte si ≤ {fmt_price(p['max_price'])})" if p.get("max_price") else ""
        lines = [f"**{p['name']}**{cap}"]
        for o in by_product[p["id"]]:
            r = results[offer_id(p, o)]
            tag = " *(test)*" if o.get("status") == "test" else ""
            if r["status"] in PROBLEM_STATUSES:
                detail = no_ping(r["source"])
            else:
                detail = f"{fmt_price(r['price'])} (lu via {r['source']})"
            lines.append(f"{ICONS.get(r['status'], '❓')} {o['store']}{tag} : "
                         f"{STATUS_LABEL.get(r['status'], r['status'])}, {detail}")
        for msg in chunks(lines):
            notify(msg)

    # un diagnostic par magasin (la 1re page en échec), le détail complet est sur GitHub
    per_store = {}
    for p, o in offers:
        r = results[offer_id(p, o)]
        store = o["store"].split(" (")[0]
        if r["status"] in PROBLEM_STATUSES and store not in per_store:
            d = r.get("diag") or {}
            per_store[store] = f"• **{store}** : {no_ping(d.get('court') or r['source'])}"
    if per_store:
        head = ("🔎 **Diagnostic par magasin** (détail complet : onglet Actions → "
                "ce passage → Summary)")
        for msg in chunks([head] + list(per_store.values())):
            notify(msg)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--test", action="store_true", help="récap Discord, sans toucher à l'état")
    parser.add_argument("--dry-run", action="store_true", help="aucun envoi Discord, aucune écriture")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
