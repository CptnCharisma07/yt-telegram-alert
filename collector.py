"""YouTube tracker: dashboard data + Telegram alerts, in one run.

Every run:
  1. finds new long videos and live streams (never Shorts) from each channel's
     "videos" (UULF) and "live streams" (UULV) playlists
  2. refreshes stats with the YouTube Data API (if YOUTUBE_API_KEY is set)
  3. records views gained per channel per day, views at 24h, and short-term growth
  4. sends Telegram alerts for new uploads, scheduled lives and lives that started
  5. writes everything to DATA_DIR (the "data" branch on GitHub)

Files in DATA_DIR:
  meta.json      last update time, API quota used          (tiny, polled by the page)
  channels.json  channels with brand, exams, subscribers    (small)
  videos.json    lean list of uploads from the last 60 days
  daily.json     views gained per channel per IST day (video, live)
  details.json   description and tags, loaded only when a video is opened
  history.json   view snapshots for the last 7 days, loaded only for the chart
  alerts.json    which uploads were already sent to Telegram

Env vars: YOUTUBE_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATA_DIR

Usage:
  python collector.py             normal run
  python collector.py --dry-run   print alerts instead of sending them (data is still saved)
  python collector.py --test      send one Telegram test message
  python collector.py --chat-id   show chat ids of people who messaged the bot
"""
import csv, html, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
import concurrent.futures as cf
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", os.path.join(HERE, "data-store"))
KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

RETAIN = timedelta(days=60)        # uploads kept on the dashboard
HIST_KEEP = timedelta(days=7)      # view snapshots kept for charts
DAILY_KEEP = 90                    # days of views-gained totals
ALERT_MAX_AGE = timedelta(days=2)  # never alert on anything older (prevents floods)
UPCOMING_TTL = timedelta(days=10)  # stop waiting for a scheduled stream after this
IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime.now(timezone.utc)
NOW_ISO = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "en-US,en;q=0.9", "Cookie": "CONSENT=YES+1"}
quota = 0


class NotFound(Exception):
    pass


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def get(url, timeout=25):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def api(path, **params):
    """One YouTube Data API call (1 quota unit). Raises NotFound on 404, RuntimeError on refusal."""
    global quota
    params["key"] = KEY
    url = f"https://www.googleapis.com/youtube/v3/{path}?{urllib.parse.urlencode(params)}"
    last = ""
    for _ in range(3):
        quota += 1
        try:
            return json.loads(get(url))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            if e.code == 404:
                raise NotFound(body)
            if e.code in (400, 403):
                raise RuntimeError(f"YouTube API {path} refused ({e.code}): {body}")
            last = f"HTTP {e.code}"
        except Exception as e:
            last = str(e)
        time.sleep(2)
    raise RuntimeError(f"YouTube API {path} failed: {last}")


def load(name, default):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(name, obj):
    os.makedirs(DATA, exist_ok=True)
    tmp = os.path.join(DATA, name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, os.path.join(DATA, name))


def load_channels():
    with open(os.path.join(HERE, "channels.csv"), encoding="utf-8") as f:
        return [{"id": r["channel_id"].strip(), "name": r["name"].strip(), "brand": (r.get("brand") or "Other").strip(),
                 "exams": [e.strip() for e in (r.get("exams") or "").split(";") if e.strip()]}
                for r in csv.DictReader(f) if r.get("channel_id")]


def rss(playlist):
    try:
        xml = get(f"https://www.youtube.com/feeds/videos.xml?playlist_id={playlist}")
    except Exception:
        return []
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        g = lambda p: (re.search(p, e) or [None, None])[1]
        vid, pub = g(r"<yt:videoId>([^<]+)"), g(r"<published>([^<]+)")
        if vid and pub:
            out.append({"id": vid, "t": html.unescape(g(r"<title>([^<]*)") or ""), "p": iso(parse(pub)),
                        "v": int(g(r'<media:statistics views="(\d+)"') or 0), "l": int(g(r'<media:starRating count="(\d+)"') or 0)})
    return out


