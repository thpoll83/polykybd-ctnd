/* SPDX-License-Identifier: GPL-2.0-only */
'use strict';

const socket    = io();
const logEl     = document.getElementById('log-output');
const statusEl  = document.getElementById('status-text');
const clockEl   = document.getElementById('clock');
const actionBtns = [
  'btn-flash-left', 'btn-flash-right', 'btn-run', 'btn-reregister', 'btn-restart',
  'btn-usb-left',     'btn-bootsel-left',  'btn-reset-left',
  'btn-usb-right',    'btn-bootsel-right', 'btn-reset-right',
  'btn-hand-left',    'btn-hand-right',
  'btn-perf',         'btn-measure-phase',
].map(id => document.getElementById(id));

/* ── Clock ── */
function tick() { clockEl.textContent = new Date().toLocaleTimeString(); }
tick();
setInterval(tick, 1000);

/* ── Socket events ── */
socket.on('connect',    () => appendLog('[ws] connected'));
socket.on('disconnect', () => appendLog('[ws] disconnected'));

socket.on('status', ({ value }) => {
  document.body.dataset.status = value;
  statusEl.textContent = value;
  const busy = value !== 'idle' && value !== 'error';
  actionBtns.forEach(b => b.disabled = busy);
  if (!busy) resultsEnded();
});

socket.on('log', ({ msg }) => appendLog(msg));

socket.on('ci_status', ({ running, url }) => {
  const badge = document.getElementById('ci-badge');
  badge.dataset.ci  = running ? 'running' : 'idle';
  badge.textContent = running ? 'CI ▶' : 'CI ✓';
  badge.title       = running ? 'CI running — do not flash!' : 'CI idle';
  badge.style.cursor = url ? 'pointer' : 'default';
  badge.onclick = url ? () => window.open(url, '_blank') : null;
});

const RUNNER_BADGE = {
  unknown: ['RUNNER',   'Runner status unknown — tap to run diagnostics'],
  online:  ['RUNNER ✓', 'Runner online & idle — tap to run diagnostics'],
  busy:    ['RUNNER ▶', 'Runner online, busy with a job — tap to run diagnostics'],
  offline: ['RUNNER ✕', 'Runner registered but OFFLINE — tap to run diagnostics'],
  missing: ['RUNNER !', 'No runner has the required labels — tap to run diagnostics'],
  noauth:  ['RUNNER ⚿', 'Token lacks runner-admin scope — tap to run diagnostics'],
};

socket.on('runner_status', ({ status }) => {
  const badge = document.getElementById('rn-badge');
  const [text, title] = RUNNER_BADGE[status] || RUNNER_BADGE.unknown;
  badge.dataset.rn  = status;
  badge.textContent = text;
  badge.title       = title;
});

socket.on('update_status', ({ state, behind, branch }) => {
  const badge = document.getElementById('up-badge');
  const br = branch || 'main';
  badge.dataset.up = state;
  if (state === 'current') {
    badge.textContent = 'UP ✓';
    badge.title = `Up to date with origin/${br} — tap to re-check / update`;
  } else if (state === 'behind') {
    badge.textContent = `UP ↓${behind}`;
    badge.title = `${behind} commit(s) behind origin/${br} — tap to update`;
  } else if (state === 'updating') {
    badge.textContent = 'UP …';
    badge.title = 'Updating — the station will restart and reconnect';
  } else {
    badge.textContent = 'UPDATE';
    badge.title = 'Update status unknown — tap to check / update';
  }
});

// Per-test status marks — a plain-text simplification of the backend's GitHub
// summary emoji (test_runner.py _STATUS_MARK): same five statuses, terminal-
// friendly glyphs (✓/✗ here vs ✅/❌ there). Records carry a `status`
// (pass/fail/skip/xfail/xpass); fall back to the legacy `passed` bool for
// records produced before the status field existed.
const TEST_MARK = { pass: '✓', fail: '✗', skip: '⏭', xfail: '🟡', xpass: '❗' };

socket.on('test_result', result => {
  renderResults(result);
  const icon = result.passed ? '✓ PASS' : '✗ FAIL';
  appendLog(`\n── ${icon} ──`);
  (result.results || []).forEach(r => {
    const status = r.status || (r.passed ? 'pass' : 'fail');
    const mark = TEST_MARK[status] || '?';
    const note = r.reason ? '  (' + r.reason + ')' : (r.error ? '  (' + r.error + ')' : '');
    appendLog(`  ${mark} ${r.name}${note}`);
  });
  appendLog('');
});

