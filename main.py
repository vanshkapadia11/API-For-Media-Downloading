import io
import os
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
import hashlib
import json
import re
from datetime import datetime, timezone

import requests as req_lib
import yt_dlp
from flask import Flask, request, jsonify, send_file

# ── ffmpeg ─────────────────────────────────────────────────────────────────────
try:
    import imageio_ffmpeg

    _ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    os.environ["PATH"] = (
        os.path.dirname(_ffmpeg_exe) + os.pathsep + os.environ.get("PATH", "")
    )
    print(f"[ffmpeg] ✅ {_ffmpeg_exe}")
except Exception as e:
    print(f"[ffmpeg] ⚠️ {e}")

# ── Node.js ────────────────────────────────────────────────────────────────────
_node_exe = shutil.which("node") or shutil.which("node.exe")
if not _node_exe:
    for _p in [
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
        os.path.expanduser(r"~\AppData\Roaming\nvm\current\node.exe"),
        os.path.expanduser(r"~\AppData\Local\Programs\nodejs\node.exe"),
    ]:
        if os.path.exists(_p):
            _node_exe = _p
            break

if _node_exe:
    os.environ["PATH"] = (
        os.path.dirname(_node_exe) + os.pathsep + os.environ.get("PATH", "")
    )
    print(f"[node] ✅ {_node_exe}")
else:
    print("[node] ⚠️ Not found — JS challenge solving will fail")

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_cors import CORS

app = Flask(__name__)
API_SECRET = os.environ.get("API_SECRET", "")

# Only allow requests from your frontend domain
CORS(
    app,
    origins=[
        "https://www.vidiflow.co/",  # ← replace with your actual frontend URL
    ],
)

# Rate limiting — 30 requests per hour per IP
limiter = Limiter(
    get_remote_address, app=app, default_limits=["30 per hour"], storage_uri="memory://"
)


# ══════════════════════════════════════════════════════════════════════════════
# PROXY MANAGER
# ══════════════════════════════════════════════════════════════════════════════

_PROXY_RAW = os.environ.get("YTDLP_PROXY", "")
_PROXY_LIST_RAW = os.environ.get("YTDLP_PROXY_LIST", "")


def _parse_proxy_list() -> list[str]:
    proxies = []
    if _PROXY_LIST_RAW:
        for entry in _PROXY_LIST_RAW.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) == 4:
                host, port, password, username = parts
                formatted = f"http://{password}:{username}@{host}:{port}"
                proxies.append(formatted)
                print(f"[proxy] Parsed → {formatted[:60]}")
            else:
                proxies.append(entry)
    if _PROXY_RAW and _PROXY_RAW not in proxies:
        proxies.append(_PROXY_RAW)
    return proxies


class ProxyManager:
    def __init__(self):
        self._proxies = _parse_proxy_list()
        self._index = 0
        self._lock = threading.Lock()
        if self._proxies:
            print(f"[proxy] {len(self._proxies)} proxies loaded")
            for i, p in enumerate(self._proxies):
                print(f"[proxy]   [{i}] {p[:60]}")
        else:
            print("[proxy] No proxies configured")

    def current(self) -> str:
        with self._lock:
            if not self._proxies:
                return ""
            for _ in range(len(self._proxies)):
                p = self._proxies[self._index % len(self._proxies)]
                with _proxy_failures_lock:
                    if _proxy_failures.get(p, 0) < _PROXY_DEAD_THRESHOLD:
                        return p
                self._index = (self._index + 1) % len(self._proxies)
            return self._proxies[0]  # all dead, return first as fallback

    def rotate(self) -> str:
        with self._lock:
            if not self._proxies:
                return ""
            self._index = (self._index + 1) % len(self._proxies)
            new = self._proxies[self._index]
            print(f"[proxy] Rotated → [{self._index}] {new[:60]}")
            return new

    def has_proxies(self) -> bool:
        return len(self._proxies) > 0

    def record_failure(self, proxy: str):
        if not proxy:
            return
        with _proxy_failures_lock:
            _proxy_failures[proxy] = _proxy_failures.get(proxy, 0) + 1
            count = _proxy_failures[proxy]
            if count >= _PROXY_DEAD_THRESHOLD:
                print(f"[proxy] 💀 {proxy[:50]} marked dead ({count} failures)")

    def record_success(self, proxy: str):
        if not proxy:
            return
        with _proxy_failures_lock:
            if _proxy_failures.get(proxy, 0) > 0:
                print(f"[proxy] ✅ {proxy[:50]} recovered")
            _proxy_failures[proxy] = 0


_proxy_failures: dict[str, int] = {}
_proxy_failures_lock = threading.Lock()
_PROXY_DEAD_THRESHOLD = 5
_proxy_manager = ProxyManager()


# ══════════════════════════════════════════════════════════════════════════════
# COOKIE MANAGEMENT
# ══════════════════════════

_cookie_path_cache: dict[str, str | None] = {}
_cookie_freshness_cache: dict[str, tuple[bool, float]] = {}
_cookie_cache_lock = threading.Lock()

# Track how many consecutive bot-check failures we've seen per cookie file
# so we can emit escalating warnings without spamming logs every request.
_bot_fail_counts: dict[str, int] = {}
_bot_fail_lock = threading.Lock()

# ── PO TOKEN CACHE ─────────────────────────────────────────────────────────────
_po_token_cache: dict[str, tuple[str, str, float]] = {}
_po_lock = threading.Lock()
_po_token_generator_available: bool | None = None  # None = not yet checked


def _check_po_generator() -> bool:
    global _po_token_generator_available
    if _po_token_generator_available is not None:
        return _po_token_generator_available
    found = bool(shutil.which("youtube-po-token-generator"))
    if found:
        try:
            r = subprocess.run(
                ["youtube-po-token-generator"],
                capture_output=True,
                text=True,
                timeout=30,  # ← was 8
            )
            json.loads(r.stdout)
            print(f"[po-token] ✅ found and working")
        except Exception:
            print(f"[po-token] ❌ found but non-functional — disabling")
            found = False
    else:
        print(f"[po-token] ❌ not found — skipping PO tokens")
    _po_token_generator_available = found
    return found


_po_token_broken = False  # module-level flag


