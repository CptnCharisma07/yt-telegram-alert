# Civil Services YouTube Tracker

One GitHub workflow, running every ~10 minutes, does two jobs:
- **Telegram alerts** for new long videos and live streams from the channels in `channels.csv`. Shorts are never included.
- **A dashboard** (`docs/index.html`, served by GitHub Pages) for comparing brands and exams.

Files in the repo:
| File | What it is |
|---|---|
| `collector.py` | Fetches new uploads and stats, sends the alerts, saves the data |
| `channels.csv` | The channels to track: name, channel ID, brand, exams |
| `docs/index.html` | The dashboard page |
| `.github/workflows/alerts.yml` | The schedule |

All saved data, including the bot's memory of which alerts it has sent, lives on a separate **`data`** branch that the workflow replaces on every run. The `main` branch only changes when you edit it.

Alert types: 🎥 New video · ⏰ Live (or premiere) scheduled, with time in IST · 🔴 LIVE NOW, with viewer count · 🎬 Premiere now. Each alert ends with brand and exam hashtags, such as `#Drishti #UPPCS`, so you can tap a tag in Telegram to filter.

## Setup

### 1. Telegram bot
In Telegram, open **@BotFather**, send `/newbot`, and copy the token. Open your bot and press **Start**. To get your chat ID, open **@userinfobot** and press **Start**; it shows your **Id**, which is not your phone number.

### 2. YouTube Data API key (free)
1. Go to console.cloud.google.com, create a project, then open **APIs & Services → Library → YouTube Data API v3 → Enable**.
2. Open **APIs & Services → Credentials → Create credentials → API key** and copy the key.
3. Optional: click the key and restrict it to **YouTube Data API v3**.

It uses about 3,000 of the 10,000 free daily units.

### 3. Secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**. Add three:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `YOUTUBE_API_KEY`

### 4. Dashboard
Repo → **Settings → Pages**: choose Source **Deploy from a branch**, Branch **main**, Folder **/docs**, then save.
The dashboard is at `https://<username>.github.io/<repo>/`.

### 5. Start
**Actions → YouTube tracker → Run workflow.** The first run sends only "✅ Tracker is on". It also loads the last 60 days of uploads, which can take a few minutes.

## What the dashboard measures
- **Views gained**: new views on every tracked upload during the selected IST days. This is the fairest "who did best today or yesterday" number. It starts counting from the first run.
- **Avg @24h**: average views each upload had 24 hours after it went up, which compares uploads at the same age.
- **Views on uploads**: current total views on uploads posted in the period. Older uploads have had longer to collect views.
- **Subscribers**: YouTube rounds these (12.9M stays 12.9M until 13.0M), so growth for big channels moves in steps.

Refresh timing: uploads under 3 days old and anything live or scheduled refresh on every run; uploads 3–14 days old every 6 hours; older ones once a day. Uploads are kept for 60 days. The page checks for new data every 2 minutes and redraws only when something changed.

## Everyday use
- **Add or remove a channel:** edit `channels.csv`. The `brand` column groups channels together, which is how Mrunal Patel and Sudarshan Gurjar sit under Unacademy and Dr Shivin under Sarrthi. The `exams` column takes one or more exams separated by `;`.
- **Home brand:** the brand highlighted on the dashboard is set by `const HOME = "Drishti"` in `docs/index.html`.
- **Pause everything:** Actions → YouTube tracker → ⋯ → **Disable workflow**.

## Testing on a PC
```
python collector.py --dry-run   # prints the alerts it would send; saves data to ./data-store
python collector.py --test      # sends a Telegram test message (needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
python collector.py --chat-id   # lists chat IDs of people who messaged the bot
```
