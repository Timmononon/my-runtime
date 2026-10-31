"""Case-local execution of pinned BFCL simulators, without Python ``eval``.

The simulator implementations are trusted, vendored package resources. Corpus
files provide only data: they cannot select imports, load Python, use attribute
chains, or execute shell/file/network operations on the host.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import math
import re
import sys
from contextlib import contextmanager
from decimal import localcontext
from pathlib import Path

from jsonschema import Draft202012Validator

from aster.domain import ToolSpec
from aster.evaluation.bfcl import _schema

_PUBLIC = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")
_WIRE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_CLASSES = frozenset({"GorillaFileSystem", "MathAPI", "MessageAPI", "TwitterAPI", "TicketAPI",
                      "TradingBot", "TravelAPI", "VehicleControlAPI"})
_FORBIDDEN = frozenset({"eval", "exec", "compile", "open", "getattr", "setattr", "delattr",
                        "globals", "locals", "kill", "exit", "quit", "popen", "Popen", "run", "unlink"})


class BFCLExecutionUnsupported(ValueError):
    """A requested backend or expression cannot run in this isolated adapter."""


def _public(name):
    return isinstance(name, str) and _PUBLIC.fullmatch(name) and name not in _FORBIDDEN


def serialize_result(value):
    """Use the exact result representation used by the pinned BFCL executor."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        try:
            return json.dumps(value)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _literal(node):
    # literal_eval accepts set(), but calls anywhere in argument values are not
    # part of this adapter's data-only expression contract.
    if any(isinstance(item, (ast.Call, ast.Attribute, ast.Name, ast.Starred)) for item in ast.walk(node)
           if not isinstance(item, ast.Constant)):
        raise BFCLExecutionUnsupported("Function arguments must be literals")
    try:
        value = ast.literal_eval(node)
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError) as error:
        raise BFCLExecutionUnsupported("Function arguments must be literals") from error
    _finite(value)
    return value


def _finite(value, depth=0):
    if depth > 32:
        raise BFCLExecutionUnsupported("Function argument nesting exceeds the adapter limit")
    if isinstance(value, float) and not math.isfinite(value):
        raise BFCLExecutionUnsupported("Non-finite arguments are unsupported")
    if isinstance(value, (list, tuple, set, dict)):
        if len(value) > 10000:
            raise BFCLExecutionUnsupported("Function argument collection exceeds the adapter limit")
        for item in value:
            _finite(item, depth + 1)
            if isinstance(value, dict):
                _finite(value[item], depth + 1)


def parse_call(expression):
    """Parse a single public call with literal arguments; never evaluate code."""
    if not isinstance(expression, str) or len(expression) > 100000:
        raise BFCLExecutionUnsupported("A bounded call expression is required")
    try:
        call = ast.parse(expression, mode="eval").body
    except (SyntaxError, ValueError, MemoryError, RecursionError) as error:
        raise BFCLExecutionUnsupported("Invalid call expression") from error
    if not isinstance(call, ast.Call) or len(list(ast.walk(call))) > 20000:
        raise BFCLExecutionUnsupported("Only a single public method call is supported")
    owner = None
    if isinstance(call.func, ast.Name):
        name = call.func.id
    elif (isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
          and call.func.value.id in _CLASSES):
        owner, name = call.func.value.id, call.func.attr
    else:
        raise BFCLExecutionUnsupported("Attribute chains and dynamic call targets are prohibited")
    if not _public(name) or any(keyword.arg is None or not _public(keyword.arg) for keyword in call.keywords):
        raise BFCLExecutionUnsupported("Private, dynamic, or dangerous calls are prohibited")
    names = [keyword.arg for keyword in call.keywords]
    if len(set(names)) != len(names):
        raise BFCLExecutionUnsupported("Duplicate call keyword")
    return owner, name, [_literal(argument) for argument in call.args], {
        keyword.arg: _literal(keyword.value) for keyword in call.keywords}


@contextmanager
def _simulator_scope(instances):
    """Restore incidental upstream numeric precision and mutable constants.

    Aster's runner serializes all simulator calls; no environment or CWD is
    changed. Private RNG state remains on its own instance, as in BFCL.
    """
    modules = {sys.modules[type(instance).__module__] for instance in instances.values()}
    constants = []
    for module in modules:
        for name, value in vars(module).items():
            if name.isupper() and isinstance(value, (dict, list, set)):
                constants.append((value, copy.deepcopy(value)))
    mpmath = sys.modules.get("mpmath")
    precision = mpmath.mp.dps if mpmath is not None else None
    try:
        with localcontext():
            yield
    finally:
        for value, original in constants:
            value.clear()
            if isinstance(value, dict):
                value.update(original)
            elif isinstance(value, list):
                value.extend(original)
            else:
                value.update(original)
        if precision is not None:
            mpmath.mp.dps = precision


