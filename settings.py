"""Settings für yt_archive: Tracking-Setups mit manuell gesetzten Tisch-Ecken
und Ball-Farben, plus globale Tracking-Parameter."""
from __future__ import annotations
import json
import uuid
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

APP_DIR = Path.home() / ".yt_archive"
APP_DIR.mkdir(exist_ok=True)
SETTINGS_PATH = APP_DIR / "settings.json"
GLOBAL_PATH = APP_DIR / "global.json"


@dataclass
class Setting:
    """Ein Tracking-Setup: manuell gesetzte Tisch-Ecken + Ball-Farben für genau
    einen Kamera-Winkel. Settings sind frei benennbar, keine Auto-Match-Logik."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "neues setting"
    # 4 Tisch-Ecken in Original-Frame-Pixeln, Reihenfolge: TL, TR, BR, BL
    table_corners: list = field(default_factory=list)
    felt_color_hex: str = "#1c5f3a"
    felt_tolerance: int = 40
    # [weiss, gelb, rot] in dieser festen Reihenfolge
    ball_colors_hex: list = field(default_factory=list)
    reference_video_id: str = ""
    reference_frame_time: float = 0.0
    reference_frame_size: list = field(default_factory=lambda: [1920, 1080])
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    last_used_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Setting":
        filtered = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        # Leere id raus, damit default_factory eine neue generiert
        if not filtered.get("id"):
            filtered.pop("id", None)
        return cls(**filtered)

    def is_complete(self) -> bool:
        return (len(self.table_corners) == 4
                and len(self.ball_colors_hex) == 3
                and all(isinstance(c, str) and c.startswith("#") for c in self.ball_colors_hex))


@dataclass
class GlobalSettings:
    """Tracking-Parameter, die für alle Settings gelten. Physikalische Konstanten
    und Schwellen, die der User normalerweise nicht anfassen muss."""
    min_clip_frames: int = 25
    max_gap_frames: int = 15
    felt_detect_pct: float = 60.0     # % Sample-Punkte die Filz sein müssen für "Tisch sichtbar"
    init_min_felt_pct: float = 70.0   # min. Filz-Anteil im rektifizierten Bild für Pivot-Kandidat
    init_sample_interval_s: float = 1.0  # Pivot-Suche: alle X Sekunden ein Sample
    preview_interval: int = 15        # _preview.jpg alle X Frames updaten
    v_max_mps: float = 7.0            # max. Ballgeschwindigkeit für Tracking-Radius
    color_adaptation_rate: float = 0.2  # EMA-Alpha für Ball-Farbe (0 = keine Anpassung, 1 = nur letzte)
    max_color_drift_bgr: float = 60.0 # Bound: Tracking-Farbe darf nie weiter weg von Setup-Farbe
    max_ball_lost_frames: int = 5     # nach X verlorenen Frames wird Such-Radius nicht weiter vergrößert

    # ---- Clip-Splitting / Stillstands-Detektion (Paket B) ----
    min_clip_duration_s: float = 7.0       # Sub-Clips kürzer als das werden verworfen
    stillness_max_window_px: float = 3.0   # Max Positions-Spannweite im Fenster damit "still"
    stillness_window_frames: int = 10      # Fenster-Größe (frames) für Stillstands-Check
    stillness_min_duration_s: float = 1.0  # Min Dauer um als Stillstands-Phase zu zählen

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GlobalSettings":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---- Persistence ---------------------------------------------------------

def list_settings() -> list[Setting]:
    if not SETTINGS_PATH.exists():
        return []
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        settings = [Setting.from_dict(d) for d in data]
        # Migration: wenn welche eine neue id bekommen haben (war leer im File),
        # einmal zuruecksichern damit sie persistent bleibt.
        if any(not d.get("id") for d in data):
            print("[settings] Migration: ergaenze fehlende Setting-IDs")
            save_settings(settings)
        return settings
    except Exception as e:
        print(f"[settings] read failed: {e}")
        return []


def save_settings(settings: list[Setting]):
    SETTINGS_PATH.write_text(
        json.dumps([s.to_dict() for s in settings], indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def get_setting(sid: str) -> Setting | None:
    for s in list_settings():
        if s.id == sid:
            return s
    return None


def upsert_setting(setting: Setting) -> Setting:
    settings = list_settings()
    for i, s in enumerate(settings):
        if s.id == setting.id:
            settings[i] = setting
            save_settings(settings)
            return setting
    settings.append(setting)
    save_settings(settings)
    return setting


def delete_setting(sid: str) -> bool:
    settings = list_settings()
    n = len(settings)
    settings = [s for s in settings if s.id != sid]
    if len(settings) < n:
        save_settings(settings)
        return True
    return False


def touch_setting(sid: str):
    settings = list_settings()
    for s in settings:
        if s.id == sid:
            s.last_used_at = time.strftime("%Y-%m-%d %H:%M:%S")
            save_settings(settings)
            return


def load_global() -> GlobalSettings:
    if not GLOBAL_PATH.exists():
        g = GlobalSettings()
        save_global(g)
        return g
    try:
        data = json.loads(GLOBAL_PATH.read_text(encoding="utf-8"))
        return GlobalSettings.from_dict(data)
    except Exception as e:
        print(f"[settings] global read failed: {e}")
        return GlobalSettings()


def save_global(g: GlobalSettings):
    GLOBAL_PATH.write_text(
        json.dumps(g.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


# ---- Color helpers -------------------------------------------------------

def hex_to_bgr(hex_str: str) -> tuple[int, int, int]:
    """Convert '#rrggbb' -> (B, G, R) tuple."""
    if not hex_str or not hex_str.startswith("#"):
        return (0, 0, 0)
    h = hex_str.lstrip("#")
    if len(h) != 6:
        return (0, 0, 0)
    try:
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
        return (b, g, r)
    except ValueError:
        return (0, 0, 0)


def bgr_to_hex(bgr) -> str:
    """Convert BGR tuple/array -> '#rrggbb' string."""
    b, g, r = int(bgr[0]), int(bgr[1]), int(bgr[2])
    return f"#{r:02x}{g:02x}{b:02x}"
