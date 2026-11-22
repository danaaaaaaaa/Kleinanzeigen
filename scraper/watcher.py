#!/usr/bin/env python3
"""
Kleinanzeigen-Preiswächter
==========================
Wird per GitHub Actions regelmäßig ausgeführt. Ablauf je Produkt der Watchlist:

  1. Suchergebnisse bei Kleinanzeigen abrufen      (KleinanzeigenClient)
  2. Anzeigen aus dem HTML lesen                   (ListingParser)
  3. Filtern, Preisstatistik, gute Angebote finden (PriceAnalyzer)
  4. Verlauf fortschreiben                         (ProductHistory / Storage)
  5. Neue gute Angebote per ntfy-Push melden       (Notifier)

Nur Python-Standardbibliothek, es müssen keine Pakete installiert werden.
"""
from __future__ import annotations

import json
import html
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------- Konstanten
ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config" / "watchlist.json"
DATA_FILE = ROOT / "data" / "history.json"

BASE_URL = "https://www.kleinanzeigen.de"
MIN_LISTINGS_FOR_RELATIVE_DEAL = 6  # darunter ist der Median zu unsicher
BASELINE_DAYS = 30                  # Zeitraum für den "normalen" Preis
MAX_SNAPSHOTS = 3000                # Verlauf pro Produkt begrenzen
KEEP_LISTING_DAYS = 30              # Anzeigen so lange nach letzter Sichtung behalten
MAX_NOTIFIED = 500                  # Merkliste bereits gemeldeter Anzeigen
MAX_PUSH_PER_PRODUCT = 5            # max. Einzel-Pushes je Produkt und Lauf


