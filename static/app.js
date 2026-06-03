// =========================================================================
// yt::archive frontend — radikal vereinfacht
// =========================================================================

const $ = (id) => document.getElementById(id);
const $$ = (sel) => document.querySelectorAll(sel);

const state = {
    channelVideos: [],
    channelUrl: "",
    channelOffset: 0,
    channelPageSize: 30,
    selectedVideoIds: new Set(),
    historyVideos: [],
    expandedClipVids: new Set(),
    settings: [],
    activeSettingId: null,
    pollTimer: null,
    previewTimers: {},      // video_id -> intervalId für live-preview
    lastTrackingStatus: {}, // video_id -> letzter Status zum Wechsel-erkennen
    // Setup state
    currentFrame: null,     // { b64, width, height, naturalWidth, naturalHeight }
    clickMode: "none",      // "none" | "corners" | "balls"
    cornersInProgress: [],  // [[x,y], ...] frame-koordinaten (max 4)
    ballClicksInProgress: [],  // [{x, y, hex}, ...] (max 3)
    // Zoom & Pan
    zoom: 1.0,
    minZoom: 0.1,
    maxZoom: 6.0,
    panning: false,
    panLastX: 0,
    panLastY: 0,
    // Live tracking preview
    livePreviewTimer: null,
    livePreviewInFlight: false,
    liveImage: null,             // letztes geladenes rektifiziertes Image-Element
    liveTrail: {                 // pro Klasse: [[x, y], ...] — keine null-Marker mehr
        weiss: [],
        gelb: [],
        rot: [],
    },
    liveMaxTrailLen: 50000,      // praktisch unbegrenzt (~3h @ 5fps)
    trackingState: null,         // Server-State {balls, track_colors, lost}
    lastDetectionAt: 0,          // performance.now()/1000
    lastVideoTime: 0,            // currentTime vor seek, fuer delta-check
    skipNextSeekReset: false,    // setzt der frame-step-button
    assumedFps: 30,              // fps fuer frame-step
};

const BALL_LABELS = ["weiss", "gelb", "rot"];
const BALL_RING_COLORS = ["#f0f0f0", "#e6c648", "#c83030"];

// =========================================================================
// View switching
// =========================================================================

$$(".nav-btn").forEach(btn => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
});

function switchView(name) {
    if (name !== "setup") stopLivePreviewLoop();
    $$(".nav-btn").forEach(b => b.classList.toggle("active", b.dataset.view === name));
    $$(".view").forEach(v => v.classList.toggle("active", v.id === `view-${name}`));
    if (name === "history") loadHistory();
    if (name === "setup") {
        loadSettings();
        loadHistoryForSetupSelect();
    }
    if (name === "queue") refreshStatus();
}

// =========================================================================
// Channel browse
// =========================================================================

async function loadChannel(append = false) {
    const url = $("channel-input").value.trim();
    if (!url) return;
    if (!append) {
        state.channelUrl = url;
        state.channelOffset = 0;
        state.channelVideos = [];
        state.selectedVideoIds.clear();
        $("channel-info").textContent = "lade...";
        $("video-list").innerHTML = "";
    }
    $("btn-load-more").classList.add("hidden");
    try {
        const r = await fetch("/api/channel", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                url: state.channelUrl,
                limit: state.channelPageSize,
                offset: state.channelOffset,
            }),
        });
        const d = await r.json();
        if (d.error) {
            $("channel-info").textContent = "fehler: " + d.error;
            return;
        }
        const newVideos = d.videos || [];
        state.channelVideos = state.channelVideos.concat(newVideos);
        state.channelOffset += newVideos.length;
        renderVideos();
        $("channel-info").textContent = `${state.channelVideos.length} videos angezeigt`;
        // "mehr laden" zeigen wenn die letzte Seite voll war
        if (newVideos.length >= state.channelPageSize) {
            $("btn-load-more").classList.remove("hidden");
        }
    } catch (e) {
        $("channel-info").textContent = "fehler: " + e.message;
    }
}

$("btn-load-channel").addEventListener("click", () => loadChannel(false));
$("channel-input").addEventListener("keypress", e => {
    if (e.key === "Enter") loadChannel(false);
});
$("btn-load-more").addEventListener("click", () => loadChannel(true));

// Einzelvideo per URL hinzufuegen
$("btn-add-single").addEventListener("click", addSingleVideo);
$("single-url-input").addEventListener("keypress", e => {
    if (e.key === "Enter") addSingleVideo();
});

async function addSingleVideo() {
    const url = $("single-url-input").value.trim();
    if (!url) return;
    $("channel-info").textContent = "lade video-info...";
    try {
        const r = await fetch("/api/video-info", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({url}),
        });
        const d = await r.json();
        if (d.error) {
            $("channel-info").textContent = "fehler: " + d.error;
            return;
        }
        // Duplikat-Check
        if (state.channelVideos.some(v => v.video_id === d.video.video_id)) {
            $("channel-info").textContent = "video schon in der liste";
            return;
        }
        // Vorne dranhaengen, damit es prominent sichtbar ist
        state.channelVideos.unshift(d.video);
        renderVideos();
        $("single-url-input").value = "";
        $("channel-info").textContent = `${state.channelVideos.length} videos angezeigt`;
    } catch (e) {
        $("channel-info").textContent = "fehler: " + e.message;
    }
}

function renderVideos() {
    const wrap = $("video-list");
    wrap.innerHTML = "";
    state.channelVideos.forEach(v => {
        const row = document.createElement("div");
        row.className = "video-row";
        // Cross-Ref Badges
        let badges = "";
        if (v.in_archive) {
            badges += `<span class="video-badge archived">archiv ✓</span>`;
            if (v.clips_count != null) {
                badges += `<span class="video-badge">${v.clips_count} clips</span>`;
            }
        }
        const durTxt = v.duration_s != null
            ? fmtDuration(v.duration_s)
            : (v.duration ? fmtDuration(v.duration) : "");
        // title_en bevorzugen wenn das Video schon im Archiv ist und uebersetzt
        // wurde — fuer konsistente Anzeige zwischen Browse/Queue/Archiv
        const browseTitle = v.title_en || v.title;
        row.innerHTML = `
            <input type="checkbox" data-vid="${v.video_id}">
            <img class="video-thumb" src="${v.thumbnail}" alt="" data-yt-id="${v.video_id}">
            <div class="video-meta">
                <div class="video-title">${escapeHtml(browseTitle)}${badges}</div>
                <div class="muted small">${v.video_id}${durTxt ? ` · ${durTxt}` : ""}</div>
            </div>
            <div class="video-actions">
                <button class="btn-dl small" data-vid="${v.video_id}" title="nur download">↓ download</button>
                <button class="btn-dl-track primary small" data-vid="${v.video_id}" title="download + tracking">↓ + track</button>
            </div>
        `;
        const cb = row.querySelector("input");
        cb.addEventListener("change", () => {
            if (cb.checked) state.selectedVideoIds.add(v.video_id);
            else state.selectedVideoIds.delete(v.video_id);
            updateSelectionCount();
        });
        // Thumbnail-Klick: YouTube oeffnen (Stream-Embed wuerde CORS/Player-Politur brauchen)
        row.querySelector(".video-thumb").addEventListener("click", () => {
            window.open(`https://www.youtube.com/watch?v=${v.video_id}`, "_blank");
        });
        row.querySelector(".btn-dl").addEventListener("click", () => downloadSingle(v));
        row.querySelector(".btn-dl-track").addEventListener("click", () => openTrackModalForDownload(v));
        wrap.appendChild(row);
    });
    updateSelectionCount();
}

async function downloadSingle(v) {
    const r = await fetch("/api/download", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({videos: [v]}),
    });
    const d = await r.json();
    if (d.error) { alert("fehler: " + d.error); return; }
    switchView("queue");
    startPolling();
}

