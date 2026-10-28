"""Human control receipts coexist with a live model writer and survive a crash."""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import textwrap

import httpx
import pytest

from aster.api import create_app
from aster.domain import ModelTurn, ToolCall
from aster.engine.fake import FakeModel
from aster.service import RunCoordinator
from aster.state import SessionBusy
from aster.state.store import dumps

TOKEN = "browser-operator-offline-token-at-least-thirty-two-characters"
pytest_plugins = ["test_browser_bridge"]


@pytest.mark.browser
async def test_operator_pause_settles_live_mutation_and_blocks_next_without_second_writer(tmp_path, owned_cdp):
    ready, release = asyncio.Event(), asyncio.Event()
    model_turns = 0
    page_id = None

    async def respond(messages, tools):
        nonlocal model_turns
        model_turns += 1
        if model_turns == 1:
            return ModelTurn(tool_calls=[ToolCall("inflight", "browser_fill", {"page_id": page_id, "selector": "#delayed", "text": "first accepted", "timeout": 5})], finish_reason="tool_calls")
        if model_turns == 2:
            ready.set()
            await release.wait()
            return ModelTurn(tool_calls=[ToolCall("paused-next", "browser_fill", {"page_id": page_id, "selector": "#name", "text": "must not happen"})], finish_reason="tool_calls")
        return ModelTurn("Finished with paused browser")

    model = FakeModel([respond] * 5)
    coordinator = RunCoordinator(tmp_path / "data", model_factory=lambda settings: model)
    config = {"mode": "bridge", "headless": True, "cdp_endpoint": owned_cdp["endpoint"], "allowed_pages": [owned_cdp["url"]]}
    import yaml
    (coordinator.data_dir / "config.yaml").write_text(yaml.safe_dump({"browser": config, "permissions": {"auto_approve_tools": ["browser_fill"]}}))
    app = create_app(coordinator, token=TOKEN)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
            headers={"Authorization": "Bearer " + TOKEN}) as client:
        sid = coordinator.create_session(tmp_path)["id"]
        endpoint = f"/browser-control/sessions/{sid}"
        attached = (await client.post(endpoint + "/attach", json={"arguments": {"url": owned_cdp["url"]}, "approved": True, "request_id": "attach-once"})).json()
        assert not attached["is_error"], attached
        page_id = attached["data"]["page_id"]
        assert coordinator.store.list_runs(sid) == [] and coordinator.store.history(sid) == []
        # Same transport retry receipt does not create a second SDK lease.
        again = (await client.post(endpoint + "/attach", json={"arguments": {"url": owned_cdp["url"]}, "approved": True, "request_id": "attach-once"})).json()
        assert again == attached and len(coordinator.factory.browser._leases) == 1
        await owned_cdp["page"].evaluate("() => { const e=document.createElement('input'); e.id='delayed'; e.style.display='none'; document.body.appendChild(e); }")
        run = await coordinator.submit(sid, "Run the authorized fixture workflow")
        deadline = asyncio.get_running_loop().time() + 5
        while coordinator.factory.browser._active_response is None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert coordinator._session_gates[sid].locked()
        pause = asyncio.create_task(client.post(endpoint + "/pause", json={"approved": True, "request_id": "pause-once"}))
        deadline = asyncio.get_running_loop().time() + 2
        while not coordinator.browser_controls().paused(sid) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert coordinator.browser_controls().paused(sid) and not pause.done()
        assert len(coordinator.store.list_runs(sid)) == 1
        with pytest.raises(SessionBusy):
            await coordinator.change_session_lifecycle(sid, "delete")
        # User allows the already accepted action to settle; pause never cancels it.
        await owned_cdp["page"].evaluate("document.querySelector('#delayed').style.display='block'")
        result = (await asyncio.wait_for(pause, 5)).json()
        assert not result["is_error"] and result["data"]["paused"]
        await asyncio.wait_for(ready.wait(), 5)
        release.set()
        finished = await coordinator.wait(run["id"], timeout=10)
        assert finished.status == "completed", finished.error
        assert await owned_cdp["page"].input_value("#delayed") == "first accepted"
        assert await owned_cdp["page"].input_value("#name") == ""
        assert len(coordinator.store.list_runs(sid)) == 1
        raw = coordinator.store.history(sid)
        assert [call.id for message in raw for call in message.tool_calls] == ["inflight", "paused-next"]
        assert not any("browser_handoff" == call.name for message in raw for call in message.tool_calls)
        control_state = (await client.get(endpoint)).json()
        assert control_state["paused"] and len(control_state["receipts"]) == 2
        assert (await client.post(endpoint + "/resume", json={"approved": False, "request_id": "unapproved"})).status_code == 403
        resumed = (await client.post(endpoint + "/resume", json={"approved": True, "request_id": "human-resume"})).json()
        assert not resumed["is_error"]
        await coordinator.change_session_lifecycle(sid, "delete")
        assert coordinator.store.connection.execute("SELECT COUNT(*) FROM browser_control_receipts WHERE session_id=?", (sid,)).fetchone()[0] == 0
        assert coordinator.store.connection.execute("SELECT COUNT(*) FROM browser_handoffs WHERE session_id=?", (sid,)).fetchone()[0] == 0
        assert not coordinator.factory.browser._leases and owned_cdp["browser"].is_connected()


