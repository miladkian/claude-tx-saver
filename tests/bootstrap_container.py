"""Run installers from scratch in an isolated Linux container with real Python/TLS.

Only GitHub archive retrieval, SSH credential copying and the systemd supervisor
are replaced. Runtime code, pip installation, certificates, listeners and settings
writes are real. Never run this harness on a host: it owns /opt and /etc fixtures.
"""

import json
import os
from pathlib import Path
import ssl
import subprocess
import tarfile
import urllib.error
import urllib.request

assert Path('/.dockerenv').exists(), 'This harness must run in its disposable container'
source = Path('/source')
with tarfile.open('/tmp/source.tar.gz', 'w:gz') as archive:
    for name in ['proxy.py', 'wire.py', 'tx_config.py', 'requirements.txt',
                 'install_client.py', 'Dockerfile', '.dockerignore', 'server.sh', 'client.sh']:
        archive.add(source / name, arcname='fixture/' + name)

bin_dir = Path('/tmp/test-bin')
bin_dir.mkdir()
os.environ['PATH'] = str(bin_dir) + ':' + os.environ['PATH']

def executable(name, code):
    path = bin_dir / name
    path.write_text(code)
    path.chmod(0o755)

executable('curl', '''#!/usr/bin/env python3
import os,shutil,sys
if os.environ.get('TEST_DETECTION'):
 if 'https://api.ipify.org' in sys.argv:
  print('invalid address')
  sys.exit(0)
 if 'https://checkip.amazonaws.com' in sys.argv:
  print('93.184.216.34' if os.environ['TEST_DETECTION']=='success' else '192.168.1.1')
  sys.exit(0)
 if any('/tarball/' in arg for arg in sys.argv):
  print('PUBLIC_IP_DETECTED_REACHED_DOWNLOAD',file=sys.stderr)
  sys.exit(22)
if any('/tarball/' in arg for arg in sys.argv):
 shutil.copy('/tmp/source.tar.gz',sys.argv[sys.argv.index('-o')+1])
elif any('/releases/latest/download/' in arg for arg in sys.argv):
 name='server.sh' if any(arg.endswith('/server.sh') for arg in sys.argv) else 'client.sh'
 shutil.copy('/source/'+name,sys.argv[sys.argv.index('-o')+1])
else:
 os.execv('/usr/bin/curl',['curl',*sys.argv[1:]])
''')
executable('ssh', '#!/bin/sh\ncat /etc/claude-proxy/client.json\n')
executable('claude', '#!/bin/sh\necho fixture\n')
executable('systemctl', '''#!/usr/bin/env python3
import os,pathlib,shlex,signal,subprocess,sys
user='--user' in sys.argv
pid=pathlib.Path('/tmp/client.pid' if user else '/tmp/server.pid')
if 'restart' in sys.argv or 'disable' in sys.argv:
 if pid.exists():
  try: os.kill(int(pid.read_text()),signal.SIGTERM)
  except ProcessLookupError: pass
if 'restart' in sys.argv:
 if user:
  unit=pathlib.Path.home()/'.config/systemd/user/claude-tx-saver.service'
 else:
  unit=pathlib.Path('/etc/systemd/system/claude-proxy.service')
 line=next(line for line in unit.read_text().splitlines() if line.startswith('ExecStart='))
 command=shlex.split(line.split('=',1)[1].replace('%d/relay-token','/etc/claude-proxy/token').replace('%d/tls-key','/etc/claude-proxy/relay.key'))
 with open('/tmp/client.log' if user else '/tmp/server.log','ab') as log:
  process=subprocess.Popen(command,stdout=log,stderr=log,start_new_session=True)
 pid.write_text(str(process.pid))
''')
settings = Path.home() / '.claude/settings.json'
settings.parent.mkdir()
settings.write_text('{"env":{"CUSTOM":"keep","ANTHROPIC_BASE_URL":"https://old.example"}}')
try:
    # Exercise first-install detection and fallback, stopping before any install.
    for mode in ['success', 'failure']:
        probe = subprocess.run(['bash', str(source / 'server.sh')],
                               env={**os.environ, 'TEST_DETECTION': mode}, capture_output=True, text=True)
        assert probe.returncode != 0
        expected = 'PUBLIC_IP_DETECTED_REACHED_DOWNLOAD' if mode == 'success' else 'Could not detect public IPv4'
        assert expected in probe.stderr, (mode, probe.stdout, probe.stderr)
    subprocess.run(['bash', str(source / 'server.sh'), '127.0.0.1'], check=True)
    server = Path('/etc/claude-proxy')
    original_token = (server / 'token').read_bytes()
    original_cert = (server / 'relay.crt').read_bytes()
    assert (server / 'client.json').stat().st_mode & 0o777 == 0o600
    config = json.loads((server / 'client.json').read_text())
    request = urllib.request.Request(config['relay'], data=b'bad', headers={'Authorization': 'Bearer '+config['token']})
    try:
        urllib.request.urlopen(request, context=ssl.create_default_context(cafile=server/'relay.crt'))
    except urllib.error.HTTPError as error:
        assert error.code == 400
    else:
        raise AssertionError('Invalid envelope accepted')
    subprocess.run(['bash', str(source / 'client.sh'), '127.0.0.1'], check=True)
    assert json.loads(settings.read_text())['env'] == {'CUSTOM': 'keep', 'ANTHROPIC_BASE_URL': 'http://127.0.0.1:8787'}
    state = Path.home() / '.config/claude-proxy'
    for name in ['token','relay.crt','relay-url']:
        assert (state / name).stat().st_mode & 0o777 == 0o600
    subprocess.run(['/usr/local/bin/claude-tx-server', '--update'], check=True)
    subprocess.run([str(Path.home() / '.local/bin/claude-tx-client'), '--update'], check=True)
    assert (server / 'token').read_bytes() == original_token
    assert (server / 'relay.crt').read_bytes() == original_cert
    assert (server / 'install-mode').read_text().strip() == 'native'
    assert (state / 'client-mode').read_text().strip() == 'native'
    subprocess.run(['bash', str(source / 'client.sh'), '--uninstall'], check=True)
    assert json.loads(settings.read_text())['env'] == {'CUSTOM': 'keep', 'ANTHROPIC_BASE_URL': 'https://old.example'}
    # No IP argument on reinstall: preserve the saved address, not a new lookup.
    subprocess.run(['bash', str(source / 'server.sh')], check=True)
    assert (server / 'token').read_bytes() == original_token
    assert (server / 'relay.crt').read_bytes() == original_cert
    print('PASS: fresh server/client install, verified TLS/auth, private files, one-command updates, settings rollback')
except Exception:
    for name in ['/tmp/client.log', '/tmp/server.log']:
        if Path(name).exists():
            print(Path(name).read_text())
    raise
