"""Real disposable-account install/update with fixture release transport.

The download opener returns locally packaged immutable-source assets. Installed
updates execute the exact stable launcher's retained Python loader in a fresh
isolated process, bypassing only its shell exec so that transport can be stubbed.
Bootstrap verification, closed runtime imports, subprocesses and SQLite are real.
Normal public shell launchers are also exercised, including an offline check.
"""

import json
import shutil
import unittest

import test_package_release as packaging
import test_public_profile as profile


_INVOKE = r'''
import io, json, pathlib, pwd, runpy, shlex, sys, urllib.request
assert sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
routes = json.loads(pathlib.Path('/tmp/release-routes.json').read_text())
log = pathlib.Path('/tmp/release-downloads.json')

class Response(io.BytesIO):
    def __init__(self, body, url):
        super().__init__(body)
        self.url = url

class LocalReleaseTransport:
    def open(self, request, timeout):
        url = request.full_url
        assert url in routes, 'unexpected release download route'
        downloads = json.loads(log.read_text()) if log.exists() else []
        log.write_text(json.dumps([*downloads, url]))
        return Response(pathlib.Path(routes[url]).read_bytes(), url)

urllib.request.build_opener = lambda *handlers: LocalReleaseTransport()
mode, arguments = sys.argv[1], sys.argv[2:]
if mode == 'install':
    version, arguments = arguments[0], arguments[1:]
    namespace = runpy.run_path('/tmp/release-assets/' + version + '/install.py',
                              run_name='pinned_release_fixture')
    raise SystemExit(namespace['install_main'](arguments))
assert mode == 'update'
launcher = pathlib.Path(pwd.getpwuid(__import__('os').getuid()).pw_dir) / '.local/bin/multithread'
tokens = shlex.split(launcher.read_text(), comments=True)
assert tokens[:6] == ['exec', '/usr/bin/python3', '-I', '-S', '-B', '-c']
assert tokens[-1] == '$@' and len(tokens) == 12
# These are the exact installed loader bytes and its own pinned identities.
# No Relay module or installation implementation has been substituted.
sys.argv = ['-c', *tokens[7:-1], 'update', *arguments]
exec(compile(tokens[6], '<retained-installed-loader>', 'exec'), {'__name__': '__main__'})
'''


