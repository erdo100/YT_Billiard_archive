# yt::archive

Lokale Web-App zum YouTube-Download und Carambol-Billard-Tracking. Läuft auf
`127.0.0.1:5000`, keine Cloud, keine Telemetrie.

## Was sie kann

- YouTube-Kanäle durchsuchen und Videos batch-downloaden (`yt-dlp`)
- Pro Kamera-Winkel ein **Setting** anlegen (Tisch-Ecken + Ball-Farben durch
  Klicks im Browser definieren)
- Videos tracken: Bewegung der drei Carambol-Bälle Frame für Frame, automatisch
  in Clips (eine Position = ein Clip) zerlegt
- Pro Clip ein rektifizierter Top-Down-MP4 plus JSON mit allen Positionen

## Setup (Windows, Empfehlung)

```powershell
# Verzeichnis anlegen, z.B. D:\Programming\yt_archive
cd D:\Programming\yt_archive

# Virtual env
python -m venv .venv
.venv\Scripts\activate

# Dependencies
pip install -r requirements.txt

# ffmpeg (für yt-dlp Video+Audio-Merging)
winget install Gyan.FFmpeg
# danach Terminal neu starten damit ffmpeg im PATH ist

# Start
python app.py
```

Öffnet sich automatisch im Browser. Daten liegen in `~/.yt_archive/`
(Settings, Global-Parameter, History-DB).

## Workflow

### 1. Download

Tab **browse** → Kanal-URL oder `@handle` eingeben → laden → Videos
auswählen → `↓ download`. Fortschritt im Tab **queue**.

Heruntergeladene Videos landen unter `~/Downloads/YouTube/YYMMDD/YYMMDD_NN_titel_VIDEOID/`
(Ordner-Pfad lässt sich in der Config ändern, oben rechts ⚙ config).

### 2. Setting anlegen

Tab **setup** → `+ neu` → Name vergeben.

Im Editor:
1. **Referenz-Video wählen** aus der History.
2. Im Video-Player zu einer Stelle springen wo der Tisch gut sichtbar ist
   (Top-Down-View, keine Spieler davor) → **frame greifen**.
3. **tisch-ecken setzen** klicken → im Canvas die 4 Ecken nacheinander
   anklicken in dieser Reihenfolge: **TL, TR, BR, BL** (oben-links,
   oben-rechts, unten-rechts, unten-links). Nach dem 4. Klick wird die
   Filz-Farbe automatisch aus dem Polygon-Zentrum gesampelt.
4. **ball-farben setzen** klicken → drei Bälle in der Reihenfolge
   **weiss, gelb, rot** anklicken. Pro Klick wird ein 5×5-Median um den
   Klick-Punkt gesampelt.
5. **speichern**.

Ein Setting wird nur als "fertig" markiert (✓ in der Sidebar) wenn alle
4 Ecken und 3 Ball-Farben gesetzt sind. Unvollständige Settings können
nicht zum Tracken benutzt werden.

### 3. Tracken

Tab **history** → bei einem Video auf `tracken` klicken → Setting wählen →
`starten`. Live-Preview im Vorschaubild des History-Eintrags. Status
unten im Panel "tracking-status".

Ergebnis pro Clip in `<video-folder>/`:
- `clip01.mp4` — rektifizierter Top-Down-Tisch (1420×710 px) mit
  Ball-Markern und 30-Frame-Trail
- `clip01.json` — Frame-für-Frame Positionen aller drei Bälle
- `_preview.jpg` — letztes Update während des Trackings

## Tracking-Algorithmus (Pivot-basiert)

Das wichtige Teil. Funktioniert in **zwei Pässen**:

### Pass 1 — Clip-Bereiche finden

Streaming durchs Video, pro Frame ein 9-Punkt-Filz-Sample im
Setup-Polygon. Wenn genug Punkte (≥ `felt_detect_pct` %) Filz-Farbe haben,
zählt der Frame als "Tisch sichtbar". Zusammenhängende Bereiche werden
zu Clip-Kandidaten (Lücken bis `max_gap_frames` werden überbrückt,
Bereiche kürzer als `min_clip_frames` verworfen).

### Pass 2 — Pivot-Init + bidirektionales Tracking

Pro Clip-Bereich:

1. Frames in JPEG-komprimierter Form in Memory laden (≈ 200 KB pro
   1080p-Frame).
