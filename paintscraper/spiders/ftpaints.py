"""
Scrapy spider for https://www.ftpaints.co.uk/vehicle-colour-codes
Scrapes every vehicle paint colour FT Paints lists, by make.

Outputs: manufacturer, color_name, color_codes, year, finish, paint_stages, url
Filters: makes (optional), FT's URL slugs, e.g.  -a makes=porsche,land-rover
Options: previous (optional), the last scrape of this site, e.g.
             -a previous=ftpaints_full.json
         max_details (optional, default 500), see "New colours" below

URL pattern:
    /vehicle-colour-codes                           index of every make + count
    /vehicle-colour-codes/{make}/all-colours        every colour of a make, one page
    /vehicle-colour-codes/{make}/{colour-slug}      one colour: code, name,
                                                    finish, paint stages, years

Why not the paged listing any more:
    Until September 2026 this spider read the make listing
    (/vehicle-colour-codes/{make}?page=N, newest first), because only its
    cards carried the year range. Since then FT's robots.txt disallows
    /*?page= (and /*?year=, /*?sort= and other listing parameters), so that
    crawl would stop after page 1 of every make: 2,372 of its 2,516
    requests would be skipped. Checked 2026-10-27; the run on 2026-09-22
    was not affected.

How it works now:
    1. The index states every make's colour count ("Porsche 597 colour
       codes") and its display name.
    2. Each make's all-colours page lists the make's COMPLETE set as
       links, "CODE — Name", one page, no pagination:
           <a href="/vehicle-colour-codes/zaz/zaz-z9051-abendrot">
             <span class="font-medium text-ink">Z9051</span> — Abendrot</a>
       Codes are taken from the <span>, never the URL slug (the slug
       mangles punctuation: 'R699:23' becomes 'r699-23'). Checked
       2026-10-27 against the 2026-09-22 scrape for Porsche (597), Mercedes
       Benz (1,089), Ford (3,949) and ZAZ (40): every code and name
       identical, so switching pages changes nothing in those two fields.
    3. Year range, finish and paint stages are only on each colour's own
       page, which costs one request per colour (58,702 in all). So they
       are fetched only for colours that are NEW since the previous scrape:
       see below.

    About 145 requests plus one per new colour; a few minutes at the house
    pace (settings.py).

New colours (-a previous=...):
    Give the spider the last scrape of this site and it compares every
    colour against it on (manufacturer, color_codes, color_name). Colours
    found there are written with year, finish and paint_stages BLANK,
    because they were not fetched on this run; the earlier scrape still
    has their year. Colours not found there get their own page fetched,
    and those three fields filled in from it.

    A blank year therefore means "not fetched this run", never "the site
    has no year". Without -a previous, nothing is fetched and all three
    fields are blank on every row.

    max_details caps the colour pages fetched in one run (default 500).
    If a run finds more new colours than that, the rest are written
    without the extra fields and the log says how many, so a mismatch in
    the previous file (renamed make, changed name format) cannot turn
    into an accidental 58,000-request crawl.

Swatch:
    The card swatch colour is deliberately NOT scraped. It is a
    colour-family placeholder, not a paint swatch: on the Porsche listing
    (checked again 2026-10-27) 48 colours share 8 hex values, Lugano Blue
    and Pale Blue share #3b6ea5, and Provence gets the grey #d4d4d8.

Completeness check:
    Colours are counted per make and compared with the index count when
    the spider closes. Any make that came up short or long is logged as a
    WARNING, so a partial crawl cannot pass silently.

Run (from the paintscraper folder, keeping the previous scrape as it is):
    python -m scrapy crawl ftpaints -a previous=ftpaints_full.json -O ftpaints_2026-10-27.json
    python -m scrapy crawl ftpaints -a makes=zaz,porsche -a previous=ftpaints_full.json -O ftpaints_test.json
    (-O overwrites; -o would append a second JSON array to an existing
    file and corrupt it. Never point -O at the file passed as previous.)

Logging:
    LOG_FILE writes the full run log to ftpaints_log.txt in the working
    directory, so the terminal stays quiet during the crawl. The run ends
    with an INFO summary, one WARNING per make whose count does not match
    the index, and a WARNING if robots.txt made Scrapy skip anything.
"""

