"""
=============================================================================
 Freelance Market Monitor — Web Scraper
 Course   : CS313x Information Retrieval
 Project  : Freelance Market Monitor
 Platforms: Freelancer.com  +  Mostaqel.com
 Author   : [Your Name]
 Date     : 2025
=============================================================================

ETHICAL SCRAPING NOTICE
-----------------------
This script:
  • Reads and obeys each site's robots.txt before scraping.
  • Inserts random human-like delays between every request.
  • Sends realistic browser User-Agent strings.
  • Collects only publicly available, non-personal data.
  • Is intended solely for academic / educational use.

HOW TO RUN
----------
1. Install dependencies:
       pip install requests beautifulsoup4 lxml fake-useragent

2. Run the scraper:
       python scraper.py

3. Output is saved to:
       freelance_data.json

BYPASSING BASIC BLOCKING
-------------------------
The main techniques used here are:
  a) Rotating User-Agent headers (via fake-useragent library).
  b) Random sleep intervals (1.5 – 4 seconds) between requests.
  c) Retry logic with exponential back-off on HTTP 429 / 503.
  d) A persistent requests.Session() to reuse TCP connections and
     carry cookies across pages (mimics a real browser session).
  e) Accepting gzip/br encoding and sending realistic Accept headers.
=============================================================================
"""

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Schema
# ---------------------------------------------------------------------------
@dataclass
class FreelanceProject:
    """
    Canonical record for one freelance project.
    All fields that cannot be found are stored as None (null in JSON).
    """
    platform: str                          # Source platform name
    title: Optional[str] = None            # Project / job title
    url: Optional[str] = None             # Direct link to the project
    budget_min: Optional[float] = None    # Minimum budget (numeric, USD or local)
    budget_max: Optional[float] = None    # Maximum budget (numeric)
    budget_currency: Optional[str] = None # Currency code, e.g. "USD", "SAR"
    budget_type: Optional[str] = None     # "fixed" | "hourly" | "unknown"
    skills: list = field(default_factory=list)  # List of required skills
    category: Optional[str] = None        # Project category / domain
    posted_date: Optional[str] = None     # Raw date string as shown on site
    description_snippet: Optional[str] = None   # First ~200 chars of description


# ---------------------------------------------------------------------------
# Utility Helpers
# ---------------------------------------------------------------------------

# A pool of realistic desktop User-Agent strings.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
]