async function openTrackModalForDownload(v) {
    // Wir benutzen das gleiche Track-Modal wie im Archiv. Aber statt sofort
    // zu tracken, geht der setting_id mit als auto_track_setting in den download.
    if (state.settings.length === 0) await loadSettings();
    if (state.settings.length === 0) {
        alert("erst im setup-tab ein setting erstellen");
        return;
    }
    // Pre-fill modal-track als "download + track" mode
    $("track-video-name").textContent = v.title_en || v.title;
    $("modal-track").dataset.videoId = v.video_id;
    $("modal-track").dataset.mode = "download_track";
    $("modal-track").dataset.payload = JSON.stringify(v);
    populateTrackSettings({last_setting: null});
    $("track-warning").textContent = "wird heruntergeladen und dann automatisch getrackt.";
    $("modal-track").classList.remove("hidden");
}

function updateSelectionCount() {
    $("selection-count").textContent = `${state.selectedVideoIds.size} ausgewaehlt`;
}

$("btn-select-all").addEventListener("click", () => {
    state.channelVideos.forEach(v => state.selectedVideoIds.add(v.video_id));
    $$('#video-list input[type=checkbox]').forEach(cb => cb.checked = true);
    updateSelectionCount();
});
$("btn-select-none").addEventListener("click", () => {
    state.selectedVideoIds.clear();
    $$('#video-list input[type=checkbox]').forEach(cb => cb.checked = false);
    updateSelectionCount();
});

$("btn-download-selected").addEventListener("click", async () => {
    if (state.selectedVideoIds.size === 0) {
        alert("nichts ausgewaehlt");
        return;
    }
    const videos = state.channelVideos.filter(v => state.selectedVideoIds.has(v.video_id));
    const r = await fetch("/api/download", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({videos}),
    });
    const d = await r.json();
    alert(`${d.queued || 0} videos in die queue gelegt`);
    switchView("queue");
    startPolling();
});

// =========================================================================
// Status polling (downloads + tracking)
// =========================================================================

function startPolling() {
    if (state.pollTimer) return;
    state.pollTimer = setInterval(refreshStatus, 1000);
}

function stopPollingIfIdle() {
    // wenn keine running downloads und kein running tracking → stoppen
    // Implementiert im refreshStatus
}

async function refreshStatus() {
    try {
        const r = await fetch("/api/status");
        const d = await r.json();
        renderDownloadQueue(d.downloads || []);
        renderTrackingStatus(d.tracking || {});

        const activeDl = (d.downloads || []).some(x => x.status === "downloading" || x.status === "queued");
        const activeTrk = Object.values(d.tracking || {}).some(x => x.status === "running");
        if (!activeDl && !activeTrk) {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
        }
    } catch (e) {
        console.warn("status poll fail", e);
    }
}

function renderDownloadQueue(items) {
    const wrap = $("queue-list");
    wrap.innerHTML = "";
    if (items.length === 0) {
        $("queue-empty").style.display = "block";
        return;
    }
    $("queue-empty").style.display = "none";
    items.forEach(it => {
        const row = document.createElement("div");
        row.className = `queue-item status-${it.status}`;
        row.innerHTML = `
            <div class="queue-title">${escapeHtml(it.title)}</div>
            <div class="queue-status">${it.status}${it.progress != null && it.status === "downloading" ? ` ${it.progress}%` : ""}</div>
            ${it.error ? `<div class="queue-error">${escapeHtml(it.error)}</div>` : ""}
            ${it.status === "downloading" ? `<div class="progress-bar"><div class="progress-fill" style="width:${it.progress || 0}%"></div></div>` : ""}
        `;
        wrap.appendChild(row);
    });
}

$("btn-clear-done").addEventListener("click", async () => {
    await fetch("/api/queue/clear-done", {method: "POST"});
    refreshStatus();
});

function renderTrackingStatus(jobs) {
    // Status-Übergänge erkennen → Archive neu laden bei Wechsel auf done/cancelled
    for (const [vid, job] of Object.entries(jobs)) {
        const prev = state.lastTrackingStatus[vid];
        if (prev === "running" && (job.status === "done" || job.status === "cancelled")) {
            loadHistory();
        }
        state.lastTrackingStatus[vid] = job.status;
    }

    // Aktiver Job hat Vorrang. Wenn mehrere laufen, zeigen wir den ersten "running",
    // sonst den ersten "done" der noch nicht weggeklickt wurde, sonst nichts.
    const entries = Object.entries(jobs);
    const running = entries.find(([_, j]) => j.status === "running");
    const focusEntry = running || entries.find(([_, j]) => j.status === "done")
                                  || entries.find(([_, j]) => j.status === "error")
                                  || entries.find(([_, j]) => j.status === "cancelled");

    const panel = $("live-tracking-panel");
    const cancelBtn = $("btn-cancel-tracking");
    if (!focusEntry) {
        panel.classList.add("hidden");
        cancelBtn.classList.add("hidden");
        stopAllPreviewPolling();
        return;
    }
    panel.classList.remove("hidden");

    const [vid, job] = focusEntry;
    const v = state.historyVideos.find(x => x.video_id === vid);
    const title = v ? (v.title_en || v.title || vid) : vid;

    $("ltp-title").textContent = title;

    // Status-Zeile
    const statusEl = $("ltp-status");
    if (job.status === "running") {
        statusEl.textContent = "läuft";
        statusEl.className = "muted small status-running";
        cancelBtn.classList.remove("hidden");
        cancelBtn.dataset.vid = vid;
    } else if (job.status === "done") {
        statusEl.textContent = "fertig";
        statusEl.className = "muted small status-done";
        cancelBtn.classList.add("hidden");
    } else if (job.status === "error") {
        statusEl.textContent = "fehler";
        statusEl.className = "muted small status-error";
        cancelBtn.classList.add("hidden");
    } else if (job.status === "cancelled") {
        statusEl.textContent = "abgebrochen";
        statusEl.className = "muted small";
        cancelBtn.classList.add("hidden");
    } else {
        statusEl.textContent = job.status || "";
        statusEl.className = "muted small";
        cancelBtn.classList.add("hidden");
    }

    // Phase
    const phaseMap = {
        "init": "vorbereiten",
        "scan": "pass 1 — top-view-szenen suchen",
        "clip_load": "frames laden",
        "clip_pivot": "pivot-frame suchen",
        "clip_track": "bälle tracken",
        "clip_write": "clip schreiben",
        "done": "abgeschlossen",
    };
    $("ltp-phase").textContent = phaseMap[job.phase] || job.phase || "";

    // Progress
    if (job.phase === "scan" && job.total > 0) {
        const pct = Math.round((job.current / job.total) * 100);
        $("ltp-progress").textContent = `${pct}% (frame ${job.current.toLocaleString()} / ${job.total.toLocaleString()})`;
    } else if (job.total > 0) {
        $("ltp-progress").textContent = `${job.current} / ${job.total}`;
    } else {
        $("ltp-progress").textContent = "";
    }

    // Ranges live
    if (job.phase === "scan") {
        const cnt = job.ranges_so_far_count ?? 0;
        $("ltp-ranges").textContent = cnt > 0
            ? `top-view-bereiche bisher: ${cnt}`
            : "(noch keine top-view-bereiche gefunden)";
    } else if (job.status === "done") {
        $("ltp-ranges").textContent = `${job.clips_found ?? 0} clips aus ${job.ranges_found ?? 0} bereichen`;
    } else if (job.status === "error") {
        $("ltp-ranges").textContent = `${job.error || ""}`;
    } else {
        $("ltp-ranges").textContent = "";
    }

    // Live-Preview-Bild — dediziertes Polling auf nur DIESES img-Element
    if (job.status === "running") {
        ensureLivePreviewPolling(vid);
    } else {
        stopAllPreviewPolling();
        // Bei done/error wenigstens noch einmal das aktuelle Bild zeigen
        $("ltp-preview").src = `/api/preview/${vid}?t=${Date.now()}`;
    }
}

// Live-Preview-Polling — schreibt direkt auf das #ltp-preview img Element,
// das nie zerstoert wird (nur sein src). Damit kein Race mit renderHistory.
function ensureLivePreviewPolling(vid) {
    if (state.previewTimers.__live && state.previewTimers.__liveVid === vid) return;
    stopAllPreviewPolling();
    state.previewTimers.__liveVid = vid;
    const tick = () => {
        const img = $("ltp-preview");
        if (img) img.src = `/api/preview/${vid}?t=${Date.now()}`;
    };
    tick();   // sofort einmal
    state.previewTimers.__live = setInterval(tick, 1500);
}

