#!/usr/bin/env python3
"""Pokéfada : surveille des fiches produit et prévient sur Discord au réassort.

Pour chaque produit et chaque magasin, le script retrouve tout seul la fiche produit grâce
à la recherche du site (code-barres puis nom), vérifie que c'est le bon produit, garde
l'adresse, et la recherche de nouveau si elle change.

Usage :
    python monitor.py            passage normal (alertes + mise à jour de etat.json)
    python monitor.py --test     récap sur Discord de ce que le script lit pour chaque produit
                                 et chaque magasin, sans toucher à etat.json
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
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, unquote, urljoin, urlsplit, urlunsplit

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
PROBLEM_STATUSES = {"blocked", "error", "unknown", "lien", "liste", "autre"}
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
    "autre": "page changée",
}
STATUS_LABEL = {
    "in_stock": "en stock", "preorder": "précommande", "out_of_stock": "rupture",
    "absent": "pas trouvé dans ce magasin", "attente": "recherche au prochain passage",
    "recherche": "recherche du site illisible",
    **PROBLEM_LABEL,
}
ICONS = {"in_stock": "🟢", "preorder": "🟡", "out_of_stock": "🔴",
         "blocked": "⛔", "error": "⛔", "unknown": "❓", "lien": "🔗", "liste": "📋", "autre": "📋",
         "absent": "⚪", "attente": "⏳", "recherche": "❔"}


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

# ================================================================ reconnaître le bon produit

def norm(text):
    """Minuscules, sans accents ni ponctuation, entouré d'espaces : ' coffret dresseur elite '."""
    text = unicodedata.normalize("NFKD", unescape(text or "")).encode("ascii", "ignore").decode()
    return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()).strip() + " "


def has_term(text, term):
    """`term` est un mot/une expression, ou une liste d'alternatives (une seule suffit)."""
    terms = term if isinstance(term, list) else [term]
    return any(norm(t) in text for t in terms)


def name_matches(product, text):
    text = norm(text)
    return (all(has_term(text, t) for t in product.get("must", []))
            and not any(has_term(text, t) for t in product.get("exclude", [])))


def ean_in(text, ean):
    digits = (ean or "").lstrip("0")
    return bool(digits) and re.search(rf"(?<!\d)0*{digits}(?!\d)", text) is not None


def page_product_name(html):
    """Nom du produit de la fiche : JSON-LD, sinon <h1>, sinon <title>."""
    names = []
    for m in LD_RE.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for node in nodes:
            if isinstance(node, dict) and "Product" in str(node.get("@type")) and node.get("name"):
                names.append(str(node["name"]))
    if len(names) == 1:
        return names[0]
    for tag in ("h1", "title"):
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", html, re.I | re.S)
        if m:
            return re.sub(r"<[^>]+>", " ", m.group(1))
    return ""


def verify(product, html):
    """La page est-elle bien la fiche de ce produit ? Retourne la preuve ou None."""
    if product.get("ean") and ean_in(html, product["ean"]):
        return "code-barres"
    if product.get("must") and name_matches(product, page_product_name(html)):
        return "nom du produit"
    return None


# ================================================================ recherche dans les magasins

A_RE = re.compile(r"<a\b([^>]*?)href=[\"']([^\"'#]+)[\"']([^>]*)>(.*?)</a>", re.S | re.I)
NOT_PRODUCT_SEGMENTS = {"panier", "cart", "basket", "compte", "account", "login", "connexion", "wishlist",
                        "favoris", "magasin", "magasins", "c", "categorie", "categories", "aide",
                        "contact", "cgv", "mentions-legales"}


def not_product_path(path):
    """Adresses qui ne sont sûrement pas des fiches (panier, compte, recherche, catégorie...)."""
    if re.search(r"recherche|search|resultat", path, re.I):
        return True
    segments = [seg.split(".")[0].lower() for seg in path.split("/") if seg]
    return any(seg in NOT_PRODUCT_SEGMENTS for seg in segments)
