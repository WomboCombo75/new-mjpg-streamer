#!/usr/bin/env python3
"""
HTTP control plane for `mjpg_streamer` (stdlib only).

Unified model: JSON config + "running" flag. Spawns mjpg_streamer with cwd set to
the streamer build directory and the same MJPG_* defaults as the binary.

Environment (optional):
  MJPG_STREAMER_ROOT   Directory with mjpg_streamer, *.so, www/ (default: script dir)
  STREAMCTL_BIND       Listen address (default: 127.0.0.1)
  STREAMCTL_PORT       Control API port (default: 8899)

API:
  GET  /              — JSON discovery, or HTML control page if Accept: text/html or ?html=1
  GET  /api/stream    — { running, pid, config, last_error }
  PUT  /api/stream     — JSON: same keys as config + "running": true|false
  OPTIONS /api/stream  — CORS preflight

MJPEG browser pages (index, stream) are served by mjpg_streamer on http_port (default 8080),
not by this control service (default 8899).

If http://localhost:8899/ hangs in the browser, use http://127.0.0.1:8899/ (localhost can use
IPv6 ::1 while the listener is IPv4-only on some setups).

No authentication. Bind to localhost by default. Do not expose on untrusted networks.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple, Type


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """One client cannot block others (e.g. hung /api/stream vs GET /)."""

    daemon_threads = True
    allow_reuse_address = True


class DualStackThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """Listen on :: with IPV6_V6ONLY=0 so IPv4 (127.0.0.1) and IPv6 (::1) both work."""

    address_family = socket.AF_INET6
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def _create_http_server(
    bind: str, port: int, handler_class: Type[BaseHTTPRequestHandler]
) -> Tuple[HTTPServer, str]:
    """Return (server, effective_bind_label_for_logs)."""
    all_ifaces = bind in ("0.0.0.0", "", "::")
    if all_ifaces:
        # Prefer IPv6 any (::) + IPV6_V6ONLY=0 so http://localhost resolves via ::1 works.
        for factory, label in (
            (lambda: DualStackThreadingHTTPServer(("::", port), handler_class), ":: (IPv4+IPv6)"),
            (lambda: ThreadingHTTPServer(("0.0.0.0", port), handler_class), "0.0.0.0"),
        ):
            try:
                return factory(), label
            except OSError:
                continue
        raise OSError(
            "could not bind port %s (try: sudo fuser -k %s/tcp  or stop other streamctl)"
            % (port, port)
        )
    return ThreadingHTTPServer((bind, port), handler_class), bind

_DEFAULT_CONFIG: Dict[str, Any] = {
    "running": False,
    "device": "/dev/video0",
    "resolution": "1920x1080",
    "fps": 60,
    "http_port": 8080,
    "www": "./www",
    "input_plugin": "./input_uvc.so",
    # Full input line for MJPG_INPUT (device/resolution/fps ignored unless unset)
    "custom_input": None,
    # If true with custom_input, use -i "..." instead of MJPG_INPUT
    "use_explicit_input": False,
    # Full output line, e.g. 'output_http.so -w ./www -p 8080'
    "custom_output": None,
}

_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None
_config: Dict[str, Any] = {}
_last_error: Optional[str] = None
_log_tail: list[str] = []
_LOG_TAIL_MAX = 200


def _append_log(line: str) -> None:
    global _log_tail
    _log_tail.append(line)
    if len(_log_tail) > _LOG_TAIL_MAX:
        _log_tail = _log_tail[-_LOG_TAIL_MAX:]


def _drain_streamer_stderr_unlocked(p: subprocess.Popen) -> None:
    """Read mjpg_streamer stderr so the PIPE never blocks; store a tail for UI/API."""
    try:
        if p.stderr is None:
            return
        for raw in iter(p.stderr.readline, b""):
            try:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
            except Exception:
                line = repr(raw)
            with _lock:
                _append_log(line)
    except Exception as e:
        with _lock:
            _append_log(f"[streamctl] stderr reader error: {e}")
    finally:
        try:
            if p.stderr is not None:
                p.stderr.close()
        except Exception:
            pass


def _streamer_root() -> str:
    if os.environ.get("MJPG_STREAMER_ROOT"):
        return os.path.abspath(os.environ["MJPG_STREAMER_ROOT"])
    return os.path.dirname(os.path.abspath(__file__))


def _state_path(root: str) -> str:
    return os.path.join(root, ".streamctl_config.json")


def _load_disk_config(root: str) -> Dict[str, Any]:
    merged = dict(_DEFAULT_CONFIG)
    path = _state_path(root)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                disk = json.load(f)
            for k in _DEFAULT_CONFIG:
                if k in disk:
                    merged[k] = disk[k]
        except (OSError, json.JSONDecodeError):
            pass
    return merged


def _save_disk_config(root: str, cfg: Dict[str, Any]) -> None:
    snap = {k: cfg[k] for k in _DEFAULT_CONFIG}
    try:
        with open(_state_path(root), "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2)
    except OSError:
        pass


def _sync_proc() -> None:
    global _proc, _last_error
    if _proc is not None and _proc.poll() is not None:
        # Process exited; keep log tail for debugging.
        _last_error = f"stream exited with code {_proc.returncode}"
        _proc = None


def _stop_unlocked() -> None:
    global _proc, _last_error
    _sync_proc()
    if _proc is None:
        return
    try:
        _proc.send_signal(signal.SIGTERM)
        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(_proc.pid, signal.SIGKILL)
            _proc.wait(timeout=2)
    except (ProcessLookupError, ChildProcessError, OSError) as e:
        _last_error = str(e)
    finally:
        _proc = None


def _build_cmd_env(cfg: Dict[str, Any], root: str) -> Tuple[list, dict]:
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = root + os.pathsep + env.get("LD_LIBRARY_PATH", "")

    for k in (
        "MJPG_INPUT",
        "MJPG_INPUT_PLUGIN",
        "MJPG_DEVICE",
        "MJPG_RESOLUTION",
        "MJPG_FPS",
        "MJPG_PORT",
        "MJPG_WWW",
    ):
        env.pop(k, None)

    argv = [os.path.join(root, "mjpg_streamer")]
    ex_in = cfg.get("custom_input")
    ex_out = cfg.get("custom_output")
    explicit_i = bool(cfg.get("use_explicit_input"))

    if ex_in and explicit_i:
        argv += ["-i", str(ex_in)]
    elif ex_in:
        env["MJPG_INPUT"] = str(ex_in)

    if ex_out:
        argv += ["-o", str(ex_out)]
    else:
        env["MJPG_PORT"] = str(int(cfg["http_port"]))
        env["MJPG_WWW"] = str(cfg["www"])

    if not ex_in:
        env["MJPG_INPUT_PLUGIN"] = str(cfg["input_plugin"])
        env["MJPG_DEVICE"] = str(cfg["device"])
        env["MJPG_RESOLUTION"] = str(cfg["resolution"])
        env["MJPG_FPS"] = str(int(cfg["fps"]))

    return argv, env


def _start_unlocked(root: str, cfg: Dict[str, Any]) -> Optional[str]:
    global _proc, _last_error, _log_tail
    _last_error = None
    _log_tail = []
    _sync_proc()
    if _proc is not None:
        return "stream already running"

    bin_path = os.path.join(root, "mjpg_streamer")
    if not os.path.isfile(bin_path) or not os.access(bin_path, os.X_OK):
        return "mjpg_streamer missing or not executable (run make in %s)" % root

    argv, env = _build_cmd_env(cfg, root)
    try:
        _proc = subprocess.Popen(
            argv,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        t = threading.Thread(target=_drain_streamer_stderr_unlocked, args=(_proc,), daemon=True)
        t.start()
    except OSError as e:
        _proc = None
        return str(e)
    return None


def _validate_config(cfg: Dict[str, Any]) -> Optional[str]:
    try:
        port = int(cfg.get("http_port", 0))
        if not (1 <= port <= 65535):
            return "http_port must be in range 1..65535"
    except Exception:
        return "http_port must be an integer"

    try:
        fps = int(cfg.get("fps", 0))
        if not (1 <= fps <= 240):
            return "fps must be in range 1..240"
    except Exception:
        return "fps must be an integer"

    res = str(cfg.get("resolution") or "")
    if "x" not in res:
        return "resolution must look like WIDTHxHEIGHT (e.g. 1920x1080)"
    w, _, h = res.partition("x")
    if not (w.isdigit() and h.isdigit() and 1 <= int(w) <= 10000 and 1 <= int(h) <= 10000):
        return "resolution must be WIDTHxHEIGHT with sane numbers"

    dev = str(cfg.get("device") or "")
    if dev and not dev.startswith("/dev/"):
        return "device must be a /dev/... path"

    www = str(cfg.get("www") or "")
    if www.strip() == "":
        return "www must not be empty"

    return None


def _public_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {k: cfg[k] for k in _DEFAULT_CONFIG}


def _handle_put(root: str, body: bytes) -> Tuple[int, dict]:
    global _config
    try:
        data = json.loads(body.decode("utf-8") if body else "{}")
    except json.JSONDecodeError as e:
        return 400, {"error": "invalid json", "detail": str(e)}

    if not isinstance(data, dict):
        return 400, {"error": "body must be a JSON object"}

    with _lock:
        cur = dict(_config)
        nxt = _normalize_config(data, cur)
        err = _validate_config(nxt)
        if err:
            return 400, {"ok": False, "error": err, "config": _public_config(nxt)}
        want = bool(nxt["running"])
        _sync_proc()
        running = _proc is not None

        if want:
            if running and _configs_equal_stream(cur, nxt):
                _config = nxt
                _save_disk_config(root, _config)
                return 200, {"ok": True, "message": "unchanged", "config": _public_config(_config)}
            if running:
                _stop_unlocked()
            err = _start_unlocked(root, nxt)
            if err:
                nxt["running"] = False
                _config = nxt
                _save_disk_config(root, _config)
                return 500, {"ok": False, "error": err, "config": _public_config(_config)}
            nxt["running"] = True
            _config = nxt
            _save_disk_config(root, _config)
            return 200, {"ok": True, "message": "started", "config": _public_config(_config)}

        if running:
            _stop_unlocked()
        nxt["running"] = False
        _config = nxt
        _save_disk_config(root, _config)
        return 200, {"ok": True, "message": "stopped", "config": _public_config(_config)}


def _normalize_config(data: Dict[str, Any], prev: Dict[str, Any]) -> Dict[str, Any]:
    cfg = dict(prev)
    for key in _DEFAULT_CONFIG:
        if key not in data:
            continue
        if key == "running":
            cfg["running"] = bool(data["running"])
        elif key in ("fps", "http_port"):
            cfg[key] = int(data[key])
        elif key in ("custom_input", "custom_output"):
            v = data[key]
            cfg[key] = None if v in (None, "") else str(v)
        elif key == "use_explicit_input":
            cfg[key] = bool(data[key])
        else:
            cfg[key] = str(data[key])
    return cfg


def _configs_equal_stream(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    keys = [k for k in _DEFAULT_CONFIG if k != "running"]
    return all(a.get(k) == b.get(k) for k in keys)


def _dashboard_html() -> bytes:
    """Small browser UI: control API vs MJPEG pages are on different ports."""
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>Stream control</title>
<style>
body{font-family:system-ui,sans-serif;max-width:42rem;margin:2rem auto;padding:0 1rem}
code{background:#eee;padding:0 .2em} .row{margin:.6rem 0} label{display:inline-block;min-width:9rem}
.status{padding:.75rem;border-radius:6px;background:#f4f4f4;margin:1rem 0}
a{color:#06c} .err{color:#a00}
button{padding:.35rem .75rem;margin-right:.5rem}
</style></head><body>
<h1>Stream control</h1>
<noscript><p>Enable JavaScript for this control page, or use <code>curl http://127.0.0.1:8899/api/stream</code>.</p></noscript>
<p class="status" id="st">Starting…</p>
<p>This page talks to the <strong>control</strong> service on this port. The camera <strong>web UI</strong>
(MJPG-streamer <code>www/</code> pages, snapshot, stream) is on the <strong>stream HTTP port</strong> below
(usually 8080). Open it in another tab if the menu or live stream is what you want.</p>
<p>MJPEG UI: <a id="mjpegProxy" href="/mjpeg/">/mjpeg/</a> (available when running)</p>
<h2>Settings</h2>
<div class="row"><label>Device</label>
  <select id="device" style="min-width: 18rem"></select>
  <button type="button" id="btnDevRefresh">Refresh</button>
  <label style="margin-left:.5rem;font-weight:400">
    <input id="showAllDevs" type="checkbox" /> show all
  </label>
</div>
<div class="row"><label>Resolution</label><input id="resolution" type="text" placeholder="1920x1080"/></div>
<div class="row"><label>FPS</label><input id="fps" type="number"/></div>
<div class="row"><label>Stream HTTP port</label><input id="http_port" type="number"/></div>
<div class="row"><label>www folder</label><input id="www" type="text"/></div>
<p>
<button type="button" id="btnStart">Start / apply</button>
<button type="button" id="btnStop">Stop</button>
<button type="button" id="btnRefresh">Refresh status</button>
</p>
<p>
<strong>Presets:</strong>
<button type="button" id="p720p30">720p30</button>
<button type="button" id="p1080p30">1080p30</button>
<button type="button" id="p1080p60">1080p60</button>
<button type="button" id="p1080p60hq">1080p60 (your default)</button>
</p>
<p>
<strong>Quick checks:</strong>
<button type="button" id="btnTestSnapshot">Test snapshot</button>
<span id="snap"></span>
</p>
<h2>Logs (tail)</h2>
<p class="status" style="background:transparent;padding:0;margin:.25rem 0">
<button type="button" id="btnLogRefresh">Refresh logs</button>
<button type="button" id="btnLogAuto">Auto: off</button>
<span id="loghint"></span>
</p>
<pre id="log" style="white-space:pre-wrap;word-break:break-word;background:#f7f7f7;border-radius:6px;padding:.75rem;max-height:240px;overflow:auto;margin:.5rem 0"></pre>
<h2>Live preview</h2>
<p class="status" style="background:transparent;padding:0;margin:.25rem 0">
  <button type="button" id="btnPreviewOn">Start preview</button>
  <button type="button" id="btnPreviewOff">Stop preview</button>
  <span id="previewStatus"></span>
</p>
<div style="background:#f7f7f7;border-radius:6px;padding:.5rem;margin:.5rem 0">
  <img id="preview" alt="Live preview" style="max-width:100%;height:auto;display:block" />
</div>
<h2>Camera controls</h2>
<p>
  <button type="button" id="btnCtlLoad">Load controls</button>
  <span id="ctlStatus"></span>
</p>
<div id="controls" style="background:#f7f7f7;border-radius:6px;padding:.75rem;max-height:280px;overflow:auto"></div>
<p class="err" id="err"></p>
<script>
const $ = (id) => document.getElementById(id);
function mjpegBase() {
  const p = parseInt($('http_port').value, 10) || 8080;
  const h = location.hostname || '127.0.0.1';
  return 'http://' + h + ':' + p + '/';
}
function setMjpegLink() {
  const a = $('mjpegProxy');
  a.href = '/mjpeg/';
  a.textContent = '/mjpeg/';
}
async function load() {
  $('err').textContent = '';
  $('st').textContent = 'Contacting API…';
  try {
    const ctrl = new AbortController();
    const t = setTimeout(function () { ctrl.abort(); }, 10000);
    const r = await fetch('/api/stream', { signal: ctrl.signal, cache: 'no-store' });
    clearTimeout(t);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const j = await r.json();
    const c = j.config || {};
    $('st').textContent = j.running ? ('Running (pid ' + j.pid + ')') : 'Stopped';
    if (j.last_error) {
      $('st').textContent += ' — last error: ' + j.last_error;
      $('st').style.color = '#a00';
    } else {
      $('st').style.color = '#111';
    }
    const tail = j.log_tail || [];
    // Highlight likely errors/warnings without pulling in dependencies.
    const esc = (s) => String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
    const html = tail.length ? tail.map((ln) => {
      const t = String(ln);
      const low = t.toLowerCase();
      const isErr = low.includes('error') || low.includes('failed') || low.includes('fatal') || low.includes('bind(');
      const isWarn = low.includes('warn') || low.includes('coerced') || low.includes('timeout');
      const color = isErr ? '#a00' : (isWarn ? '#b36b00' : '#111');
      return '<span style=\"color:' + color + '\">' + esc(t) + '</span>';
    }).join('\\n') : '(no logs yet)';
    $('log').innerHTML = html;
    $('loghint').textContent = tail.length ? (' (' + tail.length + ' lines)') : '';
    await loadDevices(c.device || '');
    $('resolution').value = c.resolution || '';
    $('fps').value = c.fps || 60;
    $('http_port').value = c.http_port || 8080;
    $('www').value = c.www || './www';
    setMjpegLink();
  } catch (e) {
    $('st').textContent = 'Could not load /api/stream';
    $('err').textContent = (e && e.name === 'AbortError')
      ? 'Request timed out. Try http://127.0.0.1:' + (location.port || '8899') + '/ (avoid localhost / IPv6 issues).'
      : String(e);
    $('log').textContent = '(logs unavailable)';
    $('loghint').textContent = '';
  }
}

function opt(value, label, selected) {
  const o = document.createElement('option');
  o.value = value;
  o.textContent = label;
  if (selected) o.selected = true;
  return o;
}

function isCameraLike(name) {
  const n = String(name || '').toLowerCase();
  if (n.includes('uvc') || n.includes('camera') || n.includes('webcam')) return true;
  // Raspberry Pi CSI stack often shows up as isp capture/output; keep those if user wants CSI.
  if (n.includes('isp-capture') || n.includes('bcm2835-isp-capture')) return true;
  return false;
}

async function loadDevices(selectedPath) {
  const sel = $('device');
  const current = selectedPath || sel.value || '';
  sel.innerHTML = '';
  sel.appendChild(opt('', '(select device)', !current));
  try {
    const r = await fetch('/api/devices', { cache: 'no-store' });
    const j = await r.json();
    const devices = (j && j.devices) ? j.devices : [];
    const showAll = $('showAllDevs').checked;
    for (const d of devices) {
      if (!showAll && !isCameraLike(d.name)) continue;
      const label = (d.name ? d.name : d.path) + ' — ' + d.path;
      sel.appendChild(opt(d.path, label, d.path === current));
    }
    if (current && !Array.from(sel.options).some(o => o.value === current)) {
      sel.appendChild(opt(current, current + ' (custom)', true));
    }
  } catch (e) {
    if (current) sel.appendChild(opt(current, current + ' (current)', true));
  }
}
async function apply(running) {
  $('err').textContent = '';
  const body = {
    running: running,
    device: $('device').value,
    resolution: $('resolution').value,
    fps: parseInt($('fps').value, 10),
    http_port: parseInt($('http_port').value, 10),
    www: $('www').value
  };
  try {
    const r = await fetch('/api/stream', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!j.ok) $('err').textContent = j.error || JSON.stringify(j);
  } catch (e) {
    $('err').textContent = String(e);
  }
  await load();
}
$('btnStart').onclick = () => apply(true);
$('btnStop').onclick = () => apply(false);
$('btnRefresh').onclick = load;
$('http_port').onchange = setMjpegLink;
function preset(res, fps) {
  $('resolution').value = res;
  $('fps').value = fps;
}
$('p720p30').onclick = () => preset('1280x720', 30);
$('p1080p30').onclick = () => preset('1920x1080', 30);
$('p1080p60').onclick = () => preset('1920x1080', 60);
$('p1080p60hq').onclick = () => preset('1920x1080', 60);

async function testSnapshot() {
  $('snap').textContent = ' checking…';
  try {
    const r = await fetch('/api/test-snapshot', { cache: 'no-store' });
    const j = await r.json();
    $('snap').textContent = j.ok ? (' ok (' + j.http_status + ')') : (' failed: ' + (j.error || j.http_status));
  } catch (e) {
    $('snap').textContent = ' failed: ' + String(e);
  }
}
$('btnTestSnapshot').onclick = testSnapshot;
$('btnDevRefresh').onclick = () => loadDevices('');
$('showAllDevs').onchange = () => loadDevices('');
let autoTimer = null;
function setAuto(on) {
  if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
  if (on) { autoTimer = setInterval(load, 2000); }
  $('btnLogAuto').textContent = on ? 'Auto: on' : 'Auto: off';
}
$('btnLogRefresh').onclick = load;
$('btnLogAuto').onclick = () => setAuto(!autoTimer);

function setPreviewStatus(msg, color) {
  const el = $('previewStatus');
  el.textContent = msg ? (' ' + msg) : '';
  el.style.color = color || '#111';
}

function startPreview() {
  const img = $('preview');
  // cache-bust so browsers reconnect; MJPEG stream keeps the connection open
  img.src = '/mjpeg/?action=stream&t=' + Date.now();
  setPreviewStatus('running', '#0a7a28');
}

function stopPreview() {
  const img = $('preview');
  img.removeAttribute('src');
  img.src = '';
  setPreviewStatus('stopped', '#111');
}

$('btnPreviewOn').onclick = startPreview;
$('btnPreviewOff').onclick = stopPreview;

function setCtlStatus(msg, color) {
  const el = $('ctlStatus');
  el.textContent = msg ? (' ' + msg) : '';
  el.style.color = color || '#111';
}

function cmdUrl(dest, plugin, id, group, value) {
  const p = new URLSearchParams({
    action: 'command',
    dest: String(dest),
    plugin: String(plugin),
    id: String(id),
    group: String(group),
    value: String(value),
  });
  return '/mjpeg/?' + p.toString();
}

async function sendControl(control) {
  // input controls: dest=0, plugin index=0
  const url = cmdUrl(0, 0, control.id, control.group, control.value);
  await fetch(url, { cache: 'no-store' });
}

function renderControls(j) {
  const root = $('controls');
  root.innerHTML = '';
  const list = (j && j.controls) ? j.controls : [];
  if (!list.length) {
    root.textContent = '(no controls)';
    return;
  }
  let currentSection = null;
  for (const c of list) {
    const type = parseInt(c.type, 10);
    if (type === 6) {
      const h = document.createElement('div');
      h.style.fontWeight = '600';
      h.style.margin = '0.25rem 0 0.5rem 0';
      h.textContent = c.name || 'Controls';
      root.appendChild(h);
      currentSection = h;
      continue;
    }

    const row = document.createElement('div');
    row.style.display = 'grid';
    row.style.gridTemplateColumns = '12rem 1fr';
    row.style.gap = '0.5rem';
    row.style.alignItems = 'center';
    row.style.margin = '0.25rem 0';

    const label = document.createElement('div');
    label.textContent = c.name || ('id ' + c.id);
    label.style.fontSize = '13px';
    row.appendChild(label);

    const cell = document.createElement('div');

    if (type === 1) {
      // integer: slider + number
      const min = parseInt(c.min, 10);
      const max = parseInt(c.max, 10);
      const step = parseInt(c.step, 10) || 1;
      const val = parseInt(c.value, 10);
      const wrap = document.createElement('div');
      wrap.style.display = 'flex';
      wrap.style.gap = '0.5rem';
      wrap.style.alignItems = 'center';

      const range = document.createElement('input');
      range.type = 'range';
      range.min = String(min);
      range.max = String(max);
      range.step = String(step);
      range.value = String(val);
      range.style.flex = '1';

      const num = document.createElement('input');
      num.type = 'number';
      num.min = String(min);
      num.max = String(max);
      num.step = String(step);
      num.value = String(val);
      num.style.width = '6rem';

      const applyVal = async (v) => {
        c.value = String(v);
        setCtlStatus('sending…', '#b36b00');
        try {
          await sendControl({ id: c.id, group: c.group, value: v });
          setCtlStatus('ok', '#0a7a28');
        } catch (e) {
          setCtlStatus('failed: ' + String(e), '#a00');
        }
      };

      range.onchange = () => { num.value = range.value; applyVal(range.value); };
      num.onchange = () => { range.value = num.value; applyVal(num.value); };

      wrap.appendChild(range);
      wrap.appendChild(num);
      cell.appendChild(wrap);
    } else if (type === 2) {
      // boolean
      const chk = document.createElement('input');
      chk.type = 'checkbox';
      chk.checked = String(c.value) === '1';
      chk.onchange = async () => {
        setCtlStatus('sending…', '#b36b00');
        try {
          await sendControl({ id: c.id, group: c.group, value: chk.checked ? 1 : 0 });
          setCtlStatus('ok', '#0a7a28');
        } catch (e) {
          setCtlStatus('failed: ' + String(e), '#a00');
        }
      };
      cell.appendChild(chk);
    } else if (type === 3 && c.menu) {
      // menu/select
      const sel = document.createElement('select');
      for (const [k, v] of Object.entries(c.menu)) {
        const o = document.createElement('option');
        o.value = k;
        o.textContent = v;
        if (String(c.value) === String(k)) o.selected = true;
        sel.appendChild(o);
      }
      sel.onchange = async () => {
        setCtlStatus('sending…', '#b36b00');
        try {
          await sendControl({ id: c.id, group: c.group, value: sel.value });
          setCtlStatus('ok', '#0a7a28');
        } catch (e) {
          setCtlStatus('failed: ' + String(e), '#a00');
        }
      };
      cell.appendChild(sel);
    } else {
      const t = document.createElement('span');
      t.textContent = '(unsupported control type ' + String(c.type) + ')';
      t.style.color = '#666';
      cell.appendChild(t);
    }

    row.appendChild(cell);
    root.appendChild(row);
  }
}

async function loadControls() {
  setCtlStatus('loading…', '#111');
  try {
    const r = await fetch('/mjpeg/input_0.json', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const j = await r.json();
    renderControls(j);
    setCtlStatus('loaded', '#0a7a28');
  } catch (e) {
    $('controls').textContent = '(controls unavailable — start stream first)';
    setCtlStatus('failed: ' + String(e), '#a00');
  }
}

$('btnCtlLoad').onclick = loadControls;
load();
</script>
</body></html>
""".encode(
        "utf-8"
    )


