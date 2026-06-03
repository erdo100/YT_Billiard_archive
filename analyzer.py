"""Carambol-Tracking-Analyzer mit manueller Tisch-Geometrie und bidirektionalem
Pivot-Tracking. 2-Pass Algorithmus:
  Pass 1: Streaming durchs Video, identifiziere zusammenhaengende Tisch-sichtbar-Bereiche
  Pass 2: Pro Bereich: Frames in JPEG-Memory laden, Pivot finden (bester Init-Frame),
          vom Pivot aus vor- und zurueck-tracken mit Bewegungs-Constraint
"""
from __future__ import annotations
import json
import time
import cv2
import numpy as np
from pathlib import Path
from itertools import permutations, combinations

from settings import Setting, GlobalSettings, hex_to_bgr, bgr_to_hex


# ---- Konstanten ----------------------------------------------------------

# Carambol-Tisch: 2.84 m x 1.42 m (Spielfeld innen, Standard-Match-Tisch)
TABLE_W_MM = 2840
TABLE_H_MM = 1420
SCALE = 0.5                              # 1 px = 2 mm
OUT_W = int(TABLE_W_MM * SCALE)          # 1420
OUT_H = int(TABLE_H_MM * SCALE)          # 710
PX_PER_M = 1000.0 * SCALE                # 500 px/m

# Carambol-Bälle: ~61 mm Durchmesser
BALL_RADIUS_MM = 30
BALL_RADIUS_PX = max(1, int(BALL_RADIUS_MM * SCALE))   # 15

# Rand-Inset im rektifizierten Bild — verhindert dass die Bande (falls die
# User-Ecken nicht 100% praezise auf dem Filz-Rand sitzen) als Non-Filz-Pixel
# erkannt wird und mit Baellen verschmilzt. Wert in Pixeln im rektifizierten
# Bild (1 px ≈ 2 mm).
BORDER_INSET_PX = 8

# Visualisierungs-Rand fuer Clip-Output: zusaetzliche 20 cm in jede Richtung
# um den Tischrahmen / die Markierungen sichtbar zu machen. Wirkt sich NUR auf
# den gespeicherten Clip aus (Visualisierung), NICHT auf das Tracking selbst.
BORDER_VIZ_MM = 200                                   # 20 cm
BORDER_VIZ_PX = int(BORDER_VIZ_MM * SCALE)            # 100 px
VIZ_OUT_W = OUT_W + 2 * BORDER_VIZ_PX                 # 1620
VIZ_OUT_H = OUT_H + 2 * BORDER_VIZ_PX                 # 910


def rectify_frame_extended(frame: np.ndarray, H: np.ndarray) -> np.ndarray:
    """Wie rectify_frame, aber mit BORDER_VIZ_PX zusaetzlichem Rand drumherum.
    Spielfeld-Inneres landet bei [BORDER_VIZ_PX, BORDER_VIZ_PX+OUT_W] horizontal
    und [BORDER_VIZ_PX, BORDER_VIZ_PX+OUT_H] vertikal. Der Aussenrand zeigt
    den Tischrahmen / die Bande aus dem Originalbild (falls dort sichtbar).
    """
    T = np.array([[1.0, 0.0, BORDER_VIZ_PX],
                  [0.0, 1.0, BORDER_VIZ_PX],
                  [0.0, 0.0, 1.0]], dtype=np.float32)
    H_ext = T @ H
    return cv2.warpPerspective(frame, H_ext, (VIZ_OUT_W, VIZ_OUT_H))


def _viz_pt(pos) -> tuple[int, int]:
    """Tracking-Koord (OUT_W x OUT_H) → Visualisierungs-Koord (VIZ_OUT_W x VIZ_OUT_H)."""
    return (int(pos[0]) + BORDER_VIZ_PX, int(pos[1]) + BORDER_VIZ_PX)

# Ball-Klassen in fester Reihenfolge (Index entspricht setting.ball_colors_hex[i])
BALL_CLASSES = ["weiss", "gelb", "rot"]

# Visualisierung
VIZ_COLORS = {
    "weiss": (240, 240, 240),
    "gelb":  (40, 215, 230),
    "rot":   (40, 40, 230),
}
TRAIL_FRAMES = 30
JPEG_QUALITY = 85


# ---- Pass-1-Helpers: schneller Tisch-Sichtbarkeits-Check -----------------

def is_table_visible(frame: np.ndarray, setting: Setting,
                     threshold_pct: float) -> tuple[bool, float]:
    """9-Punkt-Sample im Setup-Polygon. Schnell, kein Rektifizieren.
    Returns: (visible_bool, gemessene_pct)
    """
    corners = np.array(setting.table_corners, dtype=np.float32)
    if len(corners) != 4:
        return False, 0.0

    felt_bgr = np.array(hex_to_bgr(setting.felt_color_hex), dtype=np.float32)
    tol = setting.felt_tolerance
    h, w = frame.shape[:2]
    tl, tr, br, bl = corners

    hits = 0
    total = 0
    for u in (0.2, 0.5, 0.8):
        for v in (0.2, 0.5, 0.8):
            top = tl + (tr - tl) * u
            bot = bl + (br - bl) * u
            p = top + (bot - top) * v
            px, py = int(p[0]), int(p[1])
            if 0 <= px < w and 0 <= py < h:
                total += 1
                pixel = frame[py, px].astype(np.float32)
                if np.linalg.norm(pixel - felt_bgr) <= tol:
                    hits += 1
    if total == 0:
        return False, 0.0
    pct = 100.0 * hits / total
    return pct >= threshold_pct, pct


# ---- Geometrie ----------------------------------------------------------

def compute_homography(corners: list) -> np.ndarray | None:
    """4 Setup-Punkte (TL, TR, BR, BL) -> Homography in OUT_W x OUT_H Rect."""
    if len(corners) != 4:
        return None
    src = np.array(corners, dtype=np.float32)
    dst = np.array([
        [0, 0],
        [OUT_W - 1, 0],
        [OUT_W - 1, OUT_H - 1],
        [0, OUT_H - 1],
    ], dtype=np.float32)
    H, _ = cv2.findHomography(src, dst)
    return H


def rectify_frame(frame: np.ndarray, H: np.ndarray) -> np.ndarray:
    return cv2.warpPerspective(frame, H, (OUT_W, OUT_H))


# ---- Blob-Detection im rektifizierten Tisch ------------------------------

