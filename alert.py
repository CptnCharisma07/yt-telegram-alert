"""YouTube -> Telegram alerts for long videos and live streams (no Shorts).

Reads channels.csv, checks each channel's "videos" (UULF) and "live streams" (UULV)
RSS feeds, and sends one Telegram message per new item. State lives in state.json.

Env vars:
  TELEGRAM_BOT_TOKEN  - from @BotFather
  TELEGRAM_CHAT_ID    - your chat id (run: python alert.py --chat-id)

Usage:
  python alert.py              normal run (first run only records what exists, sends a "ready" message)
  python alert.py --dry-run    print what would be sent, change nothing
  python alert.py --test       send one test message
  python alert.py --chat-id    print chat ids of people who messaged the bot
"""
import csv, html, json, os, re, sys, time, urllib.parse, urllib.request
import concurrent.futures as cf
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
CHANNELS_FILE = os.path.join(HERE, "channels.csv")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "en-US,en;q=0.9",
      "Cookie": "CONSENT=YES+1"}
MAX_AGE = timedelta(days=2)       # ignore feed items older than this (stops floods after downtime)
UPCOMING_TTL = timedelta(days=10)  # stop tracking a scheduled stream after this


def get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def load_channels():
    with open(CHANNELS_FILE, encoding="utf-8") as f:
        return [(r["name"].strip(), r["channel_id"].strip()) for r in csv.DictReader(f) if r.get("channel_id")]


def feed(playlist_id):
    """Return list of (video_id, title, published_dt). Empty list if the playlist doesn't exist."""
    try:
        xml = get(f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}")
    except Exception:
        return None  # network error or 404 (channel has no videos of this type)
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        vid = re.search(r"<yt:videoId>([^<]+)", e)
        title = re.search(r"<title>([^<]*)", e)
        pub = re.search(r"<published>([^<]+)", e)
        if vid and pub:
            out.append((vid.group(1), html.unescape(title.group(1) if title else ""), datetime.fromisoformat(pub.group(1))))
    return out


def live_status(video_id):
    """Return ('live'|'upcoming'|'ended'|'unknown', scheduled_start_dt_or_None)."""
    try:
        page = get(f"https://www.youtube.com/watch?v={video_id}")
    except Exception:
        return "unknown", None
    start = None
    m = re.search(r'"scheduledStartTime":"(\d+)"', page)
    if m:
        start = datetime.fromtimestamp(int(m.group(1)), timezone.utc)
    if '"isLiveNow":true' in page:
        return "live", start
    if '"isUpcoming":true' in page or '"upcomingEventData"' in page:
        return "upcoming", start
    if '"isLiveNow":false' in page or '"isLiveContent":true' in page:
        return "ended", start
    return "unknown", start


IST = timezone(timedelta(hours=5, minutes=30))


def fmt_ist(dt):
    return dt.astimezone(IST).strftime("%d %b, %I:%M %p IST")


def tg(method, params, token):
    data = urllib.parse.urlencode(params).encode()
    for attempt in range(5):
        try:
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = json.loads(e.read() or b"{}")
            if e.code == 429:
                time.sleep(body.get("parameters", {}).get("retry_after", 5) + 1)
                continue
            if method == "sendPhoto" and e.code == 400:  # bad thumbnail -> fall back to text
                p = dict(params); cap = p.pop("caption"); p.pop("photo")
                p["text"] = cap
                return tg("sendMessage", p, token)
            raise RuntimeError(f"Telegram {method} failed: {e.code} {body}")
        except Exception:
            time.sleep(3)
    raise RuntimeError(f"Telegram {method} failed after retries")


def build_caption(kind, channel, title, vid, start=None):
    head = {"video": "🎥 <b>New video</b>", "live": "🔴 <b>LIVE NOW</b>",
            "upcoming": "⏰ <b>Live scheduled</b>", "stream": "🔴 <b>Live stream</b>"}[kind]
    when = f"\n🕒 {fmt_ist(start)}" if kind == "upcoming" and start else ""
    url = f"https://www.youtube.com/watch?v={vid}"
    return (f"{head} · {html.escape(channel, quote=False)}\n\n<a href=\"{url}\">{html.escape(title, quote=False)}</a>{when}")