function stopAllPreviewPolling() {
    if (state.previewTimers.__live) {
        clearInterval(state.previewTimers.__live);
        delete state.previewTimers.__live;
        delete state.previewTimers.__liveVid;
    }
}

// =========================================================================
// History
// =========================================================================

async function loadHistory() {
    try {
        const r = await fetch("/api/history");
        const d = await r.json();
        state.historyVideos = d.videos || [];
        renderHistory();
    } catch (e) {
        console.error(e);
    }
}

function renderHistory() {
    const wrap = $("history-list");
    wrap.innerHTML = "";
    if (state.historyVideos.length === 0) {
        $("history-empty").style.display = "block";
        return;
    }
    $("history-empty").style.display = "none";
    state.historyVideos.forEach(v => {
        const row = document.createElement("div");
        row.className = "history-row";
        row.dataset.vid = v.video_id;

        // Extras: Dauer + Clip-Anzahl
        const durTxt = v.duration_s != null ? fmtDuration(v.duration_s) : "?";
        const clipsTxt = v.clips_count != null ? `${v.clips_count} clips` : "noch nicht getrackt";
        const clipsCss = v.clips_count > 0 ? "ok" : "";
        const trackedTxt = v.last_tracked_at ? `getrackt ${v.last_tracked_at}` : "";

        const hasClips = v.clips && v.clips.length > 0;
        const clipsExpanded = state.expandedClipVids.has(v.video_id);
        const displayTitle = v.title_en || v.title || v.video_id;

        row.innerHTML = `
            <img class="history-preview" src="/api/thumb/${v.video_id}?t=${Date.now()}"
                 data-vid="${v.video_id}" onerror="this.style.display='none'">
            <div class="history-info">
                <div class="history-title">${escapeHtml(displayTitle)}</div>
                <div class="muted small">${v.video_id} · ${v.date || "?"} · ${escapeHtml(v.channel || "")}</div>
                <div class="history-meta-extras">
                    <span>dauer: ${durTxt}</span>
                    <span class="${clipsCss}">${clipsTxt}</span>
                    ${trackedTxt ? `<span>${trackedTxt}</span>` : ""}
                </div>
                <div class="history-path">${escapeHtml(v.folder || "")}</div>
            </div>
            <div class="history-actions">
                <button class="btn-track" data-vid="${v.video_id}">tracken</button>
                ${hasClips ? `<button class="btn-toggle-clips small" data-vid="${v.video_id}">${clipsExpanded ? "− clips" : "+ clips"}</button>` : ""}
                <button class="btn-files small" data-vid="${v.video_id}">dateien</button>
                <button class="btn-del danger small" data-vid="${v.video_id}">×</button>
            </div>
            <div class="history-clips ${clipsExpanded ? "" : "hidden"}" data-clips-for="${v.video_id}"></div>
            <div class="history-files hidden" data-files-for="${v.video_id}"></div>
        `;
        wrap.appendChild(row);

        // Preview-Klick: Original-Video abspielen
        row.querySelector(".history-preview")?.addEventListener("click", () => {
            playArchiveVideo(v);
        });

        if (clipsExpanded && hasClips) {
            fillClipsArea(v);
        }
    });

    $$(".btn-track").forEach(b => b.addEventListener("click", () => openTrackModal(b.dataset.vid)));
    $$(".btn-files").forEach(b => b.addEventListener("click", () => toggleFiles(b.dataset.vid)));
    $$(".btn-del").forEach(b => b.addEventListener("click", () => deleteHistory(b.dataset.vid)));
    $$(".btn-toggle-clips").forEach(b => b.addEventListener("click", () => toggleClips(b.dataset.vid)));
}

function fillClipsArea(v) {
    const panel = document.querySelector(`[data-clips-for="${v.video_id}"]`);
    if (!panel) return;
    if (!v.clips || v.clips.length === 0) {
        panel.innerHTML = `<div class="muted small">keine clips vorhanden</div>`;
        return;
    }
    let html = `<div class="clip-grid">`;
    v.clips.forEach(c => {
        const framesTxt = c.frames != null ? `${c.frames}f` : "";
        const thumbSrc = c.thumb
            ? `/api/file/${v.video_id}/${encodeURIComponent(c.thumb)}`
            : "";
        html += `<div class="clip-card" data-vid="${v.video_id}" data-clip="${c.mp4}" data-clipname="${c.name}">
            ${thumbSrc ? `<img class="clip-thumb" src="${thumbSrc}" alt="">` : `<div class="clip-thumb clip-thumb-placeholder">▶</div>`}
            <div class="clip-card-name">${c.name}</div>
            <div class="clip-card-meta">${framesTxt}</div>
        </div>`;
    });
    html += "</div>";
    panel.innerHTML = html;
    panel.querySelectorAll(".clip-card").forEach((card, idx) => {
        card.addEventListener("click", () => {
            setPlayerContext(v.video_id, v.clips, idx);
            playArchiveFile(card.dataset.vid, card.dataset.clip, card.dataset.clipname);
        });
    });
}

function toggleClips(vid) {
    const v = state.historyVideos.find(x => x.video_id === vid);
    if (!v) return;
    const panel = document.querySelector(`[data-clips-for="${vid}"]`);
    if (!panel) return;
    const isOpen = state.expandedClipVids.has(vid);
    if (isOpen) {
        state.expandedClipVids.delete(vid);
        panel.classList.add("hidden");
    } else {
        state.expandedClipVids.add(vid);
        panel.classList.remove("hidden");
        fillClipsArea(v);
    }
    // Toggle Button-Text
    const btn = document.querySelector(`.btn-toggle-clips[data-vid="${vid}"]`);
    if (btn) btn.textContent = state.expandedClipVids.has(vid) ? "− clips" : "+ clips";
}

async function playArchiveVideo(v) {
    // Original-Datei aus dem Ordner finden
    const r = await fetch(`/api/video-folder/${v.video_id}`);
    const d = await r.json();
    const vfile = (d.files || []).find(f => f.is_video);
    if (!vfile) {
        alert("keine video-datei gefunden");
        return;
    }
    clearPlayerContext();
    playArchiveFile(v.video_id, vfile.name, v.title_en || v.title || v.video_id);
}

function playArchiveFile(vid, filename, title) {
    $("player-title").textContent = title || filename;
    $("player-caption").textContent = `${vid} · ${filename}`;
    const vp = $("player-video");
    vp.pause();
    vp.src = `/api/file/${vid}/${encodeURIComponent(filename)}`;
    vp.load();
    $("modal-player").classList.remove("hidden");
    vp.play().catch(err => {
        console.debug("autoplay blocked:", err);
    });
}

// Player-Kontext: welche Clip-Liste gerade durchnavigiert wird, und an welcher
// Stelle wir sind. Wird beim Aufruf des Players gesetzt; ohne Kontext sind die
// Prev/Next-Buttons disabled.
function setPlayerContext(vid, clips, currentIndex) {
    state.playerContext = {
        vid,
        clips: clips || [],
        currentIndex: currentIndex,
    };
    updatePlayerNavButtons();
}

function clearPlayerContext() {
    state.playerContext = null;
    updatePlayerNavButtons();
}

function updatePlayerNavButtons() {
    const ctx = state.playerContext;
    const prev = $("player-prev");
    const next = $("player-next");
    if (!ctx || !ctx.clips || ctx.clips.length <= 1) {
        prev.disabled = true;
        next.disabled = true;
        return;
    }
    prev.disabled = ctx.currentIndex <= 0;
    next.disabled = ctx.currentIndex >= ctx.clips.length - 1;
}

function playPlayerNav(delta) {
    const ctx = state.playerContext;
    if (!ctx) return;
    const newIdx = ctx.currentIndex + delta;
    if (newIdx < 0 || newIdx >= ctx.clips.length) return;
    ctx.currentIndex = newIdx;
    const c = ctx.clips[newIdx];
    playArchiveFile(ctx.vid, c.mp4, c.name);
    updatePlayerNavButtons();
}

