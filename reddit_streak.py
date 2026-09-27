#!/usr/bin/env python3
"""
Reddit streak bot: upvotes one of the top 3 posts of the day on a subreddit,
waits a random time, then removes the upvote. Runs at a configured time daily.
Auth: asks the StreakBot Chrome extension for Reddit cookies, then uses its own window. Or use cookies_file.
"""

import os
import subprocess
import sys
from pathlib import Path

# Detect venv and create one if not running inside a virtual environment
_BASE_DIR = Path(__file__).resolve().parent
_VENV_DIR = _BASE_DIR / ".venv"


def _in_venv() -> bool:
    """True if we're running inside a virtual environment."""
    if os.environ.get("VIRTUAL_ENV"):
        return True
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _ensure_venv() -> None:
    """If not in a venv, create .venv, install deps, and re-exec this script inside it."""
    if _in_venv():
        return
    if not _VENV_DIR.exists():
        print("No virtual environment detected. Creating .venv and installing dependencies...")
        import venv
        venv.create(_VENV_DIR, with_pip=True)
    py = _VENV_DIR / "Scripts" / "python.exe" if os.name == "nt" else _VENV_DIR / "bin" / "python"
    if not py.exists():
        py = _VENV_DIR / "bin" / "python"  # fallback
    req = _BASE_DIR / "requirements.txt"
    if req.exists():
        subprocess.run([str(py), "-m", "pip", "install", "-r", str(req)], check=True, cwd=_BASE_DIR)
    subprocess.run([str(py), "-m", "playwright", "install", "chromium"], check=True, cwd=_BASE_DIR)
    print("Restarting inside virtual environment...")
    os.execv(str(py), [str(py), os.path.abspath(__file__)] + sys.argv[1:])


_ensure_venv()

import json
import logging
import random
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

# Paths
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
USER_DATA_DIR = Path(__file__).resolve().parent / "browser_profile"
TOS_ACCEPTED_PATH = Path(__file__).resolve().parent / ".streakbot_tos_accepted"
EXTENSION_DIR = Path(__file__).resolve().parent / "extension"
EXTENSION_ID = "gmeopkibjanmlpphhmblkiaakdfpcplc"

log = logging.getLogger(__name__)


