"""Malformed discovery cannot silently confirm a partial or extra selection."""
import httpx
import pytest

from shiri.runtime.backend import OwnToneClient, normalize_output
from shiri.runtime.system import RuntimeFailure

OUTPUT = {'id': '123', 'name': 'Kitchen speaker', 'type': 'AirPlay 2',
          'selected': True, 'volume': 50, 'offset_ms': 0}


@pytest.mark.parametrize('change', [
    {'type': []}, {'selected': 'false'}, {'requires_auth': 'false'},
    {'volume': True}, {'volume': 101}, {'offset_ms': None},
    {'offset_ms': 2001}, {'name': 'Kitchen\nother'}, {'format': []},
    {'supported_formats': ['PCM', {}]}, {'airplay_timing': []},
    {'airplay_timing': 'auto'}, {'airplay_timing': False},
])
@pytest.mark.asyncio
async def test_malformed_output_is_visible_failure_even_with_valid_selected_peer(change):
    client = OwnToneClient('http://private-backend', transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={'outputs': [OUTPUT, {**OUTPUT, 'id': '124', **change}]})))
    try:
        assert normalize_output({**OUTPUT, **change}, set()) is None
        with pytest.raises(RuntimeFailure, match='selection is unconfirmed'):
            await client.outputs(set())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_equivalent_duplicate_ids_cannot_choose_the_last_observed_output():
    client = OwnToneClient('http://private-backend', transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={'outputs': [OUTPUT, {**OUTPUT, 'id': '0123', 'name': 'Other speaker'}]})))
    try:
        with pytest.raises(RuntimeFailure, match='duplicate output identities'):
            await client.outputs(set())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unknown_transport_and_excluded_receivers_remain_visible_unassignable_outputs():
    client = OwnToneClient('http://private-backend', transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={'outputs': [OUTPUT, {**OUTPUT, 'id': '124', 'type': 'Unknown'}]})))
    try:
        outputs = await client.outputs({'Kitchen speaker'})
        assert len(outputs) == 2
        assert all(not item['assignable'] for item in outputs)
        assert outputs[0]['selected'] is True
    finally:
        await client.close()