// Cancel-Tracking-Button
document.addEventListener("DOMContentLoaded", () => {
    const cancelBtn = document.getElementById("btn-cancel-tracking");
    if (cancelBtn) {
        cancelBtn.addEventListener("click", async () => {
            const vid = cancelBtn.dataset.vid;
            if (!vid) return;
            if (!confirm("tracking abbrechen?")) return;
            try {
                await fetch("/api/track/cancel", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({video_id: vid}),
                });
            } catch (e) {
                console.error(e);
            }
        });
    }
    const prevBtn = document.getElementById("player-prev");
    const nextBtn = document.getElementById("player-next");
    if (prevBtn) prevBtn.addEventListener("click", () => playPlayerNav(-1));
    if (nextBtn) nextBtn.addEventListener("click", () => playPlayerNav(1));
});

// Beim Schliessen des Players: src clearen damit es nicht weiterspielt
document.addEventListener("click", (ev) => {
    const tgt = ev.target;
    if (tgt && tgt.dataset && tgt.dataset.closeModal === "modal-player") {
        const vp = $("player-video");
        vp.pause();
        vp.removeAttribute("src");
        vp.load();
    }
});

async function toggleFiles(vid) {
    const panel = document.querySelector(`[data-files-for="${vid}"]`);
    if (!panel) return;
    if (!panel.classList.contains("hidden")) {
        panel.classList.add("hidden");
        return;
    }
    const r = await fetch(`/api/video-folder/${vid}`);
    const d = await r.json();
    let html = `<div class="files-grid">`;
    (d.files || []).forEach(f => {
        const sizeKb = (f.size / 1024).toFixed(0);
        html += `<div class="file-entry">
            <a href="/api/file/${vid}/${encodeURIComponent(f.name)}" target="_blank">${escapeHtml(f.name)}</a>
            <span class="muted small">${sizeKb} KB</span>
        </div>`;
    });
    html += "</div>";
    panel.innerHTML = html;
    panel.classList.remove("hidden");
}

async function deleteHistory(vid) {
    if (!confirm("aus der history loeschen? (datei bleibt auf der platte)")) return;
    await fetch("/api/history/delete", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({video_id: vid}),
    });
    loadHistory();
}

$("btn-rescan").addEventListener("click", async () => {
    const r = await fetch("/api/rescan", {method: "POST"});
    const d = await r.json();
    alert(`${d.new_videos || 0} neue videos hinzugefuegt`);
    loadHistory();
});

// =========================================================================
// Track Modal
// =========================================================================

function openTrackModal(vid) {
    const v = state.historyVideos.find(x => x.video_id === vid);
    if (!v) return;
    $("track-video-name").textContent = v.title_en || v.title || vid;
    $("modal-track").dataset.videoId = vid;
    $("modal-track").dataset.mode = "track_only";
    delete $("modal-track").dataset.payload;

    const sel = $("track-setting-select");
    sel.innerHTML = "";
    if (state.settings.length === 0) {
        loadSettings().then(() => populateTrackSettings(v));
    } else {
        populateTrackSettings(v);
    }
    $("track-warning").textContent = "";
    $("modal-track").classList.remove("hidden");
}

function populateTrackSettings(v) {
    const sel = $("track-setting-select");
    sel.innerHTML = "";
    if (state.settings.length === 0) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "— noch keine settings angelegt —";
        sel.appendChild(opt);
        $("track-warning").textContent = "erst im setup-tab ein setting erstellen";
        $("btn-track-start").disabled = true;
        return;
    }
    $("btn-track-start").disabled = false;
    $("track-warning").textContent = "";
    state.settings.forEach(s => {
        const opt = document.createElement("option");
        opt.value = s.id;
        const complete = s.table_corners?.length === 4 && s.ball_colors_hex?.length === 3;
        opt.textContent = `${s.name}${complete ? "" : "  (unvollstaendig)"}`;
        opt.disabled = !complete;
        if (v.last_setting === s.id) opt.selected = true;
        sel.appendChild(opt);
    });
}

$("btn-track-start").addEventListener("click", async () => {
    const sid = $("track-setting-select").value;
    if (!sid) {
        alert("setting waehlen");
        return;
    }
    const mode = $("modal-track").dataset.mode || "track_only";

    if (mode === "download_track") {
        const v = JSON.parse($("modal-track").dataset.payload || "{}");
        const r = await fetch("/api/download", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({videos: [v], auto_track_setting: sid}),
        });
        const d = await r.json();
        if (d.error) { alert("fehler: " + d.error); return; }
        closeModal("modal-track");
        switchView("queue");
        startPolling();
        return;
    }

    // track_only
    const vid = $("modal-track").dataset.videoId;
    const r = await fetch("/api/track", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({video_id: vid, setting_id: sid}),
    });
    const d = await r.json();
    if (d.error) {
        alert("fehler: " + d.error);
        return;
    }
    closeModal("modal-track");
    startPolling();
});

// =========================================================================
// Modal handling
// =========================================================================

$$('[data-close-modal]').forEach(b => {
    b.addEventListener("click", () => closeModal(b.dataset.closeModal));
});

function closeModal(id) {
    $(id).classList.add("hidden");
}

// =========================================================================
// Global settings modal
// =========================================================================

$("btn-global").addEventListener("click", async () => {
    const r = await fetch("/api/global");
    const d = await r.json();
    const g = d.global || {};
    $$('#modal-global [data-gparam]').forEach(inp => {
        const k = inp.dataset.gparam;
        if (k in g) inp.value = g[k];
    });
    $("modal-global").classList.remove("hidden");
});

$("btn-save-global").addEventListener("click", async () => {
    const payload = {};
    $$('#modal-global [data-gparam]').forEach(inp => {
        const k = inp.dataset.gparam;
        const v = inp.value;
        payload[k] = (inp.type === "number") ? parseFloat(v) : v;
    });
    await fetch("/api/global", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
    });
    closeModal("modal-global");
});

// =========================================================================
// Config modal
// =========================================================================

$("btn-config").addEventListener("click", async () => {
    const r = await fetch("/api/config");
    const d = await r.json();
    $("config-download-dir").value = d.download_dir || "";
    $("modal-config").classList.remove("hidden");
});

$("btn-save-config").addEventListener("click", async () => {
    const dir = $("config-download-dir").value.trim();
    const r = await fetch("/api/config", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({download_dir: dir}),
    });
    const d = await r.json();
    if (d.error) {
        alert("fehler: " + d.error);
        return;
    }
    closeModal("modal-config");
});

// =========================================================================
// SETUP TAB
// =========================================================================

async function loadSettings() {
    const r = await fetch("/api/settings");
    const d = await r.json();
    state.settings = d.settings || [];
    renderSettingList();
}

function renderSettingList() {
    const ul = $("setting-list");
    ul.innerHTML = "";
    state.settings.forEach(s => {
        const li = document.createElement("li");
        li.className = "setting-li";
        if (s.id === state.activeSettingId) li.classList.add("active");
        const complete = s.table_corners?.length === 4 && s.ball_colors_hex?.length === 3;
        li.innerHTML = `
            <span class="setting-name">${escapeHtml(s.name)}</span>
            <span class="setting-status">${complete ? "✓" : "○"}</span>
        `;
        li.addEventListener("click", () => loadSettingIntoEditor(s.id));
        ul.appendChild(li);
    });
}

$("btn-new-setting").addEventListener("click", () => {
    const newSetting = {
        id: "",  // backend vergibt eine
        name: "neues setting",
        table_corners: [],
        felt_color_hex: "#1c5f3a",
        felt_tolerance: 40,
        ball_colors_hex: [],
        reference_video_id: "",
        reference_frame_time: 0,
        reference_frame_size: [1920, 1080],
    };
    fillEditor(newSetting);
    state.activeSettingId = "";
    showEditor();
    // Direkt im Namens-Feld landen, Text vorausgewählt
    setTimeout(() => {
        const nameInput = $("setting-name");
        nameInput.focus();
        nameInput.select();
    }, 50);
});

function loadSettingIntoEditor(sid) {
    const s = state.settings.find(x => x.id === sid);
    if (!s) return;
    state.activeSettingId = sid;
    fillEditor(s);
    renderSettingList();
    showEditor();
}

function showEditor() {
    $("setup-editor").classList.remove("hidden");
    $("setup-empty").style.display = "none";
}