/* One row per test in the Test tab, with a status chip, so the outcome is
   visible without scrolling the log. */
function renderResults(result) {
  _resultsPending = false;
  const box = document.getElementById('results');
  box.replaceChildren();
  const head = document.createElement('div');
  head.className = 'result-head ' + (result.passed ? 'st-pass' : 'st-fail');
  head.textContent = result.passed ? 'PASS' : 'FAIL';
  box.appendChild(head);
  (result.results || []).forEach(r => {
    const status = r.status || (r.passed ? 'pass' : 'fail');
    const row = document.createElement('div');
    row.className = 'result-row';
    const chip = document.createElement('span');
    chip.className = 'chip st-' + status;
    chip.textContent = status.toUpperCase();
    const name = document.createElement('span');
    name.className = 'result-name';
    name.textContent = r.name;
    row.append(chip, name);
    const why = r.reason || r.error || r.info;
    if (why) {
      const note = document.createElement('span');
      note.className = 'result-note';
      note.textContent = why;
      row.appendChild(note);
    }
    box.appendChild(row);
  });
}

// Set while a test run's results are expected. A run that ends without a
// test_result (an exception, a lost connection) would otherwise leave the
// "Running…" text up for good, so the status handler replaces it.
let _resultsPending = false;

function resultsEnded() {
  if (!_resultsPending) return;
  _resultsPending = false;
  const box = document.getElementById('results');
  box.replaceChildren();
  const p = document.createElement('p');
  p.className = 'placeholder';
  p.textContent = 'The run ended without a result. The Log tab shows why.';
  box.appendChild(p);
}

function resultsRunning(what) {
  _resultsPending = true;
  const box = document.getElementById('results');
  box.replaceChildren();
  const p = document.createElement('p');
  p.className = 'placeholder';
  p.textContent = what + ' — the newest log line is shown at the bottom; the Log tab has all of it.';
  box.appendChild(p);
}

/* ── Primary actions ── */
function flashSide(side) {
  const sel = document.getElementById(side === 'left' ? 'left-fw' : 'right-fw');
  if (!sel.value) { appendLog(`[ui] select a firmware file for ${side} first`); return; }
  socket.emit('flash', { side, uf2: sel.value });
}

/* The Extended toggle adds the slow tier (animation, idle engage + Eden
   screensaver, split-link soak, reboot power cycle) — roughly a minute more, for
   a release or a change big enough to want it. Off by default, like CI. */
function runTests() {
  const left  = document.getElementById('left-fw').value;
  const right = document.getElementById('right-fw').value;
  if (!left || !right) { appendLog('[ui] select both left and right firmware files first'); return; }
  const extended = document.getElementById('extended-tests').checked;
  appendLog(extended ? '[ui] running the EXTENDED suite (slow checks included)…'
                     : '[ui] running the default suite…');
  resultsRunning(extended ? 'Running the extended suite' : 'Running the default suite');
  socket.emit('run_tests', { left_uf2: left, right_uf2: right, extended });
}

/* Performance measurement. Needs a PROFILING firmware pair (built with
   -e POLYKYBD_LOOP_PROFILE=yes); on a normal build the profiler command NACKs
   and the run says so rather than reporting zeros. */
function runPerf() {
  const left  = document.getElementById('left-fw').value;
  const right = document.getElementById('right-fw').value;
  if (!left || !right) { appendLog('[ui] select both left and right firmware files first'); return; }
  appendLog('[ui] running performance measurement (needs a POLYKYBD_LOOP_PROFILE build)…');
  socket.emit('run_perf', { left_uf2: left, right_uf2: right });
}

function runDiagnostics() {
  appendLog('[ui] running runner diagnostics…');
  socket.emit('run_diagnostics');
}

/* Restart the runner service. Non-destructive (just bounces it), so no confirm.
   Fixes the common "configured but wedged" case without a reconfigure. */
function restartRunner() {
  appendLog('[ui] restarting runner service…');
  socket.emit('restart_runner');
}

/* Re-register the GitHub Actions runner. Disruptive (stops → reconfigures →
   restarts the runner), so require a two-tap confirm before firing. */
