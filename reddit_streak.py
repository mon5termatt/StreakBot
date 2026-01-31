#!/usr/bin/env python3
"""
Reddit streak bot: upvotes one of the top 3 posts of the day on a subreddit,
waits a random time, then removes the upvote. Runs at a configured time daily.
Auth: grabs cookies from your Chrome profile but uses its own window (Chrome can stay open). Or use cookies_file.
"""

import json
import logging
import os
import random
import re
import time
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

# Paths
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
USER_DATA_DIR = Path(__file__).resolve().parent / "browser_profile"

log = logging.getLogger(__name__)


def load_config():
    log.debug("Loading config from %s", CONFIG_PATH)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    subreddits = get_subreddits(cfg)
    log.info(
        "Config loaded: subreddits=r/%s, run_time=%s, wait=%s-%ss",
        ", r/".join(subreddits) if subreddits else "?",
        cfg.get("run_time"),
        cfg.get("wait_seconds_min"),
        cfg.get("wait_seconds_max"),
    )
    return cfg


def get_subreddits(config) -> list[str]:
    """Return list of subreddit names from config (subreddits list or single subreddit)."""
    if "subreddits" in config and config["subreddits"]:
        raw = config["subreddits"]
        if isinstance(raw, list):
            return [str(s).strip() for s in raw if s]
        return [str(raw).strip()]
    if config.get("subreddit"):
        return [str(config["subreddit"]).strip()]
    return []


def get_user_urls(config) -> tuple[str, str]:
    """Return (streak_check_url, upvoted_page_url) from reddit_username or explicit config."""
    username = (config.get("reddit_username") or "").strip()
    streak = (config.get("streak_check_url") or "").strip()
    upvoted = (config.get("upvoted_page_url") or "").strip()
    if username:
        if not streak:
            streak = f"https://www.reddit.com/user/{username}/achievements/category/3/"
        if not upvoted:
            upvoted = f"https://www.reddit.com/user/{username}/upvoted/"
    return (streak, upvoted)


def load_cookies_from_chrome(domain_name: str = "reddit.com") -> list[dict]:
    """Load cookies from the Chrome profile (our own window; Chrome can stay open)."""
    try:
        import browser_cookie3
    except ImportError:
        log.error("browser_cookie3 not installed. Run: pip install browser-cookie3")
        return []
    try:
        cj = browser_cookie3.chrome(domain_name=domain_name)
    except Exception as e:
        log.exception("Failed to load Chrome cookies: %s. Try closing Chrome, or use cookies_file.", e)
        return []
    out = []
    for c in cj:
        domain = c.domain if c.domain.startswith(".") else f".{c.domain}"
        cookie = {
            "name": c.name,
            "value": c.value,
            "domain": domain,
            "path": c.path or "/",
            "secure": getattr(c, "secure", False),
            "httpOnly": getattr(c, "has_nonstandard_attr", lambda x: False)("HttpOnly") or False,
            "sameSite": "Lax",
        }
        if c.expires:
            cookie["expires"] = int(c.expires)
        out.append(cookie)
    return out