def get_session() -> requests.Session:
    """
    Build a requests Session with realistic browser-like headers.
    A Session reuses the underlying TCP connection and stores cookies,
    which makes the traffic look far more like a real browser visit.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    })
    return session


def polite_sleep(min_s: float = 1.5, max_s: float = 4.0) -> None:
    """
    Sleep a random amount of time between min_s and max_s seconds.
    Randomising the delay is crucial — fixed delays are easy to detect.
    """
    duration = random.uniform(min_s, max_s)
    log.debug("  ↳ sleeping %.2f s …", duration)
    time.sleep(duration)


def fetch_page(
    session: requests.Session,
    url: str,
    retries: int = 3,
    backoff: float = 5.0,
) -> Optional[BeautifulSoup]:
    """
    Fetch a URL and return a BeautifulSoup tree.

    Implements:
      • Retry logic (up to `retries` attempts).
      • Exponential back-off when the server signals rate-limiting
        (HTTP 429) or temporary unavailability (HTTP 503).
      • Rotates User-Agent on each retry to reduce fingerprinting.

    Returns None if all attempts fail.
    """
    for attempt in range(1, retries + 1):
        # Rotate UA on every attempt
        session.headers["User-Agent"] = random.choice(USER_AGENTS)
        try:
            response = session.get(url, timeout=15)

            if response.status_code == 200:
                return BeautifulSoup(response.text, "lxml")

            elif response.status_code in (429, 503):
                wait = backoff * attempt
                log.warning(
                    "Rate-limited (HTTP %d) on attempt %d/%d — "
                    "waiting %.0f s before retry …",
                    response.status_code, attempt, retries, wait,
                )
                time.sleep(wait)

            elif response.status_code == 404:
                log.warning("404 Not Found: %s", url)
                return None

            else:
                log.warning(
                    "HTTP %d for %s (attempt %d/%d)",
                    response.status_code, url, attempt, retries,
                )

        except requests.exceptions.Timeout:
            log.warning("Timeout on %s (attempt %d/%d)", url, attempt, retries)
        except requests.exceptions.ConnectionError as exc:
            log.warning("Connection error on %s: %s", url, exc)

        if attempt < retries:
            polite_sleep(backoff, backoff * 2)

    log.error("All %d attempts failed for: %s", retries, url)
    return None



def _check_wildcard_disallow(robots_text: str, path: str) -> bool:
    """
    Python's RobotFileParser ignores the '*' wildcard in Disallow rules.
    This helper manually checks whether any wildcard Disallow pattern
    (e.g. 'Disallow: /search*') matches the given path.

    Returns True if a wildcard rule BLOCKS the path, False otherwise.
    Only applies to the universal User-agent (*) section for simplicity.
    """
    in_wildcard_section = False
    for raw_line in robots_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        lower = line.lower()
        if lower.startswith("user-agent:"):
            agent = lower.split(":", 1)[1].strip()
            in_wildcard_section = (agent == "*")
        elif in_wildcard_section and lower.startswith("disallow:"):
            rule = line.split(":", 1)[1].strip()
            if "*" in rule:
                # Convert glob pattern to a simple prefix check
                prefix = rule.split("*")[0]
                if path.startswith(prefix):
                    return True   # blocked by wildcard rule
    return False


def is_allowed_by_robots(base_url: str, path: str = "/") -> bool:
    """
    Check whether the given path is allowed by the site's robots.txt.

    Python's built-in RobotFileParser.read() uses urllib internally and
    does NOT expose the HTTP status code — critically, when the server
    returns 403 on /robots.txt, the parser sets disallow_all=True and
    blocks every path, even ones that are perfectly legal to scrape.

    Per RFC 9309 the correct interpretation of each status code is:
      • 200  → parse the file and apply its rules            (normal)
      • 404 / 410  → no rules exist → everything is allowed  (fail open)
      • 401 / 403  → access to rules is denied → treat as
                     "no rules" and allow (same as 404 above) [§2.3.1.3]
      • 5xx  → temporary error → assume allowed, retry later (fail open)

    We therefore fetch robots.txt ourselves with requests so we can
    inspect the status code before handing the body to RobotFileParser.
    """
    robots_url = urljoin(base_url, "/robots.txt")
    target_url = urljoin(base_url, path)

    try:
        resp = requests.get(
            robots_url,
            timeout=10,
            headers={"User-Agent": random.choice(USER_AGENTS)},
        )

        # ── 200: parse and obey ────────────────────────────────────────────
        if resp.status_code == 200:
            rp = RobotFileParser()
            rp.set_url(robots_url)
            rp.parse(resp.text.splitlines())   # parse from text, not re-fetch

            # Python stdlib does NOT support the wildcard * in Disallow rules
            # (e.g. "Disallow: /search*").  We add a manual wildcard check.
            stdlib_allowed = rp.can_fetch("*", target_url)
            wildcard_blocked = _check_wildcard_disallow(resp.text, path)
            allowed = stdlib_allowed and not wildcard_blocked

            if not allowed:
                log.warning(
                    "robots.txt explicitly disallows: %s  (rule blocks %s)",
                    target_url, path,
                )
            else:
                log.info("robots.txt allows: %s", target_url)
            return allowed

        # ── 404 / 410: no robots.txt → no restrictions ────────────────────
        elif resp.status_code in (404, 410):
            log.info(
                "robots.txt not found (HTTP %d) for %s → assuming allowed.",
                resp.status_code, base_url,
            )
            return True

        # ── 401 / 403: robots.txt itself is access-restricted ─────────────
        # RFC 9309 §2.3.1.3: treat the same as "unavailable" → fail open.
        # The site is not telling us "you can't scrape" — it just won't
        # serve the policy file with our credentials.  This is different
        # from a rule that explicitly Disallows a path.
        elif resp.status_code in (401, 403):
            log.info(
                "robots.txt returned HTTP %d (access restricted) for %s "
                "→ no explicit rules found → treating as allowed per RFC 9309.",
                resp.status_code, base_url,
            )
            return True

        # ── 5xx: server error → fail open, try scraping anyway ────────────
        elif resp.status_code >= 500:
            log.warning(
                "robots.txt server error HTTP %d for %s → failing open.",
                resp.status_code, base_url,
            )
            return True

        # ── Anything else: be conservative, log and allow ─────────────────
        else:
            log.warning(
                "Unexpected HTTP %d fetching robots.txt for %s → allowing.",
                resp.status_code, base_url,
            )
            return True

    except requests.exceptions.RequestException as exc:
        # Network failure → can't know the rules → fail open
        log.warning("Could not reach robots.txt at %s: %s → allowing.", robots_url, exc)
        return True


def clean_budget(raw: Optional[str]):
    """
    Parse a messy budget string such as:
        "$50 - $100"   →  min=50.0, max=100.0, currency="USD"
        "£500"         →  min=500.0, max=500.0, currency="GBP"
        "SR 200 - 500" →  min=200.0, max=500.0, currency="SAR"
        "Negotiable"   →  min=None,  max=None,  currency=None

    Returns a tuple: (min_val, max_val, currency, budget_type)
    """
    if not raw:
        return None, None, None, "unknown"

    raw = raw.strip()

    # Detect currency symbol
    currency_map = {
        "$": "USD", "£": "GBP", "€": "EUR",
        "SAR": "SAR", "SR": "SAR", "ر.س": "SAR",
        "EGP": "EGP", "ج.م": "EGP",
    }
    currency = None
    for symbol, code in currency_map.items():
        if symbol in raw:
            currency = code
            break

    # Detect hourly vs fixed
    budget_type = "hourly" if "/hr" in raw.lower() or "hour" in raw.lower() else "fixed"

    # Extract all numbers from the string
    numbers = re.findall(r"[\d,]+\.?\d*", raw.replace(",", ""))
    nums = [float(n) for n in numbers if n]

    if len(nums) == 0:
        return None, None, currency, "unknown"
    elif len(nums) == 1:
        return nums[0], nums[0], currency, budget_type
    else:
        return min(nums), max(nums), currency, budget_type


# ---------------------------------------------------------------------------
# Scraper 1: Freelancer.com
# ---------------------------------------------------------------------------

FREELANCER_BASE = "https://www.freelancer.com"
FREELANCER_SEARCH = "/jobs/"   # Public job listings — pagination via ?page=N


def scrape_freelancer(
    session: requests.Session,
    max_pages: int = 10,
    category_slug: str = "",
) -> list[FreelanceProject]:
    """
    Scrape public job listings from Freelancer.com.

    Freelancer.com renders its main listing page server-side (HTML),
    making plain requests + BeautifulSoup sufficient for most pages.
    JavaScript-heavy detail pages are skipped — we extract what is
    available in the listing cards.

    Args:
        session     : Shared requests.Session with headers already set.
        max_pages   : Maximum number of paginated listing pages to visit.
        category_slug: Optional category path, e.g. "web-development/".

    Returns:
        List of FreelanceProject objects.
    """
    projects: list[FreelanceProject] = []
    search_path = FREELANCER_SEARCH + category_slug

    # ── Ethics check ──────────────────────────────────────────────────────
    if not is_allowed_by_robots(FREELANCER_BASE, search_path):
        log.warning("Freelancer.com robots.txt blocks this path. Skipping.")
        return projects

    log.info("▶ Starting Freelancer.com scrape (max %d pages) …", max_pages)

    for page_num in range(1, max_pages + 1):
        # Freelancer paginates with ?page=N (1-indexed)
        page_url = f"{FREELANCER_BASE}{search_path}?page={page_num}"
        log.info("  Page %d/%d → %s", page_num, max_pages, page_url)

        soup = fetch_page(session, page_url)
        if soup is None:
            log.warning("  Could not fetch page %d. Stopping Freelancer scrape.", page_num)
            break

        # ── Parse job cards ───────────────────────────────────────────────
        # Each project is wrapped in a <div> with class "JobSearchCard-item"
        # NOTE: CSS class names may change — update selectors if needed.
        cards = soup.select("div.JobSearchCard-item")

        if not cards:
            # Fallback: try alternate card selectors used on different layouts
            cards = soup.select("div[class*='job-card']") or \
                    soup.select("li.job-wrap") or \
                    soup.select("div.search-result-item")

        if not cards:
            log.warning("  No job cards found on page %d. Site layout may have changed.", page_num)
            # Dump a snippet to help debugging
            log.debug("  HTML snippet: %s", soup.body.get_text()[:300] if soup.body else "N/A")
            break

        for card in cards:
            project = _parse_freelancer_card(card)
            if project:
                projects.append(project)

        log.info("  → %d projects collected so far.", len(projects))
        polite_sleep()   # ← Ethical delay between page requests

    log.info("✔ Freelancer.com done. Total: %d projects.", len(projects))
    return projects


def _parse_freelancer_card(card) -> Optional[FreelanceProject]:
    """
    Extract fields from a single Freelancer.com job card element.
    Returns None if the card is fundamentally malformed (no title).
    All missing fields are gracefully handled and stored as None.
    """
    try:
        # ── Title ──────────────────────────────────────────────────────────
        title_tag = (
            card.select_one("a.JobSearchCard-primary-heading-link") or
            card.select_one("h2.JobSearchCard-primary-heading a") or
            card.select_one("[class*='heading'] a")
        )
        title = title_tag.get_text(strip=True) if title_tag else None
        if not title:
            return None   # Skip cards with no title

        # ── URL ────────────────────────────────────────────────────────────
        raw_href = title_tag.get("href", "") if title_tag else ""
        url = urljoin(FREELANCER_BASE, raw_href) if raw_href else None

        # ── Budget ─────────────────────────────────────────────────────────
        budget_tag = (
            card.select_one("div.JobSearchCard-primary-price") or
            card.select_one("[class*='price']") or
            card.select_one("[class*='budget']")
        )
        raw_budget = budget_tag.get_text(strip=True) if budget_tag else None
        bmin, bmax, currency, btype = clean_budget(raw_budget)

        # ── Skills ─────────────────────────────────────────────────────────
        skills_tags = (
            card.select("a.JobSearchCard-primary-tagsLink") or
            card.select("[class*='skill'] a") or
            card.select("[class*='tag'] a")
        )
        skills = [s.get_text(strip=True) for s in skills_tags if s.get_text(strip=True)]

        # ── Category ───────────────────────────────────────────────────────
        # Freelancer shows the category as a breadcrumb or badge
        category_tag = (
            card.select_one("a.JobSearchCard-primary-category") or
            card.select_one("[class*='category']")
        )
        category = category_tag.get_text(strip=True) if category_tag else None

        # ── Description snippet ────────────────────────────────────────────
        desc_tag = (
            card.select_one("p.JobSearchCard-secondary-description") or
            card.select_one("[class*='description']")
        )
        desc = desc_tag.get_text(strip=True)[:250] if desc_tag else None

        # ── Posted date ────────────────────────────────────────────────────
        date_tag = card.select_one("span[class*='ago']") or card.select_one("time")
        posted = date_tag.get_text(strip=True) if date_tag else None

        return FreelanceProject(
            platform="Freelancer.com",
            title=title,
            url=url,
            budget_min=bmin,
            budget_max=bmax,
            budget_currency=currency,
            budget_type=btype,
            skills=skills,
            category=category,
            posted_date=posted,
            description_snippet=desc,
        )

    except Exception as exc:
        # Log and skip — never crash the whole loop over one bad card
        log.warning("  Error parsing Freelancer card: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Scraper 2: Mostaqel.com
# ---------------------------------------------------------------------------

MOSTAQEL_BASE = "https://mostaql.com"
MOSTAQEL_PROJECTS = "/projects"   # Arabic freelance project listings


def scrape_mostaqel(
    session: requests.Session,
    max_pages: int = 10,
) -> list[FreelanceProject]:
    """
    Scrape project listings from Mostaql.com (مستقل).

    Mostaql renders listings server-side with clean HTML, making
    BeautifulSoup parsing reliable.

    Args:
        session   : Shared requests.Session.
        max_pages : Max paginated pages to visit.

    Returns:
        List of FreelanceProject objects.
    """
    projects: list[FreelanceProject] = []

    # ── Ethics check ──────────────────────────────────────────────────────
    if not is_allowed_by_robots(MOSTAQEL_BASE, MOSTAQEL_PROJECTS):
        log.warning("Mostaqel robots.txt blocks project listings. Skipping.")
        return projects

    log.info("▶ Starting Mostaqel.com scrape (max %d pages) …", max_pages)

    for page_num in range(1, max_pages + 1):
        # Mostaqel paginates with ?page=N
        page_url = f"{MOSTAQEL_BASE}{MOSTAQEL_PROJECTS}?page={page_num}"
        log.info("  Page %d/%d → %s", page_num, max_pages, page_url)

        soup = fetch_page(session, page_url)
        if soup is None:
            log.warning("  Failed to fetch page %d. Stopping Mostaqel scrape.", page_num)
            break

        # ── Parse project cards ───────────────────────────────────────────
        # Mostaqel wraps each project in a <tr> inside a table, or in
        # a <div class="project-card"> depending on layout version.
        cards = (
            soup.select("table.projects-table tbody tr") or
            soup.select("div.project-row") or
            soup.select("[class*='project-card']") or
            soup.select("article.project")
        )

        if not cards:
            log.warning("  No project cards on page %d. Layout may have changed.", page_num)
            break

        for card in cards:
            project = _parse_mostaqel_card(card)
            if project:
                projects.append(project)

        log.info("  → %d projects collected so far.", len(projects))
        polite_sleep()

    log.info("✔ Mostaqel.com done. Total: %d projects.", len(projects))
    return projects


def _parse_mostaqel_card(card) -> Optional[FreelanceProject]:
    """
    Extract fields from a single Mostaqel project card.
    """
    try:
        # ── Title ──────────────────────────────────────────────────────────
        title_tag = (
            card.select_one("h2.project__title a") or
            card.select_one("h2 a") or
            card.select_one("a.project-title") or
            card.select_one("[class*='title'] a")
        )
        title = title_tag.get_text(strip=True) if title_tag else None
        if not title:
            return None

        # ── URL ────────────────────────────────────────────────────────────
        raw_href = title_tag.get("href", "") if title_tag else ""
        url = urljoin(MOSTAQEL_BASE, raw_href) if raw_href else None

        # ── Budget ─────────────────────────────────────────────────────────
        budget_tag = (
            card.select_one("div.project__price") or
            card.select_one("[class*='price']") or
            card.select_one("[class*='budget']") or
            card.select_one("span.budget")
        )
        raw_budget = budget_tag.get_text(strip=True) if budget_tag else None
        bmin, bmax, currency, btype = clean_budget(raw_budget)

        # ── Skills / Tags ──────────────────────────────────────────────────
        skills_tags = (
            card.select("ul.project__skills li") or
            card.select("[class*='skill']") or
            card.select("span.tag")
        )
        skills = [s.get_text(strip=True) for s in skills_tags if s.get_text(strip=True)]

        # ── Category ───────────────────────────────────────────────────────
        category_tag = (
            card.select_one("a.project__category") or
            card.select_one("[class*='category'] a") or
            card.select_one("span.category")
        )
        category = category_tag.get_text(strip=True) if category_tag else None

        # ── Description ────────────────────────────────────────────────────
        desc_tag = (
            card.select_one("div.project__brief") or
            card.select_one("p.project-description") or
            card.select_one("[class*='description']")
        )
        desc = desc_tag.get_text(strip=True)[:250] if desc_tag else None

        # ── Date ───────────────────────────────────────────────────────────
        date_tag = card.select_one("time") or card.select_one("[class*='date']")
        posted = date_tag.get("datetime") or \
                 (date_tag.get_text(strip=True) if date_tag else None)

        return FreelanceProject(
            platform="Mostaqel.com",
            title=title,
            url=url,
            budget_min=bmin,
            budget_max=bmax,
            budget_currency=currency,
            budget_type=btype,
            skills=skills,
            category=category,
            posted_date=posted,
            description_snippet=desc,
        )

    except Exception as exc:
        log.warning("  Error parsing Mostaqel card: %s", exc)
        return None


# ---------------------------------------------------------------------------
# JSON Exporter
# ---------------------------------------------------------------------------

def export_to_json(projects: list[FreelanceProject], filepath: str = "freelance_data.json") -> None:
    """
    Serialise the list of FreelanceProject dataclasses to a well-structured
    JSON file.

    Schema per record:
    {
        "platform":             "Freelancer.com",
        "title":                "Build a REST API",
        "url":                  "https://www.freelancer.com/projects/...",
        "budget_min":           50.0,
        "budget_max":           150.0,
        "budget_currency":      "USD",
        "budget_type":          "fixed",
        "skills":               ["Python", "Django", "REST API"],
        "category":             "Web Development",
        "posted_date":          "2 hours ago",
        "description_snippet":  "Looking for an experienced developer …"
    }
    """
    output = {
        "metadata": {
            "total_records": len(projects),
            "platforms": list({p.platform for p in projects}),
            "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "schema_version": "1.0",
        },
        "projects": [asdict(p) for p in projects],
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log.info("💾 Saved %d records → %s", len(projects), filepath)


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def main():
    """
    Orchestrates the full ETL pipeline:
      1. Create a shared HTTP session.
      2. Scrape each platform.
      3. Merge results.
      4. Export to JSON.
    """
    log.info("=" * 60)
    log.info("  Freelance Market Monitor — Scraper Starting")
    log.info("=" * 60)

    session = get_session()
    all_projects: list[FreelanceProject] = []

    # ── Platform 1: Freelancer.com ─────────────────────────────────────────
    # 10 pages × ~20 cards/page ≈ 200 records
    freelancer_projects = scrape_freelancer(session, max_pages=10)
    all_projects.extend(freelancer_projects)

    # Brief pause between platforms — looks more natural
    polite_sleep(3, 7)

    # ── Platform 2: Mostaqel.com ───────────────────────────────────────────
    # 10 pages × ~15 cards/page ≈ 150 records
    mostaqel_projects = scrape_mostaqel(session, max_pages=10)
    all_projects.extend(mostaqel_projects)

    # ── Summary ────────────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("  SCRAPE COMPLETE")
    log.info("  Freelancer.com : %d projects", len(freelancer_projects))
    log.info("  Mostaqel.com   : %d projects", len(mostaqel_projects))
    log.info("  TOTAL          : %d projects", len(all_projects))
    log.info("=" * 60)

    if not all_projects:
        log.warning("No data collected. The sites' HTML structure may have changed.")
        log.warning("Run with DEBUG logging to inspect: logging.basicConfig(level=logging.DEBUG)")
        return

    # ── Export ─────────────────────────────────────────────────────────────
    export_to_json(all_projects, "freelance_data.json")


if __name__ == "__main__":
    main()
