#!/usr/bin/env python3
"""
Universal Media Downloader Bot
Telethon (MTProto, 2 GB uploads) + yt-dlp (1000+ sites) + MongoDB (motor)

How big files are handled
  1. Download the best quality that fits your MAX_SOURCE_MB limit.
  2. Remux to MP4 only (lossless, no quality change).
  3. If the file is under Telegram's 2 GB limit -> send as-is (never re-encoded).
  4. If it is over the limit -> visually-lossless x264 re-encode (CRF 20, audio copied).
  5. If it is STILL over the limit -> split into parts with stream copy (lossless).

Fallbacks: Instagram -> instaloader, YouTube -> savetube API (from the PHP site).
"""

import asyncio
import html
import importlib.util
import io
import json
import logging
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yt_dlp
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from telethon import Button, TelegramClient, errors, events, functions, types
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
)

# ================================ CONFIG ================================
MB = 1024 * 1024


def _int(name, default):
    return int(os.getenv(name, default))


API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
DB_NAME = os.getenv("DB_NAME", "media_bot")
SESSION_NAME = os.getenv("SESSION_NAME", "media_bot")

ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
FORCE_SUB = [
    c.strip().lstrip("@")
    for c in os.getenv("FORCE_SUB", "DGDRIFT,driftarmyff").split(",")
    if c.strip()
]  # public channel usernames; the bot must be ADMIN in each

UPLOAD_LIMIT = _int("UPLOAD_LIMIT_MB", 1950) * MB   # Telegram max is ~2000 MB
MAX_SOURCE = _int("MAX_SOURCE_MB", 4096) * MB       # refuse downloads bigger than this
MAX_PARALLEL = _int("MAX_PARALLEL", 2)
COOLDOWN = _int("COOLDOWN_SECONDS", 5)
CRF = _int("COMPRESS_CRF", 20)                      # 18-20 = visually lossless
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
COOKIES_FILE = os.getenv("COOKIES_FILE")            # optional Netscape cookies.txt
COOKIES_PATH = Path(COOKIES_FILE or "cookies_runtime.txt")  # admin can upload cookies from the bot
IG_USERNAME = os.getenv("IG_USERNAME")
IG_SESSION_FILE = os.getenv("IG_SESSION_FILE")
PORT = os.getenv("PORT")                            # set by Render/Railway for health checks
PROXY_URL = os.getenv("PROXY_URL")                  # optional, e.g. http://user:pass@host:port (helps when YouTube blocks the server IP)
DENO_DIR = Path(__file__).resolve().parent / ".deno" / "bin"   # local JS runtime for yt-dlp (auto-installed)
if DENO_DIR.exists():
    os.environ["PATH"] = f"{DENO_DIR}{os.pathsep}{os.environ.get('PATH', '')}"

SAVETUBE_API = "https://apis.davidcyriltech.my.id/download/savetube"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
YT_RE = re.compile(r"(?:youtube\.com|youtu\.be)", re.I)
IG_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:reels?|p|tv)/([\w-]+)", re.I)
LADDER = (2160, 1440, 1080, 720, 480, 360)
VIDEO_EXTS = {"mp4", "mkv", "webm", "mov", "avi", "flv", "m4v", "ts", "3gp"}
AUDIO_OK = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus"}

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("mediabot")

client = None  # TelegramClient, created in main()
db = None      # Database, created in main()
BOT_USERNAME = ""

PENDING = {}                 # token -> request data (expires after 1h)
RUNNING = {}                 # token -> asyncio.Task
ACTIVE = set()               # user ids with a running job
JOB_SEM = None               # asyncio.Semaphore, created in main()
_sub_cache = {}              # user_id -> expiry timestamp
_last_request = {}           # user_id -> timestamp
ADMIN_STATE = {}             # admin id -> {"action": ...} while waiting for their next message
CFG = {                      # editable from the bot (/admin), persisted in MongoDB
    "force_sub": list(FORCE_SUB),
    "force_sub_enabled": True,
    "cooldown": COOLDOWN,
    "welcome": None,         # None = default welcome text
}

esc = html.escape


class Cancelled(Exception):
    pass


class TooLarge(Exception):
    pass


# ================================ HELPERS ================================
def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_time(sec):
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def bar(pct, width=12):
    filled = int(width * max(0.0, min(100.0, pct)) / 100)
    return "█" * filled + "░" * (width - filled)


def utcnow():
    return datetime.now(timezone.utc)


def cooldown_left(user_id):
    now = time.time()
    left = CFG["cooldown"] - (now - _last_request.get(user_id, 0))
    if left > 0:
        return math.ceil(left)
    _last_request[user_id] = now
    return 0


def new_token(user_id, url, data):
    now = time.time()
    for k in [k for k, v in PENDING.items() if now - v["ts"] > 3600]:
        PENDING.pop(k, None)
    token = uuid.uuid4().hex[:10]
    PENDING[token] = {"user_id": user_id, "url": url, "ts": now, **data}
    return token


def friendly_error(e):
    if isinstance(e, TooLarge):
        return (
            "❌ <b>File too large</b>\n\n"
            f"It exceeds this server's limit of {human_size(MAX_SOURCE)}. Try a lower quality."
        )
    m = str(e).lower()
    rules = [
        (("valid media", "invalid file", "invalid video", "web page instead"),
         "⚠️ <b>Invalid file received</b>\n\nThe source returned a broken file. Please try again or pick another quality."),
        (("ffmpeg", "ffprobe"), "⚙️ <b>Server setup issue</b>\n\nffmpeg is missing on the server. Please tell the admin."),
        (("javascript runtime", "js runtime", "n challenge", "requested format is not available",
          "only images are available", "po token"),
         "⚙️ <b>Temporary problem</b>\n\nThis site isn't working on our server right now. Please try again later."),
        (("unsupported url",), "🌐 <b>Unsupported link</b>\n\nThis website isn't supported."),
        (("drm",), "🔒 <b>DRM-protected</b>\n\nSpotify, Netflix and similar services can't be downloaded."),
        (("429", "too many requests", "rate limit", "rate-limit"),
         "🚦 <b>Rate limited</b>\n\nThe site is throttling us. Please try again in a few minutes."),
        (("not a bot", "sign in", "log in", "login", "cookies", "private", "confirm your age", "age-restricted"),
         "🔒 <b>Private or login required</b>\n\nOnly public content can be downloaded."),
        (("not available", "unavailable", "removed", "deleted", "404", "does not exist"),
         "❌ <b>Not available</b>\n\nThe content was removed or the link is broken."),
        (("geo", "your country"), "🌍 <b>Region blocked</b>\n\nThis content isn't available from the server's region."),
    ]
    for keys, text in rules:
        if any(k in m for k in keys):
            return text
    return "❌ <b>Download failed</b>\n\nSomething went wrong. Check the link and try again."


def error_message(e, user_id):
    """User-friendly text; admins also get the raw error so problems are easy to diagnose."""
    text = friendly_error(e)
    if user_id in ADMIN_IDS:
        text += f"\n\n🛠 <i>Admin debug:</i>\n<code>{esc(str(e)[:350])}</code>"
    return text