def load_cookies_from_json(file_path: Path, domain_filter: str = "reddit.com") -> list[dict]:
    """Load cookies from a JSON file (EditThisCookie / browser export format)."""
    with open(file_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        data = [data]
    out = []
    same_site_map = {"no_restriction": "None", "strict": "Strict", "lax": "Lax"}
    for c in data:
        domain = c.get("domain", "")
        if domain_filter not in domain:
            continue
        if not domain.startswith("."):
            domain = f".{domain}"
        exp = c.get("expirationDate")
        if c.get("session") or exp is None:
            expires = None
        else:
            try:
                expires = int(float(exp))
            except (TypeError, ValueError):
                expires = None
        ss = c.get("sameSite")
        if ss in same_site_map:
            same_site = same_site_map[ss]
        else:
            same_site = "Lax"
        cookie = {
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": domain,
            "path": c.get("path", "/"),
            "secure": c.get("secure", False),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": same_site,
        }
        if expires is not None:
            cookie["expires"] = expires
        out.append(cookie)
    return out


def load_cookies_from_netscape_file(file_path: Path, domain_filter: str = "reddit.com") -> list[dict]:
    """Load cookies from a Netscape-format cookies.txt (from extensions like 'Get cookies.txt')."""
    cookies = []
    with open(file_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, path, secure, expires, name = parts[0], parts[2], parts[3], parts[4], parts[5]
            value = "\t".join(parts[6:])  # value can contain tabs
            if domain_filter not in domain:
                continue
            try:
                exp = int(expires)
            except ValueError:
                exp = -1
            cookie = {
                "name": name,
                "value": value,
                "domain": domain if domain.startswith(".") else f".{domain}",
                "path": path,
                "secure": secure.lower() == "true",
                "httpOnly": False,
                "sameSite": "Lax",
            }
            if exp >= 0:
                cookie["expires"] = exp
            cookies.append(cookie)
    return cookies


def run_upvote_flow(config):
    """Open Reddit, upvote one of top 3 posts, wait, then remove upvote."""
    subreddits = get_subreddits(config)
    if not subreddits:
        log.error("No subreddit(s) in config. Set subreddits: [\"python\", ...] or subreddit: \"python\"")
        return False
    subreddit = random.choice(subreddits)
    url = f"https://www.reddit.com/r/{subreddit}/top/?t=day"

    wait_min = config["wait_seconds_min"]
    wait_max = config["wait_seconds_max"]
    wait_seconds = random.uniform(wait_min, wait_max)
    log.info("Starting upvote flow: r/%s (from %d subreddit(s)), will wait %.1f–%.1fs before removing (chose %.1fs)", subreddit, len(subreddits), wait_min, wait_max, wait_seconds)

    streak_check_url, upvoted_page_url = get_user_urls(config)
    use_chrome_cookies = config.get("use_chrome_cookies", True)
    cookies_file = config.get("cookies_file")
    if cookies_file:
        cookies_path = Path(cookies_file)
        if not cookies_path.is_absolute():
            cookies_path = Path(__file__).resolve().parent / cookies_path
    else:
        cookies_path = None

    browser = None  # set when using our own window (cookies file or Chrome cookies)
    with sync_playwright() as p:
        if cookies_path and cookies_path.exists():
            log.info("Auth: using cookies file %s (our own window)", cookies_path)
            browser = p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context()
            if cookies_path.suffix.lower() == ".json":
                cookies = load_cookies_from_json(cookies_path)
            else:
                cookies = load_cookies_from_netscape_file(cookies_path)
            if cookies:
                context.add_cookies(cookies)
                log.info("Loaded %d cookies from %s", len(cookies), cookies_path)
            else:
                log.warning("No Reddit cookies found in %s", cookies_path)
            page = context.new_page()
            log.debug("Created new page (cookies context)")
            page.bring_to_front()
        elif use_chrome_cookies:
            log.info("Auth: grabbing cookies from Chrome profile, using our own window")
            browser = p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context()
            cookies = load_cookies_from_chrome("reddit.com")
            if not cookies:
                log.error("No Reddit cookies from Chrome. Log in to Reddit in Chrome first, or use cookies_file.")
                browser.close()
                return False
            context.add_cookies(cookies)
            log.info("Loaded %d cookies from Chrome profile", len(cookies))
            page = context.new_page()
            page.bring_to_front()
        else:
            log.info("Auth: using script browser profile at %s", USER_DATA_DIR)
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(USER_DATA_DIR),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            log.debug("Persistent context launched")
            log.info("Waiting 4s for browser to finish loading...")
            time.sleep(4)
            page = context.pages[0] if context.pages else context.new_page()
            log.info("Using %s for Reddit", "first tab" if context.pages else "new tab")
            page.bring_to_front()

        page.set_default_timeout(30_000)

        try:
            if streak_check_url:
                log.info("Checking streak status before upvote...")
                reached, days = check_streak_on_page(page, streak_check_url)
                if days is not None:
                    log.info("Streak: %d day(s)", days)
                if reached and not config.get("test_mode"):
                    log.info("Streak already reached today, skipping upvote.")
                    return True
                if reached and config.get("test_mode"):
                    log.info("Streak already reached today; test_mode: doing upvote anyway.")
                elif not reached:
                    log.info("Streak not reached today — proceeding with upvote.")

            log.info("Navigating to %s", url)
            page.bring_to_front()
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_load_state("load", timeout=10_000)
            # Reddit often never reaches networkidle (long-lived connections); wait for content
            log.info("Waiting 3s for vote buttons to render...")
            time.sleep(3)

            # Reddit: only real posts (shreddit-post), not ads (shreddit-ad-post). Upvote button has [icon-name="upvote"].
            upvotes = page.locator('shreddit-post button:has([icon-name="upvote"])').all()
            if not upvotes:
                upvotes = page.locator('shreddit-post button[aria-label="upvote"]').all()
            if not upvotes:
                upvotes = page.locator('shreddit-post [role="button"][aria-label="upvote"]').all()
            if not upvotes:
                log.debug("No upvote in shreddit-post, trying page-wide selectors")
                upvotes = page.locator('button:has([icon-name="upvote"])').all()
            if not upvotes:
                upvotes = page.locator('button[aria-label="upvote"], [aria-label="upvote"]').all()

            if len(upvotes) < 1:
                log.error("No upvote buttons found. Are you logged in? Log in to Reddit in the browser.")
                context.close()
                return False

            # Pick one of the first 3 real posts; get its post URL only (do not click upvote on listing)
            n = min(3, len(upvotes))
            choice = random.randint(0, n - 1) if n > 0 else 0
            btn = upvotes[choice]
            log.info("Found %d real post(s), choosing #%d of top %d", len(upvotes), choice + 1, n)
            btn.scroll_into_view_if_needed()
            time.sleep(0.3)

            # Get post URL from the chosen post (a[slot="full-post-link"] with matching data-ks-id = post id)
            post_url = None
            try:
                url_result = btn.evaluate("""el => {
                    const root = el.getRootNode();
                    const post = (root.nodeType === 11 && root.host) ? root.host : (el.closest('shreddit-post') || el.closest('article') || el.closest('[id^="t3_"]'));
                    if (!post) return null;
                    const postId = post.id || post.getAttribute('id') || '';
                    function findLink(r) {
                        if (!r) return null;
                        if (postId) {
                            const a = r.querySelector('a[slot="full-post-link"][data-ks-id="' + postId + '"]') || r.querySelector('a[data-ks-id="' + postId + '"]');
                            if (a && a.href) return (a.href || '').split('?')[0];
                        }
                        const a = r.querySelector('a[slot="full-post-link"]') || r.querySelector('a[data-ks-id^="t3_"]') || r.querySelector('a[href*="/comments/"]');
                        return a && a.href ? (a.href || '').split('?')[0] : null;
                    }
                    return (post.shadowRoot ? findLink(post.shadowRoot) : null) || findLink(post);
                }""")
                if url_result:
                    post_url = url_result
            except Exception:
                pass
            if not post_url:
                log.error("Could not get post URL from chosen post; skipping.")
                context.close()
                return False

            post_url_abs = post_url if post_url.startswith("http") else ("https://www.reddit.com" + (post_url if post_url.startswith("/") else "/" + post_url))
            log.info("Opening post: %s", post_url_abs)
            page.goto(post_url_abs, wait_until="domcontentloaded")
            page.wait_for_load_state("load", timeout=10_000)
            time.sleep(2)

            # On the post page: click upvote (to upvote)
            upvote_btns = page.locator('[data-post-click-location="vote"] button[upvote]').all()
            if not upvote_btns:
                upvote_btns = page.locator('shreddit-post button:has([icon-name="upvote"]), shreddit-post button[upvote]').all()
            if not upvote_btns:
                upvote_btns = page.locator('button:has([icon-name="upvote"]), button[upvote], button[aria-label="upvote"]').all()
            if upvote_btns:
                upvote_btns[0].scroll_into_view_if_needed()
                time.sleep(0.3)
                upvote_btns[0].click()
                log.info("Upvoted on post page.")
            else:
                log.warning("No upvote button found on post page.")
                context.close()
                return False

            log.info("Waiting %.1fs before removing upvote...", wait_seconds)
            time.sleep(wait_seconds)

            # On the same post page: click upvote again to remove it (button is now pressed)
            unvote_btns = page.locator('[data-post-click-location="vote"] button[upvote][aria-pressed="true"]').all()
            if not unvote_btns:
                unvote_btns = page.locator('[data-post-click-location="vote"] button[aria-pressed="true"]').all()
            if not unvote_btns:
                unvote_btns = page.locator('button[upvote][aria-pressed="true"]').all()
            if not unvote_btns:
                unvote_btns = page.locator('shreddit-post button:has([icon-name="upvote-fill"]), shreddit-post button[upvote][aria-pressed="true"]').all()
            if not unvote_btns:
                unvote_btns = page.locator('shreddit-post button[aria-pressed="true"]').all()
            if not unvote_btns:
                unvote_btns = page.locator('button:has([icon-name="upvote-fill"]), button[upvote][aria-pressed="true"]').all()
            if not unvote_btns:
                unvote_btns = page.locator('button:has([icon-name="unvote"]), button[aria-label="unvote"]').all()
            if not unvote_btns:
                unvote_btns = page.locator('button:has([icon-name="upvote"]), button[aria-label="upvote"]').all()
            if unvote_btns:
                unvote_btns[0].scroll_into_view_if_needed()
                time.sleep(0.3)
                unvote_btns[0].click()
                log.info("Removed upvote on post page.")
            else:
                log.warning("No vote button found on post page to remove upvote; upvote may still be active.")

            if streak_check_url:
                log.info("Rechecking streak status after upvote...")
                reached, days = check_streak_on_page(page, streak_check_url)
                if days is not None:
                    log.info("Streak: %d day(s)", days)
                if reached:
                    log.info("Streak status: Reached today.")
                else:
                    log.info("Streak status: NOT reached today (may take a moment to update).")
        except Exception as e:
            log.exception("Error during upvote flow: %s", e)
            return False
        finally:
            log.debug("Closing context and browser")
            context.close()
            if browser is not None:
                browser.close()

    return True


def check_streak_on_page(page, streak_url: str) -> tuple[bool, int | None]:
    """Navigate to streak_url, check fire image and day count. Returns (reached_today, days or None)."""
    page.goto(streak_url, wait_until="domcontentloaded")
    page.wait_for_load_state("load", timeout=10_000)
    time.sleep(2)

    fire_el = page.locator('img[data-testid="streak-fire-image"]').first
    fire_el.wait_for(state="visible", timeout=15_000)
    src = fire_el.get_attribute("src") or ""
    alt = fire_el.get_attribute("alt") or ""

    reached = not ("fire-faded" in src or "not been reached" in alt.lower()) and (
        "fire.png" in src or "has been reached" in alt.lower()
    )

    streak_days = None
    try:
        text = page.locator("span.current-streak").first.inner_text(timeout=5000)
        if text and text.strip().isdigit():
            streak_days = int(text.strip())
    except Exception:
        pass

    return (reached, streak_days)


def run_streak_check(config):
    """Open Reddit achievements page and report if today's streak has been reached (fire vs fire-faded)."""
    streak_url, _ = get_user_urls(config)
    if not streak_url:
        log.error("test_mode is true but reddit_username (or streak_check_url) is not set in config.")
        return False

    use_chrome_cookies = config.get("use_chrome_cookies", True)
    cookies_file = config.get("cookies_file")
    if cookies_file:
        cookies_path = Path(cookies_file)
        if not cookies_path.is_absolute():
            cookies_path = Path(__file__).resolve().parent / cookies_path
    else:
        cookies_path = None

    browser = None
    with sync_playwright() as p:
        if cookies_path and cookies_path.exists():
            log.info("Auth: using cookies file %s (test mode)", cookies_path)
            browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context()
            if cookies_path.suffix.lower() == ".json":
                cookies = load_cookies_from_json(cookies_path)
            else:
                cookies = load_cookies_from_netscape_file(cookies_path)
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
        elif use_chrome_cookies:
            log.info("Auth: grabbing cookies from Chrome profile (test mode)")
            browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context()
            cookies = load_cookies_from_chrome("reddit.com")
            if not cookies:
                log.error("No Reddit cookies from Chrome. Log in to Reddit in Chrome first, or use cookies_file.")
                browser.close()
                return False
            context.add_cookies(cookies)
            page = context.new_page()
        else:
            log.info("Auth: using script browser profile (test mode)")
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(USER_DATA_DIR),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            time.sleep(4)
            page = context.pages[0] if context.pages else context.new_page()

        page.set_default_timeout(20_000)
        try:
            log.info("Navigating to achievements page: %s", streak_url)
            page.goto(streak_url, wait_until="domcontentloaded")
            page.wait_for_load_state("load", timeout=10_000)
            time.sleep(2)

            fire_el = page.locator('img[data-testid="streak-fire-image"]').first
            fire_el.wait_for(state="visible", timeout=15_000)
            src = fire_el.get_attribute("src") or ""
            alt = fire_el.get_attribute("alt") or ""

            # Streak day count is in span.current-streak (e.g. "487")
            streak_days = None
            try:
                text = page.locator("span.current-streak").first.inner_text(timeout=5000)
                if text and text.strip().isdigit():
                    streak_days = int(text.strip())
            except Exception:
                pass

            if streak_days is not None:
                log.info("Streak: %d day(s)", streak_days)
            else:
                log.info("Streak: day count not found (check page structure)")

            if "fire-faded" in src or "not been reached" in alt.lower():
                log.info("Streak status: NOT reached today (fire-faded). You still need to upvote today.")
            elif "fire.png" in src or "has been reached" in alt.lower():
                log.info("Streak status: Reached today (fire). You're good.")
            else:
                log.warning("Streak status: unknown (src=%s, alt=%s)", src[:80] if src else "", alt[:80] if alt else "")
        except Exception as e:
            log.exception("Streak check failed: %s", e)
            return False
        finally:
            context.close()
            if browser is not None:
                browser.close()
    return True


def main():
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if level == logging.DEBUG:
        log.debug("Verbose (DEBUG) logging enabled")

    config = load_config()

    if config.get("test_mode"):
        log.info("test_mode is true — running full upvote flow (even if streak already reached)")
        run_upvote_flow(config)
        return

    if config.get("run_now"):
        log.info("run_now is true — running once immediately")
        run_upvote_flow(config)
        return

    run_time = config["run_time"]  # e.g. "09:00"
    hour, minute = map(int, run_time.split(":"))
    subreddits = get_subreddits(config)
    log.info("Scheduler started: run daily at %s, subreddits r/%s", run_time, ", r/".join(subreddits) if subreddits else "?")
    log.info("Minimize this window; browser will open at the scheduled time")
    log.info("First run: log in to Reddit in the browser window when it opens")

    while True:
        now = time.localtime()
        if now.tm_hour == hour and now.tm_min == minute:
            log.info("Scheduled time reached — starting upvote flow")
            run_upvote_flow(config)
            log.debug("Sleeping 65s to avoid re-running in same minute")
            time.sleep(65)
        time.sleep(30)


if __name__ == "__main__":
    main()
