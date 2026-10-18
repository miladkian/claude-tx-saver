"""Deploy the authenticated TLS relay to the specified VPS over SSH."""

import argparse
import ipaddress
import os
from pathlib import Path
import shlex
import subprocess


ROOT = Path(__file__).resolve().parent


def ssh(host, script, capture=False):
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "root@" + host, "sh -se"],
        input=script.encode(), check=True,
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="IPv4 address of a Linux VPS with root SSH access")
    args = parser.parse_args()
    host = str(ipaddress.IPv4Address(args.host))
    ssh(host, """
umask 077
install -d -m 755 /opt/claude-proxy /etc/claude-proxy
if [ ! -f /etc/claude-proxy/token ]; then
    openssl rand -hex 32 > /etc/claude-proxy/token
fi
if [ ! -f /etc/claude-proxy/relay.key ]; then
    openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 365 \\
      -keyout /etc/claude-proxy/relay.key -out /etc/claude-proxy/relay.crt \\
      -subj '/CN=claude-proxy' -addext 'subjectAltName=IP:HOST_IP' \\
      -addext 'basicConstraints=critical,CA:TRUE' 2>/dev/null
fi
chmod 600 /etc/claude-proxy/token /etc/claude-proxy/relay.key
chmod 644 /etc/claude-proxy/relay.crt
""".replace("HOST_IP", host))
    for name in ["proxy.py", "wire.py", "tx_config.py", "requirements.txt"]:
        # Only source and dependency declarations travel to the server.
        subprocess.run(["scp", "-q", str(ROOT / name), "root@" + host + ":/opt/claude-proxy/" + name], check=True)
    unit = """[Unit]
Description=Claude request delta relay (authenticated HTTPS)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
DynamicUser=yes
WorkingDirectory=/opt/claude-proxy
LoadCredential=relay-token:/etc/claude-proxy/token
LoadCredential=tls-key:/etc/claude-proxy/relay.key
ExecStart=/opt/claude-proxy/.venv/bin/python /opt/claude-proxy/proxy.py server --port 8443 --token-file %d/relay-token --cert /etc/claude-proxy/relay.crt --key %d/tls-key
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
LimitCORE=0
MemoryMax=1G
UMask=0077

[Install]
WantedBy=multi-user.target
"""
    ssh(host, """
chmod 644 /opt/claude-proxy/proxy.py /opt/claude-proxy/wire.py /opt/claude-proxy/tx_config.py /opt/claude-proxy/requirements.txt
if [ ! -x /opt/claude-proxy/.venv/bin/python ]; then
    python3 -m venv /opt/claude-proxy/.venv
fi
/opt/claude-proxy/.venv/bin/pip install --disable-pip-version-check -q -r /opt/claude-proxy/requirements.txt
chmod -R a+rX /opt/claude-proxy/.venv
cat > /etc/systemd/system/claude-proxy.service <<'UNIT_EOF'
""" + unit + """UNIT_EOF
chmod 644 /etc/systemd/system/claude-proxy.service
systemctl daemon-reload
systemctl enable --now claude-proxy.service
systemctl restart claude-proxy.service
if command -v ufw >/dev/null && ufw status | head -1 | grep -q 'Status: active'; then
    ufw allow 8443/tcp comment 'Claude delta relay HTTPS'
fi
systemctl is-active claude-proxy.service
""")
    state = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "claude-proxy"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    for remote, local in [("token", "token"), ("relay.crt", "relay.crt")]:
        data = ssh(host, "cat " + shlex.quote("/etc/claude-proxy/" + remote), capture=True)
        path = state / local
        # Avoid writing credentials to a file with permissive permissions.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        path.chmod(0o600)
    path = state / "relay-url"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write("https://" + host + ":8443/v1/relay\n")
    path.chmod(0o600)
    print("Relay installed at https://" + host + ":8443/v1/relay")
    print("Client credentials saved privately under " + str(state))


if __name__ == "__main__":
    main()
