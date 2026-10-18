#!/bin/sh
# Run from a reviewed checkout. Deploys the VPS relay and installs a user service.
set -eu
if [ "$#" -ne 2 ] || [ "$1" != "--host" ]; then
    echo "Usage: ./install.sh --host YOUR_VPS_IPV4" >&2
    exit 2
fi
case "$(uname -s)" in
    Darwin|Linux) ;;
    *) echo "Automatic setup supports macOS and Linux." >&2; exit 2 ;;
esac
command -v python3 >/dev/null
command -v ssh >/dev/null
command -v scp >/dev/null
command -v claude >/dev/null
if [ "$(uname -s)" = Linux ]; then
    systemctl --user show-environment >/dev/null
fi
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$project_dir"
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
.venv/bin/python deploy.py --host "$2"
.venv/bin/python install_client.py
