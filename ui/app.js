"use strict";
/* Instrumental Studio UI */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = {
  async get(path) { const r = await fetch(path); const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j; },
  async post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j;
  },
};
const fmt = t => { t = Math.max(0, t || 0); const m = Math.floor(t / 60), s = Math.floor(t % 60); return `${m}:${String(s).padStart(2, "0")}`; };
const fmtP = t => { t = Math.max(0, t || 0); const m = Math.floor(t / 60), s = (t % 60).toFixed(1); return `${m}:${s.padStart(4, "0")}`; };
const pct = x => (x > 0 && x < 0.005 ? "<1%" : `${Math.round(100 * x)}%`);
const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
const native = () => window.pywebview && window.pywebview.api;

const st = {
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
    for (const [k, v] of Object.entries(s.models)) sel.add(new Option(v, k));
    sel.value = localStorage.getItem("sepMode") || "4stem";
  }
  $("#engineInfo").textContent = s.demucs
    ? `Separation model installed. Running on: ${s.device}.`
    : "Separation model not installed yet (pip install demucs). Imported stems and the demo still work.";
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
  try { await api.post("/api/load", { id }); await openWorkspace(); } catch (e) { fail(e); }
}

function showWelcome(on) {
  $("#welcome").hidden = !on;
  if (on) refreshState();
}

async function startSong(path) {
  try {
    await api.post("/api/open_song", { path, mode: $("#sepMode").value || "4stem" });
    watchJob();
  } catch (e) { if (e.message === "need_demucs") $("#dlgDemucs").showModal(); else fail(e); }
}

async function upload(file, batch) {
  const r = await fetch("/api/upload", { method: "POST", headers: { "X-Filename": encodeURIComponent(file.name), "X-Batch": batch }, body: file });
  const j = await r.json(); if (!r.ok) throw new Error(j.error); return j;
}

$("#btnOpenSong").onclick = async () => {
  if (!st.state?.demucs) { $("#dlgDemucs").showModal(); return; }
  if (native()) { const p = await native().choose_song(); if (p) startSong(p); return; }
  $("#fileSong").click();
};
$("#fileSong").onchange = async e => {
  const f = e.target.files[0]; if (!f) return;
  showBusy(0.01, "Copying the song into the app");
  try { const j = await upload(f, "song" + Date.now()); startSong(j.path); } catch (err) { hideBusy(); fail(err); }
  e.target.value = "";
};
$("#btnOpenStems").onclick = async () => {
  if (native()) { const p = await native().choose_folder(); if (p) startStems(p); return; }
  $("#fileStems").click();
};
$("#fileStems").onchange = async e => {
  const files = [...e.target.files]; if (!files.length) return;
  const batch = "stems" + Date.now();
  try {
    let folder;
    for (let i = 0; i < files.length; i++) { showBusy(i / files.length * 0.05, `Copying ${files[i].name}`); folder = (await upload(files[i], batch)).folder; }
    const name = files[0].name.replace(/\.[^.]+$/, "").replace(/[ _-]*(vocals?|drums|bass|other|instrumental|no_vocals)$/i, "") || "Imported stems";
    startStems(folder, name);
  } catch (err) { hideBusy(); fail(err); }
  e.target.value = "";
};
async function startStems(folder, name) {
  try { await api.post("/api/open_stems", { folder, name }); watchJob(); } catch (e) { hideBusy(); fail(e); }
}
$("#btnDemo").onclick = async () => { try { await api.post("/api/demo"); watchJob(); } catch (e) { fail(e); } };
$("#btnSongs").onclick = () => { stopPreview(); $("#player").pause(); showWelcome(true); };
$("#sepMode").onchange = e => localStorage.setItem("sepMode", e.target.value);
$("#btnReveal").onclick = () => api.post("/api/reveal", {});

/* ------------------------------------------------------------------ busy */
function showBusy(f, msg) { $("#busy").hidden = false; $("#busyMsg").textContent = msg; $("#busyBar").style.width = pct(f); $("#busyPct").textContent = pct(f); }
function hideBusy() { $("#busy").hidden = true; }
async function watchJob() {
  for (;;) {
    const s = await api.get("/api/state");
    const j = s.job;
    if (j.error) { hideBusy(); fail(new Error(j.error)); return; }
    showBusy(j.progress, j.message || "Working");
    if (!j.running && s.has_project) { hideBusy(); await openWorkspace(); return; }
    if (!j.running && !s.has_project) { hideBusy(); return; }
    await new Promise(r => setTimeout(r, 400));
  }
}

