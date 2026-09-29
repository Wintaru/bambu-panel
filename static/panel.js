import {VideoRTC} from './video-rtc.js';

// ---------------------------------------------------------------- camera
class PrinterCam extends VideoRTC {
  constructor() {
    super();
    this.mode = 'mse';
    this.media = 'video';
    this.background = true;       // kiosk: never pause the stream
    this.visibilityCheck = false;
  }
  oninit() {
    super.oninit();
    this.video.controls = false;
    this.video.muted = true;
    const msg = document.getElementById('camMsg');
    this.video.addEventListener('playing', () => { msg.hidden = true; });
    this.video.addEventListener('waiting', () => { msg.textContent = 'Buffering…'; msg.hidden = false; });
    this.video.addEventListener('emptied', () => { msg.textContent = 'Reconnecting camera…'; msg.hidden = false; });
  }
}
customElements.define('printer-cam', PrinterCam);
document.getElementById('cam').src = `ws://${location.hostname}:1984/api/ws?src=printer`;

document.getElementById('camPanel').addEventListener('click', () => {
  document.body.classList.toggle('cam-full');
});

// ---------------------------------------------------------------- helpers
const $ = id => document.getElementById(id);
const num = v => (v === undefined || v === null || v === '' ? NaN : Number(v));

const STAGES = {
  1: 'Auto bed leveling', 2: 'Heatbed preheating', 3: 'Vibration compensation', 4: 'Changing filament',
  5: 'M400 pause', 6: 'Paused: filament runout', 7: 'Heating hotend', 8: 'Calibrating extrusion',
  9: 'Scanning bed surface', 10: 'Inspecting first layer', 11: 'Identifying build plate',
  12: 'Calibrating micro lidar', 13: 'Homing toolhead', 14: 'Cleaning nozzle tip',
  15: 'Checking extruder temperature', 16: 'Paused by user', 17: 'Paused: front cover fell',
  18: 'Calibrating micro lidar', 19: 'Calibrating extrusion flow', 20: 'Paused: nozzle temperature fault',
  21: 'Paused: heatbed temperature fault', 22: 'Unloading filament', 23: 'Paused: skipped step',
  24: 'Loading filament', 25: 'Calibrating motor noise', 26: 'Paused: AMS lost',
  27: 'Paused: heatbreak fan speed low', 28: 'Paused: chamber temperature fault', 29: 'Cooling chamber',
  30: 'Paused by G-code', 31: 'Motor noise showoff', 32: 'Paused: nozzle clumping detected',
  33: 'Paused: cutter error', 34: 'Paused: first layer error', 35: 'Paused: nozzle clog',
};
const SPEEDS = {1: 'Silent', 2: 'Standard', 3: 'Sport', 4: 'Ludicrous'};
const STATE_LABEL = {IDLE: 'IDLE', PREPARE: 'PREPARING', RUNNING: 'PRINTING', PAUSE: 'PAUSED',
                     FINISH: 'FINISHED', FAILED: 'FAILED', SLICING: 'SLICING'};