2. **Pivot-Suche**: alle `init_sample_interval_s` Sekunden ein Sample. Pro
   Sample ein Init-Quality-Score:
   ```
   score = felt_pct                                # mehr Filz = weniger Spieler/Hand
         - ball_match_dist × 0.5                   # bessere Ball-Farben-Treffer
         - (anzahl_blobs - 3) × 10                 # weniger Extra-Blobs = sauberer
   ```
   Voraussetzung: `felt_pct ≥ init_min_felt_pct` UND drei plausible Bälle.
   Pivot = Sample mit höchstem Score.
3. **Bidirektional tracken**: vom Pivot aus vorwärts bis Clip-Ende, dann
   rückwärts bis Clip-Anfang. Pro Frame, pro Ball:
   - Suche-Radius = `v_max_mps / fps × 500 px/m` (z.B. ~117 px bei
     7 m/s @ 30 fps)
   - Kandidaten = Blobs im Filz-Loch in Reichweite
   - Score = Distanz + Farb-Distanz zur Tracking-Farbe (EMA-Update,
     hart gebunden an die Setup-Farbe)
4. Output schreiben (MP4 + JSON).

Wenn kein sauberer Pivot in einem Bereich gefunden wird (z.B. Spieler
die ganze Zeit am Tisch), wird der Bereich übersprungen und im
Status-Panel als "skipped" geloggt.

## Globale Tracking-Parameter

Über `⚙ global` oben rechts erreichbar. Defaults sind für PBA-artige
Top-Down-Kameras passend; in der Regel muss man nichts ändern.

| Parameter                 | Default | Bedeutung                                                              |
|---------------------------|---------|------------------------------------------------------------------------|
| `min_clip_frames`         | 25      | Clip wird verworfen wenn kürzer                                        |
| `max_gap_frames`          | 15      | Tisch darf so viele Frames "weg" sein ohne dass der Clip endet         |
| `felt_detect_pct`         | 60      | 9-Punkt-Quote ab der ein Frame als "Tisch sichtbar" gilt               |
| `init_min_felt_pct`       | 70      | Pivot-Kandidat muss mindestens so viel Filz im rektifizierten Bild haben |
| `init_sample_interval_s`  | 1.0     | Pivot-Suche: Sample-Abstand in Sekunden                                |
| `preview_interval`        | 15      | `_preview.jpg` alle X Frames updaten (während Pass 1)                  |
| `v_max_mps`               | 7.0     | Maximale Ballgeschwindigkeit → Such-Radius pro Frame                   |
| `color_adaptation_rate`   | 0.2     | EMA-Alpha für Ball-Farbe (0 = nie anpassen, 1 = nur letzte messen)     |
| `max_color_drift_bgr`     | 60      | Tracking-Farbe darf nie weiter als so weit von der Setup-Farbe driften |
| `max_ball_lost_frames`    | 5       | Nach X verlorenen Frames wird der Such-Radius nicht weiter vergrößert  |

## Verzeichnis-Layout

```
~/.yt_archive/
├── history.db           # SQLite mit allen heruntergeladenen Videos
├── settings.json        # Alle Tracking-Settings
├── global.json          # Globale Tracking-Parameter
└── config.json          # Download-Ordner-Pfad

~/Downloads/YouTube/      # (konfigurierbar)
└── 260301/               # YYMMDD vom Upload
    └── 260301_01_some_title_dQw4w9WgXcQ/
        ├── 260301_01_some_title_dQw4w9WgXcQ.mp4    # Original
        ├── _preview.jpg
        ├── clip01.mp4
        ├── clip01.json
        ├── clip02.mp4
        └── clip02.json
```

## Annahmen / Grenzen

- Genau **3 Bälle** Carambol-Standard (weiß, gelb, rot)
- Top-Down-Kamera ist **fest** im Frame (sonst pro Winkel ein eigenes Setting)
- Ein Setting matched auf einen Kamera-Winkel — wenn dein Kanal mehrere
  benutzt, brauchst du mehrere Settings und musst sie pro Video selber
  zuordnen.
- Die Tisch-Maße sind Carambol-Standard (2.84 × 1.42 m) fest verdrahtet.
  Für andere Tisch-Größen müsste man `TABLE_W_MM`/`TABLE_H_MM` in
  `analyzer.py` anpassen.
- Bei extrem schnellen Ball-Geschwindigkeiten (> `v_max_mps`) verliert das
  Tracking den Ball. `v_max_mps` hochsetzen wenn nötig — kostet aber
  Robustheit (mehr falsche Kandidaten in Reichweite).

## Was es nicht (mehr) hat

Frühere Versionen hatten Quickscan, Auto-Profile-Matching, Auto-Tisch-Detection,
Auto-Tune, Skip-Resolve. Alles raus. Aktueller Workflow ist:
**4 Klicks für den Tisch, 3 Klicks für die Bälle, einmal speichern, fertig.**
