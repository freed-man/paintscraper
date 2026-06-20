"""
Scrapy spider for https://code.peinturevoiture.fr/

Scrapes every paint colour for each brand in a single request per brand,
using the brand's "toutes les couleurs" (all colours) page.

Filters: makes (optional)
Outputs: manufacturer, color_name, color_codes, color_hex, year

URL patterns:
    Brands index:  /marques
    Brand (all):   /marque/{id}-{slug}/toutes

How it works:
    /marques is an A-Z index of brands. Each brand links either straight
    to its /marque/{id}-{slug}/toutes page (small brands) or to a brand
    landing page /marque/{id}-{slug} (big brands) that in turn links to
    /toutes. Either way the /toutes page lists the brand's COMPLETE set
    of codes (every model, every year) in one static HTML table, so we go
    directly to /toutes per brand -- appending '/toutes' to the marque URL
    when the index didn't already point there. Ford's /toutes page alone
    carries ~1,665 codes in one response; there is no pagination.

    Each row of the table is:
        <img>  swatch thumbnail  (an image, /images/teintes/mini/{slug}.png,
                                  or a CP_IMG_NOT_AVAILABLE placeholder)
        <a>    "{CODE} - {NAME}"  linking to a /{numeric-id}.html detail page
        Années  year range        e.g. "1981 - 1984", "1982 -", "2025 -", "-"
        Bicouches / Brillant Direct  finish markers (not captured for now)

    Code vs name: the link text is "{CODE} - {NAME}" separated by " - "
    (space-hyphen-space). Codes themselves may contain hyphens WITHOUT
    surrounding spaces ("3D-HA2-PA2 - STERLING GREY", "9B-FW9 - OLYMPIC
    WHITE") or slashes ("785/A - STORM GREY"), so we split on the FIRST
    " - " only: everything before is the code, everything after is the name.

    The code is emitted verbatim (e.g. "3D-HA2-PA2"), NOT split on its
    internal hyphens. Some brands join several equivalent cross-reference
    codes with hyphens; whether to split those is a merge-time decision
    (see merge_databases.py and the note below), not the spider's job --
    the project rule is that punctuation in codes is often semantic
    (Renault encodes a matt finish in a dot, dashes mark variants), so the
    spider extracts faithfully and lets the merge own code normalisation.

    Hex: the swatch is an image, not a value in the HTML, so color_hex is
    left blank. This source is added for code/name cross-validation and its
    year ranges, not for swatches (the other two sources already give ~92%
    hex coverage). Deriving hex by averaging the thumbnail pixels was
    judged not worth it.

    Year: the Années column is captured into a `year` field -- unique to
    this source. The current merge does not read `year` (it is year-blind),
    so this sits in the raw output for now; it is the lever that could later
    refine the name->code direction for genuinely-different-paint cases.

    Make name: taken from the index link text (the display name, e.g.
    "Fiat", "Ford Europe", "Alfa Romeo"), NOT the URL slug -- the slug is
    sometimes off ("fiat-group" for Fiat), which would split the make in
    the merge. Cross-source make alignment (e.g. peinturevoiture
    "Ferrari-Maserati" vs others' "Ferrari") is the merge's job via aliases.

Politeness:
    ROBOTSTXT_OBEY is True, 1s delay, honest UA -- same as the other two
    spiders. robots.txt is respected at runtime; if the site disallows
    these paths the run log will say so.

Usage:
    python -m scrapy crawl peinturevoiture -o peinturevoiture_full.json
    python -m scrapy crawl peinturevoiture -a makes=citroen -o pv_citroen.json
    python -m scrapy crawl peinturevoiture -a makes=ford-europe,citroen -o pv_sel.json

Logging:
    LOG_FILE writes the full run log to peinturevoiture_log.txt in the
    working directory on every run. Note: with LOG_FILE set, Scrapy sends
    the log to the file rather than the console, so the terminal stays quiet
    during the crawl. To watch it live AND keep the file, drop the LOG_FILE
    line and instead run:
        python -m scrapy crawl peinturevoiture -o peinturevoiture_full.json
            2>&1 | Tee-Object peinturevoiture_log.txt
"""

import re
import scrapy
from urllib.parse import urlparse