_FLOW = profile._COMMON + r'''
import sqlite3
assert not pathlib.Path('/source').exists() and not pathlib.Path('/bundle').exists()
metadata = {version: json.loads(pathlib.Path('/tmp/release-assets', version, 'relay-release.json').read_text())
            for version in ('0.2.0', '0.3.0')}
before_foreign = snapshot(foreign)
before_hooks = snapshot(project / '.git/hooks')
assert not launcher.exists() and not (project / '.relay').exists()
assert not (home / '.local/share/relay/enrollments').exists()
invoke = ['/usr/bin/python3', '-I', '-S', '-B', '/tmp/invoke-release.py']
download_log = pathlib.Path('/tmp/release-downloads.json')

def request(mode, arguments, *, success=True):
    previous = json.loads(download_log.read_text()) if download_log.exists() else []
    result = subprocess.run([*invoke, mode, *arguments], cwd=project,
                            text=True, capture_output=True, timeout=45)
    assert result.returncode == (0 if success else 1), (result.returncode, result.stdout, result.stderr)
    report = json.loads(result.stdout)
    downloads = json.loads(download_log.read_text())[len(previous):]
    assert report['provider_started'] is False, report
    assert not list(pathlib.Path('/tmp').glob('relay-download-*')), 'download staging survived the call'
    return report, downloads

fresh, downloads = request('install', ['0.2.0', '--yes', '--enroll-repo', str(project), '--json'])
assert fresh['installation'] == 'installed' and fresh['state'] == 'setup_checked', fresh
assert fresh['repository']['state'] == 'ready', fresh
assert fresh['repository']['repository']['enrollment']['state'] == 'verified', fresh
assert fresh['current']['activation']['release_id'] == metadata['0.2.0']['release_id']
assert len(downloads) == 1 and downloads[0].endswith(metadata['0.2.0']['archive'])
first = call([str(launcher), 'runtime', 'status'])['activation']
assert first['version'] == '0.2.0'
base = [str(launcher), '--repo', str(project), '--json']
assert call(base + ['doctor'])['integrity'] == 'ok'

# Preserve real active coordination while code releases change.
git = ['/usr/bin/git', '-C', str(project), '-c', 'core.hooksPath=/dev/null',
       '-c', 'user.name=Fixture', '-c', 'user.email=fixture.invalid', '-c', 'commit.gpgsign=false']
subprocess.run(git + ['commit', '--allow-empty', '-qm', 'Synthetic update witness'],
               check=True, capture_output=True)
claim = call(base + ['claim', 'code:update', '--agent', 'codex', '--session', 'update-owner',
                    '--purpose', 'Preserve ownership during an explicit release update'])['claim']
handoff = call(base + ['signal', 'work.handoff', '--agent', 'codex', '--session', 'update-owner',
    '--target', 'claude', '--work-id', 'update-witness', '--commit', 'HEAD',
    '--summary', 'Preserve pending review during update'])['event']
before_events = call(base + ['events'])
before_status = call(base + ['status'])
before_brief = call(base + ['brief', '--agent', 'claude'])
assert len(before_status['active_claims']) == 1, before_status
assert any(row['seq'] == handoff['seq'] for row in before_brief['pending_signals']), before_brief
registry = home / '.local/share/relay/enrollments'
before_registry = snapshot(registry)

def sqlite_witness():
    database = project / '.relay/relay.sqlite3'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as ledger:
        ledger.execute('PRAGMA query_only=ON')
        assert ledger.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert ledger.execute('SELECT claim_id, holder_agent, holder_session FROM claims '
                              'WHERE released_event_seq IS NULL').fetchall() == [
                                  (claim['claim_id'], 'codex', 'update-owner')]
        rows = ledger.execute('SELECT seq, event_id, canonical_json, body_hash FROM events ORDER BY seq').fetchall()
        assert len(rows) == len(before_events)
        assert ledger.execute("SELECT COUNT(*) FROM events WHERE kind='delivery.acknowledged'").fetchone()[0] == 0
        return rows

before_sqlite = sqlite_witness()

def preserved():
    assert call(base + ['events']) == before_events
    assert call(base + ['status']) == before_status
    assert call(base + ['brief', '--agent', 'claude']) == before_brief
    assert sqlite_witness() == before_sqlite
    assert snapshot(registry) == before_registry
    assert snapshot(project / '.git/hooks') == before_hooks
    assert snapshot(foreign) == before_foreign

reused, downloads = request('install', ['0.2.0', '--yes', '--enroll-repo', str(project), '--json'])
assert reused['installation'] == 'reused' and reused['state'] == 'setup_checked', reused
assert reused['current']['activation'] == first
assert call([str(launcher), 'runtime', 'status'])['activation'] == first
assert len(downloads) == 1
preserved()

selection = ['--version', '0.3.0', '--approve-sha256', metadata['0.3.0']['release_id'],
             '--expected-activation', first['activation_id'], '--yes', '--json']
stale_selection = list(selection)
stale_selection[5] = ('0' if first['activation_id'][0] != '0' else '1') + first['activation_id'][1:]
before_refusal = snapshot(home)
refused, downloads = request('update', stale_selection, success=False)
assert refused['state'] == 'unavailable' and refused['installation'] == 'unchanged', refused
assert 'Activation changed' in refused['message'], refused
assert len(downloads) == 2 and len(set(downloads)) == 2, 'stale preparation retried a download'
assert snapshot(home) == before_refusal, 'stale update wrote installed or enrollment state'
assert call([str(launcher), 'runtime', 'status'])['activation'] == first
preserved()

updated, downloads = request('update', selection)
assert updated['installation'] == 'updated' and updated['state'] == 'ready_for_setup', updated
second = call([str(launcher), 'runtime', 'status'])['activation']
assert second == updated['current']['activation']
assert second['release_id'] == metadata['0.3.0']['release_id'] and second['version'] == '0.3.0'
assert second['activation_id'] != first['activation_id']
assert updated['current']['launcher'] == str(launcher)
assert len(downloads) == 2 and len(set(downloads)) == 2
assert (installation / 'releases' / first['release_id']).is_dir(), 'prior release was discarded'
preserved()

# An old genuine observation cannot authorize fresh enrollment on the new activation.
before_refusal = snapshot(home)
refused, downloads = request('update', [*selection, '--enroll-repo', str(project)], success=False)
assert refused['state'] == 'unavailable' and 'Activation changed' in refused['message'], refused
assert len(downloads) == 1 and downloads[0].endswith('/relay-release.json')
assert snapshot(home) == before_refusal
assert call([str(launcher), 'runtime', 'status'])['activation'] == second
preserved()
pathlib.Path('/tmp/update-witness.json').write_text(json.dumps({
    'activation': second, 'events': before_events, 'status': before_status, 'brief': before_brief,
    'registry': before_registry, 'hooks': before_hooks, 'foreign': before_foreign,
    'claim_id': claim['claim_id'], 'handoff_seq': handoff['seq'],
}))
print(json.dumps({'fresh_enrollment': True, 'reuse_preserved_activation': True,
                  'installed_update_dispatch': True, 'stale_activation_refused': True,
                  'active_claim_and_pending_handoff_preserved': True, 'sqlite_integrity': 'ok'}))
'''


