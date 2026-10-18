"""Lossless, content-addressed request transport for an Anthropic HTTP proxy."""

import argparse
import asyncio
from collections import OrderedDict
import hashlib
import json
import logging
from pathlib import Path
import re
import secrets
import ssl
import struct

import aiohttp
from aiohttp import web
from yarl import URL
from wire import pack, unpack
from tx_config import configured_relay, validate_relay

LOG = logging.getLogger("claude-proxy")
HTTP = web.AppKey("http", aiohttp.ClientSession)
READ_TIMEOUT = web.AppKey("read_timeout", float)
ORIGIN_FIELDS = web.ResponseKey("origin_fields", set)
MAX_BODY = 64 * 1024 * 1024
MAX_WIRE = 2 * MAX_BODY
MAX_METADATA = 1024 * 1024
RESPONSE_LENGTH = struct.Struct("!I")
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}
# Stable, content-defined boundaries re-align after insertions into the context.
GEAR = [int.from_bytes(hashlib.sha256(bytes([i])).digest()[:8], "big") for i in range(256)]


def chunks(body):
    start = 0
    rolling = 0
    for i, byte in enumerate(body):
        rolling = ((rolling << 1) + GEAR[byte]) & ((1 << 64) - 1)
        size = i + 1 - start
        if size >= 2048 and (rolling & 8191 == 0 or size >= 32768):
            yield body[start:i + 1]
            start = i + 1
            rolling = 0
    if start < len(body):
        yield body[start:]


def digest(body):
    return hashlib.sha256(body).hexdigest()


def headers_without_hops(headers, preserve_length=False):
    pairs = list(headers.items()) if hasattr(headers, "items") else headers
    blocked = set(HOP_HEADERS)
    if preserve_length:
        blocked.remove("content-length")
    for key, value in pairs:
        if key.lower() == "connection":
            blocked.update(part.strip().lower() for part in value.split(","))
    return [(key, value) for key, value in pairs if key.lower() not in blocked]


class ChunkCache:
    def __init__(self, limit):
        self.limit = limit
        self.size = 0
        self.data = OrderedDict()

    def get(self, key):
        value = self.data.get(key)
        if value is not None:
            self.data.move_to_end(key)
        return value

    def put(self, key, value):
        if key in self.data or len(value) > self.limit:
            return
        while self.size + len(value) > self.limit:
            _, old = self.data.popitem(last=False)
            self.size -= len(old)
        self.data[key] = value
        self.size += len(value)


CACHE = web.AppKey("cache", ChunkCache)


async def session_context(app):
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=app[READ_TIMEOUT])
    async with aiohttp.ClientSession(
        timeout=timeout, auto_decompress=False, cookie_jar=aiohttp.DummyCookieJar(),
        skip_auto_headers={"User-Agent", "Accept-Encoding", "Content-Type", "Accept"},
        middlewares=(no_automatic_retry,),
    ) as session:
        app[HTTP] = session
        yield


async def no_automatic_retry(request, handler):
    try:
        return await handler(request)
    except (aiohttp.ClientOSError, aiohttp.ServerDisconnectedError) as error:
        # aiohttp normally retries these for HTTP-idempotent methods. Changing
        # the exception type through its public middleware API prevents replay.
        raise aiohttp.ClientError("Connection interrupted; delivery unknown") from error


def application(max_body=MAX_BODY, read_timeout=None):
    app = web.Application(client_max_size=2 * max_body, handler_args={"auto_decompress": False})
    app[READ_TIMEOUT] = read_timeout
    app.cleanup_ctx.append(session_context)

    async def preserve_origin_headers(request, response):
        original = response.get(ORIGIN_FIELDS)
        if original is not None:
            for name in ["Content-Type", "Date", "Server"]:
                if name.lower() not in original:
                    response.headers.popall(name, None)

    app.on_response_prepare.append(preserve_origin_headers)
    return app


async def stream_body(request, response, output):
    try:
        async for data in response.content.iter_any():
            await output.write(data)
        await output.write_eof()
    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError):
        # Headers have already been sent. Close instead of fabricating a success
        # or retrying a potentially billable upstream request.
        if request.transport:
            request.transport.close()
    return output


