"""Independent acceptance: real process crashes, authority races and restored artifacts."""
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import textwrap
import uuid
import xml.etree.ElementTree as ET
from functools import partial
from pathlib import Path

import httpx
import pytest

from aster.api.app import create_app
from aster.domain import Message, ModelTurn, ToolCall, ToolContext, ToolResult, ToolSpec
from aster.engine import Engine
from aster.engine.dispatcher import Dispatcher
from aster.engine.fake import FakeModel
from aster.maintenance import autostart_definition, backup_bundle, resolve_artifact, restore_bundle
from aster.platform.processes import ProcessSupervisor
from aster.policy import ApprovalBroker, PermissionPolicy
from aster.providers.deepseek import encode_messages
from aster.service.coordinator import RunCoordinator
from aster.state import Store
from aster.tools.process import ProcessTool
from aster.tools.registry import Registry

CRASH_WORKER = r'''
import asyncio,json,os,sys
from pathlib import Path
from aster.domain import ModelTurn,ToolCall,ToolResult,ToolSpec
from aster.engine import Engine
from aster.engine.fake import FakeModel
from aster.maintenance import ServiceLease
from aster.policy import PermissionPolicy
from aster.state import Store
from aster.tools.registry import Registry

data=Path(sys.argv[1]); phase=sys.argv[2]; mode=sys.argv[3]
data.mkdir(parents=True,exist_ok=True)
effect=data/'effect.txt'; identity=data/'identity.json'
class FaultStore(Store):
    def commit_tool(self,*args,**kwargs):
        result=super().commit_tool(*args,**kwargs)
        if phase=='commit' and mode=='crash': os._exit(72)
        return result
    def checkpoint(self,*args,**kwargs):
        result=super().checkpoint(*args,**kwargs)
        if phase=='checkpoint' and mode=='crash' and effect.exists(): os._exit(73)
        return result
class WriteOnce:
    spec=ToolSpec('write_once','Append one durable line',{'type':'object','properties':{},'required':[], 'additionalProperties':False},risk='write')
    async def execute(self,args,context):
        identity.write_text(json.dumps({'session_id':context.session_id,'run_id':context.run_id}))
        with effect.open('ab') as output:
            output.write(b'one external action\n'); output.flush(); os.fsync(output.fileno())
        if phase=='dispatch' and mode=='crash': os._exit(71)
        return ToolResult('one line appended')
async def main():
    with ServiceLease(data,host=None,port=None):
        store=FaultStore(data/'runtime.db')
        if mode=='crash':
            sid=store.create_session(data)['id']
            model=FakeModel([ModelTurn(tool_calls=[ToolCall('write-1','write_once',{})]),ModelTurn(text='done')])
        else:
            sid=json.loads(identity.read_text())['session_id']; model=FakeModel([ModelTurn(text='recovered')])
        engine=Engine(model,Registry([WriteOnce()]),store,policy=PermissionPolicy(allowed_tools={'write_once'}))
        if mode=='crash':
            await engine.run(sid,'append once')
        else:
            run_id=json.loads(identity.read_text())['run_id']
            result=await engine.recover(run_id)
            print(json.dumps({'status':result.status,'model_calls':len(model.calls),'history_tool_results':sum(m.role=='tool' for m in store.history(sid))}))
        store.close()
asyncio.run(main())
'''


@pytest.mark.parametrize("phase,code,expected", [("dispatch", 71, "needs_reconciliation"),
                                                ("commit", 72, "completed"),
                                                ("checkpoint", 73, "completed")])
def test_actual_cross_process_crash_never_repeats_effect(tmp_path, phase, code, expected):
    script = tmp_path / "crash worker.py"
    script.write_text(textwrap.dedent(CRASH_WORKER), encoding="utf-8")
    data = tmp_path / "中文 runtime"
    crashed = subprocess.run([sys.executable, str(script), str(data), phase, "crash"],
                             capture_output=True, timeout=15, check=False)
    assert crashed.returncode == code, crashed.stderr.decode(errors="replace")
    assert (data / "effect.txt").read_bytes() == b"one external action\n"
    recovered = subprocess.run([sys.executable, str(script), str(data), phase, "recover"],
                               capture_output=True, timeout=15, check=False)
    assert recovered.returncode == 0, recovered.stderr.decode(errors="replace")
    result = json.loads(recovered.stdout)
    assert result["status"] == expected
    assert result["history_tool_results"] == 1
    assert result["model_calls"] == (0 if phase == "dispatch" else 1)
    assert (data / "effect.txt").read_bytes() == b"one external action\n"


