# Contributing

Use Python 3.10+ and a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -v
sh -n install.sh client.sh claude-via-proxy.sh
```

Tests use local mock HTTP servers. No VPS, Anthropic credential, or paid API
request is needed. Keep dependencies in `pyproject.toml` and `requirements.txt`
in sync. Use `benchmarks/requirements.txt` only for benchmark tools.

Preserve opaque request/response bodies, unknown endpoints and headers, upstream
status codes, duplicate headers, and incremental SSE delivery. Add regression
coverage for protocol behavior. Never automatically replay a request whose
upstream delivery is uncertain. Transport v3 changes require upgrading both
proxies together; changes must not depend on Anthropic JSON/SSE field names.

Open an issue or pull request explaining the behavior change and validation.
Live benchmarks are opt-in, consume account allowance, and require permission
to deploy their temporary authenticated sidecar. Do not commit credentials,
request captures, user contexts, or host-specific configuration.
