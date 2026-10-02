"""The log follower cannot survive a dead or replaced broker parent."""

from unittest.mock import Mock

import pytest

from shiri.runtime import log_reader


def test_parent_is_verified_before_and_after_kernel_signal_admission(monkeypatch):
    matches = Mock(side_effect=[True, True])
    armed = Mock()
    monkeypatch.setattr(log_reader, "parent_matches", matches)
    monkeypatch.setattr(log_reader, "death_signal", armed)
    log_reader.arm_parent(100, "12345")
    assert matches.call_args_list[0].args == matches.call_args_list[1].args == (100, "12345")
    armed.assert_called_once_with()


@pytest.mark.parametrize("observations", [[False], [True, False]])
def test_death_before_or_during_arming_refuses_exec(monkeypatch, observations):
    matches, armed = Mock(side_effect=observations), Mock()
    monkeypatch.setattr(log_reader, "parent_matches", matches)
    monkeypatch.setattr(log_reader, "death_signal", armed)
    with pytest.raises(ValueError, match="parent|broker"):
        log_reader.arm_parent(100, "12345")
    assert armed.call_count == int(observations[0])


@pytest.mark.parametrize("pid,birth", [(1, "1"), (True, "1"), (100, ""), (100, "invalid")])
def test_malformed_parent_identity_never_arms_the_reader(monkeypatch, pid, birth):
    armed = Mock()
    monkeypatch.setattr(log_reader, "death_signal", armed)
    with pytest.raises(ValueError):
        log_reader.arm_parent(pid, birth)
    armed.assert_not_called()


def test_kernel_signal_failure_is_not_an_orphan_fallback(monkeypatch):
    monkeypatch.setattr(log_reader, "parent_matches", Mock(return_value=True))
    monkeypatch.setattr(log_reader, "death_signal", Mock(side_effect=OSError("not available")))
    with pytest.raises(OSError, match="not available"):
        log_reader.arm_parent(100, "12345")


def test_actual_pid_matches_but_changed_birth_is_rejected(monkeypatch):
    monkeypatch.setattr(log_reader.os, "getppid", lambda: 100)
    fields = ["0"] * 20
    fields[19] = "12345"
    monkeypatch.setattr(log_reader.Path, "read_text", lambda _: "100 (parent with spaces)) " + " ".join(fields))
    assert log_reader.parent_matches(100, "12345")
    assert not log_reader.parent_matches(100, "99999")
    monkeypatch.setattr(log_reader.os, "getppid", lambda: 101)
    assert not log_reader.parent_matches(100, "12345")
