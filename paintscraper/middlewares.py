"""
Downloader middleware shared by every spider in this project.

PolitenessGuard: slow down when a site pushes back, and stop before a
temporary block turns into a permanent one.

Why it exists:
    Out of the box Scrapy does two unhelpful things when a site starts
    refusing requests. A 429 ("too many requests") or 503 is retried
    straight away, ignoring any Retry-After the site sent, so the crawl
    keeps the same pace that caused the refusal. A 403 is not retried but
    the crawl carries on, so a spider that has been blocked keeps sending
    hundreds of requests that all come back 403. That second case is how a
    short rate-limit becomes a long ban.

What it does (enabled in settings.py, so every spider gets it):
    * 429, 503 or a Cloudflare challenge: that site's delay is raised to
      double its current value, at least 5 s, and at least what the
      site's Retry-After asked for, capped at GUARD_MAX_DELAY. With
      AutoThrottle on (settings.py), the delay then eases back down on its
      own as normal responses return.
    * 403 counts the same way and gets the same slow-down.
    * Other server errors (500, 502, 504, 522, 524) only slow down; they
      are usually the site being busy, not the site refusing us.
    * After GUARD_PUSHBACK_LIMIT refusals in a row (403, 429, 503 or a
      challenge, with no normal response in between) the spider stops
      with close reason "site_pushback". Items already scraped are still
      written to the output file, and the log says what happened.
    * When the spider closes, it logs a WARNING if robots.txt made Scrapy
      skip any requests. Scrapy only reports those at DEBUG level, so at
      the project's INFO level a site tightening its robots.txt would
      otherwise go unnoticed (it happened on ftpaints in October 2026).

It never changes the user agent, headers or anything else about how the
spider identifies itself.

Settings (settings.py):
    GUARD_PUSHBACK_LIMIT   refusals in a row before stopping (default 5)
    GUARD_MAX_DELAY        largest delay it will set, seconds (default 300)

Works with Scrapy 2.13 and later.
"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from scrapy import signals

REFUSALS = {403, 429, 503}           # count towards the stop limit
SLOW_DOWN = {500, 502, 504, 522, 524}  # slow down only


def _retry_after_seconds(response):
    """Seconds asked for by a Retry-After header, or 0 if absent or unreadable.
    The header is either a number of seconds or an HTTP date."""
    raw = response.headers.get(b"Retry-After")
    if not raw:
        return 0.0
    value = raw.decode("latin-1").strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _is_challenge(response):
    """Cloudflare marks its bot-check pages with this header."""
    return response.headers.get(b"cf-mitigated", b"").lower() == b"challenge"


class PolitenessGuard:

    def __init__(self, crawler):
        self.crawler = crawler
        settings = crawler.settings
        self.limit = settings.getint("GUARD_PUSHBACK_LIMIT", 5)
        self.max_delay = settings.getfloat("GUARD_MAX_DELAY", 300.0)
        self.in_a_row = 0
        self.stopping = False
        crawler.signals.connect(self.spider_closed, signal=signals.spider_closed)

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    # -- Responses --------------------------------------------------------

    def process_response(self, request, response, spider=None):
        # Scrapy 2.13 passes the spider; newer versions do not.
        spider = spider or self.crawler.spider
        status = response.status
        refused = status in REFUSALS or _is_challenge(response)

        if refused or status in SLOW_DOWN:
            self._slow_down(request, response, spider, refused)
        elif status < 400:
            self.in_a_row = 0

        if refused:
            self.in_a_row += 1
            self.crawler.stats.inc_value(f"guard/refused/{status}")
            if self.in_a_row >= self.limit and not self.stopping:
                self.stopping = True
                spider.logger.error(
                    "Stopping: %d refusals in a row, the last %d from %s. The "
                    "site is turning us away; wait before running again "
                    "rather than retrying now. Items scraped so far are kept.",
                    self.in_a_row, status, request.url)
                self._close(spider, "site_pushback")
        return response

    def _slow_down(self, request, response, spider, refused):
        downloader = self.crawler.engine.downloader
        key = request.meta.get("download_slot")
        slot = downloader.slots.get(key) if key is not None else None
        if slot is None:
            return
        wanted = max(slot.delay * 2, 5.0, _retry_after_seconds(response))
        new_delay = min(wanted, self.max_delay)
        if new_delay > slot.delay:
            spider.logger.warning(
                "%s answered %d%s; waiting %.0f s between requests to it "
                "for now.", key, response.status,
                " (Cloudflare challenge)" if _is_challenge(response) else "",
                new_delay)
            slot.delay = new_delay
        self.crawler.stats.inc_value("guard/slowdowns")

    def _close(self, spider, reason):
        engine = self.crawler.engine
        close_async = getattr(engine, "close_spider_async", None)
        if close_async is not None:      # Scrapy 2.14 and later
            from scrapy.utils.defer import deferred_from_coro
            deferred_from_coro(close_async(reason=reason))
        else:                            # Scrapy 2.13
            engine.close_spider(spider, reason)

    # -- End of run -------------------------------------------------------

    def spider_closed(self, spider, reason):
        stats = self.crawler.stats
        forbidden = stats.get_value("robotstxt/forbidden", 0)
        if forbidden:
            spider.logger.warning(
                "robots.txt made Scrapy skip %d request(s). If that is new, "
                "the site has changed its rules and the spider needs looking "
                "at before its output is trusted.", forbidden)
        refused = {k.rsplit("/", 1)[-1]: v for k, v in stats.get_stats().items()
                   if k.startswith("guard/refused/")}
        if refused:
            spider.logger.warning(
                "Refusals during the run (status: count): %s",
                ", ".join(f"{k}: {v}" for k, v in sorted(refused.items())))
