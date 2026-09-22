"""
Scrapy spider for https://www.carcolourservices.co.uk/car-paint/

Scrapes the site's complete paint-code database in ONE request.

Outputs: manufacturer, color_name, color_codes
Filters: makes (optional), matched case-insensitively against the site's
         own make names, e.g.  -a makes="Audi,Fiat Auto"

How it works:
    The Paint Code Finder page is an AngularJS table (the site's
    "car-paint-system" WordPress plugin). The table is NOT filled by an
    API call: the page ships the ENTIRE database inline as a JavaScript
    array,

        var colorCodes = [{"id":"1","SOURCE_MMF":"Aixam","OEM_CODE":"409",
                           "colour_family":"Black","COLOUR_DESCRIPTION":"NOIR",
                           ...}, ...];

    and the Brand / Year dropdowns, the filter boxes and "Load More" all
    filter or page that array client-side. So one GET of the page plus a
    JSON decode of the array is the whole crawl: no pagination, no
    per-brand or per-year requests.

    Field mapping (site -> house schema, so merge_databases.py takes the
    output unchanged, same keys as chipex / colorndrive / peinturevoiture):
        SOURCE_MMF          -> manufacturer
        OEM_CODE            -> color_codes
        COLOUR_DESCRIPTION  -> color_name
    colour_family, brand_id, image_url and local_file are ignored. There
    is no swatch data (image_url is null on every record), so color_hex is
    omitted; the merge reads it with .get() and treats absence as blank.

    Values are passed through exactly as the site gives them, only
    trimmed. Make names are left raw like the other spiders; the merge
    normalises them. 742 codes contain a slash ('424/A', '086/F'): these
    are Fiat-style codes where the slash is part of the code, and the
    merge splits on ',' and ';' only, so they survive whole.

    Verified against a saved copy of the page (2026-09-22): 19,666
    records across 73 makes, all three fields 100% filled. Record ids run
    1..24,177 with 4,511 ids missing in 4,056 small scattered gaps (the
    largest is 10), which is the signature of rows deleted over time, not
    of a partial first page, so the array is taken to be the full table.
    16 dropdown brands (e.g. Tesla, Geely, Great Wall Motors) have no
    records at all; that is the site's data, not a scrape gap.

    If the array cannot be found or decoded, the site has changed shape:
    the spider logs an ERROR and yields nothing rather than guessing.

Run:
    python -m scrapy crawl carcolourservices -O carcolourservices_full.json
    (-O overwrites; -o would append a second JSON array to an existing
    file and corrupt it.)

Logging:
    LOG_FILE writes the full run log to carcolourservices_log.txt in the
    working directory on every run, so the terminal stays quiet during
    the crawl. The run ends with an INFO summary line; if it yielded 0,
    check the log for "Forbidden by robots.txt" or the ERROR above.
"""

import json
import re

import scrapy


# Tolerant of spacing changes: "var colorCodes = [" / "var colorCodes=[".
ARRAY_MARKER = re.compile(r"var\s+colorCodes\s*=\s*")


class CarColourServicesSpider(scrapy.Spider):

    name = "carcolourservices"
    allowed_domains = ["carcolourservices.co.uk"]
    start_urls = ["https://www.carcolourservices.co.uk/car-paint/"]

    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "DOWNLOAD_DELAY": 1.0,
        "CONCURRENT_REQUESTS": 1,
        "USER_AGENT": "PaintScraper (personal learning project)",
        "LOG_LEVEL": "INFO",
        "LOG_FILE": "carcolourservices_log.txt",
    }

    def __init__(self, makes=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filter_makes = set(
            m.strip().lower() for m in makes.split(",") if m.strip()
        ) if makes else set()

    def parse(self, response):
        match = ARRAY_MARKER.search(response.text)
        if not match:
            self.logger.error(
                "colorCodes array not found on %s -- the page layout has "
                "changed; nothing scraped.", response.url)
            return

        try:
            records, _ = json.JSONDecoder().raw_decode(
                response.text, match.end())
        except json.JSONDecodeError as exc:
            self.logger.error(
                "colorCodes array found but did not decode (%s); "
                "nothing scraped.", exc)
            return

        if not isinstance(records, list):
            self.logger.error(
                "colorCodes is a %s, not a list; nothing scraped.",
                type(records).__name__)
            return

        yielded = skipped = 0
        makes_seen = set()
        for rec in records:
            if not isinstance(rec, dict):
                skipped += 1
                continue
            make = str(rec.get("SOURCE_MMF") or "").strip()
            code = str(rec.get("OEM_CODE") or "").strip()
            name = str(rec.get("COLOUR_DESCRIPTION") or "").strip()

            # A record without a make or a code is unusable downstream:
            # the merge keys on (make, code).
            if not make or not code:
                skipped += 1
                continue
            if self.filter_makes and make.lower() not in self.filter_makes:
                continue

            makes_seen.add(make)
            yielded += 1
            yield {
                "manufacturer": make,
                "color_name": name,
                "color_codes": code,
            }

        self.logger.info(
            "carcolourservices: %d records in array, %d yielded across %d "
            "makes, %d skipped (no make or code).",
            len(records), yielded, len(makes_seen), skipped)
