"""
scrapers/wellfound.py — Wellfound (AngelList) job scraper using Playwright.

Strategy:
  Wellfound uses DataDome + Cloudflare anti-bot protection that blocks headless
  browsers. We use a two-phase approach:

  Phase A (One-time setup): Launch headed browser → user solves CAPTCHA →
           cookies are saved to disk for future runs.

  Phase B (Production runs): Load saved cookies → scrape with stealth mode →
           extract jobs from page content → save to SQLite.

  Fallback: If cookies expire or are blocked, the scraper gracefully notifies
            via logging and returns 0 (never crashes).

Usage:
  # First run (interactive — solves CAPTCHA manually):
  python scrapers/wellfound.py --login

  # Production runs (automated, uses saved cookies):
  from scrapers.wellfound import scrape_wellfound
  new_count = scrape_wellfound()
"""

import json
import re
import logging
import time
import random
from datetime import datetime, timedelta, timezone
from typing import Optional
from pathlib import Path
from playwright.sync_api import sync_playwright, Page, BrowserContext
from bs4 import BeautifulSoup

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import BLACKLIST, MAX_JOB_AGE_HOURS, CREDENTIALS_DIR
from tracker.database import Database

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
COOKIES_FILE = CREDENTIALS_DIR / "wellfound_cookies.json"
BROWSER_STATE_DIR = CREDENTIALS_DIR / "wellfound_browser_state"

# ── URLs ──────────────────────────────────────────────────────────────────────
SEARCH_URLS = [
    "https://wellfound.com/role/l/software-engineer/india",
    "https://wellfound.com/role/l/software-engineer/india?remote=true",
]
MAX_PAGES = 5  # Max pages to scrape per URL


# ── Cookie Management ────────────────────────────────────────────────────────

def _save_cookies(context: BrowserContext):
    """Save browser cookies to disk for reuse."""
    cookies = context.cookies()
    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(COOKIES_FILE, "w") as f:
        json.dump(cookies, f, indent=2)
    logger.info(f"Saved {len(cookies)} cookies to {COOKIES_FILE}")


def _load_cookies(context: BrowserContext) -> bool:
    """Load saved cookies into the browser context."""
    if not COOKIES_FILE.exists():
        logger.warning("No saved cookies found. Run with --login first.")
        return False
    try:
        with open(COOKIES_FILE) as f:
            cookies = json.load(f)
        context.add_cookies(cookies)
        logger.info(f"Loaded {len(cookies)} cookies from disk.")
        return True
    except Exception as e:
        logger.error(f"Failed to load cookies: {e}")
        return False


# ── Interactive Login ─────────────────────────────────────────────────────────

def interactive_login():
    """
    Launch user's REAL Chrome browser with a persistent profile.
    This avoids Playwright's Chromium fingerprint entirely — looks 100% human.
    The user solves any CAPTCHA, and cookies are saved for future headless runs.
    """
    print("\n" + "=" * 60)
    print("  Wellfound Interactive Login (using your Chrome)")
    print("  Your real Chrome browser will open. Please:")
    print("  1. Solve any CAPTCHA or wait for the block to clear")
    print("  2. Log in to Wellfound if needed")
    print("  3. Make sure you can see job listings")
    print("  4. Come back here and press Enter")
    print("  ⚠️  Do NOT close the browser window!")
    print("=" * 60 + "\n")

    # Ensure directories exist
    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    BROWSER_STATE_DIR.mkdir(parents=True, exist_ok=True)

    profile_dir = str(BROWSER_STATE_DIR / "chrome_profile")

    with sync_playwright() as pw:
        # Use persistent context with real Chrome — not Playwright's Chromium
        context = pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            channel="chrome",  # Use installed Chrome, not Playwright Chromium
            viewport={"width": 1366, "height": 768},
            args=["--disable-blink-features=AutomationControlled"],
        )

        # Navigate
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://wellfound.com/role/l/software-engineer/india", timeout=60000)

        input("\n✋ Press ENTER after you can see job listings...\n")

        # Verify and report
        try:
            title = page.title()
            content_len = len(page.content())
            print(f"  Page title: {title}")
            print(f"  Content length: {content_len} chars")

            # Check if we actually got past the block
            if "restricted" in title.lower() or content_len < 5000:
                print("  ⚠️  Page still looks blocked. Try waiting a few minutes")
                print("     and refreshing, then press Enter again.")
                input("\n✋ Press ENTER when you can see real job listings...\n")
                title = page.title()
                content_len = len(page.content())
                print(f"  Page title: {title}")
                print(f"  Content length: {content_len} chars")
        except Exception:
            print("  ⚠️  Browser page closed, saving what we can...")

        # Save cookies
        try:
            _save_cookies(context)
            context.storage_state(path=str(BROWSER_STATE_DIR / "state.json"))
            print("\n✅ Cookies & browser state saved!")
        except Exception as e:
            print(f"\n❌ Failed to save: {e}")

        try:
            context.close()
        except Exception:
            pass

    print(f"   Cookie file: {COOKIES_FILE}")
    print(f"   Profile dir: {profile_dir}\n")


