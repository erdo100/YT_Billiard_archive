"""yt_archive Backend: Download via yt-dlp, History-DB, Settings-CRUD, Tracking.
Reduzierter Funktionsumfang nach Refactor: kein Quickscan, keine Profile,
kein Skip-Resolve, keine Auto-Tisch-Detection."""
from __future__ import annotations
import os
import re
import json
import base64
import sqlite3
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import cv2
import numpy as np
from flask import Flask, jsonify, request, send_file, send_from_directory, render_template

import yt_dlp

try:
    from deep_translator import GoogleTranslator
    _TRANSLATOR_AVAILABLE = True
except ImportError:
    _TRANSLATOR_AVAILABLE = False
    print("[translate] deep-translator nicht installiert — Titel werden nicht uebersetzt")

from settings import (
    Setting, GlobalSettings,
    list_settings, get_setting, upsert_setting, delete_setting,
    touch_setting, load_global, save_global,
    APP_DIR, hex_to_bgr,
)
from analyzer import (
    track_video,
    sample_color_at, sample_color_at_polygon_center,
    extract_frame_at_time,
)


# ---- App + State --------------------------------------------------------

app = Flask(__name__, static_folder="static", template_folder="templates")
DB_PATH = APP_DIR / "history.db"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "YouTube"
CONFIG_PATH = APP_DIR / "config.json"


def load_config() -> dict:
    cfg = {"download_dir": str(DEFAULT_DOWNLOAD_DIR)}
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[config] read failed: {e}")

    # Heilung: gespeicherter Pfad nicht erreichbar? Dann auf Default zuruecksetzen.
    dl = cfg.get("download_dir") or str(DEFAULT_DOWNLOAD_DIR)
    try:
        Path(dl).mkdir(parents=True, exist_ok=True)
    except (OSError, FileNotFoundError) as e:
        print(f"[config] gespeicherter download_dir '{dl}' nicht erreichbar ({e})")
        print(f"[config] heile config: setze auf default {DEFAULT_DOWNLOAD_DIR}")
        cfg["download_dir"] = str(DEFAULT_DOWNLOAD_DIR)
        try:
            DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e2:
            print(f"[config] heilung schlug fehl: {e2}")
    return cfg


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


_config = load_config()


def get_download_dir() -> Path:
    """Liefert den aktuellen Download-Ordner. Bei nicht-erreichbarem Pfad
    Fallback auf default, statt zu crashen.
    """
    configured = _config.get("download_dir") or str(DEFAULT_DOWNLOAD_DIR)
    p = Path(configured)
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except (OSError, FileNotFoundError) as e:
        print(f"[config] download_dir '{p}' nicht erreichbar ({e})")
        print(f"[config] fallback auf default: {DEFAULT_DOWNLOAD_DIR}")
        try:
            DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as e2:
            print(f"[config] auch default schlug fehl: {e2}")
            # Letzter Ausweg: Working-Dir
            return Path.cwd()
        _config["download_dir"] = str(DEFAULT_DOWNLOAD_DIR)
        try:
            save_config(_config)
        except Exception:
            pass
        return DEFAULT_DOWNLOAD_DIR


# Worker-State
status_lock = threading.Lock()
download_queue: list[dict] = []          # {video_id, title, url, status, progress, error?}
tracking_jobs: dict[str, dict] = {}      # video_id -> {status, phase, current, total, clips_found, error?}
download_worker_running = False
tracking_worker_running = False


# ---- DB ------------------------------------------------------------------

