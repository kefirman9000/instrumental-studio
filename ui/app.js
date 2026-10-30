"use strict";
/* Instrumental Studio UI */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
// This session's secret, written into the page by the app. Every change sent to the app carries it,
// so other web pages open in a browser can't drive the app.
const TOKEN = document.querySelector('meta[name="studio-token"]')?.content || "";
const api = {
  async get(path, timeoutMs = 15000) {
    const ctl = new AbortController(), t = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const r = await fetch(path, { signal: ctl.signal }); const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.statusText); noteAlive(); return j;
    } catch (e) { if (e.name === "AbortError") { noteStuck(); throw new Error("The app took too long to answer."); } throw e; }
    finally { clearTimeout(t); }
  },
  async post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Studio-Token": TOKEN }, body: JSON.stringify(body || {}) });
    const j = await r.json();
    if (!r.ok) { const e = new Error(j.error || r.statusText); e.data = j; throw e; }
    return j;
  },
};
const fmt = t => { t = Math.max(0, t || 0); const m = Math.floor(t / 60), s = Math.floor(t % 60); return `${m}:${String(s).padStart(2, "0")}`; };
const fmtP = t => { t = Math.max(0, t || 0); const m = Math.floor(t / 60), s = (t % 60).toFixed(1); return `${m}:${s.padStart(4, "0")}`; };
const pct = x => (x > 0 && x < 0.005 ? "<1%" : `${Math.round(100 * x)}%`);
const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
const nice = k => (k || "").replace(/_/g, " ");
const native = () => window.pywebview && window.pywebview.api;
// The app window connects its file pickers a moment after the page loads. A click in that moment used
// to fall back to the browser's upload (which copies the whole file); wait briefly for them instead.
const BOOTED_AT = Date.now();
// errors in the window go into app.log too, so an exported log shows both sides
const clientLog = msg => { try { api.post("/api/clientlog", { msg: String(msg).slice(0, 2000) }).catch(() => {}); } catch {} };
window.addEventListener("error", e => clientLog(`error: ${e.message} at ${e.filename}:${e.lineno}`));
window.addEventListener("unhandledrejection", e => clientLog(`unhandled: ${e.reason && (e.reason.stack || e.reason.message) || e.reason}`));
async function nativeReady() {
  if (native() && native().choose_song) return native();
  if (Date.now() - BOOTED_AT > 6000) return null;          // a normal browser: no app pickers coming
  return await new Promise(res => {
    const done = () => res(native() && native().choose_song ? native() : null);
    window.addEventListener("pywebviewready", done, { once: true });
    setTimeout(done, 2500);
  });
}

const st = {
  hear: (() => { try { return localStorage.getItem("hear") || "mix"; } catch { return "mix"; } })(),
  ov: null, rev: 0, specs: {}, mode: "damage", version: "repaired",
  view: { start: 0, pps: 20 }, sel: null, filter: "review", scope: null, picking: null,
  expanded: new Set(),
};

/* ------------------------------------------------------------------ toasts */
function toast(msg, opts = {}) {
  const t = $("#toast"); t.innerHTML = ""; t.className = "toast" + (opts.err ? " err" : "");
  const s = document.createElement("span"); s.textContent = msg; t.append(s);
  if (opts.action) { const b = document.createElement("button"); b.className = "ghost small"; b.textContent = opts.action[0]; b.onclick = opts.action[1]; t.append(b); }
  t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), opts.ms || 4500);
}
const fail = e => toast(e.message || String(e), { err: true, ms: 7000 });

/* ------------------------------------------------------------------ start / songs */
async function refreshState() {
  const s = await api.get("/api/state");
  st.state = s;
  const sel = $("#sepMode");
  if (!sel.options.length) {
    fillModelSelect(sel, s.models);
    sel.value = localStorage.getItem("sepMode") || "4stem";
    sel.dataset.auto = localStorage.getItem("sepMode") ? "" : "1";
  }
  renderModel(s); renderExtras(s); renderSpeedTip();
  const rec = $("#recent"); rec.innerHTML = "";
  if (s.projects.length) {
    const h = document.createElement("h2"); h.textContent = "Recent songs"; rec.append(h);
    for (const p of s.projects.slice(0, 8)) {
      const d = document.createElement("div"); d.className = "recent-item"; d.tabIndex = 0;
      d.innerHTML = `<b></b><span class="dim">${fmt(p.duration)}</span><span class="dim small">${new Date(p.modified * 1000).toLocaleDateString()}</span><button class="ghost small x" title="Delete this project">Delete</button>`;
      d.querySelector("b").textContent = p.name;
      d.onclick = () => loadProject(p.id);
      d.onkeydown = e => { if (e.key === "Enter") loadProject(p.id); };
      d.querySelector(".x").onclick = async e => {
        e.stopPropagation();
        if (!confirm(`Delete the project "${p.name}"? Exported files are kept.`)) return;
        await api.post("/api/delete_project", { id: p.id }); refreshState();
      };
      rec.append(d);
    }
  }
  return s;
}

async function loadProject(id) {
  const name = (st.state?.projects || []).find(p => p.id === id)?.name || "the song";
  try {
    showLoading(0.15, `Opening ${name}: loading its tracks`);
    await api.post("/api/load", { id });
    await openWorkspace(true);
  } catch (e) { fail(e); } finally { hideBusy(); }
}

function showWelcome(on) {
  $("#welcome").hidden = !on;
  $("#btnUndo").hidden = on; $("#btnExport").hidden = on;
  if (on) refreshState();
}

async function startSong(path) {
  try {
    await api.post("/api/open_song", { path });
    watchJob();
  } catch (e) {
    hideBusy();
    if (e.message === "need_model") needModel(); else fail(e);
  }
}

const modelReady = () => !!st.state?.model?.installed;
function needModel() {
  showWelcome(true);
  const card = $("#modelCard");
  card.classList.remove("attention"); void card.offsetWidth; card.classList.add("attention");
  card.scrollIntoView({ block: "center", behavior: "smooth" });
  setTimeout(() => card.classList.remove("attention"), 2500);
  toast(st.state?.model?.installing
    ? "The separation model is still downloading. Split the song once the light turns green; your song stays open in Songs."
    : "Splitting needs the separation model. Download it here; the light turns green when it's ready. Your song is saved in Songs.", { ms: 7000 });
}

async function upload(file, batch) {
  const r = await fetch("/api/upload", { method: "POST", headers: { "X-Filename": encodeURIComponent(file.name), "X-Batch": batch, "X-Studio-Token": TOKEN }, body: file });
  const j = await r.json(); if (!r.ok) throw new Error(j.error); return j;
}

$("#btnOpenSong").onclick = async () => {
  const nat = await nativeReady();
  if (nat) { const p = await nat.choose_song(); if (p) startSong(p); return; }
  $("#fileSong").click();
};
async function openSongFile(f) {
  showBusy(0.01, "Copying the song into the app");
  try { const j = await upload(f, "song" + Date.now()); startSong(j.path); } catch (err) { hideBusy(); fail(err); }
}
$("#fileSong").onchange = async e => {
  const f = e.target.files[0]; if (!f) return;
  await openSongFile(f);
  e.target.value = "";
};
let stemsAuto = false;          // set by "Import stems and build", used once by startStems
async function pickStems(auto) {
  stemsAuto = auto;
  const nat = await nativeReady();
  if (nat) { const p = await nat.choose_folder(); if (p) startStems(p); else stemsAuto = false; return; }
  $("#fileStems").click();
}
$("#btnOpenStems").onclick = () => pickStems(false);
$("#btnOpenStemsAuto").onclick = () => pickStems(true);
$("#fileStems").onchange = async e => {
  const files = [...e.target.files]; if (!files.length) return;
  await openStemFiles(files);
  e.target.value = "";
};
async function openStemFiles(files) {
  const batch = "stems" + Date.now();
  try {
    let folder;
    for (let i = 0; i < files.length; i++) { showBusy(i / files.length * 0.05, `Copying ${files[i].name}`); folder = (await upload(files[i], batch)).folder; }
    const name = files[0].name.replace(/\.[^.]+$/, "").replace(/[ _-]*(vocals?|drums|bass|other|instrumental|no_vocals)$/i, "") || "Imported stems";
    startStems(folder, name);
  } catch (err) { hideBusy(); fail(err); }
}

/* ---------- drag and drop: a song opens it; a set of stems (one named "vocals") imports them */
const AUDIO_RE = /\.(wav|mp3|flac|m4a|aac|ogg|oga|opus|aiff?|wma|mp4)$/i;
let dragDepth = 0;
const dragHasFiles = e => [...(e.dataTransfer?.types || [])].includes("Files");
window.addEventListener("dragenter", e => {
  if (!dragHasFiles(e)) return;
  e.preventDefault(); dragDepth++;
  if (S_busy()) return;
  const inEditor = $("#welcome").hidden && st.ov;
  $("#dropTitle").textContent = inEditor ? "Drop a song to start a new project" : "Drop a song to open it";
  $("#dropSub").textContent = inEditor
    ? `"${st.ov.meta.name}" is saved and stays in Songs. A set of stems (including the vocals) also starts a new project.`
    : "Or drop a full set of stems, including the vocals, to import them";
  $("#dropZone").hidden = false;
});
window.addEventListener("dragover", e => { if (dragHasFiles(e)) { e.preventDefault(); e.dataTransfer.dropEffect = S_busy() ? "none" : "copy"; } });
window.addEventListener("dragleave", e => { if (!dragHasFiles(e)) return; if (--dragDepth <= 0) { dragDepth = 0; $("#dropZone").hidden = true; } });
window.addEventListener("drop", async e => {
  if (!dragHasFiles(e)) return;
  e.preventDefault(); dragDepth = 0; $("#dropZone").hidden = true;
  if (S_busy()) { toast("Wait for the current song to finish processing, then drop the next one."); return; }
  const all = [...e.dataTransfer.files];
  const files = all.filter(f => AUDIO_RE.test(f.name) || (f.type || "").startsWith("audio/"));
  if (!files.length) { toast(all.length ? "That doesn't look like an audio file. Drop an MP3, WAV, FLAC, M4A or OGG." : "Drop audio files, not folders."); return; }
  stopPreview(); player.pause();
  if (files.length > 1 && files.some(f => /vocal|vox/i.test(f.name))) return (stemsAuto = false, openStemFiles)(files);
  if (files.length > 1) toast(`Opening ${files[0].name}. Drop one song at a time, or a full set of stems including the vocals.`, { ms: 6000 });
  openSongFile(files[0]);
});
function S_busy() { return !$("#busy").hidden || !!st.state?.job?.running; }
async function startStems(folder, name) {
  const auto = stemsAuto; stemsAuto = false;
  try { await api.post("/api/open_stems", { folder, name, auto }); watchJob(); } catch (e) { hideBusy(); fail(e); }
}
$("#btnDemo").onclick = async () => { try { await api.post("/api/demo"); watchJob(); } catch (e) { fail(e); } };
$("#btnSongs").onclick = () => { stopPreview(); $("#player").pause(); showWelcome(true); };
$("#sepMode").onchange = e => { localStorage.setItem("sepMode", e.target.value); e.target.dataset.auto = ""; renderSpeedTip(); };
// Without a graphics card the best-quality model (4 passes) is slow, so start with Fast until the person picks
function renderSpeedTip() {
  const m = st.state?.model || {}, sel = $("#sepMode");
  if (m.installed && /CPU/i.test(m.device || "") && sel.dataset.auto) sel.value = "4stem_fast";
  sel.title = speedTip(sel.value, /CPU/i.test(m.device || ""));
  if (st.ov) renderSteps();
}
$("#btnReveal").onclick = () => api.post("/api/reveal", {});

/* ------------------------------------------------------------------ busy */
let busySince = 0;
function showBusy(f, msg) {
  if ($("#busy").hidden) busySince = Date.now();
  const secs = Math.round((Date.now() - busySince) / 1000);
  $("#busyTime").textContent = secs >= 5 ? `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")} elapsed` : "";
  $("#btnBusyHelp").hidden = secs < 45;
  $("#busy").hidden = false; $("#busyMsg").textContent = msg; $("#busyBar").style.width = pct(f); $("#busyPct").textContent = pct(f);
  $("#btnStop").hidden = false; }
// a progress screen for opening songs (nothing to stop there)
function showLoading(f, msg) { showBusy(f, msg); $("#btnStop").hidden = true; $("#btnBusyHelp").hidden = true; }
function hideBusy() { $("#busy").hidden = true; }
async function watchJob() {
  for (;;) {
    let s;
    try { s = await api.get("/api/state"); }
    catch { await new Promise(r => setTimeout(r, 1000)); continue; }
    const j = s.job;
    if (j.stopped && !j.running) { hideBusy(); toast("Stopped."); if (s.has_project) await openWorkspace(); return; }
    if (j.error) { hideBusy(); fail(new Error(j.error)); return; }
    showBusy(j.progress, j.message || "Working");
    if (!j.running && s.has_project) {
      hideBusy(); await openWorkspace();
      const res = j.result;
      if (res && res.report) { showAutoReport(res.report, res.title); return; }
      if (res && res.new_ids && res.new_ids.length) {      // another model was tried for one part
        st.filter = "takes"; st.scope = null; select(res.new_ids[0]);
        toast(`Added to Review: ${res.new_ids.length} track${res.new_ids.length === 1 ? "" : "s"} from the other model.`
          + (res.skipped?.length ? ` It has no separate ${res.skipped.join(", ")}, so those stay as they are.` : "")
          + " Listen to before and after with all tracks, then Apply.", { ms: 9000 });
      }
      return;
    }
    if (!j.running && !s.has_project) { hideBusy(); return; }
    await new Promise(r => setTimeout(r, 400));
  }
}

/* ------------------------------------------------------------------ workspace */
async function openWorkspace(loading) {
  if (loading) showLoading(0.5, "Laying out the song");
  // the first look at a big song (many tracks) can take a while; don't call the app stuck for it
  const r = await api.get("/api/overview", 120000);
  if (loading) showLoading(0.8, "Drawing the instruments");
  st.rev = r.rev; st.ov = r.overview; st.sel = null; st.scope = null; st.specs = {};
  setTimeout(followWarm, 0);
  showWelcome(false);
  $("#workspace").hidden = false; $("#transport").hidden = false; $("#btnExport").disabled = false;
  $("#songName").textContent = st.ov.meta.name || "";
  buildLanes();
  await loadSpectros(st.ov.stems.length ? st.ov.stems.concat(["vocals"]) : ["original"], ["raw", "after", "damage"]);
  fitView();
  setVersion(st.version, true);
  renderAll();
}