@pytest.mark.browser
async def test_own_workbench_targets_and_frames_cannot_self_approve(tmp_path, owned_cdp):
    coordinator = RunCoordinator(tmp_path / "data", model_factory=lambda settings: FakeModel([]))
    import yaml
    config = {"mode": "bridge", "headless": True, "cdp_endpoint": owned_cdp["endpoint"], "allowed_pages": [owned_cdp["url"]]}
    (coordinator.data_dir / "config.yaml").write_text(yaml.safe_dump({"browser": config}))
    origin = owned_cdp["url"].rsplit("/", 1)[0]
    app = create_app(coordinator, token=TOKEN, allowed_origins={origin})
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
            headers={"Authorization": "Bearer " + TOKEN}) as client:
        sid = coordinator.create_session(tmp_path)["id"]
        result = (await client.post(f"/browser-control/sessions/{sid}/attach", json={"arguments": {"url": owned_cdp["url"]}, "approved": True, "request_id": "allowed-testpath"})).json()
        assert not result["is_error"], result
        page_id = result["data"]["page_id"]
        tools = {tool.spec.name: tool for tool in coordinator.factory.tools}
        from aster.domain import ToolContext
        ctx = ToolContext(sid, "r", tmp_path, tmp_path / "artifacts", asyncio.Event(), services={"browser_config": config})
        target = await tools["browser_navigate"].execute({"page_id": page_id, "url": origin + "/"}, ctx)
        assert target.is_error and owned_cdp["page"].url == owned_cdp["url"]
        # An allowed outer test page cannot expose or click a workbench frame.
        await owned_cdp["page"].evaluate("url => {let frame=document.createElement('iframe'); frame.src=url; document.body.appendChild(frame)}", origin + "/")
        await owned_cdp["page"].locator("iframe").wait_for()
        await asyncio.sleep(0.1)
        blocked = await tools["browser_click"].execute({"page_id": page_id, "selector": "#set"}, ctx)
        assert blocked.is_error and "workbench" in blocked.content
        snapshot = await tools["browser_snapshot"].execute({"page_id": page_id}, ctx)
        assert snapshot.is_error
        assert coordinator.store.history(sid) == []


WORKER = r'''
import asyncio,json,os,sys
from pathlib import Path
from aster.domain import ModelTurn,ToolCall,ToolResult,ToolSpec,to_dict
from aster.providers.deepseek import encode_messages
from aster.service import RunCoordinator
data=Path(sys.argv[1]); mode=sys.argv[2]; data.mkdir(parents=True,exist_ok=True)
identity=data/'identity.json'; effect=data/'effect.txt'; ready=asyncio.Event()
class WriteOnce:
    spec=ToolSpec('write_once','Persist one controlled effect',{'type':'object','properties':{},'required':[]},risk='write')
    async def execute(self,args,ctx):
        assert not effect.exists(); effect.write_text('once'); return ToolResult('effect complete')
class WorkerModel:
    model='offline-control-recovery'
    def __init__(self):self.calls=0
    async def generate(self,messages,tools,**kwargs):
        encode_messages(messages); self.calls+=1
        if mode=='crash':
            if self.calls==1:return ModelTurn(tool_calls=[ToolCall('material','write_once',{})],finish_reason='tool_calls')
            ready.set(); await asyncio.Event().wait()
        if self.calls==1:return ModelTurn(tool_calls=[ToolCall('blocked-after-restart','browser_open',{})],finish_reason='tool_calls')
        return ModelTurn('Recovered without replay')
async def main():
    model=WorkerModel(); coordinator=RunCoordinator(data,model_factory=lambda settings:model,extra_tools_factory=lambda:[WriteOnce()])
    if mode=='crash':
        (data/'config.yaml').write_text('permissions:\n  auto_approve_tools: [write_once]\n')
        await coordinator.start(); sid=coordinator.create_session(data)['id']; run=await coordinator.submit(sid,'Write once then continue')
        await asyncio.wait_for(ready.wait(),10)
        paused=await coordinator.browser_controls().execute(sid,'pause',{},'human-pause',approved=True)
        assert not paused.is_error and len(coordinator.store.list_runs(sid))==1
        identity.write_text(json.dumps({'sid':sid,'rid':run['id'],'raw':to_dict(coordinator.store.history(sid)),'snapshot':coordinator.store.run_request(run['id'])}))
        os._exit(93)
    saved=json.loads(identity.read_text()); sid=saved['sid']
    assert coordinator.browser_controls().paused(sid)
    result=await coordinator.recover(saved['rid'])
    archive=coordinator.store.history(sid); encode_messages(archive)
    blocked=next(message for message in archive if message.role=='tool' and message.tool_call_id=='blocked-after-restart')
    status={'status':result.status,'raw_prefix_unchanged':to_dict(archive[:len(saved['raw'])])==saved['raw'],
      'snapshot_unchanged':coordinator.store.run_request(saved['rid'])==saved['snapshot'],'single_run':len(coordinator.store.list_runs(sid))==1,
      'paused_preserved':coordinator.browser_controls().paused(sid),'recovered_mutation_blocked':'paused' in blocked.content,
      'material_results':sum(message.role=='tool' and message.tool_call_id=='material' for message in archive),
      'control_calls_in_raw':sum(call.name=='browser_handoff' for message in archive for call in message.tool_calls)}
    await coordinator.close(); print(json.dumps(status))
asyncio.run(main())
'''


