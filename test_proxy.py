import asyncio
import gzip
import json
import random
import unittest
import zlib

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from aiohttp import ClientPayloadError
from yarl import URL

from proxy import CACHE, ChunkCache, chunks, client_app, digest, pack, server_app, unpack

TOKEN = "test-token-" + "a" * 40


class ProtocolTests(unittest.TestCase):
    def test_chunk_reconstruction_and_realign_after_insertion(self):
        body = random.Random(7).randbytes(400_000)
        original = list(chunks(body))
        changed = list(chunks(body[:50_000] + b"new context" * 100 + body[50_000:]))
        self.assertEqual(b"".join(original), body)
        reused = set(map(digest, original))
        self.assertGreater(sum(len(c) for c in changed if digest(c) in reused), 350_000)

    def test_cache_bound_and_eviction(self):
        cache = ChunkCache(6)
        cache.put("a", b"abc")
        cache.put("b", b"def")
        cache.get("a")
        cache.put("c", b"xyz")
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.size, 6)

    def test_bad_envelopes(self):
        for bad in [b"garbage", pack({}) + b"extra", pack({})[:-1]]:
            with self.assertRaises((ValueError, zlib.error)):
                unpack(bad)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.received = []
        self.wire = []
        self.after_relay = None

        async def upstream(request):
            self.received.append((request.method, request.raw_path, request.headers, await request.read()))
            if request.path == "/stream":
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                await response.write(b"data: first\n\n")
                await self.finish_stream.wait()
                await response.write(b"data: last\n\n")
                return response
            if request.path == "/compressed":
                return web.Response(body=gzip.compress(b"compressed response"), headers={"Content-Encoding": "gzip"})
            if request.path == "/conflict":
                return web.Response(status=409, body=b"upstream conflict")
            if request.path == "/redirect":
                return web.Response(status=307, headers={"Location": "https://example.com/"})
            if request.path.startswith("/status/"):
                return web.Response(status=int(request.path.rsplit("/", 1)[1]), body=b'opaque upstream error\x00\xff',
                                    headers=[("WWW-Authenticate", "Bearer realm=upstream"),
                                             ("Retry-After", "9"), ("X-Relay-Control", "upstream-owned")])
            if request.path == "/future/opaque":
                return web.Response(status=299, reason="Future Status", body=self.received[-1][3],
                                    headers=[("x-future-result", "enabled"), ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")])
            if request.path == "/disconnect":
                request.transport.close()
                return web.Response()
            if request.path == "/truncated":
                response = web.StreamResponse(headers={"Content-Length": "100"})
                await response.prepare(request)
                await response.write(b"partial")
                request.transport.close()
                return response
            return web.Response(body=b"ok", headers={"request-id": "request-123"})

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", upstream)

        async def omit_headers(request, response):
            if request.path == "/absent-headers":
                for name in ["Date", "Server", "Content-Type"]:
                    response.headers.popall(name, None)

        app.on_response_prepare.append(omit_headers)
        self.upstream = await self.start(app)
        self.relay_app = server_app(TOKEN, str(self.upstream.make_url("/")))

        @web.middleware
        async def capture(request, handler):
            self.wire.append(await request.read())
            response = await handler(request)
            if self.after_relay:
                self.after_relay(response)
            return response

        self.relay_app.middlewares.insert(0, capture)
        self.relay = await self.start(self.relay_app)
        self.client = await self.start(client_app(TOKEN, str(self.relay.make_url("/v1/relay"))))
        self.http = ClientSession(auto_decompress=False)
        self.finish_stream = asyncio.Event()

    async def start(self, app):
        server = TestServer(app)
        await server.start_server()
        self.servers.append(server)
        return server

    async def asyncTearDown(self):
        self.finish_stream.set()
        await self.http.close()
        for server in reversed(self.servers):
            await server.close()

    async def send(self, body, path="/v1/messages?beta=true"):
        return await self.http.post(self.client.make_url(path), data=body, headers={
            "Authorization": "Bearer oauth-secret", "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json", "x-api-key": "api-secret",
        }, allow_redirects=False)

    async def test_deltas_exact_bytes_auth_headers_and_cache_restart(self):
        text = random.Random(1).randbytes(180_000).hex()
        first = json.dumps({"messages": [{"role": "user", "content": text}]}).encode()
        second = first[:-2] + b', {"role":"user","content":"next turn"}]}'
        for body in [first, second]:
            async with await self.send(body) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["request-id"], "request-123")
                self.assertEqual(await response.read(), b"ok")
            self.assertEqual(self.received[-1][3], body)
        self.assertLess(len(self.wire[1]), len(self.wire[0]) // 10)
        headers = self.received[-1][2]
        self.assertEqual(headers["Authorization"], "Bearer oauth-secret")
        self.assertEqual(headers["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(headers["x-api-key"], "api-secret")
        self.assertEqual(self.received[-1][1], "/v1/messages?beta=true")
        self.relay_app[CACHE].data.clear()
        self.relay_app[CACHE].size = 0
        before = len(self.received)
        async with await self.send(second) as response:
            self.assertEqual(response.status, 200)
            await response.read()
        self.assertEqual(len(self.received), before + 1)
        self.assertEqual(len(self.wire), 4)

    async def test_auth_failure_never_reaches_upstream(self):
        async with self.http.post(self.relay.make_url("/v1/relay"), data=b"garbage") as response:
            self.assertEqual(response.status, 401)
        self.assertEqual(self.received, [])

    async def test_bad_chunk_and_missing_chunk_never_reach_upstream(self):
        payload = {"version": 3, "method": "POST", "path": "/v1/messages", "headers": [],
                   "chunks": [digest(b"hello")], "sha256": digest(b"hello"), "data": {}}
        for data, expected in [({}, 409), ({digest(b"hello"): b"wrong"}, 400)]:
            payload["data"] = data
            async with self.http.post(self.relay.make_url("/v1/relay"), data=pack(payload),
                                      headers={"Authorization": "Bearer " + TOKEN}) as response:
                self.assertEqual(response.status, expected)
        self.assertEqual(self.received, [])

    async def test_upstream_conflict_not_retried_and_redirect_not_followed(self):
        for path, status in [("/conflict", 409), ("/redirect", 307)]:
            async with await self.send(b"{}", path) as response:
                self.assertEqual(response.status, status)
                await response.read()
        self.assertEqual(len(self.received), 2)

    async def test_all_upstream_errors_and_headers_are_preserved(self):
        for status in [400, 401, 403, 409, 429, 500, 502, 503]:
            async with await self.send(b"{}", "/status/" + str(status)) as response:
                self.assertEqual(response.status, status)
                self.assertEqual(response.headers["WWW-Authenticate"], "Bearer realm=upstream")
                self.assertEqual(response.headers["Retry-After"], "9")
                self.assertEqual(response.headers["X-Relay-Control"], "upstream-owned")
                self.assertEqual(await response.read(), b'opaque upstream error\x00\xff')
        self.assertEqual(len(self.received), 8)

    async def test_future_endpoint_binary_body_headers_and_encoded_query(self):
        body = b"\x00\xffunknown future body; no JSON schema\x01"
        path = "/future/opaque?x=%2f&x=%2B&empty=&future=a+b"
        headers = [("x-future-request", "one"), ("x-future-request", "two"),
                   ("anthropic-beta", "unknown-future-beta")]
        async with self.http.post(self.client.make_url(URL(path, encoded=True)), data=body, headers=headers) as response:
            self.assertEqual(response.status, 299)
            self.assertEqual(response.reason, "Future Status")
            self.assertEqual(response.headers.getall("Set-Cookie"), ["a=1", "b=2"])
            self.assertEqual(response.headers["x-future-result"], "enabled")
            self.assertEqual(await response.read(), body)
        self.assertEqual(self.received[-1][1], path)
        self.assertEqual(self.received[-1][2].getall("x-future-request"), ["one", "two"])
        self.assertNotIn("Authorization", self.received[-1][2])

    async def test_head_preserves_length_without_waiting_for_body(self):
        async with self.http.head(self.client.make_url("/head"), timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Length"], "2")
            self.assertEqual(await response.read(), b"")
        self.assertEqual(self.received[-1][0], "HEAD")

    async def test_absent_origin_headers_are_not_invented(self):
        async with await self.send(b"{}", "/absent-headers") as response:
            self.assertEqual(await response.read(), b"ok")
            for name in ["Date", "Server", "Content-Type"]:
                self.assertNotIn(name, response.headers)

    async def test_disconnected_get_is_never_automatically_retried(self):
        async with self.http.get(self.client.make_url("/disconnect")) as response:
            self.assertEqual(response.status, 502)
            await response.read()
        self.assertEqual(len(self.received), 1)

    async def test_truncated_response_stays_a_transport_failure(self):
        async with self.http.get(self.client.make_url("/truncated")) as response:
            with self.assertRaises(ClientPayloadError):
                await response.read()
        self.assertEqual(len(self.received), 1)

    async def test_bad_relay_token_is_distinct_from_upstream_401(self):
        bad_client = await self.start(client_app("wrong-token", str(self.relay.make_url("/v1/relay"))))
        async with self.http.get(bad_client.make_url("/anything")) as response:
            self.assertEqual(response.status, 502)
            self.assertEqual(await response.text(), "Relay authentication failed")
        self.assertEqual(self.received, [])

    async def test_no_local_route_shadows_anthropic_paths(self):
        async with self.http.get(self.client.make_url("/__proxy_health")) as response:
            self.assertEqual(await response.read(), b"ok")
        self.assertEqual(self.received[-1][1], "/__proxy_health")

    async def test_compressed_response_preserved(self):
        async with await self.send(b"{}", "/compressed") as response:
            self.assertEqual(response.headers["Content-Encoding"], "gzip")
            self.assertEqual(gzip.decompress(await response.read()), b"compressed response")

    async def test_compressed_request_preserved_on_wire(self):
        body = gzip.compress(b'{"messages":[]}')
        async with self.http.post(self.client.make_url("/v1/messages"), data=body,
                                  headers={"Content-Encoding": "gzip"}) as response:
            self.assertEqual(response.status, 200)
            await response.read()
        # The mock upstream deliberately decompresses inbound HTTP bodies;
        # our client and server must preserve the encoding header and bytes.
        self.assertEqual(self.received[-1][2]["Content-Encoding"], "gzip")
        self.assertEqual(self.received[-1][3], gzip.decompress(body))

    async def test_stream_first_event_arrives_before_upstream_finishes(self):
        async with await self.send(b"{}", "/stream") as response:
            first = await asyncio.wait_for(response.content.readuntil(b"\n\n"), 2)
            self.assertEqual(first, b"data: first\n\n")
            self.finish_stream.set()
            self.assertEqual(await response.read(), b"data: last\n\n")

    async def test_tiny_cache_still_reconstructs_request(self):
        self.relay_app[CACHE].limit = 1
        body = random.Random(42).randbytes(80_000)
        for _ in range(2):
            async with await self.send(body) as response:
                self.assertEqual(response.status, 200)
                await response.read()
        self.assertEqual([r[3] for r in self.received], [body, body])

    async def test_further_eviction_between_cache_miss_and_retry(self):
        body = random.Random(4).randbytes(120_000)
        async with await self.send(body) as response:
            await response.read()
        cache = self.relay_app[CACHE]
        _, removed = cache.data.popitem(last=False)
        cache.size -= len(removed)

        def evict_after_miss(response):
            if response.status == 409:
                cache.data.clear()
                cache.size = 0

        self.after_relay = evict_after_miss
        async with await self.send(body) as response:
            self.assertEqual(response.status, 200)
            await response.read()
        self.assertEqual(self.received[-1][3], body)


if __name__ == "__main__":
    unittest.main()