async function loadSpectros(stems, kinds) {
  const { cols, rows } = st.ov.display;
  await Promise.all(stems.flatMap(k => kinds.map(async kind => {
    if ((k === "vocals" || k === "original") && kind !== "raw") return;
    const buf = new Uint8Array(await (await fetch(`/api/spectro?stem=${k}&kind=${kind}&r=${st.rev}`)).arrayBuffer());
    (st.specs[k] ||= {})[kind + "Data"] = buf;
  })));
  for (const k of stems) {
    const s = st.specs[k];
    for (const kind of ["raw", "after", "damage"]) s[kind] = s[kind] || null;
    s.raw = paintSpec(s.rawData, cols, rows, null);
    if (k !== "vocals" && k !== "original") {
      s.damage = paintSpec(s.rawData, cols, rows, { overlay: s.damageData, color: [229, 82, 122] });
      s.after = paintSpec(s.afterData, cols, rows, { fillFrom: s.rawData, color: [240, 176, 74] });
    }
  }
}

// spectrogram colour ramp: slate -> blue -> pale
const RAMP = (() => {
  const stops = [[0, [21, 25, 34]], [0.35, [36, 52, 86]], [0.6, [62, 98, 140]], [0.8, [128, 160, 190]], [1, [228, 234, 242]]];
  const out = new Uint8Array(256 * 3);
  for (let i = 0; i < 256; i++) {
    const x = i / 255; let j = 0; while (j < stops.length - 2 && x > stops[j + 1][0]) j++;
    const [a, ca] = stops[j], [b, cb] = stops[j + 1], t = (x - a) / (b - a);
    for (let c = 0; c < 3; c++) out[i * 3 + c] = ca[c] + (cb[c] - ca[c]) * t;
  }
  return out;
})();