def _get_po_token(proxy: str = "") -> tuple[str, str]:
    global _po_token_broken
    if _po_token_broken:
        return "", ""
    if not _check_po_generator():
        return "", ""

    now = time.time()
    key = proxy or "direct"
    with _po_lock:
        cached = _po_token_cache.get(key)
        if cached and now < cached[2]:
            return cached[0], cached[1]

    # Run in background thread with short timeout so we never block a request
    result_holder: list = []

    def _run():
        try:
            env = os.environ.copy()
            if proxy:
                env["HTTPS_PROXY"] = proxy
                env["HTTP_PROXY"] = proxy
            r = subprocess.run(
                ["youtube-po-token-generator"],
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
            )
            data = json.loads(r.stdout)
            result_holder.append((data.get("poToken", ""), data.get("visitorData", "")))
        except Exception as e:
            print(f"[po-token] ❌ {e}")
            result_holder.append(("", ""))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # t.join(timeout=12)  # wait max 12s, then give up
    t.join(timeout=25)  # fail fast, don't block requests

    if not result_holder:
        print("[po-token] ⏱ timed out repeatedly — disabling for this session")
        _po_token_broken = True
        return "", ""

    token, visitor = result_holder[0]
    if token:
        with _po_lock:
            _po_token_cache[key] = (token, visitor, now + 21600)
        print(f"[po-token] ✅ cached for proxy={key[:40]}")
    return token, visitor


_YT_SESSION_KEYS = {"SAPISID", "__Secure-3PAPISID", "LOGIN_INFO", "SID", "HSID"}


def _check_cookie_freshness(cookie_path: str) -> bool:
    now = time.time()
    with _cookie_cache_lock:
        cached = _cookie_freshness_cache.get(cookie_path)
        if cached and (now - cached[1]) < 300:
            return cached[0]
    result = _do_check_cookie_freshness(cookie_path)
    with _cookie_cache_lock:
        _cookie_freshness_cache[cookie_path] = (result, now)
    return result


def _invalidate_cookie_cache(cookie_path: str):
    """Force re-validation next time (call after detecting a bot check)."""
    with _cookie_cache_lock:
        _cookie_freshness_cache.pop(cookie_path, None)


def _do_check_cookie_freshness(cookie_path: str) -> bool:
    if not cookie_path or not os.path.exists(cookie_path):
        return False
    now = time.time()
    found = 0
    expired = 0
    try:
        with open(cookie_path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) != 7:
                    continue
                domain, _, _, _, expiry_str, name, _ = parts
                if "youtube.com" not in domain and "google.com" not in domain:
                    continue
                if name not in _YT_SESSION_KEYS:
                    continue
                found += 1
                try:
                    expiry = int(expiry_str)
                    if expiry != 0 and expiry < now:
                        expired += 1
                except ValueError:
                    pass
        if found == 0:
            print("[cookies] ⚠️  No YouTube session cookies found in file")
            return False
        if expired == found:
            print(f"[cookies] ❌ All {found} session cookies are EXPIRED")
            return False
        print(f"[cookies] ✅ {found - expired}/{found} session cookies valid")
        return True
    except Exception as e:
        print(f"[cookies] ⚠️  Could not validate cookies: {e}")
        return False


def _get_cookie_path(platform: str) -> str | None:
    with _cookie_cache_lock:
        if platform in _cookie_path_cache:
            return _cookie_path_cache[platform]
    result = _resolve_cookie_path(platform)
    with _cookie_cache_lock:
        _cookie_path_cache[platform] = result
    return result


def _resolve_cookie_path(platform: str) -> str | None:
    # Check Render secret file location first — copy to /tmp since /etc/secrets is read-only
    secret_path = f"/etc/secrets/{platform}_cookies.txt"
    if os.path.exists(secret_path):
        tmp_copy = f"/tmp/{platform}_cookies_secret.txt"
        shutil.copy2(secret_path, tmp_copy)
        print(f"[cookies] ✅ secret file copied → {tmp_copy}")
        return tmp_copy

    local = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), f"{platform}_cookies.txt"
    )
    if os.path.exists(local):
        print(f"[cookies] ✅ local {platform}_cookies.txt")
        return local
    env_key = "YOUTUBE_COOKIES" if platform == "youtube" else "INSTAGRAM_COOKIES"
    raw = os.environ.get(env_key, "")
    if not raw.strip():
        print(f"[cookies] ⚠️ no cookies for {platform}")
        return None
    content = raw
    try:
        content = (
            raw.encode("utf-8")
            .decode("unicode_escape")
            .encode("latin-1")
            .decode("utf-8")
        )
    except Exception:
        pass
    content = content.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    lines = [l for l in content.split("\n") if l.strip() and not l.startswith("#")]
    valid = [l for l in lines if len(l.split("\t")) == 7]
    print(f"[cookies] {env_key}: {len(lines)} lines, {len(valid)} valid (7-col)")
    if not valid:
        print(f"[cookies] ❌ No valid cookie lines — skipping")
        return None
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        prefix=f"{platform}_cookies_",
        newline="\n",
    )
    tmp.write(content)
    tmp.flush()
    tmp.close()
    print(f"[cookies] ✅ {len(valid)} cookies written → {tmp.name}")
    return tmp.name


# ══════════════════════════════════════════════════════════════════════════════
# BOT-CHECK DETECTION & TRACKING
# ══════════════════════════════════════════════════════════════════════════════

# Error substrings that confirm YouTube served us a bot/consent check.
# Ordered from most to least specific.
_BOT_PATTERNS = [
    "sign in to confirm",
    "confirm you're not a bot",
    "bot check",
    "please sign in",
    "requires authentication",
    "age-restricted",
    "precondition check",
    "nsig",
    "429",
    "too many requests",
]

# Errors that are definitely NOT a bot check and should not trigger cookie advice.
_HARD_ERRORS = [
    "private video",
    "video is private",
    "copyright",
    "not available in your country",
    "removed by",
]


def _is_bot_check(msg: str) -> bool:
    m = msg.lower()
    if any(p in m for p in _HARD_ERRORS):
        return False
    return any(p in m for p in _BOT_PATTERNS)


def _is_proxy_error(msg: str) -> bool:
    m = msg.lower()
    return any(
        x in m
        for x in [
            "407",
            "proxy authentication",
            "tunnel connection failed",
            "unable to connect to proxy",
            "proxyerror",
            "proxy error",
        ]
    )


def _record_bot_check(cookie_path: str | None):
    """
    Increment the consecutive bot-check counter for this cookie file.
    Invalidates the freshness cache so the next request re-validates.
    Logs a clear, actionable message with escalating severity.
    """
    key = cookie_path or "__no_cookies__"
    with _bot_fail_lock:
        _bot_fail_counts[key] = _bot_fail_counts.get(key, 0) + 1
        count = _bot_fail_counts[key]

    if cookie_path:
        _invalidate_cookie_cache(cookie_path)

    if count == 1:
        print(
            "[bot-check] ⚠️  YouTube served a bot/consent challenge. "
            "If this persists, re-export cookies from a fresh browser session."
        )
    elif count == 3:
        print(
            "[bot-check] ❌ 3 consecutive bot checks. Cookies are likely stale. "
            "ACTION REQUIRED: re-export youtube_cookies.txt from Chrome/Firefox "
            "while logged in to YouTube, then restart the server."
        )
    elif count >= 5 and count % 5 == 0:
        print(
            f"[bot-check] 🚨 {count} consecutive bot checks. "
            "All clients are being blocked. Check your IP reputation / proxy health."
        )


