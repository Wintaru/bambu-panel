#!/usr/bin/env python3
"""Bambu X1C touch panel backend.

- Keeps an MQTT connection to the printer and merges its (partial) reports into one state.
- Pushes that state to the UI over a WebSocket.
- Runs go2rtc to relay the printer camera to the browser without transcoding.
- Fetches the current job's plate preview from the printer's SD card (FTPS) for the UI.
- Forwards control commands only when controls_enabled is true in config.json.
"""
import asyncio, ctypes, ftplib, io, json, re, signal, ssl, subprocess, time, zipfile
from pathlib import Path

import paho.mqtt.client as mqtt
from aiohttp import web

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text())
REPORT = f"device/{CFG['serial']}/report"
REQUEST = f"device/{CFG['serial']}/request"
GO2RTC_PORT = 1984


def deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v


class ImplicitFTPS(ftplib.FTP_TLS):
    """The printer's FTP server is implicit TLS on 990: wrap the socket before the greeting."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        # The printer requires the data channel to reuse the control channel's TLS session
        return self.context.wrap_socket(conn, session=self.sock.session), size


class FtpFile(io.RawIOBase):
    """Seekable read-only view of a file on the FTP server, fetched with REST + RETR ranges,
    so zipfile can pull one image out of a .3mf without downloading the whole thing."""
    def __init__(self, ftp, path):
        self.ftp, self.path, self.pos = ftp, path, 0
        ftp.voidcmd("TYPE I")
        self.size = ftp.size(path)

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.pos

    def seek(self, offset, whence=0):
        self.pos = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        return self.pos

    def readinto(self, b):
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        conn = self.ftp.transfercmd(f"RETR {self.path}", rest=self.pos)
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(min(65536, n - len(buf)))
            if not chunk:
                break
            buf += chunk
        conn.close()  # abandoning the transfer early; the server answers 426/226, either is fine
        try:
            self.ftp.voidresp()
        except ftplib.Error:
            pass
        b[:len(buf)] = buf
        self.pos += len(buf)
        return len(buf)


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def fetch_thumbnail(job):
    """Blocking. Find the job's .3mf on the SD card and return its plate preview PNG, or None.
    Cloud and Studio prints are stored as <subtask_name>.gcode.3mf in /cache or /."""
    name, plate, gcode_file = job
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # self-signed printer cert
    ftp = ImplicitFTPS(context=ctx, timeout=20)
    ftp.connect(CFG["host"], 990)
    try:
        ftp.login("bblp", CFG["access_code"])
        ftp.prot_p()
        candidates = [gcode_file] if gcode_file.endswith(".3mf") else []
        candidates += [f"{d}/{name}{ext}" for d in ("/cache", "") for ext in (".gcode.3mf", ".3mf")]
        path = None
        for c in candidates:
            try:
                ftp.size(c)
                path = c
                break
            except ftplib.Error:
                pass
        if path is None:
            # Special characters get mangled in stored file names (e.g. "/" -> "2f"); fuzzy match
            want = _norm(name)
            for d in ("/cache", "/"):
                for entry in ftp.nlst(d):
                    base = entry.rsplit("/", 1)[-1]
                    for ext in (".gcode.3mf", ".3mf"):
                        if base.endswith(ext) and _norm(base[:-len(ext)]) == want:
                            path = entry
            if path is None:
                return None
        with zipfile.ZipFile(io.BufferedReader(FtpFile(ftp, path), 1 << 16)) as z:
            names = z.namelist()
            want = f"Metadata/plate_{plate}.png"
            if want not in names:
                plates = sorted(n for n in names if re.fullmatch(r"Metadata/plate_\d+\.png", n))
                if not plates:
                    return None
                want = plates[0]
            return z.read(want)
    finally:
        try:
            ftp.quit()
        except (OSError, ftplib.Error):
            ftp.close()


class Printer:
    def __init__(self, loop):
        self.loop = loop
        self.state = {}
        self.online = False
        self.last_msg = 0.0
        self.clients = set()
        self.dirty = asyncio.Event()
        self.seq = 0
        self.thumb = None       # PNG bytes of the current job's plate preview
        self.thumb_job = None   # job the thumbnail (or the last attempt) belongs to
        self.thumb_tried = 0.0
        self.thumb_busy = False

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"panel-{int(time.time())}")
        c.username_pw_set("bblp", CFG["access_code"])
        c.tls_set(cert_reqs=ssl.CERT_NONE)
        c.tls_insecure_set(True)
        c.reconnect_delay_set(1, 30)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        self.mqtt = c

    def start(self):
        self.mqtt.connect_async(CFG["host"], 8883, keepalive=30)
        self.mqtt.loop_start()

    # --- MQTT thread callbacks -------------------------------------------
    def _on_connect(self, c, u, flags, rc, props=None):
        if rc != 0:
            print(f"mqtt connect failed: {rc}", flush=True)
            return
        print("mqtt connected", flush=True)
        c.subscribe(REPORT)
        self.request_full()
        self.loop.call_soon_threadsafe(self._set_online, True)

    def _on_disconnect(self, c, u, flags, rc, props=None):
        print(f"mqtt disconnected: {rc}", flush=True)
        self.loop.call_soon_threadsafe(self._set_online, False)

    def _on_message(self, c, u, msg):
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        self.loop.call_soon_threadsafe(self._ingest, data)

    # --- event loop side -------------------------------------------------
    def _set_online(self, v):
        self.online = v
        self.dirty.set()

    def _ingest(self, data):
        self.last_msg = time.time()
        for section, body in data.items():
            if not isinstance(body, dict):
                continue
            cmd = body.get("command")
            if cmd in (None, "push_status"):
                deep_merge(self.state, body)
                self.dirty.set()
                self.check_thumb()
            elif "result" in body or "reason" in body:
                # Reply to a command we (or Bambu Studio) sent
                self._broadcast({"type": "cmd_result", "section": section, "command": cmd,
                                 "result": body.get("result"), "reason": body.get("reason")})

    def current_job(self):
        name = self.state.get("subtask_name")
        if not name:
            return None
        gcode_file = self.state.get("gcode_file") or ""
        plate = self.state.get("plate_idx")
        if not plate:
            m = re.search(r"plate_(\d+)", gcode_file)
            plate = int(m.group(1)) if m else 1
        return (name, int(plate), gcode_file)

    def check_thumb(self):
        job = self.current_job()
        if self.thumb_busy or job is None:
            return
        if job == self.thumb_job and (self.thumb or time.time() - self.thumb_tried < 120):
            return
        self.thumb_busy = True
        self.thumb_tried = time.time()
        asyncio.ensure_future(self._load_thumb(job))

    async def _load_thumb(self, job):
        try:
            png = await self.loop.run_in_executor(None, fetch_thumbnail, job)
            print(f"thumbnail for {job[0]!r} plate {job[1]}: {len(png) if png else 'not found'}", flush=True)
        except Exception as e:
            print(f"thumbnail fetch failed: {e!r}", flush=True)
            png = None
        self.thumb_busy = False
        if job != self.thumb_job or png:
            self.thumb = png
        self.thumb_job = job
        self.dirty.set()

    def request_full(self):
        self.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})

    def publish(self, payload):
        self.seq += 1
        for body in payload.values():
            body["sequence_id"] = str(self.seq)
        self.mqtt.publish(REQUEST, json.dumps(payload))

    def snapshot(self):
        return {"type": "state", "online": self.online, "controls": CFG["controls_enabled"],
                "name": CFG["printer_name"], "print": self.state,
                # Changes whenever the image does, so the UI knows to reload it
                "thumb": f"{self.thumb_job[0]}|{self.thumb_job[1]}" if self.thumb else None}

    def _broadcast(self, msg):
        text = json.dumps(msg)
        for ws in list(self.clients):
            asyncio.ensure_future(ws.send_str(text))

    async def pump(self):
        """Send merged state to UI clients, at most 4 times a second."""
        while True:
            await self.dirty.wait()
            self.dirty.clear()
            self._broadcast(self.snapshot())
            await asyncio.sleep(0.25)

    async def watchdog(self):
        """Re-request a full report periodically and flag the printer offline if it goes quiet."""
        while True:
            await asyncio.sleep(60)
            if self.online:
                self.request_full()
            if self.online and time.time() - self.last_msg > 90:
                self._set_online(False)
                self.mqtt.reconnect()


COMMANDS = {
    "pause":  lambda v: {"print": {"command": "pause", "param": ""}},
    "resume": lambda v: {"print": {"command": "resume", "param": ""}},
    "stop":   lambda v: {"print": {"command": "stop", "param": ""}},
    "speed":  lambda v: {"print": {"command": "print_speed", "param": str(int(v))}},
    "light":  lambda v: {"system": {"command": "ledctrl", "led_node": "chamber_light",
                                    "led_mode": "on" if v else "off", "led_on_time": 500,
                                    "led_off_time": 500, "loop_times": 0, "interval_time": 0}},
}


async def ws_handler(request):
    printer = request.app["printer"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    printer.clients.add(ws)
    await ws.send_str(json.dumps(printer.snapshot()))
    try:
        async for _ in ws:
            pass
    finally:
        printer.clients.discard(ws)
    return ws


async def thumb_handler(request):
    png = request.app["printer"].thumb
    if not png:
        raise web.HTTPNotFound()
    return web.Response(body=png, content_type="image/png")


async def cmd_handler(request):
    body = await request.json()
    name = body.get("cmd")
    if name not in COMMANDS:
        return web.json_response({"ok": False, "error": f"unknown command {name}"}, status=400)
    if not CFG["controls_enabled"]:
        return web.json_response({"ok": False, "error": "controls disabled"}, status=403)
    request.app["printer"].publish(COMMANDS[name](body.get("value")))
    return web.json_response({"ok": True})


SYSTEM_ACTIONS = {
    # Closes the kiosk browser (identified by its dedicated profile); Plasma is underneath.
    "exit":    ["pkill", "-f", "bambu-panel/firefox-profile"],
    # --no-block: the restart kills this process, so don't wait on the job
    "restart": ["systemctl", "--user", "--no-block", "restart", "bambu-panel.service"],
    # logind allows this for the active seat user without a password; autologin brings the panel back
    "reboot":  ["systemctl", "reboot"],
}


async def system_handler(request):
    """Tablet controls. Independent of controls_enabled, which only gates printer commands."""
    action = (await request.json()).get("action")
    if action not in SYSTEM_ACTIONS:
        return web.json_response({"ok": False, "error": f"unknown action {action}"}, status=400)
    print(f"system action: {action}", flush=True)
    subprocess.Popen(SYSTEM_ACTIONS[action])
    return web.json_response({"ok": True})


@web.middleware
async def no_cache(request, handler):
    # Kiosk never reloads on its own, so make it revalidate the UI files after an update
    resp = await handler(request)
    resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


async def index(request):
    return web.FileResponse(ROOT / "static" / "index.html",
                            headers={"Cache-Control": "no-cache"})


def start_go2rtc():
    cfg = {
        "api": {"listen": f"127.0.0.1:{GO2RTC_PORT}", "origin": "*"},
        "rtsp": {"listen": ""},
        "webrtc": {"listen": ""},
        "log": {"level": "warn"},
        # rtspx = RTSPS without certificate verification (printer uses a self-signed cert)
        "streams": {"printer": f"rtspx://bblp:{CFG['access_code']}@{CFG['host']}:322/streaming/live/1"},
    }
    path = ROOT / "go2rtc.yaml"
    path.write_text(json.dumps(cfg, indent=2))  # JSON is valid YAML
    path.chmod(0o600)
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    # PR_SET_PDEATHSIG: go2rtc gets SIGTERM if this process dies, so it never outlives us
    return subprocess.Popen([str(ROOT / "bin" / "go2rtc"), "-config", str(path)],
                            preexec_fn=lambda: libc.prctl(1, signal.SIGTERM))


async def main():
    loop = asyncio.get_running_loop()
    go2rtc = start_go2rtc()
    printer = Printer(loop)
    printer.start()

    app = web.Application(middlewares=[no_cache])
    app["printer"] = printer
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/thumb", thumb_handler)
    app.router.add_post("/api/cmd", cmd_handler)
    app.router.add_post("/api/system", system_handler)
    app.router.add_static("/static", ROOT / "static")

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", CFG["port"]).start()
    print(f"panel on http://127.0.0.1:{CFG['port']}", flush=True)

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tasks = [asyncio.create_task(printer.pump()), asyncio.create_task(printer.watchdog())]
    await stop.wait()

    for t in tasks:
        t.cancel()
    printer.mqtt.loop_stop()
    go2rtc.terminate()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