def _compute_felt_mask(rectified: np.ndarray, felt_bgr: np.ndarray,
                       felt_tolerance: int) -> np.ndarray:
    """Felt-Mask: 255 wo NICHT Filz (Ball-Kandidat), 0 wo Filz.

    Statt euklidischer BGR-Distanz wird im LAB-Farbraum nur die Chrominanz
    (a, b) verglichen — die Lightness-Komponente (L) wird IGNORIERT. Dadurch
    sind Schatten / Beleuchtungsschwankungen toleriert, solange die Farbe
    (grün) gleich bleibt. Der border_inset_px-Ring am Rand wird als Filz
    markiert, damit die Bande nicht als Blob auftaucht.
    """
    # In LAB konvertieren
    lab = cv2.cvtColor(rectified, cv2.COLOR_BGR2LAB)
    felt_bgr_u8 = np.uint8(np.clip(felt_bgr, 0, 255).reshape(1, 1, 3))
    felt_lab = cv2.cvtColor(felt_bgr_u8, cv2.COLOR_BGR2LAB)[0, 0]

    a_diff = lab[:, :, 1].astype(np.int16) - int(felt_lab[1])
    b_diff = lab[:, :, 2].astype(np.int16) - int(felt_lab[2])
    ab_dist = np.sqrt(a_diff * a_diff + b_diff * b_diff)

    # Tolerance war historisch fuer BGR-Distanz (0..441) — fuer LAB-ab (0..360)
    # ist eine etwas kleinere Skala natuerlich. Wir behalten den User-Wert,
    # mappen aber konservativ: tol_lab = tol_bgr * 0.7
    tol_lab = max(8.0, felt_tolerance * 0.7)
    mask = (ab_dist > tol_lab).astype(np.uint8) * 255

    # Morph-Cleanup
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    # Rand-Inset: alles am Rand als Filz markieren (Banden ausblenden)
    h, w = mask.shape
    bi = BORDER_INSET_PX
    if bi > 0:
        mask[:bi, :] = 0
        mask[h - bi:, :] = 0
        mask[:, :bi] = 0
        mask[:, w - bi:] = 0

    return mask


def _try_split_cluster(rectified: np.ndarray, blob_mask: np.ndarray,
                       expected_area: float, a_min: float, a_max: float
                       ) -> list[dict]:
    """Versucht einen zusammenhaengenden Blob (mehrere Baelle die sich
    beruehren oder leicht ueberlappen) in Sub-Blobs zu zerlegen via
    Distance-Transform und Schwellwert-Detektion der lokalen Maxima.
    Returns: Liste von Sub-Blob-Dicts (kann leer sein wenn nicht teilbar).
    """
    # Distance transform: pro Pixel die Distanz zum naechsten 0-Pixel
    dist = cv2.distanceTransform(blob_mask, cv2.DIST_L2, 3)
    max_d = float(dist.max())
    if max_d < 4.0:
        return []

    # Lokale Maxima isolieren — ueber Schwellwert. Wir probieren mehrere
    # Schwellen, weil bei stark verschmolzenen Baellen ein hoeherer Wert
    # noetig ist um sie zu trennen.
    sub_blobs = []
    for thresh_ratio in (0.55, 0.65, 0.75, 0.85):
        threshold = thresh_ratio * max_d
        peaks_mask = (dist >= threshold).astype(np.uint8) * 255
        n_peaks, peak_labels, peak_stats, peak_centroids = cv2.connectedComponentsWithStats(peaks_mask)
        n_real_peaks = n_peaks - 1  # ohne Background
        if n_real_peaks >= 2:
            # Mehrere Peaks gefunden → das sind unsere Ball-Zentren
            for j in range(1, n_peaks):
                cx, cy = peak_centroids[j]
                rad = max(3, int(BALL_RADIUS_PX * 0.6))
                y0 = max(0, int(cy) - rad)
                y1 = min(rectified.shape[0], int(cy) + rad + 1)
                x0 = max(0, int(cx) - rad)
                x1 = min(rectified.shape[1], int(cx) + rad + 1)
                patch = rectified[y0:y1, x0:x1].reshape(-1, 3)
                if patch.size == 0:
                    continue
                color = patch.mean(axis=0).astype(np.float32)
                sub_blobs.append({
                    "x": float(cx),
                    "y": float(cy),
                    "area": int(expected_area),
                    "color": color,
                })
            return sub_blobs
    return sub_blobs


def felt_percentage(rectified: np.ndarray, felt_bgr: np.ndarray,
                    felt_tolerance: int) -> float:
    """Liefert in % wie viele Pixel des rektifizierten Bilds Filz sind.
    Konsistent mit der Mask-Logik in _compute_felt_mask (LAB ab-Distanz).
    """
    mask = _compute_felt_mask(rectified, felt_bgr, felt_tolerance)
    non_felt = (mask > 0).sum()
    total = mask.size
    return 100.0 * (total - non_felt) / max(total, 1)


def find_blobs(rectified: np.ndarray, felt_bgr: np.ndarray, felt_tolerance: int,
               min_area_factor: float = 0.2, max_area_factor: float = 4.0
               ) -> list[dict]:
    """Alle Luecken im Filz als Ball-Kandidaten. Zu grosse Blobs (mehrere
    Baelle die sich beruehren) werden via Erosion gesplittet.
    Returns: list of {x, y, area, color (BGR float32)}
    """
    mask = _compute_felt_mask(rectified, felt_bgr, felt_tolerance)

    num, labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
    expected = np.pi * (BALL_RADIUS_PX ** 2)
    a_min = expected * min_area_factor
    a_max = expected * max_area_factor
    a_cluster = expected * 1.6   # ab dieser Groesse: cluster-splitting versuchen

    blobs = []
    for i in range(1, num):
        area = int(stats[i, cv2.CC_STAT_AREA])
        # Komplett zu klein oder absurd gross
        if area < a_min or area > a_max * 2:
            continue

        blob_pixmask = (labels == i)
        color = rectified[blob_pixmask].mean(axis=0).astype(np.float32)
        cx, cy = centroids[i]

        if area <= a_cluster:
            # Normaler Single-Blob
            if area <= a_max:
                blobs.append({
                    "x": float(cx),
                    "y": float(cy),
                    "area": area,
                    "color": color,
                })
            continue

        # Cluster-Verdacht: versuche zu splitten
        single_mask = blob_pixmask.astype(np.uint8) * 255
        sub_blobs = _try_split_cluster(rectified, single_mask, expected, a_min, a_max)
        if sub_blobs:
            blobs.extend(sub_blobs)
        elif area <= a_max:
            # Splitting fehlgeschlagen, aber Original noch im Area-Bereich → behalten
            blobs.append({
                "x": float(cx),
                "y": float(cy),
                "area": area,
                "color": color,
            })

    return blobs


