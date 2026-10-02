"""Real manager verifier handles reload ordering without changing entitlements."""

from copy import deepcopy
from itertools import permutations
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from shiri.runtime import units
from shiri.runtime.system import RuntimeFailure
from test_daemon_privileges import BOOT, actual, spec


@pytest.fixture(autouse=True)
def boot(monkeypatch):
    monkeypatch.setattr(units, "boot_id", lambda: BOOT)


def observations():
    service = spec()
    initial = actual(service)
    # Actual135 launch reports the reversed request order. systemd v249
    # serializes this list, then prepends parsed entries on daemon-reload.
    initial["DeviceAllow"] = [["/dev/urandom", "r"], ["/dev/random", "r"],
                              ["/dev/zero", "rw"], ["/dev/null", "rw"]]
    entry = service.intent() | {"policy_fence": {"DeviceAllow": deepcopy(initial["DeviceAllow"])}}
    manager = units.UnitManager(SimpleNamespace(), Mock())
    return manager, entry, initial


@pytest.mark.parametrize("order", list(permutations(range(4))))
def test_all_exact_device_pair_permutations_preserve_original_and_requested_boundary(order):
    manager, entry, observed = observations()
    before = deepcopy(entry)
    observed["DeviceAllow"] = [observed["DeviceAllow"][index] for index in order]
    invocation, cgroup = manager.verify(entry, observed)
    assert invocation == "11" * 16 and cgroup.endswith(entry["unit"])
    assert entry == before  # Historical raw receipt is never rewritten.


@pytest.mark.parametrize("change", ["extra_node", "missing_node", "broader_rights", "different_node", "duplicate"])
def test_reloaded_device_entitlement_changes_still_refuse_before_any_authority(change):
    manager, entry, observed = observations()
    if change == "extra_node":
        observed["DeviceAllow"].append(["/dev/snd/controlC0", "rw"])
    elif change == "missing_node":
        observed["DeviceAllow"].pop()
    elif change == "broader_rights":
        observed["DeviceAllow"][0][1] = "rwm"
    elif change == "different_node":
        observed["DeviceAllow"][0][0] = "/dev/tty"
    else:
        observed["DeviceAllow"].append(deepcopy(observed["DeviceAllow"][0]))
    with pytest.raises(RuntimeFailure, match="device boundary"):
        manager.verify(entry, observed)


@pytest.mark.parametrize("bad", [None, "", {}, [["/dev/null"]], [["/dev/null", "rw", "extra"]],
                               [["/dev/null", True]], [[True, "rw"]], [["/dev/null", ""]]])
def test_malformed_actual_and_saved_device_array_are_refused(bad):
    manager, entry, observed = observations()
    with pytest.raises(RuntimeFailure, match="device policy observation"):
        manager.verify(entry, observed | {"DeviceAllow": bad})
    entry["policy_fence"]["DeviceAllow"] = bad
    with pytest.raises(RuntimeFailure, match="device policy observation"):
        manager.verify(entry, observed)


def test_saved_device_pair_multiplicity_remains_exact_after_order_canonicalization():
    manager, entry, observed = observations()
    entry["policy_fence"]["DeviceAllow"].append(deepcopy(entry["policy_fence"]["DeviceAllow"][0]))
    with pytest.raises(RuntimeFailure, match="immutable property DeviceAllow"):
        manager.verify(entry, observed)


@pytest.mark.parametrize("property_name,new_value", [("RestrictNamespaces", 2**64-1),
                                                   ("DevicePolicy", "auto"), ("NoNewPrivileges", False),
                                                   ("PrivateDevices", False)])
def test_device_array_reordering_does_not_permit_other_security_policy_changes(property_name, new_value):
    manager, entry, observed = observations()
    observed["DeviceAllow"].reverse()
    observed[property_name] = new_value
    with pytest.raises(RuntimeFailure):
        manager.verify(entry, observed)


def test_other_immutable_complex_arrays_retain_original_exact_order_guard():
    manager, entry, observed = observations()
    entry["policy_fence"]["SystemCallFilter"] = deepcopy(observed["SystemCallFilter"])
    observed["DeviceAllow"].reverse()
    observed["SystemCallFilter"][1].reverse()
    with pytest.raises(RuntimeFailure, match="immutable property SystemCallFilter"):
        manager.verify(entry, observed)
