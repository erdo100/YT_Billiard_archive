# YT Billiard Archive

Local web app for YouTube download and carambol billiard tracking. Runs on
127.0.0.1:5000, no cloud, no telemetry.

## What it can do

- Browse YouTube channels and batch-download videos (yt-dlp)
- Create one setting per camera angle (define table corners + ball colors
  by clicking in the browser)
- Track videos: movement of the three carambol balls frame by frame, automatically
  split into clips (one shot = one clip)
- For each clip, a rectified top-down MP4 plus JSON with all positions

## Setup (Windows, recommended)

    # Create directory, e.g. D:\Programming\yt_archive
    cd D:\Programming\yt_archive

    # Virtual env
    python -m venv .venv
    .venv\Scripts\activate

    # Dependencies
    pip install -r requirements.txt

    # ffmpeg (for yt-dlp video+audio merging)
    winget install Gyan.FFmpeg
    # restart the terminal afterwards so ffmpeg is in PATH

    # Start
    python app.py

It will automatically open in your browser. Data is stored in ~/.yt_archive/
(settings, global parameters, history DB).

## Workflow

### 1. Download

Tab browse -> enter channel URL or @handle -> load -> select videos ->
↓ download. Progress in the queue tab.

Downloaded videos are placed in ~/Downloads/YouTube/YYMMDD/YYMMDD_NN_title_VIDEOID/
(the folder path can be changed in the config, top-right ⚙ config).

### 2. Create a setting

Tab setup -> + new -> give it a name.

In the editor:
1. Choose a reference video from the archive.
2. In the video player, go to a frame where the table is clearly visible
   (top-down view, no players in front) -> grab frame.
3. Click set table corners -> in the canvas click the 4 corners in order:
   TL, TR, BR, BL (top-left, top-right, bottom-right, bottom-left).
   After the 4th click, the felt colour is automatically sampled from the
   polygon centre.
4. Click set ball colors -> click the three balls in order
   white, yellow, red. Each click samples a 5×5 median around the click point.
5. Save.

A setting is marked as "complete" (✓ in the sidebar) only when all
4 corners and 3 ball colours are set. Incomplete settings cannot be used for tracking.

### 3. Track

Tab history -> click track on a video -> choose a setting ->
start. Live preview in the history entry's thumbnail. Status shown in the
"tracking-status" panel below.

Result per clip in <video-folder>/:
- clip01.mp4 — rectified top-down table (1420×710 px) with
  ball markers and a 30-frame trail
- clip01.json — frame-by-frame positions of all three balls
- _preview.jpg — latest update during tracking

## Tracking algorithm (pivot-based)

The important part. Works in two passes:

### Pass 1 — find clip ranges

Streaming through the video, per frame a 9-point felt sample inside the
setup polygon. If enough points (>= felt_detect_pct %) match the felt colour,
the frame counts as "table visible". Contiguous ranges become clip candidates
(gaps up to max_gap_frames are bridged, ranges shorter than min_clip_frames
are discarded).

### Pass 2 — pivot init + bidirectional tracking

Per clip range:

1. Load frames into memory as JPEGs (≈ 200 KB per 1080p frame).
2. Pivot search: sample every init_sample_interval_s seconds. For each
   sample, an init quality score:

   score = felt_pct                                # more felt = fewer players/hands
         - ball_match_dist × 0.5                   # better ball colour match
         - (number_of_blobs - 3) × 10              # fewer extra blobs = cleaner

   Prerequisites: felt_pct >= init_min_felt_pct AND three plausible balls.
   Pivot = sample with highest score.
3. Bidirectional tracking: from the pivot forward to the end of the clip,
   then backward to the start. Per frame, per ball:
   - Search radius = v_max_mps / fps × 500 px/m (e.g. ~117 px at 7 m/s @ 30 fps)
   - Candidates = blobs inside the felt hole within range
   - Score = distance + colour distance to tracking colour (EMA update,
     hard-bound to the setup colour)
4. Write output (MP4 + JSON).

If no clean pivot is found in a range (e.g. players are in front of the table
the whole time), the range is skipped and logged as "skipped" in the status panel.

## Global tracking parameters

Accessible via ⚙ global at the top right. Defaults are suitable for PBA-style
top-down cameras; usually you don't need to change anything.

Parameter                 | Default | Meaning
--------------------------|---------|--------------------------------------------------
min_clip_frames           | 25      | Clip is discarded if shorter
max_gap_frames            | 15      | Table may be "gone" for this many frames without ending the clip
felt_detect_pct           | 60      | 9-point quota for a frame to be considered "table visible"
init_min_felt_pct         | 70      | Pivot candidate needs at least this much felt in the rectified image
init_sample_interval_s    | 1.0     | Pivot search: sample every X seconds
preview_interval          | 15      | Update _preview.jpg every X frames (during pass 1)
v_max_mps                 | 7.0     | Maximum ball speed → search radius per frame
color_adaptation_rate     | 0.2     | EMA alpha for ball colour (0 = never adapt, 1 = only last measurement)
max_color_drift_bgr       | 60      | Tracking colour must never drift further from the setup colour
max_ball_lost_frames      | 5       | After X lost frames, the search radius is not increased further

## Directory layout

~/.yt_archive/
├── history.db           # SQLite with all downloaded videos
├── settings.json        # All tracking settings
├── global.json          # Global tracking parameters
└── config.json          # Download folder path

~/Downloads/YouTube/      # (configurable)
└── 260301/               # YYMMDD of upload
    └── 260301_01_some_title_dQw4w9WgXcQ/
        ├── 260301_01_some_title_dQw4w9WgXcQ.mp4    # Original
        ├── _preview.jpg
        ├── clip01.mp4
        ├── clip01.json
        ├── clip02.mp4
        └── clip02.json

## Assumptions / limitations

- Exactly 3 balls (white, yellow, red) – carambol standard
- The top-down camera is fixed in the frame (otherwise create one setting per angle)
- One setting matches one camera angle – if your channel uses multiple angles,
  you need multiple settings and must assign them manually per video
- The table dimensions are fixed to carambol standard (2.84 × 1.42 m).
  For other table sizes you would need to change TABLE_W_MM/TABLE_H_MM
  in analyzer.py.
- At extremely high ball speeds (> v_max_mps) the tracker may lose the ball.
  Increase v_max_mps if needed – but that costs robustness (more false
  candidates within range).

## What it no longer has

Earlier versions had quickscan, auto-profile matching, auto-table detection,
auto-tune, skip-resolve. All removed. Current workflow is:
4 clicks for the table, 3 clicks for the balls, save once – done.


## Sample picture, video, output file

[clip04.json](https://github.com/user-attachments/files/28596402/clip04.json)

https://github.com/user-attachments/assets/b80266b8-eea7-4026-8852-41a9d296691c

<img width="1620" height="910" alt="clip04_thumb" src="https://github.com/user-attachments/assets/8f4c1455-c0a4-4847-8d8b-5072c8135e37" />

