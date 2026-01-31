# Reddit Streak Bot

Keeps your Reddit streak active by upvoting one of the top 3 posts of the day on a subreddit you choose, then removing the upvote after a random delay. Runs at a time you set in the config.

> **Disclaimer:** This project is not affiliated with Reddit, Inc. Use of this software may violate [Reddit’s Terms of Service](https://www.redditinc.com/policies/user-agreement) and [API Terms](https://www.redditinc.com/policies/data-api-terms). Automating interactions (e.g. voting) or scraping the site can result in account restrictions or bans. Use at your own risk.

## Setup

1. **Create a virtual environment and install dependencies:**

   ```bash
   python -m venv .venv
   .venv\Scripts\activate   # Windows
   pip install -r requirements.txt
   playwright install chromium
   # browser-cookie3 (in requirements) reads cookies from Chrome for auth
   ```

2. **Edit `config.yaml`:**
   - `subreddit` – subreddit name (e.g. `python`, `askreddit`)
   - `run_time` – time to run in 24h format (e.g. `09:00`, `14:30`)
   - `wait_seconds_min` / `wait_seconds_max` – random wait (in seconds) before removing the upvote
   - **Auth (avoids “verify you’re not a bot”):**
     - **Default – Chrome cookies:** Set `use_chrome_cookies: "chrome"` or `"edge"` to use that browser’s real profile and cookies. **Close Chrome or Edge completely before the bot runs** (the profile is locked while the browser is open).
     - **Option B – cookies file:** Export Reddit cookies from your main browser (e.g. with the “Get cookies.txt” extension) to a file like `cookies.txt`, then set `cookies_file: "cookies.txt"`. No need to close your browser; re-export if you get logged out.
   - `run_now: true` – run once immediately for testing (then set back to `false` for scheduled runs)

3. **First run – log in (only if not using system browser or cookies file):**
   - If you don’t set `use_chrome_cookies` or `cookies_file`, run the script and when the browser opens, go to Reddit and log in. Your session is stored in `browser_profile/`.

### Extracting cookies with Cookie-Editor

To use a cookies file instead of Chrome profile cookies:

1. Install [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) from the Chrome Web Store.
2. Log in to Reddit in Chrome, then open the Cookie-Editor extension on the Reddit tab.
3. Export cookies as **JSON** (save as `cookies.json`) or **Netscape** (save as `cookies.txt`).
4. In `config.yaml` set `cookies_file: "cookies.json"` or `cookies_file: "cookies.txt"` and ensure `use_chrome_cookies` is `false` or omit it if you want the file to be used.

You can re-export whenever your session expires; you do not need to close Chrome.

## Usage

- **Scheduled (default):** Run `python reddit_streak.py` and leave it running. It will open the browser and perform the upvote at `run_time` every day.
- **Test once:** Set `run_now: true` in `config.yaml`, run `python reddit_streak.py`. It will run the upvote flow once and exit.

## Config example

```yaml
subreddit: "python"
run_time: "09:00"
wait_seconds_min: 30
wait_seconds_max: 90
# Grab cookies from Chrome, use our own window (Chrome can stay open)
use_chrome_cookies: true
# cookies_file: "cookies.txt"   # optional override
run_now: false
```

## Logging

- The script logs to stdout with timestamps. For more detail (e.g. load-state steps), set `LOG_LEVEL=DEBUG` when running: `set LOG_LEVEL=DEBUG` (Windows) or `LOG_LEVEL=DEBUG python reddit_streak.py` (Unix).

## Notes

- You must be logged in to Reddit (via system browser profile, cookies file, or the bot’s own profile) for upvoting to work.
- **`use_chrome_cookies`:** Uses your real Chrome/Edge profile so Reddit sees your normal cookies and is less likely to ask “verify you’re not a bot.” Close that browser completely before the scheduled run so the profile isn’t locked.
- **`cookies_file`:** Use [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) to export as JSON (`cookies.json`) or Netscape (`cookies.txt`). Or use a Netscape-format `cookies.txt` (e.g. from “Get cookies.txt” or “EditThisCookie”). Re-export if your session expires.