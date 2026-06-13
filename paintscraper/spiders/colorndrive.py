"""
Scrapy spider for https://colorndrive.com/

Filters: makes, models (all optional)
Outputs: manufacturer, model, color_name, color_codes, color_hex

URL patterns:
    Brands list:  /en/car-touch-up-paint  (section #brands)
    Brand page:   /en/{make-slug}-touch-up-paint
    Model colors: /en/touch-up-paint/{make-slug}-{model-slug}

Note:
    ColorNDrive does not structure colors by year. Colors are listed per
    model (all years combined). The spider does not output a year field —
    the merge script should handle this accordingly.

    The site also provides alternative regional color names (German,
    Portuguese, French, etc.) and hex values via CSS background gradients.

Usage:
    scrapy crawl colorndrive -o colorndrive_full.json
    scrapy crawl colorndrive -a makes=volkswagen -o colorndrive_vw.json
    scrapy crawl colorndrive -a makes=volkswagen -a models=id3,golf -o colorndrive_vw_sel.json

Logging:
    LOG_FILE writes the full run log to colorndrive_log.txt in the working
    directory on every run. Note: with LOG_FILE set, Scrapy sends the log
    to the file rather than the console, so the terminal stays quiet during
    the crawl. To watch it live AND keep the file, drop the LOG_FILE line
    and instead run:
        python -m scrapy crawl colorndrive -o colorndrive_full.json 2>&1 |
            Tee-Object colorndrive_log.txt
"""

import re
import scrapy


