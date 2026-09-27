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
   ```

2. **Edit `config.yaml`:**
   - `subreddit` – subreddit name (e.g. `python`, `askreddit`)
   - `run_time` – time to run in 24h format (e.g. `09:00`, `14:30`)
   - `wait_seconds_min` / `wait_seconds_max` – random wait (in seconds) before removing the upvote
   - **Auth:** Set `use_chrome_cookies: true` (the default). The bot asks a local Chrome extension for your Reddit cookies, then uses its own window. Chrome can stay open. Or set `cookies_file` to a Cookie-Editor export.
   - `run_now: true` – run once immediately for testing (then set back to `false` for scheduled runs)

3. **First run – log in (only if not using system browser or cookies file):**
   - If you don’t set `use_chrome_cookies` or `cookies_file`, run the script and when the browser opens, go to Reddit and log in. Your session is stored in `browser_profile/`.

### Chrome cookie extension

Chrome encrypts cookies so a script cannot copy them out of the profile. Load this extension once and the bot can ask Chrome for Reddit cookies while Chrome is open:

1. Open `chrome://extensions`.
2. Turn on **Developer mode**.
3. Click **Load unpacked** and choose the `extension` folder in this project.
4. Log in to Reddit in that Chrome profile.

On each run the script opens the extension, the extension sends only `reddit.com` cookies to the script on this computer, and the script writes `cookies.json`.

[Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) still works as a manual export: save JSON as `cookies.json` or Netscape as `cookies.txt`, and set `cookies_file` in `config.yaml`.

## Usage

- **Scheduled (default):** Run `python reddit_streak.py` and leave it running. It will open the browser and perform the upvote at `run_time` every day.
- **Test once:** Set `run_now: true` in `config.yaml`, run `python reddit_streak.py`. It will run the upvote flow once and exit.

## Config example

```yaml
subreddit: "python"
run_time: "09:00"
wait_seconds_min: 30
wait_seconds_max: 90
# Ask the StreakBot Chrome extension for cookies; vote in our own window
use_chrome_cookies: true
# cookies_file: "cookies.txt"   # optional override
run_now: false
```

## Logging

- The script logs to stdout with timestamps. For more detail (e.g. load-state steps), set `LOG_LEVEL=DEBUG` when running: `set LOG_LEVEL=DEBUG` (Windows) or `LOG_LEVEL=DEBUG python reddit_streak.py` (Unix).

## Notes

- You must be logged in to Reddit (via system browser profile, cookies file, or the bot’s own profile) for upvoting to work.
- **`use_chrome_cookies`:** Asks the StreakBot extension in your open Chrome window for Reddit cookies, then votes in the bot’s own window.
- **`cookies_file`:** A Cookie-Editor export (`cookies.json` or `cookies.txt`). If you get logged out, log in in the bot’s browser window when prompted; a JSON cookies file is updated automatically.