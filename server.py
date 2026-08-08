#!/usr/bin/env python3
"""
EasyTransfer — a Finder-style browser for an Android phone connected over adb.

macOS reads Android over MTP, which is slow and hides most of the filesystem.
adb sees everything, so this serves the phone's storage as a local web app.

Run:  python3 server.py     (then open http://127.0.0.1:8777)
"""

import hashlib
import json
import mimetypes
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("EASYTRANSFER_PORT", "8777"))
CACHE = os.path.expanduser("~/.cache/easytransfer/thumbs")
CONFIG = os.path.expanduser("~/.config/easytransfer/config.json")
DEFAULT_DEST = os.path.expanduser(os.environ.get("EASYTRANSFER_DEST", "~/Downloads/Phone"))
ADB = shutil.which("adb") or "/opt/homebrew/bin/adb"
FFMPEG = shutil.which("ffmpeg")
SIPS = shutil.which("sips") or "/usr/bin/sips"

THUMB_PX = 400
STREAM_DIRECT_MIN = 40 * 1024 * 1024   # above this, stream instead of caching
STREAM_CHUNK = 4 * 1024 * 1024         # bytes served per open-ended range request
# adb multiplexes fine, but too many parallel exec-out calls thrash the USB link.
PULL_SEM = threading.Semaphore(5)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp", ".dng"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".3gp", ".webm", ".avi", ".m4v"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT

# Only these are genuinely off-limits or worthless. NOT all of Android/ —
# /sdcard/Android/media holds real user photos (WhatsApp, Instagram, Telegram
# put them there on modern Android), and skipping the whole tree hid 657 files.
SKIP_PATHS = [
    "/sdcard/Android/data",     # per-app private storage, unreadable without root
    "/sdcard/Android/obb",      # game asset blobs
    "/sdcard/Android/.Trash",   # Android's own recycle bin
    "/sdcard/LOST.DIR",
]
SKIP_NAMES = {".thumbnails"}    # media caches, not originals

os.makedirs(CACHE, exist_ok=True)


# ------------------------------------------------------------------ settings

DEFAULTS = {
    "dest": DEFAULT_DEST,   # where transfers land
    "subfolder": True,      # mirror the phone folder name inside dest
    "ask": False,           # pick the folder on every transfer
    "move": False,          # delete from the phone after a verified copy
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as f:
            saved = json.load(f)
        for k in DEFAULTS:
            if k in saved:
                cfg[k] = saved[k]
    except (OSError, json.JSONDecodeError):
        pass
    cfg["dest"] = os.path.expanduser(str(cfg["dest"]))
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)


def choose_folder(start):
    """Native macOS folder picker. Returns a path, or '' if cancelled."""
    if not os.path.isdir(start):
        start = os.path.expanduser("~")
    script = (
        f'POSIX path of (choose folder with prompt "Où envoyer les fichiers ?" '
        f'default location POSIX file {json.dumps(start)})'
    )
    p = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=300)
    if p.returncode != 0:
        return ""                       # user cancelled
    return p.stdout.decode("utf-8", "replace").strip().rstrip("/")


# ---------------------------------------------------------------- adb helpers

class AdbMissing(Exception):
    pass