_SOURCE_ABSENT = profile._COMMON + r'''
assert not pathlib.Path('/source').exists() and not pathlib.Path('/bundle').exists()
assert not pathlib.Path('/tmp/release-assets').exists()
assert not pathlib.Path('/tmp/invoke-release.py').exists()
assert not pathlib.Path('/tmp/release-routes.json').exists()
witness = json.loads(pathlib.Path('/tmp/update-witness.json').read_text())
assert call([str(launcher), 'runtime', 'status'])['activation'] == witness['activation']
base = [str(launcher), '--repo', str(project), '--json']
assert call(base + ['doctor'])['integrity'] == 'ok'
for command in ('events', 'status'):
    assert call(base + [command]) == witness[command]
assert call(base + ['brief', '--agent', 'claude']) == witness['brief']
before_home = snapshot(home)
# This uses the real shell launcher and actual unavailable network, with no stub.
offline = subprocess.run([str(launcher), 'update', '--check', '--version', '0.3.0', '--json'],
                         text=True, capture_output=True, timeout=20)
assert offline.returncode == 1, (offline.stdout, offline.stderr)
report = json.loads(offline.stdout)
assert report['state'] == 'unavailable' and report['stage'] == 'release_selection', report
assert report['installation'] == 'unchanged' and report['provider_started'] is False
assert 'unavailable' in report['message'].lower(), report
assert snapshot(home) == before_home
assert snapshot(home / '.local/share/relay/enrollments') == witness['registry']
assert snapshot(project / '.git/hooks') == witness['hooks']
assert snapshot(foreign) == witness['foreign']
assert not (home / '.codex').exists() and not (home / '.claude').exists()
print(json.dumps({'source_and_assets_absent': True, 'public_launcher_verified': True,
                  'offline_update_unknown': True, 'providers_started': 0}))
'''


class PublicUpdateTests(unittest.TestCase):
    def test_pinned_install_and_installed_update_preserve_real_coordination(self):
        fixture = profile.PublicProfileTests(methodName='test_public_installed_ledger_in_fresh_rootless_account')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        # Reuse the packager's committed-source fixture, including current closed
        # runtime bytes. Neither release is represented as a published version.
        source = packaging.PackageReleaseTests(methodName='test_archive_is_closed_normalized_and_independently_checksummed')
        source.setUp()
        self.addCleanup(source.doCleanups)
        assets = fixture.base / 'tmp/release-assets'
        assets.mkdir(mode=0o700)
        routes = {}
        for version in ('0.2.0', '0.3.0'):
            metadata = packaging.subject.package_release(source.repository, source.revision, version, assets / version)
            for name in ('relay-release.json', metadata['archive']):
                url = packaging.subject.REPOSITORY_URL + '/releases/download/v' + version + '/' + name
                routes[url] = '/tmp/release-assets/' + version + '/' + name
        (fixture.base / 'tmp/release-routes.json').write_text(json.dumps(routes))
        (fixture.base / 'tmp/invoke-release.py').write_text(_INVOKE)
        result = fixture.sandbox(_FLOW, source.revision, include_source=False)
        self.assertEqual({'fresh_enrollment': True, 'reuse_preserved_activation': True,
                          'installed_update_dispatch': True, 'stale_activation_refused': True,
                          'active_claim_and_pending_handoff_preserved': True, 'sqlite_integrity': 'ok'}, result)
        shutil.rmtree(assets)
        for name in ('invoke-release.py', 'release-routes.json'):
            (fixture.base / 'tmp' / name).unlink()
        result = fixture.sandbox(_SOURCE_ABSENT, source.revision, include_source=False)
        self.assertEqual({'source_and_assets_absent': True, 'public_launcher_verified': True,
                          'offline_update_unknown': True, 'providers_started': 0}, result)


if __name__ == '__main__':
    unittest.main()
