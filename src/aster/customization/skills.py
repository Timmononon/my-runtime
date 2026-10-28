from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from aster.config import safe_identifier
from aster.domain import ToolContext, ToolResult, ToolSpec
from aster.packages.manager import tree_digest


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    content: str
    version: str
    allowed_tools: frozenset[str] | None
    secrets: tuple[tuple[str, str], ...] = ()
    files: tuple[tuple[str, bytes], ...] = ()

    def resolve(self, reference: str) -> Path:
        path = (self.path.parent / reference).resolve(strict=True)
        if not path.is_relative_to(self.path.parent.resolve()) or not path.is_file():
            raise ValueError("Skill references must remain inside the skill directory")
        return path

    def read_reference(self, reference: str, max_bytes: int = 256 * 1024) -> str:
        if self.files:
            candidate = (self.path.parent / reference).resolve()
            if not candidate.is_relative_to(self.path.parent.resolve()):
                raise ValueError("Skill reference escapes its directory")
            name = candidate.relative_to(self.path.parent.resolve()).as_posix()
            content = dict(self.files).get(name)
            if content is None:
                raise ValueError("Reference is absent from this skill's source snapshot")
            if len(content) > max_bytes:
                raise ValueError("Skill reference exceeds read budget")
            return content.decode("utf-8-sig")
        path = self.resolve(reference)
        if path.stat().st_size > max_bytes:
            raise ValueError("Skill reference exceeds read budget")
        return path.read_text(encoding="utf-8-sig")


class SkillRegistry:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._skills: dict[str, Skill] = {}

    def reload(self) -> list[Skill]:
        found: dict[str, Skill] = {}
        if self.root.exists():
            for path in sorted(self.root.glob("*/SKILL.md")):
                if not path.resolve().is_relative_to(self.root):
                    raise ValueError("Skill path escapes root")
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError("Skill exceeds maximum source size")
                source = path.read_text(encoding="utf-8-sig")
                match = re.match(r"\A---\s*\r?\n(.*?)\r?\n---(?:\r?\n|$)(.*)\Z", source, re.DOTALL)
                metadata, body = ({}, source)
                if match:
                    metadata, body = yaml.safe_load(match[1]) or {}, match[2]
                if not isinstance(metadata, dict):
                    raise TypeError("Skill frontmatter must be a mapping")
                name = safe_identifier(str(metadata.get("name", path.parent.name)))
                if name in found:
                    raise ValueError(f"Duplicate skill name: {name}")
                allowed = metadata.get("allowed-tools")
                if isinstance(allowed, str):
                    allowed = re.split(r"[\s,]+", allowed.strip()) if allowed.strip() else []
                if allowed is not None and (
                    not isinstance(allowed, list) or any(not isinstance(x, str) for x in allowed)
                ):
                    raise ValueError("allowed-tools must be a list or whitespace-separated names")
                secrets = metadata.get("secrets", {})
                if not isinstance(secrets, dict) or any(
                    not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k)) or not isinstance(v, str)
                    for k, v in secrets.items()
                ):
                    raise ValueError("secrets must map environment names to credential references")
                if any(not reference.startswith(("env:", "keyring:")) for reference in secrets.values()):
                    raise ValueError("Skill secrets must use env:/keyring: references")
                files = {}
                for entry in sorted(path.parent.rglob("*")):
                    if "__pycache__" in entry.relative_to(path.parent).parts:
                        continue
                    if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
                        raise ValueError("Skill source snapshots cannot include links")
                    if entry.is_file() and entry.name != ".aster-receipt.json":
                        if entry.stat().st_size > 1024 * 1024:
                            raise ValueError("Skill reference exceeds snapshot file budget")
                        files[entry.relative_to(path.parent).as_posix()] = entry.read_bytes()
                        if len(files) > 250 or sum(map(len, files.values())) > 10 * 1024 * 1024:
                            raise ValueError("Skill source snapshot exceeds bundle budget")
                # Store references only. Actual values never become part of the Skill object.
                found[name] = Skill(
                    name,
                    str(metadata.get("description", "")),
                    path.resolve(),
                    body,
                    tree_digest(files),
                    None if allowed is None else frozenset(allowed),
                    tuple(secrets.items()),
                    tuple(files.items()),
                )
        self._skills = found
        return list(found.values())

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise ValueError(f"Unknown skill: {name}") from exc

    def catalog(self) -> list[dict]:
        return [
            {"name": skill.name, "description": skill.description, "version": skill.version}
            for skill in self._skills.values()
        ]


class CredentialResolver:
    """Explicit references resolved from the selected local home only when used."""

    def __call__(self, reference: str) -> str:
        from aster.customization.credentials import CredentialStore
        return CredentialStore().resolve(reference)


class SkillScope:
    def __init__(
        self,
        registry: SkillRegistry,
        *,
        bound_skills: list[str] | None = None,
        secret_resolver: Callable[[str], str] | None = None,
    ):
        self.registry = registry
        self.bound_skills = frozenset(bound_skills) if bound_skills is not None else None
        self.secret_resolver = secret_resolver or CredentialResolver()
        self._active: dict[str, Skill] = {}

    def activate(self, name: str) -> Skill:
        if self.bound_skills is not None and name not in self.bound_skills:
            raise ValueError("Skill is not bound to this Agent")
        if name in self._active:
            return self._active[name]
        skill = self.registry.get(name)
        # Snapshot source/version: reloads must not silently change an active run.
        self._active[name] = skill
        return skill

    def check_tool(self, name: str) -> bool:
        # Pure discovery and skill loading remain available; never exempt side-effect tools.
        if name in {"use_skill", "tool_search"}:
            return True
        return all(
            skill.allowed_tools is None or name in skill.allowed_tools for skill in self._active.values()
        )

    def environment(self, base_env: dict[str, str], *, skill_name: str | None = None) -> dict[str, str]:
        env = dict(base_env)
        if skill_name is None:
            return env
        if skill_name not in self._active:
            raise ValueError("Secrets are available only inside an activated skill invocation")
        for variable, reference in self._active[skill_name].secrets:
            env[variable] = self.secret_resolver(reference)
        return env

    def versions(self) -> dict[str, str]:
        return {name: skill.version for name, skill in self._active.items()}


class UseSkillTool:
    spec = ToolSpec(
        "use_skill",
        "Load a local skill into this run and enforce its tool restrictions",
        {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1}, "reference": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        risk="read",
        replay_safe=True,
    )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        if context.cancel.is_set():
            return ToolResult("Cancelled", is_error=True, error_category="cancelled")
        try:
            scope: SkillScope = context.services["skill_scope"]
            skill = scope.activate(arguments["name"])
            text = (
                skill.read_reference(arguments["reference"]) if arguments.get("reference") else skill.content
            )
            return ToolResult(
                text,
                data={
                    "skill": skill.name,
                    "version": skill.version,
                    "skill_directory": str(skill.path.parent),
                    "allowed_tools": sorted(skill.allowed_tools) if skill.allowed_tools is not None else None,
                },
            )
        except (KeyError, ValueError, OSError) as exc:
            return ToolResult(str(exc), is_error=True, error_category="validation")
