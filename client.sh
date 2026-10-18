#!/usr/bin/env bash
# Run on your Mac/Linux desktop: bash client.sh YOUR_VPS_IPV4
set -euo pipefail
VERSION=v0.2.1
REPO=mohsenyz/claude-tx-saver
install_dir="${XDG_DATA_HOME:-$HOME/.local/share}/claude-tx-saver"
state_dir="${XDG_CONFIG_HOME:-$HOME/.config}/claude-proxy"
docker_mode=false
if [[ ${1:-} == --docker ]]; then docker_mode=true; shift; fi
if [[ ${1:-} == --update && $# == 1 ]]; then
    server_ip=$(python3 -c 'import pathlib,sys,urllib.parse; print(urllib.parse.urlsplit(pathlib.Path(sys.argv[1]).read_text().strip()).hostname)' "$state_dir/relay-url")
    updater=$(mktemp)
    trap 'rm -f "$updater"' EXIT
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
        "https://github.com/$REPO/releases/latest/download/client.sh" -o "$updater"
    if [[ -f "$state_dir/client-mode" && $(cat "$state_dir/client-mode") == docker ]]; then
        bash "$updater" --docker "$server_ip"
    else
        bash "$updater" "$server_ip"
    fi
    exit
fi

if [[ ${1:-} == --help || $# != 1 ]]; then
    echo 'Usage: bash client.sh [--docker] YOUR_VPS_IPV4 | --update | --uninstall'
    echo 'Installs the local proxy and configures Claude Code. SSH is used once to copy credentials.'
    [[ ${1:-} == --help ]] && exit 0 || exit 2
fi
if [[ $1 == --uninstall ]]; then
    [[ -f "$install_dir/install_client.py" ]] || { echo 'Client is not installed.' >&2; exit 1; }
    exec python3 "$install_dir/install_client.py" --uninstall
fi
case $(uname -s) in
    Darwin) ;;
    Linux) if ! $docker_mode; then systemctl --user show-environment >/dev/null || { echo 'A working systemd user session is required, or use --docker.' >&2; exit 1; }; fi ;;
    *) echo 'Client installer supports macOS and Linux.' >&2; exit 1 ;;
esac
for tool in python3 curl tar ssh claude; do
    command -v "$tool" >/dev/null || { echo "Missing $tool. Install it and retry (macOS Python: brew install python)." >&2; exit 1; }
done
if $docker_mode; then docker info >/dev/null || { echo 'Install and start Docker first.' >&2; exit 1; }; fi
server_ip=$(python3 -c 'import ipaddress,sys; assert sys.version_info >= (3,10), "Python 3.10+ required"; print(ipaddress.IPv4Address(sys.argv[1].removeprefix("root@")))' "$1")
python3 -m venv --help >/dev/null 2>&1 || { echo 'Install Python venv support first (Ubuntu: sudo apt-get install python3-venv).' >&2; exit 1; }
umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/source"
echo "Installing Claude TX Saver $VERSION…"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    "https://api.github.com/repos/$REPO/tarball/$VERSION" -o "$work/source.tar.gz"
tar -xzf "$work/source.tar.gz" --strip-components=1 -C "$work/source"
echo 'Copying transport credentials over SSH (setup only; no tunnel)…'
ssh -n -o ConnectTimeout=15 "root@$server_ip" 'cat /etc/claude-proxy/client.json' > "$work/client.json"
# Validate everything before writing credentials or changing Claude settings.
python3 - "$work/client.json" "$server_ip" "$state_dir" "$work/source" <<'PY'
import json, pathlib, ssl, sys
sys.path.insert(0, sys.argv[4])
from install_client import read_settings, write_private
from tx_config import validate_relay
config = json.loads(pathlib.Path(sys.argv[1]).read_text())
expected = f'https://{sys.argv[2]}:8443/v1/relay'
if validate_relay(config['relay']) != expected:
    raise SystemExit('Server configuration has a different relay URL.')
if not isinstance(config['token'], str) or len(config['token']) < 32:
    raise SystemExit('Invalid relay token.')
ssl.create_default_context(cadata=config['certificate'])
read_settings(pathlib.Path.home() / '.claude' / 'settings.json')
state = pathlib.Path(sys.argv[3])
state.mkdir(parents=True, exist_ok=True, mode=0o700)
state.chmod(0o700)
for name, value in [('token', config['token']), ('relay.crt', config['certificate']), ('relay-url', expected)]:
    write_private(state / name, (value + '\n').encode())
PY
mkdir -p "$install_dir"
for name in proxy.py wire.py tx_config.py install_client.py requirements.txt Dockerfile .dockerignore client.sh; do
    cp "$work/source/$name" "$install_dir/$name"
done
mkdir -p "$HOME/.local/bin"
chmod 755 "$install_dir/client.sh"
ln -sf "$install_dir/client.sh" "$HOME/.local/bin/claude-tx-client"
if $docker_mode; then
    docker build -q -t "claude-tx-saver:$VERSION" "$install_dir"
    installer_python=python3
else
    python3 -m venv "$install_dir/.venv"
    "$install_dir/.venv/bin/python" -m pip install --disable-pip-version-check -q -r "$install_dir/requirements.txt"
    installer_python="$install_dir/.venv/bin/python"
fi
# Verify public HTTPS and token before starting the local service.
"$installer_python" - "$state_dir" <<'PY'
import pathlib, ssl, sys, urllib.request
state = pathlib.Path(sys.argv[1])
request = urllib.request.Request((state / 'relay-url').read_text().strip(), data=b'invalid',
    headers={'Authorization': 'Bearer ' + (state / 'token').read_text().strip()})
try:
    urllib.request.urlopen(request, context=ssl.create_default_context(cafile=state / 'relay.crt'), timeout=15)
except urllib.error.HTTPError as error:
    if error.code != 400:
        raise SystemExit(f'Relay check failed: HTTP {error.code}')
else:
    raise SystemExit('Unexpected relay response.')
PY
if $docker_mode; then
    "$installer_python" "$install_dir/install_client.py" --docker --image "claude-tx-saver:$VERSION"
else
    "$installer_python" "$install_dir/install_client.py"
fi
echo 'Done. Restart any open Claude Code sessions, then run: claude'
echo 'Next update: ~/.local/bin/claude-tx-client --update'