class RecordingWrite:
    spec = ToolSpec("record_write", "Record approved target", {"type": "object", "properties": {
        "target": {"type": "string"}}, "required": ["target"], "additionalProperties": False},
        risk="write", resource="protected-resource")

    def __init__(self):
        self.targets = []

    async def execute(self, arguments, context):
        self.targets.append(arguments["target"])
        return ToolResult("recorded")


COMPACTION_WORKER = r'''
import asyncio,json,os,sys
from pathlib import Path
from aster.domain import Message,ModelTurn,ToolCall,ToolResult,ToolSpec,to_dict
from aster.engine import Engine,RunConfig
from aster.engine.fake import FakeModel
from aster.maintenance import ServiceLease
from aster.policy import PermissionPolicy
from aster.providers.deepseek import encode_messages
from aster.state import Store
from aster.tools.registry import Registry
data=Path(sys.argv[1]); mode=sys.argv[2]; data.mkdir(parents=True,exist_ok=True)
effect=data/'effect.txt'; identity=data/'identity.json'
constraint='Never delete original measurements. Keep D:/critical/middle.csv unchanged.'
class CrashStore(Store):
    def checkpoint(self,*args,**kwargs):
        result=super().checkpoint(*args,**kwargs)
        if mode=='crash' and effect.exists(): os._exit(93)
        return result
class WriteOnce:
    spec=ToolSpec('write_once','Append once',{'type':'object','properties':{},'required':[]},risk='write')
    async def execute(self,args,ctx):
        identity.write_text(json.dumps({'session_id':ctx.session_id,'run_id':ctx.run_id}))
        with effect.open('ab') as stream:
            stream.write(b'once\n'); stream.flush(); os.fsync(stream.fileno())
        return ToolResult('durable write completed')
class ReadAgain:
    spec=ToolSpec('read_again','Read a numbered observation',{'type':'object','properties':{'number':{'type':'integer'}},'required':['number']},risk='read',replay_safe=True)
    async def execute(self,args,ctx): return ToolResult('observation '+str(args['number']))
async def main():
    with ServiceLease(data,host=None,port=None):
        store=CrashStore(data/'runtime.db')
        if mode=='crash':
            sid=store.create_session(data)['id']; prior,_=store.create_run(sid)
            original=[Message('user','Analyze the long measurement history')]
            for number in range(18):
                if number==8: original.append(Message('user',constraint))
                original.extend([Message('assistant',tool_calls=[ToolCall('old-'+str(number),'read_again',{'number':number})]),
                                 Message('tool','full original observation '+str(number)+' '+('data '*300),tool_call_id='old-'+str(number))])
            for message in original: store.append_message(sid,prior['id'],message)
            store.set_run_status(prior['id'],'completed')
            (data/'archive-prefix.json').write_text(json.dumps(to_dict(original)))
        else: sid=json.loads(identity.read_text())['session_id']
        normal=0
        def respond(messages,tools):
            nonlocal normal
            if not tools: return ModelTurn('<summary>## Current task & next steps\nAnalyze measurements and continue the task.\n## User corrections & decisions\nPreserve original measurements.\n## Open files / important reads\nOlder observations were reviewed; exact evidence remains in session history.</summary>')
            normal+=1
            if mode=='crash': return ModelTurn(tool_calls=[ToolCall('material','write_once',{})])
            if normal<=2: return ModelTurn(tool_calls=[ToolCall('continued-'+str(normal),'read_again',{'number':100+normal})])
            return ModelTurn('complete after recovery')
        model=FakeModel([respond]*20)
        config=RunConfig(context_tokens=6000,reserve_tokens=500,retained_context_rounds=2,max_iterations=15,summary_max_chunks=12)
        engine=Engine(model,Registry([WriteOnce(),ReadAgain()]),store,
                      config=config,policy=PermissionPolicy(allowed_tools={'write_once'}))
        if mode=='crash': await engine.run(sid,'Write the one output and then continue analysis')
        else:
            result=await engine.recover(json.loads(identity.read_text())['run_id'])
            archive=store.history(sid); active=store.active_history(sid)
            prefix=json.loads((data/'archive-prefix.json').read_text())
            main_calls=[call for call in model.calls if call['tools']]
            encode_messages(active)  # actual provider pairing validation, without a network call
            print(json.dumps({'status':result.status,'main_calls':len(main_calls),
                'original_archive_unchanged':to_dict(archive[:len(prefix)])==prefix,
                'constraint_preserved':all(constraint in '\n'.join(m.content for m in call['messages']) for call in main_calls),
                'semantic_checkpoint':any(m.metadata.get('semantic') for m in active),
                'compacted':len(active)<len(archive),
                'material_results':sum(m.role=='tool' and m.tool_call_id=='material' for m in archive),
                'continued_results':sum(m.role=='tool' and str(m.tool_call_id).startswith('continued-') for m in archive)}))
        store.close()
asyncio.run(main())
'''


