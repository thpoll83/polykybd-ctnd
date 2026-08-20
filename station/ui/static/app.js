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

function openRunner() {
  document.getElementById('main-controls').hidden = true;
  document.getElementById('runner-panel').hidden = false;
  document.getElementById('log-panel').hidden = true;
  document.getElementById('ci-jobs-panel').hidden = false;
  if (_ciJobsTimer) clearInterval(_ciJobsTimer);
  fetchCiJobs();
  _ciJobsTimer = setInterval(fetchCiJobs, 30000);
}

function closeRunner() {
  clearInterval(_ciJobsTimer);
  _ciJobsTimer = null;
  document.getElementById('runner-panel').hidden = true;
  document.getElementById('main-controls').hidden = false;
  document.getElementById('ci-jobs-panel').hidden = true;
  document.getElementById('log-panel').hidden = false;
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

function openMore()  {
  document.getElementById('main-controls').hidden = true;
  document.getElementById('more-panel').hidden = false;
}

function closeMore() {
  document.getElementById('more-panel').hidden = true;
  document.getElementById('main-controls').hidden = false;
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
  const panel = logEl.parentElement;
  panel.scrollTop = panel.scrollHeight;
}

refreshFirmware();