function paintSpec(data, cols, rows, opt) {
  const cv = document.createElement("canvas"); cv.width = cols; cv.height = rows;
  const ctx = cv.getContext("2d"); const img = ctx.createImageData(cols, rows); const px = img.data;
  for (let r = 0; r < rows; r++) {
    const y = rows - 1 - r;
    for (let c = 0; c < cols; c++) {
      const v = data[r * cols + c]; const o = (y * cols + c) * 4;
      let R = RAMP[v * 3], G = RAMP[v * 3 + 1], B = RAMP[v * 3 + 2];
      if (opt?.overlay) {
        const a = opt.overlay[r * cols + c] / 255;
        if (a > 0.04) { const k = Math.min(1, a * 1.3); R += (opt.color[0] - R) * k; G += (opt.color[1] - G) * k; B += (opt.color[2] - B) * k; }
      }
      if (opt?.fillFrom) {
        const d = (v - opt.fillFrom[r * cols + c]);    // brighter than raw = filled in
        if (d > 6) { const k = Math.min(1, (d - 6) / 30); R += (opt.color[0] - R) * k; G += (opt.color[1] - G) * k; B += (opt.color[2] - B) * k; }
      }
      px[o] = R; px[o + 1] = G; px[o + 2] = B; px[o + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  return cv;
}

function buildLanes() {
  const wrap = $("#stemLanes"); wrap.innerHTML = "";
  $(".vox-lane").hidden = !st.ov.stems.length;
  if (!st.ov.stems.length) {
    const lane = document.createElement("div"); lane.className = "lane stem-lane full-lane"; lane.dataset.stem = "original";
    lane.innerHTML = `<div class="lane-head"><div class="head-row"><span class="dot" style="background:var(--text)"></span><span>Full song</span></div>
      <div class="badges"><span>Split it to see each instrument</span></div></div><canvas></canvas>`;
    lane.querySelector("canvas").addEventListener("click", e => seek(xToTime(e, e.currentTarget)));
    wrap.append(lane);
  }
  for (const k of st.ov.stems) {
    const lane = document.createElement("div"); lane.className = "lane stem-lane"; lane.dataset.stem = k;
    lane.innerHTML = `
      <div class="lane-head">
        <div class="head-row"><span class="dot" style="background:${st.ov.colors[k] || "#888"}"></span><span class="nm"></span>
          <div class="ms"><button class="m" title="Mute">M</button><button class="s" title="Solo">S</button></div></div>
        <label class="gain">Level <input type="range" min="-24" max="12" step="0.5" value="0"><output>0 dB</output></label>
        <div class="badges"></div>
      </div><canvas></canvas>`;
    lane.querySelector(".nm").textContent = nice(k[0].toUpperCase() + k.slice(1));
    const m = st.ov.mixer;
    const rng = lane.querySelector("input"), out = lane.querySelector("output");
    rng.value = m.stem_gain[k] || 0; out.textContent = `${(+rng.value).toFixed(1)} dB`;
    rng.oninput = () => { out.textContent = `${(+rng.value).toFixed(1)} dB`; m.stem_gain[k] = +rng.value; pushMixer(); };
    rng.ondblclick = () => { rng.value = 0; rng.oninput(); };
    const bm = lane.querySelector(".m"), bs = lane.querySelector(".s");
    bm.classList.toggle("on", !!m.mute[k]); bs.classList.toggle("on", !!m.solo[k]);
    bm.onclick = () => { m.mute[k] = !m.mute[k]; bm.classList.toggle("on", m.mute[k]); pushMixer(); };
    bs.onclick = () => { m.solo[k] = !m.solo[k]; bs.classList.toggle("on", m.solo[k]); pushMixer(); };
    const cv = lane.querySelector("canvas");
    cv.addEventListener("click", e => laneClick(e, k));
    cv.addEventListener("mousemove", e => laneHover(e, k));
    wrap.append(lane);
  }
  const ruler = $("#ruler");
  ruler.onmousedown = e => { st.drag = { t0: xToTime(e, ruler), t1: xToTime(e, ruler), moved: false }; hideKeepPop(); };
  window.onmousemove = e => {
    if (!st.drag) return;
    st.drag.t1 = clamp(xToTime(e, ruler), 0, st.ov.meta.duration);
    if (Math.abs(st.drag.t1 - st.drag.t0) * st.view.pps > 4) st.drag.moved = true;
    if (st.drag.moved) renderLanes();
  };
  window.onmouseup = e => {
    const d = st.drag; if (!d) return; st.drag = null;
    if (!d.moved) {
      const t = d.t0, si = st.ov.sections.findIndex(s => t >= s.start && t < s.end);
      seek(t); if (si >= 0 && st.ov.stage === "detected") setScope({ section: si }); return;
    }
    const a = Math.min(d.t0, d.t1), b = Math.max(d.t0, d.t1);
    if (b - a < 0.25) { renderLanes(); return; }
    showKeepPop(a, b, e);
  };
  $("#voxCanvas").onclick = e => seek(xToTime(e, $("#voxCanvas")));
}

let mixTimer;
function pushMixer() {
  clearTimeout(mixTimer);
  mixTimer = setTimeout(async () => { try { applyEdit(await api.post("/api/mixer", st.ov.mixer), { keepSpectro: true }); } catch (e) { fail(e); } }, 300);
}

/* ------------------------------------------------------------------ view */
function laneWidth() { const c = $("#ruler"); return c.clientWidth || 800; }
function fitView() { st.view.start = 0; st.view.pps = laneWidth() / st.ov.meta.duration; $("#zoom").value = 0; }
function viewSec() { return laneWidth() / st.view.pps; }
function tToX(t) { return (t - st.view.start) * st.view.pps; }
function xToTime(e, cv) { const r = cv.getBoundingClientRect(); return st.view.start + (e.clientX - r.left) / st.view.pps; }
$("#zoom").oninput = e => {
  const minP = laneWidth() / st.ov.meta.duration, maxP = 400;
  const center = st.view.start + viewSec() / 2;
  st.view.pps = minP * Math.pow(maxP / minP, e.target.value / 100);
  st.view.start = clamp(center - viewSec() / 2, 0, Math.max(0, st.ov.meta.duration - viewSec()));
  renderLanes();
};
$("#btnFit").onclick = () => { fitView(); renderLanes(); };
$("#lanes").addEventListener("wheel", e => {
  if (!st.ov) return;
  if (e.ctrlKey || e.metaKey) {
    e.preventDefault();
    const z = $("#zoom"); z.value = clamp(+z.value - Math.sign(e.deltaY) * 6, 0, 100); z.oninput({ target: z });
  } else if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
    e.preventDefault();
    const d = (e.deltaX || e.deltaY) / st.view.pps;
    st.view.start = clamp(st.view.start + d, 0, Math.max(0, st.ov.meta.duration - viewSec()));
    renderLanes();
  }
}, { passive: false });
window.addEventListener("resize", () => { if (st.ov) { const was = st.view.pps * laneWidth(); renderLanes(); } });

function prepCanvas(cv) {
  const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); return [ctx, w, h];
}

function drawKept(ctx, w, h, label) {
  for (const r of st.ov.mixer.keep || []) {
    const x0 = Math.max(0, tToX(r.start)), x1 = Math.min(w, tToX(r.end)); if (x1 <= 0 || x0 >= w) continue;
    const col = r.mode === "original" ? "159,196,255" : "139,148,165";
    ctx.fillStyle = `rgba(${col},0.10)`; ctx.fillRect(x0, 0, x1 - x0, h);
    ctx.save(); ctx.beginPath(); ctx.rect(x0, 0, x1 - x0, h); ctx.clip();
    ctx.strokeStyle = `rgba(${col},0.35)`; ctx.lineWidth = 1;
    for (let x = x0 - h; x < x1; x += 7) { ctx.beginPath(); ctx.moveTo(x, h); ctx.lineTo(x + h, 0); ctx.stroke(); }
    ctx.restore();
    ctx.fillStyle = `rgba(${col},0.9)`; ctx.fillRect(x0, 0, 1.5, h); ctx.fillRect(x1 - 1.5, 0, 1.5, h);
    if (label && x1 - x0 > 60) { ctx.font = "600 10.5px system-ui"; ctx.textBaseline = "top"; ctx.fillText(r.mode === "original" ? "Original song" : "No repairs", x0 + 5, 3); }
  }
  if (st.drag && st.drag.moved) {
    const x0 = tToX(Math.min(st.drag.t0, st.drag.t1)), x1 = tToX(Math.max(st.drag.t0, st.drag.t1));
    ctx.fillStyle = "rgba(240,176,74,0.18)"; ctx.fillRect(x0, 0, x1 - x0, h);
    ctx.fillStyle = "#f0b04a"; ctx.fillRect(x0, 0, 1.5, h); ctx.fillRect(x1 - 1.5, 0, 1.5, h);
  }
}
function renderRuler() {
  const [ctx, w, h] = prepCanvas($("#ruler"));
  ctx.fillStyle = "#151922"; ctx.fillRect(0, 0, w, h);
  ctx.font = "600 11.5px system-ui, sans-serif"; ctx.textBaseline = "middle";
  st.ov.sections.forEach((s, i) => {
    const x0 = tToX(s.start), x1 = tToX(s.end); if (x1 < 0 || x0 > w) return;
    const sel = st.scope && st.scope.section === i;
    ctx.fillStyle = s.color + (sel ? "66" : "33"); ctx.fillRect(x0 + 1, 4, x1 - x0 - 2, h - 8);
    ctx.fillStyle = s.color; ctx.fillRect(x0 + 1, 4, 3, h - 8);
    ctx.save(); ctx.beginPath(); ctx.rect(x0, 0, x1 - x0, h); ctx.clip();
    ctx.fillStyle = "#e4e8ef"; ctx.fillText(s.name, x0 + 9, h / 2); ctx.restore();
  });
  drawKept(ctx, w, h, false);
  // time ticks
  const step = [1, 2, 5, 10, 15, 30, 60].find(s => s * st.view.pps > 70) || 120;
  ctx.fillStyle = "#8b94a5"; ctx.font = "10.5px system-ui, sans-serif"; ctx.textBaseline = "bottom";
  for (let t = Math.ceil(st.view.start / step) * step; t < st.view.start + viewSec(); t += step) {
    const x = tToX(t); ctx.fillRect(x, h - 4, 1, 4);
  }
}

function renderVox() {
  const [ctx, w, h] = prepCanvas($("#voxCanvas"));
  ctx.fillStyle = "#151922"; ctx.fillRect(0, 0, w, h);
  const b = st.ov.beats, va = st.ov.vocal_activity;
  ctx.fillStyle = "#d36a9a";
  for (let j = 0; j < va.length; j++) {
    const x0 = tToX(b[j]), x1 = tToX(b[j + 1]); if (x1 < 0 || x0 > w) continue;
    const v = Math.min(1, va[j] * 1.6); ctx.globalAlpha = 0.25 + 0.75 * v;
    ctx.fillRect(x0, h - 4 - v * (h - 8), Math.max(1, x1 - x0 - 0.5), v * (h - 8));
  }
  ctx.globalAlpha = 1;
}

const STATUS_COLOR = { auto: "#f0b04a", accepted: "#f0b04a", pending: "#e4e8ef", low: "#e5527a", nodonor: "#e5527a", rejected: "#4b5464" };

function renderLane(k) {
  const lane = $(`.stem-lane[data-stem="${k}"]`); if (!lane) return;
  const cv = lane.querySelector("canvas"); const [ctx, w, h] = prepCanvas(cv);
  const spec = st.specs[k]?.[st.mode] || st.specs[k]?.raw; const fps = st.ov.display.fps;
  ctx.imageSmoothingEnabled = true; ctx.fillStyle = "#151922"; ctx.fillRect(0, 0, w, h);
  const markH = 8;
  if (spec) ctx.drawImage(spec, st.view.start * fps, 0, viewSec() * fps, spec.height, 0, 0, w, h - markH);
  // section boundaries
  ctx.fillStyle = "#ffffff22";
  for (const s of st.ov.sections) { const x = tToX(s.start); if (x > 0 && x < w) ctx.fillRect(x, 0, 1, h); }
  // suggestion marks along the bottom
  for (const s of st.ov.suggestions) {
    if (s.stem !== k) continue;
    const x0 = tToX(s.start), x1 = tToX(s.end); if (x1 < 0 || x0 > w) continue;
    ctx.fillStyle = s.type === "bleed" ? "#d36a9a" : s.type === "glitch" ? "#ff8f5a" : s.type === "smooth" ? "#b9a5ff" : s.type === "ess" ? "#e7b0d0" : ["take", "alt", "hits", "bars", "rebuild"].includes(s.type) ? "#6dbf8c" : STATUS_COLOR[s.status] || "#888";
    ctx.globalAlpha = s.status === "rejected" ? 0.5 : 1;
    const y = s.type === "bleed" ? h - markH : h - markH + 3;
    ctx.fillRect(x0 + 1, y, Math.max(2, x1 - x0 - 2), s.type === "bleed" ? 3 : markH - 3);
    ctx.globalAlpha = 1;
  }
  // selection + donor
  const sel = st.sel && st.ov.suggestions.find(s => s.id === st.sel);
  if (sel && sel.stem === k) {
    ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 1.5;
    ctx.strokeRect(tToX(sel.start) + .75, .75, tToX(sel.end) - tToX(sel.start) - 1.5, h - 1.5);
    for (const [i, d] of (sel.donors || []).entries()) {
      ctx.strokeStyle = "#58c6d6"; ctx.globalAlpha = i === 0 ? 1 : 0.45; ctx.setLineDash(i === 0 ? [] : [4, 3]);
      const x0 = tToX(d.start), x1 = tToX(d.end);
      ctx.strokeRect(x0 + .75, .75, x1 - x0 - 1.5, h - markH - 1.5);
      ctx.fillStyle = "#58c6d6"; if (i === 0) { ctx.font = "600 10.5px system-ui"; ctx.fillText("source", x0 + 4, 12); }
      ctx.setLineDash([]); ctx.globalAlpha = 1;
    }
  }
  drawKept(ctx, w, h, true);
  drawMuted(ctx, w, h, k);
  if (st.picking && st.picking.stem === k && st.hoverT != null) {
    const sug = st.ov.suggestions.find(s => s.id === st.picking.id);
    const x0 = tToX(st.hoverT), x1 = tToX(st.hoverT + (sug.end - sug.start));
    ctx.fillStyle = "#58c6d633"; ctx.fillRect(x0, 0, x1 - x0, h);
  }
}

function renderLanes() {
  if (!st.ov) return;
  renderRuler(); if (st.ov.stems.length) renderVox();
  for (const k of (st.ov.stems.length ? st.ov.stems : ["original"])) renderLane(k);
  movePlayhead();
}

function laneHit(k, t) {
  const hits = st.ov.suggestions.filter(s => s.stem === k && t >= s.start && t < s.end);
  return hits.find(s => s.type === "repair") || hits[0];
}
function laneClick(e, k) {
  const t = xToTime(e, e.currentTarget);
  if (st.picking) {
    if (st.picking.stem !== k) { toast(`Pick inside the ${st.picking.stem} lane — repairs borrow from the same instrument.`); return; }
    const id = st.picking.id; endPick();
    edit("/api/donor", { id, time: t }); return;
  }
  const hit = laneHit(k, t);
  if (hit) { select(hit.id, { scroll: true }); } else seek(t);
}
function laneHover(e, k) {
  if (!st.picking) return;
  st.hoverT = xToTime(e, e.currentTarget); renderLane(k);
}
$$("#viewSeg button").forEach(b => b.onclick = () => {
  $$("#viewSeg button").forEach(x => x.classList.toggle("on", x === b)); st.mode = b.dataset.m; renderLanes();
});

/* ------------------------------------------------------------------ main playback */
const player = $("#player");
function setVersion(v, force) {
  if (v === st.version && !force && player.src) return;
  st.version = v;
  $$("#versionSeg button").forEach(b => b.classList.toggle("on", b.dataset.v === v));
  reloadAudio();
}
function reloadAudio() {
  const t = player.currentTime || 0, wasPlaying = !player.paused;
  player.src = `/api/audio?version=${st.version}&rev=${st.rev}`;
  // Nothing is fetched until it plays (preload="none"): the app used to render the whole mix when a song
  // opened and after every edit, even if you never pressed play. The position is kept for when you do.
  try { player.currentTime = t; } catch {}
  if (wasPlaying) player.play().catch(() => {});
}
$$("#versionSeg button").forEach(b => b.onclick = () => setVersion(b.dataset.v));
$("#btnPlay").onclick = togglePlay;
function togglePlay() { stopPreview(); if (player.paused) player.play().catch(fail); else player.pause(); }
player.onplay = () => $("#playIcon").setAttribute("d", "M6 5h4v14H6zM14 5h4v14h-4z");
// the mix is only rendered when you first press play after opening or editing: show that it's coming
player.addEventListener("waiting", () => $("#btnPlay").classList.add("loading"));
player.addEventListener("loadstart", () => { if (!player.paused) $("#btnPlay").classList.add("loading"); });
for (const ev of ["playing", "pause", "error", "canplay"]) player.addEventListener(ev, () => $("#btnPlay").classList.remove("loading"));
player.onpause = () => $("#playIcon").setAttribute("d", "M7 5v14l12-7z");
function seek(t) { player.currentTime = clamp(t, 0, st.ov.meta.duration); movePlayhead(); }
function movePlayhead() {
  if (!st.ov) return;
  const t = player.currentTime || 0;
  $("#timeNow").textContent = fmt(t); $("#timeTotal").textContent = "/ " + fmt(st.ov.meta.duration);
  const x = tToX(t), w = laneWidth(), ph = $("#playhead");
  ph.hidden = x < 0 || x > w; ph.style.left = 168 + x + "px";
  if (!player.paused && x > w * 0.92 && st.view.pps * st.ov.meta.duration > w + 1) {
    st.view.start = clamp(t - viewSec() * 0.1, 0, st.ov.meta.duration - viewSec()); renderLanes();
  }
}
(function loop() { if (!player.paused) movePlayhead(); requestAnimationFrame(loop); })();

/* ------------------------------------------------------------------ previews (Web Audio, A/B looping) */
let actx, pv = null;
function ctx() { return actx ||= new (window.AudioContext || window.webkitAudioContext)(); }
async function fetchBuf(id, mode) {
  const r = await fetch(`/api/preview?id=${id}&mode=${mode}&hear=${st.hear || "mix"}&r=${st.rev}`);
  return ctx().decodeAudioData(await r.arrayBuffer());
}
function stopPreview() {
  if (pv) { pv.nodes.forEach(n => { try { n.stop(); } catch {} }); pv = null; }
  $$(".preview button, .ab button.abplay").forEach(b => b.classList.remove("playing"));
  const ab = $(".ab .abplay"); if (ab) ab.textContent = "Loop A/B";
}
async function playPreview(id, mode, btn) {
  const was = pv && pv.key === id + mode; stopPreview(); if (was) return;
  player.pause();
  try {
    const buf = await fetchBuf(id, mode);
    const src = ctx().createBufferSource(); src.buffer = buf; src.connect(ctx().destination); src.start();
    pv = { key: id + mode, nodes: [src] }; btn?.classList.add("playing");
    src.onended = () => { if (pv && pv.nodes[0] === src) stopPreview(); };
  } catch (e) { fail(e); }
}
async function playAB(id, btn) {
  if (pv && pv.key === id + "ab") { stopPreview(); return; }
  stopPreview(); player.pause();
  try {
    const [a, b] = await Promise.all([fetchBuf(id, "before"), fetchBuf(id, "after")]);
    const c = ctx(), ga = c.createGain(), gb = c.createGain();
    const sa = c.createBufferSource(), sb = c.createBufferSource();
    sa.buffer = a; sb.buffer = b; sa.loop = sb.loop = true;
    sa.connect(ga).connect(c.destination); sb.connect(gb).connect(c.destination);
    const side = st.abSide || "after";
    ga.gain.value = side === "before" ? 1 : 0; gb.gain.value = side === "after" ? 1 : 0;
    const t0 = c.currentTime + 0.05; sa.start(t0); sb.start(t0);
    pv = { key: id + "ab", nodes: [sa, sb], ga, gb };
    btn.classList.add("playing"); btn.textContent = "Stop loop";
  } catch (e) { fail(e); }
}
function setAB(side) {
  st.abSide = side;
  $$(".ab .seg button").forEach(b => b.classList.toggle("on", b.dataset.ab === side));
  if (pv && pv.ga) { const t = ctx().currentTime; pv.ga.gain.setTargetAtTime(side === "before" ? 1 : 0, t, 0.01); pv.gb.gain.setTargetAtTime(side === "after" ? 1 : 0, t, 0.01); }
}

/* ------------------------------------------------------------------ edits */
async function edit(path, body) {
  try { applyEdit(await api.post(path, body)); } catch (e) { fail(e); }
}
async function applyEdit(r, opt = {}) {
  st.rev = r.rev; st.ov = r.overview;
  if (!opt.keepSpectro) await loadSpectros(st.ov.stems, ["after", "damage"]);
  reloadAudio();
  renderAll();
  followWarm();
}
// Slow suggestions (per-instrument measurements, repeated hits and notes) are worked out in the
// background so a song opens straight away. While the app says it's still at it, ask again now and
// then and refresh just the lists; nothing else is reloaded.
let warmTimer = null;
function followWarm() {
  if (warmTimer || !st.ov || !st.ov.warming) return;
  warmTimer = setTimeout(async () => {
    warmTimer = null;
    const rev = st.rev;
    try {
      const r = await api.get("/api/overview", 60000);
      if (r.rev === rev && st.rev === rev) { st.ov = r.overview; renderAll(); }
    } catch { /* try again later */ }
    followWarm();
  }, 2500);
}
$("#btnUndo").onclick = () => edit("/api/undo", {});

/* ---------- kept parts */
function vocalsIn(a, b) {
  const bt = st.ov.beats, va = st.ov.vocal_activity; let mx = 0;
  for (let j = 0; j < va.length; j++) if (bt[j + 1] > a && bt[j] < b) mx = Math.max(mx, va[j]);
  return mx;
}
function showKeepPop(a, b, e) {
  const pop = $("#keepPop"), tl = $("#timeline").getBoundingClientRect();
  $("#keepPopTitle").textContent = `Keep ${fmtP(a)} to ${fmtP(b)} as it is`;
  const sung = st.ov.vocal_activity.length && vocalsIn(a, b) > 0.08;
  const warn = $("#keepPopWarn");
  warn.hidden = !sung;
  warn.textContent = "There's singing in this part. Using the original song here only takes effect where nobody sings (with a little room around the singing), so the rest stays as it is.";
  const split = st.ov.stems.length > 0;
  pop.querySelector('[data-mode="no_edits"]').hidden = !split;
  pop.querySelectorAll("[data-split-only]").forEach(el => (el.hidden = !split));
  const detected = st.ov.stage === "detected";
  pop.querySelector('[data-act="scan"]').hidden = !detected;
  const mr = $("#muteRow"); mr.innerHTML = "";
  for (const k of [null, ...st.ov.stems]) {
    const btn = document.createElement("button"); btn.className = "small"; btn.textContent = k ? k : "Everything";
    btn.onclick = () => { hideKeepPop(); setMutes([...(st.ov.mixer.mute_ranges || []), { start: a, end: b, stem: k }]); };
    mr.append(btn);
  }
  pop.querySelector('[data-act="smooth"]').onclick = async () => {
    hideKeepPop();
    try {
      const r = await api.post("/api/smooth", { start: a, end: b });
      await applyEdit(r);
      if (!r.new_ids.length) return toast("No singing in that part, so there's nothing to smooth.");
      st.filter = "cleanup"; st.scope = null; select(r.new_ids[0]);
      toast(`${r.new_ids.length} smoothing fix${r.new_ids.length === 1 ? "" : "es"} ready to review. Listen before and after, then Apply.`, { ms: 7000 });
    } catch (err) { fail(err); }
  };
  const am = $("#altMode"), as = $("#altStem"), models = st.state?.models || {};
  as.innerHTML = "";
  const curMode = (st.ov.meta.sep_mode || "").split("+")[0];
  fillModelSelect(am, models, curMode);      // same grouping as the Split step, without the one already used
  as.add(new Option("All tracks", ""));
  for (const k of st.ov.stems) as.add(new Option(nice(k), k));
  pop.querySelector(".alt-row").hidden = !split || st.ov.meta.source === "demo";
  pop.querySelector(".alt-row").previousElementSibling.hidden = pop.querySelector(".alt-row").hidden;
  pop.querySelector('[data-act="alt"]').onclick = () => { hideKeepPop(); tryAlt(am.value, a, b, as.value || null, $("#altBlend").checked ? 0.5 : 0); };
  pop.querySelector(".alt-blend").hidden = pop.querySelector(".alt-row").hidden;
  const rs = $("#rebuildStem"); rs.innerHTML = "";
  rs.add(new Option("All tracks", ""));
  for (const k of st.ov.stems) if (k !== "vocals" && k !== "backing_vocals") rs.add(new Option(nice(k), k));
  pop.querySelectorAll("[data-rebuild]").forEach(el => (el.hidden = !split || !detected));
  pop.querySelector('[data-act="rebuild"]').onclick = async () => {
    hideKeepPop(); showBusy(0.5, "Rebuilding from the matching bars");
    try {
      const r = await api.post("/api/rebuild", { start: a, end: b, stem: rs.value || null }); hideBusy(); await applyEdit(r);
      st.filter = "review"; st.scope = null; select(r.new_ids[0]);
      toast(`${r.new_ids.length} rebuilt bar${r.new_ids.length === 1 ? "" : "s"} ready to review. Listen before and after, then Apply.`, { ms: 7000 });
    } catch (e) { hideBusy(); fail(e); }
  };
  pop.querySelector('[data-act="sides"]').onclick = () => {
    hideKeepPop();
    setSides([...(st.ov.mixer.sides || []), { start: a, end: b, amount: 1 }]);
    toast("Using the original's stereo sides there. Remove it in the Levels tab, or Undo.", { ms: 6000 });
  };
  pop.querySelector('[data-act="cleanup"]').hidden = !detected;
  pop.querySelector('[data-act="cleanup"]').onclick = async () => {
    hideKeepPop(); showBusy(0.5, "Looking for everything that can be tidied here");
    try {
      const r = await api.post("/api/cleanup", { start: a, end: b }); hideBusy(); await applyEdit(r);
      if (!r.new_ids.length) return toast("Nothing to tidy in that part.");
      st.filter = "review"; st.scope = null; select(r.new_ids[0]);
      const kinds = {};
      for (const id of r.new_ids) { const x = st.ov.suggestions.find(y => y.id === id); if (x) kinds[x.type] = (kinds[x.type] || 0) + 1; }
      const words = { glitch: "click or gap", smooth: "smoothing", hits: "drum-hit rebuild", bars: "repeated-bar fix" };
      toast("Added to Review: " + Object.entries(kinds).map(([k, n]) => `${n} ${words[k] || k}${n === 1 ? "" : "s"}`).join(", ") + ". Listen, then Apply the ones you like.", { ms: 9000 });
    } catch (e) { hideBusy(); fail(e); }
  };
  pop.querySelector('[data-act="scan"]').onclick = async () => {
    hideKeepPop();
    const before = st.ov.suggestions.filter(x => x.type === "glitch").length;
    await edit("/api/glitch_scan", { start: a, end: b });
    const now = st.ov.suggestions.filter(x => x.type === "glitch" && x.end > a && x.start < b);
    toast(now.length ? `Found ${now.length} click${now.length === 1 ? "" : "s"} or gap${now.length === 1 ? "" : "s"} there.` : "No clicks or gaps found there.");
    if (now.length) { st.filter = "cleanup"; select(now[0].id); }
  };
  pop.style.left = Math.min(tl.width - 290, Math.max(180, e.clientX - tl.left - 135)) + "px";
  pop.style.top = (e.clientY - tl.top + 14) + "px";
  pop.hidden = false;
  pop.querySelectorAll("button[data-mode]").forEach(btn => btn.onclick = () => {
    hideKeepPop();
    if (btn.dataset.mode) setKeep([...(st.ov.mixer.keep || []), { start: a, end: b, mode: btn.dataset.mode }]);
    else renderLanes();
  });
}
async function tryAlt(mode, start, end, stem, blend = 0) {
  try { await api.post("/api/alt", { mode, start, end, stem, blend }); watchJob(); }
  catch (e) {
    if (e.message === "need_model") return installModelFromStep();
    if (e.message === "need_extra") {
      const ex = st.state?.extras || {};
      const names = e.data.extras.map(k => `${ex[k]?.label || k} (${ex[k]?.size || "a download"})`).join(" and ");
      if (confirm(`That model needs ${names}. Download now? Then try again once its light turns green.`)) for (const k of e.data.extras) await installExtra(k);
      return;
    }
    fail(e);
  }
}
function hideKeepPop() { $("#keepPop").hidden = true; }
async function setKeep(list) {
  try { applyEdit(await api.post("/api/mixer", { keep: list })); } catch (e) { fail(e); }
}
function renderKeepList() {
  const w = $("#keepList"), list = st.ov.mixer.keep || [];
  w.innerHTML = list.length ? "" : `<p class="help">Nothing kept yet.</p>`;
  list.forEach((r, i) => {
    const d = document.createElement("div"); d.className = "keep-item";
    d.innerHTML = `<i class="sw ${r.mode}"></i><span>${fmtP(r.start)} to ${fmtP(r.end)}</span>
      <select><option value="original">Original song</option><option value="no_edits">Split, no repairs</option></select>
      <button class="ghost small">Remove</button>`;
    const sel = d.querySelector("select"); sel.value = r.mode;
    if (!st.ov.stems.length) sel.querySelector('[value="no_edits"]').disabled = true;
    sel.onchange = () => setKeep(list.map((x, j) => (j === i ? { ...x, mode: sel.value } : x)));
    d.querySelector("button").onclick = () => setKeep(list.filter((_, j) => j !== i));
    d.querySelector("span").onclick = () => { seek(r.start); };
    w.append(d);
  });
}
async function setSides(list) {
  try { applyEdit(await api.post("/api/mixer", { sides: list })); } catch (e) { fail(e); }
}
function renderSidesList() {
  const w = $("#sidesList"); if (!w) return;
  const list = st.ov.mixer.sides || [];
  w.innerHTML = list.length ? "" : `<p class="help">None yet.</p>`;
  list.forEach((r, i) => {
    const d = document.createElement("div"); d.className = "keep-item";
    d.innerHTML = `<i class="sw original"></i><span></span>
      <select><option value="1">Full</option><option value="0.5">Half</option></select>
      <button class="ghost small">Remove</button>`;
    d.querySelector("span").textContent = `${fmtP(r.start)} to ${fmtP(r.end)}`;
    d.querySelector("span").onclick = () => seek(r.start);
    const sel = d.querySelector("select"); sel.value = (r.amount ?? 1) >= 0.75 ? "1" : "0.5";
    sel.onchange = () => setSides(list.map((x, j) => (j === i ? { ...x, amount: +sel.value } : x)));
    d.querySelector("button").onclick = () => setSides(list.filter((_, j) => j !== i));
    w.append(d);
  });
}
async function setMutes(list) {
  try { applyEdit(await api.post("/api/mixer", { mute_ranges: list })); } catch (e) { fail(e); }
}
function renderMuteList() {
  const w = $("#muteList"), list = st.ov.mixer.mute_ranges || [];
  w.innerHTML = list.length ? "" : `<p class="help">Nothing muted.</p>`;
  list.forEach((r, i) => {
    const d = document.createElement("div"); d.className = "keep-item";
    d.innerHTML = `<i class="sw no_edits"></i><span></span><button class="ghost small">Unmute</button>`;
    d.querySelector("span").textContent = `${r.stem || "Everything"}, ${fmtP(r.start)} to ${fmtP(r.end)}`;
    d.querySelector("span").onclick = () => seek(r.start);
    d.querySelector("button").onclick = () => setMutes(list.filter((_, j) => j !== i));
    w.append(d);
  });
}
function drawMuted(ctx, w, h, stem) {
  for (const r of st.ov.mixer.mute_ranges || []) {
    if (r.stem && r.stem !== stem) continue;
    const x0 = Math.max(0, tToX(r.start)), x1 = Math.min(w, tToX(r.end)); if (x1 <= 0 || x0 >= w) continue;
    ctx.fillStyle = "rgba(13,16,22,0.72)"; ctx.fillRect(x0, 0, x1 - x0, h);
    ctx.fillStyle = "#e5527a"; ctx.fillRect(x0, 0, 1.5, h); ctx.fillRect(x1 - 1.5, 0, 1.5, h);
    if (x1 - x0 > 40) { ctx.font = "600 10.5px system-ui"; ctx.textBaseline = "top"; ctx.fillText("Muted", x0 + 5, h - 24); }
  }
}
const inKept = s => (st.ov.mixer.keep || []).some(r => r.start <= s.start + 0.05 && r.end >= s.end - 0.05);

/* ---------- quality tips */
function renderTips() {
  const tips = st.ov.tips || [], box = $("#tipsBox"), list = $("#tipsList");
  box.hidden = !tips.length && !st.ov.warming;
  $("#warmChip").hidden = !st.ov.warming;
  const actionable = tips.filter(t => t.action).length;
  $("#tipCount").textContent = actionable ? `${actionable}` : "";
  list.innerHTML = "";
  for (const t of tips) {
    const d = document.createElement("div"); d.className = "tip";
    d.innerHTML = `<b></b><p></p>`;
    d.querySelector("b").textContent = t.title; d.querySelector("p").textContent = t.detail;
    if (t.button && t.action) {
      const b = document.createElement("button"); b.className = "small"; b.textContent = t.button;
      b.onclick = () => runTip(t.action); d.append(b);
    }
    list.append(d);
  }
}
// long one-off jobs: show the busy screen while they run
async function runChecked(msg, fn) {
  showBusy(0.5, msg);
  try { await fn(); } catch (e) { fail(e); } finally { hideBusy(); }
}
async function scoreSections() {
  return runChecked("Scoring how clean each section is", async () => {
    await applyEdit(await api.post("/api/quality", {}));
    activateTab("grid");
    toast("Clean scores are in the Repairability table: before repairs, then now.");
  });
}
async function runTip(a) {
  if (a.kind === "keep") return setKeep([...(st.ov.mixer.keep || []), ...a.ranges]);
  if (a.kind === "bulk") return edit("/api/bulk", { action: a.action, types: ["repair"] });
  if (a.kind === "lift") return edit("/api/mixer", { vocal_lift: { ...(st.ov.mixer.vocal_lift || {}), [a.stem]: a.db } });
  if (a.kind === "filter") { st.filter = a.filter; st.scope = null; activateTab("review"); return renderReview(); }
  if (a.kind === "detect") return $("#btnDetect").onclick();
  if (a.kind === "tab") return activateTab(a.tab);
  if (a.kind === "alt") return tryAlt(a.mode, a.start, a.end, null);
  if (a.kind === "mute") return setMutes([...(st.ov.mixer.mute_ranges || []), { start: a.start, end: a.end, stem: a.stem }]);
  if (a.kind === "mutes") return setMutes([...(st.ov.mixer.mute_ranges || []), ...a.ranges]);
  if (a.kind === "mud") return edit("/api/mixer", { mud_cut: { ...(st.ov.mixer.mud_cut || {}), [a.stem]: a.db } });
  if (a.kind === "status") return edit("/api/status", { ids: a.ids, status: a.status });
  if (a.kind === "verify") return runChecked("Double-checking every fix against the original", async () => {
    const r = await api.post("/api/verify", {}); await applyEdit(r);
    const n = r.flagged.length;
    if (!n) return toast("Everything lines up with the original.");
    toast(`${n} fix${n === 1 ? "" : "es"} don't line up with the original.`, { ms: 9000, action: ["Show them", () => { st.filter = "misfit"; st.scope = null; activateTab("review"); renderReview(); }] });
  });
  if (a.kind === "best_of") return runChecked("Scoring every bar of every split", async () => {
    const r = await api.post("/api/best_of", {}); await applyEdit(r);
    const n = r.new_ids.length;
    if (!n) return toast("The current split is already the cleanest everywhere.");
    toast(`${n} instrument${n === 1 ? "" : "s"} built from the cleanest split, waiting in Review.`, { ms: 9000, action: ["Show them", () => { st.filter = "takes"; st.scope = null; select(r.new_ids[0]); }] });
  });
  if (a.kind === "scan") {
    await edit("/api/glitch_scan", {});
    const n = st.ov.suggestions.filter(x => x.type === "glitch" && x.status === "pending").length;
    return toast(n ? `${n} click${n === 1 ? "" : "s"} or gap${n === 1 ? "" : "s"} to review.` : "No clicks or gaps found.");
  }
  if (a.kind === "resplit") { $("#sepMode").value = a.mode; localStorage.setItem("sepMode", a.mode); return $("#btnSplit").onclick(); }
}

/* ---------- steps: import -> split -> find damage -> review */
function renderSteps() {
  const ov = st.ov, stage = ov.stage, m = st.state?.model || {};
  const demo = ov.meta.source === "demo";
  const order = ["imported", "split", "detected"];
  const at = order.indexOf(stage);
  const mark = (name, done, current, locked) => {
    const el = $(`.step[data-step="${name}"]`);
    el.classList.toggle("done", done); el.classList.toggle("current", current); el.classList.toggle("locked", locked);
  };
  mark("import", true, false, false);
  mark("split", at >= 1, at === 0, false);
  mark("detect", at >= 2, at === 1, at < 1);
  mark("review", false, at === 2, at < 2);

  const fromFile = !demo && ov.meta.separation !== "Imported stems" && !!ov.meta.source;
  $("#vinylBox").hidden = !fromFile;
  const vy = ov.meta.vinyl;
  $("#btnVinylUndo").hidden = !vy;
  $("#importSub").textContent = vy ? `Song loaded. Vinyl crackle removed (${vy.strength}, ${Number(vy.clicks).toLocaleString()} clicks).`
                                   : "Song loaded, ready to play";
  $("#btnVinyl").textContent = vy ? "Remove again" : "Remove vinyl crackle";
  const split = $("#btnSplit"), sel = $("#sepMode"), modelOk = m.installed || demo;
  const auto = $("#btnSplitAuto");
  auto.hidden = !(at < 1 && (modelOk || m.installing));
  auto.className = "";
  auto.disabled = at < 1 && !modelOk;
  sel.hidden = demo;
  if (at >= 1) {
    $("#splitSub").textContent = `Split with ${ov.meta.separation || "a separator"}: ${ov.stems.join(", ")} and vocals.`;
    split.textContent = "Split again"; split.className = "ghost small"; split.disabled = false;
    sel.hidden = true;
  } else if (demo) {
    $("#splitSub").textContent = "The demo comes pre-split, no model needed.";
    split.textContent = "Split"; split.className = "primary"; split.disabled = false;
  } else if (modelOk) {
    $("#splitSub").textContent = speedTip(sel.value, /CPU/i.test(m.device || ""));
    split.textContent = "Split"; split.className = "primary"; split.disabled = false;
  } else if (m.installing) {
    $("#splitSub").textContent = `Waiting for the separation model: ${pct(m.progress || 0)}.`;
    split.textContent = "Split"; split.className = ""; split.disabled = true;
  } else {
    $("#splitSub").textContent = `Needs the separation model (one-time download, ${m.plan?.size || "about 1-3 GB"}).`;
    split.textContent = "Get the model"; split.className = ""; split.disabled = false;
  }
  const det = $("#btnDetect");
  det.disabled = at < 1;
  if (at < 1) { $("#detectSub").textContent = "Available after splitting."; det.textContent = "Find damage"; det.className = ""; }
  else if (at === 1) { $("#detectSub").textContent = "Looks for instrument sound the vocals took and suggests repairs. Nothing changes until you apply them."; det.textContent = "Find damage"; det.className = "primary"; }
  else {
    const n = ov.suggestions.filter(x => x.type === "repair").length;
    $("#detectSub").textContent = `${n} spot${n === 1 ? "" : "s"} found.`; det.textContent = "Find again"; det.className = "ghost small";
  }
  const waiting = ov.suggestions.filter(x => x.status === "pending").length;
  $("#reviewSub").textContent = at < 2 ? "After finding damage." : waiting ? `${waiting} waiting for you in the Review tab.` : "Everything is decided. Export when ready.";
  $("#btnExport").disabled = at < 1;
  $("#splitExtra").hidden = demo;
  $("#viewSeg").hidden = at < 1; $(".legend").hidden = at < 2;
  // what you can listen to
  $$("#versionSeg button").forEach(b => { b.disabled = b.dataset.v !== "original" && at < 1; });
  if (at < 1 && st.version !== "original") setVersion("original");
}
// "4 tracks · Best quality" -> grouped under "4 tracks: drums, bass, other", shown as "Best quality"
const GROUP_NAMES = { "4 tracks": "4 tracks: drums, bass, other, vocals", "6 tracks": "6 tracks: adds guitar and piano" };
const shortModel = label => (label.split(" · ")[1] || label);
function fillModelSelect(sel, models, skip) {
  sel.innerHTML = "";
  const groups = {};
  for (const [k, label] of Object.entries(models)) {
    if (k === skip) continue;
    const g = label.split(" · ")[0];
    if (!groups[g]) { groups[g] = document.createElement("optgroup"); groups[g].label = GROUP_NAMES[g] || g; sel.append(groups[g]); }
    groups[g].append(new Option(shortModel(label), k));
  }
}
function speedTip(mode, cpu) {
  return {
    "4stem": cpu ? "Recommended. Best quality: 4 models in a row, 5-15 min per song on a CPU." : "Recommended. Best quality: 4 models in a row, about a minute on a GPU.",
    "4stem_fast": cpu ? "Fast: a few minutes per song on a CPU." : "Fast: about 4x quicker than best quality.",
    "6stem": "6 tracks: adds guitar and piano lanes, so damage is found and repaired per instrument. The piano track is less clean.",
    "6stem_best": cpu ? "6 tracks with drums, bass and vocals from the best model: 2 models in a row, slow on a CPU." : "6 tracks with drums, bass and vocals from the best model: 2 models in a row.",
    "ensemble": "Two models combined: they fill each other's gaps, so least is lost under the vocals. Slowest.",
    "hybrid": "Roformer takes the vocals out first (the cleanest removal), then the best model splits the rest. Extra download.",
    "hybrid6": "Roformer takes the vocals out first (the cleanest removal), then the 6-track model splits the rest. Extra download.",
  }[mode] || "";
}
$("#btnSplit").onclick = () => doSplit(false);
$("#btnSplitAuto").onclick = () => doSplit(true);
async function doSplit(auto) {
  const ov = st.ov, m = st.state?.model || {};
  if (ov.stage !== "imported") {
    const lose = ov.user_decisions ? ` Your ${ov.user_decisions} review decision(s) will be cleared.` : "";
    if (!confirm(`Split the song again? Damage you've found will need to be found again.${lose}`)) return;
  }
  if (!m.installed && ov.meta.source !== "demo") {
    if (m.installing) return;
    return installModelFromStep();
  }
  try {
    await api.post("/api/split", { mode: $("#sepMode").value || "4stem", backing: $("#optBacking").checked, drums: $("#optDrums").checked,
                                   auto });
    watchJob();
  } catch (e) {
    if (e.message === "need_model") installModelFromStep();
    else if (e.message === "need_extra") {
      const ex = st.state?.extras || {};
      const names = e.data.extras.map(k => `${ex[k]?.label || k} (${ex[k]?.size || "a download"})`).join(" and ");
      if (confirm(`That needs ${names}. Download now? Click Split again once the light turns green.`)) {
        for (const k of e.data.extras) await installExtra(k);
      }
    } else fail(e);
  }
}
async function installModelFromStep() {
  const m = st.state?.model || {};
  if (!confirm(`Download the separation model now? It's a one-time download of ${m.plan?.size || "about 1-3 GB"}, set up to run on ${m.plan?.runs_on || "your computer"}. You can keep working meanwhile.`)) return;
  await installModel(false);
  watchModelInEditor();
}
function watchModelInEditor() {
  const t = setInterval(async () => {
    try {
      const s = await api.get("/api/state"); st.state = s; renderModel(s); if (st.ov) renderSteps();
      if (!s.model.installing) clearInterval(t);
    } catch {}
  }, 1000);
}
$("#btnDetect").onclick = async () => {
  if (st.ov.stage === "detected") {
    const lose = st.ov.user_decisions ? ` Your ${st.ov.user_decisions} review decision(s) will be cleared.` : "";
    if (!confirm(`Look for damage again from scratch?${lose}`)) return;
  }
  try { await api.post("/api/detect"); watchJob(); } catch (e) { fail(e); }
};

function renderAll() {
  $("#autoCard").hidden = st.ov.stage !== "detected";
  renderSteps(); renderTips(); renderKeepList(); renderSidesList(); renderMuteList();
  $("#btnUndo").disabled = !st.ov.can_undo;
  $("#btnUndo").title = st.ov.can_undo ? `Undo: ${st.ov.undo_label}` : "Nothing to undo";
  renderBadges(); renderLanes(); renderReview(); renderGrid(); renderLevels(); renderSettings();
}

function renderBadges() {
  for (const k of st.ov.stems) {
    const s = st.ov.stem_summary[k]; const el = $(`.stem-lane[data-stem="${k}"] .badges`); if (!el) continue;
    if (st.ov.stage !== "detected") { el.innerHTML = "<span>Not checked for damage yet</span>"; continue; }
    el.innerHTML = s.count ? `<span>Damage <b>${pct(Math.min(1, s.damage))}</b></span><span>Fixable <b>${pct(s.repairability)}</b></span>` : `<span>No damage found</span>`;
  }
}

/* ---------- review list */
const ok = s => s.status === "auto" || s.status === "accepted";
const FILTERS = {
  review: s => s.status === "pending" || (s.type === "bleed" && s.status === "pending"),
  applied: s => ok(s),
  bleed: s => s.type === "bleed" || s.type === "ess",
  cleanup: s => s.type === "glitch" || s.type === "smooth",
  takes: s => ["take", "alt", "hits", "bars", "rebuild"].includes(s.type),
  hard: s => s.status === "low" || s.status === "nodonor",
  misfit: s => !!(s.check && s.check.flag) && s.status !== "rejected",
  all: () => true,
};
$$("#filters button").forEach(b => b.onclick = () => { st.filter = b.dataset.f; renderReview(); });

function setScope(scope) { st.scope = scope; activateTab("review"); renderReview(); renderRuler(); }

function statusText(s) {
  if (s.type === "bleed" || s.type === "ess") return { pending: "Suggested", accepted: "Cleaned", rejected: "Kept as is" }[s.status] || s.status;
  if (s.type === "rebuild") return { pending: "Suggested", accepted: "Rebuilt", rejected: "Kept as is" }[s.status] || s.status;
  if (s.type === "glitch" || s.type === "smooth") return { pending: "Suggested", accepted: "Fixed", rejected: "Kept as is" }[s.status] || s.status;
  if (["take", "alt", "hits", "bars"].includes(s.type)) return { pending: "Suggested", accepted: "Using it", rejected: "Kept as is" }[s.status] || s.status;
  if (s.status === "pending" && s.recommended) return "Recommended";
  return { auto: "Applied", accepted: "Applied", pending: "Needs review", low: "Weak match", nodonor: "No source", rejected: "Rejected" }[s.status] || s.status;
}

function renderReview() {
  const ov = st.ov, all = ov.suggestions;
  const repairs = all.filter(s => s.type === "repair");
  const applied = repairs.filter(ok).length, pending = all.filter(FILTERS.review).length, hard = repairs.filter(FILTERS.hard).length;
  $("#reviewSummary").innerHTML = `<div><b>${applied}</b><span>repairs applied by you</span></div><div><b>${pending}</b><span>waiting for you</span></div><div><b>${hard}</b><span>without a good source</span></div>`;
  $$("#filters button").forEach(b => b.classList.toggle("on", b.dataset.f === st.filter));
  const sc = $("#scope");
  if (st.scope) {
    const parts = [];
    if (st.scope.section != null) parts.push(ov.sections[st.scope.section].name);
    if (st.scope.stem) parts.push(st.scope.stem);
    sc.hidden = false; sc.innerHTML = `<span>Showing <b></b></span><button class="ghost small" style="margin-left:auto">Show everything</button>`;
    sc.querySelector("b").textContent = parts.join(", ");
    sc.querySelector("button").onclick = () => setScope(null);
  } else sc.hidden = true;
  let list = all.filter(FILTERS[st.filter]);
  if (st.scope?.section != null) list = list.filter(s => s.section === st.scope.section);
  if (st.scope?.stem) list = list.filter(s => s.stem === st.scope.stem);
  list.sort((a, b) => a.start - b.start || a.stem.localeCompare(b.stem));

  const bulk = $("#bulkBar"); bulk.innerHTML = "";
  if (list.some(s => s.status !== "nodonor")) {
    const scopeArgs = { stem: st.scope?.stem ?? null, section: st.scope?.section ?? null };
    const types = st.filter === "bleed" ? ["bleed", "ess"] : st.filter === "cleanup" ? ["glitch", "smooth"] : st.filter === "takes" ? ["take", "alt", "hits", "bars", "rebuild"] : st.filter === "misfit" ? ["repair", "take", "alt", "hits", "bars", "rebuild"] : st.filter === "review" ? ["repair", "bleed", "ess", "glitch", "smooth", "take", "alt", "hits", "bars", "rebuild"] : ["repair"];
    const mk = (label, action, cls) => { const b = document.createElement("button"); b.className = "small " + (cls || "ghost"); b.textContent = label; b.onclick = () => edit("/api/bulk", { action, ...scopeArgs, types }); bulk.append(b); };
    const what = st.filter === "bleed" || st.filter === "cleanup" || st.filter === "takes" ? "fixes" : "repairs";
    const rec = list.filter(x => x.recommended && x.status === "pending").length;
    if (rec && !["bleed", "cleanup", "takes"].includes(st.filter)) mk(`Apply ${rec} recommended`, "accept_recommended", "primary");
    mk(`Apply all${st.scope ? " here" : ""}`, "accept", "");
    mk(`Reject all${st.scope ? " here" : ""}`, "reject");
    mk("Clear decisions", "reset");
  }

  const wrap = $("#sugList"); wrap.innerHTML = "";
  if (!list.length) {
    const p = document.createElement("p"); p.className = "help";
    p.textContent = ov.stage === "imported" ? "Nothing to review yet. Split the song into instruments (step 2), then click Find damage (step 3)."
      : ov.stage === "split" ? "Nothing to review yet. Click Find damage (step 3) to look for spots the vocals damaged."
      : st.filter === "review" ? "Nothing waiting for review. Applied repairs are under Applied; play the song with Repaired selected to hear them." : "Nothing in this list.";
    wrap.append(p); return;
  }
  let lastSec = -1;
  for (const s of list) {
    if (s.section !== lastSec) {
      lastSec = s.section; const sec = ov.sections[s.section];
      const h = document.createElement("div"); h.className = "group-title"; h.innerHTML = `<i style="background:${sec.color}"></i><span></span><span class="dim small"></span>`;
      h.children[1].textContent = sec.name; h.children[2].textContent = `${fmt(sec.start)} to ${fmt(sec.end)}`; wrap.append(h);
    }
    wrap.append(sugItem(s));
  }
  const selEl = wrap.querySelector(".sug.sel"); if (selEl && st.scrollSel) { selEl.scrollIntoView({ block: "nearest" }); st.scrollSel = false; }
}

function sugItem(s) {
  const d = document.createElement("div"); d.className = "sug" + (st.sel === s.id ? " sel" : ""); d.dataset.id = s.id;
  const color = st.ov.colors[s.stem] || "#888";
  const label = {
    bleed: `Remove vocal leftover from ${s.stem}`,
    glitch: s.kind === "click" ? `Fix a click in the ${s.stem}` : `Fill a gap in the ${s.stem}`,
    smooth: `Smooth watery ${s.stem}`,
    take: `Use the ${s.stem} from ${st.ov.sections[s.source_section]?.name || "another section"}`,
    alt: `Use the other model's ${nice(s.stem)}`,
    hits: `Rebuild ${nice(s.stem)} hits`,
    bars: `Use the cleanest ${nice(s.stem)} bar`,
    ess: `Remove leftover "s" sounds from ${s.stem}`,
    rebuild: `Rebuild ${nice(s.stem)} from matching bars`,
  }[s.type] || `Rebuild ${s.stem}`;
  d.innerHTML = `<div class="sug-row"><span class="dot" style="background:${color}"></span><span class="what"></span><span class="when">${fmtP(s.start)}</span><span class="pill ${inKept(s) ? "kept" : s.status}${!inKept(s) && s.status === "pending" && s.recommended ? " recommended" : ""}">${inKept(s) ? "In a kept part" : statusText(s)}</span></div>`;
  d.querySelector(".what").textContent = label;
  if (st.sel !== s.id) { d.onclick = () => select(s.id); return d; }

  const body = document.createElement("div");
  const sec = st.ov.sections;
  const facts = [];
  if (s.type === "repair") {
    facts.push(["Estimated loss", `${pct(s.severity)} of this ${s.stem} under the vocal`]);
    facts.push(["Match quality", `<span class="meter" style="display:inline-block;width:90px;vertical-align:middle"><i style="width:${pct(s.conf)}"></i></span> ${pct(s.conf)}`]);
    if (s.donors?.length) facts.push(["Borrowed from", s.donors.map((x, i) => `${i ? "" : "<b>"}${esc(sec[x.section].name)} at ${fmtP(x.start)}${i ? "" : "</b>"}`).join(", ")]);
    if (s.manual_donor) facts.push(["", "You chose this source"]);
  } else if (s.type === "bleed") {
    facts.push(["Leftover vocal", `${pct(s.conf)} certain; the part removed sits ${Math.abs(s.removed_db ?? 0).toFixed(0)} dB below the ${s.stem}`]);
  }
  if (s.note && !s.manual_donor && s.type === "repair") facts.push(["Note", esc(s.note)]);
  if (s.check && s.check.flag) facts.push(["Doesn't fit", esc(s.check.flag)]);
  if (["glitch", "smooth", "take", "alt", "hits", "bars", "ess", "rebuild"].includes(s.type)) facts.push(["What", esc(s.note || "")]);
  if (s.type === "rebuild" && s.donors?.length) facts.push(["Borrowed from", s.donors.map(x => `${esc(sec[x.section].name)} at ${fmtP(x.start)}`).join(", ")]);
  if (s.type === "take") facts.push(["How alike", `${pct(s.conf)}, level matched by ${(s.gain_db || 0) >= 0 ? "+" : ""}${(s.gain_db || 0).toFixed(1)} dB`]);
  body.innerHTML = `<div class="facts">${facts.map(([a, b]) => `<span>${a}</span><b>${b}</b>`).join("")}</div>`;

  if (s.status !== "nodonor") {
    const pvRow = document.createElement("div"); pvRow.className = "preview";
    const modes = [["before", "Before"], ["after", "After"], ["patch", s.type === "bleed" || s.type === "ess" ? "Only what's removed" : "Only what's added"]];
    if ((s.type === "repair" || s.type === "rebuild") && s.donors?.length) modes.push(["donor", "Source"]);
    modes.push(["original", "Original song"]);
    for (const [m, label] of modes) {
      const b = document.createElement("button"); b.className = "small"; b.textContent = "▶ " + label;
      b.onclick = () => playPreview(s.id, m, b); pvRow.append(b);
    }
    body.append(pvRow);
    const ab = document.createElement("div"); ab.className = "ab";
    ab.innerHTML = `<button class="small abplay">Loop A/B</button><div class="seg small"><button data-ab="before">A Before</button><button data-ab="after">B After</button></div>
      
      <div class="hear"><span>Hear</span><div class="seg small" role="radiogroup" aria-label="What to hear">
        <button data-hear="mix" title="Every instrument, as it sounds right now">All tracks</button>
        <button data-hear="song" title="Every instrument plus the vocals">All tracks + vocals</button>
        <button data-hear="solo" title="Only this instrument">Just ${esc(s.stem)}</button></div></div>`;
    ab.querySelector(".abplay").onclick = e => playAB(s.id, e.currentTarget);
    ab.querySelectorAll("[data-ab]").forEach(b => { b.classList.toggle("on", b.dataset.ab === (st.abSide || "after")); b.onclick = () => setAB(b.dataset.ab); });
    ab.querySelectorAll("[data-hear]").forEach(b => {
      b.classList.toggle("on", b.dataset.hear === (st.hear || "mix"));
      b.onclick = () => { st.hear = b.dataset.hear; try { localStorage.setItem("hear", st.hear); } catch {} stopPreview(); renderReview(); };
    });
    body.append(ab);
    body.insertBefore(ab.querySelector(".hear"), pvRow);   // choose what you hear first, then play

    const strength = document.createElement("div"); strength.className = "row"; strength.hidden = ["glitch", "take", "alt", "hits", "bars"].includes(s.type);
    strength.innerHTML = `<label>Strength</label><input type="range" min="0.2" max="1.5" step="0.05" value="${s.strength}"><output>${pct(s.strength)}</output>`;
    const sr = strength.querySelector("input"), so = strength.querySelector("output");
    sr.oninput = () => (so.textContent = pct(+sr.value));
    sr.onchange = () => edit("/api/strength", { id: s.id, strength: +sr.value });
    body.append(strength);
  }
  if (s.type === "repair") {
    const donorRow = document.createElement("div"); donorRow.className = "row";
    donorRow.innerHTML = `<label>Source</label><select><option value="">Other places that match…</option></select><button class="small ghost">Pick on timeline</button>`;
    const selEl = donorRow.querySelector("select");
    api.get(`/api/candidates?id=${s.id}`).then(r => {
      for (const c of r.candidates) selEl.add(new Option(`${st.ov.sections[c.section].name} at ${fmtP(c.start)}, ${pct(c.match)} match`, c.beat));
      if (!r.candidates.length) selEl.options[0].text = "No other matching places";
    }).catch(() => {});
    selEl.onchange = () => { if (selEl.value !== "") edit("/api/donor", { id: s.id, beat: +selEl.value }); };
    donorRow.querySelector("button").onclick = () => startPick(s);
    body.append(donorRow);
  }
  const dec = document.createElement("div"); dec.className = "decide";
  if (s.status !== "nodonor") {
    const applied = ok(s);
    const a = document.createElement("button"); a.className = applied ? "ghost" : "primary";
    a.textContent = applied ? (s.type === "bleed" || s.type === "ess" ? "Cleaned" : "Applied") : (s.type === "bleed" || s.type === "ess" ? "Clean it" : "Apply");
    a.disabled = applied; a.onclick = () => decide(s.id, "accepted");
    const r = document.createElement("button"); r.className = "ghost"; r.textContent = s.status === "rejected" ? "Rejected" : (s.type === "bleed" ? "Keep as is" : "Reject");
    r.disabled = s.status === "rejected"; r.onclick = () => decide(s.id, "rejected");
    dec.append(a, r);
  }
  const go = document.createElement("button"); go.className = "ghost"; go.textContent = "Show on timeline"; go.onclick = () => focusOn(s);
  dec.append(go);
  body.append(dec);
  d.append(body);
  return d;
}
// Apply or reject, then move on to the next item if this one leaves the current list
async function decide(id, status) {
  const ids = $$("#sugList .sug").map(x => x.dataset.id), i = ids.indexOf(id);
  const next = ids[i + 1] || ids[i - 1];
  await edit("/api/status", { ids: [id], status });
  const now = st.ov.suggestions.find(x => x.id === id);
  if (now && !FILTERS[st.filter](now) && next) select(next);
}
function esc(t) { const d = document.createElement("div"); d.textContent = t; return d.innerHTML; }

function select(id, opt = {}) {
  stopPreview(); st.sel = id; st.scrollSel = true;
  const s = st.ov.suggestions.find(x => x.id === id);
  if (s && !FILTERS[st.filter](s)) st.filter = "all";
  if (s && st.scope && ((st.scope.section != null && st.scope.section !== s.section) || (st.scope.stem && st.scope.stem !== s.stem))) st.scope = null;
  activateTab("review");
  $$(".stem-lane").forEach(l => l.classList.toggle("focus", s && l.dataset.stem === s.stem));
  renderReview(); renderLanes();
}
function focusOn(s) {
  const span = Math.max(8, (s.end - s.start) * 4);
  const target = laneWidth() / span; st.view.pps = Math.max(target, laneWidth() / st.ov.meta.duration);
  const minP = laneWidth() / st.ov.meta.duration; $("#zoom").value = 100 * Math.log(st.view.pps / minP) / Math.log(400 / minP);
  st.view.start = clamp(s.start - viewSec() / 2 + (s.end - s.start) / 2, 0, Math.max(0, st.ov.meta.duration - viewSec()));
  seek(s.start - 1); renderLanes();
  $(`.stem-lane[data-stem="${s.stem}"]`)?.scrollIntoView({ block: "nearest" });
}
function startPick(s) {
  st.picking = { id: s.id, stem: s.stem }; $("#pickStem").textContent = s.stem; $("#pickHint").hidden = false;
  if (st.view.pps * st.ov.meta.duration > laneWidth() * 1.05) { fitView(); renderLanes(); }
}
function endPick() { st.picking = null; st.hoverT = null; $("#pickHint").hidden = true; renderLanes(); }
$("#pickCancel").onclick = endPick;

/* ---------- repairability grid */
function renderBest() {
  const w = $("#bestList"), list = st.ov.best_versions || [];
  if (st.ov.stage !== "detected") { w.innerHTML = `<p class="help">Available after Find damage.</p>`; return; }
  w.innerHTML = list.length ? "" : `<p class="help">No instrument has a cleaner take of the same part elsewhere in this song.</p>`;
  for (const x of list) {
    const d = document.createElement("div"); d.className = "best-item";
    d.innerHTML = `<div class="bi-top"><span class="dot" style="background:${st.ov.colors[x.stem] || "#888"}"></span><b></b><span class="pill"></span></div>
      <p></p><div class="row"><label>Use take from</label><select></select></div>
      <div class="btns"><button class="small primary">Apply</button><button class="small">Add to review</button></div>`;
    d.querySelector("b").textContent = `${x.stem} in ${x.name}`;
    const pill = d.querySelector(".pill");
    pill.textContent = x.status ? statusText({ type: "take", status: x.status }) : `${pct(x.cover)} lost here`;
    d.querySelector("p").textContent = `Cleanest same part: ${x.best.name} (${pct(x.best.cover)} lost, ${pct(x.best.similarity)} alike).`;
    const sel = d.querySelector("select");
    for (const c of x.candidates) sel.add(new Option(`${c.name}: ${pct(c.similarity)} alike, ${pct(c.cover)} lost`, c.section));
    if (x.source != null) sel.value = x.source;
    const [ap, rv] = d.querySelectorAll(".btns button");
    ap.onclick = () => addFixes("/api/take", { stem: x.stem, section: x.section, source: +sel.value }, true);
    rv.onclick = () => addFixes("/api/take", { stem: x.stem, section: x.section, source: +sel.value }, false);
    w.append(d);
  }
}
// Create fixes and either apply them right away or queue them for review; either way stay on this tab
async function addFixes(path, body, accept) {
  try {
    const r = await api.post(path, body);
    const bad = new Set(r.overview.suggestions.filter(x => r.new_ids.includes(x.id) && x.check && x.check.flag).map(x => x.id));
    const good = r.new_ids.filter(id => !bad.has(id));
    if (accept && good.length) {
      await applyEdit(await api.post("/api/status", { ids: good, status: "accepted" }));
    } else await applyEdit(r);
    const n = r.new_ids.length;
    if (!n) return toast("Nothing there was damaged enough to rebuild.");
    const held = bad.size ? ` ${bad.size} didn't line up with the original, so ${bad.size === 1 ? "it waits" : "they wait"} in Review under Doesn't fit.` : "";
    toast(accept ? (good.length ? `Applied ${good.length} fix${good.length === 1 ? "" : "es"}. Undo takes ${good.length === 1 ? "it" : "them"} back.` : "Nothing applied.") + held
                 : `Added ${n} item${n === 1 ? "" : "s"} to Review.` + held,
      { action: ["Show in Review", () => { st.filter = "takes"; st.scope = null; select(r.new_ids[0]); }] });
  } catch (e) { fail(e); }
}
async function applyBest(kind, accept, blend = false) {
  try {
    const r = await api.post("/api/apply_best", { kind, accept, blend });
    await applyEdit(r);
    const n = r.new_ids.length;
    if (!n) return toast("Nothing left to rebuild there.");
    const bad = r.overview.suggestions.filter(x => r.new_ids.includes(x.id) && x.check && x.check.flag).length;
    const held = bad ? ` ${bad} didn't line up with the original and wait${bad === 1 ? "s" : ""} in Review under Doesn't fit.` : "";
    toast(accept ? `Applied ${n - bad} repeated-part fix${n - bad === 1 ? "" : "es"}. Undo takes them all back in one step.${held}`
                 : `Added ${n} item${n === 1 ? "" : "s"} to Review.`,
      { ms: 8000, action: ["Show in Review", () => { st.filter = "takes"; st.scope = null; select(r.new_ids[0]); }] });
  } catch (e) { fail(e); }
}
$$("#bulkBest button, .hdr-btn").forEach(b => b.onclick = () => applyBest(b.dataset.kind, b.dataset.accept === "1", b.dataset.blend === "1"));

function renderHitsBars() {
  const ov = st.ov, hl = $("#hitList"), bl = $("#barList");
  if (ov.stage !== "detected") { hl.innerHTML = bl.innerHTML = `<p class="help">Available after Find damage.</p>`; return; }
  // onBlend (drum hits only): an extra Blend button that applies a blend of the cleanest hits
  const row = (title, pillText, body, onAdd, color, onBlend, blendTip) => {
    const d = document.createElement("div"); d.className = "best-item";
    d.innerHTML = `<div class="bi-top"><span class="dot" style="background:${color}"></span><b></b><span class="pill"></span></div><p></p>
      <div class="btns"><button class="small primary">Apply</button>${onBlend ? `<button class="small blend">Blend</button>` : ""}<button class="small">Add to review</button></div>`;
    d.querySelector("b").textContent = title; d.querySelector(".pill").textContent = pillText; d.querySelector("p").textContent = body;
    const bs = d.querySelectorAll("button");
    bs[0].onclick = () => onAdd(true); bs[bs.length - 1].onclick = () => onAdd(false);
    if (onBlend) { bs[1].onclick = () => onBlend(); bs[1].title = blendTip || ""; }
    return d;
  };
  const added = (path, body) => async accept => addFixes(path, body, accept);
  hl.innerHTML = (ov.best_hits || []).length ? "" : ov.warming ? `<p class="help"><span class="spin small"></span>Looking for repeated hits and notes. They'll show up here in a moment.</p>` : `<p class="help">No smeared drum hits found${ov.stems.some(k => ["drums", "kick", "snare", "toms", "cymbals"].includes(k)) ? "" : " (no drum track)"}.</p>`;
  for (const h of ov.best_hits || []) {
    const canBlend = (h.blend_n || 0) >= 2;
    hl.append(row(`${nice(h.stem)}: ${h.name}`,
      h.status ? statusText({ type: "hits", status: h.status }) + (h.blended ? " (blend)" : "") : `${h.damaged} under vocals`,
      `${h.count} hits in the song, ${h.clean ?? "?"} clean; cleanest at ${fmtP(h.best_time)}.`,
      added("/api/hits", { stem: h.stem, sound: h.sound }), ov.colors[h.stem] || "#888",
      canBlend ? () => addFixes("/api/hits", { stem: h.stem, sound: h.sound, blend: true }, true) : null,
      `Apply, filling in from a blend of the ${h.blend_n} cleanest ${h.name} hits instead of just one. ` +
      `Evens out one-off noise in any single hit. Replaces the single-hit version if one is applied.`));
  }
  bl.innerHTML = (ov.best_bars || []).length ? "" : `<p class="help">No damaged repeats of a bar found.</p>`;
  for (const g of ov.best_bars || []) {
    bl.append(row(`${nice(g.stem)} bar from ${fmtP(g.best_time)}`, g.status ? statusText({ type: "bars", status: g.status }) : `${g.damaged.length} damaged`,
      `Played ${g.count} times; damaged at ${g.damaged_times.map(fmtP).join(", ")}.`,
      added("/api/bars", { stem: g.stem, best: g.best, damaged: g.damaged }), ov.colors[g.stem] || "#888"));
  }
}
function renderGrid() {
  renderBest(); renderHitsBars();
  const any = st.ov.stage === "detected" && ((st.ov.best_versions || []).some(x => !x.status) || (st.ov.best_hits || []).some(x => !x.status) || (st.ov.best_bars || []).some(x => !x.status)
    || (st.ov.best_hits || []).some(x => x.status === "accepted" && !x.blended && (x.blend_n || 0) >= 2));
  $("#bulkBest").hidden = !any;
  $$(".hdr-btn").forEach(b => (b.hidden = st.ov.stage !== "detected"));
  const ov = st.ov, w = $("#gridWrap");
  if (ov.stage !== "detected") { w.innerHTML = `<p class="help">This fills in after step 3, Find damage.</p>`; return; }
  const STATE = { clean: "No vocals", undamaged: "Undamaged", repaired: "Repaired", repairable: "Repairable", partial: "Partly", hard: "Hard to fix" };
  const Q = ov.meta.quality, stale = Q && Q.applied !== ov.applied_count;
  const qcell = i => { if (!Q) return ""; const b = Q.before[String(i)], n = Q.now[String(i)];
    if (n == null) return `<td class="q dim">-</td>`;
    const up = b != null && n > b, col = n >= 85 ? "var(--ok)" : n >= 70 ? "#e3c46f" : "var(--damage)";
    return `<td class="q" title="Clean score, 0 to 100: higher means less sound the original doesn't have and less leftover singing.${stale ? " Changed since scored: score again." : ""}"><b style="color:${col}">${n}</b>${b != null ? `<span class="dim small">${up ? "up from" : "was"} ${b}</span>` : ""}</td>`; };
  let html = `<table class="grid"><thead><tr><th></th>${ov.stems.map(k => `<th><span class="dot" style="display:inline-block;background:${ov.colors[k]}"></span> ${esc(k)}</th>`).join("")}${Q ? `<th>Clean score${stale ? " *" : ""}</th>` : ""}</tr></thead><tbody>`;
  html += `<tr><th class="sec">Whole song</th>${ov.stems.map(k => { const s = ov.stem_summary[k];
    const state = !s.count ? "undamaged" : s.repairability >= 0.55 ? "repairable" : s.repairability >= 0.3 ? "partial" : "hard";
    return `<td class="st-${state}" data-stem="${k}"><b>${s.count ? pct(Math.min(1, s.damage)) + " hit" : "Clean"}</b>${s.count ? pct(s.repairability) + " fixable" : ""}</td>`; }).join("")}${Q ? "<td></td>" : ""}</tr>`;
  ov.sections.forEach((sec, i) => {
    html += `<tr><th class="sec" title="Double-click to rename" data-sec="${i}"><span class="dot" style="display:inline-block;background:${sec.color}"></span> ${esc(sec.name)}</th>`;
    for (const k of ov.stems) {
      const g = ov.grid[`${i}:${k}`] || { state: "clean" };
      const body = g.count ? `<b>${pct(Math.min(1, g.damage))} hit</b>${pct(g.repairability)} fixable` : `<b>${STATE[g.state]}</b>`;
      html += `<td class="st-${g.state}" data-sec="${i}" data-stem="${k}" title="${STATE[g.state]}: ${g.count} spot(s), ${pct(g.fixed || 0)} repaired">${body}</td>`;
    }
    html += `${qcell(i)}</tr>`;
  });
  html += `</tbody></table><div class="grid-key"><span style="color:var(--ok)">Good source found</span><span style="color:#e3c46f">Partial match</span><span style="color:var(--damage)">No good source</span><span style="color:var(--repair)">Repaired</span></div>`;
  w.innerHTML = html;
  const qb = $("#btnQuality"); if (qb) { qb.hidden = ov.stage !== "detected"; qb.textContent = Q ? "Score again" : "Score how clean each section is"; }
  w.querySelectorAll("td:not(.q)").forEach(td => td.onclick = () => {
    const sec = td.dataset.sec != null ? +td.dataset.sec : null; st.filter = "all"; setScope({ section: sec, stem: td.dataset.stem });
  });
  w.querySelectorAll("th[data-sec]").forEach(th => th.ondblclick = async () => {
    const i = +th.dataset.sec; const name = prompt("Section name", ov.sections[i].name);
    if (name) edit("/api/rename_section", { index: i, name });
  });
}

/* ---------- levels */
function sectionMuted(i, k) {
  const sec = st.ov.sections[i];
  return (st.ov.mixer.mute_ranges || []).some(r => (r.stem === k || !r.stem) && r.start <= sec.start + 0.05 && r.end >= sec.end - 0.05);
}
function renderLevels() {
  const ov = st.ov, w = $("#levelsWrap"), sg = ov.mixer.section_gain || {};
  if (!ov.stems.length) { w.innerHTML = `<p class="help">Levels for each instrument appear after step 2, Split into instruments.</p>`; return; }
  const cols = ov.stems.concat(["vocals"]);
  let html = `<table class="levels"><thead><tr><th></th>${cols.map(k => `<th>${esc(k)}</th>`).join("")}</tr></thead><tbody>`;
  ov.sections.forEach((sec, i) => {
    html += `<tr><th>${esc(sec.name)}</th>`;
    for (const k of cols) {
      const lv = ov.levels[`${i}:${k}`]; const m = clamp((lv + 60) / 60, 0, 1);
      const g = sg[`${i}:${k}`] || 0;
      html += `<td><div class="lvl"><span class="dim small">${lv < -90 ? "silent" : lv.toFixed(1) + " dB"}</span><div class="m"><i style="width:${pct(m)};background:${ov.colors[k] || "#888"}"></i></div>`
        + (k === "vocals" ? "" : `<div class="lvl-row"><input type="number" step="0.5" min="-24" max="12" value="${g}" class="${g ? "changed" : ""}" data-key="${i}:${k}" title="Boost or cut in dB">`
          + `<button class="mbtn ${sectionMuted(i, k) ? "on" : ""}" data-sec="${i}" data-stem="${k}" title="Mute ${k} in ${esc(sec.name)}">M</button></div>`) + `</div></td>`;
    }
    html += `</tr>`;
  });
  html += `</tbody></table><div class="row" style="margin-top:12px"><button class="ghost small" id="lvlReset">Reset section levels</button></div>`;
  w.innerHTML = html;
  w.querySelectorAll("input").forEach(inp => inp.onchange = () => {
    const v = clamp(+inp.value || 0, -24, 12); ov.mixer.section_gain[inp.dataset.key] = v; inp.classList.toggle("changed", !!v); pushMixer();
  });
  $("#lvlReset").onclick = () => { ov.mixer.section_gain = {}; pushMixer(); };
  w.querySelectorAll(".mbtn").forEach(btn => btn.onclick = () => {
    const sec = ov.sections[+btn.dataset.sec], k = btn.dataset.stem, list = ov.mixer.mute_ranges || [];
    const same = r => r.stem === k && Math.abs(r.start - sec.start) < 0.05 && Math.abs(r.end - sec.end) < 0.05;
    setMutes(list.some(same) ? list.filter(r => !same(r)) : [...list, { start: sec.start, end: sec.end, stem: k }]);
  });
}

/* ---------- settings */
function renderSettings() {
  const s = st.ov.meta.settings;
  const a = $("#autoConf"), r = $("#reviewConf");
  if (document.activeElement !== a) a.value = s.auto_conf; if (document.activeElement !== r) r.value = s.review_conf;
  $("#autoConfVal").textContent = pct(+a.value); $("#reviewConfVal").textContent = pct(+r.value);
  for (const [id, key, def] of [["#guardBefore", "guard_before", 0.3], ["#guardAfter", "guard_after", 0.8]]) {
    const el = $(id); if (el && document.activeElement !== el) el.value = s[key] ?? def;
  }
}
for (const [id, key] of [["#guardBefore", "guard_before"], ["#guardAfter", "guard_after"]])
  $(id).onchange = e => { const v = Math.max(0, Math.min(5, +e.target.value || 0)); edit("/api/settings", { [key]: v }); };
$("#autoConf").oninput = e => ($("#autoConfVal").textContent = pct(+e.target.value));
$("#reviewConf").oninput = e => ($("#reviewConfVal").textContent = pct(+e.target.value));
$("#btnApplySettings").onclick = () => edit("/api/settings", { auto_conf: +$("#autoConf").value, review_conf: +$("#reviewConf").value });

/* ---------- tabs */
function activateTab(name) {
  $$(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === name));
  $$(".tab").forEach(t => (t.hidden = t.id !== "tab-" + name));
}
$$(".tabs button").forEach(b => b.onclick = () => activateTab(b.dataset.tab));