async def relay_response(request, response):
    """Frame HTTP metadata separately from the opaque response body."""
    metadata = json.dumps({
        "version": 3, "status": response.status, "reason": response.reason,
        "headers": headers_without_hops(response.headers, preserve_length=True),
    }, separators=(",", ":")).encode()
    if len(metadata) > MAX_METADATA:
        raise ValueError("upstream response headers too large")
    output = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
    await output.prepare(request)
    await output.write(RESPONSE_LENGTH.pack(len(metadata)) + metadata)
    return await stream_body(request, response, output)


async def forward_response(request, response):
    length = RESPONSE_LENGTH.unpack(await response.content.readexactly(RESPONSE_LENGTH.size))[0]
    if length > MAX_METADATA:
        raise ValueError("response metadata too large")
    metadata = json.loads(await response.content.readexactly(length))
    if metadata["version"] != 3:
        raise ValueError("unsupported response protocol")
    output = web.StreamResponse(
        status=metadata["status"], reason=metadata["reason"],
        headers=metadata["headers"],
    )
    output[ORIGIN_FIELDS] = {key.lower() for key, value in metadata["headers"]}
    await output.prepare(request)
    return await stream_body(request, response, output)


def server_app(token, upstream="https://api.anthropic.com", cache_bytes=256 * 1024 * 1024,
               max_body=MAX_BODY, read_timeout=None):
    if len(token) < 32:
        raise ValueError("relay token must contain at least 32 characters")
    app = application(max_body, read_timeout)
    app[CACHE] = ChunkCache(cache_bytes)

    @web.middleware
    async def authenticate(request, handler):
        provided = request.headers.get("Authorization", "").encode()
        if not secrets.compare_digest(provided, ("Bearer " + token).encode()):
            return web.Response(status=401, text="Unauthorized")
        return await handler(request)

    app.middlewares.append(authenticate)
    base = URL(upstream)
    if base.scheme not in {"http", "https"} or not base.host or base.path not in {"", "/"} or base.query_string or base.fragment or base.user:
        raise ValueError("upstream must be an HTTP(S) origin without path or credentials")

    async def relay(request):
        try:
            payload = unpack(await request.read(), 2 * max_body)
            if payload["version"] != 3:
                raise ValueError("unsupported protocol")
            method, path = payload["method"], payload["path"]
            if not isinstance(method, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", method):
                raise ValueError("invalid method")
            if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "#" in path or any(ord(c) < 32 for c in path):
                raise ValueError("invalid path")
            manifest, supplied = payload["chunks"], payload["data"]
            if not isinstance(manifest, list) or len(manifest) > max_body // 2048 + 1 or not isinstance(supplied, dict):
                raise ValueError("invalid manifest")
            incoming = {}
            for key, value in supplied.items():
                if not isinstance(value, bytes) or len(value) > 32768 or digest(value) != key:
                    raise ValueError("invalid chunk")
                incoming[key] = value
            pieces, missing, size = [], set(), 0
            for key in manifest:
                value = incoming.get(key)
                if value is None:
                    value = app[CACHE].get(key)
                if value is None:
                    missing.add(key)
                else:
                    size += len(value)
                    if size > max_body:
                        raise ValueError("body too large")
                    pieces.append(value)
            if missing:
                # This response guarantees no upstream request has been sent.
                return web.json_response({"missing": sorted(missing)}, status=409)
            body = b"".join(pieces)
            if digest(body) != payload["sha256"]:
                raise ValueError("body hash mismatch")
            headers = headers_without_hops(payload["headers"], preserve_length=True)
            for key, value in headers:
                if not isinstance(key, str) or not isinstance(value, str) or "\r" in key + value or "\n" in key + value:
                    raise ValueError("invalid header")
            for key, value in incoming.items():
                app[CACHE].put(key, value)
        except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
            return web.Response(status=400, text="Invalid relay request")
        try:
            # Concatenation to a fixed origin prevents the caller choosing a host.
            async with app[HTTP].request(
                method, URL(str(base.origin()) + path, encoded=True),
                data=body if body else None, headers=headers, allow_redirects=False,
            ) as response:
                return await relay_response(request, response)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return web.Response(status=502, text="Upstream connection failed; request was not retried")

    app.router.add_post("/v1/relay", relay)
    return app


def client_app(token, relay_url, stats=False, relay_ssl=True,
               max_body=MAX_BODY, read_timeout=None):
    app = application(max_body, read_timeout)
    known = OrderedDict()

    async def forward(request):
        body = await request.read()
        if len(body) > max_body:
            raise web.HTTPRequestEntityTooLarge(max_size=max_body, actual_size=len(body))
        parts = list(chunks(body))
        manifest = [digest(part) for part in parts]
        local = dict(zip(manifest, parts))
        payload = {
            "version": 3, "method": request.method, "path": request.raw_path,
            "headers": headers_without_hops(request.headers, preserve_length=True),
            "chunks": manifest, "sha256": digest(body),
            "data": {key: value for key, value in local.items() if key not in known},
        }
        sent = 0
        try:
            for attempt in range(2):
                wire = pack(payload)
                sent += len(wire)
                async with app[HTTP].post(
                    relay_url, data=wire,
                    headers={"Authorization": "Bearer " + token},
                    allow_redirects=False, ssl=relay_ssl,
                ) as response:
                    if response.status == 409:
                        missing = (await response.json())["missing"]
                        if attempt or not missing or any(key not in local for key in missing):
                            return web.Response(status=502, text="Relay cache synchronization failed")
                        # Re-send all chunks on cache loss. Other requests can
                        # evict additional chunks between the 409 and retry.
                        payload["data"] = local
                        continue
                    if response.status == 401:
                        return web.Response(status=502, text="Relay authentication failed")
                    if response.status != 200:
                        return web.Response(status=502, text="Relay transport failed")
                    # Even an upstream error means reconstruction succeeded.
                    # Network errors are never retried: delivery is ambiguous.
                    for key in manifest:
                        known[key] = None
                        known.move_to_end(key)
                    while len(known) > 131072:
                        known.popitem(last=False)
                    if stats:
                        LOG.info("request_bytes=%d transport_bytes=%d", len(body), sent)
                    return await forward_response(request, response)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return web.Response(status=502, text="Relay connection failed; request was not retried")
        except (ValueError, KeyError, TypeError, asyncio.IncompleteReadError):
            return web.Response(status=502, text="Invalid relay response")

    app.router.add_route("*", "/{path:.*}", forward)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["client", "server"])
    parser.add_argument("--port", type=int)
    parser.add_argument("--listen", help="server bind address; client always uses loopback")
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--cert", type=Path, help="server TLS certificate")
    parser.add_argument("--key", type=Path, help="server TLS private key")
    parser.add_argument("--relay-ca", type=Path, help="trusted relay certificate or CA")
    parser.add_argument("--relay", help="HTTPS relay URL; defaults to CLAUDE_TX_RELAY or saved deployment")
    parser.add_argument("--upstream", default="https://api.anthropic.com")
    parser.add_argument("--cache-mib", type=int, default=256)
    parser.add_argument("--max-body-mib", type=int, default=64)
    parser.add_argument("--read-timeout", type=float, default=0, help="idle timeout in seconds; 0 disables it")
    parser.add_argument("--stats", action="store_true", help="log byte counts only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.stats else logging.WARNING)
    if args.cache_mib < 1:
        parser.error("--cache-mib must be positive")
    if args.max_body_mib < 1 or args.read_timeout < 0:
        parser.error("--max-body-mib must be positive and --read-timeout nonnegative")
    options = {"max_body": args.max_body_mib * 1024 * 1024, "read_timeout": args.read_timeout or None}
    token = args.token_file.read_text().strip()
    if len(token) < 32:
        parser.error("token file must contain at least 32 characters")
    tls = None
    if args.mode == "server":
        if not args.cert or not args.key:
            parser.error("server requires --cert and --key for HTTPS")
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(args.cert, args.key)
        app = server_app(token, args.upstream, args.cache_mib * 1024 * 1024, **options)
    else:
        if args.listen and args.listen != "127.0.0.1":
            parser.error("client must bind to 127.0.0.1")
        try:
            args.relay = validate_relay(args.relay) if args.relay else configured_relay()
        except ValueError as error:
            parser.error(str(error))
        relay_ssl = ssl.create_default_context(cafile=args.relay_ca)
        app = client_app(token, args.relay, args.stats, relay_ssl, **options)
    web.run_app(
        app, host=(args.listen or "0.0.0.0") if args.mode == "server" else "127.0.0.1",
        port=args.port or (8787 if args.mode == "client" else 8443),
        access_log=None, handler_cancellation=True, ssl_context=tls,
    )


if __name__ == "__main__":
    main()