let _reregTimer = null;
function reregisterRunner() {
  const btn = document.getElementById('btn-reregister');
  if (btn.dataset.armed !== 'true') {
    btn.dataset.label = btn.textContent;          // remember the real label
    btn.dataset.armed = 'true';
    btn.textContent = '⚠ Tap to confirm';
    _reregTimer = setTimeout(() => {
      btn.dataset.armed = 'false';
      btn.textContent = btn.dataset.label;
    }, 3000);
    return;
  }
  clearTimeout(_reregTimer);
  btn.dataset.armed = 'false';
  btn.textContent = btn.dataset.label;
  appendLog('[ui] re-registering runner (stops, reconfigures, restarts it)…');
  socket.emit('reregister_runner');
}

/* Update the station from the tracked branch. Disruptive when behind (it
   restarts the service, dropping this socket), so require a two-tap confirm —
   same pattern as Re-register. A re-check when already up to date is harmless,
   but one confirm flow keeps it predictable. */
let _updateTimer = null;
function updateStation() {
  const badge = document.getElementById('up-badge');
  if (badge.dataset.armed !== 'true') {
    badge.dataset.label = badge.textContent;
    badge.dataset.prevTitle = badge.title;
    badge.dataset.armed = 'true';
    badge.textContent = '⚠ tap';
    badge.title = 'Tap again to pull the tracked branch and restart the station';
    _updateTimer = setTimeout(() => {
      badge.dataset.armed = 'false';
      badge.textContent = badge.dataset.label;
      badge.title = badge.dataset.prevTitle || badge.title;
    }, 3000);
    return;
  }
  clearTimeout(_updateTimer);
  badge.dataset.armed = 'false';
  badge.textContent = badge.dataset.label;
  badge.title = badge.dataset.prevTitle || badge.title;
  appendLog('[ui] updating station from tracked branch…');
  socket.emit('update_now');
}

function clearLog() { logEl.textContent = ''; }

function copyLog() {
  const text = logEl.textContent;
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById('btn-copy');
    const prev = btn.textContent;
    btn.textContent = '✓ Copied';
    setTimeout(() => { btn.textContent = prev; }, 1500);
  });
}

/* ── Utility actions ── */
socket.on('usb_state', state => {
  ['left', 'right'].forEach(side => updateUsbBtn(side, state[side]));
});

socket.on('bootsel_state', state => {
  ['left', 'right'].forEach(side => updateBootselBtn(side, state[side]));
});

socket.on('run_state', state => {
  ['left', 'right'].forEach(side => updateRunBtn(side, state[side]));
});

function updateBootselBtn(side, asserted) {
  const btn = document.getElementById(`btn-bootsel-${side}`);
  const prefix = side === 'left' ? 'L' : 'R';
  btn.dataset.state = asserted ? 'asserted' : 'released';
  btn.textContent   = asserted ? `${prefix}: BOOT ●` : `${prefix}: BOOTSEL`;
}

function updateRunBtn(side, asserted) {
  const btn = document.getElementById(`btn-reset-${side}`);
  const prefix = side === 'left' ? 'L' : 'R';
  btn.dataset.state = asserted ? 'asserted' : 'released';
  btn.textContent   = asserted ? `${prefix}: RST ●` : `${prefix}: Reset`;
}

function toggleBootsel(side) {
  const btn      = document.getElementById(`btn-bootsel-${side}`);
  const asserted = btn.dataset.state !== 'asserted';
  socket.emit('bootsel', { side, asserted });
}

function toggleRun(side) {
  const btn      = document.getElementById(`btn-reset-${side}`);
  const asserted = btn.dataset.state !== 'asserted';
  socket.emit('reset_board', { side, asserted });
}

function updateUsbBtn(side, state) {
  const btn = document.getElementById(`btn-usb-${side}`);
  const prefix = side === 'left' ? 'L' : 'R';
  if (state === true)       { btn.dataset.state = 'on';      btn.textContent = `${prefix}: USB ON`;  }
  else if (state === false) { btn.dataset.state = 'off';     btn.textContent = `${prefix}: USB OFF`; }
  else                      { btn.dataset.state = 'unknown'; btn.textContent = `${prefix}: USB ?`;   }
}