/* ---------- export */
$("#btnExport").onclick = () => $("#dlgExport").showModal();
$("#dlgExport").addEventListener("close", async () => {
  const dlg = $("#dlgExport"); if (dlg.returnValue !== "ok") return;
  const f = dlg.querySelector("form");
  const what = ["mix", "stems", "raw", "vocals", "report", "samples_raw", "samples_merged"].filter(n => f.elements[n] && f.elements[n].checked);
  const format = f.elements.fmt.value;
  showBusy(0.3, "Writing files");
  try {
    const r = await api.post("/api/export", { what, format, reveal: true, match_level: f.elements.match_level.checked,
                                               declick: f.elements.declick.checked });
    hideBusy(); toast(`Saved ${r.files.length} file(s) to ${r.folder}`, { ms: 9000, action: ["Show folder", () => api.post("/api/reveal", { path: r.folder })] });
  } catch (e) { hideBusy(); fail(e); }
});

/* ---------- keyboard */
document.addEventListener("keydown", e => {
  if (!st.ov || $("#workspace").hidden || e.target.matches("input, select, textarea")) return;
  if (e.key === " ") { e.preventDefault(); togglePlay(); }
  else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); if (st.ov.can_undo) edit("/api/undo", {}); }
  else if (e.key === "Escape") { if (st.picking) endPick(); else if (!$("#keepPop").hidden) { hideKeepPop(); renderLanes(); } else stopPreview(); }
  else if (e.key === "j" || e.key === "k") {
    const items = $$("#sugList .sug").map(x => x.dataset.id); if (!items.length) return;
    const i = items.indexOf(st.sel); const n = e.key === "j" ? Math.min(items.length - 1, i + 1) : Math.max(0, i - 1);
    select(items[i < 0 ? 0 : n]);
  } else if (st.sel && (e.key === "a" || e.key === "b")) setAB(e.key === "a" ? "before" : "after");
  else if (st.sel && e.key === "Enter") decide(st.sel, "accepted");
  else if (st.sel && (e.key === "Backspace" || e.key === "Delete")) decide(st.sel, "rejected");
});


