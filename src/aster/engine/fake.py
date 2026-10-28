"""Deterministic model for offline scenario and fault-injection tests."""
from __future__ import annotations

import asyncio
import copy
import inspect

from aster.domain import ModelTurn


class FakeModel:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls: list[dict] = []

    async def generate(self, messages, tools, *, system, emit=None, cancel=None):
        if cancel and cancel.is_set():
            raise asyncio.CancelledError
        self.calls.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools), "system": system})
        if not self.turns:
            raise RuntimeError("FakeModel exhausted its scripted responses")
        turn = self.turns.pop(0)
        if isinstance(turn, BaseException):
            raise turn
        if callable(turn):
            turn = turn(messages, tools)
            if inspect.isawaitable(turn):
                turn = await turn
        if not isinstance(turn, ModelTurn):
            raise TypeError("FakeModel requires a ModelTurn or callable producing one")
        if emit and turn.text:
            await emit("model.text.delta", {"text": turn.text})
        return copy.deepcopy(turn)
