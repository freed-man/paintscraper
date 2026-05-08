"""
Scrapy spider for https://www.automotivetouchup.com/

Filters: year, make, model (all optional)
Outputs: year, manufacturer, model, color_name, color_codes

Usage:
    scrapy crawl atu -a years=2024 -a makes=volkswagen -a models=golf -o colors.json
    scrapy crawl atu -a years=2014 -a makes=volkswagen -a models=golf,jetta -o colors.json
    scrapy crawl atu -a years=2024 -a makes=volkswagen,ford -o multi.json
    scrapy crawl atu -a makes=volkswagen -o vw_all.json
    scrapy crawl atu -o all_colors.json          # entire site (slow!)
"""

import scrapy
from urllib.parse import urlparse

# URL path depth constants
DEPTH_MAKE = 2   # /touch-up-paint/{make}/
DEPTH_YEAR = 3   # /touch-up-paint/{make}/{year}/
DEPTH_MODEL = 4  # /touch-up-paint/{make}/{year}/{model}/

GRID_SELECTOR = "[id*='ColorResultsGridView']"
ROW_SELECTOR = "tr[data-color-result-id]"
NAME_SELECTOR = "span::text"


class AtuSpider(scrapy.Spider):

    name = "atu"
    allowed_domains = ["www.automotivetouchup.com"]
    start_urls = ["https://www.automotivetouchup.com/touch-up-paint/"]

    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "LOG_LEVEL": "INFO",

        # -- Anti-blocking: slower, randomised, single connection -----------
        "DOWNLOAD_DELAY": 3,                    # 1.5s – 4.5s random range
        "RANDOMIZE_DOWNLOAD_DELAY": True,
        "CONCURRENT_REQUESTS": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,

        # -- Autothrottle: backs off automatically if server slows down -----
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 3,
        "AUTOTHROTTLE_MAX_DELAY": 30,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0,

        # -- Rotate user agents via scrapy-fake-useragent -------------------
        "DOWNLOADER_MIDDLEWARES": {
            "scrapy.downloadermiddlewares.useragent.UserAgentMiddleware": None,
            "scrapy_fake_useragent.middleware.RandomUserAgentMiddleware": 400,
        },

        # -- Browser-like headers -------------------------------------------
        "DEFAULT_REQUEST_HEADERS": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        },

        # -- Disable cookies (avoids session tracking) ----------------------
        "COOKIES_ENABLED": False,
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
    def _path_parts(url):
        """Extract path segments from an absolute or relative URL."""
        path = urlparse(url).path if "://" in url else url
        return [p for p in path.strip("/").split("/") if p]

    @staticmethod
    def _format_name(slug):
        """
        Convert a URL slug to a display name.
        'mercedes-benz' → 'Mercedes-Benz', 'bmw' → 'BMW', 'gmc' → 'GMC'
        """
        upper = {"bmw", "gmc", "mg"}
        if slug in upper:
            return slug.upper()
        return slug.replace("-", " ").title()

    def _unique_links(self, response, depth, manufacturer=None, year=None):
        """
        Yield deduplicated (href, slug) pairs at a given URL depth.
        depth=2 → makes, depth=3 → years, depth=4 → models
        """
        seen = set()
        for href in response.css("a::attr(href)").getall():
            parts = self._path_parts(href)
            if len(parts) != depth or parts[0] != "touch-up-paint":
                continue
            if manufacturer and parts[1].lower() != manufacturer:
                continue
            if year and parts[2] != year:
                continue

            slug = parts[depth - 1].lower()
            if slug in seen:
                continue
            seen.add(slug)
            yield href, slug

    # -- Level 1: Makes ---------------------------------------------------

    def parse(self, response):
        for href, make_slug in self._unique_links(response, DEPTH_MAKE):
            if self.filter_makes and make_slug not in self.filter_makes:
                continue
            self.logger.info(f"Found make: {make_slug}")
            yield response.follow(
                href,
                callback=self.parse_make,
                cb_kwargs={"manufacturer": make_slug},
            )

    # -- Level 2: Years ----------------------------------------------------

    def parse_make(self, response, manufacturer):
        for href, year_slug in self._unique_links(
            response, DEPTH_YEAR, manufacturer=manufacturer
        ):
            if not year_slug.isdigit():
                continue
            if self.filter_years and year_slug not in self.filter_years:
                continue
            yield response.follow(
                href,
                callback=self.parse_year,
                cb_kwargs={"manufacturer": manufacturer, "year": year_slug},
            )

    # -- Level 3: Models ---------------------------------------------------

    def parse_year(self, response, manufacturer, year):
        for href, model_slug in self._unique_links(
            response, DEPTH_MODEL, manufacturer=manufacturer, year=year
        ):
            if model_slug == "all-models":
                continue
            if self.filter_models and model_slug not in self.filter_models:
                continue
            yield response.follow(
                href,
                callback=self.parse_colors,
                cb_kwargs={
                    "manufacturer": manufacturer,
                    "year": year,
                    "model": model_slug,
                },
            )

    # -- Level 4: Colors ---------------------------------------------------

    def parse_colors(self, response, manufacturer, year, model):
        grid = response.css(GRID_SELECTOR)
        if not grid:
            self.logger.warning(f"No color table on {response.url}")
            return

        for row in grid.css(ROW_SELECTOR):
            cells = row.css("td")
            if len(cells) < 3:
                continue

            color_spans = cells[2].css(NAME_SELECTOR).getall()
            color_name = ", ".join(s.strip() for s in color_spans if s.strip())
            if not color_name:
                continue

            # Full code list lives in the chip image alt attribute
            alt_text = cells[0].css("img::attr(alt)").get("")
            codes = [c.strip() for c in alt_text.split(",") if c.strip()]

            yield {
                "year": int(year),
                "manufacturer": self._format_name(manufacturer),
                "model": self._format_name(model),
                "color_name": color_name,
                "color_codes": ", ".join(codes),
            }