/* ------------------------------------------------------------------ updates */
const upd = { polling: false, announced: false, autoRestarted: false };
function renderUpdate(s) {
  if (!s) return;
  const u = s.update, info = u.info, prefs = s.update_prefs;
  $("#verNow").textContent = `Version ${s.version}`;
  $("#welcomeVer").textContent = `Instrumental Studio ${s.version}`;
  $("#welcomeUpdStatus").textContent = $("#updStatus").textContent;
  $("#btnWelcomeCheck").disabled = u.checking || u.installing;
  const wi = $("#btnWelcomeInstall");
  wi.hidden = !((info && info.available) || u.installed);
  wi.disabled = u.installing;
  wi.textContent = u.installed ? "Restart to update" : u.installing ? (u.message || "Installing…") : `Install ${info?.latest || "update"}`;
  $("#updRepo").textContent = `github.com/${s.repo}`;
  $("#updOnStart").checked = !!prefs.check_on_start; $("#updAuto").checked = !!prefs.auto_install;
  let status = "";
  if (u.checking) status = "Checking…";
  else if (u.error) status = u.error;
  else if (u.installed) status = `Version ${u.installed} is installed. Restart to start using it.`;
  else if (info && info.available) status = `Version ${info.latest} is available.`;
  else if (info && info.latest) status = `You have the latest version.`;
  else if (info) status = info.message || "";
  $("#updStatus").textContent = status;
  $("#welcomeUpdStatus").textContent = status;
  $("#btnCheckUpdate").disabled = u.checking || u.installing;
  const showBox = !!(info && info.available && !u.installed);
  $("#updBox").hidden = !showBox;
  if (showBox) {
    $("#updTitle").textContent = `${info.name}${info.published ? ", released " + new Date(info.published).toLocaleDateString() : ""}`;
    $("#updNotes").textContent = info.notes || "No release notes.";
    $("#updNotes").hidden = !info.notes;
    $("#updLink").href = info.url || "#";
    $("#btnInstallUpdate").disabled = u.installing;
    $("#btnInstallUpdate").textContent = u.installing ? (u.message || "Installing…") : "Install update";
  }
  $("#updBarWrap").hidden = !u.installing; $("#updBar").style.width = pct(u.progress || 0);
  $("#updRestartRow").hidden = !u.installed;
  const pill = $("#updPill");
  if (u.installed) { pill.hidden = false; pill.textContent = "Restart to update"; }
  else if (info && info.available) { pill.hidden = false; pill.textContent = u.installing ? "Updating…" : `Update to ${info.latest}`; }
  else pill.hidden = true;

  if (s.just_updated && !upd.announced) { upd.announced = true; toast(`Updated to version ${s.version}.`); }
  // installed in the background with the user's standing permission: restart now if nothing is open
  if (u.installed && u.auto && !upd.autoRestarted && !s.job.running && !$("#welcome").hidden) {
    upd.autoRestarted = true; restartApp(true);
  }
}
async function pollUpdate(maxMs = 120000) {
  if (upd.polling) return; upd.polling = true;
  const t0 = Date.now();
  try {
    for (;;) {
      const s = await api.get("/api/state"); st.state = s; renderUpdate(s);
      const u = s.update;
      if (!u.checking && !u.installing && (u.info || u.error || Date.now() - t0 > 8000)) break;
      if (Date.now() - t0 > maxMs) break;
      await new Promise(r => setTimeout(r, 700));
    }
  } catch (e) { /* server restarting */ }
  upd.polling = false;
}
$("#btnCheckUpdate").onclick = async () => {
  try { $("#updStatus").textContent = "Checking…"; await api.post("/api/update/check"); } catch (e) { fail(e); }
  pollUpdate();
};
async function installUpdate() {
  try { await api.post("/api/update/install"); pollUpdate(); } catch (e) { fail(e); }
}
$("#btnInstallUpdate").onclick = installUpdate;
$("#btnWelcomeCheck").onclick = () => $("#btnCheckUpdate").onclick();
$("#btnWelcomeInstall").onclick = () => (st.state?.update?.installed ? restartApp(false) : installUpdate());
$("#btnRestart").onclick = () => restartApp(false);
$("#updPill").onclick = () => {
  const u = st.state?.update;
  if (u?.installed) return restartApp(false);
  if (u?.installing) return;
  const v = u?.info?.latest;
  if (confirm(`Install version ${v} now? Your projects are kept. The app restarts when it's done.`)) installUpdate();
};
async function restartApp(auto) {
  try {
    await api.post("/api/update/restart");
    showBusy(1, auto ? "Installed an update. Restarting…" : "Restarting…");
    setTimeout(() => { $("#busyMsg").textContent = "The updated app opens in a new window. You can close this one."; try { window.close(); } catch {} }, 4000);
  } catch (e) { fail(e); }
}
async function savePrefs() {
  try { await api.post("/api/update/prefs", { check_on_start: $("#updOnStart").checked, auto_install: $("#updAuto").checked }); }
  catch (e) { fail(e); }
}
$("#updOnStart").onchange = savePrefs;
$("#updAuto").onchange = async () => {
  await savePrefs();
  const u = st.state?.update;
  if ($("#updAuto").checked && u?.info?.available && !u.installing && !u.installed) installUpdate();
};

