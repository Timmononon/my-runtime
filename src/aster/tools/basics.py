"""Small independent tools, with bounded arithmetic and session-owned OS actions."""
from __future__ import annotations

import ast
import base64
import json
import math
import operator
import os
import platform
import shutil
import time
import uuid
from datetime import UTC, datetime
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import psutil

from aster.domain import ToolResult, ToolSpec
from aster.tools.files import TEXT, failure, schema

# These defaults bound interactive arithmetic: exceeding one returns a clear
# budget error. Operators can change calculate_budget in trusted config.yaml.
CALCULATE_DEFAULTS = {"expression_chars": 1000, "nodes": 128, "digits": 200,
                      "exponent": 1000, "result_bits": 4096, "seconds": 0.05}


def calculate(expression, settings=None):
    budget = {**CALCULATE_DEFAULTS, **(settings or {})}
    if set(budget) != set(CALCULATE_DEFAULTS) or any(type(value) not in {int, float} or not math.isfinite(value) or value <= 0 for value in budget.values()):
        raise ValueError("Invalid configured calculate_budget")
    if not isinstance(expression, str) or len(expression) > budget["expression_chars"]:
        raise ValueError("Arithmetic expression exceeds configured character budget")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > budget["nodes"]:
        raise ValueError("Arithmetic expression exceeds configured node budget")
    began = time.monotonic()

    def checked(value):
        if type(value) not in {int, float} or (type(value) is float and not math.isfinite(value)):
            raise ValueError("Arithmetic requires finite real numbers")
        if type(value) is int and value.bit_length() > budget["result_bits"]:
            raise ValueError("Arithmetic exceeds configured result bit budget")
        return value

    def evaluate(node):
        if time.monotonic() - began > budget["seconds"]:
            raise ValueError("Arithmetic exceeds configured execution budget")
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            if len(ast.get_source_segment(expression, node).replace("_", "")) > budget["digits"]:
                raise ValueError("Arithmetic literal exceeds configured digit budget")
            return checked(node.value)
        if isinstance(node, ast.UnaryOp) and type(node.op) in {ast.UAdd, ast.USub}:
            return checked((operator.pos if isinstance(node.op, ast.UAdd) else operator.neg)(evaluate(node.operand)))
        if isinstance(node, ast.BinOp) and type(node.op) in {ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow}:
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Pow):
                if abs(right) > budget["exponent"]:
                    raise ValueError("Arithmetic exponent exceeds configured budget")
                if abs(left) > 1 and right > 0 and math.log2(abs(left)) * right > budget["result_bits"]:
                    raise ValueError("Power would exceed configured result bit budget")
            operation = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                         ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
                         ast.Mod: operator.mod, ast.Pow: operator.pow}[type(node.op)]
            return checked(operation(left, right))
        raise ValueError("Only numeric literals and arithmetic operators are allowed; no names, calls or code")

    return evaluate(tree.body)


class BasicTool:
    def __init__(self, name):
        properties, required = ({"expression": TEXT}, ["expression"]) if name == "calculate" else ({"timezone": TEXT}, []) if name == "current_time" else ({}, [])
        self.spec = ToolSpec(name, "Bounded arithmetic" if name == "calculate" else "Read explicit timezone time" if name == "current_time" else
                             "Read safe platform CPU memory and working disk metadata without environment or user data",
                             schema(properties, required), risk="read", replay_safe=True, concurrency_safe=True)

    async def execute(self, arguments, context):
        try:
            if self.spec.name == "calculate":
                result = {"value": calculate(arguments["expression"], context.services.get("calculate_budget"))}
            elif self.spec.name == "current_time":
                timezone = arguments.get("timezone", "UTC")
                result = {"timezone": timezone, "time": datetime.now(UTC).astimezone(ZoneInfo(timezone)).isoformat()}
            else:
                memory, disk = psutil.virtual_memory(), psutil.disk_usage(str(context.cwd))
                result = {"platform": platform.system(), "release": platform.release(), "architecture": platform.machine(),
                          "cpu_logical": psutil.cpu_count(), "cpu_physical": psutil.cpu_count(logical=False),
                          "memory_total": memory.total, "memory_available": memory.available,
                          "disk_total": disk.total, "disk_free": disk.free}
            return ToolResult(json.dumps(result), data=result)
        except (ValueError, SyntaxError, ArithmeticError, KeyError, OSError) as error:
            return failure(error, outcome="not_executed")


