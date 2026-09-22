"""
Scrapy spider for https://www.ftpaints.co.uk/vehicle-colour-codes

Scrapes every vehicle paint colour, with its production year range.

Outputs: manufacturer, color_name, color_codes, year
Filters: makes (optional), FT's URL slugs, e.g.  -a makes=porsche,land-rover

URL pattern:
    /vehicle-colour-codes                     index of every make + count
    /vehicle-colour-codes/{make}?page=N       24 colour cards per page

How it works:
    The index links every make and states how many colours it has
    ("Porsche 597 colour codes"). Each make's listing is a server-rendered
    Next.js page of 24 cards, newest first, paged with ?page=N. A card
    carries the make, the paint code, the colour name and the production
    year range:

        <article> ... <p class="... font-medium ...">Porsche</p>
                      <p class="... font-mono ...">R699:23</p>
                      <h3><a href="/vehicle-colour-codes/porsche/...">Felsengruen</a></h3>
                      <p class="...">2023 - 2025</p> ... </article>

    Page 1 of each make is fetched, its highest page number is read from
    the pagination links, and the remaining pages are requested directly.

    Why the paged view and not /{make}/all-colours: all-colours lists a
    make in ONE page, but only as "CODE -- Name" links with no year range.
    The year range exists only on the cards, so the paged view is the only
    single pass that gets all four fields. Sized from the index
    (2026-09-22): 144 makes, 58,702 colours, 2,516 page requests; see
    custom_settings for how they are paced.

    Field mapping (house schema, same keys as the other spiders so
    merge_databases.py takes the output unchanged):
        card make   -> manufacturer
        card code   -> color_codes
        card name   -> color_name
        year range  -> year   (same field and "YYYY - YYYY" / "YYYY -"
                               shape as peinturevoiture's year; the merge
                               currently ignores year entirely)

    The card swatch colour is deliberately NOT scraped into color_hex. It
    is a colour-family placeholder, not a paint swatch: on the saved
    Porsche page seven different blues (Miami Blue to Enzian Blue) share
    #3b6ea5, and orange Papaya and red Copper Ruby get the grey fallback
    #d4d4d8. The site says so itself: "Swatches are an approximate guide
    based on the colour name". Fed to the merge, it would paint Papaya
    grey.

    Codes are taken from the card text, never the URL slug: the slug
    mangles punctuation ('R699:23' becomes 'r699-23'). Values are only
    whitespace-collapsed. Some names on the site already carry a broken
    character (U+FFFD, e.g. 'Cuv?e Silver'); that is the site's data and
    is passed through unchanged.

    Completeness check: every card is counted per make and compared with
    the index count when the spider closes. Any make that came up short or
    long is logged as a WARNING, so a partial crawl cannot pass silently.
    A card seen twice (the listing shifting mid-crawl) is yielded once.

Run:
    python -m scrapy crawl ftpaints -O ftpaints_full.json
    (-O overwrites; -o would append a second JSON array to an existing
    file and corrupt it.)

Logging:
    LOG_FILE writes the full run log to ftpaints_log.txt in the working
    directory on every run, so the terminal stays quiet during the crawl.
    The run ends with an INFO summary and one WARNING per make whose count
    does not match the index. If it yielded 0, check the log for
    "Forbidden by robots.txt" or an ERROR line.
"""

import re
from collections import Counter
from urllib.parse import urlparse

import scrapy


# /vehicle-colour-codes/{slug} with exactly one segment after the base.
MAKE_PATH = re.compile(r"^/vehicle-colour-codes/([a-z0-9][a-z0-9-]*)/?$")
# "597 colour codes" (index link text; the count may carry a comma).
COUNT_TEXT = re.compile(r"(\d[\d,]*)\s*colour codes", re.I)
# Year slot: "2023 - 2025", "2012 -", "- 2012" or "2012".
YEAR_TEXT = re.compile(r"\d{4}(?:\s*-\s*(?:\d{4})?)?|-\s*\d{4}")
PAGE_PARAM = re.compile(r"[?&]page=(\d+)")

CARDS_PER_PAGE = 24


def _clean(text):
    """Collapse whitespace; None-safe."""
    return " ".join((text or "").split())


