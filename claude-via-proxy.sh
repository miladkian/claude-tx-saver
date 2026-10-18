#!/bin/sh
# Per-invocation CLI settings override a different saved base URL as well.
set -eu
exec claude --settings '{"env":{"ANTHROPIC_BASE_URL":"http://127.0.0.1:8787"}}' "$@"