def require_tos_acceptance() -> bool:
    """One-time prompt: user must accept that use may violate Reddit TOS. Returns True if accepted (or already accepted)."""
    if TOS_ACCEPTED_PATH.exists():
        return True
    print()
    print("  Reddit Streak Bot — Terms of Service notice")
    print("  --------------------------------------------")
    print("  This software automates interaction with Reddit (upvoting and removing votes).")
    print("  Such automation may violate Reddit's Terms of Service and can result in")
    print("  account restrictions or bans. This project is not affiliated with Reddit.")
    print()
    while True:
        try:
            reply = input("  Type 'yes' to accept this risk and continue, or 'no' to exit: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if reply in ("yes", "y"):
            try:
                TOS_ACCEPTED_PATH.write_text("accepted", encoding="utf-8")
            except OSError:
                log.warning("Could not write TOS acceptance file; continuing anyway.")
            return True
        if reply in ("no", "n"):
            print("  Exiting.")
            return False
        print("  Please type 'yes' or 'no'.")


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


def _time_to_minutes(s: str) -> int:
    """Convert 'HH:MM' to minutes since midnight."""
    h, m = map(int, str(s).strip().split(":", 1))
    return h * 60 + m


def _minutes_to_time(m: int) -> str:
    """Convert minutes since midnight to 'HH:MM'."""
    return f"{m // 60:02d}:{m % 60:02d}"


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


def _chrome_exe() -> Path | None:
    """Path to the installed Chrome executable."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def write_extension_cookies_json(cookies: list, dest: Path) -> int:
    """Write cookies from the Chrome extension. Returns count written, or 0 if there is no login session."""
    out = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        domain = c.get("domain") or ""
        name = c.get("name") or ""
        if "reddit.com" not in domain or not name:
            continue
        out.append({
            "domain": domain,
            "expirationDate": c.get("expirationDate"),
            "hostOnly": bool(c.get("hostOnly", not str(domain).startswith("."))),
            "httpOnly": bool(c.get("httpOnly", False)),
            "name": name,
            "path": c.get("path") or "/",
            "sameSite": c.get("sameSite") or "lax",
            "secure": bool(c.get("secure", False)),
            "session": bool(c.get("session", False)),
            "storeId": c.get("storeId"),
            "value": c.get("value") or "",
        })
    if not any(c["name"] == "reddit_session" and c["value"] for c in out):
        return 0
    dest.write_text(json.dumps(out, indent=4), encoding="utf-8")
    return len(out)


def _log_extension_install_help() -> None:
    log.warning(
        "The cookie extension did not respond. In Chrome open chrome://extensions, enable Developer mode, "
        "and Load unpacked this folder: %s",
        EXTENSION_DIR,
    )


def export_chrome_cookies_via_extension(dest: Path) -> bool:
    """Ask the installed StreakBot extension to send Reddit cookies to this process.

    Chrome only decrypts cookies for code running inside Chrome. The extension reads them
    and posts them to a localhost listener started for this call.
    """
    chrome = _chrome_exe()
    if chrome is None:
        log.warning("Chrome is not installed in the usual location, so its cookies could not be exported.")
        return False
    if not (EXTENSION_DIR / "manifest.json").is_file():
        log.warning("Cookie extension is missing at %s", EXTENSION_DIR)
        return False

    token = secrets.token_urlsafe(24)
    payload: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", f"chrome-extension://{EXTENSION_ID}")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Streakbot-Token")
            self.end_headers()

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != "/cookies":
                self.send_error(404)
                return
            if self.headers.get("X-Streakbot-Token") != token:
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if length <= 0 or length > 2_000_000:
                self.send_error(400)
                return
            payload["body"] = self.rfile.read(length)
            self.send_response(204)
            self._cors()
            self.end_headers()
            done.set()

        def log_message(self, fmt, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"chrome-extension://{EXTENSION_ID}/export.html?port={port}&token={token}"
    log.info("Asking the StreakBot Chrome extension to export Reddit cookies...")
    try:
        subprocess.Popen(
            [str(chrome), "--profile-directory=Default", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not done.wait(45):
            _log_extension_install_help()
            return False
        try:
            data = json.loads(payload["body"].decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
            log.warning("The cookie extension sent data that could not be read.")
            return False
        if not isinstance(data, list):
            log.warning("The cookie extension sent an unexpected response.")
            return False
        written = write_extension_cookies_json(data, dest)
        if not written:
            log.warning("Chrome replied, but Reddit is not logged in there. Log in to Reddit in Chrome, then run again.")
            return False
        log.info("Exported %d Reddit cookies from Chrome to %s", written, dest)
        return True
    finally:
        server.shutdown()
        server.server_close()


def try_export_chrome_cookies(dest: Path) -> bool:
    """Export Reddit cookies from the main Chrome profile via the local extension."""
    return export_chrome_cookies_via_extension(dest)


def prepare_cookie_source(config) -> Path | None:
    """Export Reddit cookies from the main Chrome profile first. Fall back to the configured cookies file."""
    dest = session_cookie_path(config)
    if try_export_chrome_cookies(dest):
        return dest
    cookies_file = config.get("cookies_file")
    if not cookies_file:
        log.warning("Chrome export failed and no cookies_file is set.")
        return None
    cookies_path = Path(cookies_file)
    if not cookies_path.is_absolute():
        cookies_path = Path(__file__).resolve().parent / cookies_path
    if not cookies_path.exists():
        log.warning("Cookies file not found: %s", cookies_path)
        return None
    log.info("Using saved cookies file %s", cookies_path)
    return cookies_path


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


def session_cookie_path(config) -> Path:
    """Where to write a fresh Cookie-Editor JSON export after login."""
    cookies_file = config.get("cookies_file")
    if cookies_file:
        path = Path(cookies_file)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent / path
        if path.suffix.lower() == ".json":
            return path
    return Path(__file__).resolve().parent / "cookies.json"


def save_reddit_cookies(context, dest: Path) -> int:
    """Save current Reddit cookies in Cookie-Editor JSON format. Returns count written."""
    raw = context.cookies()
    same_site_out = {"None": "no_restriction", "Strict": "strict", "Lax": "lax"}
    out = []
    for c in raw:
        domain = c.get("domain") or ""
        if "reddit.com" not in domain:
            continue
        expires = c.get("expires")
        session = expires is None or expires < 0
        out.append({
            "domain": domain,
            "expirationDate": None if session else float(expires),
            "hostOnly": not domain.startswith("."),
            "httpOnly": bool(c.get("httpOnly", False)),
            "name": c.get("name", ""),
            "path": c.get("path", "/"),
            "sameSite": same_site_out.get(c.get("sameSite") or "Lax", "lax"),
            "secure": bool(c.get("secure", False)),
            "session": session,
            "storeId": None,
            "value": c.get("value", ""),
        })
    if not out:
        return 0
    dest.write_text(json.dumps(out, indent=4), encoding="utf-8")
    return len(out)


def listing_upvote_buttons(page):
    """Upvote buttons for real posts on a listing page."""
    selectors = (
        'shreddit-post button:has([icon-name="upvote"])',
        'shreddit-post button[aria-label="upvote"]',
        'shreddit-post [role="button"][aria-label="upvote"]',
        'button:has([icon-name="upvote"])',
        'button[aria-label="upvote"], [aria-label="upvote"]',
    )
    for sel in selectors:
        found = page.locator(sel).all()
        if found:
            return found
    return []


def has_reddit_session(page) -> bool:
    """True when the browser has Reddit's login cookie."""
    try:
        for c in page.context.cookies("https://www.reddit.com"):
            if c.get("name") == "reddit_session" and c.get("value"):
                return True
    except Exception:
        return False
    return False


def cookie_file_candidates(config) -> list[Path]:
    """The configured cookies file only. Backups such as cookies.json.old are ignored."""
    base = Path(__file__).resolve().parent
    cookies_file = config.get("cookies_file")
    if not cookies_file:
        return []
    path = Path(cookies_file)
    if not path.is_absolute():
        path = base / path
    return [path]


def apply_saved_cookies(context, path: Path) -> int:
    """Load a cookies file into the browser context. Returns how many cookies were added."""
    if path.suffix.lower() == ".json":
        cookies = load_cookies_from_json(path)
    else:
        cookies = load_cookies_from_netscape_file(path)
    if cookies:
        context.add_cookies(cookies)
    return len(cookies)


def recover_login_from_cookies(page, context, config) -> bool:
    """When the browser has no login cookie, try Chrome and saved cookie files."""
    log.warning("Not logged in. Exporting cookies from the main Chrome profile...")
    dest = session_cookie_path(config)
    if try_export_chrome_cookies(dest):
        count = apply_saved_cookies(context, dest)
        log.info("Loaded %d cookies exported from Chrome", count)
        if has_reddit_session(page):
            return True
    found_any = False
    for path in cookie_file_candidates(config):
        if not path.exists():
            log.info("Cookie file not found: %s", path)
            continue
        found_any = True
        count = apply_saved_cookies(context, path)
        log.info("Loaded %d cookies from %s", count, path)
        if has_reddit_session(page):
            log.info("Reddit session cookie found in %s", path)
            return True
    if not found_any:
        log.warning("No cookie files found. Export cookies with Cookie-Editor or log in in the browser window.")
    else:
        log.warning("Saved cookies did not include a Reddit login session. Log in in the browser window.")
    return False


def page_has_captcha(page) -> bool:
    """True when a captcha challenge is blocking the page.

    Reddit embeds a reCAPTCHA anchor iframe on normal pages, including when you are logged in.
    Only the challenge popup counts.
    """
    try:
        frames = page.locator(
            'iframe[src*="recaptcha"][src*="bframe"], '
            'iframe[src*="hcaptcha.com"][src*="challenge"], '
            'iframe[title*="recaptcha challenge" i]'
        )
        for i in range(frames.count()):
            frame = frames.nth(i)
            if not frame.is_visible():
                continue
            box = frame.bounding_box()
            if box and box.get("width", 0) > 50 and box.get("height", 0) > 50:
                return True
    except Exception:
        return False
    return False


def page_is_logged_in(page) -> bool:
    """True only when Reddit's logged-in header is present. Upvote buttons also show while logged out."""
    try:
        if page.locator(
            '#expand-user-drawer-button, shreddit-user-drawer, button[aria-label*="user menu" i], '
            '[data-testid="user-drawer-button"]'
        ).count() > 0:
            return True
        if page.locator('a[href*="/login"], a[href*="/register"]').count() > 0:
            return False
    except Exception:
        return False
    return False


def wait_until_logged_in(page, listing_url: str | None = None, timeout_s: int = 600) -> bool:
    """Wait until the logged-in header is visible and no captcha is up. Does not click vote buttons."""
    log.warning("Not logged in. Upvote buttons are visible while logged out and do not count.")
    log.info("Log in in the browser window (complete any captcha). Waiting up to %d minutes...", timeout_s // 60)
    deadline = time.time() + timeout_s
    captcha_logged = False
    last_nav = 0.0
    while time.time() < deadline:
        if page_is_logged_in(page) and not page_has_captcha(page):
            log.info("Logged-in header detected.")
            return True
        if page_has_captcha(page):
            if not captcha_logged:
                log.warning("Captcha is on the page. Complete it in the browser; the script will not click through it.")
                captcha_logged = True
            time.sleep(2)
            continue
        captcha_logged = False
        url_now = (page.url or "").lower()
        on_login = "login" in url_now or "register" in url_now or "captcha" in url_now
        if listing_url and not on_login and "/r/" not in url_now and time.time() - last_nav > 15:
            try:
                page.goto(listing_url, wait_until="domcontentloaded")
                page.wait_for_load_state("load", timeout=10_000)
                time.sleep(2)
                last_nav = time.time()
            except Exception:
                pass
        time.sleep(2)
    return False


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
    cookies_path = prepare_cookie_source(config)

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
        elif config.get("use_chrome_cookies", True):
            log.error("No Reddit cookies from Chrome. Load the StreakBot extension and log in to Reddit in Chrome.")
            _log_extension_install_help()
            return False
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
            if not has_reddit_session(page):
                recover_login_from_cookies(page, context, config)

            if streak_check_url and not config.get("run_now") and not config.get("test_mode"):
                log.info("Checking streak status before upvote...")
                reached, days = check_streak_on_page(page, streak_check_url)
                if days is not None:
                    log.info("Streak: %d day(s)", days)
                if reached:
                    log.info("Streak already reached today, skipping upvote.")
                    return True
                log.info("Streak not reached today — proceeding with upvote.")
            elif streak_check_url:
                log.info("run_now/test_mode: skipping the streak-already-reached check.")

            log.info("Navigating to %s", url)
            page.bring_to_front()
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_load_state("load", timeout=10_000)
            # Reddit often never reaches networkidle (long-lived connections); wait for content
            log.info("Waiting 3s for vote buttons to render...")
            time.sleep(3)

            upvotes = listing_upvote_buttons(page)
            if not page_is_logged_in(page) or page_has_captcha(page):
                if not wait_until_logged_in(page, url):
                    log.error("Stopped: still logged out or captcha was not cleared. No upvote was made.")
                    context.close()
                    return False
                upvotes = listing_upvote_buttons(page)
            if len(upvotes) < 1:
                log.error("Logged in, but no upvote buttons found on the listing.")
                context.close()
                return False

            if page_is_logged_in(page):
                saved = save_reddit_cookies(context, session_cookie_path(config))
                if saved:
                    log.info("Saved %d Reddit cookies to %s", saved, session_cookie_path(config))

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

            # On the post page: click upvote only after the logged-in header is visible.
            if not page_is_logged_in(page) or page_has_captcha(page):
                if not wait_until_logged_in(page):
                    log.error("Stopped on the post page: not logged in, or a captcha is showing. No upvote was made.")
                    context.close()
                    return False

            upvote_btns = page.locator('[data-post-click-location="vote"] button[upvote]').all()
            if not upvote_btns:
                upvote_btns = page.locator('shreddit-post button:has([icon-name="upvote"]), shreddit-post button[upvote]').all()
            if not upvote_btns:
                upvote_btns = page.locator('button:has([icon-name="upvote"]), button[upvote]').all()
            if not upvote_btns:
                log.warning("No upvote button found on post page.")
                context.close()
                return False

            upvote_btns[0].scroll_into_view_if_needed()
            time.sleep(0.3)
            upvote_btns[0].click()
            time.sleep(1.5)
            if page_has_captcha(page) or not page_is_logged_in(page):
                log.error("Upvote did not register: captcha or login wall appeared. You were not upvoted.")
                context.close()
                return False
            pressed = page.locator('button[upvote][aria-pressed="true"], button[aria-pressed="true"][upvote]').count()
            if pressed < 1:
                log.error("Clicked upvote, but the button is not pressed. The vote was not applied.")
                context.close()
                return False
            log.info("Upvoted on post page (button is pressed).")

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

            saved = 0
            if page_is_logged_in(page):
                saved = save_reddit_cookies(context, session_cookie_path(config))
            if saved:
                log.info("Updated %d Reddit cookies in %s", saved, session_cookie_path(config))
            else:
                log.info("Did not overwrite the cookies file; the browser is not logged in.")

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

    cookies_path = prepare_cookie_source(config)

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
        elif config.get("use_chrome_cookies", True):
            log.error("No Reddit cookies from Chrome. Load the StreakBot extension and log in to Reddit in Chrome.")
            _log_extension_install_help()
            return False
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

    if not require_tos_acceptance():
        return

    config = load_config()

    if config.get("test_mode"):
        log.info("test_mode is true — running full upvote flow (even if streak already reached)")
        run_upvote_flow(config)
        return

    if config.get("run_now"):
        log.info("run_now is true — running once immediately")
        run_upvote_flow(config)
        return

    subreddits = get_subreddits(config)
    run_time_min = config.get("run_time_min")
    run_time_max = config.get("run_time_max")
    use_random_time = run_time_min and run_time_max

    if use_random_time:
        min_minutes = _time_to_minutes(run_time_min)
        max_minutes = _time_to_minutes(run_time_max)
        if min_minutes > max_minutes:
            min_minutes, max_minutes = max_minutes, min_minutes
        log.info(
            "Scheduler started: run daily at a random time between %s and %s, subreddits r/%s",
            run_time_min, run_time_max, ", r/".join(subreddits) if subreddits else "?",
        )
        scheduled_day = None
        target_minutes = None
        last_run_day = None
    else:
        run_time = config["run_time"]  # e.g. "09:00"
        hour, minute = map(int, str(run_time).strip().split(":", 1))
        log.info("Scheduler started: run daily at %s, subreddits r/%s", run_time, ", r/".join(subreddits) if subreddits else "?")
    log.info("Minimize this window; browser will open at the scheduled time")
    log.info("First run: log in to Reddit in the browser window when it opens")

    while True:
        now = time.localtime()
        today = (now.tm_year, now.tm_yday)
        now_minutes = now.tm_hour * 60 + now.tm_min

        if use_random_time:
            if target_minutes is None or scheduled_day != today:
                target_minutes = random.randint(min_minutes, max_minutes)
                scheduled_day = today
                log.info("Today's run scheduled at %s", _minutes_to_time(target_minutes))
            if now_minutes >= target_minutes and last_run_day != today:
                log.info("Scheduled time reached — starting upvote flow")
                run_upvote_flow(config)
                last_run_day = today
                log.debug("Sleeping 65s to avoid re-running")
                time.sleep(65)
        else:
            if now.tm_hour == hour and now.tm_min == minute:
                log.info("Scheduled time reached — starting upvote flow")
                run_upvote_flow(config)
                log.debug("Sleeping 65s to avoid re-running in same minute")
                time.sleep(65)
        time.sleep(30)


if __name__ == "__main__":
    main()