def scrape_live(video_id):
    """Fallback when there is no API key: read the watch page. Returns (state or None, scheduled start)."""
    try:
        page = get(f"https://www.youtube.com/watch?v={video_id}")
    except Exception:
        return None, None
    m = re.search(r'"scheduledStartTime":"(\d+)"', page)
    start = datetime.fromtimestamp(int(m.group(1)), timezone.utc) if m else None
    if '"isLiveNow":true' in page:
        return "live", start
    if '"isUpcoming":true' in page or '"upcomingEventData"' in page:
        return "upcoming", start
    if '"isLiveNow":false' in page:
        return "ended", start
    return None, start


def duration(s):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def air_time(v):
    return v.get("as") or v.get("ss") or v["p"]


def age(v):
    return NOW - parse(air_time(v))


def ist_day(dt):
    return dt.astimezone(IST).strftime("%Y-%m-%d")


# ---------------- Telegram ----------------
def tg(method, params):
    data = urllib.parse.urlencode(params).encode()
    for _ in range(5):
        try:
            req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = json.loads(e.read() or b"{}")
            if e.code == 429:
                time.sleep(body.get("parameters", {}).get("retry_after", 5) + 1)
                continue
            if method == "sendPhoto" and e.code == 400:  # thumbnail not ready -> send as text
                p = dict(params); p["text"] = p.pop("caption"); p.pop("photo")
                return tg("sendMessage", p)
            raise RuntimeError(f"Telegram {method} failed: {e.code} {body}")
        except Exception:
            time.sleep(3)
    raise RuntimeError(f"Telegram {method} failed after retries")


def tag(s):
    return "#" + re.sub(r"[^\w]", "", s.replace("&", "and"))


HOME = os.environ.get("HOME_BRAND", "Drishti")
LAUNCH = re.compile(r"batch|launch|admission|foundation|course|enrol|discount|offer|scholarship|demo class|orientation", re.I)


def build_summary(videos, by_cid, day_gains, today):
    """Evening Telegram digest: home brand's rank, leaders, top uploads today, launches."""
    brand = {}
    for cid, (gv, gl) in day_gains.items():
        if cid in by_cid:
            b = by_cid[cid]["brand"]; brand[b] = brand.get(b, 0) + gv + gl
    total = sum(brand.values()) or 1
    ranked = sorted(brand.items(), key=lambda x: -x[1])
    rank = next((i + 1 for i, (b, _) in enumerate(ranked) if b == HOME), None)
    fmt = lambda x: f"{x / 1e5:.1f} L" if x >= 1e5 else f"{x / 1e3:.1f}K" if x >= 1e3 else str(x)
    lines = [f"🌙 <b>Daily summary · {datetime.strptime(today, '%Y-%m-%d').strftime('%d %b')}</b>", ""]
    if rank:
        lines.append(f"<b>{html.escape(HOME)}</b>: #{rank} of {len(ranked)} brands · {brand[HOME] / total:.1%} of views gained ({fmt(brand[HOME])})")
    lines.append("Leaders: " + " · ".join(f"{html.escape(b)} {v / total:.0%}" for b, v in ranked[:3]))
    todays = [v for v in videos.values() if v.get("gd") == today and v.get("gt")]
    top = sorted(todays, key=lambda v: -v["gt"])[:5]
    if top:
        lines += ["", "<b>Most views gained today</b>"]
        for v in top:
            ch = by_cid.get(v["c"], {})
            lines.append(f"• <a href=\"https://youtu.be/{v['id']}\">{html.escape(v['t'][:70], quote=False)}</a> · {html.escape(ch.get('brand', ''))} · +{fmt(v['gt'])}")
    new_today = [v for v in videos.values() if ist_day(parse(air_time(v))) == today and v.get("ls") != "upcoming"]
    launches = [v for v in new_today if LAUNCH.search(v["t"])]
    lines += ["", f"Uploads today: {len(new_today)} ({sum(1 for v in new_today if v['k'] == 'live')} lives)"]
    if launches:
        lines.append("<b>Batch / course launches</b>")
        for v in launches[:5]:
            lines.append(f"• {html.escape(by_cid.get(v['c'], {}).get('brand', ''))}: <a href=\"https://youtu.be/{v['id']}\">{html.escape(v['t'][:70], quote=False)}</a>")
    return "\n".join(lines)