# ---------------------------------------------------------------- Hilfsfunktionen
def now_iso() -> str:
    """Aktuelle UTC-Zeit als ISO-String."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def fmt_eur(value: float) -> str:
    """1299 -> '1.299 €'"""
    return f"{round(value):,}".replace(",", ".") + " €"


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------- Datenmodelle
@dataclass
class Product:
    """Ein beobachtetes Produkt aus config/watchlist.json."""
    id: str
    name: str
    query: str
    min_price: int = 0
    max_price: Optional[int] = None
    target_price: Optional[int] = None   # fester Wunschpreis (optional)
    deal_percent: float = 20             # sonst: so viel % unter dem Normalpreis
    exclude: list = field(default_factory=list)
    max_pages: int = 2

    @classmethod
    def from_dict(cls, d: dict) -> "Product":
        return cls(
            id=str(d["id"]),
            name=d.get("name") or str(d["id"]),
            query=d["query"],
            min_price=int(d.get("min_price") or 0),
            max_price=int(d["max_price"]) if d.get("max_price") else None,
            target_price=int(d["target_price"]) if d.get("target_price") else None,
            deal_percent=float(d.get("deal_percent") or 20),
            exclude=[str(x).strip().lower() for x in d.get("exclude", []) if str(x).strip()],
            max_pages=max(1, min(int(d.get("max_pages") or 2), 5)),
        )


@dataclass
class Listing:
    """Eine einzelne Anzeige."""
    ad_id: str
    title: str
    price: int
    url: str


class ScrapeError(Exception):
    """Abruf fehlgeschlagen (Netzwerk, Blockade, Layoutänderung)."""


# ---------------------------------------------------------------- Abruf
class KleinanzeigenClient:
    """Holt Suchergebnisseiten von Kleinanzeigen."""

    HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "de-DE,de;q=0.9",
    }

    def build_url(self, product: Product, page: int) -> str:
        """Baut die Such-URL, z. B. /s-preis:150:700/seite:2/playstation-5/k0"""
        filters = []
        if product.min_price or product.max_price:
            filters.append(f"preis:{product.min_price or ''}:{product.max_price or ''}")
        if page > 1:
            filters.append(f"seite:{page}")
        slug = urllib.parse.quote(product.query.strip().lower().replace(" ", "-"))
        prefix = "s-" + "/".join(filters) + "/" if filters else "s-"
        return f"{BASE_URL}/{prefix}{slug}/k0"

    def fetch(self, url: str) -> str:
        request = urllib.request.Request(url, headers=self.HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise ScrapeError(f"HTTP {exc.code} bei {url}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ScrapeError(f"Netzwerkfehler bei {url}: {exc}") from exc


# ---------------------------------------------------------------- Parser
class ListingParser:
    """Liest Anzeigen aus dem HTML einer Suchergebnisseite."""

    ARTICLE_RE = re.compile(r"<article\b([^>]*)>(.*?)</article>", re.S | re.I)
    AD_ID_RE = re.compile(r'data-adid="(\d+)"')
    HREF_RE = re.compile(r'data-href="([^"]+)"')
    TITLE_RE = re.compile(r'<a[^>]*class="[^"]*ellipsis[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
    H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S | re.I)
    PRICE_BLOCK_RE = re.compile(r'class="[^"]*price[^"]*"[^>]*>(.*?)</(?:p|div|span)>', re.S | re.I)
    EURO_RE = re.compile(r"(\d[\d.]*)(?:,\d+)?\s*€")

    @staticmethod
    def _text(fragment: str) -> str:
        """HTML-Fragment -> sauberer Text."""
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()

    def _price(self, text: str) -> Optional[int]:
        match = self.EURO_RE.search(text)
        return int(match.group(1).replace(".", "")) if match else None

    def parse(self, page_html: str) -> list:
        listings = []
        for attrs, body in self.ARTICLE_RE.findall(page_html):
            id_match = self.AD_ID_RE.search(attrs)
            if not id_match:
                continue  # kein Anzeigen-Artikel (z. B. Werbebanner)
            title_match = self.TITLE_RE.search(body) or self.H2_RE.search(body)
            title = self._text(title_match.group(1)) if title_match else ""

            price_block = self.PRICE_BLOCK_RE.search(body)
            price = self._price(self._text(price_block.group(1))) if price_block else None
            if price is None:
                price = self._price(self._text(body))  # Notlösung: erster Eurobetrag
            if not title or price is None:
                continue  # z. B. "Zu verschenken" oder "VB" ohne Betrag

            href_match = self.HREF_RE.search(attrs)
            href = href_match.group(1) if href_match else f"/s-anzeige/{id_match.group(1)}"
            listings.append(Listing(id_match.group(1), title, price, BASE_URL + href))
        return listings


# ---------------------------------------------------------------- Analyse
class PriceAnalyzer:
    """Filtert Anzeigen, berechnet Statistik und erkennt gute Angebote."""

    def filter(self, listings: list, product: Product) -> list:
        """Preisspanne prüfen und Ausschlusswörter im Titel aussortieren."""
        result = []
        for item in listings:
            if item.price < product.min_price:
                continue
            if product.max_price and item.price > product.max_price:
                continue
            if any(word in item.title.lower() for word in product.exclude):
                continue
            result.append(item)
        return result

    def snapshot(self, listings: list, timestamp: str) -> dict:
        """Preisstatistik des aktuellen Marktes."""
        prices = sorted(item.price for item in listings)
        p25 = statistics.quantiles(prices, n=4)[0] if len(prices) >= 2 else prices[0]
        return {
            "t": timestamp,
            "count": len(prices),
            "median": round(statistics.median(prices)),
            "min": prices[0],
            "p25": round(p25),
        }

    def threshold(self, product: Product, baseline: Optional[float],
                  current_median: float, count: int) -> Optional[float]:
        """Preis, ab dem eine Anzeige als 'gut' gilt."""
        if product.target_price:
            return float(product.target_price)
        if count < MIN_LISTINGS_FOR_RELATIVE_DEAL:
            return None
        reference = baseline or current_median
        return reference * (1 - product.deal_percent / 100)

    def deals(self, listings: list, threshold: Optional[float]) -> list:
        """Alle Anzeigen unter der Schwelle, günstigste zuerst."""
        if threshold is None:
            return []
        return sorted((i for i in listings if i.price <= threshold), key=lambda i: i.price)


# ---------------------------------------------------------------- Verlauf
class ProductHistory:
    """Gespeicherter Verlauf eines Produkts (Messwerte + bekannte Anzeigen)."""

    def __init__(self, raw: Optional[dict] = None):
        raw = raw or {}
        self.snapshots: list = raw.get("snapshots", [])
        self.listings: dict = raw.get("listings", {})
        self.notified: list = raw.get("notified", [])
        self.threshold: Optional[float] = raw.get("threshold")
        self.baseline: Optional[float] = raw.get("baseline")

    def baseline_median(self, days: int) -> Optional[float]:
        """Normalpreis = Median der Messwerte der letzten Tage (min. 3 Messwerte)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        values = [s["median"] for s in self.snapshots if parse_iso(s["t"]) >= cutoff]
        return statistics.median(values) if len(values) >= 3 else None

    def add_snapshot(self, snapshot: dict) -> None:
        self.snapshots.append(snapshot)
        self.snapshots = self.snapshots[-MAX_SNAPSHOTS:]

    def update_listings(self, current: list, now: str) -> None:
        """Aktuelle Anzeigen übernehmen, verschwundene als inaktiv markieren."""
        for entry in self.listings.values():
            entry["active"] = False
        for item in current:
            entry = self.listings.get(item.ad_id)
            if entry is None:
                entry = {"first_seen": now}
                self.listings[item.ad_id] = entry
            elif entry.get("price") not in (None, item.price):
                entry["prev_price"] = entry["price"]  # Preisänderung merken
            entry.update(title=item.title, price=item.price, url=item.url,
                         last_seen=now, active=True)
        cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_LISTING_DAYS)
        self.listings = {k: v for k, v in self.listings.items()
                         if parse_iso(v["last_seen"]) >= cutoff}

    def mark_notified(self, ad_ids: list) -> None:
        self.notified = (self.notified + ad_ids)[-MAX_NOTIFIED:]

    def to_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "baseline": self.baseline,
            "snapshots": self.snapshots,
            "listings": self.listings,
            "notified": self.notified,
        }


