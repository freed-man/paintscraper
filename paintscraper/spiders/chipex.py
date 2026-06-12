"""
Scrapy spider for https://chipex.co.uk/

Scrapes every paint colour for each brand in a single request per brand,
using the brand's "All Models" page.

Outputs: manufacturer, color_name, color_codes, color_hex
Filters: make (optional)

URL pattern: /brands/{make}/all-models/

How it works:
    /brands/ is an A-Z index of brands. Each brand has an
    /brands/{make}/all-models/ page that lists the brand's COMPLETE set
    of colours (every model, every year) in one static page. A normal
    model page (/brands/{make}/{model}/) is just that same all-models
    list filtered down to one model, so all-models is the superset by
    construction — scraping it per brand captures everything the old
    per-model / per-year crawl did, with ~84 requests instead of ~2,400.

    Verified end-to-end: a full run returns 22,462 colours across 85
    brands in ~2 minutes, with per-make counts matching the old
    per-model union exactly (Acura 505, Toyota 728, Volkswagen 595).

    The site's year dropdown is a server-side (WordPress admin-ajax)
    filter, not data embedded in the page, so year is not recoverable
    without an extra request per year. We scrape year-blind. Model is
    not available at the all-models level and is not needed downstream
    (the fill DB keys on make+code and make+name, never model).

    Chipex uses hyphens in URL slugs. The 'vw' slug has no all-models
    page (404) and is harmless — 'volkswagen' carries the VW colours.

    Colours with no swatch image carry a #f2f2f2 / #ededed placeholder
    background; their name + codes are kept and the hex is left blank.

Logging:
    LOG_FILE writes the full run log to chipex_log.txt in the working
    directory on every run. Note: with LOG_FILE set, Scrapy sends the
    log to the file rather than the console, so the terminal stays quiet
    during the crawl. To watch it live AND keep the file, drop the
    LOG_FILE line and instead run:
        python -m scrapy crawl chipex -o chipex_all.json 2>&1 |
            Tee-Object chipex_log.txt
"""

import scrapy
from urllib.parse import urlparse


class ChipexSpider(scrapy.Spider):

    name = "chipex"
    allowed_domains = ["chipex.co.uk"]
    start_urls = ["https://chipex.co.uk/brands/"]

    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "DOWNLOAD_DELAY": 1.0,
        "CONCURRENT_REQUESTS": 4,
        "USER_AGENT": "PaintScraper (personal learning project)",
        "LOG_LEVEL": "INFO",
        "LOG_FILE": "chipex_log.txt",
    }

    def __init__(self, makes=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filter_makes = set(
            m.strip().lower() for m in makes.split(",")
        ) if makes else set()

    # -- Helpers ----------------------------------------------------------

    @staticmethod
    def _format_name(slug):
        """Convert a URL slug to a display name."""
        upper = {"bmw", "gmc", "mg"}
        if slug in upper:
            return slug.upper()
        return slug.replace("-", " ").title()

    @staticmethod
    def _path_parts(url):
        """Extract path segments from a URL."""
        path = urlparse(url).path if "://" in url else url
        return [p for p in path.strip("/").split("/") if p]

    # -- Level 1: Brands --> straight to each brand's all-models page ------

    def parse(self, response):
        seen = set()
        for href in response.css("a::attr(href)").getall():
            parts = self._path_parts(href)
            if len(parts) == 2 and parts[0] == "brands":
                make_slug = parts[1].lower()
                if make_slug in seen:
                    continue
                seen.add(make_slug)
                if self.filter_makes and make_slug not in self.filter_makes:
                    continue
                self.logger.info(f"Found make: {make_slug}")
                yield scrapy.Request(
                    response.urljoin(f"/brands/{make_slug}/all-models/"),
                    callback=self.parse_colors,
                    cb_kwargs={"manufacturer": make_slug},
                )

    # -- Level 2: Colours -------------------------------------------------

    def parse_colors(self, response, manufacturer):
        """
        Each colour swatch is an <a> link containing:
            <div class="color-swatch" style="background: #173a5a"></div>
            <h6 class="text-center mb-2">COLOR NAME</h6>
            <p class="text-colour-code">CODE1;CODE2;CODE3</p>

        Non-swatch /brands/ links (nav, model menu, brand logos) lack the
        inner colour-name structure and are skipped automatically.
        Colours with no swatch image use an extra h6.txt-no-colour
        ("No Swatch Available") plus a #f2f2f2 / #ededed background —
        :not() skips the placeholder name and the hex is left blank.
        """
        count = 0
        for link in response.css("a[href*='/brands/']"):
            href = link.attrib.get("href", "")
            parts = self._path_parts(href)

            if len(parts) != 3 or parts[0] != "brands":
                continue
            if parts[1].lower() != manufacturer:
                continue

            # Colour name — skip the "No Swatch Available" placeholder
            name_el = link.css(
                "h6.text-center.mb-2:not(.txt-no-colour)::text"
            ).get()
            if not name_el:
                continue

            color_name = name_el.strip()
            if not color_name:
                continue

            # Codes — semicolon-separated in a <p> tag
            code_text = link.css("p.text-colour-code::text").get("")
            codes = [c.strip() for c in code_text.split(";") if c.strip()]

            # Hex from the swatch div's background style
            # e.g. style="background: #173a5a"
            # "No Swatch Available" uses #f2f2f2 / #ededed — treat as blank
            color_hex = ""
            swatch_style = link.css("div.color-swatch::attr(style)").get("")
            if "background:" in swatch_style:
                hex_match = swatch_style.split("background:")[1].strip().split(";")[0].strip()
                if hex_match.startswith("#") and hex_match.lower() not in ("#f2f2f2", "#ededed"):
                    color_hex = hex_match

            count += 1
            yield {
                "manufacturer": self._format_name(manufacturer),
                "color_name": color_name,
                "color_codes": ", ".join(codes),
                "color_hex": color_hex,
            }

        if count == 0:
            self.logger.warning(f"No colours found on {response.url}")