def test_actual_process_crash_paused_recovery_keeps_raw_pairs_single_writer_and_material_effect(tmp_path):
    worker = tmp_path / "control worker.py"
    worker.write_text(textwrap.dedent(WORKER))
    env = {**os.environ, "DEEPSEEK_API_KEY": ""}
    crashed = subprocess.run([sys.executable, str(worker), str(tmp_path / "data"), "crash"], capture_output=True, timeout=20, env=env, check=False)
    assert crashed.returncode == 93, crashed.stderr.decode(errors="replace")
    assert (tmp_path / "data" / "effect.txt").read_text() == "once"
    recovered = subprocess.run([sys.executable, str(worker), str(tmp_path / "data"), "recover"], capture_output=True, timeout=20, env=env, check=False)
    assert recovered.returncode == 0, recovered.stderr.decode(errors="replace")
    assert json.loads(recovered.stdout) == {"status": "completed", "raw_prefix_unchanged": True,
        "snapshot_unchanged": True, "single_run": True, "paused_preserved": True,
        "recovered_mutation_blocked": True, "material_results": 1, "control_calls_in_raw": 0}
    assert (tmp_path / "data" / "effect.txt").read_text() == "once"


async def test_interrupted_control_receipt_not_replayed_and_inspection_then_delete_remains_possible(tmp_path):
    data = tmp_path / "data"
    first = RunCoordinator(data, model_factory=lambda settings: FakeModel([]))
    sid = first.create_session(tmp_path)["id"]
    first.browser_controls()
    digest = hashlib.sha256(dumps({"action": "pause", "arguments": {"operation": "pause"}}).encode()).hexdigest()
    with first.store.transaction() as db:
        db.execute("INSERT INTO browser_control_receipts VALUES(?,?,?,?,?,?,?,?)", (sid, "interrupted", "pause", digest, "dispatching", None, 1, 1))
        db.execute("INSERT INTO browser_handoffs VALUES(?,1,1)", (sid,))
    await first.close()
    second = RunCoordinator(data, model_factory=lambda settings: FakeModel([]))
    try:
        controls = second.browser_controls()
        assert controls.receipts(sid)[0]["state"] == "outcome_unknown" and controls.paused(sid)
        retry = await controls.execute(sid, "pause", {}, "interrupted", approved=True)
        assert retry.outcome == "unknown" and second.factory.browser._owner is None
        assert second.store.history(sid) == [] and second.store.list_runs(sid) == []
        # Explicitly inspect/resolve actual local state with a NEW human action;
        # the uncertain receipt is retained and never silently replayed.
        resolved = await controls.execute(sid, "resume", {}, "new-human-resume", approved=True)
        assert not resolved.is_error and not controls.paused(sid)
        await second.change_session_lifecycle(sid, "delete")
        assert second.store.connection.execute("SELECT COUNT(*) FROM browser_control_receipts").fetchone()[0] == 0
    finally:
        await second.close()


async def test_operator_control_permissions_and_request_identity_are_not_bypassed(tmp_path):
    coordinator = RunCoordinator(tmp_path / "data", model_factory=lambda settings: FakeModel([]))
    try:
        sid = coordinator.create_session(tmp_path)["id"]
        (coordinator.data_dir / "config.yaml").write_text("permissions:\n  denied_tools: [browser_handoff]\n")
        denied = await coordinator.browser_controls().execute(sid, "pause", {}, "denied", approved=True)
        assert denied.is_error and not coordinator.browser_controls().paused(sid)
        assert coordinator.browser_controls().receipts(sid) == []
        (coordinator.data_dir / "config.yaml").write_text("permissions:\n  ask_tools: [browser_handoff]\n")
        assert (await coordinator.browser_controls().execute(sid, "pause", {}, "ask", approved=False)).is_error
        paused = await coordinator.browser_controls().execute(sid, "pause", {}, "ask", approved=True)
        assert not paused.is_error and coordinator.browser_controls().paused(sid)
        from aster.state import StateError
        with pytest.raises(StateError, match="reused"):
            await coordinator.browser_controls().execute(sid, "resume", {}, "ask", approved=True)
        await coordinator.change_session_lifecycle(sid, "archive")
        assert coordinator.browser_controls().paused(sid)
        await coordinator.change_session_lifecycle(sid, "unarchive")
        await coordinator.change_session_lifecycle(sid, "delete")
        assert coordinator.store.connection.execute("SELECT COUNT(*) FROM browser_handoffs").fetchone()[0] == 0
    finally:
        await coordinator.close()
