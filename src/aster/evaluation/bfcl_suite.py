"""Freeze a local BFCL subset without creating a Runtime or requesting models."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from aster.evaluation.bfcl_dataset import CATEGORIES, SOURCE_HASHES, load_pinned_pool

DEFAULT_SEED = 20261029
PILOT_IDS = frozenset(f"simple_python_{number}" for number in (92, 115, 145, 178, 226, 235, 273, 327, 350, 369))
LICENSE_SHA256 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
_ALGORITHM = "sha256_group_category_matching_v1"
_GROUP_POLICY = "normalized_prompt_or_any_shared_function_signature_transitive_v1"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _normalized(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def _signature_schema(value):
    if isinstance(value, dict):
        return {key: ({name: _signature_schema(schema) for name, schema in item.items()}
                      if key in {"properties", "patternProperties", "$defs", "definitions"} and isinstance(item, dict)
                      else _signature_schema(item))
                for key, item in value.items() if key not in {"description", "title", "examples"}}
    if isinstance(value, list):
        return [_signature_schema(item) for item in value]
    return value


def source_groups(cases):
    """Conservatively join exact normalized prompts or any shared signature.

    This identifies mechanical overlaps, not all semantically related tasks.
    Function descriptions are ignored; function names and parameter schemas
    are retained. Schema required-name ordering is canonicalized separately.
    """
    parents = {case_id: case_id for case_id in cases}

    def find(case_id):
        while parents[case_id] != case_id:
            parents[case_id] = parents[parents[case_id]]
            case_id = parents[case_id]
        return case_id

    def join(left, right):
        left, right = find(left), find(right)
        if left != right:
            parents[max(left, right)] = min(left, right)

    seen = {}
    for case_id in sorted(cases):
        case = cases[case_id]
        tokens = ["prompt:" + _digest(_normalized(case["prompt"]))]
        for function in case["functions"]:
            schema = _signature_schema(function["parameters"])
            _sort_required(schema)
            tokens.append("function:" + _digest({"name": function["name"], "parameters": schema}))
        for token in tokens:
            if token in seen:
                join(case_id, seen[token])
            else:
                seen[token] = case_id
    grouped = defaultdict(list)
    for case_id in sorted(cases):
        grouped[find(case_id)].append(case_id)
    groups = {"group_" + _digest(members): members for members in grouped.values()}
    by_id = {case_id: group_id for group_id, members in groups.items() for case_id in members}
    return groups, by_id


def _sort_required(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "required" and isinstance(item, list) and all(isinstance(name, str) for name in item):
                item.sort()
            else:
                _sort_required(item)
    elif isinstance(value, list):
        for item in value:
            _sort_required(item)


def _rank(seed, phase, value):
    return _digest([_ALGORITHM, seed, phase, value]), value


def _select(cases, groups, by_id, seed):
    exposed_groups = {by_id[case_id] for case_id in PILOT_IDS if case_id in by_id}
    excluded = [
        {"id": case_id, "reason": "previous_pilot_exposure" if case_id in PILOT_IDS else "same_source_group_as_previous_pilot",
         "source_group": by_id[case_id]}
        for case_id in sorted(cases) if by_id[case_id] in exposed_groups
    ]
    eligible = {case_id: case for case_id, case in cases.items() if by_id[case_id] not in exposed_groups}
    choices = {}
    for category in CATEGORIES:
        candidates = {by_id[case_id] for case_id, case in eligible.items() if case["category"] == category}
        choices[category] = sorted(candidates, key=lambda group: _rank(seed, "group:" + category, group))
    # A group may offer records in several categories. Augmenting paths avoid
    # greedy category ordering consuming groups required by another quota.
    assigned = {}

    def assign(slot, visited):
        category, _index = slot
        for group in choices[category]:
            if group in visited:
                continue
            visited.add(group)
            previous = assigned.get(group)
            if previous is None or assign(previous, visited):
                assigned[group] = slot
                return True
        return False

    for category in CATEGORIES:
        for index in range(25):
            if not assign((category, index), set()):
                raise ValueError("Insufficient independent BFCL source groups for fixed category quotas")
    selected = []
    for category in CATEGORIES:
        category_groups = [group for group, slot in assigned.items() if slot[0] == category]
        category_groups.sort(key=lambda group: _rank(seed, "split:" + category, group))
        for index, group in enumerate(category_groups):
            members = [case_id for case_id in groups[group]
                       if case_id in eligible and cases[case_id]["category"] == category]
            case_id = min(members, key=lambda item: _rank(seed, "representative", item))
            selected.append({"id": case_id, "category": category, "source_group": group,
                             "split": "dev" if index < 10 else "holdout"})
    return selected, excluded, eligible


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _summary(manifest):
    lines = ["# BFCL 100 题冻结清单", "", "只做离线数据准备；模型 API 调用 0 次，尚无能力评分。", "",
             "| 类别 | 开发集 | 留出集 | 合计 |", "| --- | ---: | ---: | ---: |"]
    lines.extend(f"| {category} | 10 | 15 | 25 |" for category in CATEGORIES)
    lines.extend(["| 合计 | 40 | 60 | 100 |", "", f"抽样 seed：`{manifest['sampling']['seed']}`。",
                  "原始上游文件只读；两条 schema／参考答案冲突题由当前适配层显式隔离，不修改题目或答案。",
                  "先前已调用模型的 10 题及其机械来源组全部排除；选中 100 题来自 100 个不同来源组。",
                  "分组按规范化问题文本或任意共享函数签名传递合并，不能保证识别所有语义近似任务。",
                  "", "问题与评分答案分别保存在 JSONL 文件中。答案仅供评测器使用，不能放入 Agent 的提示或工作区。",
                  "当前范围为 BFCL V4 单轮固定子集、本地派生评分；不是官方全量成绩。", ""])
    return "\n".join(lines)


def prepare_suite(source_dir, output_dir, *, seed=DEFAULT_SEED):
    """Create a new, deterministic 100-case suite; never overwrite a suite."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("BFCL sampling seed must be an integer")
    source = Path(source_dir).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("BFCL suite output must be separate from original source directory")
    if output.exists():
        raise ValueError("BFCL suite output already exists; frozen suites cannot be overwritten")
    license_content = (source / "LICENSE-GORILLA.txt").read_bytes()
    if hashlib.sha256(license_content).hexdigest() != LICENSE_SHA256:
        raise ValueError("BFCL upstream license hash does not match pinned source")
    pool = load_pinned_pool(source)
    groups, by_id = source_groups(pool["cases"])
    selected, exposure_exclusions, eligible = _select(pool["cases"], groups, by_id, seed)
    for entry in selected:
        entry["question_sha256"] = _digest(pool["questions"][entry["id"]])
        entry["answer_sha256"] = _digest(pool["answers"][entry["id"]])
    manifest = {
        "schema_version": 1, "evaluation_scope": "bfcl_v4_single_turn_fixed_subset",
        "official_score": False, "model_requests": 0, "paid_calls_authorized": False,
        "upstream_revision": pool["revision"], "sources": pool["sources"],
        "upstream_repository": "https://github.com/ShishirPatil/gorilla", "license": "Apache-2.0",
        "sampling": {"algorithm": _ALGORITHM, "seed": seed, "per_category": 25,
                     "dev_per_category": 10, "holdout_per_category": 15,
                     "selection_inputs": "question and function metadata only; no answer or model score selection"},
        "grouping": {"policy": _GROUP_POLICY, "scope": "full strictly compatible upstream pool before exclusions",
                     "signature": "function name plus recursively description/title/examples-stripped parameter schema; required lists sorted",
                     "limit": "mechanical grouping only; does not certify semantic independence or model training non-exposure",
                     "pool_groups": len(groups), "selected_groups": len(selected)},
        "counts": {"compatible_pool": len(pool["cases"]), "eligible_pool": len(eligible), "all": 100,
                   "dev": 40, "holdout": 60,
                   "categories": dict(Counter(entry["category"] for entry in selected))},
        "quarantined": pool["quarantined"], "pilot_exposure_exclusions": exposure_exclusions,
        "pilot_case_ids": sorted(PILOT_IDS), "cases": selected,
        "selected_groups": {entry["source_group"]: groups[entry["source_group"]] for entry in selected},
        "files": {},
        "answer_policy": "Evaluator-only answer files must never be copied to agent workspaces or prompts.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bfcl-prepare-", dir=output.parent) as temporary:
        stage = Path(temporary) / "suite"
        stage.mkdir()
        for split in ("all", "dev", "holdout"):
            entries = [entry for entry in selected if split == "all" or entry["split"] == split]
            for kind, rows in (("questions", pool["questions"]), ("answers", pool["answers"])):
                name = f"BFCL_v4_{split}{'.answers' if kind == 'answers' else ''}.jsonl"
                content = "".join(_json(rows[entry["id"]]) + "\n" for entry in entries).encode("utf-8")
                (stage / name).write_bytes(content)
                manifest["files"][name] = {"sha256": hashlib.sha256(content).hexdigest(),
                                            "records": len(entries), "kind": kind, "split": split}
        summary_content = _summary(manifest).encode("utf-8")
        (stage / "selection-summary.md").write_bytes(summary_content)
        manifest["files"]["selection-summary.md"] = {"sha256": hashlib.sha256(summary_content).hexdigest(),
                                                       "kind": "documentation"}
        (stage / "LICENSE-GORILLA.txt").write_bytes(license_content)
        manifest["files"]["LICENSE-GORILLA.txt"] = {"sha256": LICENSE_SHA256, "kind": "license"}
        _write_json(stage / "manifest.json", manifest)
        # Recheck originals immediately before publication, without trusting
        # mutable provenance metadata or ever rewriting the source.
        for name, expected in SOURCE_HASHES.items():
            if hashlib.sha256((source / name).read_bytes()).hexdigest() != expected:
                raise ValueError("BFCL original source changed while preparing suite")
        if (source / "LICENSE-GORILLA.txt").read_bytes() != license_content:
            raise ValueError("BFCL upstream license changed while preparing suite")
        if output.exists():
            raise ValueError("BFCL suite output appeared during preparation")
        os.rename(stage, output)
    return manifest