/* ------------------------------------------------------------------ separation model status */
let modelTimer = null;
function renderModel(s) {
  const m = s.model || {}, plan = m.plan || {};
  const state = m.installing ? "busy" : m.error ? "err" : m.installed ? "ok" : "off";
  const size = plan.size || "about 1-3 GB", runsOn = plan.runs_on || "your computer";
  const text = {
    ok: ["Separation model ready", `Songs are split on ${m.device || runsOn}.${m.note ? " " + m.note : ""}`],
    busy: [`Downloading the separation model: ${pct(m.progress || 0)}`, `${m.message || "Starting"}. You can import stems or try the demo meanwhile.`],
    err: ["Separation model not installed", m.error || "Something went wrong."],
    off: ["Separation model not installed", `Needed only for the Split step. You can open songs without it. One-time download of ${size}, set up to run on ${runsOn}. Nothing downloads until you click the button.`],
  }[state];
  $("#modelLight").className = "light " + state;
  $("#modelTitle").textContent = text[0];
  $("#modelDetail").textContent = text[1];
  $("#modelBarWrap").hidden = state !== "busy"; $("#modelBar").style.width = pct(m.progress || 0);
  const get = $("#btnModelGet");
  get.hidden = state !== "off"; get.textContent = `Download (${size.replace(/^about /, "")})`;
  $("#btnModelRetry").hidden = state !== "err";
  $("#btnOpenSong").classList.add("primary");
  $("#modelPillLight").className = "light small " + state;
  $("#modelPillText").textContent = { ok: "Model ready", busy: `Model ${pct(m.progress || 0)}`, err: "Model not installed", off: "Model not installed" }[state];
  $("#engineInfo").textContent = text[0] + ". " + text[1];
  $("#btnModelReinstall").disabled = state === "busy";
  $("#btnModelReinstall").textContent = state === "ok" ? "Reinstall separation model" : `Download separation model (${size.replace(/^about /, "")})`;
  if (state === "busy" && !modelTimer) {
    modelTimer = setInterval(async () => {
      try {
        const s2 = await api.get("/api/state"); st.state = s2; renderModel(s2);
        if (!s2.model.installing) {
          clearInterval(modelTimer); modelTimer = null;
          if (s2.model.installed) toast("The separation model is ready. You can split songs now.");
        }
      } catch {}
    }, 1000);
  }
}
function renderExtras(s) {
  const ex = s.extras || {}, wrap = $("#extrasList"); wrap.innerHTML = "";
  $("#extrasCard").hidden = !s.model?.installed;
  for (const [key, e] of Object.entries(ex)) {
    const state = e.installing ? "busy" : e.error ? "err" : e.installed ? "ok" : "off";
    const row = document.createElement("div"); row.className = "extra-row";
    row.innerHTML = `<i class="light ${state}"></i><div class="et"><b></b><span></span></div>`;
    row.querySelector("b").textContent = e.label + (e.installing ? `: ${pct(e.progress || 0)}` : e.installed ? ": ready" : "");
    row.querySelector("span").textContent = e.error ? e.error : `${e.what} ${e.installed ? "" : "Download: " + e.size + "."}`;
    if (!e.installed && !e.installing) {
      const b = document.createElement("button"); b.className = "small"; b.textContent = e.error ? "Try again" : "Download";
      b.onclick = () => installExtra(key); row.append(b);
    }
    wrap.append(row);
  }
  const lr = $("#lightRoformer"), ld = $("#lightDrumsep");
  if (lr && ex.roformer) lr.className = "light small " + (ex.roformer.installed ? "ok" : ex.roformer.installing ? "busy" : "off");
  if (ld && ex.drumsep) ld.className = "light small " + (ex.drumsep.installed ? "ok" : ex.drumsep.installing ? "busy" : "off");
  const sel = $("#sepMode");
  for (const o of sel.options) {
    if (o.value.startsWith("hybrid")) {
      o.textContent = shortModel(s.models[o.value] || o.textContent).replace(/ \(extra download\)$/, "") + (ex.roformer?.installed ? "" : " (extra download)");
    }
  }
  const busy = Object.values(ex).some(e => e.installing);
  if (busy && !extrasTimer) extrasTimer = setInterval(async () => {
    try { const s2 = await api.get("/api/state"); st.state = s2; renderExtras(s2); if (st.ov) renderSteps();
      if (!Object.values(s2.extras).some(e => e.installing)) { clearInterval(extrasTimer); extrasTimer = null; } } catch {}
  }, 1000);
}
let extrasTimer = null;
async function installExtra(key) {
  try { await api.post("/api/extra/install", { key }); const s = await api.get("/api/state"); st.state = s; renderExtras(s); }
  catch (e) { fail(e); }
}

