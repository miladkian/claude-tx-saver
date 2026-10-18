# 100k-context benchmarks

[Measured results](results/REPORT.md) include raw JSON, methodology, and limits.
No private project content is used: the primary fixture contains public aiohttp
source. Claude Code's reported input/cache token usage calibrates its size to
100,126 actual context tokens. The reference tokenizer's count is different.

The transport matrix measures twelve trials for each of seven scenarios, with
three modes: uncompressed full upload, full Zstandard-6 upload, and deduplicated
binary Zstandard-6 upload. Mode order is randomized per trial. Reconstructed
requests are verified by SHA-256. Cold trials use fresh TCP connections for
every mode, with one small request to complete TCP/TLS before measurement.
Warm trials prime the chunk cache first. Cache loss is tested separately and
includes the extra synchronization round trip.

`codecs.py` compares Deflate, Zstandard levels 1/3/6/9, and Brotli qualities
4/5/6/9, including framing and encoding work. Compression tests include cold,
unchanged, and appended payloads. `test_wire.py` checks corruption, truncation,
size bounds, invalid descriptors, and arbitrary binary content.

`live.py` compares actual Claude Code calls through the same VPS, alternating
mode order. It uses existing subscription auth, enables no extra-usage option,
and creates no API key. Each call must be observed by the selected test proxy;
CLI `--settings` overrides the saved base URL without editing user settings.
Token calibration and warmups are excluded from latency comparisons. The
response is deliberately tiny so upload/prefill timing dominates output time.

## Reproduce

Install benchmark-only dependencies locally:

```sh
.venv/bin/pip install -r benchmarks/requirements.txt
```

The following commands change the specified VPS and expose a temporary endpoint;
run them only with authorization. Port 18443 must be free and reachable. The
sidecar uses the existing private relay token and trusted TLS certificate. It
does not add routes to the production relay or change nginx configuration.

```sh
ssh root@YOUR_VPS_IP 'install -d -m 755 /run/claude-proxy-benchmark-code'
scp proxy.py wire.py tx_config.py root@YOUR_VPS_IP:/run/claude-proxy-benchmark-code/
scp benchmarks/remote.py root@YOUR_VPS_IP:/run/claude-proxy-benchmark.py
ssh root@YOUR_VPS_IP 'systemd-run --unit=claude-proxy-benchmark-v3 --collect --property=RuntimeMaxSec=3600 --property=LimitCORE=0 --setenv=BENCHMARK_PROXY_DIR=/run/claude-proxy-benchmark-code /opt/claude-proxy/.venv/bin/python /run/claude-proxy-benchmark.py'

# Calibrate and run live calls (uses subscription allowance).
.venv/bin/python benchmarks/live.py --endpoint https://YOUR_VPS_IP:18443

# This run's calibrated reference count was 59,647 for 100,126 provider tokens.
.venv/bin/python benchmarks/run.py --endpoint https://YOUR_VPS_IP:18443 --tokens 59647 --repetitions 12 --output benchmarks/results/100k-public-code.json
.venv/bin/python benchmarks/codecs.py
.venv/bin/python benchmarks/report.py

# Remove the temporary service and files after testing.
ssh root@YOUR_VPS_IP 'systemctl stop claude-proxy-benchmark-v3'
ssh root@YOUR_VPS_IP 'rm -rf /run/claude-proxy-benchmark-code /run/claude-proxy-benchmark.py'
```

Install current production requirements on the VPS before starting the sidecar
if its environment lacks Zstandard. Always read a fresh calibration result's
`context_reference_tokens` value for a new environment rather than assuming
59,647. Package versions and model tokenizers can change.

The current public-code and live JSON files contain the final Zstandard run.
Files labeled `v2` retain earlier Deflate/base64 pilot results. The initial
`live-calibration` file's route comparison was invalid and is labeled explicitly;
only its provider token count was used for calibration. An earlier cold-cache
pilot used different TCP congestion-window histories and is not used in the
final comparison.

Public result files redact the original relay hostname. Timing and byte measurements are unchanged.
