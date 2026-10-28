import json

from aster.domain import ToolResult, ToolSpec
from aster.platform.filesystem import scoped_path


class PackageTool:
    def __init__(self, action, properties, required, *, write=False):
        self.action = action
        self.spec = ToolSpec("skill_package_" + action, "Manage a digest-pinned local skill package: " + action,
            {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            risk="write" if write else "read", replay_safe=not write, resource="skill-packages")

    async def execute(self, arguments, context):
        if context.cancel.is_set():
            return ToolResult("Cancelled", True, "cancelled", "not_executed")
        mutation_started = False
        try:
            manager = context.services["skill_packages"]
            args = dict(arguments)
            if self.action == "preview" and not args["source"].startswith("bundled:"):
                args["source"] = str(scoped_path(args["source"], context, must_exist=True))
            if self.action == "install":
                source = manager.plan_source(args["plan_id"])
                if not source.startswith("bundled:"):
                    scoped_path(source, context, must_exist=True)
            mutation_started = self.action != "preview"
            result = getattr(manager, self.action)(**args)
            return ToolResult(json.dumps(result, ensure_ascii=False), data={"package": result})
        except PermissionError as error:
            return ToolResult(str(error), True, "permission", "not_executed")
        except (ValueError, KeyError, TypeError) as error:
            return ToolResult(str(error), True, "validation", "not_executed")
        except OSError as error:
            # A filesystem failure can occur after the final directory swap.
            # The receipt/recovery journal permits inspection, never blind replay.
            return ToolResult(str(error), True, "io", "unknown" if mutation_started else "not_executed")


def build_package_tools():
    text = {"type": "string", "minLength": 1}
    return [PackageTool("preview", {"source": text}, ["source"]),
            PackageTool("install", {"plan_id": text, "expected_digest": text}, ["plan_id", "expected_digest"], write=True),
            PackageTool("uninstall", {"name": text, "expected_digest": text}, ["name", "expected_digest"], write=True)]