def test_long_compaction_crash_restart_continues_tools_without_repeating_effect(tmp_path):
    worker = tmp_path / "compaction worker.py"
    worker.write_text(textwrap.dedent(COMPACTION_WORKER), encoding="utf-8")
    data = tmp_path / "long history"
    crashed = subprocess.run([sys.executable, str(worker), str(data), "crash"],
                             capture_output=True, timeout=20, check=False)
    assert crashed.returncode == 93, crashed.stderr.decode(errors="replace")
    assert (data / "effect.txt").read_bytes() == b"once\n"
    resumed = subprocess.run([sys.executable, str(worker), str(data), "recover"],
                             capture_output=True, timeout=20, check=False)
    assert resumed.returncode == 0, resumed.stderr.decode(errors="replace")
    evidence = json.loads(resumed.stdout)
    assert evidence == {"status": "completed", "main_calls": 3, "original_archive_unchanged": True,
                        "constraint_preserved": True, "semantic_checkpoint": True, "compacted": True,
                        "material_results": 1, "continued_results": 2}
    assert (data / "effect.txt").read_bytes() == b"once\n"


async def test_approval_cannot_authorize_mutated_arguments_after_resource_wait(tmp_path):
    store = Store(tmp_path / "runtime.db")
    session = store.create_session(tmp_path)
    run, _ = store.create_run(session["id"])
    broker = ApprovalBroker(store)
    tool = RecordingWrite()
    admitted = asyncio.Event()

    class ObservedDispatcher(Dispatcher):
        async def _execute_one(self, tool, call, context):
            admitted.set()
            return await super()._execute_one(tool, call, context)

    dispatcher = ObservedDispatcher(Registry([tool]), store, PermissionPolicy(), broker)
    lock = dispatcher._resources.setdefault("protected-resource", asyncio.Lock())
    await lock.acquire()

    async def approve(kind, payload):
        if kind == "approval.waiting":
            broker.resolve(payload["request_id"], True)

    context = ToolContext(session["id"], run["id"], tmp_path, tmp_path / "artifacts", asyncio.Event(), emit=approve)
    call = ToolCall("write", "record_write", {"target": "approved-A"})
    task = asyncio.create_task(dispatcher.execute([call], context))
    await asyncio.wait_for(admitted.wait(), 3)
    call.arguments["target"] = "unapproved-B"
    lock.release()
    result = (await task)[0]
    assert tool.targets in ([], ["approved-A"]), "Changed arguments inherited another call's approval"
    assert result.is_error or tool.targets == ["approved-A"]
    store.close()