function toggleUsb(side) {
  const btn = document.getElementById(`btn-usb-${side}`);
  const on = btn.dataset.state !== 'on';  // unknown → treat as off → turn on
  socket.emit('usb_power', { side, on });
}

/* Set the keyboard's handedness (EE_HANDS) so a half displays the correct side.
   Targets the USB/master half; it syncs the opposite to the slave and both
   reboot to apply (~10 s). On the rig the master is the left board, so
   "Set USB = Left" fixes a left half that shows up as "right". */
function setHandedness(masterIsLeft) {
  appendLog(`[ui] setting handedness: USB/master half = ${masterIsLeft ? 'LEFT' : 'RIGHT'} — keyboard will reboot…`);
  socket.emit('set_handedness', { master_is_left: masterIsLeft });
}


let _ciJobsTimer = null;

/* ── Tabs ──
   One section per mode. The last tab is remembered per browser (a reload of
   the kiosk returns to it); storage can throw in a locked-down profile, so
   every access is guarded. Entering a tab starts what it polls, leaving it
   stops that again. */
const TABS = ['test', 'hardware', 'camera', 'runner', 'log'];
let _currentTab = null;

function showTab(name) {
  if (!TABS.includes(name)) name = 'test';
  if (name === _currentTab) return;
  if (_currentTab === 'camera') closeCamera();
  if (_currentTab === 'runner') { clearInterval(_ciJobsTimer); _ciJobsTimer = null; }
  TABS.forEach(t => {
    document.getElementById('tab-' + t).hidden = t !== name;
    document.querySelector(`.tab[data-tab="${t}"]`).classList.toggle('active', t === name);
  });
  _currentTab = name;
  try { localStorage.setItem('ctnd-tab', name); } catch (e) { /* no storage */ }
  if (name === 'camera') openCamera();
  if (name === 'runner') {
    fetchCiJobs();
    _ciJobsTimer = setInterval(fetchCiJobs, 30000);
  }
  if (name === 'log') logEl.parentElement.scrollTop = logEl.parentElement.scrollHeight;
}

function fetchCiJobs() {
  fetch('/ci-jobs')
    .then(r => r.json())
    .then(renderCiJobs)
    .catch(() => {
      document.getElementById('ci-jobs-list').innerHTML =
        '<p class="no-jobs">Could not load CI jobs.</p>';
    });
}

function renderCiJobs(jobs) {
  const list = document.getElementById('ci-jobs-list');
  list.replaceChildren();
  if (!jobs.length) {
    const msg = document.createElement('p');
    msg.className = 'no-jobs';
    msg.textContent = 'No scheduled or running CI jobs.';
    list.appendChild(msg);
    return;
  }
  const frag = document.createDocumentFragment();
  jobs.forEach(j => {
    const running = j.status === 'in_progress';

    const card = document.createElement('div');
    card.className = `ci-job ${running ? 'ci-job-running' : 'ci-job-queued'}`;

    const iconEl = document.createElement('span');
    iconEl.className = 'ci-job-icon';
    iconEl.textContent = running ? '▶' : '⏳';

    const bodyEl = document.createElement('span');
    bodyEl.className = 'ci-job-body';

    const nameEl = document.createElement('span');
    nameEl.className = 'ci-job-name';
    nameEl.textContent = `#${j.run_number} ${j.name ?? ''}`;

    const metaEl = document.createElement('span');
    metaEl.className = 'ci-job-meta';
    metaEl.textContent = `${j.event ?? ''} · ${j.head_branch ?? ''}`;

    const labelEl = document.createElement('span');
    labelEl.className = 'ci-job-label';
    labelEl.textContent = running ? 'Running' : 'Queued';

    bodyEl.append(nameEl, metaEl);
    card.append(iconEl, bodyEl, labelEl);
    frag.appendChild(card);
  });
  list.appendChild(frag);
}

// ── Camera view ──────────────────────────────────────────────────────────────
// Shows the rig camera with the dashed split line the RGB phase check uses, so
// the orientation (rotation, flips, split_x, master_on_left in config.yaml) can
// be checked by eye. The server refuses a snapshot while the rig is busy.
let _cameraTimer = null;
let _cameraGen   = 0;      // bumped on open and close; late responses of an older view are dropped
let _cameraInfo  = null;   // the /camera/info reply of the open view
let _cameraText  = '';     // the orientation line, restored after a snapshot error clears

