# Scrapy settings for the paintscraper project.
#
# These are the house defaults every spider runs with. A spider's own
# custom_settings override them only where that site needs something
# different (its log file, and pace where a site has been measured).
#
#     https://docs.scrapy.org/en/latest/topics/settings.html

BOT_NAME = "paintscraper"

SPIDER_MODULES = ["paintscraper.spiders"]
NEWSPIDER_MODULE = "paintscraper.spiders"

ADDONS = {}


# -- Identity ---------------------------------------------------------------
# One honest user agent for every spider. Sites can see who is crawling and
# robots.txt rules can address us; never swap this for a browser string.
USER_AGENT = "PaintScraper (personal learning project)"

# Obey robots.txt rules. Requests it forbids are skipped (and counted: the
# PolitenessGuard below warns about them at the end of the run).
ROBOTSTXT_OBEY = True


# -- Pace ---------------------------------------------------------------------
# One request at a time per site, at least ~1 s apart (Scrapy randomises
# each wait between 0.5x and 1.5x of DOWNLOAD_DELAY).
CONCURRENT_REQUESTS_PER_DOMAIN = 1
DOWNLOAD_DELAY = 1

# AutoThrottle measures how long each site takes to answer and widens the
# gap when the site slows down. It never goes below DOWNLOAD_DELAY, so with
# the values above it can only make a crawl gentler, never faster. A spider
# that has been measured as safe to run faster raises the target itself.
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.0
AUTOTHROTTLE_MAX_DELAY = 30.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0


# -- Backing off --------------------------------------------------------------
# PolitenessGuard (middlewares.py): on 403 / 429 / 503 / a Cloudflare
# challenge it slows the crawl down and honours Retry-After; after
# GUARD_PUSHBACK_LIMIT refusals in a row it stops the spider instead of
# carrying on. Priority 555 puts it in front of the retry middleware (550),
# so a retried request already waits the longer delay.
DOWNLOADER_MIDDLEWARES = {
    "paintscraper.middlewares.PolitenessGuard": 555,
}
GUARD_PUSHBACK_LIMIT = 5
GUARD_MAX_DELAY = 300


# -- Housekeeping ---------------------------------------------------------------
LOG_LEVEL = "INFO"

# The telnet console opens a Python shell on a local port while a crawl runs.
# Nothing here uses it, so keep it closed.
TELNETCONSOLE_ENABLED = False

FEED_EXPORT_ENCODING = "utf-8"

# For trial runs only: re-running a spider against cached pages costs the
# site nothing. Turn it on from the command line, never for a real scrape:
#     python -m scrapy crawl chipex -a makes=toyota -O test.json -s HTTPCACHE_ENABLED=1
HTTPCACHE_ENABLED = False
HTTPCACHE_DIR = "httpcache"