def caption(kind, v, ch):
    prem = v["k"] == "video"
    head = {"video": "🎥 <b>New video</b>", "live": "🎬 <b>PREMIERE NOW</b>" if prem else "🔴 <b>LIVE NOW</b>",
            "upcoming": "⏰ <b>Premiere scheduled</b>" if prem else "⏰ <b>Live scheduled</b>", "stream": "🔴 <b>Live stream</b>",
            "breakout": "🚀 <b>Breakout</b>"}[kind]
    url = f"https://www.youtube.com/watch?v={v['id']}"
    extra = []
    if kind == "breakout":
        hrs = max(1, int(age(v).total_seconds() // 3600))
        extra.append(f"👁 {v.get('v', 0):,} views in {hrs}h · about {v.get('_ratio', 0):.1f}× this channel's usual")
    if kind == "upcoming" and v.get("ss"):
        extra.append("🕒 " + parse(v["ss"]).astimezone(IST).strftime("%d %b, %I:%M %p IST"))
    if kind == "live" and v.get("cv"):
        extra.append(f"👁 {v['cv']:,} watching")
    if kind == "video" and v.get("d"):
        m, s = divmod(v["d"], 60); h, m = divmod(m, 60)
        extra.append("⏱ " + (f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"))
    tags = " ".join([tag(ch["brand"])] + [tag(e) for e in ch["exams"]])
    return (f"{head} · {html.escape(ch['name'], quote=False)}\n\n<a href=\"{url}\">{html.escape(v['t'], quote=False)}</a>"
            + ("\n" + " · ".join(extra) if extra else "") + f"\n\n{tags}")


# ---------------- main ----------------
def main():
    args = set(sys.argv[1:])
    if "--chat-id" in args:
        res = tg("getUpdates", {})
        chats = {(u.get("message") or {}).get("chat", {}).get("id"): (u.get("message") or {}).get("chat", {}).get("first_name") for u in res.get("result", [])}
        chats.pop(None, None)
        print(chats or "No messages yet. Open your bot in Telegram, press Start, send 'hi', then run this again.")
        return
    if "--test" in args:
        tg("sendMessage", {"chat_id": CHAT, "text": "✅ Test message from your YouTube tracker."})
        print("Test message sent.")
        return
    dry = "--dry-run" in args

    channels = load_channels()
    by_cid = {c["id"]: c for c in channels}
    old_channels = {c["id"]: c for c in load("channels.json", {"channels": []})["channels"]}
    videos = {v["id"]: v for v in load("videos.json", {"videos": []})["videos"]}
    details = load("details.json", {})
    history = load("history.json", {})
    daily = load("daily.json", {"days": {}})
    alerts = load("alerts.json", None)
    first_alert_run = alerts is None
    alerts = alerts or {"sent": {}, "pending": {}}
    meta = load("meta.json", {})
    prev_views = {k: v["v"] for k, v in videos.items() if "v" in v}
    refreshed = set()

    # 1) discovery from RSS (free) -- the type comes only from the playlist, so premieres stay "video"
    def fetch(c):
        return c["id"], rss("UULF" + c["id"][2:]), rss("UULV" + c["id"][2:])

    with cf.ThreadPoolExecutor(8) as ex:
        feeds = list(ex.map(fetch, channels))
    in_rss = set()
    for cid, vids, lives in feeds:
        for kind, items in (("video", vids), ("live", lives)):
            for it in items:
                v = videos.setdefault(it["id"], {"id": it["id"], "c": cid, "k": kind, "t": it["t"], "p": it["p"]})
                in_rss.add(it["id"])
                if not KEY:
                    v.update(t=it["t"], v=it["v"], l=it["l"], u=NOW_ISO)
                    refreshed.add(it["id"])

    if KEY:
        # 2) daily backfill so the last 60 days are complete (items found here are never alerted)
        last = parse(meta.get("backfill"))
        if not last or NOW - last > timedelta(hours=20):
            for c in channels:
                for kind, prefix in (("video", "UULF"), ("live", "UULV")):
                    token, pages = None, 0
                    while pages < 4:
                        params = {"part": "contentDetails,snippet", "playlistId": prefix + c["id"][2:], "maxResults": 50}
                        if token:
                            params["pageToken"] = token
                        try:
                            res = api("playlistItems", **params)
                        except NotFound:
                            break  # channel has no uploads of this type
                        except RuntimeError as e:
                            print("backfill:", e); break
                        pages += 1
                        stop = False
                        for it in res.get("items", []):
                            vid = it["contentDetails"]["videoId"]
                            pub = it["contentDetails"].get("videoPublishedAt") or it["snippet"]["publishedAt"]
                            if NOW - parse(pub) > RETAIN:
                                stop = True; continue
                            videos.setdefault(vid, {"id": vid, "c": c["id"], "k": kind, "t": it["snippet"]["title"], "p": pub})
                        token = res.get("nextPageToken")
                        if stop or not token:
                            break
            meta["backfill"] = NOW_ISO

        # 3) refresh stats: <3 days every run, 3-14 days every 6h, older once a day; live/upcoming every run
        due = []
        for v in videos.values():
            a = age(v)
            stale = NOW - (parse(v.get("u")) or datetime(2000, 1, 1, tzinfo=timezone.utc))
            every = timedelta(0) if a < timedelta(days=3) else timedelta(hours=6) if a < timedelta(days=14) else timedelta(hours=24)
            if v.get("ls") in ("live", "upcoming") or "v" not in v or stale >= every - timedelta(minutes=5):
                due.append(v["id"])
        for i in range(0, len(due), 50):
            batch = due[i:i + 50]
            try:
                res = api("videos", part="snippet,statistics,contentDetails,liveStreamingDetails", id=",".join(batch), maxResults=50)
            except RuntimeError as e:
                print("stats:", e); break
            got = set()
            for it in res.get("items", []):
                v = videos[it["id"]]; got.add(it["id"]); refreshed.add(it["id"])
                sn, st, cd, lsd = it["snippet"], it.get("statistics", {}), it.get("contentDetails", {}), it.get("liveStreamingDetails")
                v.update(t=sn["title"], p=sn["publishedAt"], v=int(st.get("viewCount", 0)), l=int(st.get("likeCount", 0)),
                         cm=int(st.get("commentCount", 0)), d=duration(cd.get("duration")), u=NOW_ISO)
                details[it["id"]] = {"tg": (sn.get("tags") or [])[:12], "ds": (sn.get("description") or "")[:400]}
                if lsd:  # live stream or premiere: timing only, the type stays as discovered
                    v["ss"], v["as"], v["ae"] = lsd.get("scheduledStartTime"), lsd.get("actualStartTime"), lsd.get("actualEndTime")
                    v["ls"] = "ended" if v["ae"] else "live" if v["as"] else "upcoming"
                    if "concurrentViewers" in lsd:
                        v["cv"] = int(lsd["concurrentViewers"]); v["pv"] = max(v.get("pv", 0), v["cv"])
                    elif v["ls"] != "live":
                        v.pop("cv", None)
            for vid in batch:
                if vid not in got:  # deleted or made private
                    videos.pop(vid, None); details.pop(vid, None)

        # 4) channel stats
        ids = [c["id"] for c in channels]
        for i in range(0, len(ids), 50):
            try:
                res = api("channels", part="snippet,statistics", id=",".join(ids[i:i + 50]), maxResults=50)
            except RuntimeError as e:
                print("channels:", e); break
            for it in res.get("items", []):
                c = by_cid[it["id"]]; st = it["statistics"]
                c.update(subs=int(st.get("subscriberCount", 0)), views=int(st.get("viewCount", 0)), count=int(st.get("videoCount", 0)),
                         thumb=it["snippet"]["thumbnails"].get("default", {}).get("url"), handle=it["snippet"].get("customUrl"))

    today = ist_day(NOW)
    yesterday = ist_day(NOW - timedelta(days=1))
    for c in channels:
        o = old_channels.get(c["id"], {})
        for k in ("subs", "views", "count", "thumb", "handle"):
            c.setdefault(k, o.get(k))
        hist = [h for h in o.get("hist", []) if h[0] != today]
        if c.get("subs") is not None:
            hist.append([today, c["subs"], c.get("views"), c.get("count")])
        c["hist"] = hist[-120:]

    # 5) views gained per channel per IST day, views at 24h, history and 6h growth
    day = daily["days"].setdefault(today, {})
    now_min = int(NOW.timestamp() // 60)
    new_hist = {}
    for vid, v in list(videos.items()):
        if age(v) > RETAIN or v["c"] not in by_cid:
            videos.pop(vid); details.pop(vid, None); continue
        if "v" not in v:
            continue
        if vid in refreshed:
            old = prev_views.get(vid)
            if old is not None:
                delta = max(0, v["v"] - old)
            else:
                delta = v["v"] if ist_day(parse(air_time(v))) == today else 0  # brand-new upload counts fully; old ones set a baseline
            if delta:
                b = day.setdefault(v["c"], [0, 0]); b[0 if v["k"] == "video" else 1] += delta
            # per-upload views gained: gt = today (IST), gy = yesterday, gd = the day gt belongs to
            if v.get("gd") != today:
                v["gy"] = v.get("gt", 0) if v.get("gd") == yesterday else 0
                v["gt"], v["gd"] = 0, today
            v["gt"] += delta
            if "v24" not in v and timedelta(hours=24) <= age(v) <= timedelta(hours=30) and v.get("ls") != "upcoming":
                v["v24"] = v["v"]
        a = age(v)
        if a > HIST_KEEP or v.get("ls") == "upcoming":
            v.pop("g6", None); continue
        h = history.get(vid, [])
        gap = 55 if a < timedelta(hours=48) else 355
        if h and now_min - h[-1][0] < gap:
            h[-1] = [now_min, v["v"]] if v.get("ls") == "live" else h[-1]
        else:
            h.append([now_min, v["v"]])
        new_hist[vid] = h[-80:]
        past = [s for s in h if s[0] <= now_min - 360]
        if past:
            s = past[-1]; v["g6"] = round((v["v"] - s[1]) / max(1, (now_min - s[0]) / 60))
        else:
            v["g6"] = round(v["v"] / max(1, a.total_seconds() / 3600))
    for k in sorted(daily["days"])[:-DAILY_KEEP]:
        daily["days"].pop(k)
    for vid in [k for k in details if k not in videos]:
        details.pop(vid)

    # 6) Telegram alerts
    can_send = bool(TOKEN and CHAT) or dry
    sent, pending = alerts["sent"], alerts["pending"]
    outbox = []
    for vid in in_rss:
        v = videos.get(vid)
        if not v or vid in sent:
            continue
        sent[vid] = NOW_ISO
        # a channel just added to channels.csv: record its existing uploads silently instead of flooding Telegram
        newly_added = bool(old_channels) and v["c"] not in old_channels
        if first_alert_run or newly_added or (NOW - parse(v["p"]) > ALERT_MAX_AGE and age(v) > ALERT_MAX_AGE):
            continue
        if not KEY and v["k"] == "live":
            st, ss = scrape_live(vid)
            if st: v["ls"] = st
            if ss: v["ss"] = iso(ss)
        ls = v.get("ls")
        if ls == "upcoming":
            pending[vid] = NOW_ISO; outbox.append(("upcoming", v))
        elif ls == "live":
            outbox.append(("live", v))
        elif v["k"] == "live" and ls == "ended":
            continue  # a stream that already finished before we saw it
        else:
            outbox.append(("stream" if v["k"] == "live" else "video", v))
    queued = {v["id"] for _, v in outbox}
    for vid, first in list(pending.items()):
        v = videos.get(vid)
        if not v or NOW - parse(first) > UPCOMING_TTL:
            pending.pop(vid); continue
        if vid in queued:
            continue
        if not KEY:
            st, _ = scrape_live(vid)
            if st: v["ls"] = st
        if v.get("ls") == "live":
            outbox.append(("live", v)); pending.pop(vid)
        elif v.get("ls") == "ended":
            pending.pop(vid)
    for vid in [k for k, t in sent.items() if NOW - parse(t) > timedelta(days=14)]:
        sent.pop(vid)

    # breakouts: uploads 3-36h old running well ahead of their channel's usual views at 24h
    usual = {}
    for v in videos.values():
        if v.get("v24") is not None:
            usual.setdefault((v["c"], v["k"]), []).append(v["v24"])
    usual = {k: sorted(l)[len(l) // 2] for k, l in usual.items() if len(l) >= 5}
    seed_breakouts = "breakouts" not in alerts  # first run with this feature: record, don't alert
    broke = alerts.setdefault("breakouts", {})
    found = []
    for v in videos.values():
        a = age(v)
        if v["id"] in broke or v.get("ls") in ("upcoming", "live") or not timedelta(hours=3) <= a <= timedelta(hours=36):
            continue
        m = usual.get((v["c"], v["k"]))
        if not m or m < 300:
            continue
        expected = max(100, m * min(1.0, (a.total_seconds() / 86400) ** 0.5))  # views are front-loaded, so scale by sqrt of age
        ratio = v.get("v", 0) / expected
        if ratio >= 5 and v.get("v", 0) >= 2000:
            found.append(dict(v, _ratio=ratio))
    for v in sorted(found, key=lambda x: -x["_ratio"])[:len(found) if seed_breakouts else 3]:
        broke[v["id"]] = NOW_ISO
        if not first_alert_run and not seed_breakouts:
            outbox.append(("breakout", v))
    for vid in [k for k, t in broke.items() if NOW - parse(t) > timedelta(days=14)]:
        broke.pop(vid)

    # evening summary, once a day after 9 PM IST
    summary = None
    if NOW.astimezone(IST).hour >= 21 and alerts.get("summary_day") != today and not first_alert_run:
        alerts["summary_day"] = today
        summary = build_summary(videos, by_cid, daily["days"].get(today, {}), today)

    outbox.sort(key=lambda o: air_time(o[1]))
    print(f"Checked {len(channels)} channels. Uploads tracked: {len(videos)}. Alerts: {len(outbox)}"
          + (" [first run: recording existing uploads, no alerts]" if first_alert_run else ""))
    if can_send:
        try:
            for kind, v in outbox:
                cap = caption(kind, v, by_cid[v["c"]])
                if dry:
                    print("---\n" + re.sub(r"<[^>]+>", "", cap)); continue
                tg("sendPhoto", {"chat_id": CHAT, "photo": f"https://i.ytimg.com/vi/{v['id']}/hqdefault.jpg", "caption": cap, "parse_mode": "HTML"})
                time.sleep(1.1)
            if summary:
                if dry:
                    print("--- SUMMARY\n" + re.sub(r"<[^>]+>", "", summary))
                else:
                    tg("sendMessage", {"chat_id": CHAT, "parse_mode": "HTML", "text": summary, "disable_web_page_preview": "true"})
            if first_alert_run and not dry:
                tg("sendMessage", {"chat_id": CHAT, "parse_mode": "HTML",
                                   "text": f"✅ <b>Tracker is on.</b> Watching {len(channels)} channels for new videos and live streams (no Shorts)."})
        except RuntimeError as e:
            print("telegram:", e)

    # 7) save
    lean = ("id", "c", "k", "t", "p", "ss", "as", "ae", "ls", "d", "v", "l", "cm", "cv", "pv", "u", "v24", "g6", "gt", "gy", "gd")
    out = [{k: v[k] for k in lean if v.get(k) is not None} for v in sorted(videos.values(), key=air_time, reverse=True)]
    qday = NOW.astimezone(timezone(timedelta(hours=-8))).strftime("%Y-%m-%d")  # API quota resets at Pacific midnight
    if meta.get("quota_day") != qday:
        meta["quota_day"], meta["quota_used"] = qday, 0
    meta["quota_used"] = meta.get("quota_used", 0) + quota
    meta.update(updated=NOW_ISO, api=bool(KEY), videos=len(out), gains_since=meta.get("gains_since") or today,
                vgains_since=meta.get("vgains_since") or today)  # first (partial) day of per-upload gains
    save("channels.json", {"channels": channels})
    save("videos.json", {"videos": out})
    save("daily.json", daily)
    save("details.json", details)
    save("history.json", new_hist)
    save("alerts.json", alerts)
    save("meta.json", meta)
    print(f"API units this run: {quota} (today {meta['quota_used']}).")


if __name__ == "__main__":
    main()