def _reset_bot_check_counter(cookie_path: str | None):
    """Call on a successful extraction to reset the failure streak."""
    key = cookie_path or "__no_cookies__"
    with _bot_fail_lock:
        if _bot_fail_counts.get(key, 0) > 0:
            print(f"[bot-check] ✅ Successful extraction — counter reset.")
            _bot_fail_counts[key] = 0


# ══════════════════════════════════════════════════════════════════════════════
# YT-DLP BASE OPTIONS
# ══════════════════════════════════════════════════════════════════════════════

_UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_UA_IOS = "com.google.ios.youtube/19.29.1 CFNetwork/1490.0.4 Darwin/23.2.0"
_UA_ANDROID = (
    "com.google.android.youtube/19.29.37 " "(Linux; U; Android 13; en_US; Pixel 7) gzip"
)
_UA_TV = "Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0) AppleWebKit/538.1"


def _base_opts(download: bool = False, proxy: str = "") -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": False,
        "noplaylist": False,
        "nocheckcertificate": True,
        "retries": 5 if download else 3,
        "fragment_retries": 5 if download else 2,
        # "socket_timeout": 30 if download else 15,
        # "socket_timeout": 120 if download else 15,  # Increased to 120s
        "socket_timeout": 180 if download else 15,  # ← Change 120 to 180
        "http_chunk_size": 10 * 1024 * 1024,  # 10 MB chunks for large files
    }
    if _node_exe:  # ← YE WAPAS DAL
        opts["js_runtimes"] = {"node": {"path": _node_exe}}  # ← YE WAPAS DAL
        opts["remote_components"] = ["ejs:github"]  # npm nahi, sirf github
    p = proxy or _proxy_manager.current()
    if p:
        opts["proxy"] = p
    return opts


# ══════════════════════════════════════════════════════════════════════════════
# YOUTUBE CLIENT CHAIN
#
# Ordered from least likely to trigger a bot-check to most:
#   1. tv_embedded  — TV/smart-TV client; no JS, rarely challenged
#   2. ios          — mobile client; cookie-free, good success rate
#   3. android      — mobile client; occasionally needs cookies for 18+
#   4. web_embedded — embedded player; lower bot-check rate than plain web
#   5. web          — last resort; highest capability but most challenged
#
# (client, skip_protos, use_cookies, ua)
# ══════════════════════════════════════════════════════════════════════════════

_last_success: dict = {"proxy_index": 0, "client": "android"}
_last_success_lock = threading.Lock()

_YT_CLIENT_CHAIN = [
    ("android_testsuite", [], False, _UA_ANDROID),  # best cookieless
    ("tv_embedded", [], False, _UA_TV),  # you have _UA_TV but never use it
    ("ios", [], False, _UA_IOS),
    ("android", [], False, _UA_ANDROID),
    ("mweb", [], False, _UA_IOS),
    ("web_embedded", [], True, _UA_DESKTOP),
    ("web", [], True, _UA_DESKTOP),
]

def _yt_opts_for_client(
    client: str,
    skip_protos: list,
    use_cookies: bool,
    ua: str,
    extra: dict = {},
    download: bool = False,
    proxy: str = "",
) -> dict:
    opts = _base_opts(download, proxy)
    ea: dict = {"player_client": [client]}
    if skip_protos:
        ea["skip"] = skip_protos

    # Inject PO token — helps bypass bot checks without needing fresh cookies
    po_token, visitor_data = _get_po_token(proxy)
    if po_token:
        ea["po_token"] = [f"web+{po_token}"]
    if visitor_data:
        ea["visitor_data"] = [visitor_data]

    opts.update(
        {
            "extractor_args": {"youtube": ea},
            "http_headers": {"User-Agent": ua},
            "geo_bypass": True,
            "geo_bypass_country": "US",
        }
    )
    opts.update(extra)

    if use_cookies:
        cp = _get_cookie_path("youtube")
        if cp:
            opts["cookiefile"] = cp
            # dl_opts["cookiesfrombrowser"] = ("chrome",)

    return opts


# ══════════════════════════════════════════════════════════════════════════════
# MAIN YOUTUBE EXTRACTOR — with bot-check tracking
# ══════════════════════════════════════════════════════════════════════════════


def _extract_yt(url: str, extra: dict = {}, download: bool = False):
    """
    Try each client in _YT_CLIENT_CHAIN.
    - Starts from last known working proxy+client combo.
    - On bot-check errors: record the failure, try the next client.
    - On proxy errors: rotate the proxy and retry all clients.
    - On hard errors (private, copyright): re-raise immediately.
    - Resets the bot-check counter on any success.
    """

    max_proxy_retries = (
        max(1, len(_proxy_manager._proxies)) if _proxy_manager.has_proxies() else 1
    )
    last_exc = None
    cookie_path = _get_cookie_path("youtube")

    # Start from last known good proxy
    with _last_success_lock:
        _proxy_manager._index = _last_success["proxy_index"]
        preferred_client = _last_success["client"]

    # Reorder chain to try preferred client first
    chain = sorted(_YT_CLIENT_CHAIN, key=lambda c: 0 if c[0] == preferred_client else 1)

    for proxy_attempt in range(max_proxy_retries):
        current_proxy = _proxy_manager.current()
        if proxy_attempt > 0:
            print(
                f"[yt-dlp] Proxy attempt {proxy_attempt + 1}/{max_proxy_retries} "
                f"→ {current_proxy[:50] or 'direct'}"
            )

        # for client, skip_protos, use_cookies, ua in _YT_CLIENT_CHAIN:
        for client, skip_protos, use_cookies, ua in chain:
            try:
                print(f"[yt-dlp] Trying client={client}")
                opts = _yt_opts_for_client(
                    client,
                    skip_protos,
                    use_cookies,
                    ua,
                    extra,
                    download,
                    proxy=current_proxy,
                )
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=download)

                if not info:
                    print(f"[yt-dlp] ⚠️  client={client} returned None")
                    continue

                # Validate we actually got playable formats
                fmts = info.get("formats") or []
                has_real = any(
                    f.get("url") and f.get("vcodec", "none") != "none" for f in fmts
                ) or (not fmts and info.get("url"))

                if download or has_real or info.get("url"):
                    print(f"[yt-dlp] ✅ client={client}")
                    _reset_bot_check_counter(cookie_path)
                    _proxy_manager.record_success(current_proxy)
                    with _last_success_lock:
                        _last_success["proxy_index"] = _proxy_manager._index
                        _last_success["client"] = client
                    return info, client

                print(f"[yt-dlp] ⚠️  client={client} — no real formats, skipping")

            except yt_dlp.utils.DownloadError as e:
                msg = str(e)
                print(f"[yt-dlp] ❌ client={client}: {msg[:160]}")
                last_exc = e

                if _is_proxy_error(msg):
                    print("[yt-dlp] 🔄 Proxy error — rotating and retrying all clients")
                    _proxy_manager.rotate()
                    break  # → retry outer loop with new proxy

                if _is_bot_check(msg):
                    _record_bot_check(cookie_path)
                    _proxy_manager.record_failure(current_proxy)
                    with _po_lock:
                        _po_token_cache.pop(current_proxy or "direct", None)
                    # ← REMOVE the rotate lines, just continue to next client
                    continue

                # Hard errors: propagate immediately
                msg_l = msg.lower()
                if any(p in msg_l for p in ["private", "copyright", "removed by"]):
                    raise

                continue

            except Exception as e:
                msg = str(e)
                print(f"[yt-dlp] ❌ client={client} unexpected: {msg[:120]}")
                last_exc = e
                if _is_proxy_error(msg):
                    print("[yt-dlp] 🔄 Proxy error — rotating and retrying all clients")
                    _proxy_manager.rotate()
                    break
                continue
        else:
            # Inner loop exhausted without a proxy-rotate break → no point
            # trying the same clients with the same proxy again.
            break

    raise last_exc or yt_dlp.utils.DownloadError(
        "All clients and proxies failed — YouTube may be rate-limiting this IP. "
        "Re-export cookies or rotate your proxy."
    )


