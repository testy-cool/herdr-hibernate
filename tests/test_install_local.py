"""Exercise install and rollback without touching the user's service."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

LOADER = importlib.machinery.SourceFileLoader(
    'install_local', str(Path(__file__).resolve().parents[1] / 'scripts/install-local'))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
install = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(install)


class InstallTests(unittest.TestCase):
    def exercise(self, fail_restart=False, check=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'installed'
            target.mkdir()
            registry = root / 'plugins.json'
            other = {'plugin_id': 'other.plugin', 'setting': 'keep me'}
            old = {'plugin_id': 'bengemine.hibernate', 'plugin_root': str(target),
                   'source': {'kind': 'github', 'managed_path': str(target),
                              'requested_ref': 'old', 'resolved_commit': 'old'}}
            registry.write_text(json.dumps([old, other]))
            (root / '.install-local.json').write_text(json.dumps({'registry': str(registry)}))
            calls = []
            restarts = 0

            def command(*args, cwd=None):
                nonlocal restarts
                calls.append(args)
                if args[:3] == ('git', 'status', '--porcelain'):
                    return ''
                if args[:2] == ('git', 'rev-parse'):
                    return 'new' if cwd != target or any('checkout' in call for call in calls) else 'old'
                if 'show' in args:
                    return str(target / 'herdr-hibernate') + ' watch'
                if 'restart' in args:
                    restarts += 1
                    if fail_restart and restarts == 1:
                        # Another plugin changes while our install runs.
                        data = json.loads(registry.read_text())
                        data[1]['setting'] = 'new unrelated setting'
                        registry.write_text(json.dumps(data))
                        raise RuntimeError('service failed')
                return 'active'

            with mock.patch.object(install, 'ROOT', root), \
                 mock.patch.object(install, 'run', side_effect=command), \
                 mock.patch.object(install, 'check_resume'), \
                 mock.patch.object(install.subprocess, 'run'), \
                 mock.patch('sys.argv', ['install-local'] + (['--check'] if check else [])):
                if fail_restart:
                    with self.assertRaisesRegex(RuntimeError, 'service failed'):
                        install.main()
                else:
                    install.main()
            saved = json.loads(registry.read_text())
            if check:
                self.assertEqual(saved, [old, other])
                self.assertFalse(any('checkout' in call or 'restart' in call for call in calls))
                self.assertFalse(list(root.glob('hibernate-install-*')))
            elif fail_restart:
                self.assertEqual(saved[0], old)
                self.assertEqual(saved[1]['setting'], 'new unrelated setting')
                self.assertIn(('git', 'checkout', '--detach', 'old'), calls)
                self.assertEqual(restarts, 2)
            else:
                self.assertEqual(saved[0]['source']['resolved_commit'], 'new')
                self.assertEqual(saved[1], other)
                backup = next(root.glob('hibernate-install-*'))
                self.assertEqual(json.loads((backup / 'plugins.json').read_text()), [old, other])
                self.assertEqual(json.loads((backup / 'rollback.json').read_text())['commit'], 'old')

    def test_install_updates_only_hibernate_and_keeps_backup(self):
        self.exercise()

    def test_failed_restart_rolls_back_without_losing_other_plugin_changes(self):
        self.exercise(fail_restart=True)

    def test_check_does_not_install_or_restart(self):
        self.exercise(check=True)
