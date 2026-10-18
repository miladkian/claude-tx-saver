"""Private per-user transport configuration shared by the CLI and installers."""

import os
from pathlib import Path
from urllib.parse import urlsplit


def state_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "claude-proxy"


def validate_relay(value):
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or not url.path.endswith("/v1/relay")):
        raise ValueError("relay must be an HTTPS URL ending in /v1/relay, without credentials, query, or fragment")
    # Accessing port also validates malformed/non-numeric ports.
    url.port
    return value


def configured_relay():
    value = os.environ.get("CLAUDE_TX_RELAY")
    path = state_dir() / "relay-url"
    if not value and path.exists():
        value = path.read_text().strip()
    if not value:
        raise ValueError("set --relay, CLAUDE_TX_RELAY, or run client.sh YOUR_VPS_IP first")
    return validate_relay(value)