async def test_late_approval_after_engine_cancel_never_executes(tmp_path):
    store = Store(tmp_path / "runtime.db")
    session = store.create_session(tmp_path)
    broker = ApprovalBroker(store)
    tool = RecordingWrite()
    waiting = asyncio.Event()
    request = {}

    async def emit(kind, payload):
        if kind == "approval.waiting":
            request.update(payload)
            waiting.set()

    model = FakeModel([ModelTurn(tool_calls=[ToolCall("a", "record_write", {"target": "never"})])])
    engine = Engine(model, Registry([tool]), store, approvals=broker)
    running = asyncio.create_task(engine.run(session["id"], "write", emit=emit))
    await asyncio.wait_for(waiting.wait(), 3)
    run_id = store.list_runs(session["id"])[0]["id"]
    assert engine.cancel(run_id)
    assert broker.resolve(request["request_id"], True) is False
    outcome = await running
    assert outcome.status == "cancelled" and not tool.targets
    assert store.get_pending(request["request_id"])["state"] == "cancelled"
    store.close()


async def test_restore_provider_image_and_authenticated_api_download(tmp_path):
    import io

    from PIL import Image

    source = tmp_path / "source"
    source.mkdir()
    store = Store(source / "runtime.db")
    session = store.create_session(source)
    image_path = source / "artifacts" / "example.png"
    image_path.parent.mkdir()
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, "PNG")
    image_bytes = output.getvalue()
    image_path.write_bytes(image_bytes)
    call = ToolCall("screenshot-call", "browser_screenshot", {})
    store.append_message(session["id"], None, Message("assistant", tool_calls=[call]))
    record = {"artifact_id": "screenshot-id", "path": str(image_path), "mime_type": "image/png"}
    tool_message = Message("tool", "Screenshot", tool_call_id=call.id, metadata={"artifacts": [record],
        "content_parts": [{"type": "input_image", "artifact_id": record["artifact_id"],
                           "path": str(image_path), "mime_type": "image/png"}]})
    store.append_message(session["id"], None, tool_message)
    package = backup_bundle(source, tmp_path / "bundle.zip")
    store.close()
    destination = tmp_path / "restored"
    restore_bundle(package, destination)
    image_path.rename(image_path.with_suffix(".old"))  # prove the old path cannot accidentally satisfy the test
    coordinator = RunCoordinator(destination, tools=[], model_factory=lambda settings: FakeModel([]))
    try:
        messages = coordinator.store.history(session["id"])
        encoded = encode_messages(messages, destination / "artifacts", partial(resolve_artifact, destination))
        image_part = encoded[-1]["output"][0]
        assert base64.b64decode(image_part["image_url"].split(",", 1)[1]) == image_bytes
        assert messages[-1].metadata["content_parts"][0]["path"] == str(image_path)
        token = "local-test-token-that-is-long-enough"
        app = create_app(coordinator, token=token, manage_lifecycle=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            denied = await client.get("/artifacts/screenshot-id")
            assert denied.status_code == 401
            response = await client.get("/artifacts/screenshot-id", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200 and response.content == image_bytes
            assert response.headers["content-type"].startswith("image/png")
            missing = await client.get("/artifacts/unregistered", headers={"Authorization": f"Bearer {token}"})
            assert missing.status_code == 404
    finally:
        await coordinator.close()


@pytest.mark.skipif(os.name != "nt" or os.environ.get("ASTER_TEST_INSTALL") != "1",
                    reason="Explicit temporary-environment installation acceptance")
def test_actual_installer_builds_fresh_isolated_environment(tmp_path):
    root = Path(__file__).resolve().parents[1]
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable
    environment = tmp_path / "真实 安装环境"
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-File",
                             str(root / "scripts/Install-Aster.ps1"), "-ProjectRoot", str(root),
                             "-PythonExe", sys.executable, "-VenvPath", str(environment),
                             "-IndexUrl", "https://pypi.org/simple"],
                            capture_output=True, timeout=600, check=False, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stdout[-5000:] + result.stderr[-5000:]
    python = environment / "Scripts/python.exe"
    checked = subprocess.run([str(python), "-c", "import json,sys,aster,httpx,playwright,pypdf,docx,openpyxl,pptx; print(json.dumps(sys.prefix))"],
                             capture_output=True, timeout=30, check=False, encoding="utf-8", errors="replace")
    assert checked.returncode == 0, checked.stderr
    assert Path(json.loads(checked.stdout)).resolve() == environment.resolve()
    cli = subprocess.run([str(python), "-m", "aster", "--help"], capture_output=True, timeout=30, check=False)
    assert cli.returncode == 0 and b"daemon" in cli.stdout


@pytest.mark.skipif(os.environ.get("ASTER_TEST_SLOW_PROCESSES") != "1", reason="Explicit 61-second subprocess acceptance")
async def test_requested_process_timeout_is_not_silently_capped_at_sixty_seconds(tmp_path):
    store = Store(tmp_path / "runtime.db")
    session = store.create_session(tmp_path)
    supervisor = ProcessSupervisor()
    tool = ProcessTool(supervisor)
    call = ToolCall("long-process", "process_exec", {"executable": sys.executable,
                    "argv": ["-c", "import time; time.sleep(61); print('finished after 61 seconds')"], "timeout": 70})
    model = FakeModel([ModelTurn(tool_calls=[call]), ModelTurn(text="done")])
    engine = Engine(model, Registry([tool]), store, policy=PermissionPolicy(allowed_tools={"process_exec"}))
    try:
        outcome = await engine.run(session["id"], "run with requested timeout")
        assert outcome.status == "completed"
        results = [item for item in store.history(session["id"]) if item.role == "tool"]
        assert len(results) == 1 and "finished after 61 seconds" in results[0].content
        assert not results[0].metadata["is_error"]
    finally:
        await supervisor.close()
        store.close()


@pytest.mark.skipif(os.name != "nt" or os.environ.get("ASTER_TEST_TASK_SCHEDULER") != "1",
                    reason="Explicit temporary disabled Task Scheduler validation")
def test_disabled_temporary_task_scheduler_registration_and_removal(tmp_path):
    definition = autostart_definition(tmp_path, [sys.executable, "-c", "pass"])
    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    task = ET.fromstring(definition["xml"])
    task.find("t:Triggers/t:LogonTrigger/t:Enabled", namespace).text = "false"
    settings = task.find("t:Settings", namespace)
    ET.SubElement(settings, "{http://schemas.microsoft.com/windows/2004/02/mit/task}Enabled").text = "false"
    xml_path = tmp_path / "disabled-task.xml"
    xml_path.write_text(ET.tostring(task, encoding="unicode"), encoding="utf-16")
    task_name = "AsterAcceptance-" + uuid.uuid4().hex
    registered = False
    try:
        created = subprocess.run(["schtasks.exe", "/Create", "/TN", task_name, "/XML", str(xml_path)],
                                 capture_output=True, timeout=15, check=False, creationflags=0x08000000)
        if created.returncode and (b"Access is denied" in created.stderr or b"\xbe\xdc\xbe\xf8\xb7\xc3\xce\xca" in created.stderr):
            pytest.skip("Current user is denied Task Scheduler registration; no task created")
        assert created.returncode == 0, created.stderr.decode(errors="replace")
        registered = True
        queried = subprocess.run(["schtasks.exe", "/Query", "/TN", task_name, "/XML"],
                                 capture_output=True, timeout=15, check=False, creationflags=0x08000000)
        assert queried.returncode == 0
        encoding = "utf-16-le" if b"\0" in queried.stdout else "utf-8-sig"
        persisted = ET.fromstring(queried.stdout.decode(encoding))
        assert persisted.find("t:Settings/t:Enabled", namespace).text == "false"
        assert persisted.find("t:Triggers/t:LogonTrigger/t:Enabled", namespace).text == "false"
    finally:
        if registered:
            removed = subprocess.run(["schtasks.exe", "/Delete", "/TN", task_name, "/F"],
                                     capture_output=True, timeout=15, check=False, creationflags=0x08000000)
            assert removed.returncode == 0, "Temporary task cleanup failed"
            verified = subprocess.run(["schtasks.exe", "/Query", "/TN", task_name],
                                      capture_output=True, timeout=15, check=False, creationflags=0x08000000)
            assert verified.returncode != 0, "Temporary task still registered"