class ColorNDriveSpider(scrapy.Spider):

    name = "colorndrive"
    allowed_domains = ["colorndrive.com"]
    start_urls = ["https://colorndrive.com/en/car-touch-up-paint"]

    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "DOWNLOAD_DELAY": 1.0,
        "CONCURRENT_REQUESTS": 4,
        "USER_AGENT": "PaintScraper (personal learning project)",
        "LOG_LEVEL": "INFO",
        "LOG_FILE": "colorndrive_log.txt",
    }

    def __init__(self, makes=None, models=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filter_makes = set(
            m.strip().lower() for m in makes.split(",")
        ) if makes else set()
        self.filter_models = set(
            m.strip().lower() for m in models.split(",")
        ) if models else set()

    # -- Helpers ----------------------------------------------------------

    @staticmethod
    def _format_name(text):
        """
        Convert an uppercase or slug name to a display name.
        'VOLKSWAGEN' → 'Volkswagen', 'BMW' → 'BMW', 'GMC' → 'GMC'
        """
        upper = {"bmw", "gmc", "mg", "hks", "dkw", "nsu", "lml", "lti",
                 "tvr", "umm", "rvi", "fso", "daf", "jac", "nio", "aro",
                 "amc", "awd", "bmc", "geo", "ral", "sma", "sao", "zaz"}
        if text.strip().lower() in upper:
            return text.strip().upper()
        return text.strip().title()

    @staticmethod
    def _extract_hex(style):
        """
        Extract a hex color from a CSS gradient style string.
        'background: linear-gradient(to right, #616568 , #616568);'
        → '#616568'
        """
        match = re.search(r"#([0-9a-fA-F]{6})", style or "")
        return f"#{match.group(1)}" if match else ""

    @staticmethod
    def _make_slug_from_href(href):
        """
        Extract the make slug from a brand page href.
        '/en/volkswagen-touch-up-paint' → 'volkswagen'
        '/en/ford-america-touch-up-paint' → 'ford-america'
        """
        path = href.strip("/").split("/")[-1]
        return path.replace("-touch-up-paint", "")

    # -- Level 1: Makes ---------------------------------------------------

    def parse(self, response):
        """Parse the main brands page to discover all makes."""
        for link in response.css("#brands .model-list a"):
            href = link.attrib.get("href", "")
            if not href or "touch-up-paint" not in href:
                continue

            make_slug = self._make_slug_from_href(href)
            make_name = link.css("::text").get("").strip()

            if self.filter_makes and make_slug not in self.filter_makes:
                continue

            self.logger.info(f"Found make: {make_slug}")
            yield response.follow(
                href,
                callback=self.parse_make,
                cb_kwargs={
                    "manufacturer": make_name,
                    "make_slug": make_slug,
                },
            )

    # -- Level 2: Models ---------------------------------------------------

    def parse_make(self, response, manufacturer, make_slug):
        """Parse a brand page to discover all models."""
        for link in response.css(".model-list a.model-list__item"):
            href = link.attrib.get("href", "")
            if not href or "/touch-up-paint/" not in href:
                continue

            # Full slug is like 'volkswagen-id3'
            full_slug = href.strip("/").split("/")[-1]
            # Remove make prefix to get model slug
            prefix = make_slug + "-"
            if full_slug.startswith(prefix):
                model_slug = full_slug[len(prefix):]
            else:
                model_slug = full_slug

            if self.filter_models and model_slug not in self.filter_models:
                continue

            # Get model display name from link text (between the spans)
            all_text = link.css("::text").getall()
            span_text = [s.strip() for s in link.css("span::text").getall()]
            model_name = [
                t.strip() for t in all_text
                if t.strip() and t.strip() not in span_text
            ]
            model_display = " ".join(model_name) if model_name else self._format_name(model_slug)

            yield response.follow(
                href,
                callback=self.parse_colors,
                cb_kwargs={
                    "manufacturer": manufacturer,
                    "model": model_display,
                },
            )

    # -- Level 3: Colors ---------------------------------------------------

    def parse_colors(self, response, manufacturer, model):
        """
        Parse a model's color page. Each color is an <a> with:
            h4.color-list-page__item__title  → color codes ("LA7C / C2")
            h5.color-list-page__item__desc   → color name ("PEBBLE GREY METALLIC")
            ul.alternative-names li          → regional name variants
            .color-list-page__item__color-pane style → hex via gradient

        Handles pagination via ?p=N query parameter.
        """
        items = response.css("a.color-list-page__item")
        if not items:
            self.logger.warning(
                f"No colors found for {manufacturer} {model} on {response.url}"
            )
            return

        # Determine current page from URL
        current_page = response.url.split("p=")[-1] if "p=" in response.url else "1"
        self.logger.info(
            f"Found {len(items)} colors for {manufacturer} {model} "
            f"(page {current_page})"
        )

        for item in items:
            # Color codes
            code_text = item.css(
                "h4.color-list-page__item__title::text"
            ).get("")
            codes = [c.strip() for c in code_text.split("/") if c.strip()]

            # Primary color name
            color_name = item.css(
                "h5.color-list-page__item__desc::text"
            ).get("").strip()

            if not color_name:
                continue

            # Alternative regional names
            alt_names = []
            for li in item.css("ul.alternative-names li"):
                name_parts = li.css("::text").getall()
                name = " ".join(p.strip() for p in name_parts if p.strip())
                if name:
                    alt_names.append(name)

            # Build full name with alternatives
            all_names = [color_name] + alt_names
            full_name = ", ".join(all_names)

            # Hex from swatch gradient
            style = item.css(
                ".color-list-page__item__color-pane::attr(style)"
            ).get("")
            color_hex = self._extract_hex(style)

            yield {
                "manufacturer": self._format_name(manufacturer),
                "model": model,
                "color_name": full_name,
                "color_codes": ", ".join(codes),
                "color_hex": color_hex,
            }

        # Follow next page if it exists
        # Find the <li> immediately after the active page and follow its link,
        # but only if it's a real page link (not the » arrow).
        active = response.css("nav ul.pagination li.active")
        if active:
            next_li = active.xpath("following-sibling::li[1]")
            if next_li:
                next_href = next_li.css("a::attr(href)").get("")
                next_label = next_li.css("a::attr(aria-label)").get("")
                if (next_href
                        and "javascript" not in next_href
                        and next_label != "Next"):
                    yield response.follow(
                        next_href,
                        callback=self.parse_colors,
                        cb_kwargs={
                            "manufacturer": manufacturer,
                            "model": model,
                        },
                    )