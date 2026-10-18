# Claude TX Saver 📦

[![Tests](https://github.com/mohsenyz/claude-tx-saver/actions/workflows/test.yml/badge.svg)](https://github.com/mohsenyz/claude-tx-saver/actions/workflows/test.yml)
[![MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Your conversation grew by one sentence. Why upload the whole novel again?**

Claude Code keeps sending the full conversation as it grows, eating upload
bandwidth on every turn. The goal: **send the changes, save your connection**,
and leave Claude's behavior alone.

Two proxies, content-defined chunks, and Zstandard. Claude Code talks normally;
your VPS rebuilds the exact request and forwards it to Anthropic.

```text
First turn:   [100k tokens of context] ── compressed ──▶ VPS remembers chunks
Next turn:    [same context + “fix it”] ── just changes ▶ VPS rebuilds everything
                                                      ↓
                                                   Anthropic
```

## Two scripts. Done.

**On your VPS** (Linux, as root):

```sh
curl -fsSLO https://github.com/mohsenyz/claude-tx-saver/releases/latest/download/server.sh
bash server.sh
```

**On your Mac or Linux machine** (with Claude Code + Python 3.10+ installed):

```sh
curl -fsSLO https://github.com/mohsenyz/claude-tx-saver/releases/latest/download/client.sh
bash client.sh YOUR_VPS_IP
claude
```

Prefer containers? Add **`--docker`** on both machines:

```sh
bash server.sh --docker   # VPS
bash client.sh --docker YOUR_VPS_IP   # your machine
```

Scripts download a pinned release, install dependencies, and start background
services. No clone, pip commands, or hand-edited config. Review the downloaded
scripts before running. Docker mode builds the included [Dockerfile](Dockerfile);
it needs a running Docker daemon, plus Python for setup.

The server detects its public IPv4 and prints the client command. Override detection
with `bash server.sh YOUR_VPS_IP` if needed (for example, behind NAT).
**No domain, certificate purchase, or TLS configuration.** The scripts generate
and trust a self-signed certificate automatically.
Use a public **IPv4** address, working `root@IP` SSH access, and allow **TCP 8443**
in your cloud firewall. Native mode uses systemd on the VPS and a LaunchAgent /
systemd user service locally. SSH copies credentials during setup; **traffic uses
certificate-verified HTTPS, never an SSH tunnel**. Your Claude login stays intact.
Restart already-open Claude sessions after setup.

## Does it work?

Yes, for bandwidth. Speed has a plot twist.

| 100k-context request | Full upload | Delta + Zstd | Saved |
|---|---:|---:|---:|
| Actual Claude Code | 298.5 KB | 8.85 KB | **97.04%** |
| Identical request | 291 KB | 1.19 KB | **99.59%** |
| Small appended turn | 295 KB | 7.14 KB | **97.58%** |

**Compression alone was faster on our link:** 211 ms versus 231 ms with delta
for an identical request. Delta saved far more bytes but spent ~48 ms scanning
chunks. Live first-token medians were 1.753 s full / 1.878 s delta: **no proven
latency win**. A cold cache sends all chunks; cache loss adds a round trip.

Measured with **100,126 actual context tokens**, 252 controlled measurements and
12 live Haiku calls. KB = 1,000 bytes. Upload savings only; Anthropic still gets
the full context, so **token bills do not shrink**.
[Results, methodology & raw data](benchmarks/results/REPORT.md) · [Reproduce](benchmarks/README.md)

## The small print, actually small

- Your VPS sees context and Anthropic credentials. Use a server you trust.
- Only the base URL changes. Bodies, headers, errors and SSE stay opaque.
- Native client listens on loopback; Docker publishes only `127.0.0.1:8787`.
- Credentials: `~/.config/claude-proxy/` and `/etc/claude-proxy/`. TLS expires in
  365 days; renew and redistribute the certificate before then.
- Supports HTTP + SSE. Buffers requests, streams responses. No WebSocket support.

Uninstall locally: **`bash client.sh --uninstall`**. It restores your old Claude
base URL. To stop the server: `systemctl disable --now claude-proxy`, or
`docker rm -f claude-tx-saver-server`. Credentials are kept.

## Updates without the ceremony

```sh
claude-tx-server --update                 # VPS, as root; run this first
~/.local/bin/claude-tx-client --update     # your machine
```

Fetches the latest release and remembers your server IP, native/Docker mode,
credentials, and Claude settings. Update both ends together; active sessions
may briefly disconnect. Client updates need the same SSH access as setup.

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [MIT](LICENSE)
Independent project; not affiliated with Anthropic.
