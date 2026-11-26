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
  openLog();
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
  openLog();
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
  openLog();
  socket.emit('run_perf', { left_uf2: left, right_uf2: right });
}

function runDiagnostics() {
  appendLog('[ui] running runner diagnostics…');
  openLog();
  socket.emit('run_diagnostics');
}

/* Restart the runner service. Non-destructive (just bounces it), so no confirm.
   Fixes the common "configured but wedged" case without a reconfigure. */
function restartRunner() {
  appendLog('[ui] restarting runner service…');
  openLog();
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
  openLog();
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
  openLog();
  socket.emit('update_now');
}

/* ── Log drawer ──
   On every tab. Collapsed, it shows the newest line; open, the whole log.
   Commands that report in the log (flash, tests, perf, diagnostics, runner,
   update, handedness, phase measurement) open it, so their output is visible
   without leaving the tab. */
function setLogOpen(open) {
  const drawer = document.getElementById('log-drawer');
  drawer.dataset.open = open ? 'true' : 'false';
  document.getElementById('log-panel').hidden = !open;
  const btn = document.getElementById('btn-log-toggle');
  btn.textContent = open ? 'Log ▼' : 'Log ▲';
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) logEl.parentElement.scrollTop = logEl.parentElement.scrollHeight;
  placeCameraOverlay();   // the camera image shrinks or grows with the drawer
}

function openLog() { setLogOpen(true); }
function toggleLog() { setLogOpen(document.getElementById('log-drawer').dataset.open !== 'true'); }

function clearLog() {
  logEl.textContent = '';
  document.getElementById('log-last').textContent = '—';
}

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
  openLog();
  socket.emit('set_handedness', { master_is_left: masterIsLeft });
}


let _ciJobsTimer = null;

/* ── Tabs ──
   One section per mode. The last tab is remembered per browser (a reload of
   the kiosk returns to it); storage can throw in a locked-down profile, so
   every access is guarded. Entering a tab starts what it polls, leaving it
   stops that again. */
const TABS = ['test', 'hardware', 'camera', 'runner'];
let _currentTab = null;

