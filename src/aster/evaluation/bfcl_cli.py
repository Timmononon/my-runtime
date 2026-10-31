"""Separate BFCL entry point; importing it never opens a Runtime or provider."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path


def add_parsers(commands):
    parser = commands.add_parser("bfcl", help="Inspect BFCL inputs or run an explicitly budgeted call-selection evaluation")
    actions = parser.add_subparsers(dest="bfcl_action", required=True)
    from aster.evaluation.bfcl_v4_cli import add_parser as add_v4_parser

    add_v4_parser(actions)
    prepare = actions.add_parser("prepare", help="Freeze a pinned 100-case suite locally; no Runtime or model requests")
    prepare.add_argument("--source-dir", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--seed", type=int, default=20261029)
    key = actions.add_parser("configure-key", help="Hidden input into an explicit test-only credential directory; no model request")
    key.add_argument("--credential-data-dir", type=Path, required=True)
    for action in ("inspect", "run"):
        sub = actions.add_parser(action)
        sub.add_argument("--data", type=Path, required=True, help="Local official BFCL JSONL question file")
        sub.add_argument("--answers", type=Path, required=True, help="Local matching possible_answer JSONL file")
        sub.add_argument("--category")
        sub.add_argument("--case", action="append", dest="case_ids")
        sub.add_argument("--output", type=Path, required=action == "run")
        if action == "run":
            sub.add_argument("--live", action="store_true", help="Authorize generation attempts within --max-requests")
            sub.add_argument("--max-requests", type=int)
            sub.add_argument("--max-output-tokens", type=int, default=1024)
            sub.add_argument("--credential-data-dir", type=Path, help="Explicit test credential directory; never the implicit user Runtime")
            sub.add_argument("--model")
            sub.add_argument("--reasoning-effort")


def inspect_inputs(data_path, answers_path, *, category=None, case_ids=None):
    from aster.evaluation.bfcl import load_cases

    cases = load_cases(data_path, answers_path, category=category)
    if case_ids is not None:
        selected = set(case_ids)
        if len(selected) != len(case_ids) or not selected or not selected <= {case["id"] for case in cases}:
            raise ValueError("Unknown, duplicate or empty BFCL case selection")
        cases = [case for case in cases if case["id"] in selected]
    return {
        "schema_version": 1,
        "evaluation_scope": "bfcl_single_turn_structured_calls",
        "official_score": False,
        "model_quality": "unsupported",
        "paid_calls_authorized": False,
        "data_sha256": hashlib.sha256(Path(data_path).read_bytes()).hexdigest(),
        "answers_sha256": hashlib.sha256(Path(answers_path).read_bytes()).hexdigest(),
        "case_count": len(cases),
        "categories": dict(Counter(case["category"] for case in cases)),
        "case_ids": [case["id"] for case in cases],
        "note": "Input validation only; no model capability score or official leaderboard result.",
    }


async def handle_command(args):
    if args.command != "bfcl":
        return None
    if args.bfcl_action == "v4":
        from aster.evaluation.bfcl_v4_cli import handle_command as handle_v4

        return await handle_v4(args)
    try:
        if args.bfcl_action == "prepare":
            from aster.evaluation.bfcl_suite import prepare_suite

            manifest = prepare_suite(args.source_dir, args.output_dir, seed=args.seed)
            print(json.dumps({"counts": manifest["counts"], "model_requests": 0,
                              "official_score": False, "output": str(args.output_dir)}, ensure_ascii=False, indent=2))
            return 0
        if args.bfcl_action == "configure-key":
            import getpass

            from aster.customization.credentials import CredentialStore
            from aster.paths import resolve_data_dir

            target = args.credential_data_dir.expanduser().resolve()
            if target in {resolve_data_dir(), (Path.home() / ".aster").resolve()} or (target / "runtime.db").exists():
                raise ValueError("Use a dedicated test credential directory without a Runtime database")
            secret = getpass.getpass("BFCL test DeepSeek API key (hidden): ").strip()
            if not secret or any(char.isspace() for char in secret):
                raise ValueError("A nonempty single-line test key is required")
            CredentialStore(data_dir=target).set("deepseek", secret)
            print(json.dumps({"configured": True, "model_requests": 0, "credential_slot": "deepseek"}))
            return 0
        if args.output is not None:
            target = args.output.resolve()
            if target in {args.data.resolve(), args.answers.resolve()}:
                raise ValueError("BFCL output must not overwrite input data")
            if target.name.casefold() in {"credentials.yaml", "config.yaml", "runtime.db"}:
                raise ValueError("BFCL output must not replace Runtime configuration")
        if args.bfcl_action == "inspect":
            result = inspect_inputs(args.data, args.answers, category=args.category, case_ids=args.case_ids)
            if args.output is not None:
                from aster.platform.filesystem import atomic_write

                atomic_write(args.output, json.dumps(result, ensure_ascii=False, indent=2).encode())
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.live and (not args.max_requests or args.max_requests < 1 or args.credential_data_dir is None):
            print("aster bfcl: --live requires a positive --max-requests and explicit --credential-data-dir", file=sys.stderr)
            return 2
        from aster.evaluation.bfcl_runner import run_bfcl

        report = await run_bfcl(
            args.data, args.answers, args.output,
            mode="live" if args.live else "offline", max_requests=args.max_requests,
            max_output_tokens=args.max_output_tokens, case_ids=args.case_ids, category=args.category,
            credential_data_dir=args.credential_data_dir, model=args.model, reasoning_effort=args.reasoning_effort,
        )
        summary = {key: report[key] for key in ("mode", "totals", "official_score") if key in report}
        summary["output"] = str(args.output)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        incomplete = sum(report.get("totals", {}).get(key, 0) for key in ("fail", "not_run", "case_error"))
        return 2 if incomplete else 0
    except (ValueError, TypeError, OSError, RuntimeError) as error:
        # External files and provider errors may contain source text or secrets.
        print(f"aster bfcl: operation rejected ({type(error).__name__}); check input schemas and explicit test options", file=sys.stderr)
        return 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m aster.evaluation.bfcl_cli")
    add_parsers(parser.add_subparsers(dest="command", required=True))
    return asyncio.run(handle_command(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
