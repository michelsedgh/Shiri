"""Actual Linux credentials exercise the shared ancestor and private receipts."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

import pytest

from shiri.runtime.system import RuntimeFailure

pytestmark = pytest.mark.skipif(sys.platform != 'linux' or os.geteuid() != 0,
                                reason='Actual Linux root-to-service credential boundary')


def load_supervisor():
    path = Path(__file__).parent/'linux/run_native_latency_probe.py'
    spec = importlib.util.spec_from_file_location('actual_lab_work_permissions', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def service_probe(api):
    return subprocess.run(['/usr/bin/python3', '-I', '-c',
        "import json,sqlite3,sys; from pathlib import Path; "
        "p=Path(sys.argv[1]); p.mkdir(exist_ok=True); "
        "c=sqlite3.connect(str(p/'shiri.sqlite3')); "
        "c.execute('create table if not exists admission (id integer)'); "
        "c.commit(); c.close(); "
        "print(json.dumps({'api_opened':True}))",
        str(api)], cwd='/', env={'PATH': '/usr/bin:/bin'},
        user=65534, group=65534, extra_groups=[], capture_output=True, text=True, timeout=10)


def test_fresh_work_allows_actual_service_database_and_keeps_receipts_private(monkeypatch):
    supervisor = load_supervisor()
    with tempfile.TemporaryDirectory(prefix='shiri-work-credentials-', dir='/var/tmp') as name:
        parent = Path(name)
        parent.chmod(0o755)
        work = parent/'work'
        monkeypatch.setattr(supervisor.group, 'WORK', work)
        saved_umask = os.umask(0o022)
        try:
            held = supervisor.private_directory(work/'latency-supervisor-proof')
        finally:
            os.umask(saved_umask)
        private = work/'latency-supervisor-proof'
        assert held == (private.stat().st_dev, private.stat().st_ino)
        assert stat.S_IMODE(work.stat().st_mode) == 0o755
        assert stat.S_IMODE(private.stat().st_mode) == 0o700
        receipt = private/'supervisor.json'
        receipt.write_text('{}')
        receipt.chmod(0o600)
        fixture = work/'native-group-service'
        fixture.mkdir(mode=0o750)
        os.chown(fixture, 0, 65534)
        api = fixture/'api'
        api.mkdir(mode=0o700)
        os.chown(api, 65534, 65534)
        result = service_probe(api)
        assert result.returncode == 0, result.stderr
        # Access beneath the root-only supervisor must still be refused.
        denied = subprocess.run(['/usr/bin/python3', '-I', '-c',
            "from pathlib import Path; import sys; Path(sys.argv[1]).read_bytes()", str(receipt)],
            cwd='/', env={'PATH': '/usr/bin:/bin'}, user=65534, group=65534,
            extra_groups=[], capture_output=True, text=True, timeout=10)
        assert denied.returncode != 0 and 'PermissionError' in denied.stderr
        assert json.loads(result.stdout)['api_opened'] is True


@pytest.mark.parametrize('mode', [0o700, 0o750, 0o775, 0o777])
def test_existing_inaccessible_or_writable_work_is_refused_without_chmod(monkeypatch, mode):
    supervisor = load_supervisor()
    with tempfile.TemporaryDirectory(prefix='shiri-work-refusal-', dir='/var/tmp') as name:
        parent = Path(name)
        parent.chmod(0o755)
        work = parent/'work'
        work.mkdir(mode=mode)
        work.chmod(mode)
        monkeypatch.setattr(supervisor.group, 'WORK', work)
        before = work.stat()
        with pytest.raises(RuntimeFailure):
            supervisor.private_directory(work/'latency-supervisor-proof')
        assert work.stat() == before
        assert not (work/'latency-supervisor-proof').exists()


def test_common_work_symlink_cannot_grant_service_access(monkeypatch):
    supervisor = load_supervisor()
    with tempfile.TemporaryDirectory(prefix='shiri-work-link-', dir='/var/tmp') as name:
        parent = Path(name)
        parent.chmod(0o755)
        target = parent/'target'
        target.mkdir(mode=0o755)
        work = parent/'work'
        work.symlink_to(target, target_is_directory=True)
        monkeypatch.setattr(supervisor.group, 'WORK', work)
        with pytest.raises(RuntimeFailure):
            supervisor.private_directory(work/'latency-supervisor-proof')
        assert list(target.iterdir()) == [] and work.is_symlink()