/* ------------------------------------------------------------------ workspace */
async function openWorkspace() {
  const r = await api.get("/api/overview");
  st.rev = r.rev; st.ov = r.overview; st.sel = null; st.scope = null; st.specs = {};
  showWelcome(false);
  $("#workspace").hidden = false; $("#transport").hidden = false; $("#btnExport").disabled = false;
  $("#songName").textContent = st.ov.meta.name || "";
  buildLanes();
  await loadSpectros(st.ov.stems.concat(["vocals"]), ["raw", "after", "damage"]);
  fitView();
  setVersion(st.version, true);
  renderAll();
}

async function loadSpectros(stems, kinds) {
  const { cols, rows } = st.ov.display;
  await Promise.all(stems.flatMap(k => kinds.map(async kind => {
    if (k === "vocals" && kind !== "raw") return;
    const buf = new Uint8Array(await (await fetch(`/api/spectro?stem=${k}&kind=${kind}&r=${st.rev}`)).arrayBuffer());
    (st.specs[k] ||= {})[kind + "Data"] = buf;
  })));
  for (const k of stems) {
    const s = st.specs[k];
    for (const kind of ["raw", "after", "damage"]) s[kind] = s[kind] || null;
    s.raw = paintSpec(s.rawData, cols, rows, null);
    if (k !== "vocals") {
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
  for (const k of st.ov.stems) {
    const lane = document.createElement("div"); lane.className = "lane stem-lane"; lane.dataset.stem = k;
    lane.innerHTML = `
      <div class="lane-head">
        <div class="head-row"><span class="dot" style="background:${st.ov.colors[k] || "#888"}"></span><span class="nm"></span>
          <div class="ms"><button class="m" title="Mute">M</button><button class="s" title="Solo">S</button></div></div>
        <label class="gain">Level <input type="range" min="-24" max="12" step="0.5" value="0"><output>0 dB</output></label>
        <div class="badges"></div>
      </div><canvas></canvas>`;
    lane.querySelector(".nm").textContent = k[0].toUpperCase() + k.slice(1);
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
  $("#ruler").onclick = e => {
    const t = xToTime(e, $("#ruler")); const si = st.ov.sections.findIndex(s => t >= s.start && t < s.end);
    seek(t); if (si >= 0) setScope({ section: si });
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
  const spec = st.specs[k]?.[st.mode]; const fps = st.ov.display.fps;
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
    ctx.fillStyle = s.type === "bleed" ? "#d36a9a" : STATUS_COLOR[s.status] || "#888";
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
  if (st.picking && st.picking.stem === k && st.hoverT != null) {
    const sug = st.ov.suggestions.find(s => s.id === st.picking.id);
    const x0 = tToX(st.hoverT), x1 = tToX(st.hoverT + (sug.end - sug.start));
    ctx.fillStyle = "#58c6d633"; ctx.fillRect(x0, 0, x1 - x0, h);
  }
}

function renderLanes() {
  if (!st.ov) return;
  renderRuler(); renderVox();
  for (const k of st.ov.stems) renderLane(k);
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
  player.addEventListener("loadedmetadata", () => { player.currentTime = Math.min(t, player.duration || t); if (wasPlaying) player.play(); }, { once: true });
}
$$("#versionSeg button").forEach(b => b.onclick = () => setVersion(b.dataset.v));
$("#btnPlay").onclick = togglePlay;
function togglePlay() { stopPreview(); if (player.paused) player.play().catch(fail); else player.pause(); }
player.onplay = () => $("#playIcon").setAttribute("d", "M6 5h4v14H6zM14 5h4v14h-4z");
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
async function fetchBuf(id, mode, solo) {
  const r = await fetch(`/api/preview?id=${id}&mode=${mode}&solo=${solo ? 1 : 0}&r=${st.rev}`);
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
    const solo = st.previewSolo !== false;
    const buf = await fetchBuf(id, mode, solo);
    const src = ctx().createBufferSource(); src.buffer = buf; src.connect(ctx().destination); src.start();
    pv = { key: id + mode, nodes: [src] }; btn?.classList.add("playing");
    src.onended = () => { if (pv && pv.nodes[0] === src) stopPreview(); };
  } catch (e) { fail(e); }
}
async function playAB(id, btn) {
  if (pv && pv.key === id + "ab") { stopPreview(); return; }
  stopPreview(); player.pause();
  try {
    const solo = st.previewSolo !== false;
    const [a, b] = await Promise.all([fetchBuf(id, "before", solo), fetchBuf(id, "after", solo)]);
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
}
$("#btnUndo").onclick = () => edit("/api/undo", {});

function renderAll() {
  $("#btnUndo").disabled = !st.ov.can_undo;
  $("#btnUndo").title = st.ov.can_undo ? `Undo: ${st.ov.undo_label}` : "Nothing to undo";
  renderBadges(); renderLanes(); renderReview(); renderGrid(); renderLevels(); renderSettings();
}

function renderBadges() {
  for (const k of st.ov.stems) {
    const s = st.ov.stem_summary[k]; const el = $(`.stem-lane[data-stem="${k}"] .badges`); if (!el) continue;
    el.innerHTML = s.count ? `<span>Damage <b>${pct(Math.min(1, s.damage))}</b></span><span>Fixable <b>${pct(s.repairability)}</b></span>` : `<span>No damage found</span>`;
  }
}

/* ---------- review list */
const ok = s => s.status === "auto" || s.status === "accepted";
const FILTERS = {
  review: s => s.status === "pending" || (s.type === "bleed" && s.status === "pending"),
  applied: s => ok(s),
  bleed: s => s.type === "bleed",
  hard: s => s.status === "low" || s.status === "nodonor",
  all: () => true,
};
$$("#filters button").forEach(b => b.onclick = () => { st.filter = b.dataset.f; renderReview(); });

function setScope(scope) { st.scope = scope; activateTab("review"); renderReview(); renderRuler(); }

function statusText(s) {
  if (s.type === "bleed") return { pending: "Suggested", accepted: "Cleaned", rejected: "Kept as is" }[s.status] || s.status;
  return { auto: "Applied", accepted: "Applied by you", pending: "Needs review", low: "Weak match", nodonor: "No source", rejected: "Rejected" }[s.status] || s.status;
}

function renderReview() {
  const ov = st.ov, all = ov.suggestions;
  const repairs = all.filter(s => s.type === "repair");
  const applied = repairs.filter(ok).length, pending = all.filter(FILTERS.review).length, hard = repairs.filter(FILTERS.hard).length;
  $("#reviewSummary").innerHTML = `<div><b>${applied}</b><span>repairs applied</span></div><div><b>${pending}</b><span>waiting for you</span></div><div><b>${hard}</b><span>without a good source</span></div>`;
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
    const types = st.filter === "bleed" ? ["bleed"] : ["repair"];
    const mk = (label, action, cls) => { const b = document.createElement("button"); b.className = "small " + (cls || "ghost"); b.textContent = label; b.onclick = () => edit("/api/bulk", { action, ...scopeArgs, types }); bulk.append(b); };
    const what = st.filter === "bleed" ? "cleanups" : "repairs";
    mk(`Apply all ${what}${st.scope ? " here" : ""}`, "accept", "");
    mk(`Reject all${st.scope ? " here" : ""}`, "reject");
    mk("Back to suggestions", "reset");
  }

  const wrap = $("#sugList"); wrap.innerHTML = "";
  if (!list.length) {
    const p = document.createElement("p"); p.className = "help";
    p.textContent = st.filter === "review" ? "Nothing waiting for review. Applied repairs are under Applied; play the song with Repaired selected to hear them." : "Nothing in this list.";
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
  const label = s.type === "bleed" ? `Remove vocal leftover from ${s.stem}` : `Rebuild ${s.stem}`;
  d.innerHTML = `<div class="sug-row"><span class="dot" style="background:${color}"></span><span class="what"></span><span class="when">${fmtP(s.start)}</span><span class="pill ${s.status}">${statusText(s)}</span></div>`;
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
  } else {
    facts.push(["Leftover vocal", `${pct(s.conf)} certain; the part removed sits ${Math.abs(s.removed_db ?? 0).toFixed(0)} dB below the ${s.stem}`]);
  }
  if (s.note && !s.manual_donor && s.type === "repair") facts.push(["Note", esc(s.note)]);
  body.innerHTML = `<div class="facts">${facts.map(([a, b]) => `<span>${a}</span><b>${b}</b>`).join("")}</div>`;

  if (s.status !== "nodonor") {
    const pvRow = document.createElement("div"); pvRow.className = "preview";
    const modes = [["before", "Before"], ["after", "After"], ["patch", s.type === "bleed" ? "Only what's removed" : "Only what's added"]];
    if (s.type === "repair" && s.donors?.length) modes.push(["donor", "Source"]);
    modes.push(["original", "Original song"]);
    for (const [m, label] of modes) {
      const b = document.createElement("button"); b.className = "small"; b.textContent = "▶ " + label;
      b.onclick = () => playPreview(s.id, m, b); pvRow.append(b);
    }
    body.append(pvRow);
    const ab = document.createElement("div"); ab.className = "ab";
    ab.innerHTML = `<button class="small abplay">Loop A/B</button><div class="seg small"><button data-ab="before">A Before</button><button data-ab="after">B After</button></div>
      <div class="seg small" title="Hear just this instrument, or the whole instrumental"><button data-solo="1">Solo</button><button data-solo="0">In mix</button></div>`;
    ab.querySelector(".abplay").onclick = e => playAB(s.id, e.currentTarget);
    ab.querySelectorAll("[data-ab]").forEach(b => { b.classList.toggle("on", b.dataset.ab === (st.abSide || "after")); b.onclick = () => setAB(b.dataset.ab); });
    ab.querySelectorAll("[data-solo]").forEach(b => { b.classList.toggle("on", (b.dataset.solo === "1") === (st.previewSolo !== false)); b.onclick = () => { st.previewSolo = b.dataset.solo === "1"; stopPreview(); renderReview(); }; });
    body.append(ab);

    const strength = document.createElement("div"); strength.className = "row";
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
    a.textContent = applied ? (s.type === "bleed" ? "Cleaned" : "Applied") : (s.type === "bleed" ? "Clean it" : "Apply");
    a.disabled = applied; a.onclick = () => edit("/api/status", { ids: [s.id], status: "accepted" });
    const r = document.createElement("button"); r.className = "ghost"; r.textContent = s.status === "rejected" ? "Rejected" : (s.type === "bleed" ? "Keep as is" : "Reject");
    r.disabled = s.status === "rejected"; r.onclick = () => edit("/api/status", { ids: [s.id], status: "rejected" });
    dec.append(a, r);
  }
  const go = document.createElement("button"); go.className = "ghost"; go.textContent = "Show on timeline"; go.onclick = () => focusOn(s);
  dec.append(go);
  body.append(dec);
  d.append(body);
  return d;
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
function renderGrid() {
  const ov = st.ov, w = $("#gridWrap");
  const STATE = { clean: "No vocals", undamaged: "Undamaged", repaired: "Repaired", repairable: "Repairable", partial: "Partly", hard: "Hard to fix" };
  let html = `<table class="grid"><thead><tr><th></th>${ov.stems.map(k => `<th><span class="dot" style="display:inline-block;background:${ov.colors[k]}"></span> ${esc(k)}</th>`).join("")}</tr></thead><tbody>`;
  html += `<tr><th class="sec">Whole song</th>${ov.stems.map(k => { const s = ov.stem_summary[k];
    const state = !s.count ? "undamaged" : s.repairability >= 0.55 ? "repairable" : s.repairability >= 0.3 ? "partial" : "hard";
    return `<td class="st-${state}" data-stem="${k}"><b>${s.count ? pct(Math.min(1, s.damage)) + " hit" : "Clean"}</b>${s.count ? pct(s.repairability) + " fixable" : ""}</td>`; }).join("")}</tr>`;
  ov.sections.forEach((sec, i) => {
    html += `<tr><th class="sec" title="Double-click to rename" data-sec="${i}"><span class="dot" style="display:inline-block;background:${sec.color}"></span> ${esc(sec.name)}</th>`;
    for (const k of ov.stems) {
      const g = ov.grid[`${i}:${k}`] || { state: "clean" };
      const body = g.count ? `<b>${pct(Math.min(1, g.damage))} hit</b>${pct(g.repairability)} fixable` : `<b>${STATE[g.state]}</b>`;
      html += `<td class="st-${g.state}" data-sec="${i}" data-stem="${k}" title="${STATE[g.state]}: ${g.count} spot(s), ${pct(g.fixed || 0)} repaired">${body}</td>`;
    }
    html += `</tr>`;
  });
  html += `</tbody></table><div class="grid-key"><span style="color:var(--ok)">Good source found</span><span style="color:#e3c46f">Partial match</span><span style="color:var(--damage)">No good source</span><span style="color:var(--repair)">Repaired</span></div>`;
  w.innerHTML = html;
  w.querySelectorAll("td").forEach(td => td.onclick = () => {
    const sec = td.dataset.sec != null ? +td.dataset.sec : null; st.filter = "all"; setScope({ section: sec, stem: td.dataset.stem });
  });
  w.querySelectorAll("th[data-sec]").forEach(th => th.ondblclick = async () => {
    const i = +th.dataset.sec; const name = prompt("Section name", ov.sections[i].name);
    if (name) edit("/api/rename_section", { index: i, name });
  });
}

/* ---------- levels */
function renderLevels() {
  const ov = st.ov, w = $("#levelsWrap"), sg = ov.mixer.section_gain || {};
  const cols = ov.stems.concat(["vocals"]);
  let html = `<table class="levels"><thead><tr><th></th>${cols.map(k => `<th>${esc(k)}</th>`).join("")}</tr></thead><tbody>`;
  ov.sections.forEach((sec, i) => {
    html += `<tr><th>${esc(sec.name)}</th>`;
    for (const k of cols) {
      const lv = ov.levels[`${i}:${k}`]; const m = clamp((lv + 60) / 60, 0, 1);
      const g = sg[`${i}:${k}`] || 0;
      html += `<td><div class="lvl"><span class="dim small">${lv < -90 ? "silent" : lv.toFixed(1) + " dB"}</span><div class="m"><i style="width:${pct(m)};background:${ov.colors[k] || "#888"}"></i></div>`
        + (k === "vocals" ? "" : `<input type="number" step="0.5" min="-24" max="12" value="${g}" class="${g ? "changed" : ""}" data-key="${i}:${k}" title="Boost or cut in dB">`) + `</div></td>`;
    }
    html += `</tr>`;
  });
  html += `</tbody></table><div class="row" style="margin-top:12px"><button class="ghost small" id="lvlReset">Reset section levels</button></div>`;
  w.innerHTML = html;
  w.querySelectorAll("input").forEach(inp => inp.onchange = () => {
    const v = clamp(+inp.value || 0, -24, 12); ov.mixer.section_gain[inp.dataset.key] = v; inp.classList.toggle("changed", !!v); pushMixer();
  });
  $("#lvlReset").onclick = () => { ov.mixer.section_gain = {}; pushMixer(); };
}

/* ---------- settings */
function renderSettings() {
  const s = st.ov.meta.settings;
  const a = $("#autoConf"), r = $("#reviewConf");
  if (document.activeElement !== a) a.value = s.auto_conf; if (document.activeElement !== r) r.value = s.review_conf;
  $("#autoConfVal").textContent = pct(+a.value); $("#reviewConfVal").textContent = pct(+r.value);
}
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
  const what = ["mix", "stems", "raw", "vocals", "report"].filter(n => f.elements[n].checked);
  const format = f.elements.fmt.value;
  showBusy(0.3, "Writing files");
  try {
    const r = await api.post("/api/export", { what, format, reveal: true });
    hideBusy(); toast(`Saved ${r.files.length} file(s) to ${r.folder}`, { ms: 9000, action: ["Show folder", () => api.post("/api/reveal", { path: r.folder })] });
  } catch (e) { hideBusy(); fail(e); }
});

/* ---------- keyboard */
document.addEventListener("keydown", e => {
  if (!st.ov || $("#workspace").hidden || e.target.matches("input, select, textarea")) return;
  if (e.key === " ") { e.preventDefault(); togglePlay(); }
  else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); if (st.ov.can_undo) edit("/api/undo", {}); }
  else if (e.key === "Escape") { if (st.picking) endPick(); else stopPreview(); }
  else if (e.key === "j" || e.key === "k") {
    const items = $$("#sugList .sug").map(x => x.dataset.id); if (!items.length) return;
    const i = items.indexOf(st.sel); const n = e.key === "j" ? Math.min(items.length - 1, i + 1) : Math.max(0, i - 1);
    select(items[i < 0 ? 0 : n]);
  } else if (st.sel && (e.key === "a" || e.key === "b")) setAB(e.key === "a" ? "before" : "after");
  else if (st.sel && e.key === "Enter") edit("/api/status", { ids: [st.sel], status: "accepted" });
  else if (st.sel && (e.key === "Backspace" || e.key === "Delete")) edit("/api/status", { ids: [st.sel], status: "rejected" });
});


/* ------------------------------------------------------------------ updates */
const upd = { polling: false, announced: false, autoRestarted: false };
function renderUpdate(s) {
  if (!s) return;
  const u = s.update, info = u.info, prefs = s.update_prefs;
  $("#verNow").textContent = `Version ${s.version}`;
  $("#welcomeVer").textContent = `Instrumental Studio ${s.version}`;
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

/* ------------------------------------------------------------------ boot */
(async function boot() {
  try {
    const s = await refreshState();
    renderUpdate(s); pollUpdate(60000);
    if (s.job.running) { watchJob(); return; }
    if (s.has_project) { await openWorkspace(); return; }
    showWelcome(true);
  } catch (e) { fail(e); }
})();
