"""Pin/apply/verify the complete compatible receiver control layer."""
import importlib.util
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_reverse_volume_builder_and_contract_are_exact():
    spec = importlib.util.spec_from_file_location("receiver_volume_pin", ROOT / "tests/native/check_receiver_volume.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import hashlib
    patch = ROOT / "install/patches/shairport-5.5.2-receiver-volume.patch"
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == module.PATCH_SHA
    builder = (ROOT / "install/build_backends.sh").read_text()
    assert "SHAIRPORT_VOLUME_SHA=" + module.PATCH_SHA in builder
    assert 'manifest["shairport_receiver_volume_patch"] = sys.argv[28]' in builder
    assert builder.index('apply "$SHAIRPORT_STARTUP_PATCH"') < builder.index('apply "$SHAIRPORT_VOLUME_PATCH"')
    assert builder.index('check_receiver_volume.py') < builder.index('check_shairport_startup.py')
    assert 'r"-shiri-timed3-startup1-volume1(?=' in (ROOT / "shiri/runtime/broker.py").read_text()
    subprocess.run(["bash", "-n", ROOT / "install/build_backends.sh"], check=True)