# ── Date Parsing ──────────────────────────────────────────────────────────────

def _parse_relative_date(text: str) -> Optional[datetime]:
    """Parse Wellfound's relative date strings into absolute datetimes."""
    if not text:
        return None

    text = text.lower().strip()
    now = datetime.now(timezone.utc)

    if text in ("just now", "today", "just posted", "new"):
        return now
    if text == "yesterday":
        return now - timedelta(days=1)

    match = re.match(r"(\d+)\s+(second|minute|hour|day|week|month)s?\s+ago", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        deltas = {
            "second": timedelta(seconds=amount),
            "minute": timedelta(minutes=amount),
            "hour": timedelta(hours=amount),
            "day": timedelta(days=amount),
            "week": timedelta(weeks=amount),
            "month": timedelta(days=amount * 30),
        }
        return now - deltas.get(unit, timedelta(days=999))

    # Try ISO format
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass

    return None


def _is_within_window(posted_date: Optional[datetime], max_hours: int) -> bool:
    """Check if a job was posted within the freshness window."""
    if posted_date is None:
        return True  # Include if we can't determine age
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_hours)
    return posted_date >= cutoff


def _is_blacklisted(company: str) -> bool:
    """Check if a company is in the blacklist (case-insensitive)."""
    return company.lower().strip() in [b.lower().strip() for b in BLACKLIST]


# ── Verification Handling ────────────────────────────────────────────────────

def _is_blocked(page: Page) -> bool:
    """Check if we're being served a CAPTCHA / challenge page."""
    try:
        content = page.content()
        indicators = [
            "captcha-delivery.com",
            "challenge-platform",
            "datadome",
            "cf-challenge",
            "verify you are human",
            "just a moment",
        ]
        return any(ind in content.lower() for ind in indicators)
    except Exception:
        return True


def _wait_and_check(page: Page, timeout_seconds: int = 15) -> bool:
    """Wait for dynamic content to load and check if blocked."""
    start = time.time()
    while time.time() - start < timeout_seconds:
        if not _is_blocked(page):
            return True
        time.sleep(2)
    return False


# ── Job Extraction ───────────────────────────────────────────────────────────

def _extract_from_next_data(page: Page) -> list[dict]:
    """Extract job listings from __NEXT_DATA__ JSON payload."""
    jobs = []
    try:
        next_data_raw = page.evaluate("""
            () => {
                const el = document.getElementById('__NEXT_DATA__');
                return el ? el.textContent : null;
            }
        """)

        if not next_data_raw:
            logger.info("__NEXT_DATA__ not found on page.")
            return jobs

        data = json.loads(next_data_raw)
        props = data.get("props", {}).get("pageProps", {})

        # Try known paths in the Next.js data structure
        job_listings = (
            props.get("jobListings")
            or props.get("jobs")
            or props.get("startupSearchResult", {}).get("listings", [])
            or props.get("seoLandingPageJobSearchResults", {}).get("jobListings", [])
            or _deep_find_jobs(props)
            or []
        )

        for item in job_listings:
            job = _normalize_job_data(item)
            if job and job.get("url"):
                jobs.append(job)

        logger.info(f"Extracted {len(jobs)} jobs from __NEXT_DATA__.")

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse __NEXT_DATA__: {e}")
    except Exception as e:
        logger.error(f"Error in __NEXT_DATA__ extraction: {e}")

    return jobs


def _deep_find_jobs(obj, depth=0) -> list:
    """Recursively search for job listing arrays in nested data."""
    if depth > 6:
        return []

    if isinstance(obj, list) and len(obj) > 0 and isinstance(obj[0], dict):
        sample = obj[0]
        job_indicators = {"title", "role", "jobTitle", "name", "slug"}
        if any(k in sample for k in job_indicators):
            return obj

    if isinstance(obj, dict):
        for value in obj.values():
            result = _deep_find_jobs(value, depth + 1)
            if result:
                return result

    if isinstance(obj, list):
        for item in obj:
            result = _deep_find_jobs(item, depth + 1)
            if result:
                return result

    return []