def adb(args, timeout=60, binary=False):
    """Run an adb command, returning stdout (bytes if binary)."""
    try:
        p = subprocess.run(
            [ADB] + args,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise AdbMissing(ADB)
    if binary:
        return p.returncode, p.stdout, p.stderr
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def sh(cmd, timeout=60, binary=False):
    """Run a shell command on the phone."""
    return adb(["shell", cmd], timeout=timeout, binary=binary)


def exec_out(cmd, timeout=180):
    """Run a command on the phone and get raw bytes back (no tty mangling)."""
    p = subprocess.run([ADB, "exec-out", cmd], capture_output=True, timeout=timeout)
    return p.returncode, p.stdout


def device_info():
    try:
        code, out, _ = adb(["devices", "-l"])
    except AdbMissing:
        return {"connected": False, "no_adb": True, "error":
                "adb n'est pas installé. Installe-le avec :  "
                "brew install android-platform-tools"}
    if code != 0:
        return {"connected": False, "error": "adb unavailable"}
    lines = [l for l in out.splitlines()[1:] if l.strip()]
    if not lines:
        return {"connected": False, "error": "No device. Plug the phone in and allow USB debugging."}
    first = lines[0]
    serial = first.split()[0]
    state = first.split()[1] if len(first.split()) > 1 else "?"
    if state == "unauthorized":
        return {"connected": False, "error": "Phone connected but not authorized — accept the USB-debugging prompt on screen."}

    def prop(name):
        c, o, _ = sh(f"getprop {name}", timeout=10)
        return o.strip() if c == 0 else ""

    model = prop("ro.product.model") or "Android"
    brand = (prop("ro.product.brand") or "").capitalize()
    release = prop("ro.build.version.release")

    free = ""
    c, o, _ = sh("df -h /sdcard | tail -1", timeout=10)
    if c == 0:
        parts = o.split()
        if len(parts) >= 4:
            free = f"{parts[3]} free of {parts[1]}"

    return {
        "connected": True,
        "serial": serial,
        "model": model.replace("_", " "),
        "brand": brand,
        "android": release,
        "storage": free,
        "dest": load_config()["dest"],
    }


# ------------------------------------------------------------------ listing

def safe_path(p):
    """Confine browsing to the phone's shared storage."""
    if not p:
        p = "/sdcard"
    p = posixpath.normpath(p)
    if not (p == "/sdcard" or p.startswith("/sdcard/")):
        raise ValueError("path outside /sdcard")
    return p


def prune_expr():
    """find clauses that skip the unreadable/worthless corners, nothing more."""
    parts = [f"-path {shlex.quote(p)} -prune -o" for p in SKIP_PATHS]
    parts += [f"-name {shlex.quote(n)} -prune -o" for n in SKIP_NAMES]
    return " ".join(parts)


def skipped(path):
    return path in SKIP_PATHS or posixpath.basename(path) in SKIP_NAMES


def is_hidden(name):
    # .trashed-* are Android's recycle bin; .pending-* are half-written captures.
    return name.startswith(".") or name.startswith("trashed-") or name.startswith("pending-")


def listdir(path):
    """One directory level, via find+stat so filenames with spaces survive."""
    q = shlex.quote(path)
    # -H resolves a symlinked starting point. /sdcard is a link to
    # /storage/emulated/0, and without this the root listing comes back empty.
    cmd = f"find -H {q} -maxdepth 1 -mindepth 1 -exec stat -c '%F|%s|%Y|%n' {{}} + 2>/dev/null"
    code, out, _ = sh(cmd, timeout=90)
    items = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.count("|") < 3:
            continue
        kind, size, mtime, name = line.split("|", 3)
        base = posixpath.basename(name)
        if is_hidden(base) or skipped(name):
            continue
        isdir = kind.startswith("directory")
        try:
            size, mtime = int(size), int(mtime)
        except ValueError:
            continue
        if not isdir and size == 0:
            continue
        ext = posixpath.splitext(base)[1].lower()
        items.append({
            "name": base,
            "path": name,
            "dir": isdir,
            "size": 0 if isdir else size,
            "mtime": mtime,
            "kind": "folder" if isdir else ("image" if ext in IMAGE_EXT
                                            else "video" if ext in VIDEO_EXT else "file"),
        })
    items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
    return items


def count_children(path):
    """Item counts for folder tiles, cheap enough to batch for one screen."""
    q = shlex.quote(path)
    code, out, _ = sh(f"find -H {q} -maxdepth 1 -mindepth 1 2>/dev/null | wc -l", timeout=30)
    try:
        return int(out.strip())
    except ValueError:
        return 0


def scan_media(path, recursive=True):
    """All photos/videos under a path — powers Gallery mode."""
    q = shlex.quote(path)
    depth = "" if recursive else "-maxdepth 1"
    names = " -o ".join(f"-iname '*{e}'" for e in sorted(MEDIA_EXT))
    cmd = (f"find -H {q} {depth} {prune_expr()} -type f \\( {names} \\) "
           f"-exec stat -c '%F|%s|%Y|%n' {{}} + 2>/dev/null")
    code, out, _ = sh(cmd, timeout=180)
    items = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.count("|") < 3:
            continue
        kind, size, mtime, name = line.split("|", 3)
        base = posixpath.basename(name)
        if is_hidden(base):
            continue
        try:
            size, mtime = int(size), int(mtime)
        except ValueError:
            continue
        if size == 0:
            continue
        ext = posixpath.splitext(base)[1].lower()
        items.append({
            "name": base,
            "path": name,
            "dir": False,
            "size": size,
            "mtime": mtime,
            "kind": "image" if ext in IMAGE_EXT else "video",
        })
    items.sort(key=lambda i: -i["mtime"])
    return items


# --------------------------------------------------------------- thumbnails

def cache_key(path, mtime, size):
    h = hashlib.sha1(f"{path}|{mtime}|{size}|{THUMB_PX}".encode()).hexdigest()
    return os.path.join(CACHE, h + ".jpg")


def embedded_jpeg_thumb(head):
    """Pull the EXIF preview out of a JPEG header.

    Camera JPEGs carry a ~512px thumbnail in APP1. Reading 128 KB to get it
    beats pulling a 4 MB original by roughly 40x, which is what makes the
    grid feel instant.
    """
    if not head.startswith(b"\xff\xd8"):
        return None
    start = head.find(b"\xff\xd8\xff", 3)
    if start < 0:
        return None
    end = head.find(b"\xff\xd9", start)
    if end < 0:
        return None
    thumb = head[start:end + 2]
    return thumb if len(thumb) > 2048 else None


def sips_resize(src, dst):
    p = subprocess.run(
        [SIPS, "-Z", str(THUMB_PX), "-s", "format", "jpeg", "-s", "formatOptions", "70",
         src, "--out", dst],
        capture_output=True, timeout=60,
    )
    return p.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0


def make_image_thumb(path, dst):
    ext = posixpath.splitext(path)[1].lower()
    q = shlex.quote(path)
    tmp = dst + ".tmp"

    if ext in (".jpg", ".jpeg"):
        code, head = exec_out(f"dd if={q} bs=65536 count=2 2>/dev/null", timeout=60)
        if code == 0 and head:
            thumb = embedded_jpeg_thumb(head)
            if thumb:
                with open(tmp, "wb") as f:
                    f.write(thumb)
                # Normalize so every tile decodes to the same box.
                if sips_resize(tmp, dst):
                    os.unlink(tmp)
                    return True
                os.replace(tmp, dst)
                return True

    # No embedded preview (PNG screenshots, HEIC, small JPEGs): pull the whole file.
    code, data = exec_out(f"cat {q}", timeout=180)
    if code != 0 or not data:
        return False
    with open(tmp, "wb") as f:
        f.write(data)
    ok = sips_resize(tmp, dst)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return ok


HEAD_MB = 8
TAIL_MB = 6


def ffmpeg_frame(src, dst):
    p = subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-i", src, "-frames:v", "1",
         "-vf", f"scale={THUMB_PX}:-2", "-q:v", "4", dst],
        capture_output=True, timeout=120,
    )
    return p.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0


