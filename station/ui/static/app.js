/* SPDX-License-Identifier: GPL-2.0-only */
'use strict';

const socket    = io();
const logEl     = document.getElementById('log-output');
const statusEl  = document.getElementById('status-text');
const clockEl   = document.getElementById('clock');
const actionBtns = ['btn-flash-left', 'btn-flash-right', 'btn-run'].map(id => document.getElementById(id));

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

socket.on('test_result', result => {
  const icon = result.passed ? '✓ PASS' : '✗ FAIL';
  appendLog(`\n── ${icon} ──`);
  (result.results || []).forEach(r => {
    const mark = r.passed ? '✓' : '✗';
    appendLog(`  ${mark} ${r.name}${r.error ? '  (' + r.error + ')' : ''}`);
  });
  appendLog('');
});

/* ── Actions ── */
function flashSide(side) {
  const sel = document.getElementById(side === 'left' ? 'left-fw' : 'right-fw');
  if (!sel.value) { appendLog(`[ui] select a ${side} UF2 first`); return; }
  socket.emit('flash', { side, uf2: sel.value });
}

function runTests() {
  const left  = document.getElementById('left-fw').value;
  const right = document.getElementById('right-fw').value;
  if (!left || !right) { appendLog('[ui] select both left and right UF2 files first'); return; }
  socket.emit('run_tests', { left_uf2: left, right_uf2: right });
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

function refreshFirmware() {
  fetch('/firmware')
    .then(r => r.json())
    .then(files => {
      ['left-fw', 'right-fw'].forEach(id => {
        const sel  = document.getElementById(id);
        const prev = sel.value;
        sel.innerHTML = '<option value="">— select UF2 —</option>';
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