def _handle_get() -> dict:
    # Never block the UI/API forever if something holds the lock.
    if not _lock.acquire(timeout=0.5):
        return {
            "running": False,
            "pid": None,
            "config": _public_config(dict(_DEFAULT_CONFIG)),
            "last_error": "internal: streamctl lock busy (try reload; if persistent, restart service)",
            "log_tail": [],
        }
    try:
        _sync_proc()
        pid = _proc.pid if _proc is not None else None
        running = _proc is not None
        cfg = dict(_config)
        cfg["running"] = running
        err = _last_error
        tail = list(_log_tail)[-80:]
        return {
            "running": running,
            "pid": pid,
            "config": _public_config(cfg),
            "last_error": err,
            "log_tail": tail,
        }
    finally:
        _lock.release()


def _list_video_devices() -> list[dict[str, str]]:
    """
    Enumerate local V4L2 device nodes (dependency-free).
    Returns items like: { "path": "/dev/video0", "name": "USB Camera" }.
    """
    devices: list[dict[str, str]] = []
    seen: set[str] = set()

    try:
        nodes = sorted(os.listdir("/sys/class/video4linux"))
    except OSError:
        nodes = []

    for node in nodes:
        if not node.startswith("video"):
            continue
        dev_path = f"/dev/{node}"
        if not os.path.exists(dev_path):
            continue
        label = node
        try:
            with open(f"/sys/class/video4linux/{node}/name", "r", encoding="utf-8") as f:
                label = (f.read().strip() or node)
        except OSError:
            pass
        devices.append({"path": dev_path, "name": label})
        seen.add(dev_path)

    try:
        for ent in sorted(os.listdir("/dev")):
            if not ent.startswith("video"):
                continue
            dev_path = f"/dev/{ent}"
            if dev_path in seen:
                continue
            if os.path.exists(dev_path):
                devices.append({"path": dev_path, "name": ent})
    except OSError:
        pass

    return devices