DEFAULT_TEMPLATES = ["/recherche?q={q}", "/search?q={q}", "/recherche?controller=search&s={q}",
                     "/catalogsearch/result/?q={q}"]
PROBE_QUERY = "pokemon"
PROBE_TTL = 7 * 24 * 3600        # le format de recherche d'un magasin est revérifié chaque semaine
PROBE_RETRY = 24 * 3600          # un magasin dont la recherche est illisible est retenté chaque jour
SEARCH_RETRY = 6 * 3600          # un produit introuvable est recherché de nouveau toutes les 6 h
SEARCH_BUDGET = {"normal": 3, "test": 99}   # recherches de produits par magasin et par passage


def same_site(url, domain):
    h = host_of(url)
    return h == domain or h.endswith("." + domain)


def store_templates(store):
    base = f"https://www.{store['domain']}"
    out = list(store.get("search", []))
    for t in DEFAULT_TEMPLATES:
        full = base + t
        if full not in out:
            out.append(full)
    return out


def result_links(html, base_url, domain):
    """Liens vers des pages du magasin trouvés dans une page de résultats : {url: texte}."""
    links = {}
    for before, href, after, inner in A_RE.findall(html):
        url = urljoin(base_url, unescape(href.strip()))
        if not url.startswith("http") or not same_site(url, domain):
            continue
        if not_product_path(urlsplit(url).path):
            continue
        alts = " ".join(re.findall(r'(?:alt|title|aria-label)=["\']([^"\']*)["\']', before + after + inner))
        text = re.sub(r"<[^>]+>", " ", inner) + " " + alts
        key = url.split("#")[0]
        links[key] = links.get(key, "") + " " + text
    # adresses écrites dans du JSON (pages construites en JavaScript)
    flat = html.replace("\\/", "/")
    for url in re.findall(rf"https?://(?:[\w-]+\.)*{re.escape(domain)}/[^\"'\s<>\\]+", flat):
        if not not_product_path(urlsplit(url).path):
            links.setdefault(url, "")
    return links


def rank_candidates(product, links):
    """Garde les liens qui ressemblent au produit (mots-clés dans l'adresse ou le texte)."""
    scored = []
    for url, text in links.items():
        hay = norm(unquote(urlsplit(url).path) + " " + text)
        score = 0
        if product.get("ean") and ean_in(url + " " + text, product["ean"]):
            score += 10
        must = product.get("must", [])
        hits = sum(1 for t in must if has_term(hay, t))
        score += 2 * hits
        if any(has_term(hay, t) for t in product.get("exclude", [])):
            score -= 5
        if score >= 10 or (must and hits == len(must) and score > 0):
            scored.append((score, url))
    scored.sort(key=lambda x: -x[0])
    return [u for _, u in scored[:3]]


def fetch(session, url):
    try:
        return session.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
    except requests.RequestException:
        return None


def pause():
    time.sleep(random.uniform(2, 4))


def is_blocked(r):
    if r is None:
        return "pas de réponse"
    if r.status_code in (401, 403, 429, 503):
        return f"HTTP {r.status_code}"
    low = r.text[:20000].lower()
    if any(k in low for k in ("just a moment", "captcha", "cf-chl", "access denied")):
        return "page anti-robot"
    return None


def probe_store(store, session):
    """Trouve le format d'adresse de recherche du magasin. Retourne (modèle, diagnostic)."""
    last = "aucun format de recherche ne donne de résultats lisibles"
    for template in store_templates(store):
        r = fetch(session, template.format(q=quote_plus(PROBE_QUERY)))
        pause()
        blocked = is_blocked(r)
        if blocked:
            last = f"recherche bloquée ({blocked})"
            if r is not None and r.status_code in (403, 429):
                break  # inutile d'insister : le site refuse le script
            continue
        if r.status_code >= 400:
            continue
        links = result_links(r.text, r.url, store["domain"])
        n = sum(1 for u, t in links.items() if " pokemon " in norm(unquote(urlsplit(u).path) + " " + t))
        if n >= 3:
            return template, f"recherche OK ({n} liens Pokémon)"
        last = f"page de recherche sans liens produit lisibles ({diagnose(r)['court']})"
    return None, last


