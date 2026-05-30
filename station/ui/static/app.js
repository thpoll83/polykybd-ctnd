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

socket.on('test_result', result => {
  const icon = result.passed ? '✓ PASS' : '✗ FAIL';
  appendLog(`\n── ${icon} ──`);
  (result.results || []).forEach(r => {
    const mark = r.passed ? '✓' : '✗';
    appendLog(`  ${mark} ${r.name}${r.error ? '  (' + r.error + ')' : ''}`);
  });
  appendLog('');
});

/* ── Primary actions ── */
function flashSide(side) {
  const sel = document.getElementById(side === 'left' ? 'left-fw' : 'right-fw');
  if (!sel.value) { appendLog(`[ui] select a firmware file for ${side} first`); return; }
  socket.emit('flash', { side, uf2: sel.value });
}

function runTests() {
  const left  = document.getElementById('left-fw').value;
  const right = document.getElementById('right-fw').value;
  if (!left || !right) { appendLog('[ui] select both left and right firmware files first'); return; }
  socket.emit('run_tests', { left_uf2: left, right_uf2: right });
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

/* ── Helpers ── */
function appendLog(msg) {
  logEl.textContent += msg + '\n';
  const panel = logEl.parentElement;
  panel.scrollTop = panel.scrollHeight;
}

refreshFirmware();