function fillEditor(s) {
    $("setting-name").value = s.name || "";
    $("setting-felt-hex").value = s.felt_color_hex || "#1c5f3a";
    $("setting-felt-picker").value = isValidHex(s.felt_color_hex) ? s.felt_color_hex : "#1c5f3a";
    $("setting-felt-tol").value = s.felt_tolerance || 40;

    // Ball-Farben
    for (let i = 0; i < 3; i++) {
        const hex = s.ball_colors_hex?.[i] || "";
        const inp = document.querySelector(`[data-ballcolor="${i}"]`);
        const pic = document.querySelector(`[data-ballpicker="${i}"]`);
        if (inp) inp.value = hex;
        if (pic) pic.value = isValidHex(hex) ? hex : BALL_RING_COLORS[i];
    }

    // Tisch-Ecken
    state.cornersInProgress = (s.table_corners || []).map(p => [p[0], p[1]]);
    state.ballClicksInProgress = (s.ball_colors_hex || []).map(h => ({hex: h}));

    // Wenn ein reference video drin ist und es in history existiert, auswaehlen
    const sel = $("setup-video-select");
    if (s.reference_video_id) {
        sel.value = s.reference_video_id;
        if (sel.value === s.reference_video_id) {
            loadSetupVideo(s.reference_video_id, s.reference_frame_time);
        }
    } else {
        sel.value = "";
        const vid = $("setup-video");
        vid.src = "";
    }

    state.currentFrame = null;
    setMode("none");
    updateStatusLabels();
    updateCornersReadout();
    redrawCanvas();
    $("setup-canvas-hint").textContent = 'erst „frame greifen" druecken';
    // Live-Preview-Status zuruecksetzen
    resetTrackingState();
    state.liveImage = null;
    showLivePlaceholder("erst tisch-ecken UND ball-farben setzen, dann erscheint hier die rektifizierte top-down-ansicht mit ball-erkennung");
    setLivePreviewStatus("");
    setLiveStatusBadge("inaktiv");
}

async function loadHistoryForSetupSelect() {
    if (state.historyVideos.length === 0) await loadHistory();
    const sel = $("setup-video-select");
    sel.innerHTML = '<option value="">— waehlen —</option>';
    state.historyVideos.forEach(v => {
        const opt = document.createElement("option");
        opt.value = v.video_id;
        opt.textContent = `${v.title_en || v.title || v.video_id}`;
        sel.appendChild(opt);
    });
}

$("setup-video-select").addEventListener("change", () => {
    const vid = $("setup-video-select").value;
    if (!vid) return;
    loadSetupVideo(vid, 0);
});

async function loadSetupVideo(vid, t) {
    // Finde den eigentlichen Dateinamen
    const r = await fetch(`/api/video-folder/${vid}`);
    const d = await r.json();
    const vfile = (d.files || []).find(f => f.is_video);
    if (!vfile) {
        alert("keine video-datei gefunden");
        return;
    }
    const v = $("setup-video");
    v.src = `/api/file/${vid}/${encodeURIComponent(vfile.name)}`;
    v.load();
    if (t > 0) {
        v.addEventListener("loadedmetadata", () => { v.currentTime = t; }, {once: true});
    }
    state.currentFrame = null;
    redrawCanvas();
}

// ---- Frame grab ----------------------------------------------------------

$("btn-grab-frame").addEventListener("click", () => {
    const video = $("setup-video");
    if (!video.src || video.readyState < 2) {
        alert("erst ein video laden und abspielen lassen bis der frame da ist");
        return;
    }
    const canvas = $("setup-canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(video, 0, 0);
    const b64 = canvas.toDataURL("image/jpeg", 0.9);
    state.currentFrame = {
        b64,
        naturalWidth: video.videoWidth,
        naturalHeight: video.videoHeight,
    };
    $("grab-frame-info").textContent = `frame: ${video.videoWidth}x${video.videoHeight} @ t=${video.currentTime.toFixed(2)}s`;
    $("setup-canvas-hint").textContent = "frame im canvas — wechsle in den passenden modus und klicke. mausrad = zoom, drag im betrachten-modus = verschieben";
    zoomFit();
    redrawCanvas();
});

// ---- Mode handling -------------------------------------------------------

$$(".mode-btn").forEach(b => {
    if (b.id === "btn-reset-clicks") return;
    b.addEventListener("click", () => setMode(b.dataset.mode));
});

function setMode(mode) {
    state.clickMode = mode;
    $$(".mode-btn").forEach(b => {
        if (b.id === "btn-reset-clicks") return;
        b.classList.toggle("active", b.dataset.mode === mode);
    });
    const hint = {
        none: "betrachten-modus — klick+ziehen zum verschieben, mausrad zum zoomen",
        corners: `tisch-ecken: klicke ${4 - state.cornersInProgress.length} weitere ecke(n) (reihenfolge: TL, TR, BR, BL)`,
        balls: state.ballClicksInProgress.length < 3
            ? `ball-farben: klicke auf den ${BALL_LABELS[state.ballClicksInProgress.length]}en ball`
            : "alle 3 ball-farben gesetzt — wechsle modus oder reset",
    }[mode];
    $("setup-canvas-hint").textContent = hint;
    const canvas = $("setup-canvas");
    canvas.style.cursor = (mode === "none") ? "grab" : "crosshair";
}

$("btn-reset-clicks").addEventListener("click", () => {
    if (!confirm("alle ecken und ball-farben verwerfen?")) return;
    state.cornersInProgress = [];
    state.ballClicksInProgress = [];
    for (let i = 0; i < 3; i++) {
        const inp = document.querySelector(`[data-ballcolor="${i}"]`);
        if (inp) inp.value = "";
    }
    updateStatusLabels();
    updateCornersReadout();
    redrawCanvas();
    setMode(state.clickMode);
    resetTrackingState();
    updateLivePreview();
});

// ---- Canvas clicks -------------------------------------------------------

$("setup-canvas").addEventListener("click", async (ev) => {
    if (!state.currentFrame) return;
    const canvas = $("setup-canvas");
    const rect = canvas.getBoundingClientRect();
    // Klick-Position im canvas-display-koordinatensystem
    const dispX = ev.clientX - rect.left;
    const dispY = ev.clientY - rect.top;
    // Skalieren zur tatsaechlichen canvas-Aufloesung
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    const x = Math.round(dispX * scaleX);
    const y = Math.round(dispY * scaleY);

    if (state.clickMode === "corners") {
        if (state.cornersInProgress.length >= 4) {
            alert("schon 4 ecken — reset um neu zu setzen");
            return;
        }
        state.cornersInProgress.push([x, y]);
        updateStatusLabels();
        updateCornersReadout();
        redrawCanvas();

        if (state.cornersInProgress.length === 4) {
            // Filz auto-sample
            try {
                const r = await fetch("/api/editor/sample-felt", {
                    method: "POST", headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({
                        frame_b64: state.currentFrame.b64,
                        corners: state.cornersInProgress,
                    }),
                });
                const d = await r.json();
                if (d.hex) {
                    $("setting-felt-hex").value = d.hex;
                    if (isValidHex(d.hex)) $("setting-felt-picker").value = d.hex;
                    $("setup-canvas-hint").textContent = `tisch komplett. filz auto: ${d.hex}`;
                }
            } catch (e) { console.warn(e); }
            setMode("none");
            updateLivePreview();
        } else {
            setMode("corners");  // refresh hint
        }
    }
    else if (state.clickMode === "balls") {
        if (state.ballClicksInProgress.length >= 3) {
            alert("schon 3 ball-farben — reset um neu zu setzen");
            return;
        }
        try {
            const r = await fetch("/api/editor/sample-color", {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({frame_b64: state.currentFrame.b64, x, y}),
            });
            const d = await r.json();
            if (d.hex) {
                const idx = state.ballClicksInProgress.length;
                state.ballClicksInProgress.push({x, y, hex: d.hex});
                const inp = document.querySelector(`[data-ballcolor="${idx}"]`);
                const pic = document.querySelector(`[data-ballpicker="${idx}"]`);
                if (inp) inp.value = d.hex;
                if (pic && isValidHex(d.hex)) pic.value = d.hex;
                updateStatusLabels();
                redrawCanvas();
                if (state.ballClicksInProgress.length === 3) {
                    setMode("none");
                    updateLivePreview();
                } else {
                    setMode("balls");
                }
            }
        } catch (e) { console.warn(e); }
    }
});

function updateStatusLabels() {
    $("status-corners").textContent = `ecken: ${state.cornersInProgress.length}/4`;
    $("status-corners").classList.toggle("ok", state.cornersInProgress.length === 4);
    $("status-balls").textContent = `ball-farben: ${state.ballClicksInProgress.length}/3`;
    $("status-balls").classList.toggle("ok", state.ballClicksInProgress.length === 3);
}

function updateCornersReadout() {
    const el = $("corners-readout");
    if (state.cornersInProgress.length === 0) {
        el.textContent = "noch nicht gesetzt";
        return;
    }
    const labels = ["TL", "TR", "BR", "BL"];
    el.innerHTML = state.cornersInProgress.map(
        (p, i) => `<div>${labels[i] || "?"}: (${p[0]}, ${p[1]})</div>`
    ).join("");
}

function redrawCanvas() {
    const canvas = $("setup-canvas");
    if (!state.currentFrame) {
        // Leer machen
        if (canvas.getContext) {
            canvas.width = 800;
            canvas.height = 450;
            canvas.style.width = "";
            canvas.style.height = "";
            const ctx = canvas.getContext("2d");
            ctx.fillStyle = "#1a1a1a";
            ctx.fillRect(0, 0, canvas.width, canvas.height);
            ctx.fillStyle = "#666";
            ctx.font = "16px JetBrains Mono, monospace";
            ctx.textAlign = "center";
            ctx.fillText("frame greifen", canvas.width / 2, canvas.height / 2);
        }
        return;
    }
    const img = new Image();
    img.onload = () => {
        canvas.width = img.width;
        canvas.height = img.height;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0);
        drawOverlay(ctx);
        applyZoom();
    };
    img.src = state.currentFrame.b64;
}