def search_product(product, store, template, session):
    """Cherche la fiche du produit. Retourne (url, réponse, preuve) ou (None, None, raison)."""
    queries = ([product["ean"]] if product.get("ean") else []) + [product["query"]]
    seen = set()
    for q in queries:
        r = fetch(session, template.format(q=quote_plus(q)))
        pause()
        blocked = is_blocked(r)
        if blocked:
            return None, None, f"recherche bloquée ({blocked})"
        if r.status_code >= 400:
            continue
        # certains sites ouvrent directement la fiche quand un seul produit correspond
        if not listing_reason(r):
            proof = verify(product, r.text)
            if proof:
                return clean_url(r.url) or r.url, r, proof
        for url in rank_candidates(product, result_links(r.text, r.url, store["domain"])):
            if url in seen:
                continue
            seen.add(url)
            rr = fetch(session, url)
            pause()
            if rr is None or rr.status_code != 200 or listing_reason(rr):
                continue
            proof = verify(product, rr.text)
            if proof:
                return clean_url(rr.url) or rr.url, rr, proof
    return None, None, "pas trouvé dans ce magasin"


# ================================================================ lecture d'une fiche

def evaluate(r, product, offer):
    """Transforme la réponse d'une fiche en résultat (statut, prix, source...)."""
    if r is None:
        return {"status": "error", "price": None, "source": "pas de réponse", "diag": None}
    diag = diagnose(r)
    if r.status_code in (401, 403, 429, 503):
        return {"status": "blocked", "price": None, "source": f"HTTP {r.status_code}", "diag": diag}
    if r.status_code in (404, 410):
        return {"status": "error", "price": None, "source": f"HTTP {r.status_code} (page supprimée)",
                "diag": diag, "gone": True}
    if r.status_code >= 400:
        return {"status": "error", "price": None, "source": f"HTTP {r.status_code}", "diag": diag}
    reason = listing_reason(r)
    if reason and not offer.get("allow_listing"):
        return {"status": "liste", "price": None, "source": reason, "diag": diag, "gone": True}
    proof = verify(product, r.text)
    if not proof and offer.get("auto"):
        return {"status": "autre", "price": None, "source": "la page ne montre plus ce produit",
                "diag": diag, "gone": True}
    status, price, source = analyse(r.text, offer)
    if status == "blocked":
        return {"status": "blocked", "price": None, "source": source, "diag": diag}
    return {"status": status, "price": price, "source": source, "diag": diag, "verified": bool(proof)}