class SimulatorEnvironment:
    """One model or reference environment; state is never stored in globals."""

    def __init__(self, case, *, root):
        from aster.evaluation.bfcl_v4_official import (
            vendored_simulator_classes,
            vendored_simulator_method_names,
        )

        self.root = Path(root).resolve()
        self.instances, self.results = {}, []
        self.original_names, self._methods, self._owners, self.specs = {}, {}, {}, []
        classes = case.get("involved_classes")
        if (not isinstance(classes, list) or not classes or len(classes) > len(_CLASSES)
                or len(set(classes)) != len(classes) or not set(classes) <= _CLASSES):
            raise BFCLExecutionUnsupported("Only the eight pinned local simulator classes are supported")
        functions = case.get("functions", case.get("function"))
        if not isinstance(functions, list) or not functions or len(functions) > 256:
            raise BFCLExecutionUnsupported("Explicit official function documents are required")
        factories = vendored_simulator_classes(classes)
        documented_methods = vendored_simulator_method_names(classes)
        long_context = "long_context" in case.get("category", case.get("id", ""))
        initial = case.get("initial_config", {})
        if not isinstance(initial, dict):
            raise TypeError("BFCL initial_config must be an object")
        for name in classes:
            try:
                instance = factories[name]()
            except ModuleNotFoundError as error:
                raise BFCLExecutionUnsupported("An optional local simulator dependency is unavailable") from error
            self.instances[name] = instance
            if name != "MathAPI":
                with _simulator_scope(self.instances):
                    instance._load_scenario(copy.deepcopy(initial.get(name, {})), long_context=long_context)
        for function in functions:
            name = function.get("name")
            if not _public(name) or name in self._methods:
                raise BFCLExecutionUnsupported("Function names must be unique public simulator methods")
            # The upstream executor gives the last involved class precedence.
            candidates = [(class_name, getattr(instance, name, None))
                          for class_name, instance in self.instances.items()]
            matches = [(class_name, method) for class_name, method in candidates
                       if inspect.ismethod(method) and name in documented_methods[class_name]]
            if not matches:
                raise BFCLExecutionUnsupported("A documented function has no pinned simulator implementation")
            owner, method = matches[-1]
            # Wire aliases satisfy provider naming limits; the reverse map keeps
            # simulator dispatch and schema validation keyed by the documented name.
            alias = name if _WIRE.fullmatch(name) else "b_" + hashlib.sha256(name.encode()).hexdigest()[:24]
            if alias in self.original_names:
                raise BFCLExecutionUnsupported("Simulator wire alias collision")
            schema = _schema(function.get("parameters"), root=True)
            self.original_names[alias], self._methods[name], self._owners[name] = name, method, owner
            self.specs.append(ToolSpec(alias, function.get("description", ""), schema,
                                       risk="write", concurrency_safe=False, replay_safe=False,
                                       resource="bfcl_simulated_state"))
        self._schemas = {self.original_names[spec.name]: spec.parameters for spec in self.specs}

    def execute(self, name, arguments):
        original = self.original_names.get(name, name)
        if original not in self._methods or not isinstance(arguments, dict):
            raise BFCLExecutionUnsupported("Call target is outside the documented simulator allowlist")
        _finite(arguments)
        if not Draft202012Validator(self._schemas[original]).is_valid(arguments):
            raise ValueError("Call arguments do not match the offered tool schema")
        return self._invoke(original, (), arguments)

    def _invoke(self, name, positional, arguments):
        with _simulator_scope(self.instances):
            try:
                value = self._methods[name](*positional, **copy.deepcopy(arguments))
                serialized = serialize_result(value)
            except Exception as error:  # noqa: BLE001 - official simulator error representation.
                serialized = f"Error during execution: {error}"
        self.results.append(serialized)
        return serialized

    def call_expressions(self, expressions):
        if not isinstance(expressions, list) or len(expressions) > 256:
            raise BFCLExecutionUnsupported("A bounded list of call expressions is required")
        result = []
        for expression in expressions:
            owner, name, positional, arguments = parse_call(expression)
            if name not in self._methods or owner is not None and self._owners[name] != owner:
                raise BFCLExecutionUnsupported("Call expression is outside the documented simulator allowlist")
            # Gold uses Python positional arguments and optional defaults. Bind
            # with the real signature rather than changing upstream arguments.
            try:
                inspect.signature(self._methods[name]).bind(*positional, **arguments)
            except TypeError as error:
                raise BFCLExecutionUnsupported("Call arguments do not bind the documented simulator method") from error
            result.append(self._invoke(name, positional, arguments))
        return result

    def decode_call(self, expression):
        """Project literal positional gold into named structured-tool arguments."""
        owner, name, positional, arguments = parse_call(expression)
        if name not in self._methods or owner is not None and self._owners[name] != owner:
            raise BFCLExecutionUnsupported("Call expression is outside the documented simulator allowlist")
        try:
            signature = inspect.signature(self._methods[name])
            bound = signature.bind(*positional, **arguments)
        except TypeError as error:
            raise BFCLExecutionUnsupported("Reference arguments do not bind the pinned simulator method") from error
        if any(signature.parameters[key].kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
               for key in bound.arguments):
            raise BFCLExecutionUnsupported("Variadic reference arguments require an explicit wire adapter")
        result = dict(bound.arguments)
        json.dumps(result, allow_nan=False)
        return {"name": name, "arguments": copy.deepcopy(result)}

    def admission_issues(self, expression):
        """Inspect a reference call without relaxing its declared wire schema."""
        call = self.decode_call(expression)
        name, arguments = call["name"], call["arguments"]
        failures = list(Draft202012Validator(self._schemas[name]).iter_errors(arguments))
        return [{"function": name, "method_signature": str(inspect.signature(self._methods[name])),
                 "validator": error.validator, "parameter_path": list(error.path),
                 "schema_path": list(error.schema_path),
                 "received_type": type(error.instance).__name__,
                 "declared_schema_sha256": hashlib.sha256(json.dumps(
                     self._schemas[name], sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()}
                for error in failures]

    def close(self):
        """No live processes, network handles, or host working files are owned."""


def create_environment(case, *, root, variant=None, fixtures=None):
    if variant is not None or fixtures is not None:
        raise BFCLExecutionUnsupported("Agentic variants require their explicit offline environment factory")
    return SimulatorEnvironment(case, root=root)
