"""Temporary authenticated benchmark sidecar. Never changes the production relay."""

import argparse
import base64
import json
import os
from pathlib import Path
import ssl
import sys
import time
import zlib
import zstandard as zstd

from aiohttp import web
from yarl import URL

sys.path.insert(0, os.environ.get("BENCHMARK_PROXY_DIR", "/opt/claude-proxy"))
from proxy import CACHE, HTTP, digest, headers_without_hops, relay_response, server_app


async def run(args):
    records = []

    async def echo(request):
        body = await request.read()
        return web.json_response({"sha256": digest(body), "body_bytes": len(body)})

    mock = web.Application(client_max_size=64 * 1024 * 1024)
    mock.router.add_route("*", "/{tail:.*}", echo)
    mock_runner = web.AppRunner(mock, access_log=None)
    await mock_runner.setup()
    mock_site = web.TCPSite(mock_runner, "127.0.0.1", 0)
    await mock_site.start()
    mock_port = mock_site._server.sockets[0].getsockname()[1]
    origin = "http://127.0.0.1:" + str(mock_port)
    token = Path(args.token_file).read_text().strip()
    app = server_app(token, origin)

    @web.middleware
    async def measure(request, handler):
        if request.path not in {"/v1/relay", "/full"}:
            return await handler(request)
        started = time.perf_counter()
        body = await request.read()
        received = time.perf_counter()
        http_bytes = len(request.method) + 1 + len(request.raw_path) + len(" HTTP/1.1\r\n") + 2
        http_bytes += sum(len(k) + len(v) + 4 for k, v in request.raw_headers)
        response = await handler(request)
        records.append({"path": request.path, "upload_body_bytes": len(body),
                        "upload_http_bytes": http_bytes + len(body),
                        "server_read_ms": (received - started) * 1000,
                        "server_elapsed_ms": (time.perf_counter() - started) * 1000})
        return response

    # Authentication must run before benchmark instrumentation reads bodies.
    app.middlewares.append(measure)

    async def full(request):
        body = await request.read()
        if request.headers.get("X-Benchmark-Encoding") == "deflate":
            body = zlib.decompress(body)
        elif request.headers.get("X-Benchmark-Encoding") == "zstd":
            if zstd.frame_content_size(body) > 64 * 1024 * 1024:
                raise web.HTTPRequestEntityTooLarge(max_size=64 * 1024 * 1024, actual_size=zstd.frame_content_size(body))
            body = zstd.ZstdDecompressor().decompress(body, allow_extra_data=False)
        async with app[HTTP].post(origin + "/echo", data=body) as response:
            return await relay_response(request, response)

    async def metrics(request):
        return web.json_response(records)

    async def clear_metrics(request):
        records.clear()
        return web.Response(text="ok")

    async def reset(request):
        app[CACHE].data.clear()
        app[CACHE].size = 0
        return web.Response(text="ok")

    async def ping(request):
        return web.Response(text="ok")

    app.router.add_post("/full", full)
    app.router.add_get("/metrics", metrics)
    app.router.add_post("/clear-metrics", clear_metrics)
    app.router.add_post("/reset", reset)
    app.router.add_get("/ping", ping)

    # Live A/B traffic uses the same VPS and TLS connection type for both
    # modes. Original auth stays encrypted, in memory, and is never logged.
    live = server_app(token)

    async def live_full(request):
        meta = json.loads(base64.b64decode(request.headers["X-Benchmark-HTTP"], validate=True))
        path = meta["path"]
        if not path.startswith("/") or path.startswith("//") or "#" in path:
            return web.Response(status=400)
        body = await request.read()
        async with live[HTTP].request(
            meta["method"], URL("https://api.anthropic.com" + path, encoded=True),
            data=body if body else None,
            headers=headers_without_hops(meta["headers"], preserve_length=True),
            allow_redirects=False,
        ) as response:
            return await relay_response(request, response)

    live.router.add_post("/full", live_full)
    app.add_subapp("/live", live)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(args.cert, args.key)
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", args.port, ssl_context=tls).start()
    print("benchmark sidecar ready", flush=True)
    import asyncio
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await mock_runner.cleanup()


if __name__ == "__main__":
    import asyncio
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18443)
    parser.add_argument("--token-file", default="/etc/claude-proxy/token")
    parser.add_argument("--cert", default="/etc/claude-proxy/relay.crt")
    parser.add_argument("--key", default="/etc/claude-proxy/relay.key")
    asyncio.run(run(parser.parse_args()))