function fmtDuration(min) {
  if (!(min >= 0)) return '—';
  const h = Math.floor(min / 60), m = Math.round(min % 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}
function fmtEta(min) {
  if (!(min > 0)) return '—';
  const t = new Date(Date.now() + min * 60000);
  const hm = t.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  const days = Math.round((new Date(t).setHours(0, 0, 0, 0) - new Date().setHours(0, 0, 0, 0)) / 864e5);
  return days === 0 ? hm : days === 1 ? `${hm} tmrw` : `${hm} +${days}d`;
}
function fanPct(v) {
  const n = num(v);
  return Number.isNaN(n) ? '—' : `${Math.round(n / 15 * 10) * 10}%`;
}
function hex8(n) { return (n >>> 0).toString(16).toUpperCase().padStart(8, '0'); }
function hmsCode(h) {
  const a = hex8(h.attr), c = hex8(h.code);
  return `${a.slice(0, 4)}_${a.slice(4)}_${c.slice(0, 4)}_${c.slice(4)}`;
}
function setTemp(id, cur, tar) {
  const el = $(id);
  const c = num(cur), t = num(tar);
  el.querySelector('.t-cur').textContent = Number.isNaN(c) ? '—' : `${Math.round(c)}°`;
  el.querySelector('.t-tar').textContent = t > 0 ? `→ ${Math.round(t)}°` : '';
  el.classList.toggle('heating', t > 0 && Math.abs(c - t) > 3);
}

// The plate preview is the whole 256 mm bed with the model small in it: crop to the
// non-transparent pixels so the model fills the thumbnail.
function loadThumb(url) {
  const img = new Image();
  img.onload = () => {
    const c = document.createElement('canvas');
    c.width = img.naturalWidth; c.height = img.naturalHeight;
    const g = c.getContext('2d');
    g.drawImage(img, 0, 0);
    const px = g.getImageData(0, 0, c.width, c.height).data;
    let x0 = c.width, y0 = c.height, x1 = -1, y1 = -1;
    for (let y = 0; y < c.height; y++)
      for (let x = 0; x < c.width; x++)
        if (px[(y * c.width + x) * 4 + 3] > 16) {
          if (x < x0) x0 = x; if (x > x1) x1 = x;
          if (y < y0) y0 = y; if (y > y1) y1 = y;
        }
    if (x1 < 0) { $('jobThumb').src = url; return; }
    const side = Math.max(x1 - x0, y1 - y0) * 1.1 + 8;   // square, with a little margin
    const out = document.createElement('canvas');
    out.width = out.height = 256;
    out.getContext('2d').drawImage(c, (x0 + x1 - side) / 2, (y0 + y1 - side) / 2, side, side, 0, 0, 256, 256);
    $('jobThumb').src = out.toDataURL();
  };
  img.src = url;
}

// ---------------------------------------------------------------- render
let S = {};           // latest print state
let controlsEnabled = false;
let thumbKey = null;  // which job's preview is loaded

function render(msg) {
  if (busyMessage) return;   // a restart is under way; keep its overlay up
  S = msg.print || {};
  controlsEnabled = msg.controls;
  const online = msg.online;

  $('printerName').textContent = msg.name || 'Printer';
  $('connDot').classList.toggle('on', online);
  $('offline').hidden = online;
  $('offlineText').textContent = 'Printer offline — reconnecting…';
  if (!online || !S.gcode_state) return;

  // state + stage
  const gs = S.gcode_state;
  const pill = $('statePill');
  pill.textContent = STATE_LABEL[gs] || gs;
  pill.className = 'state-pill ' + ({RUNNING: 'running', PREPARE: 'running', PAUSE: 'pause',
                                     FAILED: 'failed', FINISH: 'finish'}[gs] || '');
  const stg = num(S.stg_cur);
  $('stageText').textContent = STAGES[stg] || '';
  $('wifi').textContent = S.wifi_signal ? `Wi-Fi ${S.wifi_signal}` : '';

  // job
  const active = gs === 'RUNNING' || gs === 'PAUSE' || gs === 'PREPARE';
  const pct = num(S.mc_percent);
  const name = S.subtask_name || '';
  $('jobTitle').textContent =
    active ? (name || 'Printing') :
    gs === 'FINISH' ? `Finished: ${name}` :
    gs === 'FAILED' ? `Failed: ${name}` : 'Ready to print';
  $('jobPct').textContent = active || gs === 'FINISH' ? `${pct || 0}%` : '';
  const showThumb = !!msg.thumb && (active || gs === 'FINISH' || gs === 'FAILED');
  if (msg.thumb && msg.thumb !== thumbKey) {
    thumbKey = msg.thumb;
    loadThumb(`/api/thumb?k=${encodeURIComponent(thumbKey)}`);
  }
  $('jobThumbBox').hidden = !showThumb;
  $('barFill').style.width = `${active || gs === 'FINISH' ? pct || 0 : 0}%`;
  $('barFill').style.background = gs === 'PAUSE' ? 'var(--warn)' : gs === 'FAILED' ? 'var(--err)' : '';
  $('layer').textContent = S.total_layer_num ? `${S.layer_num || 0} / ${S.total_layer_num}` : '—';
  $('remain').textContent = active ? fmtDuration(num(S.mc_remaining_time)) : '—';
  $('eta').textContent = active ? fmtEta(num(S.mc_remaining_time)) : '—';
  $('camChip').textContent = active
    ? `${pct || 0}% · layer ${S.layer_num || 0}/${S.total_layer_num || '?'} · ${fmtDuration(num(S.mc_remaining_time))} left`
    : (STATE_LABEL[gs] || gs);

  // temps (chamber lives in device.ctc on newer X1 firmware)
  setTemp('tNozzle', S.nozzle_temper, S.nozzle_target_temper);
  setTemp('tBed', S.bed_temper, S.bed_target_temper);
  setTemp('tChamber', S.device?.ctc?.info?.temp ?? S.chamber_temper, null);

  // errors
  const alerts = [];
  if (num(S.print_error)) {
    const e = hex8(num(S.print_error));
    alerts.push(`Print error <small>${e.slice(0, 4)}_${e.slice(4)}</small>`);
  }
  for (const h of S.hms || []) alerts.push(`Printer message <small>HMS_${hmsCode(h)}</small>`);
  $('alert').innerHTML = alerts.join('<hr style="border:0;border-top:1px solid rgba(239,68,68,.3);margin:10px 0">');
  $('alert').hidden = !alerts.length;

  renderAms();

  // fans / speed
  $('fanPart').textContent = fanPct(S.cooling_fan_speed);
  $('fanAux').textContent = fanPct(S.big_fan1_speed);
  $('fanCham').textContent = fanPct(S.big_fan2_speed);
  $('speedLvl').textContent = SPEEDS[S.spd_lvl] || '—';

  // controls
  $('controls').classList.toggle('locked', !controlsEnabled);
  $('lockNote').hidden = controlsEnabled;
  const paused = gs === 'PAUSE';
  $('lblPause').textContent = paused ? 'Resume' : 'Pause';
  $('icoPause').innerHTML = paused ? '<path d="M7 5l12 7-12 7z"/>' : '<path d="M8 5v14M16 5v14"/>';
  const light = (S.lights_report || []).find(l => l.node === 'chamber_light');
  $('btnLight').classList.toggle('on', light?.mode === 'on');
}

function renderAms() {
  const ams = S.ams?.ams || [];
  const trayNow = num(S.ams?.tray_now);
  const box = $('amsSlots');
  const unit = ams[0];
  if (!unit) { box.innerHTML = '<div class="slot-sub">No AMS detected</div>'; $('amsEnv').textContent = ''; return; }

  const env = [];
  if (unit.humidity_raw) env.push(`${unit.humidity_raw}% RH`);
  if (unit.temp) env.push(`${Math.round(num(unit.temp))}°C`);
  $('amsEnv').textContent = env.join(' · ');

  const slots = [];
  ams.forEach((u, ui) => (u.tray || []).forEach((t, ti) => slots.push({t, idx: ui * 4 + ti})));
  if (trayNow === 254 && S.vt_tray) slots.push({t: S.vt_tray, idx: 254, ext: true});
  box.style.gridTemplateColumns = `repeat(${Math.min(slots.length, 5)}, 1fr)`;

  box.innerHTML = slots.map(({t, idx, ext}) => {
    const empty = !t.tray_type;
    const color = t.tray_color ? `#${t.tray_color.slice(0, 6)}` : '#333';
    const remain = num(t.remain);
    const known = remain >= 0 && t.tray_uuid && !/^0+$/.test(t.tray_uuid);
    const C = 2 * Math.PI * 26;
    const dash = known ? C * Math.max(0, Math.min(100, remain)) / 100 : C;
    return `
      <div class="slot ${idx === trayNow ? 'active' : ''} ${empty ? 'empty' : ''}">
        <div class="spool">
          <svg viewBox="0 0 58 58">
            <circle class="ring-bg" cx="29" cy="29" r="26"/>
            <circle class="ring" cx="29" cy="29" r="26" stroke-dasharray="${dash} ${C}"
                    style="${known ? '' : 'stroke:var(--line)'}"/>
          </svg>
          <div class="core" style="${empty ? '' : `background:${color}`}"></div>
        </div>
        <div class="slot-type">${empty ? 'Empty' : t.tray_type}</div>
        <div class="slot-sub">${ext ? 'External' : empty ? `Slot ${idx % 4 + 1}` : known ? `${remain}%` : (t.tray_sub_brands || '—')}</div>
      </div>`;
  }).join('');
}

// ---------------------------------------------------------------- websocket
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'state') render(msg);
    else if (msg.type === 'cmd_result' && msg.result && msg.result !== 'success') {
      toast(`Printer rejected ${msg.command}${msg.reason ? `: ${msg.reason}` : ''}`);
    }
  };
  ws.onopen = () => { busyMessage = null; };
  ws.onclose = () => {
    if (busyMessage) { showBusy(); setTimeout(connect, 2000); return; }
    $('offline').hidden = false;
    $('offlineText').textContent = 'Panel service offline — reconnecting…';
    $('connDot').classList.remove('on');
    setTimeout(connect, 2000);
  };
}
connect();

