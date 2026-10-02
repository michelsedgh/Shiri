"""BEGIN is exact admission evidence, with original finite media guards intact."""
from copy import deepcopy
import os

import pytest

from shiri.runtime.system import RuntimeFailure
from test_native_speech_finite import encoded_reference
from test_native_speech_finite_supervisor import controller, session


@pytest.fixture(scope="module")
def emitted():
    return encoded_reference()[0]


def admitted_record(tmp_path, emitted, role):
    record = session(tmp_path, emitted, role)
    observations = [record["timer_ready_observation"]]
    for row in record["rows"]:
        observations += [row["pre_release_readiness"], row["post_utterance_readiness"]]
    for observation in observations:
        health = observation["worker_health"]
        health["speech_startup_authenticated_ready_ack"]["action"] = "begin"
        health["speech_startup_authenticated_begin_ack"] = deepcopy(health["speech_startup_authenticated_ready_ack"])
    record["timer_ready_ack"] = deepcopy(record["timer_ready_observation"]["worker_health"]["speech_startup_authenticated_ready_ack"])
    return record


@pytest.mark.parametrize("role", ["idle", "native"])
def test_exact_begin_echo_qualifies_complete_reference_with_original_clocks(tmp_path, emitted, role):
    record = admitted_record(tmp_path, emitted, role)
    result = controller.validate_session(record, role, tmp_path, expected_uid=os.getuid())
    assert len(result) == 2 and all(row["cold_utterance_completeness_passed"] for row in result)
    assert all(row["source_to_final_reference_origin_ms"] == 530 for row in result)


@pytest.mark.parametrize("role", ["idle", "native"])
@pytest.mark.parametrize("field", ["incarnation", "session_id", "epoch", "generation", "operation_generation",
    "room_id", "launch_generation", "speech_id", "action", "missing_begin", "different_begin"])
def test_begin_evidence_cannot_relabel_an_old_ack_or_weaken_exact_echo_guards(tmp_path, emitted, role, field):
    record = admitted_record(tmp_path, emitted, role)
    health = record["timer_ready_observation"]["worker_health"]
    if field == "missing_begin":
        del health["speech_startup_authenticated_begin_ack"]
    elif field == "different_begin":
        health["speech_startup_authenticated_begin_ack"]["speech_id"] = "f" * 32
    else:
        ack = health["speech_startup_authenticated_ready_ack"]
        ack[field] = True if type(ack[field]) is int else "invalid"
        health["speech_startup_authenticated_begin_ack"] = deepcopy(ack)
        record["timer_ready_ack"] = deepcopy(ack)
    with pytest.raises(RuntimeFailure):
        controller.validate_session(record, role, tmp_path, expected_uid=os.getuid())
