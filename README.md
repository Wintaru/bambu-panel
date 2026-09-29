# Bambu Panel

A full-screen touch panel for a Bambu Lab printer (built for an X1 Carbon), running on an
old Linux tablet/laptop mounted next to the printer. It shows the live camera, job progress,
temperatures, fans and AMS slots, talking to the printer directly over your LAN. No cloud, no
Bambu Handy.

- Live camera with no transcoding (RTSPS → [go2rtc](https://github.com/AlexxIT/go2rtc) → MSE in the browser). Tap it for full screen.
- Job name, progress, layer, time remaining, finish time, current stage, HMS errors.
- Nozzle / bed / chamber temps, fan speeds, speed profile, AMS filament colors + humidity.
- Tablet menu (power icon, top right): reload screen, restart panel service, exit to desktop, reboot.
- Behaves like an appliance: autologin → kiosk browser, screen turns off when idle, wakes on one
  tap, never locks or suspends, power button toggles the screen.
- Printer controls (pause / resume / stop / light / speed) are built in but **off by default**; see
  [Printer controls](#printer-controls).

Reference hardware: Surface Book 1 running Arch Linux (linux-surface kernel) with KDE Plasma 6 on
Wayland. The backend is plain Python and works anywhere; the kiosk/screen parts (`kiosk.sh`)
assume KDE Plasma 6.

## How it works

```
printer ──MQTT/TLS :8883──▶ server.py ──WebSocket /ws──▶ Firefox (kiosk)
        ──RTSPS    :322 ──▶ go2rtc    ──MSE :1984─────▶   └ static/index.html
```

- `server.py`: aiohttp on `127.0.0.1:8765`. Subscribes to `device/<serial>/report`, merges the
  printer's partial updates into one state object, and pushes it to the page. Starts and
  supervises go2rtc (which exits when the server does).
- `static/`: the UI (`index.html`, `panel.css`, `panel.js`), plus go2rtc's `video-rtc.js` /
  `video-stream.js` player.
- `kiosk.sh`: applies screen settings, waits for the server, launches Firefox in kiosk mode.
- `systemd/bambu-panel.service`: user service for the backend.

Everything listens on localhost only.

## Requirements

- A Bambu printer on the same network with **LAN Mode Liveview** enabled (printer screen →
  Settings → Network), for the camera. Monitoring works in normal cloud mode.
- The printer's **IP address**, **serial number** and **LAN access code** (all on the printer's
  network settings screen).
- Linux, x86-64, with systemd, Python 3.10+, Firefox and `curl`.
- For the kiosk parts: KDE Plasma 6 (`kwriteconfig6`, `qdbus6`, `kde-inhibit`).

## Setup

The paths below assume you clone to `~/bambu-panel`. The service file and the "exit to desktop"
action expect that location.

### 1. Clone and configure

```sh
git clone https://github.com/<you>/bambu-panel.git ~/bambu-panel
cd ~/bambu-panel
cp config.example.json config.json
chmod 600 config.json
$EDITOR config.json
```

| Key | Meaning |
| --- | --- |
| `printer_name` | Shown in the top bar |
| `host` | Printer IP (give it a DHCP reservation) |
| `serial` | Printer serial number |
| `access_code` | LAN access code (8 characters, on the printer screen) |
| `controls_enabled` | `false` = monitor only. See below |
| `port` | Local web port (keep `8765` unless you also change `kiosk.sh`) |
| `screen_off_minutes` | Idle minutes before the display turns off |

`config.json` is git-ignored. Don't commit it: the access code gives full control of the printer
on your LAN.

### 2. Python environment

```sh
python3 -m venv ~/.local/share/bambu-venv
~/.local/share/bambu-venv/bin/pip install -r requirements.txt
```

### 3. go2rtc

Download the go2rtc binary for your platform from the
[releases page](https://github.com/AlexxIT/go2rtc/releases) (tested with v1.9.14) into `bin/`:

```sh
mkdir -p bin
curl -L -o bin/go2rtc \
  https://github.com/AlexxIT/go2rtc/releases/download/v1.9.14/go2rtc_linux_amd64
chmod +x bin/go2rtc
```

You don't need to configure it. `server.py` writes `go2rtc.yaml` (mode 600, git-ignored) at
startup.

### 4. Backend service

```sh
mkdir -p ~/.config/systemd/user
ln -s ~/bambu-panel/systemd/bambu-panel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bambu-panel
journalctl --user -u bambu-panel -f     # expect "panel on http://127.0.0.1:8765" and "mqtt connected"
```

Open <http://127.0.0.1:8765/> in any browser to check it. The camera should appear within a few
seconds.

Optional, so the service also runs when nobody is logged in: `sudo loginctl enable-linger $USER`.

### 5. Kiosk autostart (KDE Plasma)

```sh
mkdir -p ~/.config/autostart
sed "s|@PANEL_DIR@|$HOME/bambu-panel|" desktop/bambu-panel-kiosk.desktop \
  > ~/.config/autostart/bambu-panel-kiosk.desktop
```

(Copy it to `~/.local/share/applications/` as well if you want it in the app menu.)

At login, `kiosk.sh`:

- sets PowerDevil to turn the display off after `screen_off_minutes`, never dim, lock or suspend,
  on every power profile;
- makes the power button toggle the screen instead of sleeping;
- disables the screen locker, so a single tap wakes the screen straight to the panel;
- creates a dedicated Firefox profile in `firefox-profile/` with the prefs from `firefox/user.js`
  (no pinch zoom, swipe navigation, first-run pages or session restore; autoplay allowed; VA-API
  on);
- launches `firefox --kiosk` under `kde-inhibit --power`.

**These are changes to your user's KDE settings** (`powerdevilrc`, `kscreenlockerrc`, `kwinrc`).
Back them up first if the machine is also used for anything else.

### 6. Autologin

For an appliance-style boot, enable autologin for your user in your display manager. In KDE this
is System Settings → Login Screen (SDDM) → Behavior → "Automatically log in"; with Plasma
Login Manager it's the `[Autologin]` section of `/etc/plasmalogin.conf`:

```ini
[Autologin]
User=<your user>
Session=plasma.desktop
```

Reboot. The panel should come up full screen on its own.

## Using it

- **Tap the camera** to go full screen, tap again to go back.
- **Power icon** (top right) opens the Tablet menu: reload screen, restart panel service, exit to
  desktop (closes the kiosk browser), restart tablet.
- The display turns off after the idle timeout. One tap wakes it, and that tap isn't passed to
  the page.

## Printer controls

Pause / resume / stop / light / speed are wired up but disabled. `POST /api/cmd` refuses them
unless `controls_enabled` is `true`. Bambu firmware only accepts LAN control commands when the
printer is in **LAN Only mode with Developer Mode on**, which disconnects it from Bambu Cloud,
Handy and cloud printing from Bambu Studio. If you're fine with that:

1. On the printer: Settings → Network → LAN Only → on, then enable Developer Mode.
2. Set `"controls_enabled": true` in `config.json`.
3. `systemctl --user restart bambu-panel`

## Development notes

- Backend changes: `systemctl --user restart bambu-panel`. Logs: `journalctl --user -u bambu-panel`.
- UI changes: use "Reload screen" in the Tablet menu. The server sends `Cache-Control: no-cache`,
  so a reload picks up the edits.
- Screenshot the layout without the tablet:
  `firefox --headless --no-remote --profile "$(mktemp -d)" --screenshot out.png --window-size=1667,1112 http://127.0.0.1:8765/`
- The printer sends one full report, then partial deltas. `server.py` deep-merges them and asks for
  a full `pushall` every 60 s. The X1C reports chamber temperature under `device.ctc.info.temp`.
- To relaunch only the kiosk browser, kill it in a separate command from the relaunch, or
  `pkill -f` will match your own shell: `pkill -f '[f]irefox --kiosk'`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| "Connecting to printer…" forever | IP/serial/access code in `config.json`; can you reach port 8883? Logs show the MQTT result code. |
| No camera | LAN Mode Liveview enabled on the printer? go2rtc errors are in the service log. Only a limited number of clients can pull the stream at once (Bambu Studio counts). |
| Screen never turns off | Something is holding a screen inhibitor: `qdbus6 org.kde.Solid.PowerManagement /org/kde/Solid/PowerManagement/PolicyAgent ListInhibitions`. |
| Touch stops responding after re-running `kiosk.sh` in a live session | Seen once after a live KWin/PowerDevil reconfigure. A reboot fixed it. |

## Credits

- [go2rtc](https://github.com/AlexxIT/go2rtc) by AlexxIT: camera relay and the `video-rtc.js` /
  `video-stream.js` player (MIT).
- Bambu LAN MQTT protocol details from the community, notably
  [OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI).

## License

MIT. See [LICENSE](LICENSE). The vendored go2rtc player files in `static/` keep their own MIT
license.