// Drop the last snapshot and hide the overlay, so a reopened view never shows
// an old image next to "No camera".
function clearCameraImage() {
  const img = document.getElementById('camera-img');
  if (img.src) URL.revokeObjectURL(img.src);
  img.removeAttribute('src');
  placeCameraOverlay();   // _cameraInfo is null here, so this hides the line and labels
}

function openCamera() {
  const gen = ++_cameraGen;
  _cameraInfo = null;
  clearCameraImage();
  loadPhaseLast();
  _sharpPeak = 0;
  document.getElementById('camera-sharp').textContent = '';
  const msg = document.getElementById('camera-msg');
  msg.textContent = 'Loading camera…';
  fetch('/camera/info')
    .then(r => r.json())
    .then(info => {
      if (gen !== _cameraGen) return;   // the view was closed or reopened meanwhile
      if (!info.available) { msg.textContent = 'No camera: ' + info.detail; return; }
      _cameraInfo = info;
      const master = 'MASTER (USB)', slave = 'SLAVE';
      document.getElementById('camera-tag-left').textContent  = info.master_on_left ? master : slave;
      document.getElementById('camera-tag-right').textContent = info.master_on_left ? slave : master;
      _cameraText = `rotation ${info.rotation}°, hflip ${info.hflip}, vflip ${info.vflip}, ` +
                    (info.roi ? `roi ${info.roi}, ` : '') +
                    `split_x ${info.split_x} — the dashed line must fall between the halves`;
      msg.textContent = _cameraText;
      placeCameraOverlay();
      refreshCamera();
      cameraLive();
    })
    .catch(e => { if (gen === _cameraGen) msg.textContent = 'Camera info failed: ' + e; });
}

// Put the split line and the labels on the PAINTED image. object-fit: contain
// can leave bars at the sides or top, so split_x is a fraction of the painted
// width, not of the img box.
function placeCameraOverlay() {
  const img = document.getElementById('camera-img');
  const split = document.getElementById('camera-split');
  const tagL = document.getElementById('camera-tag-left');
  const tagR = document.getElementById('camera-tag-right');
  const show = !!(_cameraInfo && img.naturalWidth && img.clientWidth);
  [split, tagL, tagR].forEach(el => { el.hidden = !show; });
  if (!show) return;
  const scale = Math.min(img.clientWidth / img.naturalWidth, img.clientHeight / img.naturalHeight);
  const pw = img.naturalWidth * scale, ph = img.naturalHeight * scale;
  const x0 = img.offsetLeft + (img.clientWidth - pw) / 2;
  const y0 = img.offsetTop + (img.clientHeight - ph) / 2;
  split.style.left = (x0 + _cameraInfo.split_x * pw) + 'px';
  split.style.top = y0 + 'px';
  split.style.height = ph + 'px';
  split.style.bottom = 'auto';
  tagL.style.left = (x0 + 6) + 'px';
  tagL.style.top = (y0 + 6) + 'px';
  tagR.style.left = (x0 + pw - 6 - tagR.offsetWidth) + 'px';
  tagR.style.right = 'auto';
  tagR.style.top = (y0 + 6) + 'px';
}

function refreshCamera() {
  const gen = _cameraGen;
  const img = document.getElementById('camera-img');
  const msg = document.getElementById('camera-msg');
  fetch('/camera/snapshot.jpg?t=' + Date.now())
    .then(r => r.ok ? r.blob() : r.text().then(t => { throw new Error(t); }))
    .then(b => {
      if (gen !== _cameraGen) return;
      if (img.src) URL.revokeObjectURL(img.src);
      img.src = URL.createObjectURL(b);
      if (_cameraText) msg.textContent = _cameraText;   // clear an earlier "busy" error
    })
    .catch(e => { if (gen === _cameraGen) msg.textContent = 'Snapshot: ' + e.message; });
}

function cameraLive() {
  if (_cameraTimer) { clearInterval(_cameraTimer); _cameraTimer = null; }
  if (_cameraInfo && document.getElementById('camera-live').checked) {
    _cameraTimer = setInterval(refreshCamera, 2000);
  }
}

function closeCamera() {
  _cameraGen++;
  _cameraInfo = null;
  if (_cameraTimer) { clearInterval(_cameraTimer); _cameraTimer = null; }
  clearCameraImage();
}