def check_store(job):
    """Tout le travail d'un magasin (pages connues, puis recherches), une requête à la fois."""
    store, pairs, state, mode = job
    session = requests.Session()
    now = int(time.time())
    out, notes = {}, []
    fiches = state["fiches"]
    tries = state["recherches"]
    engines = state["moteurs"]
    to_search = []

    # 1. pages déjà connues : liens fixes de products.json, puis fiches trouvées par recherche
    for pid, product, offer in pairs:
        url = None if offer.get("auto") else offer_url(offer, state)
        if not offer.get("auto") and not url:
            err = state["liens"].get(offer.get("link") or "", {}).get("err") or "aucun lien"
            out[pid] = {"status": "lien", "price": None, "source": err, "diag": None,
                        "url": offer.get("link"), "via": "lien"}
            continue
        if offer.get("auto"):
            url = (fiches.get(pid) or {}).get("url")
            if not url:
                to_search.append((pid, product, offer))
                continue
        res = evaluate(fetch(session, url), product, offer)
        pause()
        res.update(url=url, via="recherche" if offer.get("auto") else "lien")
        if offer.get("auto") and res.get("gone"):
            fiches.pop(pid, None)          # la fiche a changé : on la recherchera
            tries.pop(pid, None)
            to_search.append((pid, product, offer))
        out[pid] = res

    if not to_search:
        return out, notes

    # 2. format de recherche du magasin
    eng = engines.get(store["name"]) or {}
    age = now - eng.get("ts", 0)
    if not eng or (eng.get("template") and age > PROBE_TTL) or (not eng.get("template") and age > PROBE_RETRY):
        template, diag = probe_store(store, session)
        eng = {"template": template, "ts": now, "diag": diag}
        engines[store["name"]] = eng
    notes.append(eng.get("diag", ""))
    template = eng.get("template")

    # 3. recherche des produits sans fiche
    budget = SEARCH_BUDGET[mode]
    for pid, product, offer in to_search:
        if pid in out and out[pid]["status"] not in ("liste", "autre", "error"):
            continue
        if not template:
            out[pid] = {"status": "recherche", "price": None, "source": eng.get("diag", ""),
                        "diag": None, "url": None, "via": "recherche"}
            continue
        last = tries.get(pid, 0)
        if mode == "normal" and now - last < SEARCH_RETRY:
            out.setdefault(pid, {"status": "absent", "price": None, "source": "pas trouvé dans ce magasin",
                                 "diag": None, "url": None, "via": "recherche"})
            continue
        if budget <= 0:
            out.setdefault(pid, {"status": "attente", "price": None, "source": "recherche au prochain passage",
                                 "diag": None, "url": None, "via": "recherche"})
            continue
        budget -= 1
        tries[pid] = now
        url, r, proof = search_product(product, store, template, session)
        if not url:
            status = "blocked" if proof.startswith("recherche bloquée") else "absent"
            out[pid] = {"status": status, "price": None, "source": proof, "diag": None,
                        "url": None, "via": "recherche"}
            continue
        fiches[pid] = {"url": url, "ts": now, "preuve": proof}
        res = evaluate(r, product, offer)
        res.update(url=url, via=f"recherche ({proof})", new=True)
        out[pid] = res
    return out, notes
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



# ================================================================ configuration

def load_config():
    cfg = load_json(PRODUCTS_FILE, None)
    if not isinstance(cfg, dict) or not cfg.get("products") or not cfg.get("stores"):
        return None
    for key, items in (("id", cfg["products"]), ("name", cfg["stores"])):
        values = [i[key] for i in items]
        if len(values) != len(set(values)):
            raise SystemExit(f"products.json : deux éléments ont le même {key}.")
    return cfg


def build_jobs(cfg, test):
    """Associe chaque produit à chaque magasin. Retourne ({magasin: [(pid, produit, offre)]}, lignes)."""
    def wanted(status):
        return status == "actif" or (status == "test" and test)

    jobs, rows, stores = defaultdict(list), [], {}
    for s in cfg["stores"]:
        stores[s["name"]] = s
    for p in cfg["products"]:
        explicit = {o["store"]: o for o in p.get("offers", [])}
        for s in cfg["stores"]:
            o = explicit.pop(s["name"], None)
            store_status = s.get("status", "actif")
            status = o.get("status", store_status) if o else store_status
            if not wanted(status):
                continue
            offer = o or {"store": s["name"], "auto": True}
            pid = f"{p['id']}|{s['name']}"
            jobs[s["name"]].append((pid, p, offer))
            rows.append((p, s["name"], pid, offer))
        for name, o in explicit.items():      # lien fixe vers une boutique hors de la liste
            if not wanted(o.get("status", "actif")):
                continue
            stores.setdefault(name, {"name": name, "domain": host_of(o.get("url") or "")})
            pid = f"{p['id']}|{name}"
            jobs[name].append((pid, p, o))
            rows.append((p, name, pid, o))
    return jobs, rows, stores


# ================================================================ exécution

