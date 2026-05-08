"""
Scrapy spider for https://chipex.co.uk/

Filters: year, make, model (all optional)
Outputs: year, manufacturer, model, color_name, color_codes

URL pattern: /brands/{make}/{model}/?year={year}

Usage:
    scrapy crawl chipex -a years=2025 -a makes=volkswagen -a models=id-3 -o chipex_colors.json
    scrapy crawl chipex -a years=2024 -a makes=volkswagen -a models=golf,jetta -o chipex_colors.json
    scrapy crawl chipex -a years=2024 -a makes=volkswagen -o chipex_vw_2024.json
    scrapy crawl chipex -a makes=volkswagen -o chipex_vw_all.json
    scrapy crawl chipex -o chipex_all.json         # entire site (slow!)

Note:
    Chipex lists colors per make (not per model). The same color page at
    /brands/{make}/{model}/?year={year} shows all colors for that make+year
    regardless of model. This means different models for the same make+year
    will return identical color lists — this is how Chipex structures its data.

    Chipex uses hyphens in URL slugs (id-3, id-buzz) while ATU uses dots
    (id.3, id.buzz). Use the correct slug format for each spider.

    When no year filter is given, the spider visits each model page to
    discover available years from the site (no hardcoded year range).
"""

import re
import scrapy
from urllib.parse import urlparse

# Slugs to skip — not real models
SKIP_SLUGS = {"all-models", "colours", "colors"}

# Pattern for valid years (1950–2099)
YEAR_PATTERN = re.compile(r"^(19[5-9]\d|20\d{2})$")


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
    }

    def __init__(self, years=None, makes=None, models=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filter_years = set(
            y.strip() for y in years.split(",")
        ) if years else set()
        self.filter_makes = set(
            m.strip().lower() for m in makes.split(",")
        ) if makes else set()
        self.filter_models = set(
            m.strip().lower() for m in models.split(",")
        ) if models else set()

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

    # -- Level 1: Makes ---------------------------------------------------

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
                yield response.follow(
                    href,
                    callback=self.parse_make,
                    cb_kwargs={"manufacturer": make_slug},
                )

    # -- Level 2: Models ---------------------------------------------------

    def parse_make(self, response, manufacturer):
        seen = set()
        for href in response.css("a::attr(href)").getall():
            parts = self._path_parts(href)
            if (
                len(parts) == 3
                and parts[0] == "brands"
                and parts[1].lower() == manufacturer
            ):
                model_slug = parts[2].lower()
                if model_slug in seen:
                    continue
                seen.add(model_slug)
                if model_slug in SKIP_SLUGS:
                    continue
                if self.filter_models and model_slug not in self.filter_models:
                    continue

                if self.filter_years:
                    # Years specified — skip discovery, go straight to colors
                    for year in sorted(self.filter_years):
                        url = response.urljoin(
                            href.rstrip("/") + f"/?year={year}"
                        )
                        yield scrapy.Request(
                            url,
                            callback=self.parse_colors,
                            cb_kwargs={
                                "manufacturer": manufacturer,
                                "model": model_slug,
                                "year": year,
                            },
                        )
                else:
                    # No year filter — visit model page to discover years
                    yield response.follow(
                        href,
                        callback=self.parse_model_years,
                        cb_kwargs={
                            "manufacturer": manufacturer,
                            "model": model_slug,
                            "href": href,
                        },
                    )

    # -- Level 2b: Discover years from model page --------------------------

    def parse_model_years(self, response, manufacturer, model, href):
        """
        Extract available years from the year filter dropdown on the
        model page. The dropdown contains plain text year values like
        "2026", "2025", etc. We extract only from the filter section
        to avoid picking up unrelated years from the page footer.
        """
        years = set()

        # The year filter is a dropdown/select-style element.
        # Years appear as standalone text nodes that are exactly 4 digits.
        # We target elements near the "Filter by:" / "Year" section.
        for el in response.css(".swatch-filter ::text, .year-filter ::text"):
            text = el.get().strip()
            if YEAR_PATTERN.match(text):
                years.add(text)

        # Fallback: if no years found via filter classes, scan all text
        # but only accept years that appear as standalone text nodes
        # (not embedded in sentences like "since 2009")
        if not years:
            for text in response.css("::text").getall():
                text = text.strip()
                if YEAR_PATTERN.match(text) and len(text) == 4:
                    years.add(text)

        if not years:
            self.logger.warning(
                f"No years found on {response.url} — skipping {model}"
            )
            return

        self.logger.info(
            f"Found {len(years)} years for {manufacturer}/{model}: "
            f"{sorted(years)}"
        )

        base_url = response.urljoin(href.rstrip("/"))
        for year in sorted(years):
            yield scrapy.Request(
                f"{base_url}/?year={year}",
                callback=self.parse_colors,
                cb_kwargs={
                    "manufacturer": manufacturer,
                    "model": model,
                    "year": year,
                },
            )

    # -- Level 3: Colors ---------------------------------------------------

    def parse_colors(self, response, manufacturer, model, year):
        """
        Each color swatch is an <a> link containing:
            <h6 class="text-center mb-2">COLOR NAME</h6>
            <p class="text-colour-code">CODE1;CODE2;CODE3</p>

        For colors without a swatch image, there's an extra h6 with
        class "txt-no-colour" showing "No Swatch Available" — the
        :not() selector skips it and grabs the real color name.
        """
        for link in response.css("a[href*='/brands/']"):
            href = link.attrib.get("href", "")
            parts = self._path_parts(href)

            if len(parts) != 3 or parts[0] != "brands":
                continue
            if parts[1].lower() != manufacturer:
                continue

            # Color name — skip the "No Swatch Available" placeholder
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

            # Hex color from the swatch div's background style
            # e.g. style="background: #851c20"
            # "No Swatch Available" uses #f2f2f2 / #ededed — treat as blank
            color_hex = ""
            swatch_style = link.css("div.color-swatch::attr(style)").get("")
            if "background:" in swatch_style:
                hex_match = swatch_style.split("background:")[1].strip().split(";")[0].strip()
                if hex_match.startswith("#") and hex_match.lower() not in ("#f2f2f2", "#ededed"):
                    color_hex = hex_match

            yield {
                "year": int(year),
                "manufacturer": self._format_name(manufacturer),
                "model": self._format_name(model),
                "color_name": color_name,
                "color_codes": ", ".join(codes),
                "color_hex": color_hex,
            }