/* ── RGB phase: the last measurement and "Measure phase now" ──
   The CI runner and the UI both write the last result to var/ on the rig;
   /camera/last serves it. The chart shows the two brightness traces the
   offset was computed from. */
function loadPhaseLast() {
  fetch('/camera/last')
    .then(r => r.json())
    .then(renderPhase)
    .catch(() => {});
}

socket.on('phase_result', renderPhase);

function renderPhase(d) {
  const box = document.getElementById('phase-last');
  const chart = document.getElementById('phase-chart');
  const legend = document.getElementById('phase-legend');
  box.replaceChildren();
  if (!d || !d.status) {
    const p = document.createElement('p');
    p.className = 'placeholder';
    p.textContent = 'No measurement yet. The next HIL run records one, or use Measure phase now.';
    box.appendChild(p);
    chart.toggleAttribute('hidden', true); legend.hidden = true;   // an SVG element has no .hidden property
    return;
  }
  const head = document.createElement('div');
  head.className = 'phase-head';
  const chip = document.createElement('span');
  chip.className = 'chip st-' + d.status;
  chip.textContent = d.status.toUpperCase();
  const when = document.createElement('span');
  when.className = 'phase-when';
  const t = d.time ? new Date(d.time).toLocaleString() : '';
  when.textContent = `${t} · ${d.source === 'ci' ? 'CI run' : 'measured here'}`;
  head.append(chip, when);
  box.appendChild(head);
  if (d.offset_ms !== undefined) {
    const big = document.createElement('div');
    big.className = 'phase-offset';
    big.textContent = `${d.offset_ms > 0 ? '+' : ''}${d.offset_ms} ms`;
    box.appendChild(big);
  }
  const detail = document.createElement('p');
  detail.className = 'phase-detail';
  detail.textContent = d.reason || d.info || '';
  box.appendChild(detail);

  const tr = d.traces;
  if (tr && tr.t_ms && tr.t_ms.length > 1) {
    const all = tr.master.concat(tr.slave);
    const lo = Math.min(...all), hi = Math.max(...all), span = (hi - lo) || 1;
    const tmax = tr.t_ms[tr.t_ms.length - 1] || 1;
    const pts = vals => vals.map((v, i) =>
      `${(tr.t_ms[i] / tmax * 300).toFixed(1)},${(86 - (v - lo) / span * 82).toFixed(1)}`).join(' ');
    document.getElementById('phase-line-master').setAttribute('points', pts(tr.master));
    document.getElementById('phase-line-slave').setAttribute('points', pts(tr.slave));
    chart.toggleAttribute('hidden', false); legend.hidden = false;
  } else {
    chart.toggleAttribute('hidden', true); legend.hidden = true;   // an SVG element has no .hidden property
  }
}

function measurePhase() {
  const sel = document.getElementById('phase-bin');
  if (!sel.value) { appendLog('[ui] select the .bin of the running build first'); return; }
  appendLog('[ui] measuring the RGB phase (HID stage + camera, no apply)…');
  socket.emit('measure_phase', { bin: sel.value });
}

/* ── Focus aid ──
   Sharpness of each snapshot as the variance of the 4-neighbour Laplacian of
   its grey values, computed here on a canvas (the server has no image
   library). A sharper image has stronger edges and a larger value. The
   number only compares snapshots of the same scene and lighting, so it shows
   the peak since the tab opened next to the current value. */
let _sharpPeak = 0;
const _sharpCanvas = document.createElement('canvas');

function sharpness(img) {
  const w = Math.min(img.naturalWidth, 640);
  const h = Math.round(img.naturalHeight * w / img.naturalWidth);
  if (w < 3 || h < 3) return 0;
  _sharpCanvas.width = w;
  _sharpCanvas.height = h;
  const ctx = _sharpCanvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, w, h);
  const px = ctx.getImageData(0, 0, w, h).data;
  const g = new Float32Array(w * h);
  for (let i = 0, j = 0; i < g.length; i++, j += 4) {
    g[i] = 0.299 * px[j] + 0.587 * px[j + 1] + 0.114 * px[j + 2];
  }
  let sum = 0, sum2 = 0, n = 0;
  for (let y = 1; y < h - 1; y++) {
    for (let x = 1; x < w - 1; x++) {
      const i = y * w + x;
      const lap = g[i - 1] + g[i + 1] + g[i - w] + g[i + w] - 4 * g[i];
      sum += lap; sum2 += lap * lap; n++;
    }
  }
  const mean = sum / n;
  return sum2 / n - mean * mean;
}

