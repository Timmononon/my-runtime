"""Durable, at-most-one resolution for approvals and questions."""
from __future__ import annotations

import asyncio
import time

from aster.domain import ToolContext
from aster.state import Store


class PendingBroker:
    def __init__(self, store: Store, *, timeout_seconds: float = 300):
        self.store = store
        self.timeout_seconds = timeout_seconds
        self._wakeups: dict[str, asyncio.Event] = {}

    def resolve(self, request_id: str, decision) -> bool:
        resolved = self.store.resolve_pending(request_id, decision)
        event = self._wakeups.get(request_id)
        if event:
            event.set()
        return resolved

    def cancel(self, request_id: str) -> None:
        self.store.resolve_pending(request_id, None, state="cancelled")
        if request_id in self._wakeups:
            self._wakeups[request_id].set()

    def cancel_run(self, run_id: str) -> None:
        for item in self.store.pending_requests(run_id):
            self.cancel(item["id"])

    async def request(self, kind: str, payload: dict, context: ToolContext):
        if context.cancel.is_set():
            return None
        pending = self.store.create_pending(context.session_id, context.run_id, kind, payload, self.timeout_seconds)
        pid = pending["id"]
        wakeup = self._wakeups[pid] = asyncio.Event()
        phase = "waiting_approval" if kind == "approval" else "waiting_input"
        self.store.set_run_status(context.run_id, phase)
        try:
            if context.emit:
                await context.emit(f"{kind}.waiting", {"request_id": pid, **payload, "expires_at": pending["expires_at"]})
            while True:
                row = self.store.get_pending(pid)
                if row["state"] != "pending":
                    return row["decision"] if row["state"] == "resolved" else None
                if context.cancel.is_set():
                    self.cancel(pid)
                    return None
                remaining = pending["expires_at"] - time.time()
                if remaining <= 0:
                    self.store.resolve_pending(pid, None, state="expired")
                    return None
                # Polling also observes decisions persisted by another API path.
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=min(remaining, 0.1))
                except TimeoutError:
                    pass
                wakeup.clear()
        except asyncio.CancelledError:
            self.cancel(pid)
            raise
        finally:
            self._wakeups.pop(pid, None)
            run = self.store.get_run(context.run_id)
            if run["status"] in ("waiting_approval", "waiting_input"):
                self.store.set_run_status(context.run_id, "running")


class ApprovalBroker(PendingBroker):
    async def approve(self, payload: dict, context: ToolContext) -> bool:
        decision = await self.request("approval", payload, context)
        return decision is True or (isinstance(decision, dict) and decision.get("approved") is True)


class QuestionBroker(PendingBroker):
    async def ask(self, payload: dict, context: ToolContext):
        return await self.request("question", payload, context)