# ---- Pivot-Finder --------------------------------------------------------

def assign_balls_3class(blobs: list[dict], setup_ball_bgr: list[np.ndarray]
                        ) -> tuple[dict, float]:
    """Beste 1:1 Zuordnung von 3 Blobs zu 3 Ball-Klassen.
    Wenn <3 Blobs: greedy (partial assignment).
    Returns: (dict {class: blob}, total_distance)
    """
    if len(blobs) < 3:
        # Greedy fallback
        used = set()
        result = {}
        for ci, target in enumerate(setup_ball_bgr):
            best_idx, best_dist = None, float("inf")
            for bi, b in enumerate(blobs):
                if bi in used:
                    continue
                d = float(np.linalg.norm(b["color"] - target))
                if d < best_dist:
                    best_dist, best_idx = d, bi
            if best_idx is not None:
                result[BALL_CLASSES[ci]] = blobs[best_idx]
                used.add(best_idx)
        total = sum(
            float(np.linalg.norm(result[c]["color"] - setup_ball_bgr[i]))
            for i, c in enumerate(BALL_CLASSES) if c in result
        )
        return result, total

    # Exhaustive: alle 3-Blob-Tripel x alle 3!-Permutationen
    n = len(blobs)
    cost = np.zeros((n, 3), dtype=np.float64)
    for i, b in enumerate(blobs):
        for j, t in enumerate(setup_ball_bgr):
            cost[i, j] = np.linalg.norm(b["color"] - t)

    best_total = float("inf")
    best_assign = {}
    for triple in combinations(range(n), 3):
        for perm in permutations(range(3)):
            total = sum(cost[triple[i], perm[i]] for i in range(3))
            if total < best_total:
                best_total = total
                best_assign = {
                    BALL_CLASSES[perm[i]]: blobs[triple[i]]
                    for i in range(3)
                }
    return best_assign, float(best_total)


def compute_init_quality(frame: np.ndarray, H: np.ndarray,
                         setting: Setting, gs: GlobalSettings
                         ) -> tuple[float, dict | None]:
    """Init-Quality-Score fuer einen Frame. Hoeher = besserer Pivot-Kandidat.
    Returns: (score, ball_assignment | None). None wenn disqualifiziert.
    """
    rectified = rectify_frame(frame, H)
    felt_bgr = np.array(hex_to_bgr(setting.felt_color_hex), dtype=np.float32)

    felt_pct = felt_percentage(rectified, felt_bgr, setting.felt_tolerance)
    if felt_pct < gs.init_min_felt_pct:
        return -1e9, None

    blobs = find_blobs(rectified, felt_bgr, setting.felt_tolerance)
    if len(blobs) < 3:
        return -1e9, None

    setup_bgr = [np.array(hex_to_bgr(h), dtype=np.float32) for h in setting.ball_colors_hex]
    assignment, total_dist = assign_balls_3class(blobs, setup_bgr)

    if len(assignment) < 3:
        return -1e9, None

    extra_penalty = max(0, len(blobs) - 3) * 10
    score = felt_pct - total_dist * 0.5 - extra_penalty
    return float(score), assignment


def find_pivot(clip_frames: list[bytes], fps: float,
               setting: Setting, gs: GlobalSettings
               ) -> tuple[int | None, dict | None]:
    """Sucht in einem Clip nach dem besten Pivot-Frame.
    Returns: (pivot_idx_in_clip | None, pivot_ball_assignment | None)
    """
    H = compute_homography(setting.table_corners)
    if H is None:
        return None, None

    interval = max(1, int(round(fps * gs.init_sample_interval_s)))
    best_score = -1e9
    best_idx = None
    best_assign = None

    for i in range(0, len(clip_frames), interval):
        frame = decode_jpeg(clip_frames[i])
        if frame is None:
            continue
        score, assignment = compute_init_quality(frame, H, setting, gs)
        if assignment is not None and score > best_score:
            best_score = score
            best_idx = i
            best_assign = assignment

    return best_idx, best_assign


# ---- Tracking-Schritt ---------------------------------------------------

def track_one_step(blobs: list[dict], last_positions: dict,
                   tracking_colors: dict, lost_counters: dict,
                   max_disp_px: float, max_lost: int,
                   color_weight: float = 0.5) -> dict:
    """Ordnet pro Klasse einen Blob zu (oder None).
    Suche-Radius waechst mit lost_counter (bis max_lost x max_disp_px).
    Verteilung greedy: bester (score) Pick zuerst, dann naechster.
    """
    # Pro Klasse: alle Kandidaten in Reichweite sammeln
    per_class_candidates = {}  # cls -> list of (score, blob_idx)
    for cls in BALL_CLASSES:
        last_pos = last_positions.get(cls)
        track_color = tracking_colors.get(cls)
        if last_pos is None or track_color is None:
            per_class_candidates[cls] = []
            continue

        lost = lost_counters.get(cls, 0)
        search_radius = max_disp_px * (1 + min(lost, max_lost))

        cands = []
        for bi, b in enumerate(blobs):
            dx = b["x"] - last_pos[0]
            dy = b["y"] - last_pos[1]
            dist_px = (dx * dx + dy * dy) ** 0.5
            if dist_px > search_radius:
                continue
            color_dist = float(np.linalg.norm(b["color"] - track_color))
            dist_norm = dist_px / max(search_radius, 1.0)
            color_norm = min(color_dist / 80.0, 2.0)
            score = dist_norm + color_weight * color_norm
            cands.append((score, bi))
        cands.sort()
        per_class_candidates[cls] = cands

    # Greedy global: alle (score, cls, blob_idx) sortieren und nacheinander vergeben
    all_picks = []
    for cls, cands in per_class_candidates.items():
        for score, bi in cands:
            all_picks.append((score, cls, bi))
    all_picks.sort()

    assigned = {}
    used_blobs = set()
    used_classes = set()
    for score, cls, bi in all_picks:
        if cls in used_classes or bi in used_blobs:
            continue
        assigned[cls] = blobs[bi]
        used_classes.add(cls)
        used_blobs.add(bi)
        if len(used_classes) == 3:
            break

    return {cls: assigned.get(cls) for cls in BALL_CLASSES}


def blend_color_bounded(current: np.ndarray, measured: np.ndarray,
                        alpha: float, anchor: np.ndarray,
                        max_drift: float) -> np.ndarray:
    """EMA-Blend mit harter Drift-Bound zur Anchor-Farbe."""
    new = current * (1.0 - alpha) + measured * alpha
    delta = new - anchor
    drift = float(np.linalg.norm(delta))
    if drift > max_drift:
        new = anchor + delta * (max_drift / drift)
    return new.astype(np.float32)


