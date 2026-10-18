# Claude TX Saver

The relay is implemented in Rust for a small, self-contained deployment binary.
The wire protocol remains version 3, so existing clients can be upgraded without
changing credentials or the Claude Code integration.

<p align="center"><strong>Stop uploading the same Claude Code context on every turn.</strong></p>
<p align="center"><a href="https://github.com/mohsenyz/claude-tx-saver/actions/workflows/test.yml"><img src="https://github.com/mohsenyz/claude-tx-saver/actions/workflows/test.yml/badge.svg" alt="CI"></a> <a href="https://github.com/mohsenyz/claude-tx-saver/releases"><img src="https://img.shields.io/github/v/release/mohsenyz/claude-tx-saver?sort=semver" alt="Latest release"></a> <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a></p>

Claude Code resends its growing conversation. On a slow or metered connection,
that means paying to upload the same context again and again. Claude TX Saver
sends changed chunks over an encrypted link to a VPS, rebuilds the exact request,
and lets Claude continue as normal.

<p align="center"><img src="docs/architecture.svg" alt="Claude TX Saver architecture" width="900"></p>

## Quick start

### Install a prebuilt release

Release artifacts are published for Linux x86_64 and macOS Apple Silicon. The
`claude-tx-saver` binary provides both `server` and `client` modes; the shell
installers below configure TLS, credentials, and Claude Code settings around it.

**1. On your Linux VPS, as root:**

```sh
curl -fsSL https://github.com/mohsenyz/claude-tx-saver/releases/latest/download/claude-tx-saver-linux-x86_64.tar.gz | tar xz
install -m 755 claude-tx-saver /usr/local/bin/claude-tx-saver
```

The server detects its public IPv4, creates a self-signed certificate, and prints
the next command. No domain, certificate purchase, or TLS setup is needed.

**2. On your Mac or Linux machine:**

Use the release binary on the client and start it with `client --relay https://YOUR_VPS_IP:8443/v1/relay --token-file TOKEN --relay-ca relay.crt --port 8787`. Configure Claude Code to use `http://127.0.0.1:8787`.

The client uses authenticated HTTPS; there is no SSH tunnel. Restart any Claude Code session that was already open.

For a source build, install Rust stable (1.90 or newer) and run `cargo build --release`.
The resulting `target/release/claude-tx-saver` accepts the same `client` and
`server` arguments documented by `--help`.

### Docker

Build the Rust image and run the same binary modes. The server publishes HTTPS on
port `8443`; the client listens on `127.0.0.1:8787`.

```sh
docker build -t claude-tx-saver .
docker run --rm claude-tx-saver --help
```

## What changes on the wire?

```text
first turn:  [full context] ── compressed chunks ──▶ VPS cache
next turn:   [same context + “fix it”] ── changed chunks ──▶ VPS rebuilds full request
                                                              └─▶ Anthropic
```

Anthropic receives the original request. The proxy does not understand or rewrite
models, tools, thinking blocks, authentication formats, headers, or SSE events.

## Measured result

Real Claude Code calls with **100,126 context tokens**:

<p align="center"><img src="docs/bandwidth.svg" alt="298.5 KB full request versus 8.85 KB delta request" width="780"></p>

| Scenario | Full upload | Delta + Zstd | Reduction |
|---|---:|---:|---:|
| Live Claude Code | 298.5 KB | 8.85 KB | **97.04%** |
| Identical context | 291 KB | 1.19 KB | **99.59%** |
| Small appended turn | 295 KB | 7.14 KB | **97.58%** |

Delta saves the bandwidth. Compression alone was faster on the tested link
(211 ms versus 231 ms for an identical request), because scanning the context costs
about 48 ms. Live first-token timing showed no reliable speedup. Input tokens and
Anthropic billing do not change.

[Full report](benchmarks/results/REPORT.md) · [Methodology and reproduction](benchmarks/README.md)

The benchmark harness is intentionally retained so Rust releases can be compared
with the protocol baseline. Use `cargo bench` or the workload tools in your own benchmark harness for local and live scenarios.

## Updates and removal

Download the new tagged binary and replace the previous executable. Stop the
server with `systemctl disable --now claude-proxy`. Remove the client process and
restore Claude Code's previous base URL from its settings.

## Trust boundary

Your VPS can read the full context and Anthropic authentication headers. Use a
server you trust. Credentials live outside this checkout in
`~/.config/claude-proxy/` and `/etc/claude-proxy/`. Requests are buffered; responses
stream. HTTP and SSE are supported. WebSockets, trailers, and streaming request
uploads are outside scope. See [SECURITY.md](SECURITY.md).

[Contributing](CONTRIBUTING.md) · [MIT license](LICENSE)

Independent project; not affiliated with Anthropic.
