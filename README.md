# Claude TX Saver

[![Tests](https://github.com/mohsenyz/claude-tx-saver/actions/workflows/test.yml/badge.svg)](https://github.com/mohsenyz/claude-tx-saver/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Send **changed context chunks instead of the full conversation** between Claude
Code and your VPS. Lossless content-defined deduplication plus **Zstandard level
6** cut upload traffic by **97.04%** in live Claude Code tests with a 100k-token
context. No SSH tunnel; the relay uses authenticated, certificate-verified HTTPS.

```text
Claude Code → localhost:8787 → your VPS:8443 → Anthropic
             full request     changed chunks   exact original request
                              + Zstandard
```

Anthropic still receives the full context. Model limits, token billing and
prompt caching stay the same. This saves client upload bandwidth; it is not a
way to reduce input tokens. It does **not** promise faster model responses.

## Install

You need Python **3.10+**, Git, SSH/SCP, Claude Code with working authentication,
and a Linux VPS with **systemd**, root SSH access and a public IPv4 address.
Automatic client setup supports **macOS** (LaunchAgent) and **Linux** (systemd
user service). The relay and its upstream connection must be reachable from
your network. Windows background-service setup is not provided.

Prepare a Debian/Ubuntu VPS once:

```sh
ssh root@YOUR_VPS_IP 'apt-get update && apt-get install -y python3 python3-venv openssl'
```

Then, on your client machine:

```sh
git clone https://github.com/mohsenyz/claude-tx-saver.git
cd claude-tx-saver
./install.sh --host YOUR_VPS_IP
claude
```

Review the scripts before running them. Setup installs dependencies in `.venv`,
deploys the relay under `/opt/claude-proxy`, generates or reuses private transport
credentials, installs background services, and changes only
`env.ANTHROPIC_BASE_URL` in `~/.claude/settings.json` to
`http://127.0.0.1:8787`. It saves the previous base URL for uninstall. Keep the
checkout in place: the client service runs its code from that directory.
Restart existing Claude Code processes after setup.

Port **8443/tcp** must be reachable. Setup adds an allow rule if UFW is active;
configure any cloud/provider firewall yourself. It does not alter nginx or use
an SSH tunnel. Linux clients need a working `systemctl --user` session. Their
service starts when the user logs in; an administrator can enable user lingering
if it must run before login.

Your existing Claude authentication stays with Claude Code. **Do not set
`ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY` to the proxy token.** Subscription
and gateway support can depend on Claude Code/account policy; see Anthropic's
[subscription and gateway documentation](https://code.claude.com/docs/en/llm-gateway#subscriptions-and-gateways).
The live benchmark used an existing Pro OAuth login successfully.

## Benchmarks: 100,126 context tokens

Measured over a real client–VPS HTTPS connection with a public-source fixture,
calibrated using actual Claude usage counters. Every reconstructed request was
SHA-256 verified. **252 controlled measurements**, with 12 samples per
scenario/mode, plus **12 measured live Haiku calls** (six per mode).

| Scenario | Full upload | Delta + Zstd | Upload saved | Full → delta transport latency |
|---|---:|---:|---:|---:|
| First request, cold cache | 291.0 KB | 63.7 KB | 78.10% | 576 → 432 ms |
| Identical request | 291.0 KB | 1.19 KB | **99.59%** | 293 → 231 ms |
| Append ~4 KiB | 295.2 KB | 7.14 KB | 97.58% | 316 → 242 ms |
| Append ~40 KiB | 332.2 KB | 10.07 KB | 96.97% | 297 → 252 ms |
| Edit ~4 KiB in middle | 291.0 KB | 13.09 KB | 95.50% | 288 → 246 ms |
| Change session metadata | 291.0 KB | 4.60 KB | 98.42% | 291 → 219 ms |
| Relay cache lost | 291.0 KB | 64.91 KB | 77.69% | 297 → 478 ms |

KB means 1,000 bytes; KiB means 1,024 bytes. Controlled upload sizes include HTTP
headers but exclude TLS/TCP/IP overhead. Latencies are medians to the first
response byte from a mock upstream, excluding inference. Cache loss includes
the extra synchronization round trip.

**Compression won for latency; delta won for bandwidth on this link.** For an
identical request, compressing the entire body with Zstd alone used **62.4 KB**
and took **211 ms**, versus **1.19 KB / 231 ms** for delta + Zstd. Chunk scanning
cost about **48.5 ms** locally. A slower upload link can favor delta more, but
that is not a measured result here. Compression-only is a benchmark baseline;
the shipped production transport uses delta + Zstd.

In actual Claude Code calls, median upload bodies fell from **298.5 KB to
8.85 KB (97.04%)**. First-token medians were **1.753 s full vs 1.878 s delta**;
the small sample did **not** establish a latency benefit (95% interval for the
improvement: −369 to +198 ms). All measured live calls used a warm provider
prompt cache. Long generated responses were not tested; savings concern uploads.

Zstd 6 was selected after comparing Zstd levels 1/3/6/9 and Brotli 4/5/6/9.
A cold binary envelope was **63.4 KB / 2.94 ms encoding**, versus
**119.3 KB / 6.25 ms** with the earlier Deflate/base64 format. Brotli produced
slightly smaller envelopes but took more encoding CPU.

[Full report and confidence intervals](benchmarks/results/REPORT.md) ·
[Raw measurements](benchmarks/results) · [Reproduce](benchmarks/README.md)

## Manual setup and configuration

For separate deployment steps:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python deploy.py --host YOUR_VPS_IP
.venv/bin/python install_client.py
```

The deployer currently targets root SSH on a public IPv4 address and relay port
8443. Other server layouts can run `proxy.py server --help` manually. SSH uses
your normal keys, host verification and configuration. No password is stored.

The saved relay URL is in `~/.config/claude-proxy/relay-url`. Override it with
`CLAUDE_TX_RELAY` or `--relay https://YOUR_HOST:8443/v1/relay`. The transport token
and trusted certificate are sibling files named `token` and `relay.crt`. State
respects `$XDG_CONFIG_HOME` on both supported client platforms.

To run a client without a background service or changing Claude settings:

```sh
./client.sh
# In another terminal:
./claude-via-proxy.sh
```

Stop an installed client before running a manual one on the same port. For a
custom local port, use `install_client.py --port 8788`, or pass `--port` to
`client.sh` and select that base URL explicitly in Claude Code settings.

For manual deployments, the runtime can also be installed from this checkout:

```sh
python3 -m pip install .
claude-tx-saver --help
```

The automatic installer uses the checkout scripts, not a PyPI release.

## Operation, upgrades and uninstall

```sh
# macOS client status/logs
launchctl print "gui/$(id -u)/local.claude-proxy.client"
tail -f ~/.config/claude-proxy/client.log

# Linux client status/logs
systemctl --user status claude-tx-saver
journalctl --user -u claude-tx-saver

# VPS status
ssh root@YOUR_VPS_IP systemctl status claude-proxy

# Remove local service and restore the previous Claude base URL
.venv/bin/python install_client.py --uninstall

# Stop/remove the relay from startup separately
ssh root@YOUR_VPS_IP systemctl disable --now claude-proxy
```

Uninstall preserves unrelated Claude settings, subsequent user base-URL changes,
and private credential files. Relay credentials and the server installation are
retained. Delete those separately only when no clients depend on them.

To upgrade, stop active Claude sessions, pull the checkout, and rerun
`./install.sh --host YOUR_VPS_IP`. Both proxies must use the same wire version
(currently **v3**); upgrading causes a short interruption and clears the relay
cache. Existing transport credentials are reused. The generated TLS certificate
expires after **365 days**; renew it and securely distribute the new trust anchor
before expiry. Never disable certificate verification.

## Transparency and boundaries

The proxy treats Anthropic bodies and responses as opaque bytes. It does not
parse model names, tools, thinking signatures, authentication formats or SSE
events. Unknown HTTP endpoints, query parameters, binary bodies, duplicate
headers, upstream errors, redirects and incremental SSE are covered by tests.
Host and connection/framing headers are handled per connection.

Request chunks use content-defined boundaries and SHA-256 addresses. Binary
Zstandard frames avoid base64 and validate checksums and declared decompressed
sizes. The relay has a **256 MiB in-memory LRU chunk budget**. Cache loss triggers
one resynchronization before upstream delivery; ambiguous upstream failures are
never automatically replayed by the proxies. Claude Code retains its own retries.

Both hosts buffer full request bodies (default limit **64 MiB**, configurable
with `--max-body-mib`). There is no idle response timeout by default;
`--read-timeout` can set one. Responses stream without full buffering. Ordinary
HTTP and SSE are supported; WebSockets/upgrades, trailers and streaming request
uploads are not. Upstream HTTP uses HTTP/1.1.

**Your VPS can read your context and Anthropic credentials.** Use a trusted
server. The client listener binds only to loopback. See [SECURITY.md](SECURITY.md)
for trust boundaries and credential handling.

## Development

```sh
.venv/bin/python -m unittest discover -v
```

Tests run against local mock servers without Anthropic credentials. See
[CONTRIBUTING.md](CONTRIBUTING.md). Independent project; not affiliated with or
endorsed by Anthropic. [MIT license](LICENSE).
