"""Extract structured product data (e-commerce) from a page.

Sources, in order of reliability:
  1. schema.org JSON-LD  (<script type="application/ld+json"> with @type Product / ProductGroup)
  2. schema.org microdata (itemtype=".../Product" with itemprop=...)
  3. Open Graph / product meta tags (og:type=product, product:price:amount ...)

Almost every shop (Shopify, WooCommerce, Magento, BigCommerce, most big brands)
publishes at least one of these for Google Shopping, so this is far more
robust than guessing from CSS classes.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Optional

from bs4 import BeautifulSoup, Tag

PRODUCT_TYPES = {"product", "productgroup", "productmodel", "individualproduct", "vehicle", "book"}


def _types(node: Dict) -> List[str]:
    t = node.get("@type", [])
    if isinstance(t, str):
        t = [t]
    return [str(x).split("/")[-1].lower() for x in t]


def _walk(obj: Any) -> Iterable[Dict]:
    """Yield every dict inside a JSON-LD blob (handles @graph, lists, nesting)."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        return _text(v.get("name") or v.get("@value") or v.get("value") or v.get("@id") or "")
    if isinstance(v, list):
        return ", ".join(t for t in (_text(x) for x in v) if t)
    s = re.sub(r"<[^>]+>", " ", str(v))  # descriptions sometimes contain HTML
    return re.sub(r"\s+", " ", s).strip()


def _image_urls(v: Any) -> List[str]:
    out: List[str] = []
    for item in (v if isinstance(v, list) else [v]):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            u = item.get("contentUrl") or item.get("url") or item.get("@id")
            if isinstance(u, str):
                out.append(u)
    return out


def _availability(v: Any) -> str:
    s = _text(v)
    return s.rsplit("/", 1)[-1] if s else ""


def _offers(v: Any) -> List[Dict]:
    out = []
    for o in (v if isinstance(v, list) else [v]):
        if not isinstance(o, dict):
            continue
        # AggregateOffer: lowPrice/highPrice, maybe nested offers
        if "offers" in o and isinstance(o["offers"], (list, dict)):
            out += _offers(o["offers"])
        price = o.get("price") or o.get("lowPrice")
        spec = o.get("priceSpecification")
        if price is None and isinstance(spec, dict):
            price = spec.get("price")
        if price is None and isinstance(spec, list) and spec and isinstance(spec[0], dict):
            price = spec[0].get("price")
        out.append({
            "price": _text(price),
            "high_price": _text(o.get("highPrice")),
            "currency": _text(o.get("priceCurrency")
                              or (spec.get("priceCurrency") if isinstance(spec, dict) else "")),
            "availability": _availability(o.get("availability")),
            "condition": _availability(o.get("itemCondition")),
            "sku": _text(o.get("sku")),
            "url": _text(o.get("url")),
            "seller": _text(o.get("seller")),
        })
    return [o for o in out if any(o.values())]


def _from_jsonld_node(n: Dict, norm: Callable[[str], Optional[str]]) -> Dict:
    offers = _offers(n.get("offers"))
    first = next((o for o in offers if o["price"]), offers[0] if offers else {})
    rating = n.get("aggregateRating") if isinstance(n.get("aggregateRating"), dict) else {}
    variants = []
    for v in n.get("hasVariant") or []:
        if isinstance(v, dict):
            vo = _offers(v.get("offers"))
            variants.append({
                "name": _text(v.get("name")), "sku": _text(v.get("sku")),
                "color": _text(v.get("color")), "size": _text(v.get("size")),
                "price": vo[0]["price"] if vo else "",
                "availability": vo[0]["availability"] if vo else "",
                "images": [u for u in (norm(x) for x in _image_urls(v.get("image"))) if u],
            })
    images = [u for u in (norm(x) for x in _image_urls(n.get("image"))) if u]
    for var in variants:
        images += [u for u in var["images"] if u not in images]
    props = {}
    for p in n.get("additionalProperty") or []:
        if isinstance(p, dict) and p.get("name"):
            props[_text(p["name"])] = _text(p.get("value"))
    return {
        "name": _text(n.get("name")),
        "brand": _text(n.get("brand") or n.get("manufacturer")),
        "sku": _text(n.get("sku") or first.get("sku")),
        "mpn": _text(n.get("mpn")),
        "gtin": _text(n.get("gtin13") or n.get("gtin12") or n.get("gtin14") or n.get("gtin8") or n.get("gtin")),
        "category": _text(n.get("category")),
        "color": _text(n.get("color")),
        "size": _text(n.get("size")),
        "material": _text(n.get("material")),
        "price": first.get("price", ""),
        "high_price": first.get("high_price", ""),
        "currency": first.get("currency", ""),
        "availability": first.get("availability", ""),
        "condition": first.get("condition", ""),
        "rating": _text(rating.get("ratingValue")),
        "review_count": _text(rating.get("reviewCount") or rating.get("ratingCount")),
        "description": _text(n.get("description"))[:5000],
        "url": norm(_text(n.get("url"))) or "",
        "images": images,
        "offers": offers,
        "variants": variants,
        "properties": props,
        "source": "json-ld",
    }