def is_reliable(res, offer):
    if res["source"] in RELIABLE_SOURCES or offer.get("trust_text"):
        return True
    # sur une fiche dont on a vérifié l'identité, la lecture du texte devient crédible
    return bool(res.get("verified")) and res["source"] in ("texte", "motif brut")


def describe(res):
    if res["status"] in ("in_stock", "preorder", "out_of_stock"):
        text = f"{fmt_price(res['price'])} (lu via {res['source']})"
    else:
        text = no_ping(res["source"])
    if str(res.get("via", "")).startswith("recherche") and res.get("url"):
        text += " — fiche trouvée par recherche"
    return text


def write_github_summary(rows, results):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["# Pokéfada : détail du passage", ""]
    current = None
    for p, store, pid, offer in rows:
        if p["id"] != current:
            current = p["id"]
            lines += ["", f"## {p['name']}", "",
                      "| Magasin | Statut | Prix | Lu via | Trouvée par | Page | Diagnostic |",
                      "|---|---|---|---|---|---|---|"]
        r = results[pid]
        diag = r.get("diag") or {}
        cell = (diag.get("court", "") + " — " + diag.get("texte", "")[:120]).strip(" —").replace("|", "/")
        lines.append(f"| {store} | {STATUS_LABEL.get(r['status'], r['status'])} | {fmt_price(r['price'])} | "
                     f"{r['source'].replace('|', '/')} | {r.get('via', '')} | {r.get('url') or ''} | {cell} |")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def run(args):
    cfg = load_config()
    if not cfg:
        print("products.json est vide, illisible ou à l'ancien format.")
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
    if not isinstance(state, dict):
        state = {}
    for key in ("offres", "liens", "fiches", "recherches", "moteurs"):
        state.setdefault(key, {})

    jobs, rows, stores = build_jobs(cfg, args.test)
    resolve_all([(p, o) for p, _, _, o in rows if not o.get("auto")], state, force=args.test)

    mode = "test" if args.test else "normal"
    results, notes = {}, {}
    if jobs:
        with ThreadPoolExecutor(max_workers=min(20, len(jobs))) as ex:
            futures = {name: ex.submit(check_store, (stores[name], pairs, state, mode))
                       for name, pairs in jobs.items()}
            for name, fut in futures.items():
                res, n = fut.result()
                results.update(res)
                notes[name] = n

    for p, store, pid, _ in rows:
        r = results[pid]
        print(f"- {p['name']} [{store}] -> {r['status']} / {describe(r)}")
    write_github_summary(rows, results)

    if args.test:
        return send_test_report(cfg, rows, results, notes, notify)

    # décisions d'alerte
    new_problems, recovered, found = [], [], []
    seen = set()
    for p, store, pid, offer in rows:
        seen.add(pid)
        r = results[pid]
        status, price = r["status"], r["price"]
        max_price = p.get("max_price")
        st = state["offres"].setdefault(pid, {"status": None, "price": None, "alerted": False,
                                              "problems": 0, "problem_notified": False})
        if r.get("new"):
            found.append(f"• {p['name']} — {store} : {r['url']}")

        if status in PROBLEM_STATUSES:
            st["problems"] += 1
            if st["problems"] >= PROBLEM_THRESHOLD and not st["problem_notified"]:
                new_problems.append(f"• {p['name']} — {store} : "
                                    f"{PROBLEM_LABEL.get(status, status)} ({no_ping(r['source'])})")
                st["problem_notified"] = True
            continue
        if st["problem_notified"]:
            recovered.append(f"• {p['name']} — {store}")
            st["problem_notified"] = False
        st["problems"] = 0

        price_ok = max_price is None or price is None or price <= max_price
        reliable = is_reliable(r, offer)
        alertable = status in IN_STATUSES and price_ok and reliable
        if alertable and not st["alerted"]:
            kind = "PRÉCOMMANDE" if status == "preorder" else "EN STOCK"
            lines = [f"🚨 **{kind}** : {p['name']}", f"🏬 {store}  •  💶 {fmt_price(price)}"]
            if price is None and max_price is not None:
                lines.append(f"⚠️ prix non lu, vérifie qu'il est ≤ {fmt_price(max_price)}")
            if r["source"] not in ("json-ld", "meta"):
                lines.append(f"ℹ️ détection par {r['source']} (moins fiable)")
            lines.append(r["url"])
            if notify("@everyone " + "\n".join(lines)):
                st["alerted"] = True
        elif not alertable:
            if status in IN_STATUSES and not reliable:
                print(f"  (en stock lu via {r['source']}, peu fiable : pas d'alerte)")
            elif status in IN_STATUSES:
                print(f"  (en stock mais {fmt_price(price)} > {fmt_price(max_price)}, pas d'alerte)")
            st["alerted"] = False
        st["status"], st["price"] = status, price

    if found:
        for msg in chunks(["🔎 **Nouvelles fiches trouvées** (surveillées à partir de maintenant) :"] + found):
            notify(msg)
    if new_problems:
        head = (f"⚠️ **Pages que je ne peux plus surveiller** (depuis {PROBLEM_THRESHOLD} passages) "
                f"— aucune alerte possible pour elles tant que ça dure :")
        for msg in chunks([head] + new_problems):
            notify(msg)
    if recovered:
        for msg in chunks(["✅ **Surveillance rétablie** :"] + recovered):
            notify(msg)

    # oublie ce qui a été retiré de products.json
    for key in ("offres", "fiches", "recherches"):
        state[key] = {k: v for k, v in state[key].items() if k in seen}
    if not args.dry_run:
        save_state(state)
    return 0