function drawOverlay(ctx) {
    // Ecken + Polygon
    const corners = state.cornersInProgress;
    if (corners.length > 0) {
        ctx.strokeStyle = "rgba(255, 200, 0, 0.9)";
        ctx.lineWidth = 3;
        ctx.beginPath();
        corners.forEach((p, i) => {
            if (i === 0) ctx.moveTo(p[0], p[1]);
            else ctx.lineTo(p[0], p[1]);
        });
        if (corners.length === 4) ctx.closePath();
        ctx.stroke();

        ctx.font = "bold 28px JetBrains Mono, monospace";
        corners.forEach((p, i) => {
            ctx.fillStyle = "rgba(0, 0, 0, 0.7)";
            ctx.beginPath();
            ctx.arc(p[0], p[1], 18, 0, Math.PI * 2);
            ctx.fill();
            ctx.fillStyle = "rgba(255, 200, 0, 1)";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillText(String(i + 1), p[0], p[1]);
        });
    }
    // Ball-Klicks
    const balls = state.ballClicksInProgress;
    balls.forEach((b, i) => {
        if (b.x == null) return;
        ctx.strokeStyle = BALL_RING_COLORS[i];
        ctx.lineWidth = 4;
        ctx.beginPath();
        ctx.arc(b.x, b.y, 20, 0, Math.PI * 2);
        ctx.stroke();
        ctx.fillStyle = b.hex || BALL_RING_COLORS[i];
        ctx.beginPath();
        ctx.arc(b.x, b.y, 14, 0, Math.PI * 2);
        ctx.fill();
        // Label
        ctx.font = "bold 12px JetBrains Mono, monospace";
        ctx.fillStyle = "rgba(0,0,0,0.8)";
        ctx.fillRect(b.x + 20, b.y - 10, 50, 20);
        ctx.fillStyle = BALL_RING_COLORS[i];
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillText(BALL_LABELS[i], b.x + 24, b.y);
    });
}

// ---- Zoom & Pan ----------------------------------------------------------

function applyZoom() {
    const canvas = $("setup-canvas");
    if (!canvas.width || !canvas.height) return;
    const newW = Math.round(canvas.width * state.zoom);
    const newH = Math.round(canvas.height * state.zoom);
    canvas.style.width = newW + "px";
    canvas.style.height = newH + "px";
    $("zoom-level").textContent = `${Math.round(state.zoom * 100)}%`;
}

function zoomFit() {
    const canvas = $("setup-canvas");
    const wrap = canvas.parentElement;
    if (!canvas.width || !wrap) return;
    // padding/scrollbars beruecksichtigen, ein bisschen Luft lassen
    const availW = wrap.clientWidth - 2;
    const availH = wrap.clientHeight - 2;
    const z = Math.min(availW / canvas.width, availH / canvas.height);
    state.zoom = Math.max(state.minZoom, Math.min(state.maxZoom, z));
    applyZoom();
    // Zentrieren
    wrap.scrollLeft = (canvas.width * state.zoom - availW) / 2;
    wrap.scrollTop = (canvas.height * state.zoom - availH) / 2;
}

function setZoom100() {
    state.zoom = 1.0;
    applyZoom();
}

function zoomAtViewport(newZoom, vpX, vpY) {
    // Zoom so, dass der canvas-pixel unter (vpX, vpY) dort bleibt wo er ist
    const canvas = $("setup-canvas");
    const wrap = canvas.parentElement;
    const wrapRect = wrap.getBoundingClientRect();

    // Position innerhalb des wrappers (viewport-relativ)
    const wrapX = vpX - wrapRect.left;
    const wrapY = vpY - wrapRect.top;

    // Position im "content"-koord (inkl. scroll)
    const contentX = wrapX + wrap.scrollLeft;
    const contentY = wrapY + wrap.scrollTop;

    // Normalisiert (0..1) bezogen auf alte display-groesse
    const oldDispW = canvas.width * state.zoom;
    const oldDispH = canvas.height * state.zoom;
    if (oldDispW <= 0 || oldDispH <= 0) return;
    const normX = contentX / oldDispW;
    const normY = contentY / oldDispH;

    state.zoom = Math.max(state.minZoom, Math.min(state.maxZoom, newZoom));
    applyZoom();

    const newDispW = canvas.width * state.zoom;
    const newDispH = canvas.height * state.zoom;
    wrap.scrollLeft = normX * newDispW - wrapX;
    wrap.scrollTop = normY * newDispH - wrapY;
}

$("btn-zoom-in").addEventListener("click", () => {
    const wrap = $("setup-canvas").parentElement;
    const r = wrap.getBoundingClientRect();
    zoomAtViewport(state.zoom * 1.25, r.left + r.width / 2, r.top + r.height / 2);
});

$("btn-zoom-out").addEventListener("click", () => {
    const wrap = $("setup-canvas").parentElement;
    const r = wrap.getBoundingClientRect();
    zoomAtViewport(state.zoom / 1.25, r.left + r.width / 2, r.top + r.height / 2);
});

$("btn-zoom-fit").addEventListener("click", zoomFit);
$("btn-zoom-100").addEventListener("click", setZoom100);

// Mausrad-Zoom
$("setup-canvas").addEventListener("wheel", (ev) => {
    if (!state.currentFrame) return;
    ev.preventDefault();
    const factor = ev.deltaY < 0 ? 1.15 : 1 / 1.15;
    zoomAtViewport(state.zoom * factor, ev.clientX, ev.clientY);
}, { passive: false });

// Drag-to-pan im "betrachten"-modus
$("setup-canvas").addEventListener("mousedown", (ev) => {
    if (state.clickMode !== "none") return;
    if (ev.button !== 0) return;
    state.panning = true;
    state.panLastX = ev.clientX;
    state.panLastY = ev.clientY;
    $("setup-canvas").parentElement.classList.add("panning");
    ev.preventDefault();
});

window.addEventListener("mousemove", (ev) => {
    if (!state.panning) return;
    const dx = ev.clientX - state.panLastX;
    const dy = ev.clientY - state.panLastY;
    state.panLastX = ev.clientX;
    state.panLastY = ev.clientY;
    const wrap = $("setup-canvas").parentElement;
    wrap.scrollLeft -= dx;
    wrap.scrollTop -= dy;
});

window.addEventListener("mouseup", () => {
    if (state.panning) {
        state.panning = false;
        $("setup-canvas").parentElement.classList.remove("panning");
    }
});

// Middle-click drag immer als Pan, egal in welchem Modus
$("setup-canvas").addEventListener("auxclick", (ev) => {
    // verhindert dass Browser-Default das Auto-Scroll triggert
    if (ev.button === 1) ev.preventDefault();
});

