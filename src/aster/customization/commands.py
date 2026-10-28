from __future__ import annotations

import string
from pathlib import Path

from aster.config import read_yaml, safe_identifier


class CommandRegistry:
    """Named prompt templates. Expansion cannot execute shell expressions."""

    def __init__(self, commands: dict[str, str]):
        self.commands = {}
        for name, template in commands.items():
            if not isinstance(template, str):
                raise TypeError("Custom command templates must be strings")
            if not string.Template(template).is_valid():
                raise ValueError(f"Invalid placeholders in custom command: {name}")
            self.commands[safe_identifier(name)] = template

    @classmethod
    def from_yaml(cls, path: Path):
        return cls(read_yaml(path))

    def expand(self, name: str, arguments: dict[str, str]) -> str:
        if name not in self.commands:
            raise ValueError("Unknown custom command")
        template = string.Template(self.commands[name])
        required = set(template.get_identifiers())
        if required - arguments.keys():
            raise ValueError("Missing command arguments: " + ", ".join(sorted(required - arguments.keys())))
        if arguments.keys() - required:
            raise ValueError("Unknown command arguments: " + ", ".join(sorted(arguments.keys() - required)))
        return template.substitute({key: str(value) for key, value in arguments.items()})
