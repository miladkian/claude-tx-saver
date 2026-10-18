#!/usr/bin/env bash
# Run on the VPS: bash server.sh
set -euo pipefail
VERSION=v0.2.1
REPO=mohsenyz/claude-tx-saver
docker_mode=false
if [[ ${1:-} == --docker ]]; then docker_mode=true; shift; fi
if [[ ${1:-} == --update && $# == 1 ]]; then
    [[ $EUID == 0 ]] || { echo 'Run server updates as root (or with sudo).' >&2; exit 1; }
    server_ip=$(python3 -c 'import json,urllib.parse; print(urllib.parse.urlsplit(json.load(open("/etc/claude-proxy/client.json"))["relay"]).hostname)')
    updater=$(mktemp)
    trap 'rm -f "$updater"' EXIT
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
        "https://github.com/$REPO/releases/latest/download/server.sh" -o "$updater"
    if [[ -f /etc/claude-proxy/install-mode && $(cat /etc/claude-proxy/install-mode) == docker ]]; then
        bash "$updater" --docker "$server_ip"
    else
        bash "$updater" "$server_ip"
    fi
    exit
fi

if [[ ${1:-} == --help || $# -gt 1 || ${1:-} == --* ]]; then
    echo 'Usage: sudo bash server.sh [--docker] [YOUR_PUBLIC_IPV4] | --update'
    echo 'Installs the HTTPS relay on port 8443 using systemd or Docker.'
    [[ ${1:-} == --help ]] && exit 0 || exit 2
fi
[[ $(uname -s) == Linux && $EUID == 0 ]] || { echo 'Run this on your Linux VPS as root (or with sudo).' >&2; exit 1; }
if $docker_mode; then
    docker info >/dev/null || { echo 'Install and start Docker first.' >&2; exit 1; }
else
    command -v systemctl >/dev/null || { echo 'This installer needs systemd, or use --docker.' >&2; exit 1; }
fi
# Install missing prerequisites on Debian/Ubuntu.
if ! command -v python3 >/dev/null || ! command -v curl >/dev/null || ! command -v openssl >/dev/null || ! command -v tar >/dev/null || ! python3 -m venv --help >/dev/null 2>&1 || ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
    command -v apt-get >/dev/null || { echo 'Install Python 3.10+ with venv, curl, tar and OpenSSL, then retry.' >&2; exit 1; }
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv curl tar openssl ca-certificates
fi
# Keep the existing advertised address on reinstalls; discover it only once.
server_ip=${1:-}
if [[ -z $server_ip && -f /etc/claude-proxy/client.json ]]; then
    server_ip=$(python3 -c 'import json,urllib.parse; print(urllib.parse.urlsplit(json.load(open("/etc/claude-proxy/client.json"))["relay"]).hostname)')
fi
if [[ -z $server_ip ]]; then
    echo 'Detecting public IPv4…'
    for endpoint in https://api.ipify.org https://checkip.amazonaws.com; do
        candidate=$(curl -4 --fail --silent --show-error --connect-timeout 5 --max-time 10 "$endpoint") || continue
        if server_ip=$(python3 -c 'import ipaddress,sys; ip=ipaddress.IPv4Address(sys.argv[1].strip()); assert ip.is_global; print(ip)' "$candidate" 2>/dev/null); then
            break
        fi
        server_ip=
    done
    [[ -n $server_ip ]] || { echo 'Could not detect public IPv4. Retry: bash server.sh YOUR_PUBLIC_IPV4' >&2; exit 1; }
fi
server_ip=$(python3 -c 'import ipaddress,sys; assert sys.version_info >= (3,10), "Python 3.10+ required"; print(ipaddress.IPv4Address(sys.argv[1]))' "$server_ip")
umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/source"
echo "Installing Claude TX Saver $VERSION…"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    "https://api.github.com/repos/$REPO/tarball/$VERSION" -o "$work/source.tar.gz"
tar -xzf "$work/source.tar.gz" --strip-components=1 -C "$work/source"
install -d -m 755 /opt/claude-proxy /etc/claude-proxy
for name in proxy.py wire.py tx_config.py requirements.txt server.sh; do
    install -m 644 "$work/source/$name" "/opt/claude-proxy/$name"
done
install -m 755 "$work/source/server.sh" /usr/local/bin/claude-tx-server
if $docker_mode; then
    docker build -q -t "claude-tx-saver:$VERSION" "$work/source"
else
    python3 -m venv /opt/claude-proxy/.venv
    /opt/claude-proxy/.venv/bin/python -m pip install --disable-pip-version-check -q -r /opt/claude-proxy/requirements.txt
    chmod -R a+rX /opt/claude-proxy/.venv
fi
if [[ ! -f /etc/claude-proxy/token ]]; then
    openssl rand -hex 32 > /etc/claude-proxy/token
fi
if [[ ! -f /etc/claude-proxy/relay.key && ! -f /etc/claude-proxy/relay.crt ]]; then
    openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 365 \
        -keyout /etc/claude-proxy/relay.key -out /etc/claude-proxy/relay.crt \
        -subj '/CN=claude-tx-saver' -addext "subjectAltName=IP:$server_ip" \
        -addext 'basicConstraints=critical,CA:TRUE' 2> "$work/openssl.log"
fi
openssl x509 -in /etc/claude-proxy/relay.crt -noout -checkip "$server_ip" >/dev/null || { echo 'Existing certificate does not match this IP; renew the certificate first.' >&2; exit 1; }
openssl x509 -in /etc/claude-proxy/relay.crt -noout -checkend 86400 >/dev/null || { echo 'Certificate expires within a day; renew it before installing.' >&2; exit 1; }
chmod 600 /etc/claude-proxy/token /etc/claude-proxy/relay.key
chmod 644 /etc/claude-proxy/relay.crt
# Export the three client settings into a root-only file, copied over SSH at setup.
python3 - "$server_ip" <<'PY'
import json, os, pathlib, sys
state = pathlib.Path('/etc/claude-proxy')
token = (state / 'token').read_text().strip()
if len(token) < 32:
    raise SystemExit('Existing relay token is invalid; refusing to replace it silently.')
config = {'relay': f'https://{sys.argv[1]}:8443/v1/relay', 'token': token,
          'certificate': (state / 'relay.crt').read_text()}
fd = os.open(state / 'client.json', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as output:
    json.dump(config, output)
os.chmod(state / 'client.json', 0o600)
PY
if $docker_mode; then
    # Stop only this project's other service if switching installation modes.
    if command -v systemctl >/dev/null && systemctl is-active --quiet claude-proxy.service; then
        systemctl disable --now claude-proxy.service
    fi
    docker rm -f claude-tx-saver-server >/dev/null 2>&1 || true
    docker run -d --name claude-tx-saver-server --restart unless-stopped --init \
        --read-only --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 --memory 1g \
        -p 8443:8443 -v /etc/claude-proxy:/run/credentials:ro \
        "claude-tx-saver:$VERSION" server --port 8443 --token-file /run/credentials/token \
        --cert /run/credentials/relay.crt --key /run/credentials/relay.key >/dev/null
else
    if command -v docker >/dev/null; then docker rm -f claude-tx-saver-server >/dev/null 2>&1 || true; fi
cat > /etc/systemd/system/claude-proxy.service <<'UNIT'
[Unit]
Description=Claude TX Saver HTTPS relay
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
UNIT
chmod 644 /etc/systemd/system/claude-proxy.service
systemctl daemon-reload
systemctl enable claude-proxy.service
systemctl restart claude-proxy.service
fi
if command -v ufw >/dev/null && ufw status | head -1 | grep -q 'Status: active'; then
    ufw allow 8443/tcp comment 'Claude TX Saver HTTPS'
fi
# Readiness checks the actual TLS listener, not just systemd's initial state.
curl --silent --show-error --connect-timeout 5 --max-time 10 --retry 10 --retry-connrefused --retry-delay 1 \
    --cacert /etc/claude-proxy/relay.crt --output /dev/null "https://$server_ip:8443/v1/relay"
if ! $docker_mode; then systemctl is-active --quiet claude-proxy.service; fi
if $docker_mode; then echo docker > /etc/claude-proxy/install-mode; else echo native > /etc/claude-proxy/install-mode; fi
echo "Server ready at https://$server_ip:8443. Allow TCP 8443 in your cloud firewall."
echo "On your client machine, run: bash client.sh $server_ip"
echo 'Next update: claude-tx-server --update'