def send_test_report(cfg, rows, results, notes, notify):
    notify("🧪 **Test Pokéfada** : ce que le script lit pour chaque produit et chaque magasin\n"
           "🟢 en stock • 🟡 précommande • 🔴 rupture • ⚪ pas trouvé dans ce magasin • "
           "⏳ recherche au prochain passage • ❔ recherche du site illisible • ⛔ bloqué/erreur • "
           "❓ illisible • 📋 pas une fiche produit • 🔗 lien non résolu\n"
           "*(test)* = désactivé en temps normal")
    by_product = defaultdict(list)
    for p, store, pid, offer in rows:
        by_product[p["id"]].append((store, pid, offer))
    for p in cfg["products"]:
        if p["id"] not in by_product:
            continue
        cap = f" (alerte si ≤ {fmt_price(p['max_price'])})" if p.get("max_price") else ""
        lines = [f"**{p['name']}**{cap}"]
        for store, pid, offer in by_product[p["id"]]:
            r = results[pid]
            tag = " *(test)*" if offer.get("status") == "test" else ""
            label = STATUS_LABEL.get(r["status"], r["status"])
            detail = describe(r)
            text = label if norm(detail) == norm(label) else f"{label}, {detail}"
            lines.append(f"{ICONS.get(r['status'], '❓')} {store}{tag} : {text}")
        for msg in chunks(lines):
            notify(msg)

    # un diagnostic par magasin : sa recherche, et la 1re page en échec
    per_store = {}
    for p, store, pid, offer in rows:
        r = results[pid]
        line = per_store.setdefault(store, {"engine": "; ".join(x for x in notes.get(store, []) if x),
                                            "problem": None})
        if r["status"] in PROBLEM_STATUSES and not line["problem"]:
            d = r.get("diag") or {}
            line["problem"] = d.get("court") or r["source"]
    out = ["🔎 **Diagnostic par magasin** (détail : onglet Actions → ce passage → Summary)"]
    for store, info in per_store.items():
        parts = [x for x in (info["engine"], info["problem"]) if x]
        if parts:
            out.append(f"• **{store}** : {no_ping(' / '.join(parts))}")
    if len(out) > 1:
        for msg in chunks(out):
            notify(msg)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--test", action="store_true", help="récap Discord, sans toucher à l'état")
    parser.add_argument("--dry-run", action="store_true", help="aucun envoi Discord, aucune écriture")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