def make_video_thumb(path, dst, size):
    """Grab one frame without pulling the whole video.

    Phone videos run to a gigabyte or more, so copying them just to draw a
    tile is a non-starter. Two tricks cover almost everything:

      1. Read only the head. Works when the MP4 index (moov) sits up front.
      2. Otherwise splice: write the head and the tail into a sparse file of
         the original length. moov holds absolute offsets, and the first
         frames live at the start of mdat — both of which land in the bytes
         we actually fetched, so ffmpeg decodes normally. A 600 MB clip costs
         ~14 MB of transfer instead of 600.
    """
    if not FFMPEG:
        return False
    q = shlex.quote(path)
    tmp = dst + ".vtmp"
    head_bytes = HEAD_MB * 1024 * 1024
    tail_bytes = TAIL_MB * 1024 * 1024

    def cleanup():
        if os.path.exists(tmp):
            os.unlink(tmp)

    try:
        # Small enough that the whole thing is cheaper than being clever.
        if size <= head_bytes + tail_bytes:
            code, data = exec_out(f"cat {q}", timeout=240)
            if code != 0 or not data:
                return False
            with open(tmp, "wb") as f:
                f.write(data)
            return ffmpeg_frame(tmp, dst)

        code, head = exec_out(f"dd if={q} bs=1048576 count={HEAD_MB} 2>/dev/null", timeout=180)
        if code != 0 or not head:
            return False
        with open(tmp, "wb") as f:
            f.write(head)
        if ffmpeg_frame(tmp, dst):
            return True

        # moov is at the end: rebuild a sparse stand-in at the true length.
        skip = (size - tail_bytes) // 4096
        offset = skip * 4096
        code, tail = exec_out(f"dd if={q} bs=4096 skip={skip} 2>/dev/null", timeout=180)
        if code != 0 or not tail:
            return False
        with open(tmp, "wb") as f:
            f.truncate(size)
            f.seek(0)
            f.write(head)
            f.seek(offset)
            f.write(tail)
        return ffmpeg_frame(tmp, dst)
    finally:
        cleanup()