# ---- Bidirektionales Tracking -------------------------------------------

def find_stillness_periods(tracks: dict, fps: float,
                           gs: GlobalSettings) -> list[tuple[int, int]]:
    """Findet Frame-Bereiche in denen ALLE 3 Baelle ruhen.

    Definition "Ball ruht in Frame i": ueber die letzten `stillness_window_frames`
    Positionen (innerhalb von tracks) ist die Spannweite max./min. in x UND y
    kleiner als `stillness_max_window_px`. None-Positionen werden uebersprungen;
    falls zu wenige Datenpunkte im Fenster, gilt "nicht still" (konservativ).

    Returns: Liste von (start_frame, end_frame) — beide Indizes inklusive,
             im clip-internen Frame-System (0-basiert).
    """
    if not tracks:
        return []
    all_frames: set[int] = set()
    for cls_data in tracks.values():
        all_frames.update(fi for fi in cls_data.keys())
    if not all_frames:
        return []
    f_min, f_max = min(all_frames), max(all_frames)

    win = max(3, int(gs.stillness_window_frames or 10))
    thresh = float(gs.stillness_max_window_px or 3.0)

    def ball_still_at(cls_data: dict, i: int) -> bool:
        positions = []
        for j in range(max(f_min, i - win + 1), i + 1):
            p = cls_data.get(j)
            if p is not None:
                positions.append(p)
        if len(positions) < max(3, win // 2):  # Mindestens halbes Fenster, sonst unzuverlaessig
            return False
        xs = [p[0] for p in positions]
        ys = [p[1] for p in positions]
        return (max(xs) - min(xs) <= thresh) and (max(ys) - min(ys) <= thresh)

    # Pro Frame: sind alle Baelle still?
    still_per_frame: list[bool] = []
    for i in range(f_min, f_max + 1):
        all_still = all(ball_still_at(tracks[cls], i) for cls in BALL_CLASSES)
        still_per_frame.append(all_still)

    # Zusammenhaengende True-Bereiche extrahieren
    min_dur_frames = max(2, int(round((gs.stillness_min_duration_s or 1.0) * fps)))
    periods: list[tuple[int, int]] = []
    in_run = False
    run_start = 0
    for idx, still in enumerate(still_per_frame):
        frame_abs = f_min + idx
        if still and not in_run:
            in_run = True
            run_start = frame_abs
        elif (not still) and in_run:
            in_run = False
            run_end = frame_abs - 1
            if (run_end - run_start + 1) >= min_dur_frames:
                periods.append((run_start, run_end))
    if in_run:
        run_end = f_max
        if (run_end - run_start + 1) >= min_dur_frames:
            periods.append((run_start, run_end))

    return periods


def split_tracks_into_subclips(tracks: dict, fps: float, gs: GlobalSettings,
                               range_n_frames: int
                               ) -> list[dict]:
    """Zerlegt einen getrackten Range in Sub-Clips an Stillstands-Phasen.

    Logik:
    - Stillstand-Phasen markieren die Punkte zwischen denen ein Stoss passiert.
    - Sub-Clip-Start = bis zu LOOKBACK_S vor dem Ende der vorigen Stillstand-Phase
      (so weit die Stillstand-Phase zurueckreicht). Dadurch hat der Clip immer
      eine kurze Ruhe-Phase am Anfang, in der die Start-Positionen sauber
      gemittelt werden koennen.
    - Sub-Clip-Ende = Anfang der naechsten Stillstand-Phase.
    - Wenn keine Stillstandsphasen gefunden werden: ein Sub-Clip = ganzer Range.

    Returns: Liste von {start_frame, end_frame, has_pre_stillness, has_post_stillness}
             im clip-internen Frame-System (0-basiert, end inklusive).
    """
    if range_n_frames <= 0:
        return []
    f_min, f_max = 0, range_n_frames - 1
    LOOKBACK_S = 1.0
    lookback_frames = max(1, int(round(LOOKBACK_S * fps)))

    periods = find_stillness_periods(tracks, fps, gs)
    boundaries: list[tuple[int, int, bool, bool]] = []

    if not periods:
        return [{"start_frame": f_min, "end_frame": f_max,
                 "has_pre_stillness": False, "has_post_stillness": False}]

    # Vor erster Periode (Range fängt mit Bewegung an - kein Lookback möglich)
    if f_min < periods[0][0]:
        boundaries.append((f_min, periods[0][0], False, True))

    # Zwischen den Perioden — Start mit Lookback in die vorige Stillstands-Phase hinein,
    # ENDE bis incl. Ende der naechsten Stillstands-Phase (= Ruhepunkt nach Stoss sichtbar)
    for i in range(len(periods) - 1):
        period_start, period_end = periods[i]
        sub_start = max(period_start, period_end - lookback_frames)
        sub_end = periods[i + 1][1]   # Ende der naechsten Stillstands-Phase
        if sub_end > sub_start:
            boundaries.append((sub_start, sub_end, True, True))

    # Nach letzter Periode (Range endet ohne Stillstand)
    if periods[-1][1] < f_max:
        period_start, period_end = periods[-1]
        sub_start = max(period_start, period_end - lookback_frames)
        boundaries.append((sub_start, f_max, True, False))

    return [
        {"start_frame": s, "end_frame": e,
         "has_pre_stillness": pre, "has_post_stillness": post}
        for (s, e, pre, post) in boundaries
    ]


def slice_tracks(tracks: dict, start_frame: int, end_frame: int) -> dict:
    """Liefert ein neues tracks-Dict, beschnitten auf [start_frame..end_frame]
    und mit re-normalisierten Indizes (Sub-Clip-Anfang = 0).
    """
    out = {}
    for cls in BALL_CLASSES:
        cls_data = tracks.get(cls, {})
        out[cls] = {
            (fi - start_frame): pos
            for fi, pos in cls_data.items()
            if start_frame <= fi <= end_frame
        }
    return out


def track_bidirectional(clip_frames: list[bytes], pivot_idx: int,
                        pivot_assignment: dict, H: np.ndarray,
                        max_disp_px: float, setting: Setting,
                        gs: GlobalSettings) -> dict:
    """Vom Pivot aus vorwaerts UND rueckwaerts tracken.
    Returns: dict {class: {frame_idx_in_clip: (x, y)}}.
    Frames ohne Treffer haben kein Entry (oder None?). Wir nutzen None-Entry,
    damit die Frame-Liste komplett ist.
    """
    setup_bgr = [np.array(hex_to_bgr(h), dtype=np.float32)
                 for h in setting.ball_colors_hex]
    anchor_colors = dict(zip(BALL_CLASSES, setup_bgr))
    felt_bgr = np.array(hex_to_bgr(setting.felt_color_hex), dtype=np.float32)

    # Initialer State aus Pivot
    init_positions = {cls: (pivot_assignment[cls]["x"], pivot_assignment[cls]["y"])
                      for cls in BALL_CLASSES}
    init_track_colors = {cls: pivot_assignment[cls]["color"].copy()
                         for cls in BALL_CLASSES}

    tracks = {cls: {pivot_idx: init_positions[cls]} for cls in BALL_CLASSES}

    def run(start: int, stop: int, step: int):
        """Generischer Tracking-Loop in eine Richtung. start..stop (exclusiv) mit step."""
        last_pos = dict(init_positions)
        track_colors = {cls: init_track_colors[cls].copy() for cls in BALL_CLASSES}
        lost = {cls: 0 for cls in BALL_CLASSES}

        for i in range(start, stop, step):
            frame = decode_jpeg(clip_frames[i])
            if frame is None:
                for cls in BALL_CLASSES:
                    tracks[cls][i] = None
                    lost[cls] += 1
                continue
            rectified = rectify_frame(frame, H)
            blobs = find_blobs(rectified, felt_bgr, setting.felt_tolerance)
            picks = track_one_step(blobs, last_pos, track_colors, lost,
                                   max_disp_px, gs.max_ball_lost_frames)

            for cls in BALL_CLASSES:
                picked = picks[cls]
                if picked is not None:
                    tracks[cls][i] = (picked["x"], picked["y"])
                    last_pos[cls] = (picked["x"], picked["y"])
                    track_colors[cls] = blend_color_bounded(
                        track_colors[cls], picked["color"],
                        gs.color_adaptation_rate,
                        anchor_colors[cls], gs.max_color_drift_bgr,
                    )
                    lost[cls] = 0
                else:
                    tracks[cls][i] = None
                    lost[cls] += 1

    # Forward: pivot+1 ... len(clip_frames)
    run(pivot_idx + 1, len(clip_frames), 1)
    # Backward: pivot-1 ... -1
    run(pivot_idx - 1, -1, -1)
    return tracks


# ---- Pass 1: Clip-Bereiche identifizieren -------------------------------

class NoTopViewFoundException(Exception):
    """Wird geworfen wenn nach auto_fallback_seconds keine einzige Top-View-
    Sample-Position erkannt wurde. Aufrufer kann ein anderes Setting probieren."""
    pass


def scan_clip_ranges(video_path: str, setting: Setting, gs: GlobalSettings,
                     preview_path: Path | None = None,
                     progress_cb=None) -> tuple[list[tuple[int, int]], float, int]:
    """Pass 1 — sparse Sampling: alle `scan_sample_interval_s` Sekunden ein
    Frame pruefen, ob der Tisch sichtbar ist. Daraus zusammenhaengende
    Bereiche bilden (Single-Sample-Luecken werden toleriert).

    progress_cb wird mit ("scan", current_frame, total_frames, ranges_so_far) aufgerufen,
    sodass die UI live die bisher gefundenen Bereiche anzeigen kann.

    Returns: (ranges, fps, total_frames)
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Kann Video nicht oeffnen: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    sample_interval_s = max(0.5, float(gs.scan_sample_interval_s or 5.0))
    sample_step = max(1, int(round(sample_interval_s * fps)))

    samples: list[tuple[int, bool]] = []   # (frame_idx, visible)

    # Range-Tracking waehrend des Scans (live fuer progress_cb)
    live_ranges: list[tuple[int, int]] = []
    in_run = False
    run_start_f = 0
    last_visible_f = 0
    consecutive_invisible_after_visible = 0   # fuer Single-Sample-Glue
    any_visible_sample = False

    # Auto-Fallback-Schwelle: Wenn bis zu diesem Video-Frame KEIN einziger
    # visible-Sample gefunden wurde, bricht der Scan mit NoTopViewFoundException ab.
    fallback_s = float(getattr(gs, 'auto_fallback_seconds', 0.0) or 0.0)
    fallback_threshold_f = int(fallback_s * fps) if fallback_s > 0 else 0

    sample_idx = 0
    total_samples = max(1, total // sample_step)
    for f_idx in range(0, total, sample_step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
        ret, frame = cap.read()
        if not ret:
            break

        visible, pct = is_table_visible(frame, setting, gs.felt_detect_pct)
        samples.append((f_idx, visible))
        if visible:
            any_visible_sample = True

        # Online Range-Building mit 1-Sample-Glue
        if visible:
            if not in_run:
                in_run = True
                run_start_f = f_idx
            last_visible_f = f_idx
            consecutive_invisible_after_visible = 0
        else:
            if in_run:
                consecutive_invisible_after_visible += 1
                if consecutive_invisible_after_visible >= 2:
                    # Definitiver Bruch (zwei aufeinanderfolgende invisible Samples)
                    in_run = False
                    duration_frames = last_visible_f - run_start_f + 1
                    duration_s = duration_frames / fps
                    if duration_s >= gs.min_clip_duration_s * 0.3:
                        # Pass 1 ist locker — Min-Dauer wird in Pass 2 nochmal
                        # streng angewandt (auf Sub-Clip-Ebene)
                        live_ranges.append((run_start_f, last_visible_f))

        # Auto-Fallback-Check: nach fallback_threshold_f noch nichts visible?
        if (fallback_threshold_f > 0 and
                f_idx >= fallback_threshold_f and
                not any_visible_sample):
            cap.release()
            if progress_cb:
                progress_cb("scan", f_idx, total, [])
            raise NoTopViewFoundException(
                f"Nach {fallback_s:.0f}s Video keine Top-View mit diesem Setting"
            )

        # Preview-Bild aktualisieren
        if preview_path:
            write_scan_preview(frame, setting, f_idx, total,
                               sum(1 for s in samples if s[1]), pct, preview_path)

        if progress_cb:
            progress_cb("scan", f_idx, total, list(live_ranges))

        sample_idx += 1

    # Letzten offenen Run abschliessen
    if in_run:
        duration_s = (last_visible_f - run_start_f + 1) / fps
        if duration_s >= gs.min_clip_duration_s * 0.3:
            live_ranges.append((run_start_f, last_visible_f))

    cap.release()

    if progress_cb:
        progress_cb("scan", total, total, list(live_ranges))

    return live_ranges, fps, total


# ---- Frame-Memory --------------------------------------------------------

def load_clip_frames(video_path: str, start_frame: int, end_frame: int
                     ) -> list[bytes | None]:
    """Laedt Frame start_frame..end_frame (inklusiv) als JPEG in Memory."""
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    encode_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
    out = []
    for _ in range(end_frame - start_frame + 1):
        ret, frame = cap.read()
        if not ret:
            break
        success, buf = cv2.imencode(".jpg", frame, encode_params)
        out.append(buf.tobytes() if success else None)
    cap.release()
    return out


def decode_jpeg(jpeg_bytes: bytes | None) -> np.ndarray | None:
    if not jpeg_bytes:
        return None
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


# ---- Output: Clip-MP4 + JSON --------------------------------------------

def _convert_to_h264(input_path: str, output_path: str,
                     audio_video: str | None = None,
                     audio_start_s: float | None = None,
                     audio_duration_s: float | None = None) -> bool:
    """Konvertiert ein Video zu H.264/yuv420p via ffmpeg fuer Browser-Kompat.

    Wenn audio_video gesetzt ist: muxt das Audio aus diesem Video in den Output
    rein. audio_start_s / audio_duration_s definieren den Audio-Bereich. Bei
    Videos ohne Audio-Track wird der Output stumm (dank `?` in -map).

    OpenCV's mp4v-codiertes MP4 ist nicht in allen Browsern abspielbar.
    Returns True bei Erfolg, False wenn ffmpeg fehlt/fehlschlaegt.
    """
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        return False
    try:
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-i", input_path]

        # Optional: Audio-Quelle mit Seek + Duration vor dem -i (= input-options)
        want_audio = (audio_video is not None and
                      audio_start_s is not None and
                      audio_duration_s is not None)
        if want_audio:
            cmd.extend(["-ss", f"{audio_start_s:.3f}",
                        "-t", f"{audio_duration_s:.3f}",
                        "-i", audio_video])

        cmd.extend([
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
        ])

        if want_audio:
            cmd.extend([
                "-c:a", "aac", "-b:a", "128k",
                "-map", "0:v:0",     # Video aus input 0 (silent rendering)
                "-map", "1:a:0?",    # Audio aus input 1 (optional, `?` = kein Fehler wenn kein Track)
                "-shortest",         # auf kürzeren Stream begrenzen (Video definiert Länge)
            ])

        cmd.append(output_path)
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            print(f"[ffmpeg] returncode {result.returncode}: {result.stderr.decode(errors='ignore')[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[ffmpeg] convert failed: {e}")
        return False


def write_clip_outputs(clip_idx: int, tracks: dict, clip_frames: list[bytes],
                       H: np.ndarray, fps: float, start_frame_in_video: int,
                       pivot_idx: int, output_dir: Path, setting: Setting,
                       source_video_path: str | None = None
                       ) -> dict:
    """Schreibt clipNN.mp4, clipNN.json, clipNN_thumb.jpg.

    Visualisierung im Clip:
    - Erweiterte Rektifizierung mit 20 cm Rand → Tischbande/Markierungen sichtbar
    - Gefuellter Kreis pro Ball an der Anfangsposition (Bewegungsstart)
    - Durchgaengige Linie in Ballfarbe, ueberbrueckt Tracking-Luecken
    - KEIN Marker an der aktuellen Ball-Position (User-Wunsch — nur Start + Linie)

    Tracking-Koordinaten in JSON bleiben unveraendert im OUT_W x OUT_H-System;
    nur das gerenderte Bild ist groesser.
    """
    clip_name = f"clip{clip_idx:02d}"
    tmp_mp4 = output_dir / f"{clip_name}_raw.mp4"
    final_mp4 = output_dir / f"{clip_name}.mp4"
    json_path = output_dir / f"{clip_name}.json"
    thumb_path = output_dir / f"{clip_name}_thumb.jpg"

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(tmp_mp4), fourcc, fps, (VIZ_OUT_W, VIZ_OUT_H))

    json_frames = []
    found_counts = {cls: 0 for cls in BALL_CLASSES}

    # Anfangsposition pro Ball — Mittelwert ueber die ersten paar getrackten
    # Frames (gegen Tracking-Noise). Bei Sub-Clips die mit einer Stillstands-
    # Phase beginnen ist das die "Ruheposition vor dem Stoss"; bei anderen
    # einfach die erste robuste Position.
    start_positions: dict[str, tuple] = {}
    AVG_N = 6  # mittele ueber die ersten 6 gefundenen Frames pro Ball
    for cls in BALL_CLASSES:
        collected = []
        first_idx = None
        for j in range(len(clip_frames)):
            pos = tracks[cls].get(j)
            if pos is not None:
                if first_idx is None:
                    first_idx = j
                collected.append(pos)
                if len(collected) >= AVG_N:
                    break
        if collected:
            avg_x = sum(p[0] for p in collected) / len(collected)
            avg_y = sum(p[1] for p in collected) / len(collected)
            start_positions[cls] = ((avg_x, avg_y), first_idx)

    # Trail-Overlay im erweiterten Koordinatensystem
    trail_overlay = np.zeros((VIZ_OUT_H, VIZ_OUT_W, 3), dtype=np.uint8)
    trail_mask = np.zeros((VIZ_OUT_H, VIZ_OUT_W), dtype=np.uint8)
    last_pos_per_ball = {cls: None for cls in BALL_CLASSES}

    for i in range(len(clip_frames)):
        frame = decode_jpeg(clip_frames[i])
        if frame is None:
            continue
        rectified = rectify_frame_extended(frame, H)

        # Trail-Segment hinzufuegen (ueberbrueckt Luecken)
        for cls in BALL_CLASSES:
            curr = tracks[cls].get(i)
            if curr is None:
                continue
            if last_pos_per_ball[cls] is not None:
                p0 = _viz_pt(last_pos_per_ball[cls])
                p1 = _viz_pt(curr)
                if p0 != p1:
                    cv2.line(trail_overlay, p0, p1, VIZ_COLORS[cls], 3, cv2.LINE_AA)
                    cv2.line(trail_mask, p0, p1, 255, 3, cv2.LINE_AA)
            last_pos_per_ball[cls] = curr

        if trail_mask.any():
            mask_bool = trail_mask > 0
            rectified[mask_bool] = trail_overlay[mask_bool]

        # Anfangsposition: gefuellter Kreis mit dunkler Outline (immer obenauf)
        for cls in BALL_CLASSES:
            sp = start_positions.get(cls)
            if sp is None or i < sp[1]:
                continue
            cx, cy = _viz_pt(sp[0])
            cv2.circle(rectified, (cx, cy), BALL_RADIUS_PX, VIZ_COLORS[cls], -1)
            cv2.circle(rectified, (cx, cy), BALL_RADIUS_PX, (0, 0, 0), 1)

        # JSON-Daten + found-Count (KEIN visueller Marker fuer aktuelle Position)
        balls_in_frame = {}
        for cls in BALL_CLASSES:
            pos = tracks[cls].get(i)
            if pos is None:
                balls_in_frame[cls] = None
                continue
            balls_in_frame[cls] = [round(pos[0], 1), round(pos[1], 1)]
            found_counts[cls] += 1

        # Frame-Label unten links
        is_pivot = (i == pivot_idx)
        label = f"{clip_name}  f={i}/{len(clip_frames)-1}"
        if is_pivot:
            label += "  [PIVOT]"
        cv2.putText(rectified, label, (10, VIZ_OUT_H - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

        writer.write(rectified)

        json_frames.append({
            "frame_in_clip": i,
            "frame_in_video": start_frame_in_video + i,
            "time_s": round((start_frame_in_video + i) / fps, 3),
            "balls": balls_in_frame,
        })

    writer.release()

    # Thumbnail: Frame 0.5s vor Ende mit komplettem Trail
    if json_frames:
        thumb_src_idx = max(0, len(clip_frames) - int(round(0.5 * fps)) - 1)
        thumb_src = decode_jpeg(clip_frames[thumb_src_idx])
        if thumb_src is not None:
            thumb_rect = rectify_frame_extended(thumb_src, H)
            if trail_mask.any():
                mb = trail_mask > 0
                thumb_rect[mb] = trail_overlay[mb]
            for cls in BALL_CLASSES:
                sp = start_positions.get(cls)
                if sp is None:
                    continue
                cx, cy = _viz_pt(sp[0])
                cv2.circle(thumb_rect, (cx, cy), BALL_RADIUS_PX, VIZ_COLORS[cls], -1)
                cv2.circle(thumb_rect, (cx, cy), BALL_RADIUS_PX, (0, 0, 0), 1)
            # Kein aktueller-Ball-Marker im Thumbnail (User-Wunsch)
            cv2.imwrite(str(thumb_path), thumb_rect, [cv2.IMWRITE_JPEG_QUALITY, 82])

    # H.264-Konvertierung mit optionalem Audio aus dem Original-Video
    audio_start_s = None
    audio_duration_s = None
    if source_video_path:
        audio_start_s = start_frame_in_video / fps
        audio_duration_s = len(clip_frames) / fps

    if _convert_to_h264(str(tmp_mp4), str(final_mp4),
                        audio_video=source_video_path,
                        audio_start_s=audio_start_s,
                        audio_duration_s=audio_duration_s):
        try:
            tmp_mp4.unlink()
        except Exception:
            pass
    else:
        try:
            tmp_mp4.rename(final_mp4)
        except Exception:
            pass

    json_data = {
        "clip_name": clip_name,
        "setting_id": setting.id,
        "setting_name": setting.name,
        "fps": fps,
        "frames_total": len(clip_frames),
        "start_frame_in_video": start_frame_in_video,
        "pivot_frame_in_clip": pivot_idx,
        "found_counts": found_counts,
        "frames": json_frames,
    }
    json_path.write_text(json.dumps(json_data, indent=2, ensure_ascii=False),
                         encoding="utf-8")

    return {
        "name": clip_name,
        "mp4": final_mp4.name,
        "json": json_path.name,
        "thumb": thumb_path.name if thumb_path.exists() else None,
        "frames": len(clip_frames),
        "pivot_frame_in_clip": pivot_idx,
        "start_frame_in_video": start_frame_in_video,
        "found_counts": found_counts,
    }


# ---- Preview-JPGs --------------------------------------------------------

def write_scan_preview(frame: np.ndarray, setting: Setting, frame_idx: int,
                       total: int, visible_count: int, current_pct: float,
                       preview_path: Path):
    """Preview waehrend Pass 1: Original-Frame mit Polygon-Overlay + Status."""
    img = frame.copy()
    corners = np.array(setting.table_corners, dtype=np.int32)
    if len(corners) == 4:
        cv2.polylines(img, [corners], True, (0, 200, 255), 2)

    bar_h = 70
    overlay = img.copy()
    cv2.rectangle(overlay, (0, 0), (img.shape[1], bar_h), (0, 0, 0), -1)
    img = cv2.addWeighted(overlay, 0.65, img, 0.35, 0)

    lines = [
        f"[scan] frame {frame_idx}/{total}  tisch sichtbar bisher: {visible_count}f",
        f"aktuelle filz-quote: {current_pct:5.1f}%  (schwelle: {setting.felt_tolerance} BGR)",
        f"setting: {setting.name}",
    ]
    for i, ln in enumerate(lines):
        cv2.putText(img, ln, (12, 22 + i * 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 255), 1)
    cv2.imwrite(str(preview_path), img)


def write_track_preview(rectified: np.ndarray, clip_name: str,
                        frame_idx: int, total: int, balls: dict,
                        clips_done: int, preview_path: Path):
    """Preview waehrend Pass 2: rektifizierter Tisch + aktuelle Bälle."""
    img = rectified.copy()
    for cls, pos in balls.items():
        if pos is None:
            continue
        cv2.circle(img, (int(pos[0]), int(pos[1])), BALL_RADIUS_PX,
                   VIZ_COLORS[cls], 2)

    bar_h = 50
    overlay = img.copy()
    cv2.rectangle(overlay, (0, 0), (img.shape[1], bar_h), (0, 0, 0), -1)
    img = cv2.addWeighted(overlay, 0.65, img, 0.35, 0)
    lines = [
        f"[track] {clip_name}  f={frame_idx}/{total-1}  fertig: {clips_done}",
        "  ".join(f"{c}={'-' if balls.get(c) is None else 'ok'}" for c in BALL_CLASSES),
    ]
    for i, ln in enumerate(lines):
        cv2.putText(img, ln, (10, 20 + i * 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 255), 1)
    cv2.imwrite(str(preview_path), img)


# ---- Hauptfunktion -------------------------------------------------------

def track_video(video_path: str, setting: Setting, gs: GlobalSettings,
                output_dir: Path, progress_cb=None) -> dict:
    """Trackt ein komplettes Video. Schreibt Clip-Files in output_dir.

    Ablauf (Paket B):
    1. Pass 1: scan_clip_ranges findet Tisch-sichtbare Bereiche
    2. Pass 2 pro Range:
       a) Frames laden, Pivot finden, bidirektional tracken
       b) Stillstands-Phasen finden → Range in Sub-Clips zerlegen
       c) Sub-Clips < min_clip_duration_s verwerfen
    3. Alle ueberlebenden Sub-Clips durchnummerieren (1, 2, ...) und schreiben

    progress_cb(phase: str, current: int, total: int) wird aufgerufen.
    Returns: summary dict.
    """
    if not setting.is_complete():
        raise ValueError("Setting ist unvollstaendig (Tisch-Ecken oder Ball-Farben fehlen)")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    preview_path = output_dir / "_preview.jpg"

    # ---- Pass 1: Clip-Bereiche scannen
    if progress_cb:
        progress_cb("scan", 0, 0)
    ranges, fps, total_frames = scan_clip_ranges(
        video_path, setting, gs, preview_path, progress_cb
    )

    summary = {
        "fps": fps,
        "total_frames": total_frames,
        "ranges_found": len(ranges),
        "clips": [],
        "skipped_ranges": [],
        "discarded_subclips": [],   # zu kurze Sub-Clips landen hier
    }

    if not ranges:
        if progress_cb:
            progress_cb("done", 0, 0)
        return summary

    max_disp_px = (gs.v_max_mps / max(fps, 1.0)) * PX_PER_M
    H = compute_homography(setting.table_corners)

    # ---- Pass 2: tracken + sub-clip-splitting, sammle alles zum Schreiben
    pending_subclips: list[dict] = []  # noch nicht geschriebene Sub-Clips

    for range_idx, (start_f, end_f) in enumerate(ranges, start=1):
        if progress_cb:
            progress_cb("clip_load", range_idx, len(ranges))

        clip_frames = load_clip_frames(video_path, start_f, end_f)

        if progress_cb:
            progress_cb("clip_pivot", range_idx, len(ranges))

        pivot_idx, pivot_assignment = find_pivot(clip_frames, fps, setting, gs)

        if pivot_idx is None or pivot_assignment is None:
            summary["skipped_ranges"].append({
                "range": [start_f, end_f],
                "reason": "kein sauberer Pivot-Frame (3 Baelle nicht klar isolierbar)",
            })
            continue

        if progress_cb:
            progress_cb("clip_track", range_idx, len(ranges))

        tracks = track_bidirectional(
            clip_frames, pivot_idx, pivot_assignment, H, max_disp_px, setting, gs
        )

        # Preview vom Pivot
        pivot_frame = decode_jpeg(clip_frames[pivot_idx])
        if pivot_frame is not None:
            rectified = rectify_frame(pivot_frame, H)
            balls_at_pivot = {
                cls: (pivot_assignment[cls]["x"], pivot_assignment[cls]["y"])
                for cls in BALL_CLASSES
            }
            write_track_preview(
                rectified, f"range{range_idx:02d}", pivot_idx,
                len(clip_frames), balls_at_pivot,
                len(pending_subclips), preview_path
            )

        # Sub-Clip-Splitting basierend auf Stillstands-Phasen
        boundaries = split_tracks_into_subclips(tracks, fps, gs, len(clip_frames))

        for sub in boundaries:
            sub_start = sub["start_frame"]
            sub_end = sub["end_frame"]
            sub_n_frames = sub_end - sub_start + 1
            duration_s = sub_n_frames / fps

            if duration_s < gs.min_clip_duration_s:
                summary["discarded_subclips"].append({
                    "range_idx": range_idx,
                    "frames_in_range": [sub_start, sub_end],
                    "duration_s": round(duration_s, 2),
                    "reason": f"< min_clip_duration_s ({gs.min_clip_duration_s}s)",
                })
                continue

            # Pivot ggf. in dieses Sub-Clip-Fenster anpassen; falls Pivot ausserhalb
            # liegt, einfach Mitte nehmen (kein Schaden — Pivot ist nur fuer
            # Visualisierungs-Label im Clip-Video).
            if sub_start <= pivot_idx <= sub_end:
                sub_pivot_idx = pivot_idx - sub_start
            else:
                sub_pivot_idx = sub_n_frames // 2

            pending_subclips.append({
                "tracks": slice_tracks(tracks, sub_start, sub_end),
                "frames": clip_frames[sub_start:sub_end + 1],
                "start_frame_in_video": start_f + sub_start,
                "pivot_idx": sub_pivot_idx,
                "duration_s": duration_s,
                "from_range": range_idx,
                "has_pre_stillness": sub["has_pre_stillness"],
                "has_post_stillness": sub["has_post_stillness"],
            })

    # ---- Schreiben mit finaler, durchgehender Nummerierung
    for final_idx, sc in enumerate(pending_subclips, start=1):
        if progress_cb:
            progress_cb("clip_write", final_idx, len(pending_subclips))
        meta = write_clip_outputs(
            final_idx, sc["tracks"], sc["frames"], H, fps,
            sc["start_frame_in_video"], sc["pivot_idx"], output_dir, setting,
            source_video_path=video_path,
        )
        meta["duration_s"] = round(sc["duration_s"], 2)
        meta["from_range"] = sc["from_range"]
        summary["clips"].append(meta)

    if progress_cb:
        progress_cb("done", len(summary["clips"]), len(summary["clips"]))
    return summary


# ---- Editor-Helpers (fuer Backend-Endpoints) -----------------------------

def sample_color_at(frame_bgr: np.ndarray, x: int, y: int, radius: int = 2) -> str:
    """5x5 Median-Sample um (x, y). Returns Hex-String."""
    h, w = frame_bgr.shape[:2]
    x0 = max(0, x - radius)
    x1 = min(w, x + radius + 1)
    y0 = max(0, y - radius)
    y1 = min(h, y + radius + 1)
    patch = frame_bgr[y0:y1, x0:x1].reshape(-1, 3)
    if patch.size == 0:
        return "#000000"
    med = np.median(patch, axis=0)
    return bgr_to_hex(med)


def sample_color_at_polygon_center(frame_bgr: np.ndarray, corners: list,
                                   radius: int = 10) -> str:
    """Sample Filz-Farbe aus dem Mittelpunkt des Setup-Polygons."""
    pts = np.array(corners, dtype=np.float32)
    cx = int(np.mean(pts[:, 0]))
    cy = int(np.mean(pts[:, 1]))
    return sample_color_at(frame_bgr, cx, cy, radius)


def extract_frame_at_time(video_path: str, t_seconds: float) -> np.ndarray | None:
    """Liest einen Frame an einer bestimmten Zeit aus dem Video."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_idx = int(round(t_seconds * fps))
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ret, frame = cap.read()
    cap.release()
    return frame if ret else None
