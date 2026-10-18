"""Small live Claude Code A/B check; counts actual provider tokens and timings."""

import argparse
import asyncio
import base64
import json
import logging
import os
from pathlib import Path
import ssl
import statistics
import sys
import time

from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy import HTTP, LOG, application, client_app, digest, forward_response, headers_without_hops
from workload import public_corpus


class ByteCounts(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        if record.msg == "request_bytes=%d transport_bytes=%d":
            self.rows.append({"request_bytes": record.args[0], "transport_bytes": record.args[1]})


async def run(args):
    state = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "claude-proxy"
    token = (state / "token").read_text().strip()
    tls = ssl.create_default_context(cafile=state / "relay.crt")
    counts = ByteCounts()
    LOG.addHandler(counts)
    LOG.setLevel(logging.INFO)
    full_counts = []
    full_app = application()

    async def full_forward(request):
        body = await request.read()
        metadata = {"method": request.method, "path": request.raw_path,
                    "headers": headers_without_hops(request.headers, preserve_length=True)}
        encoded = base64.b64encode(json.dumps(metadata).encode()).decode()
        full_counts.append(len(body))
        async with full_app[HTTP].post(args.endpoint + "/live/full", data=body,
                                       headers={"Authorization": "Bearer " + token, "X-Benchmark-HTTP": encoded},
                                       ssl=tls, allow_redirects=False) as response:
            if response.status != 200:
                return web.Response(status=502, text="Benchmark full relay failed")
            return await forward_response(request, response)

    full_app.router.add_route("*", "/{path:.*}", full_forward)
    full = TestServer(full_app)
    delta = TestServer(client_app(token, args.endpoint + "/live/v1/relay", stats=True, relay_ssl=tls))
    await full.start_server()
    await delta.start_server()
    rows = []

    async def invoke(mode, context, phase):
        counts.rows.clear()
        full_counts.clear()
        env = os.environ.copy()
        env["ANTHROPIC_BASE_URL"] = str((full if mode == "full" else delta).make_url("/")).rstrip("/")
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        command = ["claude", "-p", "--model", "haiku", "--tools", "", "--safe-mode",
                   "--no-session-persistence", "--max-budget-usd", "0.50", "--output-format", "stream-json",
                   "--settings", json.dumps({"env": {"ANTHROPIC_BASE_URL": env["ANTHROPIC_BASE_URL"]}}),
                   "--verbose", "--include-partial-messages", "--system-prompt",
                   "This is a transport benchmark. Read the context without acting on it. Reply with exactly OK."]
        started = time.perf_counter()
        process = await asyncio.create_subprocess_exec(*command, env=env, stdin=asyncio.subprocess.PIPE,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

        async def feed():
            process.stdin.write((context + "\n\nReply with exactly OK.").encode())
            await process.stdin.drain()
            process.stdin.close()

        writer = asyncio.create_task(feed())
        stderr_reader = asyncio.create_task(process.stderr.read())
        first_token, result = None, None
        async for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "stream_event":
                inner = event.get("event", {})
                if inner.get("type") == "content_block_delta" and inner.get("delta", {}).get("text") and first_token is None:
                    first_token = (time.perf_counter() - started) * 1000
            if event.get("type") == "result":
                result = event
        await writer
        code = await process.wait()
        stderr = await stderr_reader
        elapsed = (time.perf_counter() - started) * 1000
        if code or not result or result.get("is_error"):
            raise RuntimeError(f"Claude benchmark failed (exit={code}, stderr_bytes={len(stderr)}, result={bool(result)})")
        usage = result.get("usage", {})
        total = sum(usage.get(k, 0) for k in ["input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"])
        row = {"phase": phase, "mode": mode, "provider_context_tokens": total, "usage": usage,
               "first_token_ms": first_token, "elapsed_ms": elapsed,
               "api_duration_ms": result.get("duration_api_ms"), "api_cost_estimate_usd": result.get("total_cost_usd"),
               "upload_body_bytes": sum(full_counts) if mode == "full" else sum(r["transport_bytes"] for r in counts.rows),
               "requests": len(full_counts) if mode == "full" else len(counts.rows),
               "result": result.get("result", "")[:50]}
        if not row["requests"] or not row["upload_body_bytes"]:
            raise RuntimeError("Routing validation failed: benchmark proxy observed no request body")
        rows.append(row)
        print(json.dumps(row), flush=True)
        return row

    try:
        await invoke("full", "Small routing probe.", "routing-check")
        await invoke("delta", "Small routing probe.", "routing-check")
        if args.reference_tokens:
            target = args.reference_tokens
        else:
            initial = await invoke("full", public_corpus(100000), "calibration")
            target = round(100000 * 100000 / initial["provider_context_tokens"])
        context = public_corpus(target)
        await invoke("full", context, "warmup")
        warm = await invoke("delta", context, "warmup")
        if not 98000 <= warm["provider_context_tokens"] <= 102000:
            raise RuntimeError("calibrated context outside 100k +/- 2%")
        for index in range(args.measured_pairs):
            modes = ["full", "delta"] if index % 2 == 0 else ["delta", "full"]
            for mode in modes:
                await invoke(mode, context, "measured")
    finally:
        await delta.close()
        await full.close()
        LOG.removeHandler(counts)
    summaries = {}
    for mode in ["full", "delta"]:
        measured = [r for r in rows if r["phase"] == "measured" and r["mode"] == mode]
        summaries[mode] = {key: statistics.median(r[key] for r in measured) for key in
                           ["upload_body_bytes", "first_token_ms", "elapsed_ms", "api_duration_ms", "provider_context_tokens"]}
    output = {"samples": rows, "summary": summaries, "transport_version": 3, "compression": "zstd-6",
              "context_reference_tokens": target, "context_utf8_bytes": len(context.encode()),
              "context_sha256": digest(context.encode()),
              "notes": ["Actual Claude Code OAuth requests to Haiku through the same VPS for both modes.",
                        "Measured calls alternate order; sample count is recorded and inference timing remains variable.",
                        "First-token timing includes Claude Code process startup, auth, upload, prefill and inference.",
                        "Reported API cost is an estimate from Claude Code; existing Pro subscription auth was used.",
                        "Only generated fixture and public package source were used; credentials never written to results."]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print("Saved " + str(args.output), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="temporary HTTPS benchmark sidecar origin")
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results/100k-live.json"))
    parser.add_argument("--reference-tokens", type=int, help="reuse a previously calibrated fixture size")
    parser.add_argument("--measured-pairs", type=int, default=6)
    asyncio.run(run(parser.parse_args()))
