"""
Scrapy spider for https://colorndrive.com/

Filters: makes, models (all optional)
Outputs: manufacturer, model, color_name, color_codes, color_hex, color_hex_2

URL patterns:
    Brands list:  /en/car-touch-up-paint  (section #brands)
    Brand page:   /en/{make-slug}-touch-up-paint
    Model colors: /en/touch-up-paint/{make-slug}-{model-slug}   (30 per page, ?p=N)

Note:
    ColorNDrive does not structure colors by year. Colors are listed per
    model (all years combined). The spider does not output a year field —
    the merge script should handle this accordingly. (The model page has a
    Year filter, but it only narrows the list; there is no per-colour year
    to read.)

    The site also provides alternative regional color names (German,
    Portuguese, French, etc.) and hex values via CSS background gradients.

Swatch (color_hex, color_hex_2):
    Each swatch is a two-stop gradient,
        background: linear-gradient(to right, #9E2528 , #500D0B);
    For solid paints both stops are the same; for metallics and pearls
    they are not: a light stop then a dark one (Sunsetred Metallic above).
    On the VW Golf pages checked on 2026-10-27, 48 of 60 swatches had two
    different stops. color_hex is the FIRST stop, exactly as every earlier
    run of this spider recorded it, so it stays comparable with past
    scrapes; color_hex_2 is the second stop as served. Neither is a
    derived value. Which one (if either) represents the paint is a
    decision for whoever uses the data, not for the spider.

Pace (measured):
    The last full run (2026-06-12) made 8,627 requests in 4 h 9 min at one
    request at a time, with 5 retries (four Cloudflare 524 timeouts and one
    502) and no refusals. Pages take the site a second or more to build,
    so most of that time was spent waiting on single responses.

    This spider keeps the same minimum gap between requests (DOWNLOAD_DELAY
    1 s, randomised 0.5-1.5 s) but allows a second request to be in flight
    while a slow one finishes (CONCURRENT_REQUESTS_PER_DOMAIN 2,
    AUTOTHROTTLE_TARGET_CONCURRENCY 2.0). AutoThrottle widens the gap by
    itself if the site slows down, and the PolitenessGuard backs off on any
    refusal. Expect roughly 2 to 2.5 hours if the site answers as it did
    last time. If the log shows the guard slowing down more than a handful
    of times, go back to one at a time:
        -s CONCURRENT_REQUESTS_PER_DOMAIN=1

robots.txt (revised by the site 2026-08-16, checked 2026-10-27):
    Our user agent falls under "User-agent: *", which only closes /admin/,
    /backoffice/, /cart/, /checkout/, /login/, /register/ and /user/, with
    no crawl delay. The site's AI-crawler group (ClaudeBot, GPTBot ...) gets
    Crawl-delay 10 and no ?p= pagination; named "aggressive" bots are shut
    out and, per the file, blocked at Cloudflare. The site is behind
    Cloudflare, which is why politeness matters most here.

Run:
    python -m scrapy crawl colorndrive -O colorndrive_full.json
    python -m scrapy crawl colorndrive -a makes=volkswagen -O colorndrive_vw.json
    python -m scrapy crawl colorndrive -a makes=volkswagen -a models=id3,golf -O colorndrive_vw_sel.json
    (-O overwrites; -o would append a second JSON array to an existing
    file and corrupt it.)

Pausing and resuming the long run (optional):
    With a job directory Scrapy can stop on one Ctrl+C and carry on later
    from where it stopped. Use JSON Lines with -o (append) for this, never
    -O: a resumed run with -O would overwrite the first part.
        python -m scrapy crawl colorndrive -o colorndrive_full.jsonl -s JOBDIR=crawls/colorndrive
    Run the same command again to resume. When it finishes, turn it into
    the usual JSON array:
        python -c "import json; rows=[json.loads(l) for l in open('colorndrive_full.jsonl', encoding='utf-8') if l.strip()]; json.dump(rows, open('colorndrive_full.json', 'w', encoding='utf-8'), ensure_ascii=False)"
    Delete crawls/colorndrive (and the .jsonl) before the next fresh scrape.

Logging:
    LOG_FILE writes the full run log to colorndrive_log.txt in the working
    directory, so the terminal stays quiet during the crawl. To watch it
    live AND keep the file, drop the LOG_FILE line and instead run:
        python -m scrapy crawl colorndrive -O colorndrive_full.json 2>&1 |
            Tee-Object colorndrive_log.txt
"""

import re
import scrapy


HEX = re.compile(r"#([0-9a-fA-F]{6})")


class ColorNDriveSpider(scrapy.Spider):

    name = "colorndrive"
    allowed_domains = ["colorndrive.com"]
    start_urls = ["https://colorndrive.com/en/car-touch-up-paint"]

    # User agent, robots.txt and back-off are the house defaults in
    # settings.py. Only the pace differs; see "Pace" above.
    custom_settings = {
        "CONCURRENT_REQUESTS_PER_DOMAIN": 2,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 2.0,
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
    def _extract_hexes(style):
        """
        The two stops of the swatch gradient, as served.
        'background: linear-gradient(to right, #B7B7B1 , #484948);'
        → ('#B7B7B1', '#484948')
        A missing stop comes back as ''.
        """
        found = [f"#{h}" for h in HEX.findall(style or "")]
        first = found[0] if found else ""
        second = found[1] if len(found) > 1 else ""
        return first, second

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
            .color-list-page__item__color-pane style → swatch gradient stops

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

            # Swatch gradient: both stops, as served
            style = item.css(
                ".color-list-page__item__color-pane::attr(style)"
            ).get("")
            color_hex, color_hex_2 = self._extract_hexes(style)

            yield {
                "manufacturer": self._format_name(manufacturer),
                "model": model,
                "color_name": full_name,
                "color_codes": ", ".join(codes),
                "color_hex": color_hex,
                "color_hex_2": color_hex_2,
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
