"""Source-linked facts and bounded scoped query projections beside Runtime state."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

from aster.memory.service import MemoryService


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _time(value=None):
    parsed = datetime.now(UTC) if value is None else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Memory timestamps require an explicit timezone")
    return parsed.astimezone(UTC).isoformat()


class StructuredMemory:
    """A scoped view; every method validates scope before looking up facts/evidence."""

    def __init__(self, store, markdown: MemoryService):
        self.store, self.markdown = store, markdown
        self.agent_id, self.project_id = markdown.agent_id, markdown.project_id
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS memory_facts(
                id TEXT PRIMARY KEY,scope TEXT NOT NULL,agent_id TEXT NOT NULL,project_id TEXT NOT NULL,
                entity TEXT NOT NULL,relation TEXT NOT NULL,value_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT,
                status TEXT NOT NULL,version INTEGER NOT NULL,predecessor TEXT,
                evidence_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS memory_revisions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,fact_id TEXT NOT NULL,version INTEGER NOT NULL,
                action TEXT NOT NULL,snapshot_json TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,
                UNIQUE(fact_id,version))""")
            db.execute("CREATE INDEX IF NOT EXISTS memory_scope ON memory_facts(scope,agent_id,project_id,status,entity,relation)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(memory_facts)")}
            for column in ("entity_type", "target_type"):
                if column not in columns:
                    db.execute("ALTER TABLE memory_facts ADD COLUMN " + column + " TEXT")

    def _scope(self, scope):
        if scope == "global":
            return scope, "", ""
        if scope == "agent":
            return scope, self.agent_id, ""
        if scope == "project" and self.project_id:
            return scope, self.agent_id, self.project_id
        raise ValueError("Scope must be global, current agent, or current project")

    @staticmethod
    def _row(row):
        data = dict(row)
        data["value"] = json.loads(data.pop("value_json"))
        data["evidence"] = json.loads(data.pop("evidence_json"))
        return data

    def _get(self, db, fact_id, scope):
        row = db.execute("SELECT * FROM memory_facts WHERE id=? AND scope=? AND agent_id=? AND project_id=?",
                         (fact_id, *self._scope(scope))).fetchone()
        if row is None:
            raise ValueError("Fact not found in the requested scope")
        return self._row(row)

    def get(self, fact_id, *, scope="agent"):
        with self.store.transaction() as db:
            return self._get(db, fact_id, scope)

    def _evidence(self, db, sources, scope, *, session_id=None, run_id=None):
        if not isinstance(sources, list) or not 1 <= len(sources) <= 20:
            raise ValueError("Facts require 1-20 explicit evidence records")
        namespace = self._scope(scope)
        evidence = []
        for source in sources:
            if not isinstance(source, dict):
                raise TypeError("Evidence must be structured")
            kind = source.get("kind")
            quote = source.get("quote", "")
            if not isinstance(quote, str) or not quote.strip() or len(quote) > 8000:
                raise ValueError("Each evidence item requires a bounded exact quote")
            if kind in {"message", "current_user"}:
                if scope == "global":
                    raise ValueError("Private session evidence cannot be promoted into global memory")
                if kind == "current_user":
                    if not session_id or not run_id:
                        raise ValueError("Current-user evidence requires a running session")
                    row = db.execute("SELECT m.*,s.agent_id,s.project_id FROM messages m JOIN sessions s ON s.id=m.session_id WHERE m.session_id=? AND m.run_id=? AND m.role='user' ORDER BY m.seq DESC LIMIT 1",
                                     (session_id, run_id)).fetchone()
                else:
                    row = db.execute("SELECT m.*,s.agent_id,s.project_id FROM messages m JOIN sessions s ON s.id=m.session_id WHERE m.id=?",
                                     (source.get("message_id"),)).fetchone()
                required_project = namespace[2] or None
                if row is None or row["agent_id"] != self.agent_id or row["project_id"] != required_project:
                    raise ValueError("Evidence message is missing or belongs to another scope")
                if quote not in row["content"]:
                    raise ValueError("Evidence quote is absent from the original message")
                evidence.append({"kind": "message", "message_id": row["id"], "session_id": row["session_id"],
                    "quote": quote, "source_sha256": hashlib.sha256(row["content"].encode()).hexdigest(),
                    "verified": True, "scope": scope})
            elif kind == "markdown":
                if source.get("scope") != scope:
                    raise ValueError("Markdown evidence must be in the same scope")
                note = self.markdown.read(scope, source.get("name", "MEMORY"))
                if not note["exists"] or source.get("version") != note["version"] or quote not in note["content"]:
                    raise ValueError("Markdown evidence version or quote no longer matches")
                evidence.append({"kind": kind, "name": note["name"], "scope": scope,
                                 "version": note["version"], "quote": quote, "verified": True})
            elif kind in {"external", "user_supplied"}:
                label = source.get("label")
                if not isinstance(label, str) or not label.strip() or len(label) > 1000:
                    raise ValueError("Unverified evidence requires an explicit source label")
                evidence.append({"kind": kind, "label": label, "quote": quote, "verified": False,
                                 "scope": scope, "verification": "supplied_only_not_fetched"})
            else:
                raise ValueError("Unknown evidence kind")
        return evidence

    def _insert(self, db, *, scope, entity, relation, value, evidence, observed_at=None,
                valid_from=None, valid_to=None, predecessor=None, reason="recorded", entity_type=None, target_type=None):
        if any(not isinstance(v, str) or not v.strip() or len(v) > 300 for v in (entity, relation)):
            raise ValueError("Entity and relation require bounded nonempty text")
        encoded = _json(value)
        if len(encoded) > 16000:
            raise ValueError("Fact value exceeds 16000 characters")
        for label in (entity_type, target_type):
            if label is not None and (not isinstance(label, str) or not label.strip() or len(label) > 300):
                raise ValueError("Explicit entity types require bounded nonempty labels")
            if label is not None and not any(label in source["quote"] for source in evidence):
                raise ValueError("Explicit entity types must occur in the supplied source quotes")
        if target_type is not None and not isinstance(value, str):
            raise ValueError("A target type requires a string entity label as the fact value")
        now, observed = _time(), _time(observed_at)
        start, end = _time(valid_from or observed), _time(valid_to) if valid_to else None
        if end and end <= start:
            raise ValueError("valid_to must be later than valid_from")
        fact_id = "fact_" + uuid.uuid4().hex
        db.execute("""INSERT INTO memory_facts(id,scope,agent_id,project_id,entity,relation,value_json,observed_at,
                   valid_from,valid_to,status,version,predecessor,evidence_json,created_at,updated_at,entity_type,target_type)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (fact_id, *self._scope(scope), entity.strip(), relation.strip(), encoded, observed,
                    start, end, "current", 1, predecessor, _json(evidence), now, now, entity_type, target_type))
        result = self._get(db, fact_id, scope)
        self._revision(db, result, "record", reason)
        return result

    @staticmethod
    def _revision(db, fact, action, reason):
        db.execute("INSERT INTO memory_revisions(fact_id,version,action,snapshot_json,reason,created_at) VALUES(?,?,?,?,?,?)",
                   (fact["id"], fact["version"], action, _json(fact), reason, _time()))

    def record(self, *, entity, relation, value, sources, scope="agent", observed_at=None,
               valid_from=None, valid_to=None, session_id=None, run_id=None, entity_type=None, target_type=None):
        with self.store.transaction() as db:
            evidence = self._evidence(db, sources, scope, session_id=session_id, run_id=run_id)
            result = self._insert(db, scope=scope, entity=entity, relation=relation, value=value,
                                  evidence=evidence, observed_at=observed_at, valid_from=valid_from, valid_to=valid_to,
                                  entity_type=entity_type, target_type=target_type)
        return {**result, "conflicts": self.recall(entity=entity, relation=relation, scopes=[scope])["conflicts"]}

    def supersede(self, fact_id, *, expected_version, value, sources, reason, scope="agent",
                  observed_at=None, valid_from=None, valid_to=None, session_id=None, run_id=None,
                  entity_type=None, target_type=None):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("A bounded revision reason is required")
        with self.store.transaction() as db:
            old = self._get(db, fact_id, scope)
            if old["version"] != expected_version or old["status"] != "current":
                raise ValueError("Fact changed or is no longer current; reread before updating")
            evidence = self._evidence(db, sources, scope, session_id=session_id, run_id=run_id)
            for label in (entity_type, target_type):
                if label is not None and (not isinstance(label, str) or not any(label in source["quote"] for source in evidence)):
                    raise ValueError("Explicit entity types must occur in the new supplied source quotes")
            inherited_entity_type = entity_type if entity_type is not None else old.get("entity_type")
            inherited_target_type = (target_type if target_type is not None else
                                     old.get("target_type") if value == old["value"] else None)
            # Retain the original explicit type proof when a revision only
            # changes another property; never transfer a target type to a new label.
            for label in (inherited_entity_type, inherited_target_type):
                if label and not any(label in source["quote"] for source in evidence):
                    proof = next((source for source in old["evidence"] if label in source["quote"]), None)
                    if proof is not None and proof not in evidence:
                        evidence.append(proof)
            transition = _time(valid_from or observed_at)
            if transition <= old["valid_from"] or transition > _time():
                raise ValueError("A superseding effective time must follow the old start and cannot be future-dated")
            new = self._insert(db, scope=scope, entity=old["entity"], relation=old["relation"], value=value,
                               evidence=evidence, observed_at=observed_at, valid_from=transition,
                               valid_to=valid_to, predecessor=fact_id, reason=reason,
                               entity_type=inherited_entity_type, target_type=inherited_target_type)
            end = min(old["valid_to"], transition) if old["valid_to"] else transition
            db.execute("UPDATE memory_facts SET status='superseded',version=version+1,valid_to=?,updated_at=? WHERE id=?", (end, _time(), fact_id))
            self._revision(db, self._get(db, fact_id, scope), "supersede", reason)
            return new

    def forget(self, fact_id, *, expected_version, reason, scope="agent"):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("A bounded retraction reason is required")
        with self.store.transaction() as db:
            old = self._get(db, fact_id, scope)
            if old["version"] != expected_version or old["status"] == "retracted":
                raise ValueError("Fact changed or was already retracted")
            db.execute("UPDATE memory_facts SET status='retracted',version=version+1,updated_at=? WHERE id=?", (_time(), fact_id))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_embeddings'").fetchone():
                db.execute("DELETE FROM memory_embeddings WHERE fact_id=?", (fact_id,))
            result = self._get(db, fact_id, scope)
            self._revision(db, result, "retract", reason)
            return result

    def history(self, fact_id, *, scope="agent"):
        with self.store.transaction() as db:
            self._get(db, fact_id, scope)
            rows = db.execute("SELECT * FROM memory_revisions WHERE fact_id=? ORDER BY version", (fact_id,)).fetchall()
            return [{**dict(row), "snapshot": json.loads(row["snapshot_json"])} for row in rows]

    def recall(self, *, query="", entity=None, relation=None, scopes=None, at=None,
               statuses=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Recall limit must be between 1 and 200")
        return self._recall(query=query, entity=entity, relation=relation, scopes=scopes, at=at,
                            statuses=statuses, limit=limit)

    def candidates(self, *, scopes=None, at=None, scan_limit=2000):
        if type(scan_limit) is not int or not 1 <= scan_limit <= 10000:
            raise ValueError("Candidate scan_limit must be between 1 and 10000; raise it explicitly for larger scopes")
        return self._recall(scopes=scopes, at=at, limit=scan_limit)

    def query(self, **arguments):
        from aster.memory.graph import query
        return query(self, **arguments)

    def _recall(self, *, query="", entity=None, relation=None, scopes=None, at=None, statuses=None, limit=50):
        chosen = list(dict.fromkeys(scopes or (["agent", "project", "global"] if self.project_id else ["agent", "global"])))
        spaces = [self._scope(scope) for scope in chosen]
        # Explicit historical queries reconstruct effective-time state. Retraction
        # suppresses all ordinary recall, including historical recall; audit/history
        # and an explicit statuses=["retracted"] request retain forensic access.
        states = statuses or (["current", "superseded"] if at is not None else ["current"])
        if not states or any(status not in {"current", "superseded", "retracted"} for status in states):
            raise ValueError("Invalid fact status")
        clauses = ["(" + " OR ".join("(scope=? AND agent_id=? AND project_id=?)" for _ in spaces) + ")",
                   "status IN (" + ",".join("?" for _ in states) + ")"]
        args = [value for namespace in spaces for value in namespace] + states
        for key, value in (("entity", entity), ("relation", relation)):
            if value is not None:
                clauses.append(key + "=?")
                args.append(value)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(entity LIKE ? ESCAPE '\\' OR relation LIKE ? ESCAPE '\\' OR value_json LIKE ? ESCAPE '\\')")
            args.extend(["%" + escaped + "%"] * 3)
        instant = _time(at)
        clauses.append("valid_from<=? AND (valid_to IS NULL OR valid_to>?)")
        args.extend([instant, instant])
        with self.store.transaction() as db:
            rows = db.execute("SELECT * FROM memory_facts WHERE " + " AND ".join(clauses) + " ORDER BY observed_at DESC,id LIMIT ?", (*args, limit + 1)).fetchall()
        truncated = len(rows) > limit
        facts = [self._row(row) for row in rows[:limit]]
        groups = {}
        for fact in facts:
            groups.setdefault((fact["scope"], fact["entity"], fact["relation"]), []).append(fact)
        conflicts = [{"scope": key[0], "entity": key[1], "relation": key[2], "fact_ids": [f["id"] for f in group]}
                     for key, group in groups.items() if len({_json(f["value"]) for f in group}) > 1]
        return {"facts": facts, "conflicts": conflicts, "truncated": truncated, "at": instant,
                "no_data": not facts, "retrieval": "scoped_exact_and_keyword_not_vector"}