function showSharpness() {
  const img = document.getElementById('camera-img');
  const el = document.getElementById('camera-sharp');
  if (!_cameraInfo || !img.naturalWidth) { el.textContent = ''; return; }
  const v = sharpness(img);
  _sharpPeak = Math.max(_sharpPeak, v);
  el.textContent = `Sharpness ${v.toFixed(0)} (peak ${_sharpPeak.toFixed(0)} since this tab opened)`;
  el.classList.toggle('at-peak', v >= 0.97 * _sharpPeak);
}

document.getElementById('camera-img').addEventListener('load', placeCameraOverlay);
document.getElementById('camera-img').addEventListener('load', showSharpness);
window.addEventListener('resize', placeCameraOverlay);

function quitApp() { window.close(); }

function refreshFirmware() {
  fetch('/firmware')
    .then(r => r.json())
    .then(files => {
      const bin = document.getElementById('phase-bin');
      const prevBin = bin.value;
      bin.innerHTML = '<option value="">— .bin of the running build —</option>';
      files.filter(f => f.toLowerCase().endsWith('.bin')).forEach(f => {
        const opt = document.createElement('option');
        opt.value = opt.textContent = f;
        if (f === prevBin) opt.selected = true;
        bin.appendChild(opt);
      });
      ['left-fw', 'right-fw'].forEach(id => {
        const sel  = document.getElementById(id);
        const prev = sel.value;
        sel.innerHTML = '<option value="">— select firmware —</option>';
        files.forEach(f => {
          const opt = document.createElement('option');
          opt.value = opt.textContent = f;
          if (f === prev) opt.selected = true;
          sel.appendChild(opt);
        });
      });
    })
    .catch(() => appendLog('[ui] could not load firmware list'));
}

/* ── Idle screen-blank "touch to wake" catch layer ──
   Mirrors the kiosk's X11 DPMS blanking (`xset dpms 0 0 300` in
   polykybd-kiosk.service). After the same idle period we raise a full-screen
   layer over the UI; because it sits on top, the touch that wakes the panel
   lands on it — not on a Flash/Run button underneath — and is consumed here.
   The layer rises ~1 s before DPMS cuts the backlight so it is guaranteed up
   first. Keep WAKE_AFTER_MS in sync with the `off` value in the kiosk unit. */
const WAKE_AFTER_MS = 299000;                 // ≈ DPMS off=300 s, minus 1 s margin
const wakeLayer = document.getElementById('wake-overlay');
let _idleTimer = null;

function armIdle() {
  clearTimeout(_idleTimer);
  _idleTimer = setTimeout(() => { wakeLayer.hidden = false; }, WAKE_AFTER_MS);
}

/* The wake tap targets the overlay (it's topmost and covers the controls), so
   the click never reaches a button — we just dismiss the layer and re-arm. */
wakeLayer.addEventListener('click', () => { wakeLayer.hidden = true; armIdle(); });

/* Any interaction re-arms the idle timer so the screen stays awake while in
   use. A keypress while blanked also dismisses the layer (a hardware key isn't
   blocked spatially the way a touch on the overlay is). */
['pointerdown', 'mousedown', 'touchstart', 'wheel', 'keydown'].forEach(ev =>
  document.addEventListener(ev, e => {
    if (ev === 'keydown' && !wakeLayer.hidden) {
      e.preventDefault();
      e.stopPropagation();
      wakeLayer.hidden = true;
    }
    armIdle();
  }, { capture: true })
);

armIdle();

/* ── Helpers ── */
function appendLog(msg) {
  logEl.textContent += msg + '\n';
  const last = msg.trim();
  if (last) document.getElementById('log-last').textContent = last;
  const panel = logEl.parentElement;
  panel.scrollTop = panel.scrollHeight;
}

refreshFirmware();
let _savedTab = 'test';
try { _savedTab = localStorage.getItem('ctnd-tab') || 'test'; } catch (e) { /* no storage */ }
showTab(_savedTab);