async function installModel(force) {
  try { await api.post("/api/model/install", { force }); renderModel(await api.get("/api/state")); } catch (e) { fail(e); }
}
$("#btnModelRetry").onclick = () => installModel(false);
$("#btnModelGet").onclick = () => installModel(false);
$("#btnModelReinstall").onclick = () => {
  const inst = modelReady();
  if (!inst || confirm("Download the separation model again? Use this if splitting stopped working.")) installModel(inst);
};
$("#modelPill").onclick = () => { if (!$("#welcome").hidden) { const c = $("#modelCard"); c.classList.add("attention"); setTimeout(() => c.classList.remove("attention"), 1500); return; }
  stopPreview(); player.pause(); showWelcome(true); };

/* ------------------------------------------------------------------ troubleshooting */
let stuckCount = 0, stuckDismissed = 0;
function noteAlive() { stuckCount = 0; if (!$("#stuck").hidden) $("#stuck").hidden = true; }
function noteStuck() {
  stuckCount++;
  if (stuckCount === 3) clientLog("the window got no answer from the app 3 times in a row (stuck panel shown)");
  if (stuckCount >= 3 && Date.now() - stuckDismissed > 60000) {
    $("#stuck").hidden = false;
    $("#stuckHint").textContent = native() ? "" : "If Restart doesn't work, close this window and start the app again.";
  }
}
async function restartNow() {
  if (native() && native().restart) { try { await native().restart(); return; } catch {} }
  try { await api.post("/api/restart", { force: true }); showBusy(1, "Restarting…"); } catch (e) { fail(e); }
}
$("#btnStuckRestart").onclick = restartNow;
$("#btnStuckWait").onclick = () => { $("#stuck").hidden = true; stuckDismissed = Date.now(); };
$("#btnStop").onclick = async () => { try { await api.post("/api/stop"); $("#busyMsg").textContent = "Stopping…"; } catch (e) { fail(e); } };
function openTrouble() { $("#resetLog").textContent = ""; $("#dlgReset").showModal(); }
$("#btnBusyHelp").onclick = openTrouble;
$("#btnTrouble").onclick = openTrouble;
$$("[data-trouble]").forEach(b => (b.onclick = openTrouble));
$("#resetClose").onclick = () => $("#dlgReset").close();
$$("[data-reset]").forEach(b => b.onclick = async () => {
  const what = b.dataset.reset, log = $("#resetLog");
  try {
    if (what === "stop") { const r = await api.post("/api/stop"); log.textContent = r.stopped ? `Stopped ${r.stopped} background task${r.stopped === 1 ? "" : "s"}.` : "Nothing was running."; return; }
    if (what === "restart") { $("#dlgReset").close(); return restartNow(); }
    if (what === "reinstall") {
      if (!confirm("Download and set up the separation model again? This takes a few minutes.")) return;
      await api.post("/api/stop"); await installModel(true); log.textContent = "Reinstalling. Watch the light on the start screen."; return;
    }
    if (what === "settings") { try { localStorage.clear(); } catch {} }
    if (what === "project") { stopPreview(); player.pause(); }
    const r = await api.post("/api/reset", { what });
    log.textContent = r.message || "Done.";
    if (what === "project") { $("#dlgReset").close(); showWelcome(true); }
    refreshState().then(renderUpdate);
  } catch (e) { log.textContent = e.message; }
});

