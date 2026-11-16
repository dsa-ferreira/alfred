"""A parked turn's checkpoint, as stored in ``input_requests.turn``."""

from __future__ import annotations

import json

from alfred.agent.checkpoint import Checkpoint
from alfred.llm.chat import ToolCall

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

# What a parked turn stores.
STORED = {
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


def test_reads_what_it_stored() -> None:
    checkpoint = Checkpoint.from_json(STORED)
    assert checkpoint.call == ToolCall("c1", "mcparr_add_media", '{"title": "Dune"}')
    assert checkpoint.call_index == 1
    assert checkpoint.remaining == (ToolCall("c2", "alfred_remind_set", "{}"),)
    assert checkpoint.tail == _TAIL
    assert (checkpoint.requests_used, checkpoint.seq) == (1, 2)
    assert (checkpoint.conversation_id, checkpoint.upto_entry_id) == (7, 41)
    assert checkpoint.reset is False
    assert checkpoint.entry_ids == (38, 41)


def test_writes_the_same_keys_and_values() -> None:
    assert Checkpoint.from_json(STORED).to_json() == STORED


def test_round_trip_through_json() -> None:
    checkpoint = Checkpoint(
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
    assert set(stored) == set(STORED)
    assert Checkpoint.from_json(stored) == checkpoint