# ══════════════════════════════════════════════════════════════════════════════
# INSTAGRAM
# ══════════════════════════════════════════════════════════════════════════════

_IG_DL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) " "AppleWebKit/605.1.15"
    ),
    "Referer": "https://www.instagram.com/",
}


def _ig_opts(extra: dict = {}, proxy: str = "") -> dict:
    p = proxy or _proxy_manager.current()
    opts = _base_opts(download=bool(extra), proxy=p)
    opts.update({"http_headers": {"User-Agent": _IG_DL_HEADERS["User-Agent"]}})
    if p:
        opts["proxy"] = p
    opts.update(extra)
    cp = _get_cookie_path("instagram")
    if cp:
        opts["cookiefile"] = cp
    return opts


def _ig_download_image(img_url: str, timeout: int = 30) -> tuple[bytes, str]:
    r = req_lib.get(img_url, headers=_IG_DL_HEADERS, timeout=timeout)
    r.raise_for_status()
    ct = r.headers.get("Content-Type", "image/jpeg")
    ext = "png" if "png" in ct else "webp" if "webp" in ct else "jpg"
    return r.content, ext


def _ig_best_image_url(entry: dict) -> str:
    thumbs = entry.get("thumbnails") or []
    if thumbs:
        best = sorted(thumbs, key=lambda t: t.get("preference", 0) or 0)[-1]
        url = best.get("url", "")
        if url:
            return url
    return entry.get("url") or entry.get("thumbnail", "")


def _ig_classify(info: dict) -> str:
    entries = info.get("entries") or []
    if entries:
        return "carousel"
    if info.get("_type") == "playlist":
        return "carousel"
    # Also check if it's a multi-media post by looking at the URL pattern
    if info.get("webpage_url", "").count("/p/") > 0 and not info.get("formats"):
        return "carousel"
    fmts = info.get("formats") or []
    # Check vcodec
    has_vid = any((f.get("vcodec") or "none") != "none" for f in fmts if f.get("url"))
    if has_vid:
        return "video"
    # Fallback: if any format has a video extension, treat as video
    video_exts = {"mp4", "mov", "webm", "mkv", "m4v"}
    has_vid_ext = any(f.get("ext", "") in video_exts for f in fmts if f.get("url"))
    if has_vid_ext:
        return "video"
    # Fallback: if top-level url looks like a video
    top_url = info.get("url", "")
    if any(ext in top_url for ext in [".mp4", ".mov", ".webm"]):
        return "video"
    # Fallback: check _type field
    if info.get("_type") == "video":
        return "video"
    return "image"


def _ig_extract_with_rotation(url: str, download: bool = False, extra: dict = {}):
    last_exc = None
    cp = _get_cookie_path("instagram")

    # Each attempt uses different options to work around Instagram's blocks
    strategies = [
        # Strategy 1: allow playlist (Instagram posts are playlists)
        {"noplaylist": False},
        # Strategy 2: different UA
        {
            "noplaylist": False,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
            },
        },
        # Strategy 3: no extractor args
        {"noplaylist": False, "extractor_args": {}},
        # Strategy 4: default fallback
        {"noplaylist": True},
    ]
    for i, strategy_opts in enumerate(strategies):
        current_proxy = _proxy_manager.current()
        try:
            print(f"[IG] Trying strategy {i+1}/4")
            opts = _base_opts(download=download, proxy=current_proxy)
            opts["http_headers"] = {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"
            }
            opts.update(strategy_opts)
            opts.update(extra)
            if cp:
                opts["cookiefile"] = cp
                # opts["cookiesfrombrowser"] = ("chrome",)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
            if info:
                print(f"[IG] ✅ strategy {i+1} worked")
                return info
        except Exception as e:
            msg = str(e)
            last_exc = e
            print(f"[IG] ❌ strategy {i+1}: {msg[:120]}")
            if _is_proxy_error(msg):
                _proxy_manager.rotate()
            if "login" in msg.lower() or "private" in msg.lower():
                raise
            # For carousel posts, "no video formats" is expected on image slides
            # Don't treat this as a fatal error — let instagram_post handle it
            if "no video formats" in msg.lower():
                # Image-only carousel — yt-dlp can't handle these.
                # Return a minimal info dict; instagram_post will call
                # _ig_scrape_carousel_images to get the actual image URLs.
                print(f"[IG] image-only carousel detected — handing off to scraper")
                return {
                    "_type": "playlist",
                    "entries": [],
                    "title": "",
                    "webpage_url": url,
                    "uploader": "",
                }
            continue

    raise last_exc or Exception("All Instagram strategies failed")


# ══════════════════════════════════════════════════════════════════════════════
# SHARED HELPERS
# ══════════════════════════════════════════════════════════════════════════════


def check_auth():
    if not API_SECRET:
        return True
    token = request.headers.get("x-api-secret") or request.args.get("secret")
    return token == API_SECRET


def sanitize(name: str) -> str:
    return (
        "".join(c for c in (name or "") if c.isalnum() or c in " _-").strip() or "media"
    )


def cleanup(path: str):
    def _rm():
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    threading.Thread(target=_rm, daemon=True).start()


def find_file(folder: str, ext: str):
    for f in os.listdir(folder):
        if f.lower().endswith(f".{ext}"):
            return os.path.join(folder, f)
    for f in os.listdir(folder):
        p = os.path.join(folder, f)
        if os.path.isfile(p):
            return p
    return None