def db_conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def db_init():
    with db_conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY,
                title TEXT,
                title_en TEXT,
                channel TEXT,
                url TEXT,
                date TEXT,
                folder TEXT,
                video_path TEXT,
                added_at TEXT,
                last_setting TEXT,
                duration_s REAL,
                clips_count INTEGER,
                last_tracked_at TEXT
            )
        """)
        # Migration: fehlende Spalten ergaenzen
        cols = {row[1] for row in c.execute("PRAGMA table_info(videos)")}
        if "last_setting" not in cols:
            c.execute("ALTER TABLE videos ADD COLUMN last_setting TEXT")
        if "title_en" not in cols:
            c.execute("ALTER TABLE videos ADD COLUMN title_en TEXT")
        if "duration_s" not in cols:
            c.execute("ALTER TABLE videos ADD COLUMN duration_s REAL")
        if "clips_count" not in cols:
            c.execute("ALTER TABLE videos ADD COLUMN clips_count INTEGER")
        if "last_tracked_at" not in cols:
            c.execute("ALTER TABLE videos ADD COLUMN last_tracked_at TEXT")


db_init()


# ---- Helpers -------------------------------------------------------------

def get_video_duration_s(path: str) -> float | None:
    """Liest Dauer in Sekunden via cv2. None bei Fehler."""
    try:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        cap.release()
        if fps > 0 and frames > 0:
            return round(frames / fps, 1)
    except Exception:
        pass
    return None


def count_clips_in_folder(folder: str) -> int:
    """Zaehlt clipNN.json files im Video-Ordner."""
    try:
        p = Path(folder)
        if not p.exists():
            return 0
        return len(list(p.glob("clip*.json")))
    except Exception:
        return 0


def write_youtube_link_file(folder: Path, video_id: str, title: str = "") -> bool:
    """Schreibt eine Windows-kompatible .url-Datei mit dem YouTube-Link.
    Funktioniert auch unter macOS/Linux (Plain Text, Doppelklick-Verhalten ist
    OS-spezifisch). Dateiname: youtube_link.url"""
    try:
        url = f"https://www.youtube.com/watch?v={video_id}"
        content = f"[InternetShortcut]\nURL={url}\n"
        (folder / "youtube_link.url").write_text(content, encoding="utf-8")
        return True
    except Exception as e:
        print(f"[yt-link] {folder}: {e}")
        return False


def generate_video_thumbnail(video_path: str, output_path: str,
                             seconds_before_end: float = 0.5) -> bool:
    """Extrahiert einen Frame nahe am Video-Ende und speichert als JPEG.
    Default: 0.5s vor Schluss — gegen Outro/Ueberblendungen am letzten Frame.
    Returns True bei Erfolg.
    """
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return False
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total_frames <= 0:
            cap.release()
            return False
        offset_frames = max(1, int(round(seconds_before_end * fps)))
        target = max(0, total_frames - offset_frames)
        cap.set(cv2.CAP_PROP_POS_FRAMES, target)
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return False
        # Auf hoechstens 640 px Breite herunterskalieren
        h, w = frame.shape[:2]
        if w > 640:
            scale = 640.0 / w
            frame = cv2.resize(frame, (640, int(h * scale)))
        return bool(cv2.imwrite(output_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 82]))
    except Exception as e:
        print(f"[thumb] generate failed for {video_path}: {e}")
        return False


def list_clips_in_folder(folder: str) -> list[dict]:
    """Liefert sortierte Liste der Clips: [{name, mp4, json, thumb, frames}, ...]"""
    out = []
    try:
        p = Path(folder)
        if not p.exists():
            return out
        for jp in sorted(p.glob("clip*.json")):
            name = jp.stem
            mp4 = jp.with_suffix(".mp4")
            if not mp4.exists():
                continue
            thumb = p / f"{name}_thumb.jpg"
            entry = {
                "name": name,
                "mp4": mp4.name,
                "json": jp.name,
                "thumb": thumb.name if thumb.exists() else None,
                "frames": None,
                "start_frame_in_video": None,
                "fps": None,
            }
            try:
                meta = json.loads(jp.read_text(encoding="utf-8"))
                entry["frames"] = meta.get("frames_total")
                entry["start_frame_in_video"] = meta.get("start_frame_in_video")
                entry["fps"] = meta.get("fps")
            except Exception:
                pass
            out.append(entry)
    except Exception:
        pass
    return out


def ensure_video_meta(video_id: str) -> dict:
    """Stellt sicher dass duration_s und clips_count gefuellt sind (lazy fill),
    und dass _thumb.jpg existiert (lazy generate).
    Returns: dict mit den aktuellen Werten.
    """
    with db_conn() as c:
        row = c.execute("""
            SELECT video_path, folder, duration_s, clips_count
            FROM videos WHERE video_id = ?
        """, (video_id,)).fetchone()
        if not row:
            return {}
        d = dict(row)
        changed = False
        if d["duration_s"] is None and d["video_path"]:
            dur = get_video_duration_s(d["video_path"])
            if dur is not None:
                d["duration_s"] = dur
                c.execute("UPDATE videos SET duration_s = ? WHERE video_id = ?",
                          (dur, video_id))
                changed = True
        if d["clips_count"] is None and d["folder"]:
            cnt = count_clips_in_folder(d["folder"])
            d["clips_count"] = cnt
            c.execute("UPDATE videos SET clips_count = ? WHERE video_id = ?",
                      (cnt, video_id))
            changed = True
    # Thumbnail lazy
    if d.get("folder") and d.get("video_path"):
        thumb = Path(d["folder"]) / "_thumb.jpg"
        if not thumb.exists():
            generate_video_thumbnail(d["video_path"], str(thumb), seconds_before_end=0.5)
        # YouTube-Link lazy
        url_file = Path(d["folder"]) / "youtube_link.url"
        if not url_file.exists():
            write_youtube_link_file(Path(d["folder"]), video_id)
    return d


def sanitize_title(title: str, max_len: int = 50) -> str:
    t = re.sub(r"[^\w\s-]", "", title or "")
    t = re.sub(r"\s+", "_", t.strip())
    return t[:max_len] or "video"


def translate_title(title: str, video_id: str) -> str:
    """Title in Englisch uebersetzen. Cache via DB (videos.title_en).
    Fallback bei Fehler / offline / deep-translator nicht installiert:
    Original-Title zurueck.
    """
    if not title:
        return ""
    # Cache-Lookup
    try:
        with db_conn() as c:
            row = c.execute(
                "SELECT title_en FROM videos WHERE video_id = ?", (video_id,)
            ).fetchone()
            if row and row["title_en"]:
                return row["title_en"]
    except Exception:
        pass

    if not _TRANSLATOR_AVAILABLE:
        return title

    try:
        translator = GoogleTranslator(source="auto", target="en")
        translated = translator.translate(title)
        if translated and translated.strip():
            return translated.strip()
    except Exception as e:
        print(f"[translate] '{title[:40]}…' fehlgeschlagen: {e}")
    return title


def build_video_paths(date_str: str, title: str, video_id: str,
                      download_dir: Path) -> tuple[Path, Path, str]:
    """Returns (folder, video_file_path_pattern, folder_name)."""
    # date_str: YYYYMMDD vom upload
    short = date_str[2:] if len(date_str) >= 8 else "000000"  # YYMMDD
    sanitized = sanitize_title(title)
    # Naechste Nummer im Tages-Ordner finden
    day_dir = download_dir / short
    day_dir.mkdir(parents=True, exist_ok=True)
    existing = list(day_dir.glob(f"{short}_*"))
    nums = []
    for p in existing:
        m = re.match(rf"^{short}_(\d{{2}})_", p.name)
        if m:
            nums.append(int(m.group(1)))
    n = max(nums, default=0) + 1
    folder_name = f"{short}_{n:02d}_{sanitized}_{video_id}"
    folder = day_dir / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    return folder, folder / f"{folder_name}.%(ext)s", folder_name


def extract_video_id(url: str) -> str | None:
    """Extrahiert YouTube-Video-ID aus URL."""
    try:
        parsed = urlparse(url)
        if "youtu.be" in parsed.netloc:
            return parsed.path.lstrip("/")
        if "youtube.com" in parsed.netloc:
            qs = parse_qs(parsed.query)
            if "v" in qs:
                return qs["v"][0]
    except Exception:
        pass
    return None


def rescan_download_dir():
    """Rescan: finde alle Videos im Download-Ordner und sync DB."""
    download_dir = get_download_dir()
    if not download_dir.exists():
        return 0
    pattern = re.compile(r"_([A-Za-z0-9_-]{11})$")
    found = 0
    with db_conn() as c:
        for folder in download_dir.rglob("*"):
            if not folder.is_dir():
                continue
            m = pattern.search(folder.name)
            if not m:
                continue
            vid = m.group(1)
            # Hat es ein Video drin?
            videos = list(folder.glob(f"{folder.name}.*"))
            video_files = [v for v in videos if v.suffix.lower() in [".mp4", ".mkv", ".webm"]]
            if not video_files:
                continue
            video_path = video_files[0]
            row = c.execute("SELECT video_id FROM videos WHERE video_id = ?", (vid,)).fetchone()
            if not row:
                c.execute("""
                    INSERT INTO videos (video_id, title, channel, url, date, folder, video_path, added_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (vid, folder.name, "", f"https://youtu.be/{vid}", "",
                      str(folder), str(video_path),
                      time.strftime("%Y-%m-%d %H:%M:%S")))
                found += 1
            else:
                c.execute("UPDATE videos SET folder = ?, video_path = ? WHERE video_id = ?",
                          (str(folder), str(video_path), vid))
    return found