class PeintureVoitureSpider(scrapy.Spider):

    name = "peinturevoiture"
    allowed_domains = ["code.peinturevoiture.fr"]
    start_urls = ["https://code.peinturevoiture.fr/marques"]

    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "DOWNLOAD_DELAY": 1.0,
        "CONCURRENT_REQUESTS": 4,
        "USER_AGENT": "PaintScraper (personal learning project)",
        "LOG_LEVEL": "INFO",
        "LOG_FILE": "peinturevoiture_log.txt",
    }

    # Matches a colour detail-page link, e.g. /444.html or /109579.html
    DETAIL_RE = re.compile(r"/\d+\.html")
    # A real model year, used to locate the Années cell
    YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

    def __init__(self, makes=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filter_makes = set(
            m.strip().lower() for m in makes.split(",")
        ) if makes else set()

    # -- Helpers ----------------------------------------------------------

    @staticmethod
    def _parse_marque(href):
        """
        '/marque/4-amc-jeep/toutes' -> ('4', 'amc-jeep')
        '/marque/685-abarth'        -> ('685', 'abarth')
        Returns (id, slug) or (None, None) if not a brand link.
        """
        path = urlparse(href).path.rstrip("/")
        if path.endswith("/toutes"):
            path = path[: -len("/toutes")]
        m = re.search(r"/marque/(\d+)-(.+)$", path)
        return (m.group(1), m.group(2)) if m else (None, None)

    @staticmethod
    def _toutes_url(href):
        """Return the /toutes form of a marque href (idempotent)."""
        clean = href.rstrip("/")
        if clean.endswith("/toutes"):
            return clean
        return clean + "/toutes"

    @staticmethod
    def _display_name(link, slug):
        """Make display name from the link text, stripping 'Code Peinture '."""
        raw = " ".join(link.css("::text").getall())
        raw = re.sub(r"\s+", " ", raw).strip()
        name = re.sub(r"^code peinture\s+", "", raw, flags=re.IGNORECASE).strip()
        return name or slug.replace("-", " ").title()

    def _extract_year(self, row):
        """Find the Années cell (the non-link cell containing a real year)."""
        for td in row.xpath("./td"):
            # skip the colour-name cell (it holds the detail-page link)
            if td.xpath(".//a[contains(@href, '.html')]"):
                continue
            txt = re.sub(r"\s+", " ", " ".join(td.xpath(".//text()").getall())).strip()
            if self.YEAR_RE.search(txt):
                return txt
        return ""

    # -- Level 1: Brands --> straight to each brand's /toutes page ---------

    def parse(self, response):
        seen = set()
        for link in response.css("a[href*='/marque/']"):
            href = link.attrib.get("href", "")
            marque_id, slug = self._parse_marque(href)
            if not slug or (marque_id, slug) in seen:
                continue
            seen.add((marque_id, slug))

            name = self._display_name(link, slug)
            if self.filter_makes and not (
                slug in self.filter_makes or name.lower() in self.filter_makes
            ):
                continue

            self.logger.info(f"Found make: {name} ({slug})")
            yield response.follow(
                self._toutes_url(href),
                callback=self.parse_colors,
                cb_kwargs={"manufacturer": name},
            )

    # -- Level 2: Colours -------------------------------------------------

    def parse_colors(self, response, manufacturer):
        # Each colour is a <tr> containing an <a> to a /{id}.html detail page.
        rows = response.xpath("//tr[.//a[contains(@href, '.html')]]")
        count = 0
        for row in rows:
            link = row.xpath(".//a[contains(@href, '.html')]")
            if not link:
                continue
            link = link[0]
            href = link.attrib.get("href", "")
            if not self.DETAIL_RE.search(href):
                continue

            text = re.sub(r"\s+", " ", " ".join(link.css("::text").getall())).strip()
            if not text:
                continue

            # Split code / name on the FIRST " - " only.
            if " - " in text:
                code, name = text.split(" - ", 1)
                code, name = code.strip(), name.strip()
            else:
                code, name = text, ""
            if not code:
                continue

            count += 1
            yield {
                "manufacturer": manufacturer,
                "color_name": name,
                "color_codes": code,
                "color_hex": "",          # swatch is an image; intentionally blank
                "year": self._extract_year(row),
            }

        if count == 0:
            self.logger.warning(f"No colours found on {response.url}")
