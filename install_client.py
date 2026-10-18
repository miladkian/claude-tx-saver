"""Install/remove the macOS LaunchAgent or Linux user service and Claude base URL."""

import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import socket
import subprocess
import sys
import tempfile
import time

from tx_config import configured_relay, state_dir, validate_relay

ROOT = Path(__file__).resolve().parent
LABEL = "local.claude-proxy.client"
UNIT = "claude-tx-saver.service"


def write_private(path, data):
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def systemd_quote(value, escape_dollar=True):
    if any(c in value for c in "\n\r\0"):
        raise ValueError("service arguments cannot contain newlines or NUL")
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    return '"' + (value.replace('$', '$$') if escape_dollar else value) + '"'


def systemd_unit(arguments, working_directory):
    return ("[Unit]\nDescription=Claude TX Saver local client\nAfter=network-online.target\n\n"
            "[Service]\nType=simple\nExecStart=" + " ".join(map(systemd_quote, arguments)) +
            "\nWorkingDirectory=" + systemd_quote(str(working_directory), escape_dollar=False) +
            "\nRestart=on-failure\nRestartSec=3\nUMask=0077\nLimitCORE=0\n\n"
            "[Install]\nWantedBy=default.target\n")


def read_settings(path):
    current = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(current, dict) or not isinstance(current.get("env", {}), dict):
        raise ValueError("Claude settings and its env field must be JSON objects")
    return current


def restore_settings(settings, previous):
    if not previous.exists():
        return
    old = json.loads(previous.read_text())
    current = read_settings(settings)
    installed = old.get("installed", "http://127.0.0.1:8787")
    if current.get("env", {}).get("ANTHROPIC_BASE_URL") == installed:
        if old["present"]:
            current["env"]["ANTHROPIC_BASE_URL"] = old["value"]
        else:
            current["env"].pop("ANTHROPIC_BASE_URL", None)
        write_private(settings, (json.dumps(current, indent=2) + "\n").encode())
    previous.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--relay", help="override saved HTTPS relay URL")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    system = platform.system()
    if system not in {"Darwin", "Linux"}:
        parser.error("background services support macOS and Linux; use proxy.py client manually elsewhere")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    state = state_dir()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    domain = "gui/" + str(os.getuid())
    plist = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
    unit = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "systemd" / "user" / UNIT
    settings = Path.home() / ".claude" / "settings.json"
    previous = state / "previous-base-url.json"
    if args.uninstall:
        if system == "Darwin":
            subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], check=False, capture_output=True)
            plist.unlink(missing_ok=True)
        else:
            subprocess.run(["systemctl", "--user", "disable", "--now", UNIT], check=False)
            unit.unlink(missing_ok=True)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        restore_settings(settings, previous)
        print("Client removed; prior Claude base URL restored if it was still managed by this installer.")
        print("Relay service and private credentials are retained.")
        return
    try:
        relay = validate_relay(args.relay) if args.relay else configured_relay()
        current = read_settings(settings)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    for path in [state / "token", state / "relay.crt"]:
        if not path.exists():
            parser.error("required file missing: " + str(path) + "; run deploy.py first")
    arguments = [sys.executable, str(ROOT / "proxy.py"), "client", "--relay", relay,
                 "--port", str(args.port), "--token-file", str(state / "token"),
                 "--relay-ca", str(state / "relay.crt"), "--stats"]
    if system == "Darwin":
        plist.parent.mkdir(parents=True, exist_ok=True)
        log = state / "client.log"
        log.touch(mode=0o600, exist_ok=True)
        log.chmod(0o600)
        config = {"Label": LABEL, "ProgramArguments": arguments, "WorkingDirectory": str(ROOT),
                  "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
                  "StandardOutPath": str(log), "StandardErrorPath": str(log)}
        write_private(plist, plistlib.dumps(config))
        subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], check=False, capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    else:
        unit.parent.mkdir(parents=True, exist_ok=True)
        write_private(unit, systemd_unit(arguments, ROOT).encode())
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", UNIT], check=True)
        subprocess.run(["systemctl", "--user", "restart", UNIT], check=True)
    for attempt in range(30):
        try:
            with socket.create_connection(("127.0.0.1", args.port), timeout=2):
                break
        except OSError:
            if attempt == 29:
                raise RuntimeError("Local client failed to start; check service logs")
            time.sleep(0.2)
    base_url = "http://127.0.0.1:" + str(args.port)
    env = current.setdefault("env", {})
    if not previous.exists():
        write_private(previous, json.dumps({"present": "ANTHROPIC_BASE_URL" in env,
                                           "value": env.get("ANTHROPIC_BASE_URL"),
                                           "installed": base_url}).encode())
    else:
        old = json.loads(previous.read_text())
        old["installed"] = base_url
        write_private(previous, json.dumps(old).encode())
    env["ANTHROPIC_BASE_URL"] = base_url
    settings.parent.mkdir(parents=True, exist_ok=True)
    write_private(settings, (json.dumps(current, indent=2) + "\n").encode())
    print("Client service installed; new Claude Code sessions use " + base_url)
    print("Logs: " + (str(state / "client.log") if system == "Darwin" else "journalctl --user -u " + UNIT))


if __name__ == "__main__":
    main()