# ---- yt-dlp --------------------------------------------------------------

def fetch_channel_videos(channel_url: str, limit: int = 30,
                         offset: int = 0) -> list[dict]:
    """Holt Videos eines Kanals mit Pagination. offset=0 → neueste 30."""
    opts = {
        "quiet": True,
        "extract_flat": True,
        "playliststart": offset + 1,             # 1-indexed
        "playlistend": offset + limit,
        "skip_download": True,
    }
    out = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)
        entries = info.get("entries") or []
        for e in entries:
            if not e:
                continue
            vid = e.get("id")
            if not vid:
                continue
            out.append({
                "video_id": vid,
                "title": e.get("title") or "",
                "url": f"https://youtu.be/{vid}",
                "duration": e.get("duration"),
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            })
    return out


def download_video(item: dict):
    """Synchroner Download eines Videos. Updated item in-place."""
    url = item["url"]
    video_id = item["video_id"]
    title = item.get("title") or video_id

    def hook(d):
        if d.get("status") == "downloading":
            pct = 0.0
            if d.get("total_bytes"):
                pct = 100.0 * d["downloaded_bytes"] / d["total_bytes"]
            elif d.get("total_bytes_estimate"):
                pct = 100.0 * d["downloaded_bytes"] / d["total_bytes_estimate"]
            with status_lock:
                item["progress"] = round(pct, 1)
        elif d.get("status") == "finished":
            with status_lock:
                item["progress"] = 100.0

    # Erst Metadaten fuer date
    meta_opts = {"quiet": True, "skip_download": True}
    with yt_dlp.YoutubeDL(meta_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    date_str = info.get("upload_date") or time.strftime("%Y%m%d")
    real_title = info.get("title") or title

    # Title uebersetzen (auto → englisch). Bei Fehler: Original.
    title_en = translate_title(real_title, video_id)

    folder, outtmpl, folder_name = build_video_paths(
        date_str, title_en, video_id, get_download_dir()
    )

    ydl_opts = {
        "outtmpl": str(outtmpl),
        "format": "bestvideo[height<=1080]+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "writeinfojson": False,
        "writesubtitles": False,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # Finde tatsaechliche Video-Datei
    videos = list(folder.glob(f"{folder_name}.*"))
    video_files = [v for v in videos if v.suffix.lower() in [".mp4", ".mkv", ".webm"]]
    if not video_files:
        raise RuntimeError("download finished but no video file found")
    video_path = video_files[0]
    duration = get_video_duration_s(str(video_path))

    # Thumbnail erzeugen (0.5s vor Video-Ende)
    thumb_path = folder / "_thumb.jpg"
    generate_video_thumbnail(str(video_path), str(thumb_path), seconds_before_end=0.5)

    # YouTube-Link als .url-Datei
    write_youtube_link_file(folder, video_id, real_title)

    with db_conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO videos
            (video_id, title, title_en, channel, url, date, folder, video_path,
             added_at, last_setting, duration_s, clips_count, last_tracked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT last_setting FROM videos WHERE video_id = ?), NULL),
                    ?,
                    COALESCE((SELECT clips_count FROM videos WHERE video_id = ?), 0),
                    (SELECT last_tracked_at FROM videos WHERE video_id = ?))
        """, (video_id, real_title, title_en, info.get("uploader") or "", url, date_str,
              str(folder), str(video_path),
              time.strftime("%Y-%m-%d %H:%M:%S"), video_id, duration, video_id, video_id))


def download_worker():
    global download_worker_running
    while True:
        with status_lock:
            pending = next((q for q in download_queue if q["status"] == "queued"), None)
            if not pending:
                download_worker_running = False
                return
            pending["status"] = "downloading"

        try:
            download_video(pending)
            with status_lock:
                pending["status"] = "done"
                pending["progress"] = 100.0
            # Auto-Track wenn gewuenscht
            auto_setting = pending.get("auto_track_setting")
            if auto_setting:
                start_tracking(pending["video_id"], auto_setting)
        except Exception as e:
            with status_lock:
                pending["status"] = "error"
                pending["error"] = str(e)


def start_download_worker():
    global download_worker_running
    with status_lock:
        if download_worker_running:
            return
        download_worker_running = True
    threading.Thread(target=download_worker, daemon=True).start()


# ---- Tracking-Worker ----------------------------------------------------

def run_tracking(video_id: str, setting_id: str):
    """Tracking-Job: Setting laden, track_video aufrufen, Status updaten."""
    with db_conn() as c:
        row = c.execute("SELECT video_path, folder FROM videos WHERE video_id = ?",
                        (video_id,)).fetchone()
    if not row:
        with status_lock:
            tracking_jobs[video_id] = {
                "status": "error", "error": "video not in db", "phase": "", 
                "current": 0, "total": 0, "clips_found": 0,
            }
        return

    video_path = row["video_path"]
    folder = Path(row["folder"])

    setting = get_setting(setting_id)
    if setting is None:
        with status_lock:
            tracking_jobs[video_id] = {
                "status": "error", "error": "setting not found", "phase": "",
                "current": 0, "total": 0, "clips_found": 0,
            }
        return
    if not setting.is_complete():
        with status_lock:
            tracking_jobs[video_id] = {
                "status": "error",
                "error": "setting incomplete (table corners or ball colors missing)",
                "phase": "", "current": 0, "total": 0, "clips_found": 0,
            }
        return

    touch_setting(setting_id)

    with db_conn() as c:
        c.execute("UPDATE videos SET last_setting = ? WHERE video_id = ?",
                  (setting_id, video_id))

    gs = load_global()

    with status_lock:
        tracking_jobs[video_id] = {
            "status": "running", "phase": "init", "current": 0, "total": 0,
            "clips_found": 0, "ranges_found": 0,
        }

    def progress(phase: str, current: int, total: int, ranges_so_far=None):
        with status_lock:
            job = tracking_jobs.get(video_id, {})
            if job.get("cancel"):
                raise RuntimeError("CANCELLED_BY_USER")
            job["phase"] = phase
            job["current"] = current
            job["total"] = total
            if ranges_so_far is not None:
                job["ranges_so_far"] = ranges_so_far
                job["ranges_so_far_count"] = len(ranges_so_far)
            tracking_jobs[video_id] = job

    try:
        # Setting-Queue aufbauen — bei auto_fallback_seconds > 0 zusaetzlich
        # alle anderen vollstaendigen Settings als Fallback.
        settings_queue = [setting]
        if (gs.auto_fallback_seconds or 0) > 0:
            others = [s for s in list_settings()
                      if s.id != setting_id and s.is_complete()]
            settings_queue.extend(others)

        summary = None
        last_no_topview = None
        for s in settings_queue:
            with status_lock:
                job = tracking_jobs.get(video_id, {})
                job["current_setting"] = s.name
                tracking_jobs[video_id] = job
            try:
                from analyzer import NoTopViewFoundException
                summary = track_video(video_path, s, gs, folder, progress)
                # Erfolg: das verwendete Setting wird als last_setting gespeichert
                with db_conn() as c:
                    c.execute("UPDATE videos SET last_setting = ? WHERE video_id = ?",
                              (s.id, video_id))
                break
            except NoTopViewFoundException as e:
                last_no_topview = e
                print(f"[fallback] setting '{s.name}': {e}")
                continue

        if summary is None:
            # Alle Settings durch, keines passte
            msg = "no setting found top-view in video"
            if last_no_topview:
                msg += f" — {last_no_topview}"
            raise RuntimeError(msg)

        clips_count = len(summary["clips"])
        duration_s = None
        if summary.get("total_frames") and summary.get("fps"):
            try:
                duration_s = round(summary["total_frames"] / summary["fps"], 1)
            except Exception:
                pass
        with db_conn() as c:
            if duration_s is not None:
                c.execute("""UPDATE videos SET clips_count = ?, last_tracked_at = ?,
                             duration_s = COALESCE(duration_s, ?) WHERE video_id = ?""",
                          (clips_count, time.strftime("%Y-%m-%d %H:%M:%S"),
                           duration_s, video_id))
            else:
                c.execute("""UPDATE videos SET clips_count = ?, last_tracked_at = ?
                             WHERE video_id = ?""",
                          (clips_count, time.strftime("%Y-%m-%d %H:%M:%S"), video_id))
        with status_lock:
            tracking_jobs[video_id] = {
                "status": "done", "phase": "done",
                "current": summary["ranges_found"],
                "total": summary["ranges_found"],
                "clips_found": clips_count,
                "ranges_found": summary["ranges_found"],
                "fps": summary["fps"],
                "skipped_ranges": summary.get("skipped_ranges", []),
                "clips": summary["clips"],
            }
    except Exception as e:
        import traceback
        if "CANCELLED_BY_USER" in str(e):
            with status_lock:
                tracking_jobs[video_id] = {
                    "status": "cancelled", "phase": "cancelled",
                    "current": 0, "total": 0, "clips_found": 0,
                }
        else:
            traceback.print_exc()
            with status_lock:
                tracking_jobs[video_id] = {
                    "status": "error", "error": str(e), "phase": "",
                    "current": 0, "total": 0, "clips_found": 0,
                }


@app.post("/api/track/cancel")
def api_track_cancel():
    """Setzt das Cancel-Flag fuer einen laufenden Tracking-Job. Der Job
    bricht beim naechsten progress-cb-Aufruf sauber ab."""
    data = request.get_json(force=True)
    vid = data.get("video_id")
    if not vid:
        return jsonify({"error": "video_id missing"}), 400
    with status_lock:
        job = tracking_jobs.get(vid)
        if not job or job.get("status") != "running":
            return jsonify({"error": "no running job"}), 404
        job["cancel"] = True
        tracking_jobs[vid] = job
    return jsonify({"ok": True})


def start_tracking(video_id: str, setting_id: str) -> bool:
    """Startet einen Tracking-Job in einem eigenen Thread."""
    with status_lock:
        existing = tracking_jobs.get(video_id)
        if existing and existing.get("status") == "running":
            return False
    threading.Thread(target=run_tracking, args=(video_id, setting_id), daemon=True).start()
    return True


# ---- Frame-Decode-Helper ------------------------------------------------

def decode_b64_frame(b64_str: str | None) -> np.ndarray | None:
    if not b64_str:
        return None
    if "," in b64_str:
        b64_str = b64_str.split(",", 1)[1]
    try:
        data = base64.b64decode(b64_str)
        arr = np.frombuffer(data, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return None


# =========================================================================
# Routes
# =========================================================================

@app.route("/")
def index():
    return render_template("index.html")


# ---- Channel/Browse -----------------------------------------------------

@app.post("/api/import-local")
def api_import_local():
    """Importiert eine lokale Video-Datei. Die Datei bleibt am Original-Ort,
    nur ein Output-Ordner mit Thumbnail und ein DB-Eintrag werden angelegt.
    Body: {path: "C:/path/to/video.mp4"}
    """
    import hashlib
    data = request.get_json(force=True)
    raw = (data.get("path") or "").strip()
    # Pasted Pfade haben oft Quotes drum
    raw = raw.strip('"').strip("'")
    if not raw:
        return jsonify({"error": "path missing"}), 400
    p = Path(raw)
    if not p.exists() or not p.is_file():
        return jsonify({"error": f"file not found: {raw}"}), 404

    # Stabile video_id aus absolutem Pfad
    abs_str = str(p.resolve())
    digest = hashlib.md5(abs_str.encode("utf-8")).hexdigest()[:12]
    video_id = f"local_{digest}"

    # Duplikat-Check
    with db_conn() as c:
        existing = c.execute("SELECT video_id, folder FROM videos WHERE video_id = ?",
                             (video_id,)).fetchone()
    if existing:
        return jsonify({"error": "already imported", "video_id": video_id}), 400

    stem = p.stem
    safe_stem = sanitize_title(stem, max_len=40)
    folder = get_download_dir() / f"{time.strftime('%Y-%m-%d')}_{safe_stem}_{video_id}"
    folder.mkdir(parents=True, exist_ok=True)

    duration = get_video_duration_s(str(p))

    # Thumbnail
    thumb_path = folder / "_thumb.jpg"
    generate_video_thumbnail(str(p), str(thumb_path), seconds_before_end=0.5)

    with db_conn() as c:
        c.execute("""
            INSERT INTO videos
            (video_id, title, title_en, channel, url, date, folder, video_path,
             added_at, last_setting, duration_s, clips_count, last_tracked_at)
            VALUES (?, ?, NULL, '(local file)', NULL, ?, ?, ?, ?, NULL, ?, 0, NULL)
        """, (video_id, stem, time.strftime("%Y-%m-%d"), str(folder), str(p),
              time.strftime("%Y-%m-%d %H:%M:%S"), duration))

    return jsonify({
        "ok": True,
        "video_id": video_id,
        "title": stem,
        "folder": str(folder),
        "duration_s": duration,
    })


@app.post("/api/video-info")
def api_video_info():
    """Holt Metadaten fuer EINE Video-URL. Liefert Video-Dict im selben Format
    wie /api/channel-Eintraege, inkl. Cross-Ref-Status.
    """
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url missing"}), 400
    try:
        opts = {"quiet": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        vid = info.get("id")
        if not vid:
            return jsonify({"error": "could not determine video id"}), 400
        out = {
            "video_id": vid,
            "title": info.get("title") or "",
            "url": f"https://youtu.be/{vid}",
            "duration": info.get("duration"),
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        }
        with db_conn() as c:
            row = c.execute("""SELECT title_en, duration_s, clips_count, last_tracked_at
                               FROM videos WHERE video_id = ?""", (vid,)).fetchone()
        if row:
            out["in_archive"] = True
            out["duration_s"] = row["duration_s"]
            out["clips_count"] = row["clips_count"]
            out["last_tracked_at"] = row["last_tracked_at"]
            if row["title_en"]:
                out["title_en"] = row["title_en"]
        else:
            out["in_archive"] = False
        return jsonify({"video": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/channel")
def api_channel():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    limit = int(data.get("limit", 30))
    offset = int(data.get("offset", 0))
    if not url:
        return jsonify({"error": "url missing"}), 400
    try:
        if url.startswith("@"):
            url = f"https://www.youtube.com/{url}/videos"
        elif "youtube.com" not in url and "youtu.be" not in url:
            url = f"https://www.youtube.com/@{url.lstrip('@')}/videos"
        elif "youtube.com" in url and "/videos" not in url and "/playlist" not in url:
            if not url.rstrip("/").endswith("/videos"):
                url = url.rstrip("/") + "/videos"

        videos = fetch_channel_videos(url, limit=limit, offset=offset)

        # Cross-Reference mit DB
        if videos:
            vid_list = [v["video_id"] for v in videos]
            placeholders = ",".join("?" * len(vid_list))
            with db_conn() as c:
                rows = c.execute(f"""
                    SELECT video_id, title, title_en, duration_s, clips_count,
                           last_tracked_at, folder
                    FROM videos WHERE video_id IN ({placeholders})
                """, vid_list).fetchall()
            archived = {r["video_id"]: dict(r) for r in rows}
            for v in videos:
                m = archived.get(v["video_id"])
                if m:
                    v["in_archive"] = True
                    v["duration_s"] = m.get("duration_s")
                    v["clips_count"] = m.get("clips_count")
                    v["last_tracked_at"] = m.get("last_tracked_at")
                    # Englischer Titel falls vorhanden — fuer konsistente
                    # Anzeige zwischen Browse/Queue/Archiv
                    if m.get("title_en"):
                        v["title_en"] = m["title_en"]
                else:
                    v["in_archive"] = False

        return jsonify({"videos": videos, "offset": offset, "limit": limit})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- Download -----------------------------------------------------------

@app.post("/api/download")
def api_download():
    data = request.get_json(force=True)
    videos = data.get("videos") or []
    auto_track_setting = data.get("auto_track_setting")  # optional
    if not videos:
        return jsonify({"error": "no videos"}), 400
    with status_lock:
        for v in videos:
            if not v.get("video_id"):
                continue
            # Bereits in Queue?
            if any(q["video_id"] == v["video_id"] for q in download_queue):
                continue
            # title_en bevorzugen damit Queue & Archiv die gleiche Sprache zeigen
            display_title = v.get("title_en") or v.get("title") or v["video_id"]
            download_queue.append({
                "video_id": v["video_id"],
                "title": display_title,
                "url": v.get("url") or f"https://youtu.be/{v['video_id']}",
                "status": "queued",
                "progress": 0.0,
                "auto_track_setting": auto_track_setting,
            })
    start_download_worker()
    return jsonify({"queued": len(videos)})


@app.get("/api/status")
def api_status():
    with status_lock:
        return jsonify({
            "downloads": list(download_queue),
            "tracking": dict(tracking_jobs),
        })


@app.post("/api/queue/clear-done")
def api_clear_done():
    with status_lock:
        before = len(download_queue)
        download_queue[:] = [q for q in download_queue if q["status"] not in ("done", "error")]
        removed = before - len(download_queue)
    return jsonify({"removed": removed})


# ---- History ------------------------------------------------------------

@app.get("/api/history")
def api_history():
    with db_conn() as c:
        rows = c.execute("""
            SELECT video_id, title, title_en, channel, url, date, folder, video_path,
                   added_at, last_setting, duration_s, clips_count, last_tracked_at
            FROM videos ORDER BY added_at DESC
        """).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # ensure_video_meta laeuft immer, damit fehlende _thumb.jpg lazy nachgeneriert
        # werden. Innerhalb wird gecheckt was schon da ist, also billig.
        meta = ensure_video_meta(d["video_id"])
        d["duration_s"] = meta.get("duration_s", d.get("duration_s"))
        d["clips_count"] = meta.get("clips_count", d.get("clips_count"))
        d["clips"] = list_clips_in_folder(d["folder"])
        # Hat das Video ein eigenes Thumbnail?
        thumb_p = Path(d["folder"]) / "_thumb.jpg" if d["folder"] else None
        d["has_thumb"] = bool(thumb_p and thumb_p.exists())
        out.append(d)
    return jsonify({"videos": out})


@app.post("/api/history/delete")
def api_history_delete():
    data = request.get_json(force=True)
    vid = data.get("video_id")
    if not vid:
        return jsonify({"error": "video_id missing"}), 400
    with db_conn() as c:
        c.execute("DELETE FROM videos WHERE video_id = ?", (vid,))
    return jsonify({"ok": True})


@app.post("/api/clip/delete")
def api_clip_delete():
    """Loescht einen oder mehrere Clips aus dem Video-Ordner.
    Body: {video_id, clip_names: [...]}  -> jeder name ist "clipNN" (ohne Endung).
    Loescht clipNN.mp4, clipNN.json, clipNN_thumb.jpg.
    Aktualisiert clips_count in der DB.
    """
    data = request.get_json(force=True)
    vid = data.get("video_id")
    names = data.get("clip_names") or []
    if not vid or not names:
        return jsonify({"error": "video_id or clip_names missing"}), 400
    with db_conn() as c:
        row = c.execute("SELECT folder FROM videos WHERE video_id = ?", (vid,)).fetchone()
    if not row:
        return jsonify({"error": "video not found"}), 404
    folder = Path(row["folder"])
    if not folder.exists():
        return jsonify({"error": "folder not found"}), 404

    deleted = []
    failed = []
    for name in names:
        # Sicherheits-Check: keine Pfad-Traversal, nur clipNN-Pattern
        if not name.startswith("clip") or "/" in name or "\\" in name or ".." in name:
            failed.append(name)
            continue
        for suffix in (".mp4", ".json", "_thumb.jpg"):
            p = folder / f"{name}{suffix}"
            try:
                if p.exists():
                    p.unlink()
            except Exception as e:
                print(f"[clip-delete] {p}: {e}")
        deleted.append(name)

    # DB clips_count aktualisieren
    new_count = count_clips_in_folder(str(folder))
    with db_conn() as c:
        c.execute("UPDATE videos SET clips_count = ? WHERE video_id = ?",
                  (new_count, vid))

    return jsonify({"ok": True, "deleted": deleted, "failed": failed,
                    "clips_count": new_count})


@app.post("/api/rescan")
def api_rescan():
    found = rescan_download_dir()
    return jsonify({"new_videos": found})


# ---- File serving -------------------------------------------------------

@app.get("/api/video-folder/<vid>")
def api_video_folder(vid: str):
    with db_conn() as c:
        row = c.execute("SELECT folder FROM videos WHERE video_id = ?", (vid,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    folder = Path(row["folder"])
    if not folder.exists():
        return jsonify({"files": []})
    files = []
    for f in folder.iterdir():
        if f.is_file():
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "is_video": f.suffix.lower() in (".mp4", ".mkv", ".webm"),
                "is_json": f.suffix.lower() == ".json",
                "is_preview": f.name == "_preview.jpg",
            })
    return jsonify({"folder": str(folder), "files": files})


@app.get("/api/file/<vid>/<path:filename>")
def api_file(vid: str, filename: str):
    with db_conn() as c:
        row = c.execute("SELECT folder FROM videos WHERE video_id = ?", (vid,)).fetchone()
    if not row:
        return ("not found", 404)
    folder = Path(row["folder"])
    fp = folder / filename
    if not fp.exists() or not fp.is_file():
        return ("not found", 404)
    return send_file(fp)


@app.get("/api/preview/<vid>")
def api_preview(vid: str):
    with db_conn() as c:
        row = c.execute("SELECT folder FROM videos WHERE video_id = ?", (vid,)).fetchone()
    if not row:
        return ("not found", 404)
    pp = Path(row["folder"]) / "_preview.jpg"
    if not pp.exists():
        return ("kein preview", 404)
    resp = send_file(pp)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.get("/api/thumb/<vid>")
def api_thumb(vid: str):
    """Liefert das Standbild-Thumbnail (_thumb.jpg, 0.5s vor Ende). Falls noch
    nicht generiert, wird's lazy gemacht. Fallback: _preview.jpg.
    """
    with db_conn() as c:
        row = c.execute("SELECT folder, video_path FROM videos WHERE video_id = ?",
                        (vid,)).fetchone()
    if not row:
        return ("not found", 404)
    folder = Path(row["folder"]) if row["folder"] else None
    if not folder:
        return ("kein ordner", 404)
    thumb = folder / "_thumb.jpg"
    if not thumb.exists() and row["video_path"]:
        # Lazy-Generate
        generate_video_thumbnail(row["video_path"], str(thumb), seconds_before_end=0.5)
    if thumb.exists():
        resp = send_file(thumb)
        resp.headers["Cache-Control"] = "public, max-age=300"
        return resp
    # Fallback: _preview.jpg
    pp = folder / "_preview.jpg"
    if pp.exists():
        return send_file(pp)
    return ("kein thumbnail", 404)


# ---- Settings (CRUD) ----------------------------------------------------

@app.get("/api/settings")
def api_settings_list():
    return jsonify({
        "settings": [s.to_dict() for s in list_settings()],
    })


@app.get("/api/settings/<sid>")
def api_settings_get(sid: str):
    s = get_setting(sid)
    if s is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"setting": s.to_dict()})


@app.post("/api/settings")
def api_settings_save():
    data = request.get_json(force=True)
    setting = Setting.from_dict(data)
    setting = upsert_setting(setting)
    return jsonify({"setting": setting.to_dict()})


@app.delete("/api/settings/<sid>")
def api_settings_delete(sid: str):
    ok = delete_setting(sid)
    return jsonify({"ok": ok})


# ---- Global -------------------------------------------------------------

@app.get("/api/global")
def api_global_get():
    return jsonify({"global": load_global().to_dict()})


@app.post("/api/global")
def api_global_save():
    data = request.get_json(force=True)
    g = GlobalSettings.from_dict(data)
    save_global(g)
    return jsonify({"global": g.to_dict()})


# ---- Editor-Helfer ------------------------------------------------------

@app.post("/api/editor/sample-color")
def api_editor_sample_color():
    """Sampelt eine Farbe im uebergebenen Frame an Position (x, y)."""
    data = request.get_json(force=True)
    frame = decode_b64_frame(data.get("frame_b64"))
    if frame is None:
        return jsonify({"error": "no frame"}), 400
    x = int(data.get("x", 0))
    y = int(data.get("y", 0))
    hex_str = sample_color_at(frame, x, y)
    return jsonify({"hex": hex_str})


@app.post("/api/editor/sample-felt")
def api_editor_sample_felt():
    """Sampelt Filz-Farbe aus dem Polygon-Zentrum."""
    data = request.get_json(force=True)
    frame = decode_b64_frame(data.get("frame_b64"))
    if frame is None:
        return jsonify({"error": "no frame"}), 400
    corners = data.get("corners") or []
    if len(corners) != 4:
        return jsonify({"error": "4 corners required"}), 400
    hex_str = sample_color_at_polygon_center(frame, corners)
    return jsonify({"hex": hex_str})


@app.post("/api/setup/detect")
def api_setup_detect():
    """Live-Tracking-Vorschau mit echtem temporalem Tracking-State.
    Frontend schickt den aktuellen Tracking-State mit, Backend macht einen
    Tracking-Schritt mit Bewegungs-Constraint + EMA-Farbe (wie Pass 2 vom
    echten Tracking, aber nur forward).
    """
    from analyzer import (
        compute_homography, rectify_frame, find_blobs,
        assign_balls_3class, track_one_step, blend_color_bounded,
        is_table_visible, felt_percentage,
        BALL_CLASSES, BALL_RADIUS_PX, PX_PER_M,
    )

    data = request.get_json(force=True)
    frame = decode_b64_frame(data.get("frame_b64"))
    if frame is None:
        return jsonify({"error": "no frame"}), 400

    setting_data = data.get("setting") or {}
    corners = setting_data.get("table_corners") or []
    ball_hexes = setting_data.get("ball_colors_hex") or []
    if len(corners) != 4:
        return jsonify({"error": "table corners incomplete"}), 400
    if len(ball_hexes) != 3:
        return jsonify({"error": "ball colors incomplete"}), 400

    # Eingehender Tracking-State (null beim ersten Call oder nach Reset)
    ts_in = data.get("tracking_state") or {}
    in_balls = ts_in.get("balls") or {}
    in_colors = ts_in.get("track_colors") or {}
    in_lost = ts_in.get("lost") or {}
    in_no_table_streak = int(ts_in.get("no_table_streak", 0))
    dt_s = max(0.01, min(2.0, float(data.get("dt_s", 0.1))))

    try:
        # 1) Quick-Check: ist der Tisch im Original-Frame ueberhaupt sichtbar?
        gs = load_global()
        # Dummy-Setting-Objekt fuer is_table_visible
        from settings import Setting
        sett = Setting.from_dict(setting_data)
        table_visible, polygon_pct = is_table_visible(frame, sett, gs.felt_detect_pct)

        # 2) Rektifiziertes Bild immer rendern (User soll sehen was die Kamera sieht)
        H = compute_homography(corners)
        if H is None:
            return jsonify({"error": "homography failed"}), 400
        rectified = rectify_frame(frame, H)

        felt_hex = setting_data.get("felt_color_hex", "#1c5f3a")
        felt_tol = int(setting_data.get("felt_tolerance", 40))
        felt_bgr_arr = np.array(hex_to_bgr(felt_hex), dtype=np.float32)
        setup_bgr = [np.array(hex_to_bgr(h), dtype=np.float32) for h in ball_hexes]
        anchor_colors = dict(zip(BALL_CLASSES, setup_bgr))

        # JPEG vom rektifizierten Bild
        ok, buf = cv2.imencode(".jpg", rectified, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if not ok:
            return jsonify({"error": "encode failed"}), 500
        b64 = base64.b64encode(buf.tobytes()).decode("ascii")

        # Out-State (wird beim Tisch-weg unveraendert zurueckgegeben)
        out_balls_in_frame = {cls: None for cls in BALL_CLASSES}
        out_state = {
            "balls": {cls: in_balls.get(cls) for cls in BALL_CLASSES},
            "track_colors": {cls: in_colors.get(cls) for cls in BALL_CLASSES},
            "lost": {cls: int(in_lost.get(cls, 0)) for cls in BALL_CLASSES},
            "no_table_streak": in_no_table_streak,
        }

        # 3) Wenn kein Tisch sichtbar -> kein Tracking, State bleibt zunaechst,
        #    aber no_table_streak hochzaehlen. Wenn es lange dauert (> N Frames),
        #    State komplett resetten damit Trail im Frontend geleert wird.
        if not table_visible:
            out_state["no_table_streak"] = in_no_table_streak + 1
            # Schwelle: ca. 2 Sekunden bei 5 fps Detection-Rate
            if out_state["no_table_streak"] > 10 and any(
                in_balls.get(cls) is not None for cls in BALL_CLASSES
            ):
                out_state["balls"] = {cls: None for cls in BALL_CLASSES}
                out_state["track_colors"] = {cls: None for cls in BALL_CLASSES}
                out_state["lost"] = {cls: 0 for cls in BALL_CLASSES}
            return jsonify({
                "rectified_b64": f"data:image/jpeg;base64,{b64}",
                "rect_w": rectified.shape[1],
                "rect_h": rectified.shape[0],
                "table_visible": False,
                "felt_pct_polygon": round(polygon_pct, 1),
                "felt_pct_rect": 0.0,
                "blob_count": 0,
                "balls": out_balls_in_frame,
                "found_balls": 0,
                "status": "no_table",
                "init_eligible": False,
                "tracking_state": out_state,
            })

        # Tisch wieder sichtbar → Streak zuruecksetzen
        out_state["no_table_streak"] = 0

        # 4) Filz-Quote im rektifizierten Bild (konsistent mit Mask)
        felt_pct_rect = felt_percentage(rectified, felt_bgr_arr, felt_tol)

        # 5) Blobs finden
        blobs = find_blobs(rectified, felt_bgr_arr, felt_tol)
        blob_count = len(blobs)

        # 6) Init-Status feststellen
        have_init = all(in_balls.get(cls) is not None for cls in BALL_CLASSES)
        init_eligible = (felt_pct_rect >= gs.init_min_felt_pct and blob_count >= 3)

        if not have_init:
            # Init-Versuch
            if not init_eligible:
                return jsonify({
                    "rectified_b64": f"data:image/jpeg;base64,{b64}",
                    "rect_w": rectified.shape[1],
                    "rect_h": rectified.shape[0],
                    "table_visible": True,
                    "felt_pct_polygon": round(polygon_pct, 1),
                    "felt_pct_rect": round(felt_pct_rect, 1),
                    "blob_count": blob_count,
                    "balls": out_balls_in_frame,
                    "found_balls": 0,
                    "status": "waiting_init",
                    "init_eligible": False,
                    "tracking_state": out_state,
                })
            assignment, total_dist = assign_balls_3class(blobs, setup_bgr)
            if len(assignment) < 3:
                return jsonify({
                    "rectified_b64": f"data:image/jpeg;base64,{b64}",
                    "rect_w": rectified.shape[1],
                    "rect_h": rectified.shape[0],
                    "table_visible": True,
                    "felt_pct_polygon": round(polygon_pct, 1),
                    "felt_pct_rect": round(felt_pct_rect, 1),
                    "blob_count": blob_count,
                    "balls": out_balls_in_frame,
                    "found_balls": 0,
                    "status": "waiting_init",
                    "init_eligible": True,
                    "tracking_state": out_state,
                })
            # Init erfolgreich
            for cls in BALL_CLASSES:
                b = assignment[cls]
                out_balls_in_frame[cls] = [round(b["x"], 1), round(b["y"], 1)]
                out_state["balls"][cls] = [b["x"], b["y"]]
                out_state["track_colors"][cls] = b["color"].tolist()
                out_state["lost"][cls] = 0
            return jsonify({
                "rectified_b64": f"data:image/jpeg;base64,{b64}",
                "rect_w": rectified.shape[1],
                "rect_h": rectified.shape[0],
                "table_visible": True,
                "felt_pct_polygon": round(polygon_pct, 1),
                "felt_pct_rect": round(felt_pct_rect, 1),
                "blob_count": blob_count,
                "balls": out_balls_in_frame,
                "found_balls": 3,
                "status": "tracking",
                "init_eligible": True,
                "tracking_state": out_state,
            })

        # 7) Normales Tracking
        max_disp_px = gs.v_max_mps * dt_s * PX_PER_M
        last_positions = {}
        track_colors_np = {}
        lost_counters = {}
        for cls in BALL_CLASSES:
            pos = in_balls.get(cls)
            if pos:
                last_positions[cls] = (pos[0], pos[1])
            col = in_colors.get(cls)
            if col:
                track_colors_np[cls] = np.array(col, dtype=np.float32)
            else:
                track_colors_np[cls] = anchor_colors[cls].copy()
            lost_counters[cls] = int(in_lost.get(cls, 0))

        picks = track_one_step(
            blobs, last_positions, track_colors_np, lost_counters,
            max_disp_px, gs.max_ball_lost_frames,
        )

        found = 0
        for cls in BALL_CLASSES:
            picked = picks.get(cls)
            if picked is not None:
                out_balls_in_frame[cls] = [round(picked["x"], 1), round(picked["y"], 1)]
                out_state["balls"][cls] = [picked["x"], picked["y"]]
                new_color = blend_color_bounded(
                    track_colors_np[cls], picked["color"],
                    gs.color_adaptation_rate,
                    anchor_colors[cls], gs.max_color_drift_bgr,
                )
                out_state["track_colors"][cls] = new_color.tolist()
                out_state["lost"][cls] = 0
                found += 1
            else:
                # Position + Farbe behalten, lost-Counter hoch
                out_state["lost"][cls] = lost_counters[cls] + 1

        # Auto-Re-Init: wenn alle Baelle deutlich ueber max_ball_lost_frames verloren
        # sind (z.B. > 4x), State zuruecksetzen damit beim naechsten geeigneten
        # Frame neu initialisiert wird
        if all(out_state["lost"][cls] > gs.max_ball_lost_frames * 4 for cls in BALL_CLASSES):
            out_state = {
                "balls": {cls: None for cls in BALL_CLASSES},
                "track_colors": {cls: None for cls in BALL_CLASSES},
                "lost": {cls: 0 for cls in BALL_CLASSES},
                "no_table_streak": 0,
            }

        if found == 3:
            status = "tracking"
        elif found > 0:
            status = "tracking_partial"
        else:
            status = "tracking_partial"

        return jsonify({
            "rectified_b64": f"data:image/jpeg;base64,{b64}",
            "rect_w": rectified.shape[1],
            "rect_h": rectified.shape[0],
            "table_visible": True,
            "felt_pct_polygon": round(polygon_pct, 1),
            "felt_pct_rect": round(felt_pct_rect, 1),
            "blob_count": blob_count,
            "balls": out_balls_in_frame,
            "found_balls": found,
            "status": status,
            "init_eligible": init_eligible,
            "tracking_state": out_state,
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ---- Tracking -----------------------------------------------------------

@app.post("/api/track")
def api_track():
    data = request.get_json(force=True)
    vid = data.get("video_id")
    sid = data.get("setting_id")
    if not vid or not sid:
        return jsonify({"error": "video_id and setting_id required"}), 400
    ok = start_tracking(vid, sid)
    if not ok:
        return jsonify({"error": "tracking already running for this video"}), 409
    return jsonify({"ok": True})


# ---- Config -------------------------------------------------------------

@app.get("/api/config")
def api_config_get():
    return jsonify(_config)


@app.post("/api/config")
def api_config_save():
    global _config
    data = request.get_json(force=True)
    new_dir = data.get("download_dir")
    if new_dir:
        # Pfad validieren bevor wir ihn speichern
        try:
            Path(new_dir).mkdir(parents=True, exist_ok=True)
        except (OSError, FileNotFoundError) as e:
            return jsonify({
                "error": f"Pfad nicht erreichbar: {e}"
            }), 400
    _config.update({k: v for k, v in data.items() if k in ("download_dir",)})
    save_config(_config)
    return jsonify(_config)


# ---- Main ---------------------------------------------------------------

def open_browser():
    time.sleep(1.0)
    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    print(f"yt_archive startet ... DB: {DB_PATH}")
    print(f"Download-Ordner: {get_download_dir()}")
    print(f"Server: http://127.0.0.1:5000")
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