import json
import re
from collections import Counter
from urllib.parse import urlparse

import scrapy


# /vehicle-colour-codes/{slug}: exactly one segment after the base.
MAKE_PATH = re.compile(r"^/vehicle-colour-codes/([a-z0-9][a-z0-9-]*)/?$")
# /vehicle-colour-codes/{slug}/{colour}: exactly two segments.
COLOUR_PATH = re.compile(r"^/vehicle-colour-codes/([a-z0-9][a-z0-9-]*)/([^/]+?)/?$")
# "597 colour codes" (index caption; the count may carry a comma).
COUNT_TEXT = re.compile(r"(\d[\d,]*)\s*colour codes", re.I)
# The separator between code and name on the all-colours page.
LEADING_DASH = re.compile(r"^\s*[—–-]\s*")


def _clean(text):
    """Collapse whitespace; None-safe."""
    return " ".join((text or "").split())


class FTPaintsSpider(scrapy.Spider):

    name = "ftpaints"
    allowed_domains = ["ftpaints.co.uk"]
    start_urls = ["https://www.ftpaints.co.uk/vehicle-colour-codes"]

    # User agent, robots.txt, pace and back-off are the house defaults in
    # settings.py. At ~145 requests a run, there is no need to go faster.
    custom_settings = {
        "LOG_FILE": "ftpaints_log.txt",
    }

    def __init__(self, makes=None, previous=None, max_details=500,
                 *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filter_makes = set(
            m.strip().lower() for m in makes.split(",") if m.strip()
        ) if makes else set()
        self.max_details = int(max_details)
        self.previous = previous
        self.known = self._load_previous(previous) if previous else None

        self.expected = {}          # slug -> colour count stated on the index
        self.make_names = {}        # slug -> make name shown on the index
        self.got = Counter()        # slug -> colours written
        self.seen = set()           # colour page URLs already handled
        self.new = 0                # colours not in the previous scrape
        self.details_sent = 0
        self.details_capped = 0
        self.details_failed = 0

    @staticmethod
    def _load_previous(path):
        try:
            with open(path, encoding="utf-8") as f:
                rows = json.load(f)
        except FileNotFoundError:
            raise ValueError(f"previous: no such file: {path}") from None
        except json.JSONDecodeError as exc:
            raise ValueError(f"previous: {path} is not valid JSON ({exc})") from None
        return {
            (_clean(r.get("manufacturer")), _clean(r.get("color_codes")),
             _clean(r.get("color_name")))
            for r in rows if isinstance(r, dict)
        }

    # -- Index ----------------------------------------------------------

    def parse(self, response):
        if self.known is not None:
            self.logger.info("Previous scrape %s: %d colours to compare against.",
                             self.previous, len(self.known))

        found, names = {}, {}
        for link in response.css("a[href]"):
            path = urlparse(response.urljoin(link.attrib["href"])).path
            m = MAKE_PATH.match(path)
            if not m:
                continue
            caption = (link.css("span.text-caption").xpath("string()").get()
                       or link.xpath("string()").get())
            count = COUNT_TEXT.search(_clean(caption))
            if not count:
                continue            # breadcrumb or nav link, not a make entry
            slug = m.group(1)
            found[slug] = int(count.group(1).replace(",", ""))
            name = _clean(link.css("span.text-h4").xpath("string()").get())
            if name:
                names[slug] = name

        if not found:
            self.logger.error(
                "No make links with colour counts found on %s -- the index "
                "layout has changed; nothing scraped.", response.url)
            return

        unnamed = sorted(set(found) - set(names))
        if unnamed:
            self.logger.warning(
                "No make name on the index for %s; using the URL slug, which "
                "will not match earlier scrapes.", ", ".join(unnamed))

        if self.filter_makes:
            unknown = self.filter_makes - set(found)
            if unknown:
                self.logger.warning(
                    "makes filter: no such slug on the index: %s",
                    ", ".join(sorted(unknown)))
            found = {s: c for s, c in found.items() if s in self.filter_makes}

        self.expected = found
        self.make_names = names
        self.logger.info(
            "Index: %d makes, %d colours expected.",
            len(found), sum(found.values()))

        for slug in sorted(found):
            yield response.follow(
                f"/vehicle-colour-codes/{slug}/all-colours",
                callback=self.parse_all,
                cb_kwargs={"slug": slug})

    # -- All colours of one make ----------------------------------------

    def parse_all(self, response, slug):
        make = self.make_names.get(slug) or slug.replace("-", " ").title()
        listed = 0
        for link in response.css("a[href]"):
            url = response.urljoin(link.attrib["href"])
            m = COLOUR_PATH.match(urlparse(url).path)
            if not m or m.group(1) != slug or m.group(2) == "all-colours":
                continue
            if url in self.seen:
                continue
            code = _clean(link.css("span.font-medium").xpath("string()").get())
            if not code:
                continue            # a link to a colour page, but not a list entry
            self.seen.add(url)
            listed += 1

            text = _clean(link.xpath("string()").get())
            name = text[len(code):] if text.startswith(code) else text
            name = LEADING_DASH.sub("", name).strip()

            record = {
                "manufacturer": make,
                "color_name": name,
                "color_codes": code,
                "year": "",
                "finish": "",
                "paint_stages": "",
                "url": url,
            }

            if self.known is not None and (make, code, name) not in self.known:
                self.new += 1
                if self.details_sent < self.max_details:
                    self.details_sent += 1
                    yield response.follow(
                        url,
                        callback=self.parse_detail,
                        errback=self.detail_failed,
                        cb_kwargs={"record": record, "slug": slug})
                    continue
                self.details_capped += 1

            self.got[slug] += 1
            yield record

        if not listed:
            self.logger.error(
                "No colours listed on %s -- layout changed or empty page.",
                response.url)

    # -- One colour's own page (new colours only) ------------------------

    def parse_detail(self, response, record, slug):
        facts = {}
        for dl in response.css("dl"):
            pairs = {}
            for dt in dl.css("dt"):
                label = _clean(dt.xpath("string()").get()).lower()
                value = _clean(dt.xpath("following-sibling::dd[1]").xpath("string()").get())
                pairs[label] = value
            if "colour code" in pairs:
                facts = pairs
                break

        if not facts:
            self.logger.warning(
                "No colour facts on %s -- keeping %s %s without year/finish.",
                response.url, record["manufacturer"], record["color_codes"])
        elif facts.get("colour code") != record["color_codes"]:
            self.logger.warning(
                "Code on %s reads %r, the all-colours list says %r; keeping "
                "the list's code.", response.url, facts.get("colour code"),
                record["color_codes"])

        record = dict(record)
        record["year"] = facts.get("year range", "")
        record["finish"] = facts.get("finish", "")
        record["paint_stages"] = facts.get("paint stages", "")
        self.got[slug] += 1
        yield record

    def detail_failed(self, failure):
        request = failure.request
        record = request.cb_kwargs["record"]
        self.details_failed += 1
        self.logger.warning(
            "Colour page failed (%s): %s -- keeping %s %s without year/finish.",
            failure.getErrorMessage(), request.url, record["manufacturer"],
            record["color_codes"])
        self.got[request.cb_kwargs["slug"]] += 1
        yield record

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

        if self.known is None:
            self.logger.info(
                "No -a previous given: year, finish and paint_stages are "
                "blank on every row.")
        else:
            self.logger.info(
                "New since %s: %d colours; %d colour pages fetched, %d "
                "failed, %d not fetched (max_details=%d).",
                self.previous, self.new, self.details_sent,
                self.details_failed, self.details_capped, self.max_details)
        if self.details_capped:
            self.logger.warning(
                "%d new colours were written without year/finish because of "
                "max_details. Check the previous file matches this site's "
                "make names before raising it.", self.details_capped)

        self.logger.info(
            "ftpaints: %d colours scraped across %d makes (index expected "
            "%d); %d makes mismatched; close reason: %s.",
            sum(self.got.values()), len(self.got),
            sum(self.expected.values()), len(short), reason)