def build_formats(info: dict) -> list:
    fmts = info.get("formats") or []
    if not fmts and info.get("url"):
        return [{"quality": "auto", "ext": info.get("ext", "mp4"), "url": info["url"]}]
    out, seen = [], set()
    for f in fmts:
        h, url = f.get("height"), f.get("url")
        if not h or not url:
            continue
        label = f"{h}p"
        if label in seen:
            continue
        seen.add(label)
        out.append({"quality": label, "ext": f.get("ext", "mp4"), "url": url})
    return sorted(out, key=lambda x: int(x["quality"][:-1]), reverse=True)


HEIGHT_MAP = {
    "2160p": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
    "240p": 240,
    "144p": 144,
}

_info_cache: dict[str, tuple[dict, float]] = {}
_INFO_CACHE_TTL = 300  # 5 minutes


def _cached_extract_info(url: str):
    now = time.time()
    cached = _info_cache.get(url)
    if cached and (now - cached[1]) < _INFO_CACHE_TTL:
        print(f"[cache] ✅ hit for {url[:60]}")
        return cached[0]
    info, client = _extract_yt(url, download=False)
    result = {"info": info, "client": client}
    _info_cache[url] = (result, now)
    if len(_info_cache) > 500:
        oldest = sorted(_info_cache, key=lambda k: _info_cache[k][1])[:100]
        for k in oldest:
            _info_cache.pop(k, None)
    return result


def yt_err(msg: str):
    """
    Map a yt-dlp error string to a clean HTTP response.
    Includes a machine-readable 'reason' code so clients can act on it.
    """
    print(f"[ERROR] {msg}")
    m = msg.lower()

    # Bot / auth check — most actionable, so check first
    if _is_bot_check(msg):
        cookie_path = _get_cookie_path("youtube")
        fresh = _check_cookie_freshness(cookie_path) if cookie_path else False
        if not fresh:
            detail = (
                "YouTube cookies are missing or expired. "
                "Re-export youtube_cookies.txt from a logged-in browser session."
            )
        else:
            detail = (
                "YouTube served a bot/consent challenge. "
                "Your cookies may be fresh but your IP is flagged — "
                "consider rotating your proxy or waiting before retrying."
            )
        return (
            jsonify(
                {
                    "error": "YouTube bot / consent check triggered",
                    "reason": "bot_check",
                    "detail": detail,
                }
            ),
            403,
        )

    if "private" in m:
        return jsonify({"error": "Video is private", "reason": "private"}), 403
    if "age" in m and "restrict" in m:
        return (
            jsonify({"error": "Age-restricted video", "reason": "age_restricted"}),
            403,
        )
    if "not available" in m:
        return (
            jsonify({"error": "Not available in this region", "reason": "geo_blocked"}),
            404,
        )
    if "copyright" in m:
        return jsonify({"error": "Blocked by copyright", "reason": "copyright"}), 403
    if "429" in m or "too many requests" in m:
        return (
            jsonify(
                {
                    "error": "Rate limited by YouTube",
                    "reason": "rate_limited",
                    "detail": "Too many requests from this IP. Wait a few minutes or rotate your proxy.",
                }
            ),
            429,
        )
    if "format" in m and "available" in m:
        return (
            jsonify({"error": "No downloadable formats", "reason": "no_formats"}),
            404,
        )

    return jsonify({"error": f"yt-dlp: {msg[:400]}", "reason": "unknown"}), 500


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════════════════════


@app.route("/", methods=["GET"])
def health():
    if not check_auth():
        return jsonify({"status": "ok"}), 200
    node = shutil.which("node")
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        ffmpeg_ok = True
    except Exception:
        ffmpeg_ok = False

    base = os.path.dirname(os.path.abspath(__file__))
    yt_local = os.path.join(base, "youtube_cookies.txt")
    ig_local = os.path.join(base, "instagram_cookies.txt")

    if os.path.exists(yt_local):
        yt_cookie_status = (
            "✅ local (valid)"
            if _check_cookie_freshness(yt_local)
            else "❌ local (EXPIRED — re-export!)"
        )
    else:
        yt_cookie_status = (
            "✅ env" if os.environ.get("YOUTUBE_COOKIES") else "❌ missing"
        )

    # Bot-check failure summary
    bot_summary: dict[str, int] = {}
    with _bot_fail_lock:
        for k, v in _bot_fail_counts.items():
            if v > 0:
                short = os.path.basename(k) if k != "__no_cookies__" else "no-cookies"
                bot_summary[short] = v

    return jsonify(
        {
            "status": "ok",
            "ffmpeg": "✅" if ffmpeg_ok else "❌",
            "node": f"✅ {node}" if node else "❌ not found",
            "yt_client_chain": [c for (c, _, __, ___) in _YT_CLIENT_CHAIN],
            "yt_cookies": yt_cookie_status,
            "ig_cookies": (
                "✅ local"
                if os.path.exists(ig_local)
                else ("✅ env" if os.environ.get("INSTAGRAM_COOKIES") else "❌ missing")
            ),
            "proxy": (
                f"✅ {len(_proxy_manager._proxies)} proxies"
                if _proxy_manager.has_proxies()
                else "➖"
            ),
            "bot_check_failures": bot_summary if bot_summary else "none",
            "endpoints": {
                "youtube_info": "POST /youtube/info",
                "youtube_audio": "POST /youtube/audio",
                "youtube_video": "POST /youtube/video",
                "youtube_shorts": "POST /youtube/shorts",
                "instagram_info": "POST /instagram/info",
                "instagram_post": "POST /instagram/post",
                "instagram_video": "POST /instagram/video",
                "instagram_image": "POST /instagram/image",
                "debug_formats": "POST /youtube/debug",
                "cookie_status": "GET  /youtube/cookie-status",
            },
        }
    )


# ══════════════════════════════════════════════════════════════════════════════
# NEW: /youtube/cookie-status  — operational dashboard endpoint
# ══════════════════════════════════════════════════════════════════════════════