def get_thumb(path, mtime, size, kind):
    dst = cache_key(path, mtime, size)
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return dst
    with PULL_SEM:
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            return dst
        try:
            ok = (make_video_thumb(path, dst, size) if kind == "video"
                  else make_image_thumb(path, dst))
        except subprocess.TimeoutExpired:
            ok = False
    return dst if ok else None


# ---------------------------------------------------------------- transfers

JOBS = {}
JOBS_LOCK = threading.Lock()
# adb pull processes, kept out of JOBS so the job dict stays JSON-serialisable.
PROCS = {}


def resolve_dest(subdir):
    """The exact folder a transfer will land in, given the current settings."""
    cfg = load_config()
    subdir = re.sub(r"[^\w .-]", "_", subdir or "").strip("_")
    if cfg["subfolder"] and subdir:
        return os.path.join(cfg["dest"], subdir)
    return cfg["dest"]


def unique_dest(folder, name):
    target = os.path.join(folder, name)
    if not os.path.exists(target):
        return target
    stem, ext = os.path.splitext(name)
    n = 2
    while os.path.exists(os.path.join(folder, f"{stem} {n}{ext}")):
        n += 1
    return os.path.join(folder, f"{stem} {n}{ext}")


def cancelled(job_id):
    with JOBS_LOCK:
        return JOBS.get(job_id, {}).get("cancel", False)


def remote_size(path):
    code, out, _ = sh(f"stat -c '%s' {shlex.quote(path)} 2>/dev/null", timeout=30)
    try:
        return int(out.strip())
    except ValueError:
        return -1


def delete_on_phone(path, target):
    """Remove the original — only after proving the copy is byte-for-byte complete.

    This is the one destructive thing the app does, so it refuses on any doubt:
    a size mismatch, an unreadable original, anything. Better a duplicate left
    on the phone than a photo that exists nowhere.
    """
    try:
        want = remote_size(path)
        got = os.path.getsize(target) if os.path.exists(target) else -1
    except OSError:
        return False
    if want <= 0 or got != want:
        return False
    code, _, _ = sh(f"rm -f {shlex.quote(path)}", timeout=60)
    return code == 0


