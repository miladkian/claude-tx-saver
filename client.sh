#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
state_dir="${XDG_CONFIG_HOME:-$HOME/.config}/claude-proxy"
exec "$project_dir/.venv/bin/python" "$project_dir/proxy.py" client \
    --token-file "$state_dir/token" --relay-ca "$state_dir/relay.crt" --stats "$@"
