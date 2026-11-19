# YouTube → Telegram alerts

Sends you a Telegram message, with the thumbnail and link, whenever one of the channels in `channels.csv` posts a **long video** or a **live stream**. Shorts are never included. It runs free on GitHub Actions every ~10 minutes.

Alert types:
- 🎥 **New video**: a regular upload
- ⏰ **Live scheduled**: a stream is announced, with its start time in IST
- 🔴 **LIVE NOW**: a stream has started (you also get this when a scheduled one goes live)

## Setup (about 10 minutes)

### 1. Create your Telegram bot
1. In Telegram, open **@BotFather** (it has a blue tick) and send `/newbot`.
2. Give it a name and a username ending in `bot`, for example `myupscalerts_bot`.
3. BotFather replies with a **token** that looks like `123456789:AA...`. Keep it private.
4. Open your new bot, press **Start**, and send it `hi`.

### 2. Find your chat ID
On this PC, in a terminal in this folder:
```
set TELEGRAM_BOT_TOKEN=paste-your-token-here
python alert.py --chat-id
```
It prints something like `{987654321: 'Aditya'}`. The number is your **chat ID**.

### 3. Put the project on GitHub
1. Create a new **public** repository on github.com, for example `yt-telegram-alerts`.
   - Make it public because Actions are unlimited and free on public repos. A private repo gets 2,000 free minutes a month, and a run every 10 minutes needs about 4,300.
   - Your token stays secret either way, because it's stored as a secret and never in the code.
2. Upload these files, keeping the folder structure, including `.github/workflows/alerts.yml`.

### 4. Add your two secrets
Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:
- `TELEGRAM_BOT_TOKEN`: the token from BotFather
- `TELEGRAM_CHAT_ID`: the number from step 2

### 5. Start it
Repo → **Actions** tab → enable workflows if asked → **YouTube alerts** → **Run workflow**.

The first run doesn't send old videos. It records what already exists and sends "✅ Alerts are on". After that, every new video or live arrives as a message.

## Everyday use
- **Add or remove a channel:** edit `channels.csv`, one line per channel in the form `name,channel_id`.
- **Too many messages?** In Telegram, mute the bot chat and check it when you like.
- **Pause alerts:** Actions tab → YouTube alerts → ⋯ → **Disable workflow**.

## Testing on your PC
```
python alert.py --dry-run   # shows what would be sent, changes nothing
python alert.py --test      # sends one test message (needs both env vars)
```

## Things to know
- GitHub can delay scheduled runs by 5–15 minutes when it's busy. Alerts are usually within 10–20 minutes of upload.
- Items older than 2 days are skipped, so a long outage won't flood you.
- `state.json` is the bot's memory of what it has already sent. It's updated automatically, so don't delete it unless you want a fresh start.