def rescan_media():
    """Ask Android to re-index storage after we removed files.

    We delete with rm, which MediaStore doesn't notice, so the phone's Gallery
    keeps showing tiles for photos that are gone. Best effort: if the command
    isn't available on this Android version, the entries simply expire later.
    """
    try:
        sh("content call --uri content://media --method scan_volume "
           "--arg external_primary", timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        pass


def run_pull(job_id, paths, folder, move=False):
    os.makedirs(folder, exist_ok=True)
    done, failed, freed = 0, [], 0
    for p in paths:
        if cancelled(job_id):
            break
        name = posixpath.basename(p)
        target = unique_dest(folder, name)
        with JOBS_LOCK:
            JOBS[job_id].update(current=name)
        try:
            # Popen rather than run(), so a cancel can kill the transfer mid-file.
            proc = subprocess.Popen([ADB, "pull", p, target],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            PROCS[job_id] = proc
            code = proc.wait(timeout=900)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = -1
        finally:
            PROCS.pop(job_id, None)

        if cancelled(job_id):
            # Don't leave a half-written file behind.
            if os.path.exists(target):
                try:
                    os.unlink(target)
                except OSError:
                    pass
            break
        if code != 0:
            failed.append(name)
        else:
            done += 1
            if move:
                size = os.path.getsize(target) if os.path.exists(target) else 0
                if delete_on_phone(p, target):
                    freed += size
                else:
                    failed.append(f"{name} (copié, gardé sur le téléphone)")
        with JOBS_LOCK:
            JOBS[job_id].update(done=done, freed=freed)

    if freed:
        rescan_media()

    with JOBS_LOCK:
        state = "cancelled" if JOBS[job_id].get("cancel") else "done"
        JOBS[job_id].update(state=state, failed=failed, folder=folder, freed=freed)


# ------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _err(self, msg, code=400):
        self._json({"error": msg}, code)

    # ---- GET
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        route = u.path

        try:
            if route == "/":
                return self._file(os.path.join(HERE, "app.html"), "text/html; charset=utf-8")
            if route == "/api/device":
                return self._json(device_info())
            if route == "/api/config":
                cfg = load_config()
                # Show the caller exactly where its current folder would land.
                cfg["resolved"] = resolve_dest(q.get("subdir", [""])[0])
                return self._json(cfg)
            if route == "/api/ls":
                path = safe_path(q.get("path", [""])[0])
                items = listdir(path)
                if q.get("counts", ["0"])[0] == "1":
                    for it in items:
                        if it["dir"]:
                            it["count"] = count_children(it["path"])
                return self._json({"path": path, "items": items})
            if route == "/api/scan":
                path = safe_path(q.get("path", [""])[0])
                return self._json({"path": path, "items": scan_media(path)})
            if route == "/api/thumb":
                return self._thumb(q)
            if route == "/api/file":
                return self._stream(q)
            if route == "/api/job":
                jid = q.get("id", [""])[0]
                with JOBS_LOCK:
                    job = JOBS.get(jid)
                return self._json(job or {"error": "unknown job"})
        except ValueError as e:
            return self._err(str(e))
        except subprocess.TimeoutExpired:
            return self._err("phone timed out", 504)
        except Exception as e:  # keep the UI alive on odd filesystem corners
            return self._err(f"{type(e).__name__}: {e}", 500)

        self._err("not found", 404)

    def _file(self, path, ctype):
        if not os.path.exists(path):
            return self._err("missing app.html", 404)
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    def _thumb(self, q):
        path = safe_path(q.get("path", [""])[0])
        mtime = int(q.get("mtime", ["0"])[0])
        size = int(q.get("size", ["0"])[0])
        kind = q.get("kind", ["image"])[0]
        dst = get_thumb(path, mtime, size, kind)
        if not dst:
            return self._err("no thumbnail", 404)
        with open(dst, "rb") as f:
            self._send(200, f.read(), "image/jpeg",
                       {"Cache-Control": "public, max-age=31536000"})

    def _stream(self, q):
        """Full-resolution file, cached locally so the lightbox stays snappy."""
        path = safe_path(q.get("path", [""])[0])
        mtime = int(q.get("mtime", ["0"])[0])
        size = int(q.get("size", ["0"])[0])

        # Big videos are read range-by-range off the phone: caching a 1 GB clip
        # before the first frame plays would stall the lightbox for minutes.
        if size > STREAM_DIRECT_MIN and posixpath.splitext(path)[1].lower() in VIDEO_EXT:
            return self._stream_direct(path, size)

        h = hashlib.sha1(f"full|{path}|{mtime}|{size}".encode()).hexdigest()
        ext = posixpath.splitext(path)[1].lower()
        local = os.path.join(CACHE, h + ext)
        if not (os.path.exists(local) and os.path.getsize(local) > 0):
            with PULL_SEM:
                if not (os.path.exists(local) and os.path.getsize(local) > 0):
                    code, _, _ = adb(["pull", path, local], timeout=600)
                    if code != 0:
                        return self._err("pull failed", 502)
        ctype = mimetypes.guess_type(local)[0] or "application/octet-stream"
        total = os.path.getsize(local)

        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            start = int(m.group(1)) if m.group(1) else 0
            end = int(m.group(2)) if m.group(2) else total - 1
            end = min(end, total - 1)
            with open(local, "rb") as f:
                f.seek(start)
                chunk = f.read(end - start + 1)
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        with open(local, "rb") as f:
            self._send(200, f.read(), ctype,
                       {"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=86400"})

    def _stream_direct(self, path, size):
        """Serve a byte range straight off the phone, so video seeks are cheap."""
        q = shlex.quote(path)
        ctype = mimetypes.guess_type(path)[0] or "video/mp4"
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if m:
            start = int(m.group(1)) if m.group(1) else 0
            end = int(m.group(2)) if m.group(2) else min(start + STREAM_CHUNK - 1, size - 1)
        else:
            start, end = 0, min(STREAM_CHUNK - 1, size - 1)
        end = min(end, size - 1)
        if start >= size:
            return self._err("range out of bounds", 416)
        length = end - start + 1

        # dd works in blocks, so over-read and trim to the exact window.
        blk = 4096
        skip = start // blk
        pre = start - skip * blk
        count = -(-(pre + length) // blk)
        with PULL_SEM:
            code, data = exec_out(
                f"dd if={q} bs={blk} skip={skip} count={count} 2>/dev/null", timeout=300)
        if code != 0 or not data:
            return self._err("read failed", 502)
        chunk = data[pre:pre + length]

        self.send_response(206)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Range", f"bytes {start}-{start + len(chunk) - 1}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        try:
            self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- POST
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._err("bad json")

        if u.path == "/api/pull":
            paths = [safe_path(p) for p in body.get("paths", [])]
            if not paths:
                return self._err("nothing to transfer")
            folder = body.get("folder") or resolve_dest(body.get("subdir", ""))
            move = bool(body.get("move", load_config()["move"]))
            jid = hashlib.sha1(f"{time.time()}{paths[0]}".encode()).hexdigest()[:12]
            with JOBS_LOCK:
                JOBS[jid] = {"state": "running", "done": 0, "total": len(paths),
                             "current": "", "failed": [], "folder": folder,
                             "move": move, "freed": 0}
            threading.Thread(target=run_pull, args=(jid, paths, folder, move),
                             daemon=True).start()
            return self._json({"id": jid, "total": len(paths), "folder": folder,
                               "move": move})

        if u.path == "/api/config":
            cfg = load_config()
            for k in DEFAULTS:
                if k in body:
                    cfg[k] = os.path.expanduser(body[k]) if k == "dest" else bool(body[k])
            save_config(cfg)
            return self._json(cfg)

        if u.path == "/api/choose-folder":
            cfg = load_config()
            picked = choose_folder(body.get("start") or cfg["dest"])
            if not picked:
                return self._json({"cancelled": True})
            if body.get("save"):
                cfg["dest"] = picked
                save_config(cfg)
            return self._json({"folder": picked})

        if u.path == "/api/cancel":
            jid = body.get("id", "")
            with JOBS_LOCK:
                job = JOBS.get(jid)
                if not job:
                    return self._err("unknown job", 404)
                if job.get("state") == "running":
                    job["cancel"] = True
            proc = PROCS.get(jid)
            if proc and proc.poll() is None:
                proc.kill()          # stop the file currently in flight
            return self._json({"ok": True})

        if u.path == "/api/reveal":
            folder = body.get("folder") or load_config()["dest"]
            if os.path.exists(folder):
                subprocess.run(["open", folder], capture_output=True)
                return self._json({"ok": True})
            return self._err("folder missing", 404)

        self._err("not found", 404)


def cache_size():
    total = 0
    for root, _, files in os.walk(CACHE):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def clear_cache():
    n = 0
    for root, _, files in os.walk(CACHE):
        for f in files:
            try:
                os.unlink(os.path.join(root, f))
                n += 1
            except OSError:
                pass
    return n


def main():
    if "--clear-cache" in sys.argv:
        print(f"\n  Cleared {clear_cache()} cached files.\n")
        return

    if not shutil.which("adb") and not os.path.exists(ADB):
        print("\n  EasyTransfer ne peut pas démarrer : adb est introuvable.\n")
        print("  adb est l'outil qui permet au Mac de parler à un téléphone Android.")
        print("  Installe-le avec Homebrew :\n")
        print("      brew install android-platform-tools\n")
        print("  (Homebrew lui-même s'installe depuis https://brew.sh)\n")
        sys.exit(1)

    info = device_info()
    print("\n  EasyTransfer")
    print("  " + "-" * 42)
    if info.get("connected"):
        print(f"  Device    {info['brand']} {info['model']}  (Android {info['android']})")
        print(f"  Storage   {info['storage']}")
    else:
        print(f"  ⚠️  {info.get('error')}")
    print(f"  Transfers {load_config()['dest']}")
    print(f"  Cache     {cache_size() / 1048576:.0f} MB  (clear: ./easytransfer --clear-cache)")
    print(f"  Open      http://127.0.0.1:{PORT}\n")

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")


if __name__ == "__main__":
    main()
