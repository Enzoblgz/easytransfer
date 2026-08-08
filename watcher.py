#!/usr/bin/env python3
"""
Watches for the Android phone being plugged in, and offers to open EasyTransfer.

macOS has no launchd trigger for "a USB device appeared", so this polls adb —
cheap (~20 ms) and it uses the same transport the app needs anyway.

Runs as a LaunchAgent: see install-watcher.sh
"""

import os
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ADB = shutil.which("adb") or "/opt/homebrew/bin/adb"
PORT = int(os.environ.get("EASYTRANSFER_PORT", "8777"))
POLL = 3                  # seconds between checks
UNAUTH_GRACE = 6          # ticks (~18 s) before nagging about USB debugging
LOG = os.path.expanduser("~/.cache/easytransfer/watcher.log")


def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n"
    try:
        with open(LOG, "a") as f:
            f.write(line)
        # Keep the log from growing without bound.
        if os.path.getsize(LOG) > 256 * 1024:
            with open(LOG) as f:
                tail = f.readlines()[-400:]
            with open(LOG, "w") as f:
                f.writelines(tail)
    except OSError:
        pass


def phone_state():
    """-> 'device', 'unauthorized', or 'none'."""
    try:
        out = subprocess.run([ADB, "devices"], capture_output=True, timeout=20)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "none"
    lines = out.stdout.decode("utf-8", "replace").splitlines()[1:]
    states = [l.split()[1] for l in lines if l.strip() and len(l.split()) > 1]
    if "device" in states:
        return "device"
    if "unauthorized" in states:
        return "unauthorized"
    return "none"


def server_running():
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def parse_dialog(out):
    """osascript prints 'button returned:X, gave up:false' — pull out X."""
    if "gave up:true" in out:
        return ""
    for part in out.split(","):
        if "button returned:" in part:
            return part.split(":", 1)[1].strip()
    return ""


def dialog(text, buttons, default, timeout=90):
    """Show a macOS dialog. Returns the button pressed, or '' if it timed out."""
    btns = ", ".join(f'"{b}"' for b in buttons)
    script = (
        f'display dialog "{text}" with title "EasyTransfer" '
        f'buttons {{{btns}}} default button "{default}" '
        f'with icon note giving up after {timeout}'
    )
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        return ""
    return parse_dialog(r.stdout.decode("utf-8", "replace"))


def start_server():
    if server_running():
        return True
    logfile = open(os.path.expanduser("~/.cache/easytransfer/server.log"), "a")
    subprocess.Popen(
        [sys.executable, "-u", os.path.join(HERE, "server.py")],
        cwd=HERE, stdout=logfile, stderr=logfile,
        start_new_session=True,          # survives the watcher restarting
    )
    for _ in range(40):                  # up to ~8 s for the port to answer
        if server_running():
            return True
        time.sleep(0.2)
    return False


def on_connect():
    # Prompt even when the server is already up: it has no auto-shutdown, so
    # skipping the prompt in that case would silently kill this feature after
    # the first use. If it is running, "Ouvrir" just opens the browser.
    choice = dialog(
        "Ton téléphone est branché.\\n\\nOuvrir EasyTransfer pour parcourir tes photos ?",
        ["Plus tard", "Ouvrir EasyTransfer"], "Ouvrir EasyTransfer")
    log(f"prompt -> {choice or 'no answer'}")
    if choice != "Ouvrir EasyTransfer":
        return
    if start_server():
        subprocess.run(["open", f"http://127.0.0.1:{PORT}"], capture_output=True)
        log("opened")
    else:
        dialog("EasyTransfer n'a pas réussi à démarrer. "
               "Regarde ~/.cache/easytransfer/server.log",
               ["OK"], "OK", timeout=30)
        log("server failed to start")


def main():
    log("watcher started")
    last = phone_state()          # don't fire for a phone already plugged in
    unauth_ticks = 0
    nagged = False

    while True:
        time.sleep(POLL)
        try:
            now = phone_state()
        except Exception as e:
            log(f"poll error: {type(e).__name__}: {e}")
            continue

        if now == "device" and last != "device":
            log("phone connected")
            on_connect()
            nagged = False

        if now == "unauthorized":
            unauth_ticks += 1
            # The phone is plugged in but adb can't talk to it. This is the
            # usual failure, and it's invisible unless we say something.
            if unauth_ticks == UNAUTH_GRACE and not nagged:
                nagged = True
                log("unauthorized -> nagging")
                dialog("Téléphone détecté, mais adb n'y a pas accès.\\n\\n"
                       "Déverrouille-le et accepte « Autoriser le débogage USB ».",
                       ["OK"], "OK", timeout=45)
        else:
            unauth_ticks = 0

        if now == "none" and last != "none":
            log("phone disconnected")
            nagged = False

        last = now


if __name__ == "__main__":
    if "--test" in sys.argv:
        # Dry-run the prompt without waiting for a real plug-in event.
        print(f"adb sees: {phone_state()}   server running: {server_running()}")
        on_connect()
        sys.exit(0)
    try:
        main()
    except KeyboardInterrupt:
        pass