async def safe_edit(msg, text, **kw):
    try:
        return await msg.edit(text, parse_mode="html", **kw)
    except errors.MessageNotModifiedError:
        pass
    except errors.FloodWaitError as e:
        log.warning("Flood wait %ss on edit", e.seconds)
    except Exception as e:
        log.debug("edit failed: %s", e)


# ================================ DATABASE ================================
class Database:
    def __init__(self, uri, name):
        self.client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000)
        self.users = self.client[name]["users"]
        self.logs = self.client[name]["downloads"]
        self.settings = self.client[name]["settings"]

    async def init(self):
        await self.client.admin.command("ping")
        await self.users.create_index("user_id", unique=True)
        await self.logs.create_index([("user_id", 1), ("ts", -1)])
        await self.logs.create_index("ts")
        log.info("MongoDB connected")

    async def touch_user(self, u):
        """Create/update the user and return their document."""
        now = utcnow()
        return await self.users.find_one_and_update(
            {"user_id": u.id},
            {
                "$set": {
                    "username": u.username,
                    "first_name": u.first_name,
                    "access_hash": getattr(u, "access_hash", None),
                    "last_seen": now,
                },
                "$setOnInsert": {"joined_at": now, "downloads": 0, "bytes": 0, "banned": False},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    async def log_download(self, user_id, site, title, kind, size):
        await self.logs.insert_one(
            {"user_id": user_id, "site": site, "title": title[:200], "kind": kind, "size": size, "ts": utcnow()}
        )
        await self.users.update_one({"user_id": user_id}, {"$inc": {"downloads": 1, "bytes": size}})

    async def set_banned(self, user_id, flag):
        res = await self.users.update_one({"user_id": user_id}, {"$set": {"banned": flag}})
        return res.matched_count > 0

    async def load_config(self):
        return await self.settings.find_one({"_id": "config"}) or {}

    async def save_config(self, **kw):
        await self.settings.update_one({"_id": "config"}, {"$set": kw}, upsert=True)

    async def get_cookies(self):
        doc = await self.settings.find_one({"_id": "cookies"})
        return doc.get("text") if doc else None

    async def set_cookies(self, text):
        if text is None:
            await self.settings.delete_one({"_id": "cookies"})
        else:
            await self.settings.update_one({"_id": "cookies"}, {"$set": {"text": text}}, upsert=True)

    async def stats(self):
        day = utcnow() - timedelta(days=1)
        total = await self.users.count_documents({})
        new = await self.users.count_documents({"joined_at": {"$gte": day}})
        active = await self.users.count_documents({"last_seen": {"$gte": day}})
        sums = await self.users.aggregate(
            [{"$group": {"_id": None, "d": {"$sum": "$downloads"}, "b": {"$sum": "$bytes"}}}]
        ).to_list(1)
        sites = await self.logs.aggregate(
            [{"$group": {"_id": "$site", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}, {"$limit": 5}]
        ).to_list(5)
        s = sums[0] if sums else {"d": 0, "b": 0}
        return total, new, active, s["d"], s["b"], sites


# ================================ PROGRESS ================================
STAGE_LABELS = {
    "queued": "⏳ Waiting in queue…",
    "start": "🔎 Preparing…",
    "download": "📥 Downloading",
    "processing": "⚙️ Processing",
    "compress": "🗜 Optimizing (visually lossless)",
    "split": "✂️ Splitting into parts",
    "upload": "📤 Uploading",
}
BAR_STAGES = {"download", "compress", "upload", "split"}


class Progress:
    def __init__(self):
        self.cancel = False
        self.set_stage("start")

    def set_stage(self, stage):
        self.stage = stage
        self.pct = 0.0
        self.done = 0
        self.total = 0
        self.speed = 0.0
        self.eta = None
        self.t0 = time.time()

    def ytdlp_hook(self, d):
        if self.cancel:
            raise Cancelled()
        if d["status"] == "downloading":
            if self.stage != "download":
                self.set_stage("download")
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            self.done, self.total = done, total
            self.pct = done / total * 100 if total else 0.0
            self.speed = d.get("speed") or 0.0
            self.eta = d.get("eta")
        elif d["status"] == "finished":
            self.set_stage("processing")

    def pp_hook(self, d):
        if self.stage != "processing":
            self.set_stage("processing")

    def upload_cb(self, sent, total):
        self.done, self.total = sent, total
        self.pct = sent / total * 100 if total else 0.0
        dt = time.time() - self.t0
        if dt > 0 and sent:
            self.speed = sent / dt
            self.eta = (total - sent) / self.speed if total else None


MOONS = "🌑🌒🌓🌔🌕🌖🌗🌘"
_bg = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)
    return t


async def stop_task(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def animate(msg, base, interval=1.5, **kw):
    """Animated 'working' message: moon phases + moving dots."""
    frame = 0
    try:
        while True:
            await asyncio.sleep(interval)
            frame += 1
            await safe_edit(msg, f"{MOONS[frame % len(MOONS)]} <b>{base}{'.' * (frame % 4)}</b>", **kw)
    except asyncio.CancelledError:
        pass


async def react(chat_id, msg_id, emoji):
    if not msg_id:
        return
    try:
        await client(functions.messages.SendReactionRequest(
            peer=chat_id, msg_id=msg_id, reaction=[types.ReactionEmoji(emoticon=emoji)]))
    except Exception as e:
        log.debug("reaction failed: %s", e)


def render_progress(prog, title, frame=0):
    label = STAGE_LABELS.get(prog.stage, "⏳ Working…")
    lines = [f"<b>{esc(title[:90])}</b>", "", f"{label}{'.' * (frame % 4)} {MOONS[frame % len(MOONS)]}"]
    if prog.stage in BAR_STAGES:
        lines.append(f"<code>{bar(prog.pct)}</code> {prog.pct:.0f}%")
        if prog.total and prog.stage in ("download", "upload"):
            line = f"{human_size(prog.done)} / {human_size(prog.total)}"
            if prog.speed:
                line += f" · {human_size(prog.speed)}/s"
            if prog.eta:
                line += f" · ETA {human_time(prog.eta)}"
            lines.append(line)
    return "\n".join(lines)


async def ticker(msg, prog, title, buttons):
    frame = 0
    try:
        while True:
            await asyncio.sleep(2)
            frame += 1
            await safe_edit(msg, render_progress(prog, title, frame), buttons=buttons)
    except asyncio.CancelledError:
        pass


# ================================ YT-DLP LAYER ================================
def ydl_base(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "geo_bypass": True,
    }
    if COOKIES_PATH.exists():
        opts["cookiefile"] = str(COOKIES_PATH)
    if PROXY_URL:
        opts["proxy"] = PROXY_URL
    if extra:
        opts.update(extra)
    return opts


def is_video_fmt(f):
    vc = f.get("vcodec")
    if vc == "none":
        return False
    if vc:
        return True
    return f.get("ext") in VIDEO_EXTS or bool(f.get("height"))


def extract_info(url):
    with yt_dlp.YoutubeDL(ydl_base({"skip_download": True})) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist":
        info = next((e for e in (info.get("entries") or []) if e), None)
        if not info:
            raise RuntimeError("Nothing to download in this playlist")
    return info


def estimate_size(info):
    for key in ("filesize", "filesize_approx"):
        if info.get(key):
            return info[key]
    fm = info.get("formats") or []
    v = max((f.get("filesize") or f.get("filesize_approx") or 0 for f in fm if is_video_fmt(f)), default=0)
    a = max((f.get("filesize") or f.get("filesize_approx") or 0 for f in fm if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")), default=0)
    return (v + a) or None


def summarize(info, url):
    fmts = info.get("formats") or []
    heights = sorted({f["height"] for f in fmts if f.get("height") and is_video_fmt(f)}, reverse=True)
    has_video = any(is_video_fmt(f) for f in fmts) if fmts else info.get("vcodec") not in (None, "none")
    return {
        "title": info.get("title") or "Untitled",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration") or 0,
        "thumb": info.get("thumbnail"),
        "site": info.get("extractor_key") or "Web",
        "heights": heights,
        "has_video": has_video,
        "est_size": estimate_size(info),
    }


def search_youtube(query, n=5):
    with yt_dlp.YoutubeDL(ydl_base({"extract_flat": True, "skip_download": True})) as ydl:
        res = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    out = []
    for e in res.get("entries") or []:
        if e and e.get("id"):
            out.append(
                {
                    "url": f"https://www.youtube.com/watch?v={e['id']}",
                    "title": e.get("title") or "Untitled",
                    "duration": e.get("duration") or 0,
                }
            )
    return out


def oembed_summary(url):
    """Light YouTube info that still works when yt-dlp is blocked on the server."""
    r = requests.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"},
                     timeout=15, headers={"User-Agent": UA})
    r.raise_for_status()
    d = r.json()
    return {
        "title": d.get("title") or "YouTube video",
        "uploader": d.get("author_name") or "",
        "duration": 0,
        "thumb": d.get("thumbnail_url"),
        "site": "Youtube",
        "heights": [1080, 720, 480, 360],
        "has_video": True,
        "est_size": None,
        "fallback": True,
    }


def video_format(h):
    cap = f"[height<={h}]" if h else ""
    return (
        f"bv*{cap}[vcodec^=avc1]+ba[ext=m4a]"
        f"/bv*{cap}+ba[ext=m4a]"
        f"/bv*{cap}+ba"
        f"/b{cap}/b"
    )


def pick_output(workdir):
    skip = {".part", ".ytdl", ".json", ".jpg", ".jpeg", ".png", ".webp", ".vtt", ".srt"}
    files = [p for p in workdir.glob("src.*") if p.is_file() and p.suffix.lower() not in skip]
    if not files:
        return None
    best = max(files, key=lambda p: p.stat().st_size)
    final = workdir / f"media{best.suffix.lower()}"
    best.rename(final)
    return final


def ytdlp_download(url, workdir, kind, choice, prog):
    opts = ydl_base(
        {
            "outtmpl": str(workdir / "src.%(ext)s"),
            "max_filesize": MAX_SOURCE,
            "progress_hooks": [prog.ytdlp_hook],
            "postprocessor_hooks": [prog.pp_hook],
        }
    )
    if kind == "v":
        opts["format"] = video_format(choice)
        opts["merge_output_format"] = "mp4"
    elif choice == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
            {"key": "FFmpegMetadata"},
        ]
    else:
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegMetadata"}]

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    path = pick_output(workdir)
    if not path:
        raise TooLarge()
    return path


# ---- fallbacks -------------------------------------------------------------
def valid_media(path, kind):
    """True if ffprobe sees a real audio/video stream (guards against HTML/empty files from backup APIs)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=60,
        )
        data = json.loads(out.stdout or b"{}")
    except FileNotFoundError:
        return True  # cannot verify without ffprobe
    except Exception:
        return False
    kinds = {st.get("codec_type") for st in data.get("streams", [])}
    duration = float((data.get("format") or {}).get("duration") or 0)
    if kind == "v":
        return "video" in kinds and duration > 0.5
    return "audio" in kinds and duration > 0.5


def http_download(url, dest, prog):
    prog.set_stage("download")
    with requests.get(url, stream=True, timeout=30, headers={"User-Agent": UA}) as r:
        r.raise_for_status()
        ctype = (r.headers.get("Content-Type") or "").lower()
        if ctype.startswith("text/") or "json" in ctype:
            raise RuntimeError(f"link returned a web page instead of a media file ({ctype})")
        total = int(r.headers.get("Content-Length") or 0)
        if total > MAX_SOURCE:
            raise TooLarge()
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if prog.cancel:
                    raise Cancelled()
                f.write(chunk)
                done += len(chunk)
                if done > MAX_SOURCE:
                    raise TooLarge()
                prog.done, prog.total = done, total
                prog.pct = done / total * 100 if total else 0.0
                dt = time.time() - prog.t0
                prog.speed = done / dt if dt > 0 else 0.0
                prog.eta = (total - done) / prog.speed if total and prog.speed else None
    return dest


_insta = None


def instagram_fallback(url, workdir, prog):
    global _insta
    import instaloader

    m = IG_RE.search(url)
    if not m:
        raise RuntimeError("not an instagram post link")
    if _insta is None:
        _insta = instaloader.Instaloader(
            quiet=True, download_pictures=False, download_videos=False,
            download_video_thumbnails=False, download_geotags=False,
            download_comments=False, save_metadata=False, compress_json=False,
        )
        if IG_USERNAME and IG_SESSION_FILE and os.path.exists(IG_SESSION_FILE):
            _insta.load_session_from_file(IG_USERNAME, IG_SESSION_FILE)
    post = instaloader.Post.from_shortcode(_insta.context, m.group(1))
    if not post.is_video:
        raise RuntimeError("post has no video")
    dest = http_download(post.video_url, workdir / "media.mp4", prog)
    if not valid_media(dest, "v"):
        raise RuntimeError("instagram returned an invalid video file")
    return dest


def savetube_fallback(url, kind, choice, workdir, prog):
    if kind == "a":
        formats = ["mp3"]
    else:
        wanted = [str(h) for h in (1080, 720, 480, 360) if not choice or h <= choice]
        formats = wanted or ["720", "480", "360"]
    for fmt in formats:
        try:
            r = requests.get(SAVETUBE_API, params={"url": url, "format": fmt}, timeout=60, headers={"User-Agent": UA})
            data = r.json()
            link = (data.get("data") or {}).get("download_url")
            if data.get("success") and link:
                ext = "mp3" if fmt == "mp3" else "mp4"
                dest = http_download(link, workdir / f"media.{ext}", prog)
                if valid_media(dest, kind):
                    return dest
                log.warning("savetube %s returned an invalid file (%d bytes)", fmt, dest.stat().st_size)
                dest.unlink(missing_ok=True)
        except (Cancelled, TooLarge):
            raise
        except Exception as e:
            log.warning("savetube %s failed: %s", fmt, e)
    raise RuntimeError("fallback API failed")


def run_download(url, workdir, kind, choice, prog):
    """Runs in a worker thread. yt-dlp first, then per-site fallbacks."""
    try:
        return ytdlp_download(url, workdir, kind, choice, prog)
    except (Cancelled, TooLarge):
        raise
    except Exception as first:
        log.warning("yt-dlp failed (%s) - trying fallback", first)
        try:
            if IG_RE.search(url) and kind == "v":
                return instagram_fallback(url, workdir, prog)
            if YT_RE.search(url):
                return savetube_fallback(url, kind, choice, workdir, prog)
        except (Cancelled, TooLarge):
            raise
        except Exception as e:
            log.warning("fallback failed: %s", e)
        raise first


# ================================ FFMPEG LAYER ================================
_PROGRESS_KEY = re.compile(r"^(frame|fps|stream_\d+\S*|bitrate|total_size|out_time\w*|dup_frames|drop_frames|speed|progress)=")


async def run_ffmpeg(args, duration, prog, stage=None):
    if stage:
        prog.set_stage(stage)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    tail = []
    try:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "ignore").strip()
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    secs = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                if duration:
                    prog.pct = min(99.0, secs / duration * 100)
            elif line and not _PROGRESS_KEY.match(line):
                tail.append(line)
        rc = await proc.wait()
    except asyncio.CancelledError:
        proc.kill()
        raise
    if rc != 0:
        raise RuntimeError("ffmpeg failed: " + " | ".join(tail[-3:]))


async def probe(path):
    meta = {"duration": 0.0, "w": 0, "h": 0, "vcodec": None, "acodec": None}
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        data = json.loads(out or b"{}")
    except ValueError:
        return meta
    meta["duration"] = float((data.get("format") or {}).get("duration") or 0)
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not meta["vcodec"] and not (s.get("disposition") or {}).get("attached_pic"):
            meta["vcodec"], meta["w"], meta["h"] = s.get("codec_name"), s.get("width") or 0, s.get("height") or 0
        elif s.get("codec_type") == "audio" and not meta["acodec"]:
            meta["acodec"] = s.get("codec_name")
    return meta


async def make_thumb(path, duration):
    out = path.with_name(path.stem + ".thumb.jpg")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-ss", "1" if duration > 2 else "0", "-i", str(path),
        "-frames:v", "1", "-vf", "scale='min(320,iw)':-2", "-q:v", "5", str(out),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return out if out.exists() and out.stat().st_size > 0 else None


async def normalize(path, kind, duration, prog):
    """Lossless container fixes only (remux / MP3 conversion for odd audio formats)."""
    ext = path.suffix.lower()
    if kind == "a":
        if ext in AUDIO_OK:
            return path
        out = path.with_suffix(".mp3")
        await run_ffmpeg(["-i", str(path), "-vn", "-c:a", "libmp3lame", "-b:a", "320k", str(out)], duration, prog)
        path.unlink()
        return out
    if ext == ".mp4":
        return path
    out = path.with_suffix(".mp4")
    try:
        await run_ffmpeg(
            ["-i", str(path), "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-movflags", "+faststart", str(out)],
            duration, prog,
        )
        path.unlink()
        return out
    except RuntimeError as e:
        log.info("remux to mp4 failed, sending original container: %s", e)
        out.unlink(missing_ok=True)
        return path


async def split_file(path, kind, duration, prog):
    if not duration:
        raise RuntimeError("cannot split: unknown duration")
    size = path.stat().st_size
    seg = max(1, int(duration / math.ceil(size / (UPLOAD_LIMIT * 0.9))))
    maps = ["-map", "0:v?", "-map", "0:a?"] if kind == "v" else ["-map", "0:a"]
    for _ in range(4):
        for old in path.parent.glob(f"{path.stem}.part*{path.suffix}"):
            old.unlink()
        pattern = path.with_name(f"{path.stem}.part%03d{path.suffix}")
        await run_ffmpeg(
            ["-i", str(path), *maps, "-c", "copy", "-f", "segment", "-segment_time", str(seg),
             "-reset_timestamps", "1", str(pattern)],
            duration, prog, "split",
        )
        parts = sorted(path.parent.glob(f"{path.stem}.part*{path.suffix}"))
        if parts and all(p.stat().st_size <= UPLOAD_LIMIT for p in parts):
            path.unlink()
            return parts
        seg = max(1, int(seg * 0.7))
    raise RuntimeError("could not split the file into small enough parts")


async def fit_for_telegram(path, kind, meta, prog):
    size = path.stat().st_size
    if size <= UPLOAD_LIMIT:
        return [path]
    dur = meta["duration"]
    if kind == "v" and dur:
        out = path.with_name(path.stem + "_opt.mp4")
        audio = ["-c:a", "copy"] if meta["acodec"] in ("aac", "mp3") else ["-c:a", "aac", "-b:a", "256k"]
        try:
            await run_ffmpeg(
                ["-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "medium",
                 "-crf", str(CRF), "-pix_fmt", "yuv420p", *audio, "-movflags", "+faststart", str(out)],
                dur, prog, "compress",
            )
            if out.stat().st_size < size:
                path.unlink()
                path, size = out, out.stat().st_size
            else:
                out.unlink()
        except RuntimeError as e:
            log.warning("compress failed: %s", e)
            out.unlink(missing_ok=True)
        if size <= UPLOAD_LIMIT:
            return [path]
    return await split_file(path, kind, dur, prog)


# ================================ UPLOAD ================================
async def send_part(chat_id, reply_to, path, kind, title, uploader, site, label, prog):
    meta = await probe(path)
    name = (yt_dlp.utils.sanitize_filename(title)[:100] or "media") + (f" {label.strip()}" if label else "")
    attrs = []
    thumb = None
    if kind == "v":
        if meta["w"] and meta["h"]:
            attrs.append(
                DocumentAttributeVideo(duration=int(meta["duration"]), w=meta["w"], h=meta["h"], supports_streaming=True)
            )
            thumb = await make_thumb(path, meta["duration"])
    else:
        attrs.append(
            DocumentAttributeAudio(duration=int(meta["duration"]), title=title[:64], performer=(uploader or "")[:64])
        )
    attrs.append(DocumentAttributeFilename(name + path.suffix))
    caption = f"{'🎬' if kind == 'v' else '🎵'} <b>{esc(title[:200])}</b>{esc(label)}\n"
    if uploader:
        caption += f"👤 {esc(uploader[:60])}\n"
    caption += f"💾 {human_size(path.stat().st_size)} · 🌐 {esc(site)}\n\n🤖 via @{BOT_USERNAME}"
    prog.set_stage("upload")
    await client.send_file(
        chat_id,
        str(path),
        caption=caption,
        parse_mode="html",
        attributes=attrs,
        thumb=str(thumb) if thumb else None,
        supports_streaming=(kind == "v"),
        force_document=(kind == "v" and path.suffix.lower() != ".mp4"),
        reply_to=reply_to,
        part_size_kb=512,
        progress_callback=prog.upload_cb,
    )


# ================================ JOB RUNNER ================================
async def do_job(msg, token, req, kind, choice, chat_id, user_id):
    title = req["title"]
    prog = Progress()
    req["prog"] = prog
    workdir = DOWNLOAD_DIR / f"{user_id}_{token}"
    workdir.mkdir(parents=True, exist_ok=True)
    cancel_btn = [[Button.inline("❌ Cancel", f"cancel:{token}".encode())]]
    tick = asyncio.create_task(ticker(msg, prog, title, cancel_btn))
    try:
        if JOB_SEM.locked():
            prog.set_stage("queued")
            await safe_edit(msg, render_progress(prog, title), buttons=cancel_btn)
        async with JOB_SEM:
            prog.set_stage("start")
            arg = int(choice) if kind == "v" else choice
            path = await asyncio.to_thread(run_download, req["url"], workdir, kind, arg, prog)
            prog.set_stage("processing")
            meta = await probe(path)
            bad = (kind == "v" and not meta["vcodec"]) or (kind == "a" and not meta["acodec"]) \
                or (meta["duration"] <= 0 and path.stat().st_size < 2 * MB)
            if bad:
                raise RuntimeError("The downloaded file is empty or not a valid media file")
            path = await normalize(path, kind, meta["duration"], prog)
            meta = await probe(path)
            files = await fit_for_telegram(path, kind, meta, prog)
            total_size = 0
            for i, f in enumerate(files, 1):
                label = f" (part {i}/{len(files)})" if len(files) > 1 else ""
                total_size += f.stat().st_size
                await send_part(chat_id, None, f, kind, title, req["uploader"], req["site"], label, prog)
        await stop_task(tick)
        spawn(react(chat_id, req.get("src_msg"), "🔥"))
        await db.log_download(user_id, req["site"], title, kind, total_size)
        try:
            await msg.delete()
        except Exception:
            pass
    except (asyncio.CancelledError, Cancelled):
        await stop_task(tick)
        prog.cancel = True
        await safe_edit(msg, "🛑 <b>Cancelled</b>")
    except Exception as e:
        await stop_task(tick)
        spawn(react(chat_id, req.get("src_msg"), "😢"))
        log.warning("job failed for %s: %s: %s", req["url"], type(e).__name__, e)
        await safe_edit(msg, error_message(e, user_id))
    finally:
        tick.cancel()
        ACTIVE.discard(user_id)
        shutil.rmtree(workdir, ignore_errors=True)


# ================================ UI / HANDLERS ================================
DEFAULT_WELCOME = (
    "🎬 <b>Universal Media Downloader</b>\n\n"
    "Send me a <b>link</b> (or just a <b>song / video name</b>) and pick a format.\n\n"
    "✨ <b>What I can do</b>\n"
    "• YouTube, Instagram, TikTok, Facebook, X, Pinterest, Reddit, SoundCloud &amp; many more\n"
    "• Video in the quality you choose, or audio as MP3 320k / original\n"
    "• Files up to 2 GB — larger ones are optimized or split automatically\n"
    "• Free &amp; unlimited"
)


def welcome_text(name):
    return (CFG["welcome"] or DEFAULT_WELCOME).replace("{name}", esc(name or "there"))


async def check_subscription(event):
    user_id = event.sender_id
    if not CFG["force_sub_enabled"] or user_id in ADMIN_IDS or _sub_cache.get(user_id, 0) > time.time():
        return True
    try:
        inp = await event.get_input_sender()
        for ch in CFG["force_sub"]:
            try:
                res = await client(functions.channels.GetParticipantRequest(channel="@" + ch, participant=inp))
            except errors.UserNotParticipantError:
                return False
            if isinstance(res.participant, (types.ChannelParticipantLeft, types.ChannelParticipantBanned)):
                return False
    except Exception as e:
        log.warning("Subscription check failed (is the bot an admin of the channels?): %s", e)
        return False
    _sub_cache[user_id] = time.time() + 300
    return True


async def send_join_prompt(event):
    rows = [[Button.url(f"📢 Join @{c}", f"https://t.me/{c}")] for c in CFG["force_sub"]]
    rows.append([Button.inline("✅ I have joined", b"check_subs")])
    await event.respond(
        "⚠️ <b>Access Restricted</b>\n\nJoin the channels below to use this bot, then tap <b>I have joined</b>.",
        buttons=rows, parse_mode="html", link_preview=False,
    )


async def guard(event):
    user = await event.get_sender()
    doc = await db.touch_user(user)
    if doc and doc.get("banned"):
        return False
    if not await check_subscription(event):
        await send_join_prompt(event)
        return False
    return True


def options_keyboard(token, s):
    rows = []
    if s["has_video"]:
        if s["heights"]:
            top = s["heights"][0]
            heights = [top] + [h for h in LADDER if h < top]
            btns = [
                Button.inline(f"🏆 {h}p (Best)" if i == 0 else f"🎬 {h}p", f"dl:{token}:v:{h}".encode())
                for i, h in enumerate(heights)
            ]
        else:
            btns = [Button.inline("🎬 Video", f"dl:{token}:v:0".encode())]
        rows += [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([
        Button.inline("🎵 MP3 320k", f"dl:{token}:a:mp3".encode()),
        Button.inline("🎼 Original audio", f"dl:{token}:a:orig".encode()),
    ])
    return rows


def card_text(s):
    lines = [f"🎬 <b>{esc(s['title'][:150])}</b>"]
    if s["uploader"]:
        lines.append(f"👤 {esc(s['uploader'][:60])}")
    bits = [f"🌐 {esc(s['site'])}"]
    if s["duration"]:
        bits.insert(0, f"⏱ {human_time(s['duration'])}")
    if s["est_size"]:
        bits.append(f"💾 ≈ {human_size(s['est_size'])}")
    lines.append(" · ".join(bits))
    if s.get("fallback"):
        lines.append("ℹ️ <i>Direct fetch is blocked, so the backup downloader will be used.</i>")
    lines.append("\n<b>Choose a format 👇</b>")
    return "\n".join(lines)


async def show_options(chat_id, reply_to, user_id, url):
    wait = await client.send_message(chat_id, "🌑 <b>Analyzing link</b>", parse_mode="html", reply_to=reply_to)
    anim = spawn(animate(wait, "Analyzing link"))
    try:
        s = summarize(await asyncio.to_thread(extract_info, url), url)
    except Exception as e:
        log.warning("analyze failed for %s: %s", url, e)
        s = None
        if YT_RE.search(url):
            try:
                s = await asyncio.to_thread(oembed_summary, url)
            except Exception as e2:
                log.warning("oembed fallback failed: %s", e2)
        if s is None:
            await stop_task(anim)
            await safe_edit(wait, error_message(e, user_id))
            return
    await stop_task(anim)
    token = new_token(user_id, url, {**s, "src_msg": reply_to})
    text, buttons = card_text(s), options_keyboard(token, s)
    if s["thumb"]:
        try:
            await client.send_file(chat_id, s["thumb"], caption=text, buttons=buttons, parse_mode="html", reply_to=reply_to)
            await wait.delete()
            return
        except Exception as e:
            log.debug("thumbnail send failed: %s", e)
    await safe_edit(wait, text, buttons=buttons, link_preview=False)


async def cmd_start(event):
    if not await guard(event):
        return
    user = await event.get_sender()
    await event.reply(welcome_text(user.first_name), parse_mode="html", link_preview=False)


async def on_text(event):
    if event.message.document is not None:
        return  # files are handled by on_document (admin cookies upload)
    if is_admin(event.sender_id) and event.sender_id in ADMIN_STATE:
        await handle_admin_input(event)
        return
    if not await guard(event):
        return
    text = (event.raw_text or "").strip()
    if not text:
        return
    wait = cooldown_left(event.sender_id)
    if wait:
        await event.reply(f"⏳ Please wait {wait}s before the next request.")
        return
    spawn(react(event.chat_id, event.id, "👀"))
    m = URL_RE.search(text)
    if m:
        await show_options(event.chat_id, event.id, event.sender_id, m.group(0).rstrip(").,]"))
        return
    query = text[:100]
    status = await event.reply(f"🌑 <b>Searching “{esc(query)}”</b>", parse_mode="html")
    anim = spawn(animate(status, f"Searching “{esc(query)}”"))
    try:
        results = await asyncio.to_thread(search_youtube, query)
    except Exception as e:
        log.warning("search failed: %s", e)
        results = []
    await stop_task(anim)
    if not results:
        await safe_edit(status, "❌ <b>No results</b>\n\nTry different words or send a direct link.")
        return
    token = new_token(event.sender_id, None, {"results": results})
    rows = [
        [Button.inline(f"{r['title'][:44]} · {human_time(r['duration'])}" if r["duration"] else r["title"][:52],
                       f"pick:{token}:{i}".encode())]
        for i, r in enumerate(results)
    ]
    await safe_edit(status, f"🔎 <b>Results for</b> “{esc(query)}”", buttons=rows)


async def cb_check_subs(event):
    if await check_subscription(event):
        await event.answer("✅ Verified!")
        await safe_edit(await event.get_message(), "✅ <b>Access granted!</b>\n\nSend me a link or a song name.")
    else:
        await event.answer("❌ You haven't joined all channels yet.", alert=True)


async def cb_pick(event):
    _, token, idx = event.data.decode().split(":")
    req = PENDING.get(token)
    if not req:
        await event.answer("⌛ Expired — please search again.", alert=True)
        return
    if req["user_id"] != event.sender_id:
        await event.answer("This isn't your request.", alert=True)
        return
    await event.answer()
    url = req["results"][int(idx)]["url"]
    try:
        await (await event.get_message()).delete()
    except Exception:
        pass
    await show_options(event.chat_id, None, event.sender_id, url)


async def cb_download(event):
    _, token, kind, choice = event.data.decode().split(":")
    req = PENDING.get(token)
    if not req:
        await event.answer("⌛ Expired — please send the link again.", alert=True)
        return
    if req["user_id"] != event.sender_id:
        await event.answer("This isn't your request.", alert=True)
        return
    if event.sender_id in ACTIVE:
        await event.answer("You already have a download running.", alert=True)
        return
    if not await check_subscription(event):
        await event.answer("Join the required channels first.", alert=True)
        return
    await event.answer()
    ACTIVE.add(event.sender_id)
    msg = await event.get_message()
    task = asyncio.create_task(do_job(msg, token, req, kind, choice, event.chat_id, event.sender_id))
    RUNNING[token] = task
    task.add_done_callback(lambda _t, tk=token: RUNNING.pop(tk, None))


async def cb_cancel(event):
    token = event.data.decode().split(":")[1]
    req = PENDING.get(token)
    if not req or req["user_id"] != event.sender_id:
        await event.answer()
        return
    if req.get("prog"):
        req["prog"].cancel = True
    task = RUNNING.get(token)
    if task:
        task.cancel()
    await event.answer("Cancelling…")


def ensure_deno():
    """YouTube needs a JavaScript runtime for yt-dlp. Download deno locally if it is missing
    (works on Render's Python runtime, no build-command changes needed)."""
    if shutil.which("deno"):
        return True
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(platform.machine().lower())
    if sys.platform != "linux" or not arch:
        log.warning("Cannot auto-install deno on %s/%s", sys.platform, platform.machine())
        return False
    url = f"https://github.com/denoland/deno/releases/latest/download/deno-{arch}-unknown-linux-gnu.zip"
    try:
        r = requests.get(url, timeout=180)
        r.raise_for_status()
        DENO_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            z.extract("deno", DENO_DIR)
        (DENO_DIR / "deno").chmod(0o755)
        os.environ["PATH"] = f"{DENO_DIR}{os.pathsep}{os.environ.get('PATH', '')}"
        log.info("deno installed at %s", DENO_DIR)
        return True
    except Exception as e:
        log.warning("deno auto-install failed: %s", e)
        return False


async def cmd_diag(event):
    """Admin: show what the server has and run a live YouTube test."""
    if event.sender_id not in ADMIN_IDS:
        return
    status = await event.reply("🌑 <b>Running checks</b>", parse_mode="html")
    anim = spawn(animate(status, "Running checks"))
    ver = getattr(getattr(yt_dlp, "version", None), "__version__", "?")
    ok = lambda v: "✅" if v else "❌"  # noqa: E731
    rows = [
        f"yt-dlp: <code>{esc(str(ver))}</code>",
        f"ffmpeg {ok(shutil.which('ffmpeg'))}   ffprobe {ok(shutil.which('ffprobe'))}",
        f"deno (JS runtime) {ok(shutil.which('deno'))}",
        f"curl_cffi {ok(importlib.util.find_spec('curl_cffi'))}",
        f"cookies {ok(COOKIES_PATH.exists())}   proxy {ok(PROXY_URL)}",
    ]
    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(extract_info, "https://www.youtube.com/watch?v=jNQXAC9IVRw"), 90)
        rows.append(f"YouTube test: ✅ OK ({len(info.get('formats') or [])} formats)")
    except Exception as e:
        rows.append(f"YouTube test: ❌\n<code>{esc(str(e)[:350])}</code>")
    await stop_task(anim)
    await safe_edit(status, "🩺 <b>Diagnostics</b>\n\n" + "\n".join(rows))


# ---- admin -----------------------------------------------------------------
# ---- admin panel -----------------------------------------------------------
def is_admin(uid):
    return uid in ADMIN_IDS


def panel_text():
    fs = ", ".join("@" + c for c in CFG["force_sub"]) or "none"
    return (
        "⚙️ <b>Admin Panel</b>\n\n"
        f"🔒 Force-subscribe: <b>{'ON' if CFG['force_sub_enabled'] else 'OFF'}</b>\n"
        f"📢 Channels: {esc(fs)}\n"
        f"⏱ Cooldown: <b>{CFG['cooldown']}s</b>\n"
        f"📝 Welcome message: <b>{'custom' if CFG['welcome'] else 'default'}</b>\n"
        f"🍪 Cookies: <b>{'uploaded' if COOKIES_PATH.exists() else 'not set'}</b>\n\n"
        "🩺 Send /diag to test YouTube and check the server."
    )


def panel_buttons():
    return [
        [Button.inline("📝 Welcome message", b"adm:welcome"), Button.inline("📢 Channels", b"adm:channels")],
        [
            Button.inline(f"🔒 Force-sub: {'ON' if CFG['force_sub_enabled'] else 'OFF'}", b"adm:togglefs"),
            Button.inline("⏱ Cooldown", b"adm:cooldown"),
        ],
        [Button.inline("🍪 Upload cookies", b"adm:cookies"), Button.inline("📊 Stats", b"adm:stats")],
        [Button.inline("📣 Broadcast help", b"adm:bchelp")],
    ]


BACK = [Button.inline("⬅️ Back", b"adm:back")]

PROMPTS = {
    "welcome": (
        "📝 <b>Send the new welcome message</b> as your next message.\n\n"
        "HTML is allowed: &lt;b&gt;bold&lt;/b&gt;, &lt;i&gt;italic&lt;/i&gt;, &lt;a href=\"https://…\"&gt;link&lt;/a&gt;.\n"
        "Use <code>{name}</code> for the user's first name.\n\n/cancel to abort.",
        [[Button.inline("♻️ Reset to default", b"adm:welcome_reset")], BACK],
    ),
    "addch": (
        "📢 <b>Send the channel username</b> (like <code>@mychannel</code>).\n\n"
        "The channel must be public and this bot must already be an <b>admin</b> there.\n\n/cancel to abort.",
        [BACK],
    ),
    "cooldown": ("⏱ <b>Send the cooldown in seconds</b> (0–300).\n\n/cancel to abort.", [BACK]),
    "cookies": (
        "🍪 <b>Send your cookies.txt file</b> (Netscape format) as a document.\n\n"
        "It helps with YouTube/Instagram login or bot checks. Use a throw-away account. "
        "I'll delete the file from this chat after saving.\n\n/cancel to abort.",
        [[Button.inline("🗑 Remove cookies", b"adm:cookies_remove")], BACK],
    ),
}


def channels_view():
    rows = [[Button.inline(f"🗑 Remove @{c}", f"adm:rmch:{i}".encode())] for i, c in enumerate(CFG["force_sub"])]
    rows.append([Button.inline("➕ Add channel", b"adm:addch")])
    rows.append(BACK)
    listing = "\n".join(f"• @{esc(c)}" for c in CFG["force_sub"]) or "No channels set."
    return (
        f"📢 <b>Force-subscribe channels</b>\n\n{listing}\n\n"
        "The bot must be an <b>admin</b> in each channel.",
        rows,
    )


async def stats_text():
    total, new, active, downloads, size, sites = await db.stats()
    top = "\n".join(f"  • {esc(str(s['_id']))}: {s['n']:,}" for s in sites) or "  –"
    return (
        "📊 <b>Bot Statistics</b>\n\n"
        f"👥 Users: <b>{total:,}</b>  (🆕 {new:,} / 24h, 🟢 {active:,} active)\n"
        f"📥 Downloads: <b>{downloads:,}</b>\n"
        f"💾 Data sent: <b>{human_size(size)}</b>\n\n"
        f"🌐 <b>Top sites</b>\n{top}"
    )


async def cmd_admin(event):
    if not is_admin(event.sender_id):
        return
    ADMIN_STATE.pop(event.sender_id, None)
    await event.reply(panel_text(), buttons=panel_buttons(), parse_mode="html")


async def cmd_cancel(event):
    if is_admin(event.sender_id) and ADMIN_STATE.pop(event.sender_id, None):
        await event.reply("🚫 Cancelled. Open /admin again anytime.")


async def cmd_stats(event):
    if is_admin(event.sender_id):
        await event.reply(await stats_text(), parse_mode="html")


async def cb_admin(event):
    if not is_admin(event.sender_id):
        await event.answer("Admins only.", alert=True)
        return
    parts = event.data.decode().split(":")
    act = parts[1]
    uid = event.sender_id
    msg = await event.get_message()
    await event.answer()

    if act == "back":
        ADMIN_STATE.pop(uid, None)
        await safe_edit(msg, panel_text(), buttons=panel_buttons())
    elif act == "togglefs":
        CFG["force_sub_enabled"] = not CFG["force_sub_enabled"]
        await db.save_config(force_sub_enabled=CFG["force_sub_enabled"])
        await safe_edit(msg, panel_text(), buttons=panel_buttons())
    elif act == "channels":
        ADMIN_STATE.pop(uid, None)
        text, rows = channels_view()
        await safe_edit(msg, text, buttons=rows)
    elif act == "rmch":
        i = int(parts[2])
        if 0 <= i < len(CFG["force_sub"]):
            CFG["force_sub"].pop(i)
            await db.save_config(force_sub=CFG["force_sub"])
            _sub_cache.clear()
        text, rows = channels_view()
        await safe_edit(msg, text, buttons=rows)
    elif act in PROMPTS:
        ADMIN_STATE[uid] = {"action": act}
        text, rows = PROMPTS[act]
        await safe_edit(msg, text, buttons=rows, link_preview=False)
    elif act == "welcome_reset":
        ADMIN_STATE.pop(uid, None)
        CFG["welcome"] = None
        await db.save_config(welcome=None)
        await safe_edit(msg, panel_text(), buttons=panel_buttons())
    elif act == "cookies_remove":
        ADMIN_STATE.pop(uid, None)
        COOKIES_PATH.unlink(missing_ok=True)
        await db.set_cookies(None)
        await safe_edit(msg, panel_text(), buttons=panel_buttons())
    elif act == "stats":
        await event.respond(await stats_text(), parse_mode="html")
    elif act == "bchelp":
        await event.respond(
            "📣 <b>Broadcast</b>\n\nReply to any message (text, photo, video…) with <code>/broadcast</code>, "
            "or send <code>/broadcast your text</code>.\n\n"
            "🚫 <code>/ban user_id</code> · ✅ <code>/unban user_id</code>",
            parse_mode="html",
        )


async def handle_admin_input(event):
    uid = event.sender_id
    st = ADMIN_STATE.get(uid)
    if not st:
        return
    action = st["action"]
    text = (event.raw_text or "").strip()
    if not text:
        await event.reply("Please send text (or /cancel).")
        return

    if action == "welcome":
        old = CFG["welcome"]
        CFG["welcome"] = text
        try:
            user = await event.get_sender()
            await event.reply("👀 <b>Preview:</b>", parse_mode="html")
            await event.reply(welcome_text(user.first_name), parse_mode="html", link_preview=False)
        except Exception as e:
            CFG["welcome"] = old
            await event.reply(f"❌ That HTML is invalid, nothing was changed.\n<code>{esc(str(e)[:200])}</code>", parse_mode="html")
            return
        await db.save_config(welcome=text)
        ADMIN_STATE.pop(uid, None)
        await event.reply("✅ Welcome message saved.", buttons=[BACK])

    elif action == "cooldown":
        if not text.isdigit() or not 0 <= int(text) <= 300:
            await event.reply("Send a number between 0 and 300 (or /cancel).")
            return
        CFG["cooldown"] = int(text)
        await db.save_config(cooldown=CFG["cooldown"])
        ADMIN_STATE.pop(uid, None)
        await event.reply(f"✅ Cooldown set to {CFG['cooldown']}s.", buttons=[BACK])

    elif action == "addch":
        name = text.replace("https://t.me/", "").replace("t.me/", "").lstrip("@").strip("/ ")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", name):
            await event.reply("That doesn't look like a channel username. Try <code>@mychannel</code>.", parse_mode="html")
            return
        if name.lower() in (c.lower() for c in CFG["force_sub"]):
            await event.reply("That channel is already in the list.")
            return
        try:
            ent = await client.get_entity("@" + name)
            perms = await client.get_permissions(ent, await client.get_me())
            if not perms.is_admin:
                raise ValueError("the bot is not an admin there")
        except Exception as e:
            await event.reply(
                f"❌ Couldn't add @{esc(name)}: {esc(str(e)[:150])}\n\nMake the bot an admin there, then send the username again (or /cancel).",
                parse_mode="html",
            )
            return
        CFG["force_sub"].append(name)
        await db.save_config(force_sub=CFG["force_sub"])
        _sub_cache.clear()
        ADMIN_STATE.pop(uid, None)
        text2, rows = channels_view()
        await event.reply("✅ Added.\n\n" + text2, buttons=rows, parse_mode="html")


async def on_document(event):
    st = ADMIN_STATE.get(event.sender_id)
    if not is_admin(event.sender_id) or not st or st["action"] != "cookies":
        return
    doc = event.message.document
    if doc.size > 2 * MB:
        await event.reply("❌ File too big for a cookies file.")
        return
    data = await event.message.download_media(bytes)
    text = (data or b"").decode("utf-8", "ignore")
    if "\t" not in text or len(text) < 50:
        await event.reply("❌ That doesn't look like a Netscape cookies.txt. Export it again (or /cancel).")
        return
    COOKIES_PATH.write_text(text)
    await db.set_cookies(text)
    ADMIN_STATE.pop(event.sender_id, None)
    try:
        await event.delete()
    except Exception:
        pass
    await client.send_message(event.chat_id, "✅ Cookies saved. They are used for new downloads right away.", buttons=[BACK])


async def cmd_ban(event):
    if event.sender_id not in ADMIN_IDS:
        return
    parts = event.raw_text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await event.reply("Usage: /ban <user_id>  or  /unban <user_id>")
        return
    flag = parts[0].lower().startswith("/ban")
    ok = await db.set_banned(int(parts[1]), flag)
    await event.reply(("🚫 Banned" if flag else "✅ Unbanned") + f" {parts[1]}" if ok else "User not found.")


async def cmd_broadcast(event):
    """Reply to any message with /broadcast, or: /broadcast your text"""
    if event.sender_id not in ADMIN_IDS:
        return
    src = await event.get_reply_message()
    text = event.raw_text.partition(" ")[2].strip()
    if not src and not text:
        await event.reply("Reply to a message with /broadcast, or send: /broadcast your text")
        return
    status = await event.reply("📣 Broadcasting…")
    ok = fail = 0
    async for doc in db.users.find({"banned": {"$ne": True}, "access_hash": {"$ne": None}}):
        peer = types.InputPeerUser(doc["user_id"], doc["access_hash"])
        for attempt in (1, 2):
            try:
                await client.send_message(peer, src if src else text)
                ok += 1
                break
            except errors.FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
            except Exception:
                fail += 1
                break
        await asyncio.sleep(0.05)
    await safe_edit(status, f"📣 <b>Broadcast done</b>\n\n✅ Sent: {ok:,}\n❌ Failed: {fail:,}")


# ================================ STARTUP ================================
async def health_server(port):
    async def handle(reader, writer):
        try:
            await reader.read(1024)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK")
            await writer.drain()
        finally:
            writer.close()

    await asyncio.start_server(handle, "0.0.0.0", int(port))
    log.info("Health server on port %s", port)


def register_handlers():
    private = lambda e: e.is_private  # noqa: E731
    cmd = lambda name: events.NewMessage(incoming=True, pattern=rf"(?i)^/{name}(@\w+)?(\s.*)?$", func=private)  # noqa: E731
    client.add_event_handler(cmd_start, cmd("(?:start|help)"))
    client.add_event_handler(cmd_stats, cmd("stats"))
    client.add_event_handler(cmd_ban, cmd("(?:ban|unban)"))
    client.add_event_handler(cmd_broadcast, cmd("broadcast"))
    client.add_event_handler(cmd_admin, cmd("admin"))
    client.add_event_handler(cmd_diag, cmd("diag"))
    client.add_event_handler(cmd_cancel, cmd("cancel"))
    client.add_event_handler(
        on_document,
        events.NewMessage(incoming=True, func=lambda e: e.is_private and e.message.document is not None),
    )
    client.add_event_handler(
        on_text,
        events.NewMessage(incoming=True, func=lambda e: e.is_private and not (e.raw_text or "").startswith("/")),
    )
    client.add_event_handler(cb_check_subs, events.CallbackQuery(pattern=rb"^check_subs$"))
    client.add_event_handler(cb_pick, events.CallbackQuery(pattern=rb"^pick:"))
    client.add_event_handler(cb_download, events.CallbackQuery(pattern=rb"^dl:"))
    client.add_event_handler(cb_cancel, events.CallbackQuery(pattern=rb"^cancel:"))
    client.add_event_handler(cb_admin, events.CallbackQuery(pattern=rb"^adm:"))


async def main():
    global client, db, BOT_USERNAME, JOB_SEM
    JOB_SEM = asyncio.Semaphore(MAX_PARALLEL)
    db = Database(MONGO_URI, DB_NAME)
    await db.init()
    saved = await db.load_config()
    CFG.update({k: v for k, v in saved.items() if k in CFG})
    cookies = await db.get_cookies()
    if cookies:
        COOKIES_PATH.write_text(cookies)

    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    register_handlers()
    await client.start(bot_token=BOT_TOKEN)
    BOT_USERNAME = (await client.get_me()).username
    if not shutil.which("deno"):
        log.warning("deno not found - installing it in the background (needed for YouTube)")
        spawn(asyncio.to_thread(ensure_deno))
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            log.warning("⚠️ %s not found - downloads will fail. Deploy with the Dockerfile (Docker runtime).", tool)
    await client(
        functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(),
            lang_code="",
            commands=[
                types.BotCommand("start", "Start the bot"),
                types.BotCommand("help", "How to use"),
            ],
        )
    )
    if PORT:
        await health_server(PORT)
    log.info("🤖 @%s online | force-sub: %s | parallel jobs: %d", BOT_USERNAME, CFG["force_sub"], MAX_PARALLEL)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
