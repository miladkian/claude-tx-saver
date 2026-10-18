# Contributing

Use Python 3.10+ and a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -v
bash -n server.sh client.sh
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

The two public installers are `server.sh` and `client.sh`. Keep their pinned
`VERSION` equal to the release tag. `install_client.py` handles native/Docker
service setup and settings rollback internally. Docker images are built locally;
no registry account is required. Test containers with `docker build -t claude-tx-saver:test .`.

For real container checks:

```sh
docker build -t claude-tx-saver:test .
RUN_DOCKER_TESTS=1 .venv/bin/python -m unittest test_docker -v
docker build -f tests/Dockerfile.bootstrap -t claude-tx-saver:bootstrap-test tests
docker run --rm -v "$PWD:/source:ro" claude-tx-saver:bootstrap-test
```

The bootstrap harness runs only in a disposable container and owns its `/opt` and
`/etc` fixtures. It tests real installs, credentials and rollback, substituting
archive retrieval, SSH and systemd supervision. It never changes host services.