/* ------------------------------------------------------------------ boot */
(async function boot() {
  const msg = t => { const m = $("#bootMsg"); if (m) m.textContent = t; };
  $("#welcome").hidden = false;          // usable right away; its lists fill in as soon as the app answers
  try {
    let s;
    for (let i = 0; ; i++) {
      try { s = await refreshState(); break; }
      catch (e) {
        if (i >= 4) throw e;
        msg(i ? "Still starting. The first start after an update or restart can take a little longer." : "Waking up the app");
        await new Promise(r => setTimeout(r, 1500));
      }
    }
    msg(s.has_project ? `Opening ${s.project || "your song"}` : "Ready");
    $("#safeBanner").hidden = !s.safe;
    api.post("/api/booted", { ms: Math.round(performance.now()) }).catch(() => {});      // tells the app this start finished fine
    renderUpdate(s); pollUpdate(60000);
    if (s.job.running) { $("#boot").hidden = true; watchJob(); return; }
    if (s.has_project) { $("#boot").hidden = true; await openWorkspace(true); hideBusy(); return; }
    showWelcome(true);
  } catch (e) { fail(e); hideBusy(); }
  finally { $("#boot").hidden = true; }
})();

$("#btnQuality").onclick = () => scoreSections();

async function exportLog() {
  try {
    const r = await api.post("/api/log/export", { reveal: true });
    toast(`Log saved: ${r.path}. Attach that file when asking for help.`, { ms: 12000 });
  } catch (e) { fail(e); }
}
$$("[data-export-log]").forEach(b => b.onclick = exportLog);

/* ---------- Build the best instrumental */
async function openAutoBuild() {
  try {
    const r = await api.post("/api/auto/plan", {});
    const box = $("#autoSteps"); box.innerHTML = "";
    for (const stp of r.steps) {
      const row = document.createElement("label"); row.className = "auto-step" + (stp.on ? "" : " off");
      row.innerHTML = `<input type="checkbox" name="${stp.key}"${stp.on ? " checked" : ""}><div><b></b><span></span></div>`;
      row.querySelector("b").textContent = stp.title + (stp.count > 1 && !["mutes", "tone", "bestof", "improve", "seams"].includes(stp.key) ? ` (${stp.count})` : "");
      row.querySelector("span").textContent = stp.detail;
      box.append(row);
    }
    $("#dlgAuto").showModal();
  } catch (e) { fail(e); }
}
$("#dlgAuto").addEventListener("close", async () => {
  const dlg = $("#dlgAuto"); if (dlg.returnValue !== "ok") return;
  const steps = $$("#autoSteps input").filter(i => i.checked).map(i => i.name);
  if (!steps.length) return toast("Nothing chosen.");
  try {
    stopPreview(); player.pause();
    await api.post("/api/auto/build", { steps });
    showBusy(0.02, "Building the best instrumental");
    watchJob();
  } catch (e) { fail(e); }
});
function showAutoReport(lines, title) {
  $("#autoDoneTitle").textContent = title || "Built";
  const ul = $("#autoReport"); ul.innerHTML = "";
  for (const l of (lines.length ? lines : ["Nothing needed changing."])) { const li = document.createElement("li"); li.textContent = l; ul.append(li); }
  $("#dlgAutoDone").showModal();
}
$$("[data-auto]").forEach(b => b.onclick = openAutoBuild);

$$("[data-export-samples]").forEach(b => b.onclick = async () => {
  showBusy(0.3, "Saving hits, notes and loops");
  try {
    const r = await api.post("/api/export", { what: ["samples_raw", "samples_merged"], format: "wav", reveal: true });
    hideBusy(); toast(`Saved ${r.files.length} sample file(s) to ${r.folder}`, { ms: 9000 });
  } catch (e) { hideBusy(); fail(e); }
});

/* ---------- vinyl crackle */
$("#btnVinyl").onclick = async () => {
  const ov = st.ov;
  if (ov.stage !== "imported" && !confirm("This cleans the recording, then splits it again with the same settings (the split was made " +
      "from the crackly recording). Repairs and decisions will need doing again. Continue?")) return;
  try {
    stopPreview(); player.pause();
    await api.post("/api/vinyl", { strength: $("#vinylStrength").value });
    showBusy(0.02, "Removing vinyl crackle"); watchJob();
  } catch (e) { fail(e); }
};
$("#btnVinylUndo").onclick = async () => {
  const ov = st.ov;
  if (!confirm("Go back to the untouched recording?" + (ov.stage !== "imported" ? " It will be split again with the same settings, and repairs will need doing again." : ""))) return;
  try {
    stopPreview(); player.pause();
    await api.post("/api/vinyl/undo", {});
    showBusy(0.02, "Restoring the untouched recording"); watchJob();
  } catch (e) { fail(e); }
};