// ---------------------------------------------------------------- UI bits
let toastTimer;
function toast(text) {
  const t = $('toast');
  t.textContent = text; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}

function sheet(html, handlers) {
  const bg = $('sheet');
  $('sheetBody').innerHTML = html;
  bg.hidden = false;
  const close = () => { bg.hidden = true; };
  bg.onclick = ev => {
    if (ev.target === bg) return close();
    const b = ev.target.closest('button[data-k]');
    if (!b) return;
    close();
    handlers[b.dataset.k]?.();
  };
}

async function sendCmd(cmd, value) {
  try {
    const r = await fetch('/api/cmd', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                       body: JSON.stringify({cmd, value})});
    const j = await r.json();
    if (!j.ok) toast(j.error || 'Command failed');
  } catch { toast('Panel service unreachable'); }
}

$('controls').addEventListener('click', ev => {
  const b = ev.target.closest('.ctl');
  if (!b) return;
  if (!controlsEnabled) {
    sheet(`<h2>Controls are locked</h2>
      <p>To control the printer from this panel, turn on <b>LAN Only Mode</b> and <b>Developer Mode</b>
      in the printer's network settings, then set <code>"controls_enabled": true</code> in
      <code>~/bambu-panel/config.json</code> and restart the panel.</p>
      <p>This disconnects the printer from Bambu Cloud and the Handy app.</p>
      <div class="row"><button data-k="ok">OK</button></div>`, {});
    return;
  }
  const gs = S.gcode_state;
  switch (b.dataset.act) {
    case 'pauseResume':
      if (gs === 'PAUSE') sendCmd('resume');
      else if (gs === 'RUNNING') sheet(`<h2>Pause print?</h2><div class="row">
        <button data-k="no">Cancel</button><button class="primary" data-k="yes">Pause</button></div>`,
        {yes: () => sendCmd('pause')});
      else toast('Nothing is printing');
      break;
    case 'stop':
      if (gs !== 'RUNNING' && gs !== 'PAUSE') { toast('Nothing is printing'); break; }
      sheet(`<h2>Stop this print?</h2><p>The print will be cancelled and can't be resumed.</p>
        <div class="row"><button data-k="no">Keep printing</button>
        <button class="danger" data-k="yes">Stop print</button></div>`, {yes: () => sendCmd('stop')});
      break;
    case 'light': {
      const on = (S.lights_report || []).find(l => l.node === 'chamber_light')?.mode === 'on';
      sendCmd('light', !on);
      break;
    }
    case 'speed':
      sheet(`<h2>Print speed</h2><div class="row">${Object.entries(SPEEDS).map(([k, v]) =>
        `<button data-k="${k}" class="${num(S.spd_lvl) === +k ? 'sel' : ''}">${v}</button>`).join('')}</div>`,
        Object.fromEntries(Object.keys(SPEEDS).map(k => [k, () => sendCmd('speed', +k)])));
      break;
  }
});