def _extract_from_dom(page: Page) -> list[dict]:
    """Fallback: Parse jobs from the rendered DOM."""
    jobs = []
    try:
        html = page.content()
        soup = BeautifulSoup(html, "lxml")

        # Wellfound uses company-grouped cards with job listings inside
        seen_urls = set()

        # Strategy 1: Find all /jobs/ links
        for link in soup.find_all("a", href=re.compile(r"/jobs/")):
            href = link.get("href", "")
            if not href or href == "/jobs/":
                continue

            full_url = f"https://wellfound.com{href}" if href.startswith("/") else href

            # Deduplicate
            clean_url = full_url.split("?")[0]
            if clean_url in seen_urls:
                continue
            seen_urls.add(clean_url)

            role = link.get_text(strip=True)
            if not role or len(role) < 3 or len(role) > 200:
                continue

            # Find enclosing card
            card = _find_card_parent(link)

            company = _extract_text_near(card, r"/company/", "a", "Unknown")
            salary = _extract_text_near(card, r"[₹$€£\d].*[kKmMlL]", "span")
            location = _extract_text_near(
                card,
                r"(Remote|India|Bangalore|Mumbai|Delhi|Hyderabad|Pune|Chennai|Noida|Gurugram)",
                "span",
            )
            posted = _extract_text_near(card, r"(ago|yesterday|today|just)", "span")
            remote = _detect_remote(card)

            jobs.append({
                "platform": "wellfound",
                "company": company,
                "role": role,
                "jd": "",
                "salary": salary,
                "location": location,
                "remote": remote,
                "team_size": None,
                "subsidies": None,
                "url": clean_url,
                "posted_date": posted,
            })

        # Strategy 2: Look for structured data
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                ld = json.loads(script.string)
                if isinstance(ld, dict) and ld.get("@type") == "JobPosting":
                    jobs.append(_normalize_ld_json(ld))
                elif isinstance(ld, list):
                    for item in ld:
                        if isinstance(item, dict) and item.get("@type") == "JobPosting":
                            jobs.append(_normalize_ld_json(item))
            except (json.JSONDecodeError, TypeError):
                continue

        logger.info(f"Extracted {len(jobs)} jobs from DOM.")

    except Exception as e:
        logger.error(f"DOM extraction error: {e}")

    return jobs


def _find_card_parent(element):
    """Walk up the DOM to find the job card container."""
    parent = element.parent
    for _ in range(10):
        if parent is None:
            return element
        classes = parent.get("class", [])
        class_str = " ".join(classes) if isinstance(classes, list) else str(classes)
        if any(kw in class_str.lower() for kw in ["card", "listing", "component", "job", "startup"]):
            return parent
        if parent.name in ("article", "section", "li"):
            return parent
        parent = parent.parent
    return element.parent or element


def _extract_text_near(card, pattern: str, tag: str = "span", default=None):
    """Extract text matching a pattern from within a card element."""
    if card is None:
        return default
    try:
        for el in card.find_all(string=re.compile(pattern, re.I)):
            text = el.strip()
            if text:
                return text
    except Exception:
        pass
    return default


def _detect_remote(card) -> Optional[str]:
    """Detect remote/onsite/hybrid status from card text."""
    if card is None:
        return None
    text = card.get_text().lower()
    if "remote" in text:
        return "Remote"
    elif "hybrid" in text:
        return "Hybrid"
    elif "onsite" in text or "in office" in text or "in-office" in text:
        return "On-site"
    return None


def _normalize_ld_json(ld: dict) -> dict:
    """Normalize a JSON-LD JobPosting into our standard format."""
    return {
        "platform": "wellfound",
        "company": ld.get("hiringOrganization", {}).get("name", "Unknown"),
        "role": ld.get("title", "Unknown Role"),
        "jd": ld.get("description", ""),
        "salary": ld.get("baseSalary", {}).get("value", ""),
        "location": ld.get("jobLocation", {}).get("address", {}).get("addressLocality", ""),
        "remote": "Remote" if ld.get("jobLocationType") == "TELECOMMUTE" else None,
        "team_size": None,
        "subsidies": None,
        "url": ld.get("url", ""),
        "posted_date": ld.get("datePosted", ""),
    }