function showTab(name) {
  if (!TABS.includes(name)) name = 'test';
  if (name === _currentTab) return;
  if (_currentTab === 'camera') { if (_adjust) cancelAdjust(); closeCamera(); }
  if (_currentTab === 'runner') { clearInterval(_ciJobsTimer); _ciJobsTimer = null; }
  TABS.forEach(t => {
    document.getElementById('tab-' + t).hidden = t !== name;
    const btn = document.querySelector(`.tab[data-tab="${t}"]`);
    btn.classList.toggle('active', t === name);
    btn.setAttribute('aria-selected', t === name ? 'true' : 'false');
    btn.tabIndex = t === name ? 0 : -1;
  });
  _currentTab = name;
  try { localStorage.setItem('ctnd-tab', name); } catch (e) { /* no storage */ }
  if (name === 'camera') openCamera();
  if (name === 'runner') {
    fetchCiJobs();
    let ticks = 0;   // poll GitHub every 30 s, redraw the times every 5 s
    _ciJobsTimer = setInterval(() => { if (++ticks % 6 === 0) fetchCiJobs(); else renderCiJobs(); }, 5000);
  }
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

// Times in the Runner tab: when a run came in, when it started (with the
// wait), and how long it took (still counting while it runs). Finished runs
// show their result. Re-rendered every 5 s from the last /ci-jobs reply, so the
// running time moves between the 30 s polls.
let _ciJobs = [];

function fmtTime(iso) {
  if (!iso) return '–';
  const d = new Date(iso);
  const now = new Date();
  const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return d.toDateString() === now.toDateString() ? t
    : `${d.toLocaleDateString([], { day: '2-digit', month: '2-digit' })} ${t}`;
}

function fmtDuration(ms) {
  if (!(ms >= 0)) return '–';
  const s = Math.round(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${String(s % 60).padStart(2, '0')}s`;
  return `${s}s`;
}

function ciTimes(j) {
  const now = Date.now();
  const created = j.created_at ? Date.parse(j.created_at) : NaN;
  const started = j.run_started_at ? Date.parse(j.run_started_at) : NaN;
  const ended = j.updated_at ? Date.parse(j.updated_at) : NaN;
  const parts = [`in ${fmtTime(j.created_at)}`];
  if (j.status === 'queued') {
    parts.push(`waiting ${fmtDuration(now - created)}`);
  } else {
    parts.push(`started ${fmtTime(j.run_started_at)}` +
               (started > created ? ` (waited ${fmtDuration(started - created)})` : ''));
    parts.push(j.status === 'completed' ? `took ${fmtDuration(ended - started)}`
                                        : `running ${fmtDuration(now - started)}`);
  }
  return parts.join(' · ');
}

const CI_RESULT = { success: '✓ Passed', failure: '✗ Failed', cancelled: '⊘ Cancelled',
                    skipped: '⏭ Skipped', timed_out: '⏱ Timed out' };

function renderCiJobs(jobs) {
  if (Array.isArray(jobs)) _ciJobs = jobs;
  const list = document.getElementById('ci-jobs-list');
  list.replaceChildren();
  if (!_ciJobs.length) {
    const msg = document.createElement('p');
    msg.className = 'no-jobs';
    msg.textContent = 'No scheduled, running or recent CI jobs.';
    list.appendChild(msg);
    return;
  }
  const frag = document.createDocumentFragment();
  let finishedHeader = false;
  _ciJobs.forEach(j => {
    const running = j.status === 'in_progress';
    const finished = j.status === 'completed';
    if (finished && !finishedHeader) {
      const h = document.createElement('p');
      h.className = 'ci-jobs-heading';
      h.textContent = 'Recently finished';
      frag.appendChild(h);
      finishedHeader = true;
    }

    const card = document.createElement('div');
    const result = finished ? (j.conclusion || 'unknown') : '';
    card.className = 'ci-job ' + (running ? 'ci-job-running'
      : finished ? `ci-job-done ci-result-${result}` : 'ci-job-queued');

    const iconEl = document.createElement('span');
    iconEl.className = 'ci-job-icon';
    iconEl.textContent = running ? '▶' : finished ? (result === 'success' ? '✓' : result === 'failure' ? '✗' : '•') : '⏳';

    const bodyEl = document.createElement('span');
    bodyEl.className = 'ci-job-body';

    const nameEl = document.createElement('span');
    nameEl.className = 'ci-job-name';
    nameEl.textContent = `#${j.run_number} ${j.name ?? ''}`;

    const metaEl = document.createElement('span');
    metaEl.className = 'ci-job-meta';
    metaEl.textContent = `${j.event ?? ''} · ${j.head_branch ?? ''}`;

    const timeEl = document.createElement('span');
    timeEl.className = 'ci-job-times';
    timeEl.textContent = ciTimes(j);

    const labelEl = document.createElement('span');
    labelEl.className = 'ci-job-label';
    labelEl.textContent = running ? 'Running' : finished ? (CI_RESULT[result] || result) : 'Queued';

    bodyEl.append(nameEl, metaEl, timeEl);
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
  setCameraLoading(true);
  placeCameraOverlay();   // _cameraInfo is null here, so this hides the line and labels
}

// No image yet: hide the img (an img without a picture draws a broken-image
// icon) and show the spinner in its place.
function setCameraLoading(loading) {
  document.getElementById('camera-frame').dataset.loading = loading ? 'true' : 'false';
}

function openCamera() {
  const gen = ++_cameraGen;
  _cameraInfo = null;
  clearCameraImage();
  loadPhaseLast();
  loadPhaseHistory();
  loadPhaseBins();
  _sharpPeak = 0;
  document.getElementById('camera-sharp').textContent = '';
  const msg = document.getElementById('camera-msg');
  msg.textContent = 'Loading camera…';
  fetch('/camera/info')
    .then(r => r.json())
    .then(info => {
      if (gen !== _cameraGen) return;   // the view was closed or reopened meanwhile
      if (!info.available) { msg.textContent = 'No camera: ' + info.detail; setCameraLoading(false); return; }
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
    .catch(e => {
      if (gen !== _cameraGen) return;
      msg.textContent = 'Camera info failed: ' + e;
      setCameraLoading(false);
    });
}

// Put the split line and the labels on the PAINTED image. object-fit: contain
// can leave bars at the sides or top, so split_x is a fraction of the painted
// width, not of the img box.
// The painted image inside the img box (object-fit: contain), in frame pixels.
function paintedRect(img) {
  const scale = Math.min(img.clientWidth / img.naturalWidth, img.clientHeight / img.naturalHeight);
  const pw = img.naturalWidth * scale, ph = img.naturalHeight * scale;
  return { x0: img.offsetLeft + (img.clientWidth - pw) / 2,
           y0: img.offsetTop + (img.clientHeight - ph) / 2, pw, ph };
}

function placeCameraOverlay() {
  const img = document.getElementById('camera-img');
  const split = document.getElementById('camera-split');
  const tagL = document.getElementById('camera-tag-left');
  const tagR = document.getElementById('camera-tag-right');
  const show = !!(_cameraInfo && img.naturalWidth && img.clientWidth);
  [split, tagL, tagR].forEach(el => { el.hidden = !show; });
  if (_adjust) { drawAdjust(); return; }
  document.getElementById('camera-box').hidden = true;
  if (!show) return;
  const { x0, y0, pw, ph } = paintedRect(img);
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
  const adjusting = _adjust;   // the adjust session this request belongs to, or null
  const full = adjusting ? '&full=1' : '';
  fetch('/camera/snapshot.jpg?t=' + Date.now() + full)
    .then(r => r.ok ? r.blob() : r.text().then(t => { throw new Error(t); }))
    .then(b => {
      if (gen !== _cameraGen || adjusting !== _adjust) return;
      if (img.src) URL.revokeObjectURL(img.src);
      img.src = URL.createObjectURL(b);
      if (adjusting) {
        // Only now is the full-frame image on screen: drawing may start.
        adjusting.ready = true;
        msg.textContent = adjustHint();
      } else if (_cameraText) msg.textContent = _cameraText;   // clear an earlier "busy" error
    })
    .catch(e => {
      if (gen !== _cameraGen || adjusting !== _adjust) return;
      msg.textContent = 'Snapshot: ' + e.message;
      // Stop the spinner unless an image is already shown; a retry or the live
      // refresh starts it again by loading.
      if (!document.getElementById('camera-img').hasAttribute('src')) setCameraLoading(false);
      // The editor needs the full frame; a live capture may hold the camera briefly.
      if (adjusting && !adjusting.ready) setTimeout(() => {
        if (gen === _cameraGen && adjusting === _adjust) refreshCamera();
      }, 1500);
    });
}

function cameraLive() {
  if (_cameraTimer) { clearInterval(_cameraTimer); _cameraTimer = null; }
  if (_cameraInfo && !_adjust && document.getElementById('camera-live').checked) {
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
// Bumped by every phase_result; a /camera/last reply that started before the
// newest phase_result arrived is older than what is shown, so it is dropped.
let _phaseSeq = 0;

function loadPhaseLast() {
  const seq = _phaseSeq;
  fetch('/camera/last')
    .then(r => r.json())
    .then(d => { if (seq === _phaseSeq) renderPhase(d); })
    .catch(() => {});
}

socket.on('phase_result', d => { _phaseSeq++; renderPhase(d); loadPhaseHistory(); });

/* The last measurements (newest first): when, from where, which firmware,
   result and offset, to compare builds, e.g. a release before and after a fix. */
function loadPhaseHistory() {
  fetch('/camera/history')
    .then(r => r.json())
    .then(renderPhaseHistory)
    .catch(() => {});
}

function renderPhaseHistory(hist) {
  const box = document.getElementById('phase-history-box');
  const list = document.getElementById('phase-history');
  list.replaceChildren();
  box.hidden = !hist || !hist.length;
  (hist || []).forEach(h => {
    const row = document.createElement('div');
    row.className = 'phase-hist-row';
    const chip = document.createElement('span');
    chip.className = 'chip st-' + (h.status || 'skip');
    chip.textContent = (h.status || '?').toUpperCase();
    const off = document.createElement('span');
    off.className = 'phase-hist-offset';
    off.textContent = h.offset_ms !== undefined && h.status !== 'skip'
      ? `${h.offset_ms > 0 ? '+' : ''}${h.offset_ms} ms` : '–';
    const what = document.createElement('span');
    what.className = 'phase-hist-what';
    const src = h.source === 'ci' ? `CI${h.ref ? ' ' + h.ref : ''}${h.commit ? ' @' + h.commit : ''}` : 'here';
    what.textContent = `${fmtTime(h.time)} · ${h.fw || '?'} · ${src}`;
    what.title = h.reason || h.info || '';
    row.append(chip, off, what);
    list.appendChild(row);
  });
}

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
  openLog();
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
document.getElementById('camera-img').addEventListener('load', () => setCameraLoading(false));
document.getElementById('camera-img').addEventListener('error', () => setCameraLoading(false));
// The image box changes size after load (the sharpness line appears below it,
// the log drawer opens): place the split line and labels again each time.
if (window.ResizeObserver) new ResizeObserver(() => placeCameraOverlay()).observe(document.getElementById('camera-img'));
window.addEventListener('resize', placeCameraOverlay);

/* ── Adjust view ──
   Set camera.roi and camera.split_x by touch. The full (uncropped) image is
   shown; step 1: drag a box around both halves; step 2: tap the gap between
   them. Save sends both to the server, which validates them and stores them in
   var/camera_view.json (they override config.yaml's roi and split_x). All
   coordinates are fractions of the full image; split_x is converted to a
   fraction of the cropped image, which is what the phase check uses. */
let _adjust = null;   // { step: 'box'|'split', box: [x, y, w, h], split: x (full-image fraction), drag }

// rpicam's --roi is in sensor coordinates (libcamera ScalerCrop), before
// hflip/vflip/rotation. The editor draws on the displayed (flipped) image, so a
// box is mirrored on each flipped axis when it is saved and when it is loaded
// (the same formula both ways). rotation 180 flips both axes. split_x is not
// converted: it applies to the cropped, already flipped output image.
function flipAxes(info) {
  const rot = Number(info.rotation) === 180;
  return { h: !!info.hflip !== rot, v: !!info.vflip !== rot };
}

function toFromSensor(box, info) {
  let [x, y, w, h] = box;
  const f = flipAxes(info);
  if (f.h) x = 1 - x - w;
  if (f.v) y = 1 - y - h;
  return [x, y, w, h];
}

function fullViewFromInfo(info) {
  let box = [0, 0, 1, 1];
  if (info.roi) {
    const v = info.roi.split(',').map(Number);
    if (v.length === 4 && v.every(n => isFinite(n))) box = toFromSensor(v, info);
  }
  return { box, split: box[0] + info.split_x * box[2] };
}

function adjustHint() {
  if (!_adjust) return '';
  return _adjust.step === 'box'
    ? 'Step 1 of 2: drag a box around both halves (full camera view shown).'
    : 'Step 2 of 2: tap the gap between the halves. Drag again to redraw the box.';
}

function startAdjust() {
  if (!_cameraInfo) return;
  const v = fullViewFromInfo(_cameraInfo);
  _adjust = { step: 'box', box: v.box, split: v.split, drag: null, ready: false };
  if (_cameraTimer) { clearInterval(_cameraTimer); _cameraTimer = null; }
  _cameraGen++;          // drop any cropped snapshot still in flight
  clearCameraImage();    // and the one on screen: the editor works on the full frame only
  document.getElementById('camera-view-buttons').hidden = true;
  document.getElementById('camera-adjust-buttons').hidden = false;
  document.getElementById('btn-adjust-reset').disabled = !_cameraInfo.view_override;
  document.getElementById('btn-adjust-save').disabled = true;
  document.getElementById('camera-msg').textContent = adjustHint();
  refreshCamera();
}

function endAdjust() {
  _adjust = null;
  document.getElementById('camera-adjust-buttons').hidden = true;
  document.getElementById('camera-view-buttons').hidden = false;
  document.getElementById('camera-box').hidden = true;
}

function cancelAdjust() {
  endAdjust();
  if (_currentTab === 'camera') { closeCamera(); openCamera(); }
}

function drawAdjust() {
  const img = document.getElementById('camera-img');
  const boxEl = document.getElementById('camera-box');
  const split = document.getElementById('camera-split');
  ['camera-tag-left', 'camera-tag-right'].forEach(id => { document.getElementById(id).hidden = true; });
  if (!img.naturalWidth || !img.clientWidth) { boxEl.hidden = split.hidden = true; return; }
  const { x0, y0, pw, ph } = paintedRect(img);
  const [bx, by, bw, bh] = _adjust.box;
  boxEl.hidden = false;
  boxEl.style.left = (x0 + bx * pw) + 'px';
  boxEl.style.top = (y0 + by * ph) + 'px';
  boxEl.style.width = (bw * pw) + 'px';
  boxEl.style.height = (bh * ph) + 'px';
  split.hidden = _adjust.step !== 'split';
  split.style.left = (x0 + _adjust.split * pw) + 'px';
  split.style.top = (y0 + by * ph) + 'px';
  split.style.height = (bh * ph) + 'px';
  split.style.bottom = 'auto';
}

// Pointer position as a fraction of the full image, clamped to it.
function imageFraction(e) {
  const img = document.getElementById('camera-img');
  const frame = document.getElementById('camera-frame').getBoundingClientRect();
  const { x0, y0, pw, ph } = paintedRect(img);
  const fx = (e.clientX - frame.left - x0) / pw, fy = (e.clientY - frame.top - y0) / ph;
  return [Math.min(1, Math.max(0, fx)), Math.min(1, Math.max(0, fy))];
}

// The values exactly as they are sent: each box edge rounded once to 0.001,
// sizes derived from the rounded edges (so x + w never exceeds 1), and split_x
// from the rounded box. splitValid() checks these same numbers.
function roundedView() {
  const r = n => Math.round(n * 1000) / 1000;
  const [bx, by, bw, bh] = _adjust.box;
  const x0 = r(bx), y0 = r(by), x1 = r(Math.min(1, bx + bw)), y1 = r(Math.min(1, by + bh));
  const w = r(x1 - x0), h = r(y1 - y0);
  const full = x0 <= 0.001 && y0 <= 0.001 && x1 >= 0.999 && y1 >= 0.999;
  // Mirroring a rounded box keeps it inside 0..1 (1 - x - w >= 0), so this
  // stays valid for the server; r() only drops float noise.
  const sensor = toFromSensor([x0, y0, w, h], _cameraInfo || {}).map(r);
  return { roi: full ? '' : sensor.join(','),
           split_x: full ? r(_adjust.split) : r((_adjust.split - x0) / w) };
}

function splitValid() {
  const { split_x } = roundedView();
  return split_x > 0.05 && split_x < 0.95;
}

const _frame = document.getElementById('camera-frame');
_frame.addEventListener('pointerdown', e => {
  if (!_adjust || !_adjust.ready || !document.getElementById('camera-img').naturalWidth) return;
  e.preventDefault();
  _frame.setPointerCapture(e.pointerId);
  _adjust.drag = { start: imageFraction(e), moved: false };
});
_frame.addEventListener('pointermove', e => {
  if (!_adjust || !_adjust.drag) return;
  const [sx, sy] = _adjust.drag.start, [x, y] = imageFraction(e);
  if (Math.abs(x - sx) < 0.02 && Math.abs(y - sy) < 0.02 && !_adjust.drag.moved) return;
  _adjust.drag.moved = true;
  _adjust.box = [Math.min(sx, x), Math.min(sy, y), Math.abs(x - sx), Math.abs(y - sy)];
  drawAdjust();
});
_frame.addEventListener('pointerup', e => {
  if (!_adjust || !_adjust.drag) return;
  const moved = _adjust.drag.moved;
  _adjust.drag = null;
  if (moved) {
    // A box smaller than 10 % of the view either way is a mis-touch: keep step 1.
    if (_adjust.box[2] < 0.1 || _adjust.box[3] < 0.1) { _adjust.step = 'box'; }
    else {
      _adjust.step = 'split';
      _adjust.split = _adjust.box[0] + _adjust.box[2] / 2;
    }
  } else if (_adjust.step === 'split') {
    _adjust.split = imageFraction(e)[0];
  }
  document.getElementById('btn-adjust-save').disabled = !(_adjust.step === 'split' && splitValid());
  document.getElementById('camera-msg').textContent = adjustHint() +
    (_adjust.step === 'split' && !splitValid() ? ' The line must lie inside the box, away from its edges.' : '');
  drawAdjust();
});

function saveAdjust() {
  if (!_adjust || !_adjust.ready || _adjust.step !== 'split' || !splitValid()) return;
  const { roi, split_x } = roundedView();
  openLog();
  socket.emit('camera_set_view', { roi, split_x });
}

function resetAdjust() {
  openLog();
  socket.emit('camera_clear_view');
}

// Another browser saved or cleared the view: reload ours, unless it is mid-edit.
socket.on('camera_view_changed', () => {
  if (_currentTab === 'camera' && !_adjust) { closeCamera(); openCamera(); }
});

socket.on('camera_view', ({ ok, error }) => {
  if (!ok) {
    document.getElementById('camera-msg').textContent = 'Not saved: ' + (error || 'unknown error');
    return;
  }
  endAdjust();
  if (_currentTab === 'camera') { closeCamera(); openCamera(); }
});

/* The .bin list for "Measure phase now": newest first, each with the firmware
   version built into it and its date. When the rig was idle, the server also
   asked the keyboard which version it runs; matching images are marked ✓ and
   the newest one is preselected (the measurement refuses other versions). */
let _binsSeq = 0;   // a reply from an older loadPhaseBins() call is dropped

function loadPhaseBins() {
  const sel = document.getElementById('phase-bin');
  const note = document.getElementById('phase-bin-note');
  const seq = ++_binsSeq;
  fetch('/firmware/bins')
    .then(r => r.json())
    .then(({ bins, running, running_note }) => {
      if (seq !== _binsSeq) return;
      const prev = sel.value;   // read now: the operator may have chosen while the HID query ran
      sel.replaceChildren();
      const first = document.createElement('option');
      first.value = '';
      first.textContent = bins.length ? '— .bin of the running build —' : '— no .bin in firmware/ —';
      sel.appendChild(first);
      let pick = '';
      bins.forEach(b => {
        const match = running && b.fw === running;
        const opt = document.createElement('option');
        opt.value = b.name;
        opt.textContent = `${match ? '✓ ' : ''}${b.fw || 'unknown version'} · ${fmtTime(b.mtime)} · ${b.name}`;
        if (running && !match) opt.className = 'bin-other';
        sel.appendChild(opt);
        if (match && !pick) pick = b.name;   // bins are newest first
      });
      sel.value = bins.some(b => b.name === prev) ? prev : pick;
      note.textContent = running
        ? `Keyboard runs ${running}` + (pick ? ' — matching image selected.' : ' — no image with that version here.')
        : (running_note ? `Running version unknown: ${running_note}.` : '');
    })
    .catch(() => { if (seq === _binsSeq) note.textContent = 'Could not list the .bin files.'; });
}

function quitApp() { window.close(); }

function refreshFirmware() {
  fetch('/firmware')
    .then(r => r.json())
    .then(files => {
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
