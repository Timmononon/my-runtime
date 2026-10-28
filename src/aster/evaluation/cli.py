import argparse
import asyncio
import json
from pathlib import Path

from aster.evaluation.runner import run_evaluation


def add_parsers(subparsers):
    parser = subparsers.add_parser("evaluate", help="Run fixture protocol checks or explicitly budgeted live evaluations")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--live", action="store_true", help="Explicitly opt into paid model calls; requires --max-requests")
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--max-output-tokens", type=int, default=1024)


async def handle_command(args, coordinator=None):
    if args.command != "evaluate":
        return None
    report = await run_evaluation(args.output, mode="live" if args.live else "offline", case_ids=args.case_ids,
                                  max_requests=args.max_requests, max_output_tokens=args.max_output_tokens)
    print(json.dumps({"output": str(args.output), "mode": report["mode"], "totals": report["totals"]}, indent=2))
    return 2 if report["totals"].get("fail") else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    add_parsers(parser.add_subparsers(dest="command", required=True))
    raise SystemExit(asyncio.run(handle_command(parser.parse_args())))