// ---- Live Tracking Preview ----------------------------------------------

function grabVideoFrameDataURL(quality = 0.7) {
    const video = $("setup-video");
    if (!video.src || video.readyState < 2 ||
        !video.videoWidth || !video.videoHeight) return null;
    const c = document.createElement("canvas");
    c.width = video.videoWidth;
    c.height = video.videoHeight;
    const ctx = c.getContext("2d");
    ctx.drawImage(video, 0, 0);
    return c.toDataURL("image/jpeg", quality);
}

function isCurrentSettingComplete() {
    if (state.cornersInProgress.length !== 4) return false;
    for (let i = 0; i < 3; i++) {
        const v = (document.querySelector(`[data-ballcolor="${i}"]`)?.value || "").trim();
        if (!isValidHex(v)) return false;
    }
    return true;
}

function collectCurrentSettingForDetect() {
    const ballHexes = [];
    for (let i = 0; i < 3; i++) {
        const v = (document.querySelector(`[data-ballcolor="${i}"]`)?.value || "").trim();
        if (v) ballHexes.push(v);
    }
    return {
        table_corners: state.cornersInProgress,
        felt_color_hex: ($("setting-felt-hex").value || "#1c5f3a").trim(),
        felt_tolerance: parseInt($("setting-felt-tol").value) || 40,
        ball_colors_hex: ballHexes,
    };
}

function setLivePreviewStatus(text, css) {
    const el = $("live-stats");
    el.textContent = text || "";
    el.classList.remove("ok", "err");
    if (css) el.classList.add(css);
}

function setLiveStatusBadge(text, css) {
    const el = $("live-status-text");
    el.textContent = text || "";
    el.classList.remove("active", "ok", "err");
    if (css) el.classList.add(css);
}

function showLivePlaceholder(text) {
    const ph = $("live-placeholder");
    if (text !== undefined) ph.textContent = text;
    ph.style.display = "flex";
}

function hideLivePlaceholder() {
    $("live-placeholder").style.display = "none";
}

function resetLiveTrail() {
    state.liveTrail = { weiss: [], gelb: [], rot: [] };
    if (state.liveImage) renderLiveCanvas();
}

function renderLiveCanvas() {
    const canvas = $("live-canvas");
    const img = state.liveImage;
    if (!canvas || !img) return;
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0);

    // Trail-Linien: durchgaengig, ueberspannen Luecken (User-Wunsch).
    // Filter etwaige Alt-Daten (null-Marker aus frueheren Versionen) raus.
    for (let i = 0; i < BALL_LABELS.length; i++) {
        const cls = BALL_LABELS[i];
        const trail = (state.liveTrail[cls] || []).filter(p => p !== null);
        if (trail.length < 2) continue;
        const color = BALL_RING_COLORS[i];

        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.strokeStyle = color;
        ctx.lineWidth = 3;
        ctx.beginPath();
        ctx.moveTo(trail[0][0], trail[0][1]);
        for (let j = 1; j < trail.length; j++) ctx.lineTo(trail[j][0], trail[j][1]);
        ctx.stroke();
    }

    // Anfangsposition pro Ball: gefuellter Kreis mit dunkler Outline (oben drueber)
    for (let i = 0; i < BALL_LABELS.length; i++) {
        const cls = BALL_LABELS[i];
        const trail = (state.liveTrail[cls] || []).filter(p => p !== null);
        if (trail.length === 0) continue;
        const [x, y] = trail[0];
        const color = BALL_RING_COLORS[i];
        ctx.fillStyle = color;
        ctx.strokeStyle = "rgba(0,0,0,0.85)";
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(x, y, 14, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
    }

    // Aktuelle Ball-Marker aus trackingState (= letzte bekannte Position, null wenn weg)
    if (state.trackingState && state.trackingState.balls) {
        for (let i = 0; i < BALL_LABELS.length; i++) {
            const cls = BALL_LABELS[i];
            const pos = state.trackingState.balls[cls];
            if (!pos) continue;
            const [x, y] = pos;
            const color = BALL_RING_COLORS[i];
            ctx.strokeStyle = color;
            ctx.fillStyle = "rgba(0,0,0,0.3)";
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.arc(x, y, 15, 0, Math.PI * 2);
            ctx.fill();
            ctx.stroke();
            ctx.font = "bold 12px JetBrains Mono, monospace";
            ctx.fillStyle = "rgba(0,0,0,0.7)";
            ctx.fillRect(x + 18, y - 9, 44, 16);
            ctx.fillStyle = color;
            ctx.textAlign = "left";
            ctx.textBaseline = "middle";
            ctx.fillText(cls, x + 21, y);
        }
    }
}

function resetTrackingState() {
    state.trackingState = null;
    state.lastDetectionAt = 0;
    resetLiveTrail();
}

function statusLabel(s) {
    return ({
        "no_table":         "kein tisch",
        "waiting_init":     "wartet auf init",
        "tracking":         "tracking",
        "tracking_partial": "tracking (luecken)",
    })[s] || s || "inaktiv";
}

function statusCssClass(s, foundBalls) {
    if (s === "tracking" && foundBalls === 3) return "ok";
    if (s === "tracking" || s === "tracking_partial") return "active";
    if (s === "no_table" || s === "waiting_init") return "";
    return "";
}

async function updateLivePreview(opts = {}) {
    if (state.livePreviewInFlight) return;
    if (!isCurrentSettingComplete()) {
        showLivePlaceholder("erst tisch-ecken UND ball-farben setzen, dann erscheint hier die rektifizierte top-down-ansicht mit ball-erkennung");
        setLivePreviewStatus("");
        setLiveStatusBadge("inaktiv");
        return;
    }

    // Frame holen: bevorzugt live vom Video, fallback statisch
    let frameB64;
    const video = $("setup-video");
    const videoIsPlaying = video && !video.paused && !video.ended && video.readyState >= 2;
    if (videoIsPlaying || opts.fromVideo) {
        frameB64 = grabVideoFrameDataURL();
    }
    if (!frameB64 && state.currentFrame) {
        frameB64 = state.currentFrame.b64;
    }
    if (!frameB64) {
        showLivePlaceholder("kein video-frame verfuegbar — video laden und abspielen");
        setLivePreviewStatus("");
        setLiveStatusBadge("kein video");
        return;
    }

    // dt seit letzter Detection
    const now = performance.now() / 1000;
    const dt = state.lastDetectionAt > 0 ? Math.max(0.01, Math.min(2.0, now - state.lastDetectionAt)) : 0.1;
    state.lastDetectionAt = now;

    state.livePreviewInFlight = true;
    setLiveStatusBadge("aktiv", "active");
    try {
        const setting = collectCurrentSettingForDetect();
        const r = await fetch("/api/setup/detect", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                frame_b64: frameB64,
                setting,
                tracking_state: state.trackingState,
                dt_s: dt,
            }),
        });
        const d = await r.json();
        if (d.error) {
            showLivePlaceholder(`fehler: ${d.error}`);
            setLivePreviewStatus("");
            setLiveStatusBadge("fehler", "err");
            return;
        }

        // Tracking-State immer uebernehmen.
        // Hinweis: Wenn das Backend den State zuruecksetzt (z.B. nach langer
        // no_table-Phase), zeichnet der Renderer den letzten Marker einfach
        // nicht mehr, der Trail bleibt aber komplett erhalten. Beim naechsten
        // Tracking wird die Linie direkt zur neuen Position weitergezogen
        // (User-Wunsch: Bereiche werden verbunden).
        state.trackingState = d.tracking_state;

        // Image asynchron laden, dann rendern
        const img = new Image();
        img.onload = () => {
            state.liveImage = img;
            // Trail-Update nur bei aktivem Tracking + sichtbarem Tisch
            if (d.table_visible && (d.status === "tracking" || d.status === "tracking_partial")) {
                for (const cls of BALL_LABELS) {
                    const pos = d.balls[cls];
                    if (pos) {
                        const trail = state.liveTrail[cls];
                        const last = trail.length > 0 ? trail[trail.length - 1] : null;
                        if (last === null ||
                            Math.abs(last[0] - pos[0]) > 0.5 ||
                            Math.abs(last[1] - pos[1]) > 0.5) {
                            trail.push(pos);
                            if (trail.length > state.liveMaxTrailLen) {
                                trail.splice(0, trail.length - state.liveMaxTrailLen);
                            }
                        }
                    }
                }
            }
            renderLiveCanvas();
            hideLivePlaceholder();
        };
        img.src = d.rectified_b64;

        // Stats-Zeile + Badge
        const trailLen = state.liveTrail.weiss.length + state.liveTrail.gelb.length + state.liveTrail.rot.length;
        let statsText;
        if (d.status === "no_table") {
            statsText = `kein tisch  ·  filz (9-pkt) ${d.felt_pct_polygon}%`;
        } else {
            statsText = `filz ${d.felt_pct_rect}%  ·  baelle ${d.found_balls}/3  ·  blobs ${d.blob_count}  ·  trail ${trailLen} pkt`;
        }
        const css = (d.status === "tracking" && d.found_balls === 3) ? "ok"
                  : (d.status === "no_table" || d.status === "waiting_init") ? ""
                  : (d.found_balls === 0) ? "err" : "";
        setLivePreviewStatus(statsText, css);
        setLiveStatusBadge(statusLabel(d.status), statusCssClass(d.status, d.found_balls));
    } catch (e) {
        console.warn("live preview fail", e);
        setLivePreviewStatus("netz-/server-fehler", "err");
        setLiveStatusBadge("fehler", "err");
    } finally {
        state.livePreviewInFlight = false;
    }
}

