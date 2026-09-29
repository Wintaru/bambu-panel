#!/bin/bash
# Launch the printer panel full screen. Waits for the panel service first.
DIR="$(cd "$(dirname "$0")" && pwd)"

# Screen: turn off after screen_off_minutes of no input (config.json) or on a
# power-button press, never lock, and wake on a single tap. The tap that wakes it
# is not passed to the page.
MINUTES=$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1])).get("screen_off_minutes", 10)))' "$DIR/config.json")
for profile in AC Battery LowBattery; do
  kwriteconfig6 --file powerdevilrc --group "$profile" --group Display --key TurnOffDisplayWhenIdle true
  kwriteconfig6 --file powerdevilrc --group "$profile" --group Display --key TurnOffDisplayIdleTimeoutSec $((MINUTES * 60))
  kwriteconfig6 --file powerdevilrc --group "$profile" --group Display --key DimDisplayWhenIdle false
  kwriteconfig6 --file powerdevilrc --group "$profile" --group Display --key LockBeforeTurnOffDisplay false
  kwriteconfig6 --file powerdevilrc --group "$profile" --group SuspendAndShutdown --key AutoSuspendAction 0
  # Power button toggles the screen off/on (PowerButtonAction::ToggleScreenOnOff) instead of sleeping
  kwriteconfig6 --file powerdevilrc --group "$profile" --group SuspendAndShutdown --key PowerButtonAction 128
done
kwriteconfig6 --file kscreenlockerrc --group Daemon --key Autolock false
kwriteconfig6 --file kscreenlockerrc --group Daemon --key LockOnResume false
kwriteconfig6 --file kwinrc --group Wayland --key DoubleTapWakeup false
qdbus6 org.kde.KWin /KWin reconfigure >/dev/null 2>&1
qdbus6 org.kde.Solid.PowerManagement /org/kde/Solid/PowerManagement reparseConfiguration >/dev/null 2>&1
qdbus6 org.kde.Solid.PowerManagement /org/kde/Solid/PowerManagement refreshStatus >/dev/null 2>&1

for _ in $(seq 60); do
  curl -sf -o /dev/null http://127.0.0.1:8765/ && break
  sleep 1
done
# Dedicated Firefox profile; kiosk prefs (no zoom/swipe, autoplay) come from firefox/user.js
mkdir -p "$DIR/firefox-profile"
cp "$DIR/firefox/user.js" "$DIR/firefox-profile/user.js"
# kde-inhibit --power blocks sleep while the panel is open. (No --screenSaver:
# that would also stop the display from turning off.)
exec kde-inhibit --power firefox --kiosk --no-remote --profile "$DIR/firefox-profile" --new-instance http://127.0.0.1:8765/