// clock
function tick() {
  $('clock').textContent = new Date().toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
}
tick(); setInterval(tick, 5000);

// ---------------------------------------------------------------- tablet menu
async function systemAction(action, busyText) {
  try {
    const r = await fetch('/api/system', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                          body: JSON.stringify({action})});
    const j = await r.json();
    if (!j.ok) return toast(j.error || 'Action failed');
  } catch { return toast('Panel service unreachable'); }
  if (busyText) { busyMessage = busyText; showBusy(); }
}

let busyMessage = null;   // overrides the "offline" text while a restart is in progress
function showBusy() {
  $('offline').hidden = false;
  $('offlineText').textContent = busyMessage;
}

function confirmSheet(title, text, label, cls, onYes) {
  sheet(`<h2>${title}</h2><p>${text}</p>
    <div class="row"><button data-k="no">Cancel</button><button class="${cls}" data-k="yes">${label}</button></div>`,
    {yes: onYes});
}

$('btnTablet').addEventListener('click', () => {
  sheet(`<h2>Tablet</h2>
    <div class="menu">
      <button data-k="reload">Reload screen</button>
      <button data-k="restart">Restart panel service</button>
      <button data-k="exit">Exit to desktop</button>
      <button class="danger" data-k="reboot">Restart tablet</button>
    </div>`, {
    reload: () => location.reload(),
    restart: () => confirmSheet('Restart panel service?',
      'Reconnects to the printer and camera. Takes a few seconds.', 'Restart', 'primary',
      () => systemAction('restart', 'Restarting panel service…')),
    exit: () => confirmSheet('Exit panel?', 'Closes the full-screen panel and returns to the desktop.',
      'Exit', 'primary', () => systemAction('exit')),
    reboot: () => confirmSheet('Restart tablet?',
      'The panel will be back in about a minute. A print in progress is not affected.',
      'Restart', 'danger', () => systemAction('reboot', 'Restarting tablet…')),
  });
});
document.addEventListener('contextmenu', ev => ev.preventDefault());

// keep the screen on (backup for kde-inhibit in kiosk.sh)
async function wakeLock() {
  try { await navigator.wakeLock?.request('screen'); } catch {}
}
wakeLock();
document.addEventListener('visibilitychange', () => { if (!document.hidden) wakeLock(); });