def main():
    args = set(sys.argv[1:])
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if "--chat-id" in args:
        res = tg("getUpdates", {}, token)
        chats = {(u.get("message") or {}).get("chat", {}).get("id"): (u.get("message") or {}).get("chat", {}).get("first_name")
                 for u in res.get("result", [])}
        chats.pop(None, None)
        print(chats or "No messages yet. Open your bot in Telegram, press Start, send 'hi', then run this again.")
        return

    dry = "--dry-run" in args
    if not dry and not (token and chat):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")

    if "--test" in args:
        tg("sendMessage", {"chat_id": chat, "text": "✅ Test message from your YouTube alert bot.", "parse_mode": "HTML"}, token)
        print("Test message sent.")
        return

    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        state = {}
    now = datetime.now(timezone.utc)
    seen_at = state.get("seen", {})  # vid -> ISO time first seen
    first_run = not seen_at
    # forget ids older than 14 days; feed items that old are skipped by MAX_AGE anyway
    seen_at = {v: t for v, t in seen_at.items() if now - datetime.fromisoformat(t) < timedelta(days=14)}

    class Seen:
        def __contains__(self, v): return v in seen_at
        def add(self, v): seen_at.setdefault(v, now.isoformat())
    seen = Seen()
    upcoming = state.get("upcoming", {})  # vid -> {channel, title, first_seen}

    channels = load_channels()

    def fetch(ch):
        name, cid = ch
        return name, feed("UULF" + cid[2:]), feed("UULV" + cid[2:])

    with cf.ThreadPoolExecutor(8) as ex:
        results = list(ex.map(fetch, channels))

    outbox = []  # (sort_time, kind, channel, title, vid, start)
    failed = 0
    for name, vids, lives in results:
        if vids is None and lives is None:
            failed += 1
        live_ids = {v for v, _, _ in (lives or [])}
        for v, title, pub in (lives or []):
            if v in seen:
                continue
            seen.add(v)
            if first_run or now - pub > MAX_AGE:
                continue
            status, start = live_status(v)
            if status == "upcoming":
                upcoming[v] = {"channel": name, "title": title, "first_seen": now.isoformat()}
                outbox.append((pub, "upcoming", name, title, v, start))
            elif status == "live":
                outbox.append((pub, "live", name, title, v, start))
            elif status == "unknown":
                outbox.append((pub, "stream", name, title, v, start))
            # 'ended' = a past stream we missed; skip it
        for v, title, pub in (vids or []):
            if v in seen or v in live_ids:
                continue
            seen.add(v)
            if first_run or now - pub > MAX_AGE:
                continue
            outbox.append((pub, "video", name, title, v, None))

    # Scheduled streams that have now gone live
    for v, info in list(upcoming.items()):
        if any(o[4] == v for o in outbox):
            continue
        if now - datetime.fromisoformat(info["first_seen"]) > UPCOMING_TTL:
            upcoming.pop(v); continue
        status, start = live_status(v)
        if status == "live":
            outbox.append((now, "live", info["channel"], info["title"], v, start))
            upcoming.pop(v)
        elif status == "ended":
            upcoming.pop(v)

    outbox.sort(key=lambda o: o[0])
    print(f"Checked {len(channels)} channels ({failed} unreachable). New items: {len(outbox)}"
          + (" [first run: recording existing videos only]" if first_run else ""))

    for _, kind, name, title, v, start in outbox:
        cap = build_caption(kind, name, title, v, start)
        if dry:
            print("---\n" + re.sub(r"<[^>]+>", "", cap))
            continue
        tg("sendPhoto", {"chat_id": chat, "photo": f"https://i.ytimg.com/vi/{v}/hqdefault.jpg",
                         "caption": cap, "parse_mode": "HTML"}, token)
        time.sleep(1.1)

    if first_run and not dry:
        tg("sendMessage", {"chat_id": chat, "parse_mode": "HTML",
                           "text": f"✅ <b>Alerts are on.</b> Watching {len(channels)} channels for new videos and live streams (no Shorts)."}, token)

    if not dry:
        state = {"seen": seen_at, "upcoming": upcoming, "updated": now.isoformat()}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
