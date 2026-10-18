"""Opt-in real container integration: RUN_DOCKER_TESTS=1 python -m unittest test_docker -v."""

import os
from pathlib import Path
import random
import re
import ssl
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid


@unittest.skipUnless(os.environ.get('RUN_DOCKER_TESTS') == '1', 'opt-in Docker integration')
class DockerTests(unittest.TestCase):
    def test_tls_auth_reconstruction_reuse_and_loopback_publish(self):
        prefix = 'tx-test-' + uuid.uuid4().hex[:10]
        image = os.environ.get('CLAUDE_TX_TEST_IMAGE', 'claude-tx-saver:test')

        def docker(*args):
            return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.PIPE).strip()

        docker('network', 'create', prefix)
        names = []
        self.addCleanup(lambda: docker('network', 'rm', prefix))
        self.addCleanup(lambda: [docker('rm', '-f', name) for name in reversed(names)])
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            token = 'test-only-' + 'a' * 64
            (state / 'token').write_text(token)
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                            '-keyout', str(state / 'relay.key'), '-out', str(state / 'relay.crt'),
                            '-subj', '/CN=relay', '-addext', 'subjectAltName=DNS:relay,IP:127.0.0.1'],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Synthetic test credentials only; readable across host/container UIDs.
            for path in state.iterdir():
                path.chmod(0o644)
            state.chmod(0o755)
            upstream = prefix + '-upstream'
            names.append(upstream)
            code = ('from aiohttp import web\n'
                    'async def echo(r):\n'
                    ' assert r.headers.get("Authorization")=="Bearer provider-fixture"\n'
                    ' return web.Response(body=await r.read())\n'
                    'app=web.Application(client_max_size=1048576)\n'
                    'app.router.add_route("*", "/{tail:.*}", echo)\n'
                    'web.run_app(app, port=8000, access_log=None)')
            docker('run', '-d', '--name', upstream, '--network', prefix, '--network-alias', 'upstream',
                   '--entrypoint', 'python', image, '-c', code)
            relay = prefix + '-relay'
            names.append(relay)
            flags = ['--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '64', '--memory', '1g']
            docker('run', '-d', '--name', relay, '--network', prefix, '--network-alias', 'relay', *flags,
                   '-p', '127.0.0.1::8443', '-v', f'{state}:/run/credentials:ro', image, 'server',
                   '--port', '8443', '--token-file', '/run/credentials/token', '--cert', '/run/credentials/relay.crt',
                   '--key', '/run/credentials/relay.key', '--upstream', 'http://upstream:8000')
            relay_port = docker('port', relay, '8443/tcp').rsplit(':', 1)[1]
            tls = ssl.create_default_context(cafile=state / 'relay.crt')
            url = f'https://127.0.0.1:{relay_port}/v1/relay'
            for attempt in range(60):
                try:
                    urllib.request.urlopen(urllib.request.Request(url, data=b'bad'), context=tls, timeout=2)
                except urllib.error.HTTPError as error:
                    self.assertEqual(error.code, 401)
                    error.close()
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(.25)
            else:
                self.fail('Relay did not become ready')
            client = prefix + '-client'
            names.append(client)
            docker('run', '-d', '--name', client, '--network', prefix, *flags,
                   '-p', '127.0.0.1::8787', '-v', f'{state}:/run/credentials:ro', image,
                   'client', '--listen', '0.0.0.0', '--port', '8787', '--relay', 'https://relay:8443/v1/relay',
                   '--token-file', '/run/credentials/token', '--relay-ca', '/run/credentials/relay.crt', '--stats')
            address = docker('port', client, '8787/tcp')
            self.assertTrue(address.startswith('127.0.0.1:'))
            body = random.Random(57).randbytes(150_000)
            endpoint = 'http://' + address + '/future/opaque?version=next'
            for attempt in range(60):
                try:
                    request = urllib.request.Request(endpoint, data=body, headers={'Authorization': 'Bearer provider-fixture'})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.read(), body)
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(.25)
            else:
                self.fail('Client did not become ready')
            for data in [body, body + b'fix it']:
                request = urllib.request.Request(endpoint, data=data, headers={'Authorization': 'Bearer provider-fixture'})
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.read(), data)
            logs = subprocess.check_output(['docker', 'logs', client], text=True, stderr=subprocess.STDOUT)
            sizes = [(int(a), int(b)) for a, b in re.findall(r'request_bytes=(\d+) transport_bytes=(\d+)', logs)]
            self.assertGreaterEqual(len(sizes), 3)
            self.assertLess(sizes[-2][1], len(body) * .05)
            self.assertLess(sizes[-1][1], len(body) * .3)
