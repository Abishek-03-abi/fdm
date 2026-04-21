"""
FDM v5.0 – Free Download Manager
IDM-style: dynamic multi-segment HTTP + torrent/magnet + yt-dlp streaming
Run:  python server.py
Open: http://localhost:6800
"""

import os, sys, json, time, threading, uuid, math, shutil, subprocess
from pathlib import Path
from urllib.parse import urlparse, unquote
import requests
from flask import Flask, request, jsonify, Response, send_from_directory


def resource_path(relative_path):
    """Get absolute path to resource — works in dev and in PyInstaller bundle."""
    if getattr(sys, 'frozen', False):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent
    return str(base / relative_path)

# ── Optional dependencies ─────────────────────────────────────────────────────
try:
    import yt_dlp
    YTDLP = True
except ImportError:
    YTDLP = False

try:
    import libtorrent as lt
    TORRENT = True
except ImportError:
    TORRENT = False

# ── Config ────────────────────────────────────────────────────────────────────
DOWNLOAD_DIR = Path.home() / "Downloads" / "FDM"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

def _find_ffmpeg():
    """Auto-detect ffmpeg location across macOS, Windows, Linux."""
    # Check if it's on PATH
    ff = shutil.which("ffmpeg")
    if ff:
        return str(Path(ff).parent)
    # Common locations
    candidates = [
        "/opt/homebrew/bin",          # macOS Homebrew (Apple Silicon)
        "/usr/local/bin",             # macOS Homebrew (Intel) / Linux
        "/usr/bin",                   # Linux system
        r"C:\ffmpeg\bin",             # Windows common
        r"C:\Program Files\ffmpeg\bin",
    ]
    for p in candidates:
        if (Path(p) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")).exists():
            return p
    return ""  # not found — yt-dlp will still try PATH

FFMPEG_PATH = _find_ffmpeg()

STREAM_HOSTS = {
    "youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com",
    "music.youtube.com", "vimeo.com", "www.vimeo.com",
    "dailymotion.com", "www.dailymotion.com",
    "twitch.tv", "www.twitch.tv",
    "twitter.com", "x.com",
    "instagram.com", "www.instagram.com",
    "facebook.com", "www.facebook.com",
    "tiktok.com", "www.tiktok.com",
    "reddit.com", "www.reddit.com",
    "soundcloud.com", "www.soundcloud.com",
    "bilibili.com", "www.bilibili.com",
}

app = Flask(__name__, static_folder=resource_path("static"))
app.config["JSON_SORT_KEYS"] = False
downloads: dict = {}
_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
def _now():
    return time.strftime("%d/%m/%y %I:%M %p")

def _is_stream(url: str) -> bool:
    try:
        return urlparse(url).hostname.lower() in STREAM_HOSTS
    except:
        return False

def _is_torrent(url: str) -> bool:
    return url.startswith("magnet:") or url.lower().endswith(".torrent")

# ─────────────────────────────────────────────────────────────────────────────
class Segment:
    def __init__(self, idx, start, end, path):
        self.idx = idx
        self.start = start
        self.end = end
        self.path = Path(path)
        self.downloaded = 0
        self.done = False
        self.active = False


class DownloadTask:
    def __init__(self, url, filename=None, save_dir=None, segments=8):
        self.id         = str(uuid.uuid4())[:8]
        self.url        = url
        self.save_dir   = Path(save_dir) if save_dir else DOWNLOAD_DIR
        self.save_dir.mkdir(parents=True, exist_ok=True)

        # Detect type
        self.is_torrent = _is_torrent(url)
        self.is_stream  = (not self.is_torrent) and _is_stream(url)
        self.is_direct  = not self.is_torrent and not self.is_stream

        # Filename
        if filename:
            self.filename = filename
        elif self.is_torrent and url.startswith("magnet:"):
            self.filename = "Resolving torrent…"
        elif self.is_torrent:
            self.filename = unquote(urlparse(url).path).split("/")[-1]
        elif self.is_stream:
            self.filename = "Fetching title…"
        else:
            raw = unquote(urlparse(url).path).split("/")[-1]
            self.filename = raw or "download.bin"

        self.final_path = self.save_dir / self.filename
        self.max_segs   = segments

        # Stats
        self.total      = 0
        self.downloaded = 0
        self.status     = "queued"
        self.error      = ""
        self.added      = _now()
        self.speed      = 0
        self.conns      = 1
        self.segs: list[Segment] = []
        self.torrent_info_dict = {}   # extra torrent metadata

        # Control
        self._stop   = threading.Event()
        self._pause  = threading.Event()
        self._thread = None
        self._samples: list = []

    # ── Serialise ─────────────────────────────────────────────────────────────
    def to_dict(self):
        pct = round(self.downloaded / self.total * 100, 1) if self.total > 0 else 0
        ext = ""
        if "." in self.filename:
            ext = self.filename.rsplit(".", 1)[-1].lower()
        kind = ("torrent" if self.is_torrent
                else "stream" if self.is_stream
                else "direct")
        return {
            "id":          self.id,
            "filename":    self.filename,
            "url":         self.url,
            "total_size":  self.total,
            "downloaded":  self.downloaded,
            "pct":         pct,
            "status":      self.status,
            "speed":       self.speed,
            "eta":         self._eta(),
            "connections": self.conns,
            "added":       self.added,
            "save_path":   str(self.final_path),
            "ext":         ext,
            "error_msg":   self.error,
            "kind":        kind,
        }

    def _eta(self):
        if self.status != "downloading" or self.speed == 0 or self.total == 0:
            return "—"
        secs = int((self.total - self.downloaded) / self.speed)
        if secs < 60:   return f"{secs}s"
        if secs < 3600: return f"{secs//60}m {secs%60}s"
        return f"{secs//3600}h {(secs%3600)//60}m"

    def _tick(self, n):
        now = time.time()
        self._samples.append((now, n))
        self._samples = [(t, b) for t, b in self._samples if t >= now - 3]
        total   = sum(b for _, b in self._samples)
        elapsed = (now - self._samples[0][0]) if len(self._samples) > 1 else 1
        self.speed = int(total / elapsed) if elapsed > 0 else 0

    # ── Control ───────────────────────────────────────────────────────────────
    def start(self):
        self._stop.clear()
        self._pause.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self):
        if self.is_torrent or self.is_stream:
            # yt-dlp / libtorrent: just stop and re-start later
            self._stop.set()
        else:
            self._pause.set()
        self.status = "paused"
        self.speed  = 0

    def resume(self):
        if self.status != "paused":
            return
        self._stop.clear()
        self._pause.clear()
        self.status  = "queued"
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._pause.set()
        self.status = "error"
        self.error  = "Stopped by user"
        self.speed  = 0

    # ── Dispatch ──────────────────────────────────────────────────────────────
    def _run(self):
        try:
            self.status = "connecting"
            self.error  = ""
            if self.is_torrent:
                self._run_torrent()
            elif self.is_stream:
                self._run_ytdlp()
            else:
                self._run_direct()
        except Exception as e:
            if self.status not in ("paused", "complete"):
                self.status = "error"
                self.error  = str(e)
                self.speed  = 0

    # ═════════════════════════════════════════════════════════════════════════
    #  TORRENT / MAGNET
    # ═════════════════════════════════════════════════════════════════════════
    def _run_torrent(self):
        if not TORRENT:
            self.status = "error"
            self.error  = "libtorrent not installed. Run: conda install -c conda-forge libtorrent"
            return

        ses = lt.session()
        ses.listen_on(6881, 6891)
        settings = ses.get_settings()
        settings["active_downloads"] = 8
        ses.apply_settings(settings)

        params = {
            "save_path": str(self.save_dir),
            "storage_mode": lt.storage_mode_t.storage_mode_sparse,
        }

        local_tmp = getattr(self, "_torrent_tmp", None)

        if self.url.startswith("magnet:"):
            magnet_params = lt.parse_magnet_uri(self.url)
            magnet_params.save_path = str(self.save_dir)
            handle = ses.add_torrent(magnet_params)
            self.status   = "connecting"
            self.filename = "Resolving metadata…"
            timeout = 60
            while not handle.has_metadata() and timeout > 0:
                if self._stop.is_set():
                    ses.remove_torrent(handle)
                    return
                time.sleep(1)
                timeout -= 1
            if not handle.has_metadata():
                self.status = "error"
                self.error  = "Magnet: metadata timeout (60s)"
                ses.remove_torrent(handle)
                return
        elif local_tmp and Path(local_tmp).exists():
            # Local .torrent file uploaded from browser
            try:
                ti = lt.torrent_info(local_tmp)
                params["ti"] = ti
                handle = ses.add_torrent(params)
            except Exception as e:
                self.status = "error"
                self.error  = f"Could not load .torrent file: {e}"
                return
        else:
            # .torrent URL → fetch and parse
            try:
                r = requests.get(self.url, timeout=15,
                                 headers={"User-Agent": "Mozilla/5.0 FDM/5.0"})
                ti = lt.torrent_info(lt.bdecode(r.content))
                params["ti"] = ti
                handle = ses.add_torrent(params)
            except Exception as e:
                self.status = "error"
                self.error  = f"Could not load .torrent: {e}"
                return

        ti = handle.torrent_file()
        self.filename   = ti.name() if ti else handle.name()
        self.final_path = self.save_dir / self.filename
        self.total      = ti.total_size() if ti else 0
        self.conns      = 8
        self.status     = "downloading"

        while not handle.is_seed():
            if self._stop.is_set():
                ses.remove_torrent(handle)
                return
            while self._pause.is_set() and not self._stop.is_set():
                time.sleep(0.5)

            s = handle.status()
            self.downloaded = s.total_done
            self.total      = s.total_wanted or self.total
            self.speed      = int(s.download_rate)
            self.conns      = s.num_peers
            time.sleep(0.5)

        ses.remove_torrent(handle)
        self.status     = "complete"
        self.speed      = 0
        self.downloaded = self.total

    # ═════════════════════════════════════════════════════════════════════════
    #  STREAMING (yt-dlp)
    # ═════════════════════════════════════════════════════════════════════════
    def _run_ytdlp(self):
        if not YTDLP:
            self.status = "error"
            self.error  = "yt-dlp not installed. Run: pip install yt-dlp"
            return

        task = self
        safe_out = str(self.save_dir / f"fdm_{self.id}.%(ext)s")

        class YTLogger:
            def debug(self, m):   pass
            def warning(self, m): pass
            def error(self, m):   task.error = m

        def hook(d):
            if task._stop.is_set():
                raise yt_dlp.utils.DownloadCancelled()
            st = d.get("status")
            if st == "downloading":
                task.status    = "downloading"
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                dl    = d.get("downloaded_bytes", 0)
                spd   = d.get("speed") or 0
                if total:
                    task.total = total
                delta = dl - task.downloaded
                if delta > 0:
                    task.downloaded = dl
                    task._tick(delta)
                if spd:
                    task.speed = int(spd)
            elif st == "finished":
                task.status = "merging"
                task.speed  = 0

        opts = {
            "outtmpl":             safe_out,
            "format":              "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "ffmpeg_location":     FFMPEG_PATH,
            "noplaylist":          True,
            "progress_hooks":      [hook],
            "logger":              YTLogger(),
            "quiet":               True,
            "no_warnings":         True,
            "postprocessors": [
                {
                    "key":             "FFmpegVideoConvertor",
                    "preferedformat":  "mp4",
                }
            ],
        }

        info = None
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(task.url, download=True)
        except yt_dlp.utils.DownloadCancelled:
            return
        except Exception as e:
            if task.status not in ("paused",):
                task.status = "error"
                task.error  = str(e)
            return

        if task._stop.is_set():
            return

        # Locate the output file (handles any suffix yt-dlp may add)
        found = None
        for f in sorted(self.save_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if (f.name.startswith(f"fdm_{self.id}")
                    and not f.name.endswith(".part")
                    and f.is_file()
                    and f.stat().st_size > 10_000):
                found = f
                break

        if not found:
            task.status = "error"
            task.error  = "Output file not found — ffmpeg merge may have failed. Check ffmpeg is installed."
            return

        # Clean title rename
        title      = (info or {}).get("title", self.id)
        safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip() or self.id
        dest       = self.save_dir / f"{safe_title}.mp4"
        n = 1
        while dest.exists() and dest != found:
            dest = self.save_dir / f"{safe_title}_{n}.mp4"
            n   += 1

        if found != dest:
            found.rename(dest)

        task.final_path  = dest
        task.filename    = dest.name
        task.downloaded  = dest.stat().st_size
        task.total       = task.downloaded
        task.status      = "complete"
        task.speed       = 0

    # ═════════════════════════════════════════════════════════════════════════
    #  DIRECT HTTP — IDM-style dynamic multi-segment
    # ═════════════════════════════════════════════════════════════════════════
    def _run_direct(self):
        hdrs = {"User-Agent": "Mozilla/5.0 FDM/5.0"}
        head = requests.head(self.url, timeout=12, allow_redirects=True, headers=hdrs)

        clen   = int(head.headers.get("Content-Length", 0))
        ranged = head.headers.get("Accept-Ranges", "none").lower() == "bytes"
        self.total = clen

        # Content-Disposition filename override
        cd = head.headers.get("Content-Disposition", "")
        if "filename=" in cd:
            fn = cd.split("filename=")[-1].strip().strip('"').strip().lstrip("/\\")
            fn = Path(fn).name  # take only the basename — never a path
            if fn:
                self.filename   = fn
                self.final_path = self.save_dir / self.filename
        # Also sanitize the filename derived from URL (could be a path component)
        self.filename   = Path(self.filename).name or "download.bin"
        self.final_path = self.save_dir / self.filename

        use_segs = ranged and clen > 500_000 and self.max_segs > 1
        n        = self.max_segs if use_segs else 1
        self.conns = n

        if self._stop.is_set():
            return

        self.status = "downloading"
        tmp = self.save_dir / f".fdm_{self.id}"
        tmp.mkdir(exist_ok=True)

        if use_segs:
            seg_sz = math.ceil(clen / n)
            segs = [
                Segment(i, i * seg_sz, min((i + 1) * seg_sz - 1, clen - 1),
                        tmp / f"s{i}.tmp")
                for i in range(n)
            ]
            self.segs = segs

            threads = [threading.Thread(target=self._dl_seg, args=(s,), daemon=True)
                       for s in segs]
            for t in threads: t.start()

            # Dynamic reallocation: watch for slow segments, reassign
            threading.Thread(target=self._watchdog, args=(segs,), daemon=True).start()

            for t in threads: t.join()
        else:
            seg = Segment(0, 0, -1, tmp / "s0.tmp")
            self.segs = [seg]
            self._dl_single(seg)

        if self._stop.is_set() or self._pause.is_set():
            return

        self.status = "merging"
        with open(self.final_path, "wb") as out:
            for s in self.segs:
                if s.path.exists():
                    out.write(s.path.read_bytes())

        shutil.rmtree(tmp, ignore_errors=True)
        self.status     = "complete"
        self.speed      = 0
        self.downloaded = self.total if self.total else self.downloaded

    def _watchdog(self, segs):
        """Dynamic reallocation: if a segment stalls, steal its tail for a new thread."""
        time.sleep(5)
        while not self._stop.is_set() and not self._pause.is_set():
            time.sleep(3)
            slow = [s for s in segs if s.active and not s.done
                    and s.downloaded < (s.end - s.start) * 0.1]
            for s in slow:
                remaining = s.end - (s.start + s.downloaded)
                if remaining > 1_000_000:
                    # Split the remaining half
                    split_at  = s.start + s.downloaded + remaining // 2
                    old_end   = s.end
                    s.end     = split_at - 1
                    new_seg   = Segment(
                        len(segs), split_at, old_end,
                        s.path.parent / f"s{len(segs)}.tmp"
                    )
                    segs.append(new_seg)
                    t = threading.Thread(target=self._dl_seg, args=(new_seg,), daemon=True)
                    t.start()

    def _safe_get(self, hdrs, stream=True, timeout=60):
        """requests.get with automatic SSL-verify fallback and retries."""
        for attempt in range(4):
            if self._stop.is_set():
                return None
            try:
                verify = attempt < 2   # first 2 attempts with SSL verify, then without
                return requests.get(self.url, headers=hdrs, stream=stream,
                                    timeout=timeout, verify=verify)
            except (requests.exceptions.SSLError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ReadTimeout):
                if attempt == 3:
                    raise
                time.sleep(1.5 * (attempt + 1))
        return None

    def _dl_single(self, seg):
        hdrs = {"User-Agent": "Mozilla/5.0 FDM/5.0"}
        ex   = seg.path.stat().st_size if seg.path.exists() else 0
        if ex:
            hdrs["Range"] = f"bytes={ex}-"
        seg.active = True
        try:
            resp = self._safe_get(hdrs)
            if not resp:
                return
            mode = "ab" if ex else "wb"
            with open(seg.path, mode) as f:
                for chunk in resp.iter_content(131072):
                    if self._stop.is_set(): return
                    while self._pause.is_set() and not self._stop.is_set():
                        time.sleep(0.2)
                    if chunk:
                        f.write(chunk)
                        seg.downloaded  += len(chunk)
                        self.downloaded += len(chunk)
                        self._tick(len(chunk))
            seg.done = True
        finally:
            seg.active = False

    def _dl_seg(self, seg):
        ex = seg.path.stat().st_size if seg.path.exists() else 0
        hdrs = {
            "User-Agent": "Mozilla/5.0 FDM/5.0",
            "Range":      f"bytes={seg.start + ex}-{seg.end}",
        }
        seg.active = True
        try:
            resp = self._safe_get(hdrs)
            if not resp:
                return
            mode = "ab" if ex else "wb"
            with open(seg.path, mode) as f:
                for chunk in resp.iter_content(131072):
                    if self._stop.is_set(): return
                    while self._pause.is_set() and not self._stop.is_set():
                        time.sleep(0.2)
                    if chunk:
                        f.write(chunk)
                        seg.downloaded  += len(chunk)
                        self.downloaded += len(chunk)
                        self._tick(len(chunk))
            seg.done = True
        finally:
            seg.active = False


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/downloads", methods=["GET"])
def list_downloads():
    with _lock:
        return jsonify([d.to_dict() for d in downloads.values()])

@app.route("/api/downloads", methods=["POST"])
def add_download():
    data = request.json or {}
    url  = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    task = DownloadTask(
        url,
        filename = data.get("filename") or None,
        save_dir = data.get("save_dir") or None,
        segments = int(data.get("segments", 8)),
    )
    with _lock:
        downloads[task.id] = task
    if data.get("start", True):
        task.start()
    return jsonify(task.to_dict()), 201

@app.route("/api/torrent/upload", methods=["POST"])
def upload_torrent():
    """Accept a local .torrent file uploaded from the browser."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".torrent"):
        return jsonify({"error": "File must be a .torrent file"}), 400

    torrent_bytes = f.read()

    # Save torrent bytes to a temp file so libtorrent can read it
    tmp_dir = DOWNLOAD_DIR / ".fdm_torrents"
    tmp_dir.mkdir(exist_ok=True)
    tmp_id   = str(uuid.uuid4())[:8]
    tmp_path = tmp_dir / f"{tmp_id}.torrent"
    tmp_path.write_bytes(torrent_bytes)

    # Create task using a special "file://" url carrying the temp path
    task = DownloadTask(
        f"file://{tmp_path}",
        filename = None,
        save_dir = None,
        segments = 8,
    )
    task.is_torrent = True
    task.is_stream  = False
    task.is_direct  = False
    task.filename   = f.filename.replace(".torrent", "")
    task._torrent_tmp = str(tmp_path)   # pass path to _run_torrent

    with _lock:
        downloads[task.id] = task
    task.start()
    return jsonify(task.to_dict()), 201

@app.route("/api/downloads/<did>",          methods=["GET"])
def get_dl(did):
    with _lock: t = downloads.get(did)
    return jsonify(t.to_dict()) if t else (jsonify({"error": "not found"}), 404)

@app.route("/api/downloads/<did>/pause",    methods=["POST"])
def pause_dl(did):
    with _lock: t = downloads.get(did)
    if t: t.pause()
    return jsonify({"ok": True})

@app.route("/api/downloads/<did>/resume",   methods=["POST"])
def resume_dl(did):
    with _lock: t = downloads.get(did)
    if t: t.resume()
    return jsonify({"ok": True})

@app.route("/api/downloads/<did>/stop",     methods=["POST"])
def stop_dl(did):
    with _lock: t = downloads.get(did)
    if t: t.stop()
    return jsonify({"ok": True})

@app.route("/api/downloads/<did>",          methods=["DELETE"])
def delete_dl(did):
    with _lock: t = downloads.pop(did, None)
    if t: t.stop()
    return jsonify({"ok": True})

@app.route("/api/downloads/pause_all",      methods=["POST"])
def pause_all():
    with _lock: tasks = list(downloads.values())
    for t in tasks:
        if t.status == "downloading": t.pause()
    return jsonify({"ok": True})

@app.route("/api/downloads/resume_all",     methods=["POST"])
def resume_all():
    with _lock: tasks = list(downloads.values())
    for t in tasks:
        if t.status == "paused": t.resume()
    return jsonify({"ok": True})

@app.route("/api/stats")
def stats():
    with _lock: tasks = list(downloads.values())
    return jsonify({
        "total":        len(tasks),
        "active":       sum(1 for t in tasks if t.status == "downloading"),
        "total_speed":  sum(t.speed for t in tasks if t.status == "downloading"),
        "download_dir": str(DOWNLOAD_DIR),
        "ytdlp":        YTDLP,
        "torrent":      TORRENT,
        "ffmpeg":       FFMPEG_PATH,
    })

@app.route("/api/stream")
def stream():
    def gen():
        while True:
            with _lock:
                data = [d.to_dict() for d in downloads.values()]
            yield f"data: {json.dumps(data)}\n\n"
            time.sleep(0.5)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  FDM v5.0 – Free Download Manager")
    print(f"  Open     → http://localhost:6800")
    print(f"  Files    → {DOWNLOAD_DIR}")
    print(f"  yt-dlp   : {'✓' if YTDLP    else '✗  pip install yt-dlp'}")
    print(f"  Torrent  : {'✓' if TORRENT  else '✗  pip install python-libtorrent'}")
    print(f"  ffmpeg   : {FFMPEG_PATH}/ffmpeg")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=6800, debug=False, threaded=True)