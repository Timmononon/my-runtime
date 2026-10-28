"""Durable local triggers. Every run goes through the shared Coordinator.

All tables live in the core Store's transaction/connection. No model invocation,
tool dispatch, or permission decision occurs in this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(path: Path) -> str | None:
    try:
        stat = path.stat()
        if not path.is_file():
            return None
        return f"{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return None


def _next_cron(expression: str, timezone: str, after: float) -> float:
    """Interpret cron as local wall time: skip gaps, run folds once (earliest).

    Iterating naive wall times avoids croniter's implicit gap shifting and
    duplicate fall-back occurrences. UTC timestamps remain trigger identities.
    """
    zone = ZoneInfo(timezone)
    wall = datetime.fromtimestamp(after, zone).replace(tzinfo=None)
    iterator = croniter(expression, wall)
    while True:
        candidate = iterator.get_next(datetime)
        instant = candidate.replace(tzinfo=zone, fold=0).timestamp()
        roundtrip = datetime.fromtimestamp(instant, zone).replace(tzinfo=None)
        if roundtrip == candidate and instant > after:
            return instant


class AutomationService:
    def __init__(
        self,
        store,
        coordinator,
        *,
        clock=time.time,
        poll_seconds: float = 1,
        max_due_per_tick: int = 50,
        ignored_roots: list[Path] = (),
        event_consumer=None,
    ):
        if poll_seconds <= 0 or max_due_per_tick < 1:
            raise ValueError("Automation polling budgets must be positive")
        self.store, self.coordinator, self.clock = store, coordinator, clock
        self.poll_seconds, self.max_due_per_tick = poll_seconds, max_due_per_tick
        self.ignored_roots = tuple(Path(path).resolve() for path in ignored_roots)
        self.event_consumer = event_consumer
        self._task = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        with store.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS automation_meta(version INTEGER NOT NULL);
                INSERT INTO automation_meta(version) SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM automation_meta);
                CREATE TABLE IF NOT EXISTS automation_jobs(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, session_id TEXT NOT NULL,
                    prompt TEXT NOT NULL, config_json TEXT NOT NULL, enabled INTEGER NOT NULL,
                    next_fire REAL, last_fire REAL, observation_hash TEXT, result_hash TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id));
                CREATE TABLE IF NOT EXISTS automation_triggers(
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, trigger_key TEXT NOT NULL,
                    scheduled_at REAL NOT NULL, state TEXT NOT NULL, run_id TEXT,
                    prompt TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    last_status TEXT, error TEXT, created_at REAL NOT NULL, UNIQUE(job_id,trigger_key),
                    FOREIGN KEY(job_id) REFERENCES automation_jobs(id));
                CREATE TABLE IF NOT EXISTS automation_watch_state(
                    job_id TEXT NOT NULL, path TEXT NOT NULL, observed TEXT, delivered TEXT,
                    changed_at REAL NOT NULL, PRIMARY KEY(job_id,path),
                    FOREIGN KEY(job_id) REFERENCES automation_jobs(id));
                CREATE INDEX IF NOT EXISTS automation_trigger_state ON automation_triggers(state,scheduled_at);
                CREATE TABLE IF NOT EXISTS automation_consumers(name TEXT PRIMARY KEY,last_event_id INTEGER NOT NULL);
            """)
            version = db.execute("SELECT version FROM automation_meta").fetchone()[0]
            if version not in {1, 2, 3}:
                raise ValueError("Unsupported automation schema version")
            columns = {row[1] for row in db.execute("PRAGMA table_info(automation_triggers)")}
            if "created_at" not in columns:
                db.execute("ALTER TABLE automation_triggers ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
                db.execute("UPDATE automation_triggers SET created_at=scheduled_at WHERE created_at=0")
            if "execution_json" not in columns:
                db.execute("ALTER TABLE automation_triggers ADD COLUMN execution_json TEXT")
            db.execute("UPDATE automation_meta SET version=3")
            for row in db.execute("SELECT id,session_id,config_json FROM automation_jobs").fetchall():
                config = json.loads(row["config_json"])
                changed = False
                if "session_mode" not in config:
                    config["session_mode"] = "source"
                    changed = True
                if "selection_mode" not in config:
                    config = self._selection_config(row["session_id"], config, validate=False)
                    changed = True
                if changed:
                    db.execute("UPDATE automation_jobs SET config_json=? WHERE id=?", (_json(config), row["id"]))

    def _selection_config(self, session_id, config, *, validate=True):
        """Job choices are public metadata; actual identities belong to triggers."""
        result = dict(config)
        mode = result.setdefault("selection_mode", "fixed")
        if mode not in {"fixed", "agent_default"}:
            raise ValueError("selection_mode must be fixed or agent_default")
        if mode == "agent_default":
            result.pop("selection", None)
            return result
        selection = result.get("selection")
        if selection is None:
            reader = getattr(self.coordinator, "session_selection", None)
            selection = reader(session_id) if reader else {
                "provider_id": "deepseek", "model": "deepseek-flash", "reasoning_effort": "high", "selected_skill": None}
        if not isinstance(selection, dict):
            raise ValueError("Fixed automation selection must be a mapping")  # noqa: TRY004 - API validation maps to 422.
        fields = {"provider_id", "model", "reasoning_effort", "selected_skill"}
        if set(selection) - fields or not {"provider_id", "model"} <= set(selection):
            raise ValueError("Fixed selection accepts only provider_id, model, reasoning_effort and selected_skill")
        selection = {"reasoning_effort": None, "selected_skill": None, **selection}
        if any(not isinstance(selection[key], str) or not selection[key].strip() for key in ("provider_id", "model")):
            raise ValueError("A fixed automation requires a provider and model")
        if any(selection[key] is not None and not isinstance(selection[key], str)
               for key in ("reasoning_effort", "selected_skill")):
            raise ValueError("Automation effort and Skill must be strings or null")
        result["selection"] = selection
        capture = getattr(self.coordinator, "capture_automation_execution", None)
        if validate and capture:
            capture(session_id, selection)
        return result

    def _capture_execution(self, session_id, config):
        capture = getattr(self.coordinator, "capture_automation_execution", None)
        if capture is None:
            return None  # Scheduling-only adapters need no model configuration.
        execution = capture(session_id, config.get("selection") if config["selection_mode"] == "fixed" else None)
        execution["selection_mode"] = config["selection_mode"]
        execution["session_mode"] = config["session_mode"]
        return execution

    def _validate(self, kind: str, config: dict) -> dict:
        result = dict(config)
        result.setdefault("session_mode", "fresh")
        if result["session_mode"] not in {"fresh", "sticky", "source"}:
            raise ValueError("session_mode must be fresh, sticky, or source (legacy)")
        if kind not in {"schedule", "watcher", "heartbeat"}:
            raise ValueError("Unknown automation kind")
        result.setdefault("timezone", "Asia/Shanghai")
        ZoneInfo(result["timezone"])
        result.setdefault("overlap", "skip")
        result.setdefault("max_runs_per_day", 24)
        result.setdefault("max_pending", 1)
        result.setdefault("misfire", "coalesce")
        result.setdefault("catchup_limit", 3)
        result.setdefault("max_delivery_attempts", 3)
        result.setdefault("grace_seconds", 60)
        if result["overlap"] not in {"skip", "queue"} or result["misfire"] not in {
            "skip",
            "coalesce",
            "bounded_catchup",
        }:
            raise ValueError("Invalid overlap or missed-run policy")
        for field in ("max_runs_per_day", "max_pending", "catchup_limit", "max_delivery_attempts"):
            if not isinstance(result[field], int) or not 1 <= result[field] <= 10000:
                raise ValueError(f"Invalid {field}")
        if not isinstance(result["grace_seconds"], (int, float)) or result["grace_seconds"] < 0:
            raise ValueError("Invalid grace_seconds")
        if kind == "schedule":
            expression = result.get("cron", "")
            if (
                not isinstance(expression, str)
                or len(expression.split()) != 5
                or not croniter.is_valid(expression)
            ):
                raise ValueError("A valid five-field cron expression is required")
        if kind == "heartbeat":
            interval = result.setdefault("interval_seconds", 300)
            if not isinstance(interval, (float, int)) or interval < 30:
                raise ValueError("Heartbeat interval must be at least 30 seconds")
            target = result.get("target", {})
            if not isinstance(target, dict) or target.get("kind") not in {"file", "run"}:
                raise ValueError("Heartbeat target must be an explicit file or run")
            if target["kind"] == "file":
                result["target"] = {"kind": "file", "path": str(Path(target["path"]).resolve())}
            elif not target.get("run_id"):
                raise ValueError("Heartbeat run target needs run_id")
            active = result.get("active_hours")
            if active is not None:
                if not isinstance(active, dict):
                    raise TypeError("active_hours must be a mapping")
                for field in ("start", "end"):
                    datetime.strptime(active[field], "%H:%M")
            days = result.setdefault("weekdays", list(range(7)))
            if not isinstance(days, list) or any(day not in range(7) for day in days):
                raise ValueError("weekdays must contain values from 0 to 6")
        if kind == "watcher":
            path = Path(result["path"]).resolve()
            if not path.is_dir() or self._ignored(path):
                raise ValueError("Watch path must be an existing, non-internal directory")
            result["path"] = str(path)
            result.setdefault("pattern", "*")
            result.setdefault("recursive", False)
            result.setdefault("debounce_seconds", 1)
            result.setdefault("stable_seconds", 2)
            result.setdefault("max_files", 10000)
            if any(
                not isinstance(result[key], (int, float)) or result[key] < 0
                for key in ("debounce_seconds", "stable_seconds")
            ):
                raise ValueError("Watcher stability windows must be non-negative")
            if not 1 <= result["max_files"] <= 100000:
                raise ValueError("Invalid watcher file budget")
        return result

    def _require_destination_available(self, db, session_id, mode, *, job_id=None):
        if mode == "source":
            return
        if mode == "sticky" and job_id:
            target = db.execute("SELECT session_id FROM automation_routes WHERE route_key=?", ("automation:" + job_id,)).fetchone()
            if target:
                self.store._require_session_writable(db, target[0])
                return
        # Fresh and not-yet-created sticky routes need a new project session.
        # Existing conversations remain usable after project removal, but new
        # project sessions must wait for explicit restoration of the project.
        source = db.execute("SELECT project_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        self.store._require_project_available(db, source[0])

    def create_job(
        self, kind: str, session_id: str, prompt: str, config: dict, *, enabled: bool = True
    ) -> dict:
        self.store.get_session(session_id)
        if not prompt.strip():
            raise ValueError("Automation prompt is required")
        config = self._selection_config(session_id, self._validate(kind, config))
        if kind == "heartbeat" and config["target"]["kind"] == "run":
            target_run = self.store.get_run(config["target"]["run_id"])
            if target_run["session_id"] != session_id:
                raise ValueError("Heartbeat run targets must belong to the same session")
        now = self.clock()
        next_fire = _next_cron(config["cron"], config["timezone"], now) if kind == "schedule" else now
        job_id = "auto_" + uuid.uuid4().hex
        with self.store.transaction() as db:
            self.store._require_session_writable(db, session_id)
            self._require_destination_available(db, session_id, config["session_mode"])
            db.execute(
                "INSERT INTO automation_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    kind,
                    session_id,
                    prompt,
                    _json(config),
                    int(enabled),
                    next_fire,
                    None,
                    None,
                    None,
                    now,
                    now,
                ),
            )
            self.store._event(db, session_id, None, "automation.created", {"job_id": job_id, "kind": kind})
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict:
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM automation_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = dict(row)
        result["config"] = json.loads(result.pop("config_json"))
        result["enabled"] = bool(result["enabled"])
        return result

    def list_jobs(self, session_id: str | None = None) -> list[dict]:
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT id FROM automation_jobs WHERE next_fire IS NOT NULL"
                + (" AND session_id=?" if session_id else "")
                + " ORDER BY created_at,id",
                (session_id,) if session_id else (),
            ).fetchall()
        return [self.get_job(row[0]) for row in rows]

    def update_job(
        self,
        job_id: str,
        *,
        prompt: str | None = None,
        config: dict | None = None,
        enabled: bool | None = None,
    ) -> dict:
        job = self.get_job(job_id)
        if job["next_fire"] is None:
            raise ValueError("Deleted jobs cannot be updated")
        changes = config or {}
        base = dict(job["config"])
        if changes.get("selection_mode") == "fixed" and base.get("selection_mode") != "fixed":
            base.pop("selection", None)
        merged = self._validate(job["kind"], {**base, **changes})
        # Disabling and unrelated edits must work even after a provider or Skill
        # disappears; recapture requires an explicit choice change.
        merged = self._selection_config(job["session_id"], merged,
                                        validate=bool({"selection_mode", "selection"} & set(changes)))
        if job["kind"] == "heartbeat" and merged["target"]["kind"] == "run":
            target_run = self.store.get_run(merged["target"]["run_id"])
            if target_run["session_id"] != job["session_id"]:
                raise ValueError("Heartbeat run targets must belong to the same session")
        text = job["prompt"] if prompt is None else prompt
        if not text.strip():
            raise ValueError("Automation prompt is required")
        now = self.clock()
        next_fire = job["next_fire"]
        if config is not None:
            next_fire = (
                _next_cron(merged["cron"], merged["timezone"], now) if job["kind"] == "schedule" else now
            )
        with self.store.transaction() as db:
            # Pure disabling remains available for an archived source; edits and
            # enabling must share the same transaction as lifecycle admission.
            if enabled is not False or prompt is not None or config is not None:
                self.store._require_session_writable(db, job["session_id"])
                self._require_destination_available(db, job["session_id"], merged["session_mode"], job_id=job_id)
            db.execute(
                "UPDATE automation_jobs SET prompt=?,config_json=?,enabled=?,next_fire=?,updated_at=? WHERE id=?",
                (
                    text,
                    _json(merged),
                    int(job["enabled"] if enabled is None else enabled),
                    next_fire,
                    now,
                    job_id,
                ),
            )
            self.store._event(db, job["session_id"], None, "automation.updated", {"job_id": job_id})
        return self.get_job(job_id)

    def delete_job(self, job_id: str):
        job = self.get_job(job_id)
        # Retain trigger evidence; deletion disables and hides the configuration only
        # after queued claims are suppressed. Already running work remains cancellable.
        with self.store.transaction() as db:
            db.execute(
                "UPDATE automation_triggers SET state='skipped',error='job_deleted' WHERE job_id=? AND state='prepared'",
                (job_id,),
            )
            db.execute(
                "UPDATE automation_jobs SET enabled=0,next_fire=NULL,updated_at=? WHERE id=?",
                (self.clock(), job_id),
            )
            self.store._event(db, job["session_id"], None, "automation.deleted", {"job_id": job_id})

    def receipts(self, job_id: str | None = None) -> list[dict]:
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT * FROM automation_triggers"
                + (" WHERE job_id=?" if job_id else "")
                + " ORDER BY scheduled_at,id",
                (job_id,) if job_id else (),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            raw = item.pop("execution_json")
            execution = json.loads(raw) if raw else None
            item["selection_mode"] = execution.get("selection_mode") if execution else None
            item["selection"] = execution.get("selection") if execution else None
            item["selection_captured"] = execution is not None
            output.append(item)
        return output

    def _ignored(self, path: Path) -> bool:
        return any(path == root or path.is_relative_to(root) for root in self.ignored_roots)

    def _claim(self, job: dict, key: str, scheduled_at: float, prompt: str, now: float):
        with self.store.transaction() as db:
            # A poll can hold an old job snapshot across an explicit disable.
            current = db.execute("SELECT enabled,config_json FROM automation_jobs WHERE id=?", (job["id"],)).fetchone()
            if not current or not current[0]:
                return
            config = json.loads(current["config_json"])
            midnight = (datetime.fromtimestamp(now, ZoneInfo(config["timezone"]))
                        .replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            self.store._require_session_writable(db, job["session_id"])
            if db.execute(
                "SELECT 1 FROM automation_triggers WHERE job_id=? AND trigger_key=?", (job["id"], key)
            ).fetchone():
                return
            pending = db.execute(
                "SELECT COUNT(*) FROM automation_triggers WHERE job_id=? AND state IN ('prepared','submitted')",
                (job["id"],),
            ).fetchone()[0]
            daily = db.execute(
                "SELECT COUNT(*) FROM automation_triggers WHERE job_id=? AND created_at>=? AND state NOT IN ('skipped','error')",
                (job["id"], midnight),
            ).fetchone()[0]
            reason = None
            if daily >= config["max_runs_per_day"]:
                reason = "daily_limit"
            elif pending >= config["max_pending"] or (pending and config["overlap"] == "skip"):
                reason = "overlap"
            trigger_id = "trigger_" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO automation_triggers(id,job_id,trigger_key,scheduled_at,state,prompt,error,created_at,execution_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    trigger_id,
                    job["id"],
                    key,
                    scheduled_at,
                    "skipped" if reason else "prepared",
                    prompt,
                    reason,
                    now,
                    _json(self._capture_execution(job["session_id"], config)) if not reason else None,
                ),
            )

    def _schedule(self, job: dict, now: float):
        config = job["config"]
        due, current = [], job["next_fire"]
        # A long downtime must not expand into an unbounded historical scan.
        if config["misfire"] in {"skip", "coalesce"}:
            if config["misfire"] == "coalesce" or now - current <= config["grace_seconds"]:
                due = [current]
            next_fire = _next_cron(config["cron"], config["timezone"], now)
        else:
            for _ in range(min(config["catchup_limit"], self.max_due_per_tick)):
                if current > now:
                    break
                due.append(current)
                current = _next_cron(config["cron"], config["timezone"], current)
            next_fire = _next_cron(config["cron"], config["timezone"], now)
        for timestamp in due:
            self._claim(job, f"cron:{timestamp:.6f}", timestamp, job["prompt"], now)
        with self.store.transaction() as db:
            db.execute(
                "UPDATE automation_jobs SET next_fire=?,last_fire=? WHERE id=?", (next_fire, now, job["id"])
            )

    def _heartbeat(self, job: dict, now: float):
        config = job["config"]
        with self.store.transaction() as db:
            db.execute(
                "UPDATE automation_jobs SET next_fire=? WHERE id=?",
                (now + config["interval_seconds"], job["id"]),
            )
        local = datetime.fromtimestamp(now, ZoneInfo(config["timezone"]))
        if local.weekday() not in config["weekdays"]:
            return
        hours = config.get("active_hours")
        if hours:
            value = local.strftime("%H:%M")
            start, end = hours["start"], hours["end"]
            active = start <= value < end if start < end else value >= start or value < end
            if not active:
                return
        target = config["target"]
        if target["kind"] == "file":
            path = Path(target["path"])
            observation = {"kind": "file", "path": str(path), "fingerprint": _fingerprint(path)}
        else:
            run = self.store.get_run(target["run_id"])
            observation = {
                "kind": "run",
                "run_id": target["run_id"],
                "status": run["status"],
                "answer": run.get("answer", ""),
                "error": run.get("error"),
            }
        digest = hashlib.sha256(_json(observation).encode()).hexdigest()
        if digest != job["observation_hash"]:
            if job["observation_hash"] is not None or config.get("trigger_initial", False):
                text = job["prompt"] + "\n\nObserved target state (untrusted data):\n" + _json(observation)
                self._claim(job, f"heartbeat:{now:.6f}:{digest}", now, text, now)
            with self.store.transaction() as db:
                db.execute("UPDATE automation_jobs SET observation_hash=? WHERE id=?", (digest, job["id"]))

    def _watch(self, job: dict, now: float):
        config = job["config"]
        root = Path(config["path"])
        if not root.is_dir():
            raise ValueError("Watched directory is no longer available")
        iterator = root.rglob(config["pattern"]) if config["recursive"] else root.glob(config["pattern"])
        seen = set()
        for index, candidate in enumerate(iterator):
            if index >= config["max_files"]:
                raise ValueError("Watcher file budget reached; increase max_files or narrow the pattern")
            path = candidate.resolve()
            if (
                not path.is_relative_to(root)
                or self._ignored(path)
                or candidate.name.startswith((".", "~$"))
                or candidate.suffix in {".tmp", ".part", ".crdownload"}
            ):
                continue
            fingerprint = _fingerprint(path)
            if fingerprint is None:
                continue
            path_text = str(path)
            seen.add(path_text)
            with self.store.transaction() as db:
                row = db.execute(
                    "SELECT * FROM automation_watch_state WHERE job_id=? AND path=?", (job["id"], path_text)
                ).fetchone()
                if row is None:
                    # The first scan establishes a baseline unless explicitly requested.
                    baseline = (
                        fingerprint
                        if job["last_fire"] is None and not config.get("include_existing", False)
                        else None
                    )
                    db.execute(
                        "INSERT INTO automation_watch_state VALUES(?,?,?,?,?)",
                        (job["id"], path_text, fingerprint, baseline, now),
                    )
                    continue
                row = dict(row)
                if row["observed"] != fingerprint:
                    db.execute(
                        "UPDATE automation_watch_state SET observed=?,changed_at=? WHERE job_id=? AND path=?",
                        (fingerprint, now, job["id"], path_text),
                    )
                    continue
            if fingerprint != row["delivered"] and now - row["changed_at"] >= max(
                config["debounce_seconds"], config["stable_seconds"]
            ):
                key = "file:" + hashlib.sha256((path_text + ":" + fingerprint).encode()).hexdigest()
                self._claim(
                    job,
                    key,
                    now,
                    job["prompt"] + "\n\nStable file (untrusted path data): " + _json(path_text),
                    now,
                )
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE automation_watch_state SET delivered=? WHERE job_id=? AND path=?",
                        (fingerprint, job["id"], path_text),
                    )
        with self.store.transaction() as db:
            db.execute(
                "UPDATE automation_jobs SET last_fire=?,next_fire=? WHERE id=?",
                (now, now + self.poll_seconds, job["id"]),
            )

    async def _deliver(self, now: float):
        for receipt in self.receipts():
            if receipt["state"] != "prepared":
                continue
            job = self.get_job(receipt["job_id"])
            if not job["enabled"]:
                continue
            try:
                with self.store.transaction() as db:
                    row = db.execute("SELECT execution_json FROM automation_triggers WHERE id=?", (receipt["id"],)).fetchone()
                    execution = json.loads(row[0]) if row[0] else None
                mode = execution.get("session_mode", job["config"]["session_mode"]) if execution else job["config"]["session_mode"]
                pinned = self.store.automation_execution_session(job["session_id"], job["id"], receipt["id"], mode)
                execution_session = pinned["session_id"]
                with self.store.transaction() as db:
                    accepted = db.execute("SELECT 1 FROM runs WHERE session_id=? AND request_id=?",
                                          (execution_session, receipt["id"])).fetchone()
                    if execution is None and not accepted:
                        # Legacy prepared receipts are captured once, before the
                        # first submission. A committed old run wins idempotently.
                        execution = self._capture_execution(job["session_id"], job["config"])
                        if execution is not None:
                            db.execute("UPDATE automation_triggers SET execution_json=? WHERE id=? AND execution_json IS NULL",
                                       (_json(execution), receipt["id"]))
                kwargs = {"execution_snapshot": execution} if execution is not None and not accepted else {}
                run = await self.coordinator.submit(
                    execution_session, receipt["prompt"], request_id=receipt["id"], unattended=True, **kwargs
                )
            except Exception as exc:  # noqa: BLE001 -- persistent queue errors are recorded, not allowed to kill the scheduler
                with self.store.transaction() as db:
                    attempts = receipt["attempts"] + 1
                    db.execute(
                        "UPDATE automation_triggers SET attempts=?,state=?,error=? WHERE id=?",
                        (
                            attempts,
                            "error" if attempts >= job["config"]["max_delivery_attempts"] else "prepared",
                            type(exc).__name__,
                            receipt["id"],
                        ),
                    )
                    if attempts >= job["config"]["max_delivery_attempts"]:
                        self.store._event(
                            db,
                            job["session_id"],
                            None,
                            "automation.failed",
                            {"job_id": job["id"], "trigger_id": receipt["id"], "error": type(exc).__name__},
                        )
                continue
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE automation_triggers SET run_id=?,state='submitted',attempts=attempts+1,error=NULL WHERE id=?",
                    (run["id"], receipt["id"]),
                )
                self.store._event(
                    db,
                    job["session_id"],
                    run["id"],
                    "automation.submitted",
                    {"job_id": job["id"], "trigger_id": receipt["id"], "execution_session_id": execution_session,
                     "source_session_id": job["session_id"], "session_mode": pinned["session_mode"]},
                )

    def _mark_self_outputs(self, job: dict, run_id: str, now: float):
        if job["kind"] != "watcher":
            return
        root = Path(job["config"]["path"])
        for execution in self.store.tool_executions(run_id):
            result = execution.get("result") or {}
            paths = [item.get("path") for item in result.get("artifacts", [])]
            if execution["risk"] != "read":
                paths.append(result.get("data", {}).get("path"))
            for value in paths:
                if not value:
                    continue
                path = Path(value).resolve()
                fingerprint = _fingerprint(path)
                if path.is_relative_to(root) and fingerprint:
                    with self.store.transaction() as db:
                        db.execute(
                            "INSERT INTO automation_watch_state VALUES(?,?,?,?,?) ON CONFLICT(job_id,path) DO UPDATE SET observed=excluded.observed,delivered=excluded.delivered,changed_at=excluded.changed_at",
                            (job["id"], str(path), fingerprint, fingerprint, now),
                        )

    def _results(self, now: float):
        terminal = {
            "completed",
            "cancelled",
            "failed",
            "timed_out",
            "budget_exhausted",
            "needs_reconciliation",
        }
        for receipt in self.receipts():
            if receipt["state"] != "submitted" or not receipt["run_id"]:
                continue
            run = self.store.get_run(receipt["run_id"])
            status = run["status"]
            if status == receipt["last_status"]:
                continue
            job = self.get_job(receipt["job_id"])
            digest = hashlib.sha256(
                _json({"status": status, "answer": run.get("answer"), "error": run.get("error")}).encode()
            ).hexdigest()
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE automation_triggers SET last_status=?,state=? WHERE id=?",
                    (status, "completed" if status in terminal else "submitted", receipt["id"]),
                )
                if status in {"waiting_approval", "waiting_input", "needs_reconciliation"}:
                    self.store._event(
                        db,
                        job["session_id"],
                        run["id"],
                        "automation.action_required",
                        {"job_id": job["id"], "status": status},
                    )
                elif status in terminal and (job["kind"] != "heartbeat" or digest != job["result_hash"]):
                    self.store._event(
                        db,
                        job["session_id"],
                        run["id"],
                        "automation.result",
                        {
                            "job_id": job["id"],
                            "status": status,
                            "answer": run.get("answer", ""),
                            "error": run.get("error"),
                        },
                    )
                if status in terminal:
                    db.execute("UPDATE automation_jobs SET result_hash=? WHERE id=?", (digest, job["id"]))
            if status in terminal:
                self._mark_self_outputs(job, run["id"], now)

    async def tick(self, now: float | None = None):
        async with self._lock:
            now = self.clock() if now is None else now
            self._results(now)
            due = sorted(
                (
                    job
                    for job in self.list_jobs()
                    if job["enabled"] and job["next_fire"] is not None and job["next_fire"] <= now
                ),
                key=lambda job: (job["next_fire"], job["id"]),
            )
            for job in due[: self.max_due_per_tick]:
                try:
                    if job["kind"] == "schedule":
                        self._schedule(job, now)
                    elif job["kind"] == "heartbeat":
                        self._heartbeat(job, now)
                    else:
                        self._watch(job, now)
                except (OSError, ValueError, KeyError) as exc:
                    self.update_job(job["id"], enabled=False)
                    self.store.emit_event(
                        job["session_id"],
                        None,
                        "automation.failed",
                        {"job_id": job["id"], "error": type(exc).__name__, "message": str(exc)},
                    )
            await self._deliver(now)
            self._results(now)
            await self._consume_events()

    async def _consume_events(self):
        if self.event_consumer is None:
            return
        with self.store.transaction() as db:
            row = db.execute("SELECT last_event_id FROM automation_consumers WHERE name='default'").fetchone()
        cursor = row[0] if row else 0
        for event in self.store.events(after=cursor, limit=500):
            if event["type"].startswith("automation."):
                try:
                    result = self.event_consumer(event)
                    if hasattr(result, "__await__"):
                        await result
                except Exception:  # noqa: BLE001 -- delivery is retried with the same durable event id
                    return
            with self.store.transaction() as db:
                db.execute(
                    "INSERT INTO automation_consumers VALUES('default',?) ON CONFLICT(name) DO UPDATE SET last_event_id=excluded.last_event_id",
                    (event["event_id"],),
                )

    async def start(self):
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()

        async def loop():
            while not self._stop.is_set():
                await self.tick()
                try:
                    await asyncio.wait_for(self._stop.wait(), self.poll_seconds)
                except TimeoutError:
                    pass

        self._task = asyncio.create_task(loop(), name="aster-automation")

    async def close(self):
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    def events(self, *, after: int = 0, limit: int = 1000) -> list[dict]:
        """Global durable event cursor for UI/notification consumers."""
        return [
            event
            for event in self.store.events(after=after, limit=limit)
            if event["type"].startswith("automation.")
        ]