def _normalize_job_data(item: dict) -> Optional[dict]:
    """Normalize a job entry from __NEXT_DATA__ into our standard format."""
    try:
        startup = item.get("startup") or item.get("company") or {}
        job_data = item.get("job") or item

        company = (
            startup.get("name")
            or startup.get("companyName")
            or item.get("companyName")
            or item.get("company_name")
            or "Unknown"
        )

        role = (
            job_data.get("title")
            or job_data.get("role")
            or job_data.get("jobTitle")
            or item.get("title")
            or "Unknown Role"
        )

        jd = (
            job_data.get("description")
            or job_data.get("jobDescription")
            or item.get("description")
            or ""
        )
        if jd and "<" in jd:
            jd = BeautifulSoup(jd, "lxml").get_text(separator="\n", strip=True)

        salary = job_data.get("compensation") or job_data.get("salary") or item.get("compensation") or ""
        if isinstance(salary, dict):
            salary = f"{salary.get('min', '')} - {salary.get('max', '')} {salary.get('currency', '')}"

        location = job_data.get("locationNames") or item.get("locationNames") or job_data.get("location") or ""
        if isinstance(location, list):
            location = ", ".join(location)

        remote = job_data.get("remote") or item.get("remote") or item.get("remoteDescription") or ""
        if isinstance(remote, bool):
            remote = "Remote" if remote else "On-site"

        team_size = startup.get("companySize") or startup.get("teamSize") or ""

        # Build URL
        slug = job_data.get("slug") or item.get("slug") or job_data.get("id") or item.get("id") or ""
        startup_slug = startup.get("slug", "")
        job_id = job_data.get("id") or item.get("id") or ""

        url = ""
        if slug and "/" in str(slug):
            url = f"https://wellfound.com{slug}" if slug.startswith("/") else f"https://wellfound.com/jobs/{slug}"
        elif startup_slug and job_id:
            url = f"https://wellfound.com/company/{startup_slug}/jobs/{job_id}"
        elif slug:
            url = f"https://wellfound.com/jobs/{slug}"

        posted_str = (
            job_data.get("postedAt")
            or job_data.get("liveStartAt")
            or item.get("postedAt")
            or item.get("created_at")
            or ""
        )

        return {
            "platform": "wellfound",
            "company": str(company).strip(),
            "role": str(role).strip(),
            "jd": str(jd).strip()[:10000],  # Cap JD length
            "salary": str(salary).strip() if salary else None,
            "location": str(location).strip() if location else None,
            "remote": str(remote).strip() if remote else None,
            "team_size": str(team_size).strip() if team_size else None,
            "subsidies": None,
            "url": url,
            "posted_date": posted_str if posted_str else None,
        }

    except Exception as e:
        logger.error(f"Error normalizing job: {e}")
        return None


# ── Individual Job Detail Page ───────────────────────────────────────────────

def _enrich_job_details(page: Page, job: dict) -> dict:
    """Visit individual job page to get full JD when missing."""
    if job.get("jd") and len(job["jd"]) > 100:
        return job
    if not job.get("url"):
        return job

    try:
        page.goto(job["url"], wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)

        if _is_blocked(page):
            logger.warning(f"Blocked on job page: {job['url']}")
            return job

        # Try __NEXT_DATA__
        next_data = page.evaluate("""
            () => {
                const el = document.getElementById('__NEXT_DATA__');
                return el ? el.textContent : null;
            }
        """)
        if next_data:
            data = json.loads(next_data)
            props = data.get("props", {}).get("pageProps", {})
            jd = (
                props.get("jobListing", {}).get("description")
                or props.get("job", {}).get("description")
                or ""
            )
            if jd and "<" in jd:
                jd = BeautifulSoup(jd, "lxml").get_text(separator="\n", strip=True)
            if jd and len(jd) > 50:
                job["jd"] = jd[:10000]

        # DOM fallback for JD
        if not job.get("jd") or len(job["jd"]) < 50:
            soup = BeautifulSoup(page.content(), "lxml")
            for sel in ["[class*='description']", "[class*='job-description']", "article", "section"]:
                el = soup.select_one(sel)
                if el and len(el.get_text()) > 100:
                    job["jd"] = el.get_text(separator="\n", strip=True)[:10000]
                    break

        time.sleep(random.uniform(1, 2))

    except Exception as e:
        logger.warning(f"Could not enrich {job['url']}: {e}")

    return job


# ── Main Scraper ─────────────────────────────────────────────────────────────