class Handler(BaseHTTPRequestHandler):
    root = _streamer_root()

    def _token(self) -> Optional[str]:
        tok = os.environ.get("STREAMCTL_TOKEN")
        return tok if tok and tok.strip() else None

    def _authed(self) -> bool:
        tok = self._token()
        if not tok:
            return True
        header = self.headers.get("X-Streamctl-Token")
        if header and header == tok:
            return True
        _, qs = self._path_qs()
        if qs.get("token", [""])[0] == tok:
            return True
        return False

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Streamctl-Token")
        self.send_header("Connection", "close")

    def do_OPTIONS(self) -> None:
        if self._path() not in ("/api/stream", "/api/test-snapshot", "/api/devices"):
            self.send_error(404)
            return
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _wants_html(self) -> bool:
        accept = (self.headers.get("Accept") or "").lower()
        if "text/html" in accept:
            return True
        _, qs = self._path_qs()
        if qs.get("html", [""])[0] == "1":
            return True
        return False

    def _path_qs(self) -> Tuple[str, dict]:
        u = urllib.parse.urlparse(self.path)
        return u.path, urllib.parse.parse_qs(u.query)

    def do_GET(self) -> None:
        if self._path().startswith("/mjpeg/"):
            if not self._proxy_mjpeg():
                self._json(502, {"ok": False, "error": "mjpeg backend not reachable (start stream first)"})
            return
        if self._path() == "/":
            if self._wants_html():
                raw = _dashboard_html()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self._cors()
                self.end_headers()
                self.wfile.write(raw)
                return
            self._json(
                200,
                {
                    "service": "streamctl",
                    "stream": "/api/stream",
                    "ui": "/?html=1",
                    "hint": "Open /?html=1 in a browser, or PUT /api/stream with JSON.",
                },
            )
            return
        if self._path() == "/api/devices":
            self._json(200, {"devices": _list_video_devices()})
            return
        if self._path() == "/api/test-snapshot":
            if not self._authed():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            payload = self._test_snapshot()
            self._json(200 if payload.get("ok") else 502, payload)
            return
        if self._path() == "/favicon.ico":
            self.send_response(204)
            self._cors()
            self.end_headers()
            return
        if self._path() != "/api/stream":
            self.send_error(404)
            return
        self._json(200, _handle_get())

    def do_PUT(self) -> None:
        if self._path() != "/api/stream":
            self.send_error(404)
            return
        if not self._authed():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length > 0 else b"{}"
        code, payload = _handle_put(Handler.root, body)
        self._json(code, payload)

    def _path(self) -> str:
        return self._path_qs()[0]

    def _json(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _test_snapshot(self) -> Dict[str, Any]:
        """Try to fetch one snapshot from the MJPEG server to validate it is alive."""
        st = _handle_get()
        cfg = st.get("config") or {}
        port = int(cfg.get("http_port") or 8080)
        host = self.headers.get("Host", "127.0.0.1").split(":")[0] or "127.0.0.1"
        url = f"http://{host}:{port}/?action=snapshot"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = resp.read(64)
                return {
                    "ok": 200 <= resp.status < 300,
                    "url": url,
                    "http_status": resp.status,
                    "bytes_read": len(data),
                }
        except Exception as e:
            return {"ok": False, "url": url, "error": str(e)}

    def _proxy_mjpeg(self) -> bool:
        """
        Reverse proxy for the legacy MJPEG HTTP UI, mounted at /mjpeg/.
        This keeps streamctl (8899) as the single entrypoint.
        """
        st = _handle_get()
        cfg = st.get("config") or {}
        port = int(cfg.get("http_port") or 8080)

        # Map /mjpeg/<path> to /<path> on the streamer.
        parsed = urllib.parse.urlparse(self.path)
        backend_path = parsed.path[len("/mjpeg") :] or "/"
        if backend_path == "/":
            backend_path = "/"
        if parsed.query:
            backend_path = backend_path + "?" + parsed.query

        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", backend_path, headers={"Host": f"127.0.0.1:{port}"})
            resp = conn.getresponse()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return False

        # Send status + headers (drop hop-by-hop headers).
        self.send_response(resp.status)
        hop = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}
        for k, v in resp.getheaders():
            if k.lower() in hop:
                continue
            # Rewrite Location headers back under /mjpeg/
            if k.lower() == "location" and v.startswith("/"):
                v = "/mjpeg" + v
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            while True:
                chunk = resp.read(16 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            try:
                resp.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        return True


def main() -> None:
    global _config
    Handler.root = _streamer_root()
    root = Handler.root
    _config = _load_disk_config(root)
    _config["running"] = False

    bind = os.environ.get("STREAMCTL_BIND", "127.0.0.1")
    port = int(os.environ.get("STREAMCTL_PORT", "8899"))

    httpd, bound_label = _create_http_server(bind, port, Handler)
    print(
        "mjpg-streamctl bind=%s port=%s (%s) streamer root=%s"
        % (bind, port, bound_label, root),
        file=sys.stderr,
    )

    httpd.timeout = 0.5

    def shutdown(*_: Any) -> None:
        if _lock.acquire(timeout=0.5):
            try:
                _stop_unlocked()
            finally:
                _lock.release()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    httpd.serve_forever()
    httpd.server_close()


if __name__ == "__main__":
    main()