function startLivePreviewLoop() {
    if (state.livePreviewTimer) return;
    state.livePreviewTimer = setInterval(() => {
        if (!state.livePreviewInFlight) updateLivePreview({fromVideo: true});
    }, 200);
}

function stopLivePreviewLoop() {
    if (state.livePreviewTimer) {
        clearInterval(state.livePreviewTimer);
        state.livePreviewTimer = null;
    }
}

// Trail-Reset-Button — leert Trail UND Tracking-State (Re-Init beim naechsten Frame)
$("btn-trail-reset").addEventListener("click", () => {
    resetTrackingState();
    updateLivePreview();
});

// ---- Frame-Step ----------------------------------------------------------

function stepFrame(direction) {
    const v = $("setup-video");
    if (!v.src || v.readyState < 2) return;
    if (!v.paused) v.pause();
    const step = direction / state.assumedFps;
    const newT = Math.max(0, Math.min((v.duration || 1e9), v.currentTime + step));
    state.skipNextSeekReset = true;
    v.currentTime = newT;
    // seeked-Event triggert updateLivePreview
}

$("btn-frame-next").addEventListener("click", () => stepFrame(1));
$("btn-frame-prev").addEventListener("click", () => stepFrame(-1));

// Pfeiltasten — aber nur wenn Setup-Tab aktiv ist und kein Input fokussiert ist
document.addEventListener("keydown", (ev) => {
    if (!document.querySelector("#view-setup.active")) return;
    const t = document.activeElement;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
    if (ev.key === "ArrowRight") {
        ev.preventDefault();
        stepFrame(1);
    } else if (ev.key === "ArrowLeft") {
        ev.preventDefault();
        stepFrame(-1);
    }
});

// Video-Events anhaengen
(function wireVideoEvents() {
    const v = $("setup-video");
    if (!v) return;
    v.addEventListener("play", () => {
        startLivePreviewLoop();
    });
    v.addEventListener("pause", () => {
        stopLivePreviewLoop();
        updateLivePreview({fromVideo: true});
    });
    // Vor jedem seek/timeupdate die zuletzt bekannte Zeit merken
    v.addEventListener("timeupdate", () => {
        state.lastVideoTime = v.currentTime;
    });
    v.addEventListener("seeked", () => {
        const delta = Math.abs(v.currentTime - state.lastVideoTime);
        // Frame-Step: Trail beibehalten, sonst grosser Sprung → reset
        if (state.skipNextSeekReset) {
            state.skipNextSeekReset = false;
        } else if (delta > 0.5) {
            resetTrackingState();
        }
        state.lastVideoTime = v.currentTime;
        if (!state.livePreviewTimer) updateLivePreview({fromVideo: true});
    });
    v.addEventListener("ended", () => {
        stopLivePreviewLoop();
        updateLivePreview({fromVideo: true});
    });
    v.addEventListener("loadeddata", () => {
        resetTrackingState();
        state.lastVideoTime = v.currentTime;
        updateLivePreview({fromVideo: true});
    });
})();

// ---- Save / Delete -------------------------------------------------------

$("btn-save-setting").addEventListener("click", async () => {
    const name = $("setting-name").value.trim() || "unbenanntes setting";
    const felt = $("setting-felt-hex").value.trim() || "#1c5f3a";
    const tol = parseInt($("setting-felt-tol").value) || 40;

    const ballColors = [];
    for (let i = 0; i < 3; i++) {
        const inp = document.querySelector(`[data-ballcolor="${i}"]`);
        const v = (inp?.value || "").trim();
        if (v) ballColors.push(v);
    }

    const sel = $("setup-video-select");
    const refVid = sel.value || "";
    const refTime = parseFloat($("setup-video").currentTime || 0);
    const refSize = state.currentFrame
        ? [state.currentFrame.naturalWidth, state.currentFrame.naturalHeight]
        : [1920, 1080];

    const payload = {
        id: state.activeSettingId || "",
        name,
        table_corners: state.cornersInProgress.length === 4 ? state.cornersInProgress : [],
        felt_color_hex: felt,
        felt_tolerance: tol,
        ball_colors_hex: ballColors.length === 3 ? ballColors : [],
        reference_video_id: refVid,
        reference_frame_time: refTime,
        reference_frame_size: refSize,
    };

    const r = await fetch("/api/settings", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (d.error) {
        alert("fehler: " + d.error);
        return;
    }
    state.activeSettingId = d.setting.id;
    await loadSettings();
    renderSettingList();
    alert("gespeichert");
});

$("btn-delete-setting").addEventListener("click", async () => {
    if (!state.activeSettingId) return;
    if (!confirm("setting wirklich loeschen?")) return;
    await fetch(`/api/settings/${state.activeSettingId}`, {method: "DELETE"});
    state.activeSettingId = null;
    await loadSettings();
    $("setup-editor").classList.add("hidden");
    $("setup-empty").style.display = "block";
});

// ---- Color picker / Text sync --------------------------------------------

let _liveDebounceTimer = null;
function triggerLivePreviewDebounced() {
    clearTimeout(_liveDebounceTimer);
    _liveDebounceTimer = setTimeout(() => updateLivePreview(), 250);
}

$("setting-felt-picker").addEventListener("input", (e) => {
    $("setting-felt-hex").value = e.target.value;
    triggerLivePreviewDebounced();
});
$("setting-felt-hex").addEventListener("input", (e) => {
    if (isValidHex(e.target.value)) $("setting-felt-picker").value = e.target.value;
    triggerLivePreviewDebounced();
});
$("setting-felt-tol").addEventListener("input", triggerLivePreviewDebounced);

for (let i = 0; i < 3; i++) {
    const inp = document.querySelector(`[data-ballcolor="${i}"]`);
    const pic = document.querySelector(`[data-ballpicker="${i}"]`);
    if (pic) pic.addEventListener("input", () => {
        inp.value = pic.value;
        triggerLivePreviewDebounced();
    });
    if (inp) inp.addEventListener("input", () => {
        if (isValidHex(inp.value) && pic) pic.value = inp.value;
        triggerLivePreviewDebounced();
    });
}

// Enter im Name-Feld speichert
$("setting-name").addEventListener("keypress", (e) => {
    if (e.key === "Enter") {
        e.preventDefault();
        $("btn-save-setting").click();
    }
});

// =========================================================================
// Utils
// =========================================================================

function isValidHex(s) {
    return typeof s === "string" && /^#[0-9a-fA-F]{6}$/.test(s);
}

function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;")
        .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function fmtDuration(sec) {
    if (!sec) return "";
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    return `${m}:${String(s).padStart(2, "0")}`;
}

// =========================================================================
// Init
// =========================================================================

// Wenn ein Video schon laeuft (Polling vom Server)
refreshStatus().then(() => {
    // Polling startet sich selbst falls etwas aktiv ist
});