def _from_microdata(scope: Tag, norm) -> Dict:
    def prop(name: str, root: Tag = scope) -> str:
        el = root.find(attrs={"itemprop": name})
        if not el:
            return ""
        return (el.get("content") or el.get("value") or el.get("href") or el.get("src")
                or el.get_text(" ", strip=True) or "").strip()

    offer = scope.find(attrs={"itemprop": "offers"}) or scope
    rating = scope.find(attrs={"itemprop": "aggregateRating"}) or scope
    images = []
    for el in scope.find_all(attrs={"itemprop": "image"}):
        u = norm(el.get("content") or el.get("src") or el.get("href") or "")
        if u and u not in images:
            images.append(u)
    brand_el = scope.find(attrs={"itemprop": "brand"})
    brand = ""
    if brand_el:
        brand = prop("name", brand_el) or brand_el.get("content") or brand_el.get_text(" ", strip=True)
    return {
        "name": prop("name"), "brand": brand, "sku": prop("sku"), "mpn": prop("mpn"),
        "gtin": prop("gtin13") or prop("gtin"), "category": prop("category"),
        "color": prop("color"), "size": "", "material": prop("material"),
        "price": prop("price", offer) or prop("lowPrice", offer), "high_price": prop("highPrice", offer),
        "currency": prop("priceCurrency", offer),
        "availability": prop("availability", offer).rsplit("/", 1)[-1],
        "condition": prop("itemCondition", offer).rsplit("/", 1)[-1],
        "rating": prop("ratingValue", rating), "review_count": prop("reviewCount", rating),
        "description": re.sub(r"\s+", " ", prop("description"))[:5000],
        "url": norm(prop("url")) or "", "images": images, "offers": [], "variants": [],
        "properties": {}, "source": "microdata",
    }


def _from_meta(meta: Dict[str, str], norm) -> Optional[Dict]:
    is_product = meta.get("og:type", "").lower() in ("product", "og:product", "product.item") \
        or "product:price:amount" in meta or "og:price:amount" in meta
    if not is_product:
        return None
    img = norm(meta.get("og:image", "")) if meta.get("og:image") else None
    return {
        "name": meta.get("og:title", ""), "brand": meta.get("product:brand", ""),
        "sku": meta.get("product:retailer_item_id", ""), "mpn": "", "gtin": "",
        "category": meta.get("product:category", ""), "color": meta.get("product:color", ""),
        "size": "", "material": "",
        "price": meta.get("product:price:amount") or meta.get("og:price:amount", ""),
        "high_price": "",
        "currency": meta.get("product:price:currency") or meta.get("og:price:currency", ""),
        "availability": meta.get("product:availability") or meta.get("og:availability", ""),
        "condition": meta.get("product:condition", ""), "rating": "", "review_count": "",
        "description": meta.get("og:description", ""), "url": norm(meta.get("og:url", "")) or "",
        "images": [img] if img else [], "offers": [], "variants": [], "properties": {},
        "source": "meta",
    }


def extract_products(soup: BeautifulSoup, json_ld: List[Any], meta: Dict[str, str],
                     norm: Callable[[str], Optional[str]]) -> List[Dict]:
    products: List[Dict] = []
    seen_ids = set()
    for blob in json_ld:
        for node in _walk(blob):
            if not PRODUCT_TYPES.intersection(_types(node)):
                continue
            # a Product nested in a ProductGroup's hasVariant is reported as a variant
            if id(node) in seen_ids:
                continue
            for v in node.get("hasVariant") or []:
                seen_ids.add(id(v))
            p = _from_jsonld_node(node, norm)
            if p["name"] or p["price"] or p["sku"]:
                products.append(p)
    if not products:
        for scope in soup.find_all(attrs={"itemtype": re.compile(r"schema\.org/Product", re.I)}):
            if scope.find_parent(attrs={"itemtype": re.compile(r"schema\.org/Product", re.I)}):
                continue
            p = _from_microdata(scope, norm)
            if p["name"] or p["price"]:
                products.append(p)
    if not products:
        p = _from_meta(meta, norm)
        if p and (p["name"] or p["price"]):
            products.append(p)
    # fill gaps from meta tags (og:image is often the best product shot)
    for p in products:
        if not p["images"] and meta.get("og:image"):
            u = norm(meta["og:image"])
            if u:
                p["images"].append(u)
        if not p["currency"]:
            p["currency"] = meta.get("product:price:currency") or meta.get("og:price:currency", "")
    return products
