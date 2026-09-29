#!/usr/bin/env python3
"""Bambu X1C touch panel backend.

- Keeps an MQTT connection to the printer and merges its (partial) reports into one state.
- Pushes that state to the UI over a WebSocket.
- Runs go2rtc to relay the printer camera to the browser without transcoding.
- Forwards control commands only when controls_enabled is true in config.json.
"""
import asyncio, ctypes, json, signal, ssl, subprocess, time
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


class Printer:
    def __init__(self, loop):
        self.loop = loop
        self.state = {}
        self.online = False
        self.last_msg = 0.0
        self.clients = set()
        self.dirty = asyncio.Event()
        self.seq = 0

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
            elif "result" in body or "reason" in body:
                # Reply to a command we (or Bambu Studio) sent
                self._broadcast({"type": "cmd_result", "section": section, "command": cmd,
                                 "result": body.get("result"), "reason": body.get("reason")})

    def request_full(self):
        self.publish({"pushing": {"sequence_id": "0", "command": "pushall"}})

    def publish(self, payload):
        self.seq += 1
        for body in payload.values():
            body["sequence_id"] = str(self.seq)
        self.mqtt.publish(REQUEST, json.dumps(payload))

    def snapshot(self):
        return {"type": "state", "online": self.online, "controls": CFG["controls_enabled"],
                "name": CFG["printer_name"], "print": self.state}

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