def scrape_wellfound(
    headless: bool = True,
    max_pages: int = MAX_PAGES,
    enrich_jd: bool = True,
) -> int:
    """
    Scrape Wellfound for software engineering jobs.

    Requires cookies from a prior `interactive_login()` session.

    Returns:
        Number of new jobs added to SQLite.
    """
    db = Database()
    db.init_db()
    new_jobs_count = 0

    logger.info("=" * 60)
    logger.info("Starting Wellfound scraper...")
    logger.info("=" * 60)

    with sync_playwright() as pw:
        # Check if we have saved browser state
        state_file = BROWSER_STATE_DIR / "state.json"
        has_state = state_file.exists()

        browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        # Create context with saved state if available
        context_kwargs = {
            "viewport": {"width": 1920, "height": 1080},
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "locale": "en-US",
            "timezone_id": "Asia/Kolkata",
        }

        if has_state:
            context_kwargs["storage_state"] = str(state_file)
            logger.info("Loading saved browser state.")

        context = browser.new_context(**context_kwargs)

        # Apply stealth
        try:
            from playwright_stealth import Stealth
            Stealth().use_sync(context)
        except ImportError:
            pass

        # Load cookies if not using state
        if not has_state:
            _load_cookies(context)

        page = context.new_page()

        for base_url in SEARCH_URLS:
            logger.info(f"\nScraping: {base_url}")

            for page_num in range(1, max_pages + 1):
                url = f"{base_url}&page={page_num}" if "?" in base_url else f"{base_url}?page={page_num}"
                if page_num == 1:
                    url = base_url

                logger.info(f"  Page {page_num}: {url}")

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3000)

                    # Check if blocked
                    if _is_blocked(page):
                        logger.warning(
                            "  ⛔ Blocked by anti-bot protection. "
                            "Re-run with --login to refresh cookies."
                        )
                        break

                    # Extract jobs — try __NEXT_DATA__ first, then DOM
                    raw_jobs = _extract_from_next_data(page)
                    if not raw_jobs:
                        raw_jobs = _extract_from_dom(page)

                    if not raw_jobs:
                        logger.info(f"  No jobs found on page {page_num}. Stopping.")
                        break

                    # Filter and save
                    page_new = 0
                    for job in raw_jobs:
                        if not job.get("url"):
                            continue

                        if _is_blacklisted(job["company"]):
                            logger.info(f"  ⛔ Skipped blacklisted: {job['company']}")
                            continue

                        if db.job_exists(job["url"]):
                            continue

                        posted_dt = _parse_relative_date(str(job.get("posted_date", "")))
                        if not _is_within_window(posted_dt, MAX_JOB_AGE_HOURS):
                            logger.debug(f"  ⏭️  Too old: {job['company']} — {job['role']}")
                            continue

                        # Enrich with full JD if needed
                        if enrich_jd and (not job.get("jd") or len(job.get("jd", "")) < 50):
                            job = _enrich_job_details(page, job)
                            page.goto(url, wait_until="domcontentloaded", timeout=30000)
                            page.wait_for_timeout(2000)

                        # Save to SQLite
                        try:
                            job_id = db.insert_job(job)
                            page_new += 1
                            new_jobs_count += 1
                            logger.info(
                                f"  ✅ New: {job['company']} — {job['role']} (ID: {job_id})"
                            )
                        except Exception as e:
                            logger.error(f"  ❌ Save failed: {job['company']} — {e}")

                    logger.info(f"  Page {page_num}: {page_new} new jobs.")

                    # Respectful delay between pages
                    time.sleep(random.uniform(2, 4))

                except Exception as e:
                    logger.error(f"  ❌ Error on page {page_num}: {e}")
                    continue

        # Save updated cookies for next run
        _save_cookies(context)

        browser.close()

    # Summary
    all_jobs = db.get_all_jobs()
    logger.info("\n" + "=" * 60)
    logger.info(f"Scraper finished. {new_jobs_count} new jobs added.")
    logger.info(f"Total jobs in database: {len(all_jobs)}")
    logger.info("=" * 60)

    db.close()
    return new_jobs_count


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    import argparse

    parser = argparse.ArgumentParser(description="Wellfound Job Scraper")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Launch headed browser for interactive login & cookie capture",
    )
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--headed", action="store_true", help="Run in headed mode")
    parser.add_argument("--no-enrich", action="store_true", help="Skip JD enrichment")
    parser.add_argument("--pages", type=int, default=3, help="Max pages per URL")
    args = parser.parse_args()

    if args.login:
        interactive_login()
    else:
        count = scrape_wellfound(
            headless=not args.headed,
            max_pages=args.pages,
            enrich_jd=not args.no_enrich,
        )
        print(f"\n🎯 Result: {count} new jobs scraped.")