class ProcessInspectTool:
    def __init__(self, supervisor, action):
        self.supervisor, self.action = supervisor, action
        required = ["pid", "expected_create_time"] if action == "kill" else []
        self.spec = ToolSpec("process_" + action, "Inspect or stop only the current session's Runtime-managed roots; no command lines or environment",
                             schema({"pid": {"type": "integer", "minimum": 1},
                                     "expected_create_time": {"type": "number", "exclusiveMinimum": 0}}, required),
                             risk="execute" if action == "kill" else "read", replay_safe=action != "kill",
                             concurrency_safe=action != "kill", resource="managed_processes")

    async def execute(self, arguments, context):
        try:
            if self.action == "kill":
                result = self.supervisor.stop_owned(context.session_id, arguments["pid"], arguments["expected_create_time"])
            else:
                rows = self.supervisor.inspect(context.session_id)
                if self.action == "ports":
                    for row in rows:
                        row["ports"] = []
                        try:
                            process = psutil.Process(row["pid"])
                            if process.create_time() != row["create_time"]:
                                continue
                            row["ports"] = [{"address": item.laddr.ip, "port": item.laddr.port,
                                             "protocol": "tcp" if item.type == 1 else "udp", "status": item.status}
                                            for item in process.net_connections(kind="inet") if item.laddr]
                        except (psutil.AccessDenied, psutil.NoSuchProcess):
                            row["ports_unavailable"] = True
                result = {"processes": rows, "scope": "current_session_managed_roots"}
            return ToolResult(json.dumps(result), data=result)
        except (ValueError, PermissionError, KeyError, psutil.Error) as error:
            return failure(error, outcome="not_executed")


class NotifyTool:
    spec = ToolSpec("notify", "Submit a Windows notification; submission cannot prove delivery or that the user read it",
                    schema({"title": {"type": "string", "minLength": 1, "maxLength": 200},
                            "message": {"type": "string", "minLength": 1, "maxLength": 2000}}, ["title", "message"]),
                    risk="external", replay_safe=False, resource="notification", timeout_seconds=20)

    async def execute(self, arguments, context):
        if os.name != "nt":
            return ToolResult("Windows notifications are unavailable on this platform", True, "unsupported", "not_executed")
        if context.cancel.is_set():
            return ToolResult("Cancelled before notification submission", True, "cancelled", "not_executed")
        executable = shutil.which("powershell")
        if not executable:
            return ToolResult("Windows PowerShell notification runtime is unavailable", True, "unsupported", "not_executed")
        context.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = context.artifact_dir / (uuid.uuid4().hex + "-notification.xml")
        path.write_text('<toast><visual><binding template="ToastGeneric"><text>' + escape(arguments["title"]) +
                        '</text><text>' + escape(arguments["message"]) + '</text></binding></visual></toast>', encoding="utf-8")
        quoted = str(path).replace("'", "''")
        script = ("$ErrorActionPreference='Stop'; "
                  "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null; "
                  "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime] > $null; "
                  "$doc=New-Object Windows.Data.Xml.Dom.XmlDocument; "
                  f"$doc.LoadXml([System.IO.File]::ReadAllText('{quoted}')); "
                  "$toast=[Windows.UI.Notifications.ToastNotification]::new($doc); "
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Microsoft.Windows.PowerShell').Show($toast); "
                  "Write-Output 'ASTER_NOTIFICATION_SUBMITTED'")
        result = await context.services["process_supervisor"].run(
            [executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode()],
            cwd=context.cwd, artifact_dir=context.artifact_dir, cancel=context.cancel, timeout=15, session_id=context.session_id)
        if result.exit_code == 0 and "ASTER_NOTIFICATION_SUBMITTED" in result.stdout:
            return ToolResult("Notification submitted to Windows; delivery and reading are unverified",
                              data={"submitted": True, "delivered": None, "read": None})
        return ToolResult("Notification submission did not complete; verify before retrying", True, "notification",
                          "unknown" if result.pid else "not_executed")


def build_basic_tools(supervisor):
    return [BasicTool(name) for name in ("calculate", "current_time", "system_info")] + [NotifyTool()] + [
        ProcessInspectTool(supervisor, action) for action in ("list", "ports", "kill")]
