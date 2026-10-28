"""CLI parsing helpers; the main CLI owns coordinator/daemon lifecycle."""
import json

from aster.api.customization import scoped_memory
from aster.domain import to_dict
from aster.packages import SkillPackageManager


def add_parsers(subparsers):
    skills = subparsers.add_parser("skills", help="Inspect, preview and install pinned skill packages")
    actions = skills.add_subparsers(dest="skill_action", required=True)
    actions.add_parser("list")
    preview = actions.add_parser("preview")
    preview.add_argument("source", help="bundled:name, local directory, or local UTF-8 workflow ZIP")
    for action, identifier in (("install", "plan_id"), ("uninstall", "name")):
        parser = actions.add_parser(action)
        parser.add_argument(identifier)
        parser.add_argument("--digest", required=True, help="Exact digest inspected in preview/list")
        parser.add_argument("--session", required=True)
    memory = subparsers.add_parser("memory", help="Inspect or explicitly revise source-linked facts")
    memory.add_argument("memory_action", choices=["recall", "record", "update", "forget", "history"])
    memory.add_argument("--session", required=True)
    memory.add_argument("--arguments", default="{}", help="JSON argument object; never put credentials here")


async def handle_command(args, coordinator):
    if args.command == "skills":
        manager = SkillPackageManager(coordinator.data_dir)
        if args.skill_action == "list":
            value = {"catalog": manager.catalog(), "installed": manager.installed()}
        elif args.skill_action == "preview":
            value = manager.preview(args.source)
        else:
            arguments = {"expected_digest": args.digest}
            arguments["plan_id" if args.skill_action == "install" else "name"] = getattr(args, "plan_id" if args.skill_action == "install" else "name")
            value = to_dict(await coordinator.execute_session_tool(args.session, "skill_package_" + args.skill_action,
                arguments, unattended=True, approved=True))
    elif args.command == "memory":
        arguments = json.loads(args.arguments)
        if not isinstance(arguments, dict):
            raise ValueError("Memory arguments must be a JSON object")
        if args.memory_action in {"recall", "history"}:
            value = getattr(scoped_memory(coordinator, args.session), args.memory_action)(**arguments)
        else:
            value = to_dict(await coordinator.execute_session_tool(args.session, "memory_" + args.memory_action,
                arguments, unattended=True, approved=True))
    else:
        return None
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 2 if isinstance(value, dict) and value.get("is_error") else 0