@app.route("/youtube/cookie-status", methods=["GET"])
def youtube_cookie_status():
    """
    Returns a detailed cookie health report so operators can monitor without
    grepping logs.  No auth required (non-sensitive info).
    """
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    cookie_path = _get_cookie_path("youtube")
    if not cookie_path:
        return jsonify(
            {
                "configured": False,
                "fresh": False,
                "message": "No YouTube cookies configured. Set YOUTUBE_COOKIES env var "
                "or place youtube_cookies.txt next to app.py.",
                "action": "Export cookies from Chrome/Firefox using a browser extension "
                "such as 'Get cookies.txt LOCALLY' while logged in to YouTube.",
            }
        )

    fresh = _check_cookie_freshness(cookie_path)
    with _bot_fail_lock:
        key = cookie_path
        fail_count = _bot_fail_counts.get(key, 0)

    # Parse basic stats from the file
    session_found = 0
    session_valid = 0
    earliest_expiry: int | None = None
    now = time.time()

    try:
        with open(cookie_path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) != 7:
                    continue
                domain, _, _, _, expiry_str, name, _ = parts
                if "youtube.com" not in domain and "google.com" not in domain:
                    continue
                if name not in _YT_SESSION_KEYS:
                    continue
                session_found += 1
                try:
                    expiry = int(expiry_str)
                    if expiry == 0 or expiry > now:
                        session_valid += 1
                    if expiry > 0:
                        if earliest_expiry is None or expiry < earliest_expiry:
                            earliest_expiry = expiry
                except ValueError:
                    pass
    except Exception:
        pass

    expiry_str_human = (
        datetime.fromtimestamp(earliest_expiry, tz=timezone.utc).isoformat()
        if earliest_expiry
        else "unknown"
    )

    if fresh and fail_count == 0:
        status = "healthy"
        message = "Cookies look good."
        action = None
    elif fresh and fail_count > 0:
        status = "degraded"
        message = (
            f"Cookies are not expired but YouTube issued {fail_count} bot challenge(s). "
            "Your IP may be flagged."
        )
        action = (
            "Rotate your proxy, or wait a few minutes before retrying. "
            "If failures persist, re-export cookies from a fresh browser session."
        )
    else:
        status = "stale"
        message = "Session cookies are expired or missing."
        action = (
            "Re-export youtube_cookies.txt from Chrome or Firefox while logged in "
            "to YouTube (use the 'Get cookies.txt LOCALLY' extension), then restart "
            "the server or update the YOUTUBE_COOKIES environment variable."
        )

    return jsonify(
        {
            "configured": True,
            "fresh": fresh,
            "status": status,
            "session_keys_found": session_found,
            "session_keys_valid": session_valid,
            "earliest_expiry_utc": expiry_str_human,
            "consecutive_bot_checks": fail_count,
            "message": message,
            **({"action": action} if action else {}),
        }
    )


# ══════════════════════════════════════════════════════════════════════════════
# YOUTUBE ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════


@app.route("/youtube/info", methods=["POST"])
def youtube_info():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    try:
        cached = _cached_extract_info(url)
        info, client = cached["info"], cached["client"]
        thumb = info.get("thumbnail") or ""
        if not thumb and info.get("thumbnails"):
            thumb = sorted(
                info["thumbnails"], key=lambda t: t.get("preference", 0) or 0
            )[-1].get("url", "")
        return jsonify(
            {
                "success": True,
                "client_used": client,
                "videoId": info.get("id", ""),
                "title": info.get("title", ""),
                "author": info.get("uploader") or info.get("channel", ""),
                "thumbnail": thumb,
                "duration": info.get("duration", 0),
                "formats": build_formats(info),
            }
        )
    except yt_dlp.utils.DownloadError as e:
        return yt_err(str(e))
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@app.route("/youtube/audio", methods=["POST"])
def youtube_audio():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    tmp = tempfile.mkdtemp(prefix="vf_audio_")
    try:
        extra = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
        info, _ = _extract_yt(url, extra=extra, download=True)
        f = find_file(tmp, "mp3")
        if not f:
            return jsonify({"error": "MP3 conversion failed — check ffmpeg"}), 500
        print(f"[Audio] ✅ {os.path.getsize(f):,} bytes")
        return send_file(
            f,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=f"{sanitize(info.get('title', 'audio'))}.mp3",
        )
    except yt_dlp.utils.DownloadError as e:
        return yt_err(str(e))
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500
    finally:
        cleanup(tmp)


@app.route("/youtube/video", methods=["POST"])
def youtube_video():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    quality = data.get("quality", "720p").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    h = HEIGHT_MAP.get(quality, 720)
    tmp = tempfile.mkdtemp(prefix="vf_video_")
    try:
        extra = {
            "format": (
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={h}]+bestaudio"
                f"/best[height<={h}]"
                f"/bestvideo+bestaudio/best"
            ),
            "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
            "quiet": True,
            "no_warnings": True,
        }
        info, _ = _extract_yt(url, extra=extra, download=True)
        f = find_file(tmp, "mp4")
        if not f:
            return jsonify({"error": "Download failed"}), 500
        file_size = os.path.getsize(f) / 1024 / 1024
        print(f"[Video] ✅ {file_size:.1f} MB")

        # Stream file instead of loading into memory
        response = send_file(
            open(f, "rb"),
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{sanitize(info.get('title', 'video'))}_{quality}.mp4",
        )
        response.headers["X-Accel-Buffering"] = "no"  # Disable buffering
        response.call_on_close(lambda: cleanup(tmp))
        return response
    except yt_dlp.utils.DownloadError as e:
        return yt_err(str(e))
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@app.route("/youtube/shorts", methods=["POST"])
def youtube_shorts():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    quality = data.get("quality", "720p").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    if "/shorts/" in url:
        url = (
            "https://www.youtube.com/watch?v=" + url.split("/shorts/")[1].split("?")[0]
        )
    h = HEIGHT_MAP.get(quality, 720)
    tmp = tempfile.mkdtemp(prefix="vf_shorts_")
    try:
        extra = {
            "format": (
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={h}]+bestaudio"
                f"/best[height<={h}]/best"
            ),
            "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
        }
        info, _ = _extract_yt(url, extra=extra, download=True)
        f = find_file(tmp, "mp4")
        if not f:
            return jsonify({"error": "Download failed"}), 500
        return send_file(
            f,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{sanitize(info.get('title', 'short'))}_short.mp4",
        )
    except yt_dlp.utils.DownloadError as e:
        return yt_err(str(e))
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500
    finally:
        cleanup(tmp)


# ══════════════════════════════════════════════════════════════════════════════
# INSTAGRAM ENDPOINTS  (unchanged logic, kept intact)
# ══════════════════════════════════════════════════════════════════════════════


@app.route("/instagram/info", methods=["POST"])
def instagram_info():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    try:
        info = _ig_extract_with_rotation(url)
        if not info:
            return jsonify({"error": "No info"}), 404

        post_type = _ig_classify(info)
        entries = info.get("entries") or []

        if post_type == "carousel":
            slides = []
            for i, entry in enumerate(entries):
                entry_fmts = entry.get("formats") or []
                has_vid = any(
                    (f.get("vcodec") or "none") != "none"
                    for f in entry_fmts
                    if f.get("url")
                )
                slides.append(
                    {
                        "index": i,
                        "type": "video" if has_vid else "image",
                        "thumbnail": _ig_best_image_url(entry),
                        "url": entry.get("url", ""),
                    }
                )
            return jsonify(
                {
                    "success": True,
                    "type": "carousel",
                    "slide_count": len(slides),
                    "title": info.get("title")
                    or info.get("description")
                    or "Instagram Post",
                    "author": info.get("uploader") or info.get("channel", ""),
                    "thumbnail": info.get("thumbnail", ""),
                    "slides": slides,
                }
            )

        if post_type == "video":
            fmts = info.get("formats") or []
            formats = []
            for f in fmts:
                if f.get("url") and (f.get("vcodec") or "none") != "none":
                    h = f.get("height") or 0
                    formats.append(
                        {
                            "quality": f"{h}p" if h else "HD",
                            "url": f["url"],
                            "height": h,
                        }
                    )
            formats.sort(key=lambda x: x.get("height", 0), reverse=True)
            if not formats and info.get("url"):
                formats = [{"quality": "HD", "url": info["url"], "height": 0}]
            return jsonify(
                {
                    "success": True,
                    "type": "video",
                    "title": info.get("title")
                    or info.get("description")
                    or "Instagram Post",
                    "author": info.get("uploader") or info.get("channel", ""),
                    "thumbnail": info.get("thumbnail", ""),
                    "duration": info.get("duration", 0),
                    "formats": formats,
                    "defaultUrl": formats[0]["url"] if formats else "",
                }
            )

        img_url = _ig_best_image_url(info)
        return jsonify(
            {
                "success": True,
                "type": "image",
                "title": info.get("title")
                or info.get("description")
                or "Instagram Post",
                "author": info.get("uploader") or info.get("channel", ""),
                "thumbnail": img_url,
                "defaultUrl": img_url,
            }
        )

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "login" in msg.lower() or "private" in msg.lower():
            return jsonify({"error": "Private or login required"}), 403
        return jsonify({"error": msg[:300]}), 500
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500

