"""Measure real-link upload bytes and latency using the production proxy factories."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import ssl
import statistics
import sys
import time
import zstandard as zstd

import aiohttp
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy import RESPONSE_LENGTH, chunks, client_app, digest, pack
from workload import corpus, public_corpus, request_body


def percentile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lower = int(position)
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (position - lower)


def summary(rows):
    result = {}
    for case in sorted({row["case"] for row in rows}):
        result[case] = {}
        for mode in ["full", "full_zstd", "delta"]:
            samples = [r for r in rows if r["case"] == case and r["mode"] == mode]
            result[case][mode] = {
                "n": len(samples),
                "body_bytes_median": statistics.median(r["body_bytes"] for r in samples),
                "upload_body_bytes_median": statistics.median(r["upload_body_bytes"] for r in samples),
                "upload_http_bytes_median": statistics.median(r["upload_http_bytes"] for r in samples),
                "ttfb_ms_median": statistics.median(r["ttfb_ms"] for r in samples),
                "ttfb_ms_p95": percentile([r["ttfb_ms"] for r in samples], .95),
                "elapsed_ms_median": statistics.median(r["elapsed_ms"] for r in samples),
                "server_ms_median": statistics.median(r["server_ms"] for r in samples),
                "attempts_median": statistics.median(r["attempts"] for r in samples),
            }
        full, delta = result[case]["full"], result[case]["delta"]
        result[case]["reduction"] = {
            "upload_body_percent": 100 * (1 - delta["upload_body_bytes_median"] / full["upload_body_bytes_median"]),
            "upload_http_percent": 100 * (1 - delta["upload_http_bytes_median"] / full["upload_http_bytes_median"]),
            "ttfb_percent": 100 * (1 - delta["ttfb_ms_median"] / full["ttfb_ms_median"]),
            "ttfb_ms": full["ttfb_ms_median"] - delta["ttfb_ms_median"],
        }
    return result


async def run(args):
    started_at = datetime.now(timezone.utc).isoformat()
    state = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "claude-proxy"
    token = (state / "token").read_text().strip()
    tls = ssl.create_default_context(cafile=state / "relay.crt")
    context = public_corpus(args.tokens) if args.workload == "public-code" else corpus(args.tokens)
    base_body = request_body(context)
    timeout = aiohttp.ClientTimeout(total=120)
    rows, pings = [], []
    rng = random.Random(391)
    async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False) as network, aiohttp.ClientSession(timeout=timeout) as local:
        async def control(path, method="POST"):
            async with network.request(method, args.endpoint + path, headers={"Authorization": "Bearer " + token}, ssl=tls) as response:
                response.raise_for_status()
                if path == "/metrics":
                    return await response.json()
                await response.read()

        async def new_client():
            server = TestServer(client_app(token, args.endpoint + "/v1/relay", relay_ssl=tls))
            await server.start_server()
            # Prime TCP/TLS independently of the 100k-context chunk cache.
            async with local.post(server.make_url("/warmup"), data=b"") as response:
                response.raise_for_status()
                await response.read()
            return server

        async def measure(mode, body, client, cold=False):
            direct = network
            if cold and mode != "delta":
                # Match the fresh delta connection's congestion-window state,
                # not just its completed TLS handshake.
                direct = aiohttp.ClientSession(timeout=timeout, auto_decompress=False)
                async with direct.get(args.endpoint + "/ping", headers={"Authorization": "Bearer " + token}, ssl=tls) as warm:
                    warm.raise_for_status()
                    await warm.read()
            await control("/clear-metrics")
            started = time.perf_counter()
            if mode == "delta":
                manager = local.post(client.make_url("/echo"), data=body,
                                     headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01"})
            else:
                data = body if mode == "full" else zstd.ZstdCompressor(level=6, write_checksum=True).compress(body)
                headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
                if mode == "full_zstd":
                    headers["X-Benchmark-Encoding"] = "zstd"
                manager = direct.post(args.endpoint + "/full", data=data, headers=headers, ssl=tls)
            async with manager as response:
                response.raise_for_status()
                if mode != "delta":
                    size = RESPONSE_LENGTH.unpack(await response.content.readexactly(4))[0]
                    metadata = json.loads(await response.content.readexactly(size))
                    assert metadata["status"] == 200
                first = await response.content.read(1)
                ttfb = (time.perf_counter() - started) * 1000
                data = first + await response.read()
                elapsed = (time.perf_counter() - started) * 1000
            if direct is not network:
                await direct.close()
            result = json.loads(data)
            assert result["sha256"] == digest(body), "remote reconstruction mismatch"
            assert result["body_bytes"] == len(body)
            metrics = await control("/metrics", "GET")
            return {"body_bytes": len(body), "ttfb_ms": ttfb, "elapsed_ms": elapsed,
                    "upload_body_bytes": sum(r["upload_body_bytes"] for r in metrics),
                    "upload_http_bytes": sum(r["upload_http_bytes"] for r in metrics),
                    "server_ms": sum(r["server_elapsed_ms"] for r in metrics),
                    "attempts": len(metrics)}

        for _ in range(12):
            started = time.perf_counter()
            await control("/ping", "GET")
            pings.append((time.perf_counter() - started) * 1000)
        cases = ["cold", "unchanged", "append_1k", "tool_10k", "edit_1k", "metadata_churn", "cache_loss"]
        if args.cases:
            cases = args.cases.split(",")
        for case in cases:
            await control("/reset")
            client = await new_client()
            try:
                if case != "cold":
                    await measure("delta", base_body, client)
                for trial in range(args.repetitions):
                    if case == "append_1k":
                        body = request_body(context, corpus(1000, seed=3000 + trial))
                    elif case == "tool_10k":
                        body = request_body(context, corpus(10000, seed=5000 + trial))
                    elif case == "edit_1k":
                        middle = len(context) // 2
                        body = request_body(context[:middle] + corpus(1000, seed=7000 + trial) + context[middle + 4000:])
                    elif case == "metadata_churn":
                        body = request_body(context, metadata="changed-session-" + str(trial))
                    else:
                        body = base_body
                    modes = ["full", "full_zstd", "delta"]
                    rng.shuffle(modes)
                    for mode in modes:
                        if mode == "delta" and case == "cold":
                            await client.close()
                            client = await new_client()
                            await control("/reset")
                        if mode == "delta" and case == "cache_loss":
                            await control("/reset")
                        row = await measure(mode, body, client, cold=case == "cold")
                        row.update({"case": case, "mode": mode, "trial": trial})
                        rows.append(row)
                    if (trial + 1) % 3 == 0:
                        print(f"{case}: {trial + 1}/{args.repetitions}", flush=True)
                print(json.dumps({case: summary([r for r in rows if r["case"] == case])[case]}), flush=True)
            finally:
                await client.close()
    # Cost of chunk scanning alone, without networking or JSON/compression.
    cpu = []
    for _ in range(12):
        started = time.perf_counter()
        list(chunks(base_body))
        cpu.append((time.perf_counter() - started) * 1000)
    result = {"workload": args.workload, "reference_tokenizer": "cl100k_base", "context_reference_tokens": args.tokens,
              "transport_version": 3, "compression_baseline": "zstd-6",
              "started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(),
              "python_version": sys.version, "aiohttp_version": aiohttp.__version__,
              "context_utf8_bytes": len(context.encode()), "request_body_bytes": len(base_body),
              "endpoint": args.endpoint, "repetitions": args.repetitions,
              "ping_ms_median": statistics.median(pings), "ping_ms_samples": pings,
              "chunk_scan_ms_median": statistics.median(cpu),
              "summary": summary(rows), "samples": rows,
              "context_sha256": digest(context.encode()),
              "notes": ["Reference tokens are not Claude token counts; workload source recorded separately.",
                        "TTFB includes local proxy processing and real network transfer, excludes model inference.",
                        "TCP/TLS connections are warmed equally; cold means cold chunk cache.",
                        "Upload HTTP bytes include request line and headers, exclude TLS/TCP/IP overhead.",
                        "All reconstructed bodies verified by remote SHA-256.",
                        "Mode order is shuffled per trial to reduce timing-order bias."]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print("Saved " + str(args.output), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="temporary HTTPS benchmark sidecar origin")
    parser.add_argument("--tokens", type=int, default=100000)
    parser.add_argument("--workload", choices=["public-code", "synthetic"], default="public-code")
    parser.add_argument("--repetitions", type=int, default=12)
    parser.add_argument("--cases")
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results/100k-transport.json"))
    asyncio.run(run(parser.parse_args()))