class Storage:
    """Liest und schreibt data/history.json."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"updated": None, "products": {}}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- Benachrichtigung
class Notifier:
    """Push-Nachrichten über ntfy.sh (App 'ntfy' auf dem iPhone)."""

    def __init__(self, topic: Optional[str], server: str = "https://ntfy.sh"):
        self.topic = (topic or "").strip()
        self.server = server

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    def send(self, title: str, message: str, click: Optional[str] = None) -> bool:
        """Sendet eine Nachricht. Rückgabe: True bei Erfolg."""
        if not self.enabled:
            return False
        payload = {"topic": self.topic, "title": title, "message": message,
                   "tags": ["moneybag"], "priority": 4}
        if click:
            payload["click"] = click
        request = urllib.request.Request(
            self.server, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20):
                return True
        except (urllib.error.URLError, TimeoutError) as exc:
            log(f"  ! Push fehlgeschlagen: {exc}")
            return False


# ---------------------------------------------------------------- Hauptablauf
class Watcher:
    """Steuert den kompletten Lauf über alle Produkte."""

    def __init__(self, client: KleinanzeigenClient, parser: ListingParser,
                 analyzer: PriceAnalyzer, storage: Storage, notifier: Notifier):
        self.client = client
        self.parser = parser
        self.analyzer = analyzer
        self.storage = storage
        self.notifier = notifier

    def run(self) -> int:
        """Gibt 0 zurück, wenn mindestens ein Produkt erfolgreich war."""
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        products = [Product.from_dict(p) for p in config.get("products", [])]
        data = self.storage.load()
        data.setdefault("products", {})

        succeeded = 0
        for product in products:
            log(f"== {product.name}")
            try:
                self._process(product, data)
                succeeded += 1
            except ScrapeError as exc:
                log(f"  ! {exc}")

        if succeeded:
            data["updated"] = now_iso()
            self.storage.save(data)
        if products and not succeeded:
            log("Kein Produkt konnte abgerufen werden.")
            return 1
        return 0

    def _fetch_all(self, product: Product) -> list:
        """Alle Ergebnisseiten laden, doppelte Anzeigen entfernen."""
        unique = {}
        for page in range(1, product.max_pages + 1):
            if page > 1:
                time.sleep(random.uniform(2, 4))  # höflich bleiben
            items = self.parser.parse(self.client.fetch(self.client.build_url(product, page)))
            if not items:
                break
            for item in items:
                unique[item.ad_id] = item
        if not unique:
            raise ScrapeError("Keine Anzeigen gefunden (Layout geändert oder Zugriff blockiert?)")
        return list(unique.values())

    def _process(self, product: Product, data: dict) -> None:
        history = ProductHistory(data["products"].get(product.id))
        listings = self.analyzer.filter(self._fetch_all(product), product)
        if not listings:
            log("  Nach dem Filtern bleiben keine Anzeigen übrig (Filter zu streng?)")
            return

        now = now_iso()
        baseline = history.baseline_median(BASELINE_DAYS)  # vor dem neuen Messwert!
        snapshot = self.analyzer.snapshot(listings, now)
        threshold = self.analyzer.threshold(product, baseline, snapshot["median"], snapshot["count"])
        deals = self.analyzer.deals(listings, threshold)
        new_deals = [d for d in deals if d.ad_id not in history.notified]

        history.add_snapshot(snapshot)
        history.update_listings(listings, now)
        history.threshold = threshold
        history.baseline = baseline

        log(f"  {snapshot['count']} Anzeigen, Median {fmt_eur(snapshot['median'])}, "
            f"günstigste {fmt_eur(snapshot['min'])}, gute Angebote: {len(deals)} "
            f"(neu: {len(new_deals)})")
        self._notify(product, new_deals, snapshot, history)
        data["products"][product.id] = history.to_dict()

    def _notify(self, product: Product, new_deals: list, snapshot: dict,
                history: ProductHistory) -> None:
        """Neue gute Angebote melden und als gemeldet merken."""
        if not new_deals or not self.notifier.enabled:
            return
        sent = []
        for deal in new_deals[:MAX_PUSH_PER_PRODUCT]:
            ok = self.notifier.send(
                title=f"{product.name}: {fmt_eur(deal.price)}",
                message=f"{deal.title}\nMarkt-Median aktuell {fmt_eur(snapshot['median'])}",
                click=deal.url)
            if ok:
                sent.append(deal.ad_id)
        rest = new_deals[MAX_PUSH_PER_PRODUCT:]
        if rest and self.notifier.send(
                title=f"{product.name}: {len(rest)} weitere gute Angebote",
                message="Details in der Preiswächter-App."):
            sent += [d.ad_id for d in rest]
        history.mark_notified(sent)


def main() -> int:
    watcher = Watcher(
        client=KleinanzeigenClient(),
        parser=ListingParser(),
        analyzer=PriceAnalyzer(),
        storage=Storage(DATA_FILE),
        notifier=Notifier(os.environ.get("NTFY_TOPIC")),
    )
    return watcher.run()


if __name__ == "__main__":
    sys.exit(main())