def _ig_scrape_carousel_images(url: str) -> list[str]:
    """
    Fetch carousel image URLs via Instagram's internal GraphQL API.
    Tries multiple API endpoints in order.
    """
    cp = _get_cookie_path("instagram")
    cookies = {}
    if cp and os.path.exists(cp):
        with open(cp, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) == 7 and "instagram.com" in parts[0]:
                    cookies[parts[5]] = parts[6]

    # Extract shortcode from URL
    shortcode = ""
    for part in url.rstrip("/").split("/"):
        if part and part not in ("www.instagram.com", "instagram.com", "p", "https:", ""):
            shortcode = part
    if not shortcode:
        print("[IG scrape] ❌ Could not extract shortcode from URL")
        return []

    print(f"[IG scrape] shortcode={shortcode}")

    session = req_lib.Session()
    session.cookies.update(cookies)

    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
        "X-IG-App-ID": "936619743392459",
        "X-Requested-With": "XMLHttpRequest",
    }
    session.headers.update(base_headers)

    # ── Method 1: /api/v1/media/{media_id}/info/ ──────────────────────────
    # First convert shortcode → media_id
    def shortcode_to_id(sc: str) -> str:
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        n = 0
        for char in sc:
            n = n * 64 + alphabet.index(char)
        return str(n)

    try:
        media_id = shortcode_to_id(shortcode)
        api_url = f"https://www.instagram.com/api/v1/media/{media_id}/info/"
        r = session.get(api_url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            items = data.get("items") or []
            if items:
                media = items[0]
                carousel = media.get("carousel_media") or []
                if carousel:
                    urls = []
                    for slide in carousel:
                        img_versions = slide.get("image_versions2", {}).get("candidates") or []
                        if img_versions:
                            # Pick highest resolution (first candidate is largest)
                            urls.append(img_versions[0]["url"])
                    if urls:
                        print(f"[IG scrape] ✅ Method 1 (media/info API): {len(urls)} images")
                        return urls
                # Single image post
                img_versions = media.get("image_versions2", {}).get("candidates") or []
                if img_versions:
                    print(f"[IG scrape] ✅ Method 1 (single image): 1 image")
                    return [img_versions[0]["url"]]
        else:
            print(f"[IG scrape] Method 1 status={r.status_code}")
    except Exception as e:
        print(f"[IG scrape] Method 1 failed: {e}")

    # ── Method 2: GraphQL query ───────────────────────────────────────────
    try:
        graphql_url = "https://www.instagram.com/graphql/query/"
        # doc_id for PostPageContainer query
        payload = {
            "doc_id": "8845758582119845",
            "variables": json.dumps({"shortcode": shortcode, "fetch_tagged_user_count": None, "hoisted_comment_id": None, "hoisted_reply_id": None}),
        }
        r = session.post(graphql_url, data=payload, timeout=15)
        if r.status_code == 200:
            data = r.json()
            media = (
                data.get("data", {}).get("xdt_shortcode_media")
                or data.get("data", {}).get("shortcode_media")
                or {}
            )
            edges = media.get("edge_sidecar_to_children", {}).get("edges") or []
            if edges:
                urls = []
                for edge in edges:
                    node = edge.get("node", {})
                    resources = node.get("display_resources") or []
                    if resources:
                        urls.append(resources[-1]["src"])
                    elif node.get("display_url"):
                        urls.append(node["display_url"])
                if urls:
                    print(f"[IG scrape] ✅ Method 2 (GraphQL): {len(urls)} images")
                    return urls
        else:
            print(f"[IG scrape] Method 2 status={r.status_code}")
    except Exception as e:
        print(f"[IG scrape] Method 2 failed: {e}")

    # ── Method 3: ?__a=1 API ─────────────────────────────────────────────
    try:
        api_url = f"https://www.instagram.com/p/{shortcode}/?__a=1&__d=dis"
        r = session.get(api_url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            media = (
                data.get("graphql", {}).get("shortcode_media")
                or (data.get("items") or [{}])[0]
                or {}
            )
            edges = (
                media.get("edge_sidecar_to_children", {}).get("edges")
                or media.get("carousel_media")
                or []
            )
            urls = []
            for edge in edges:
                node = edge.get("node") or edge
                resources = node.get("display_resources") or []
                img_versions = node.get("image_versions2", {}).get("candidates") or []
                if resources:
                    urls.append(resources[-1].get("src", ""))
                elif img_versions:
                    urls.append(img_versions[0].get("url", ""))
                elif node.get("display_url"):
                    urls.append(node["display_url"])
            urls = [u for u in urls if u]
            if urls:
                print(f"[IG scrape] ✅ Method 3 (?__a=1): {len(urls)} images")
                return urls
        else:
            print(f"[IG scrape] Method 3 status={r.status_code}")
    except Exception as e:
        print(f"[IG scrape] Method 3 failed: {e}")

    print("[IG scrape] ❌ All methods failed")
    return []

@app.route("/instagram/post", methods=["POST"])
def instagram_post():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400

    tmp = tempfile.mkdtemp(prefix="vf_igpost_")
    try:
        info = _ig_extract_with_rotation(url)
        if not info:
            return jsonify({"error": "Could not fetch post info"}), 404

        post_type = _ig_classify(info)
        safe_title = sanitize(info.get("title") or info.get("description") or "post")
        print(f"[IG post] type={post_type}  title={safe_title!r}")

        # ── VIDEO ──────────────────────────────────────────────────────────
        if post_type == "video":
            return _ig_download_video_response(url, safe_title, tmp)

        # ── SINGLE IMAGE ───────────────────────────────────────────────────
        if post_type == "image":
            img_url = _ig_best_image_url(info)
            if not img_url:
                return jsonify({"error": "No image URL found"}), 404
            img_bytes, ext = _ig_download_image(img_url)
            mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(
                ext, "image/jpeg"
            )
            return send_file(
                io.BytesIO(img_bytes),
                mimetype=mime,
                as_attachment=True,
                download_name=f"{safe_title}.{ext}",
            )
        # ── CAROUSEL ───────────────────────────────────────────────────────
        if post_type == "carousel":
            zip_buf = io.BytesIO()
            entries = info.get("entries") or []
            files = []

            if entries:
                # Has entries — could be mixed video/image, try yt-dlp first
                cp = _get_cookie_path("instagram")
                dl_opts = {
                    "quiet": True,
                    "verbose": False,
                    "noplaylist": False,
                    "nocheckcertificate": True,
                    "outtmpl": os.path.join(
                        tmp, "%(playlist_index)s_%(title)s.%(ext)s"
                    ),
                    "http_headers": {"User-Agent": _IG_DL_HEADERS["User-Agent"]},
                    "merge_output_format": "mp4",
                    "postprocessors": [
                        {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
                    ],
                }
                if cp:
                    dl_opts["cookiefile"] = cp
                try:
                    with yt_dlp.YoutubeDL(dl_opts) as ydl:
                        ydl.download([url])
                except Exception as e:
                    print(f"[IG carousel] yt-dlp download error: {e}")
                files = sorted(
                    [
                        os.path.join(tmp, f)
                        for f in os.listdir(tmp)
                        if os.path.isfile(os.path.join(tmp, f))
                    ]
                )

            if not files:
                # No entries or yt-dlp failed — use scraper (handles image-only carousels)
                print("[IG carousel] using scraper for images")
                image_urls = _ig_scrape_carousel_images(url)
                for i, img_url in enumerate(image_urls, start=1):
                    try:
                        img_bytes, ext = _ig_download_image(img_url)
                        img_path = os.path.join(tmp, f"{i:02d}_slide.{ext}")
                        with open(img_path, "wb") as fh:
                            fh.write(img_bytes)
                        print(f"[IG carousel] saved slide {i} ({ext})")
                    except Exception as e:
                        print(f"[IG carousel] failed slide {i}: {e}")
                files = sorted(
                    [
                        os.path.join(tmp, f)
                        for f in os.listdir(tmp)
                        if os.path.isfile(os.path.join(tmp, f))
                    ]
                )

            print(f"[IG carousel] total files: {len(files)}")

            if not files:
                return (
                    jsonify({"error": "Carousel download failed — no files produced"}),
                    500,
                )

            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, fpath in enumerate(files, start=1):
                    ext = os.path.splitext(fpath)[1].lstrip(".")
                    with open(fpath, "rb") as fh:
                        zf.writestr(f"slide_{i:02d}.{ext}", fh.read())
                    print(f"[IG carousel] slide {i} ✅ ({ext})")

            zip_buf.seek(0)
            return send_file(
                zip_buf,
                mimetype="application/zip",
                as_attachment=True,
                download_name=f"{safe_title}_carousel.zip",
            )
        return jsonify({"error": f"Unknown post type: {post_type}"}), 500

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "login" in msg.lower() or "private" in msg.lower():
            return jsonify({"error": "Private or login required"}), 403
        return jsonify({"error": msg[:300]}), 500
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500
    finally:
        cleanup(tmp)


def _ig_download_video_response(url: str, safe_title: str, tmp: str):
    extra = {
        "format": "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
        "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
    }
    _ig_extract_with_rotation(url, download=True, extra=extra)
    f = find_file(tmp, "mp4")
    if not f:
        return jsonify({"error": "Download failed — no MP4 produced"}), 500
    print(f"[IG video] ✅ {os.path.getsize(f) / 1024 / 1024:.1f} MB")
    return send_file(
        f,
        mimetype="video/mp4",
        as_attachment=True,
        download_name=f"{safe_title}.mp4",
    )


@app.route("/instagram/video", methods=["POST"])
def instagram_video():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    tmp = tempfile.mkdtemp(prefix="vf_ig_")
    try:
        return _ig_download_video_response(url, "reel", tmp)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "login" in msg.lower() or "private" in msg.lower():
            return jsonify({"error": "Private or login required"}), 403
        return jsonify({"error": msg[:300]}), 500
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500
    finally:
        cleanup(tmp)


@app.route("/instagram/image", methods=["POST"])
def instagram_image():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    tmp = tempfile.mkdtemp(prefix="vf_ig_img_")
    try:
        info = _ig_extract_with_rotation(url)
        if not info:
            return jsonify({"error": "No info"}), 404

        post_type = _ig_classify(info)
        if post_type == "video":
            return (
                jsonify(
                    {"error": "Video post — use /instagram/video or /instagram/post"}
                ),
                400,
            )
        if post_type == "carousel":
            return jsonify({"error": "Carousel post — use /instagram/post"}), 400

        img_url = _ig_best_image_url(info)
        if not img_url:
            return jsonify({"error": "No image found"}), 404

        img_bytes, ext = _ig_download_image(img_url)
        safe = sanitize(info.get("title") or info.get("description") or "post")
        mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(
            ext, "image/jpeg"
        )
        return send_file(
            io.BytesIO(img_bytes),
            mimetype=mime,
            as_attachment=True,
            download_name=f"{safe}.{ext}",
        )
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500
    finally:
        cleanup(tmp)


# ══════════════════════════════════════════════════════════════════════════════
# DEBUG
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/youtube/debug", methods=["POST"])
def youtube_debug():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    results = {}
    for client, skip_protos, use_cookies, ua in _YT_CLIENT_CHAIN:
        try:
            opts = _yt_opts_for_client(client, skip_protos, use_cookies, ua)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            fmts = info.get("formats") or []
            results[client] = {
                "ok": True,
                "total_formats": len(fmts),
                "video_formats": sum(
                    1
                    for f in fmts
                    if f.get("vcodec", "none") != "none" and f.get("url")
                ),
                "formats": [
                    {
                        "id": f.get("format_id"),
                        "ext": f.get("ext"),
                        "height": f.get("height"),
                        "vcodec": f.get("vcodec"),
                        "acodec": f.get("acodec"),
                        "note": f.get("format_note"),
                        "has_url": bool(f.get("url")),
                    }
                    for f in fmts
                ],
            }
        except Exception as e:
            results[client] = {"ok": False, "error": str(e)[:200]}
    return jsonify(results)


# Warm up PO token in background at startup
def _warmup():
    time.sleep(3)  # let gunicorn finish booting
    print("[startup] warming up PO token...")
    token, visitor = _get_po_token()
    if token:
        print(f"[startup] ✅ PO token ready")
    else:
        print(f"[startup] ❌ PO token not available — will retry on first request")

threading.Thread(target=_warmup, daemon=True).start()

# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
