"""A parked turn's checkpoint, as stored in ``input_requests.turn`` by 0.16.0 and 0.16.1."""

from __future__ import annotations

import json

from alfred.agent.turn import _Checkpoint
from alfred.model import ToolCall

_TAIL = [
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c0",
                "type": "function",
                "function": {"name": "mcparr_find_media", "arguments": '{"title": "Dune"}'},
            },
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "mcparr_add_media", "arguments": '{"title": "Dune"}'},
            },
        ],
    },
    {"role": "tool", "tool_call_id": "c0", "content": "Dune (2021), not in the library"},
]

# Written by 0.16.1: the ids of the entries in the turn's context.
STORED_0_16_1 = {
    "call": {"id": "c1", "name": "mcparr_add_media", "arguments": '{"title": "Dune"}'},
    "call_index": 1,
    "remaining": [{"id": "c2", "name": "alfred_remind_set", "arguments": "{}"}],
    "tail": _TAIL,
    "requests_used": 1,
    "seq": 2,
    "conversation_id": 7,
    "upto_entry_id": 41,
    "reset": False,
    "entry_ids": [38, 41],
}

# Written by 0.16.0: no entry ids.
STORED_0_16_0 = {key: value for key, value in STORED_0_16_1.items() if key != "entry_ids"}


def test_reads_what_0_16_1_stored() -> None:
    checkpoint = _Checkpoint.from_json(STORED_0_16_1)
    assert checkpoint.call == ToolCall("c1", "mcparr_add_media", '{"title": "Dune"}')
    assert checkpoint.call_index == 1
    assert checkpoint.remaining == (ToolCall("c2", "alfred_remind_set", "{}"),)
    assert checkpoint.tail == _TAIL
    assert (checkpoint.requests_used, checkpoint.seq) == (1, 2)
    assert (checkpoint.conversation_id, checkpoint.upto_entry_id) == (7, 41)
    assert checkpoint.reset is False
    assert checkpoint.entry_ids == (38, 41)


def test_reads_what_0_16_0_stored() -> None:
    checkpoint = _Checkpoint.from_json(STORED_0_16_0)
    assert checkpoint.entry_ids is None  # resumed with its window up to the newest entry
    assert checkpoint.upto_entry_id == 41


def test_writes_what_0_16_1_stored() -> None:
    # Same keys and values, so an image rolled back to 0.16.1 resumes it.
    assert _Checkpoint.from_json(STORED_0_16_1).to_json() == STORED_0_16_1


def test_a_0_16_0_checkpoint_keeps_its_shape() -> None:
    assert _Checkpoint.from_json(STORED_0_16_0).to_json() == STORED_0_16_0


def test_round_trip_through_json() -> None:
    checkpoint = _Checkpoint(
        ToolCall("c0", "mcparr_add_media", "{}"),
        0,
        (),
        [],
        3,
        4,
        None,
        None,
        reset=True,
        entry_ids=(),
    )
    stored = json.loads(json.dumps(checkpoint.to_json()))
    assert set(stored) == set(STORED_0_16_1)
    assert _Checkpoint.from_json(stored) == checkpoint
