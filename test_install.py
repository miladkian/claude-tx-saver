"""Configuration and rollback tests; never touch real services or user settings."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from install_client import read_settings, restore_settings, systemd_unit, write_private
from tx_config import configured_relay, state_dir, validate_relay


class InstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_relay_requires_explicit_configuration_and_environment_overrides_saved(self):
        with self.assertRaises(ValueError):
            configured_relay()
        state_dir().mkdir()
        (state_dir() / "relay-url").write_text('https://saved.example:8443/v1/relay\n')
        self.assertEqual(configured_relay(), 'https://saved.example:8443/v1/relay')
        os.environ['CLAUDE_TX_RELAY'] = 'https://override.example/v1/relay'
        self.assertEqual(configured_relay(), 'https://override.example/v1/relay')

    def test_invalid_and_credential_bearing_relay_urls_rejected(self):
        for url in ['http://host/v1/relay', 'https://user:secret@host/v1/relay',
                    'https://host/v1/relay?token=secret', 'https://host/v1/relay#x',
                    'https:///v1/relay', 'https://host:invalid/v1/relay', 'https://host/wrong']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_relay(url)

    def test_private_writes_replace_without_following_old_file_symlink(self):
        target = self.root / 'unrelated'
        target.write_text('keep')
        path = self.root / 'settings.json'
        path.symlink_to(target)
        write_private(path, b'new')
        self.assertEqual(target.read_text(), 'keep')
        self.assertEqual(path.read_bytes(), b'new')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_uninstall_restores_old_base_url_and_preserves_other_settings(self):
        settings, previous = self.root / 'settings.json', self.root / 'previous.json'
        settings.write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': 'http://127.0.0.1:8788',
                                               'CUSTOM': 'keep'}, 'permissions': {'allow': ['Read']}}))
        previous.write_text(json.dumps({'present': True, 'value': 'https://old.example',
                                        'installed': 'http://127.0.0.1:8788'}))
        restore_settings(settings, previous)
        result = read_settings(settings)
        self.assertEqual(result['env']['ANTHROPIC_BASE_URL'], 'https://old.example')
        self.assertEqual(result['env']['CUSTOM'], 'keep')
        self.assertEqual(result['permissions'], {'allow': ['Read']})
        self.assertFalse(previous.exists())

    def test_uninstall_keeps_subsequent_user_url_change(self):
        settings, previous = self.root / 'settings.json', self.root / 'previous.json'
        settings.write_text('{"env":{"ANTHROPIC_BASE_URL":"https://user.example"}}')
        previous.write_text('{"present":false,"value":null}')
        restore_settings(settings, previous)
        self.assertEqual(read_settings(settings)['env']['ANTHROPIC_BASE_URL'], 'https://user.example')

    def test_uninstall_removes_only_injected_setting(self):
        settings, previous = self.root / 'settings.json', self.root / 'previous.json'
        settings.write_text('{"env":{"ANTHROPIC_BASE_URL":"http://127.0.0.1:8787","OTHER":"yes"}}')
        previous.write_text('{"present":false,"value":null}')
        restore_settings(settings, previous)
        self.assertEqual(read_settings(settings), {'env': {'OTHER': 'yes'}})

    def test_uninstall_tolerates_removed_settings_file(self):
        settings, previous = self.root / 'settings.json', self.root / 'previous.json'
        previous.write_text('{"present":true,"value":"https://old.example"}')
        restore_settings(settings, previous)
        self.assertFalse(settings.exists())

    def test_malformed_settings_are_rejected(self):
        settings = self.root / 'settings.json'
        for value in ['[]', '{"env": []}', '{invalid']:
            settings.write_text(value)
            with self.assertRaises(ValueError):
                read_settings(settings)

    def test_systemd_paths_with_spaces_and_specifiers_are_escaped(self):
        unit = systemd_unit(['/a path/python', '/home/user/$code%/proxy.py'], '/home/user/$code%')
        self.assertIn('ExecStart="/a path/python" "/home/user/$$code%%/proxy.py"', unit)
        self.assertIn('WorkingDirectory="/home/user/$code%%"', unit)
        with self.assertRaises(ValueError):
            systemd_unit(['bad\ncommand'], '/tmp')


if __name__ == '__main__':
    unittest.main()
