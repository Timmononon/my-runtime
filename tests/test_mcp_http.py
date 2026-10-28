"""Real loopback Streamable HTTP, including a process death after a write."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from aster.domain import ToolContext
from aster.mcp import MCPClient, MCPServerConfig
from aster.platform.processes import ProcessSupervisor

SERVER = r"""
import asyncio,json,os,socket,sys
from pathlib import Path
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

root=Path(sys.argv[1])
port=int(sys.argv[2])
state=root/'state.json'
if not state.exists(): state.write_text(json.dumps({'writes':0,'deletes':0}))
server=FastMCP('real-http-test',host='127.0.0.1',log_level='ERROR')
@server.tool(annotations=ToolAnnotations(readOnlyHint=True,idempotentHint=True))
async def read_counter():
    return json.loads(state.read_text())
@server.tool(annotations=ToolAnnotations(readOnlyHint=False,idempotentHint=False))
async def write_then_disconnect():
    value=json.loads(state.read_text())
    value['writes']+=1
    state.write_text(json.dumps(value))
    os._exit(23)
@server.custom_route('/health',methods=['GET'])
async def health(request):
    return JSONResponse({'ready':True})
app=server.streamable_http_app()
async def count_delete(request,call_next):
    if request.method=='DELETE':
        value=json.loads(state.read_text())
        value['deletes']+=1
        state.write_text(json.dumps(value))
    return await call_next(request)
app.add_middleware(BaseHTTPMiddleware,dispatch=count_delete)
async def main():
    sock=socket.socket()
    sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    sock.bind(('127.0.0.1',port))
    root.joinpath('port.txt').write_text(str(sock.getsockname()[1]))
    config=uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False)
    await uvicorn.Server(config).serve(sockets=[sock])
asyncio.run(main())
"""


@asynccontextmanager
async def real_server(root: Path, *, port: int = 0):
    script = root / "server.py"
    script.write_text(SERVER, encoding="utf-8")
    port_file = root / "port.txt"
    if port_file.exists():
        port_file.unlink()
    supervisor = ProcessSupervisor()
    cancel = asyncio.Event()
    task = asyncio.create_task(
        supervisor.run(
            [sys.executable, str(script), str(root), str(port)],
            cwd=root,
            artifact_dir=root / ("process-" + str(port)),
            cancel=cancel,
            timeout=40,
        )
    )
    try:
        async with httpx.AsyncClient(timeout=0.3) as probe:
            for _ in range(200):
                if task.done():
                    result = task.result()
                    pytest.fail(f"HTTP fixture exited: {result.stderr[-1000:]}")
                if port_file.exists():
                    bound = int(port_file.read_text())
                    try:
                        response = await probe.get(f"http://127.0.0.1:{bound}/health")
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                await asyncio.sleep(0.03)
            else:
                pytest.fail("HTTP fixture did not become ready")
        yield bound, task
    finally:
        cancel.set()
        await asyncio.wait_for(task, 10)
        await supervisor.close()


def tool_context(root):
    return ToolContext("http-session", "http-run", root, root / "artifacts", asyncio.Event())


@pytest.mark.asyncio
async def test_real_http_connect_list_call_close(tmp_path):
    async with real_server(tmp_path) as (port, _):
        config = MCPServerConfig(
            "real",
            url=f"http://127.0.0.1:{port}/mcp",
            timeout_seconds=5,
            connect_attempts=1,
            trust_annotations=True,
        )
        async with MCPClient([config]) as client:
            tools = await client.list_tools()
            assert {tool.remote_name for tool in tools} == {"read_counter", "write_then_disconnect"}
            read = next(tool for tool in tools if tool.remote_name == "read_counter")
            result = await read.execute({}, tool_context(tmp_path))
            assert not result.is_error
            assert json.loads(result.content)["writes"] == 0
        assert json.loads((tmp_path / "state.json").read_text())["deletes"] == 1


@pytest.mark.asyncio
async def test_real_http_disconnect_no_replay_and_read_reconnect(tmp_path):
    async with real_server(tmp_path) as (port, process):
        config = MCPServerConfig(
            "real",
            url=f"http://127.0.0.1:{port}/mcp",
            timeout_seconds=3,
            connect_attempts=1,
            trust_annotations=True,
        )
        client = MCPClient([config])
        try:
            tools = await client.list_tools()
            write = next(tool for tool in tools if tool.remote_name == "write_then_disconnect")
            result = await asyncio.wait_for(write.execute({}, tool_context(tmp_path)), 10)
            assert result.is_error and result.outcome == "unknown"
            await asyncio.wait_for(process, 5)
            assert json.loads((tmp_path / "state.json").read_text())["writes"] == 1
            async with real_server(tmp_path, port=port):
                tools = await asyncio.wait_for(client.list_tools(), 10)
                read = next(tool for tool in tools if tool.remote_name == "read_counter")
                result = await read.execute({}, tool_context(tmp_path))
                assert not result.is_error
                assert json.loads(result.content)["writes"] == 1
                await client.close()
        finally:
            await client.close()