class FTPaintsSpider(scrapy.Spider):

    name = "ftpaints"
    allowed_domains = ["ftpaints.co.uk"]
    start_urls = ["https://www.ftpaints.co.uk/vehicle-colour-codes"]

    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        # Paced faster than the other spiders, but adaptively. AutoThrottle
        # measures FT's response times and sets the delay itself, backing
        # off if the server slows; it starts at a gentle 1s and never goes
        # below DOWNLOAD_DELAY. The listing offers no bigger page size (no
        # page-size parameter, API route or sitemap on the page), so the
        # request count is fixed at ~2,516 and pace is the only lever:
        # ~42 minutes at 1/s becomes roughly 10-20 at this floor, depending
        # on how fast the site answers.
        "DOWNLOAD_DELAY": 0.25,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 1.0,
        "AUTOTHROTTLE_MAX_DELAY": 10.0,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 2.0,
        "CONCURRENT_REQUESTS": 4,
        # settings.py pins this to 1 for every spider; without overriding it
        # here, the concurrency above would have no effect.
        "CONCURRENT_REQUESTS_PER_DOMAIN": 4,
        "USER_AGENT": "PaintScraper (personal learning project)",
        "LOG_LEVEL": "INFO",
        "LOG_FILE": "ftpaints_log.txt",
    }

    def __init__(self, makes=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filter_makes = set(
            m.strip().lower() for m in makes.split(",") if m.strip()
        ) if makes else set()
        self.expected = {}          # slug -> colour count stated on the index
        self.got = Counter()        # slug -> cards yielded
        self.seen = set()           # colour detail hrefs already yielded

    # -- Index ----------------------------------------------------------

    def parse(self, response):
        found = {}
        for link in response.css("a[href]"):
            path = urlparse(response.urljoin(link.attrib["href"])).path
            m = MAKE_PATH.match(path)
            if not m:
                continue
            slug = m.group(1)
            count = COUNT_TEXT.search(_clean(link.xpath("string()").get()))
            if count:
                found[slug] = int(count.group(1).replace(",", ""))
            else:
                found.setdefault(slug, None)

        # Breadcrumb-style links without a count are not make entries.
        makes = {s: c for s, c in found.items() if c is not None}
        if not makes:
            self.logger.error(
                "No make links with colour counts found on %s -- the index "
                "layout has changed; nothing scraped.", response.url)
            return

        if self.filter_makes:
            unknown = self.filter_makes - set(makes)
            if unknown:
                self.logger.warning(
                    "makes filter: no such slug on the index: %s",
                    ", ".join(sorted(unknown)))
            makes = {s: c for s, c in makes.items() if s in self.filter_makes}

        self.expected = makes
        self.logger.info(
            "Index: %d makes, %d colours expected.",
            len(makes), sum(makes.values()))

        for slug in sorted(makes):
            yield response.follow(
                f"/vehicle-colour-codes/{slug}",
                callback=self.parse_page,
                cb_kwargs={"slug": slug, "page": 1})

    # -- Make listing pages ---------------------------------------------

    def parse_page(self, response, slug, page):
        cards = [c for c in response.css("article")
                 if c.css('h3 a[href*="/vehicle-colour-codes/"]')]
        if not cards:
            self.logger.error(
                "No colour cards on %s -- layout changed or empty page.",
                response.url)

        for card in cards:
            item = self._parse_card(card, response)
            if item is None:
                continue
            href, record = item
            if href in self.seen:
                continue
            self.seen.add(href)
            self.got[slug] += 1
            yield record

        if page == 1:
            last = max((int(n) for n in PAGE_PARAM.findall(
                " ".join(response.css("a::attr(href)").getall()))),
                default=1)
            expected = self.expected.get(slug)
            if expected:
                # Trust the index count if pagination is missing or short.
                last = max(last, -(-expected // CARDS_PER_PAGE))
            for n in range(2, last + 1):
                yield response.follow(
                    f"/vehicle-colour-codes/{slug}?page={n}",
                    callback=self.parse_page,
                    cb_kwargs={"slug": slug, "page": n})

    def _parse_card(self, card, response):
        link = card.css('h3 a[href*="/vehicle-colour-codes/"]')
        href = response.urljoin(link.attrib.get("href", ""))
        name = _clean(link.xpath("string()").get())
        code = _clean(card.css("p.font-mono").xpath("string()").get())
        make = _clean(card.css("p.font-medium").xpath("string()").get())

        year = ""
        for p in card.css("p"):
            text = _clean(p.xpath("string()").get())
            if YEAR_TEXT.fullmatch(text):
                year = text
                break

        # A card without a make or code is unusable downstream: the merge
        # keys on (make, code). Name and year may legitimately be blank.
        if not make or not code:
            self.logger.warning(
                "Skipping card with no make or code on %s (%s)",
                response.url, href)
            return None

        return href, {
            "manufacturer": make,
            "color_name": name,
            "color_codes": code,
            "year": year,
        }

    # -- Completeness ---------------------------------------------------

    def closed(self, reason):
        short = []
        for slug, want in sorted(self.expected.items()):
            have = self.got.get(slug, 0)
            if have != want:
                short.append(slug)
                self.logger.warning(
                    "Count mismatch for %s: index says %d, scraped %d.",
                    slug, want, have)
        self.logger.info(
            "ftpaints: %d colours scraped across %d makes (index expected "
            "%d); %d makes mismatched; close reason: %s.",
            sum(self.got.values()), len(self.got),
            sum(self.expected.values()), len(short), reason)
