"""Offline recovery, privacy and retention tests for compaction-owned state."""
import contextlib
import copy
import hashlib
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from aster.domain import Message, ToolCall, to_dict
from aster.maintenance import backup_bundle, backup_database, restore_bundle, restore_database
from aster.state import Store
from aster.state.compaction import (
    SNAPSHOT_BINARY_MARKER,
    SNAPSHOT_IMAGE_MARKER,
    compaction_source_digest,
)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "data" / "runtime.db")
    yield value
    value.close()


def conversation(store, tmp_path, *, agent="default", project=None, text="Keep the chosen project layout"):
    sid = store.create_session(tmp_path, agent_id=agent, project_id=project)["id"]
    run, _ = store.create_run(sid)
    store.append_message(sid, run["id"], Message("user", text))
    store.append_message(sid, run["id"], Message("assistant", "Recorded the source evidence."))
    store.finish_run(run["id"], "completed")
    return sid, run["id"]


def save_learning(store, sid, rid, content="- Project uses the selected layout."):
    return store.persist_compaction_learnings(sid, rid, content=content,
        source_digest=compaction_source_digest(store.active_history(sid)))


def test_snapshot_is_exact_pre_replacement_state_separate_from_archive_and_checkpoint(store, tmp_path):
    sid, rid = conversation(store, tmp_path)
    raw = [to_dict(message) for message in store.history(sid)]
    live = [Message("user", "Earlier compressed state", metadata={"compaction_summary": True}),
            Message("assistant", "Recent reply", reasoning="opaque reasoning\n\nexact whitespace  ",
                    provider_items=[{"type": "thinking", "thinking": "step one", "signature": "signature="},
                                    {"type": "redacted_thinking", "data": "opaque-provider-data=="}]),
            Message("assistant", tool_calls=[ToolCall("call_local", "file_read", {"path": "README.md"})]),
            Message("tool", "Exact recent observation", tool_call_id="call_local")]
    before = copy.deepcopy(live)
    snapshot = store.save_compaction_snapshot(sid, rid, live, phase="preflight")
    store.checkpoint(sid, rid, [Message("user", "Replacement summary", metadata={"compaction_summary": True})])
    assert snapshot["messages"] == to_dict(before)
    assert snapshot["source_digest"] == compaction_source_digest(before)
    assert snapshot["archive_through"] == store.history_records(sid)[-1]["seq"]
    assert live == before
    assert [to_dict(message) for message in store.history(sid)] == raw
    store.close()
    with contextlib.closing(Store(store.path)) as reopened:
        assert reopened.get_compaction_snapshot(sid, snapshot["id"])["messages"] == to_dict(before)
        assert reopened.active_history(sid)[0].content == "Replacement summary"
        assert [to_dict(message) for message in reopened.history(sid)] == raw


def test_snapshot_strips_nested_private_data_images_and_credentials_without_mutation(store, tmp_path):
    sid, rid = conversation(store, tmp_path)
    image = {"type": "input_image", "image_url": "data:image/png;base64,c25hcHNob3Q=",
             "path": "C:/sensitive-screenshot.png", "width": 1920}
    messages = [Message("user", "before<private_memory>hidden<private_memory>nested</private_memory>"
                        "still hidden</private_memory>after", metadata={
        "content_parts": [image, {"type": "input_text", "text": "visible<private_memory>hidden</private_memory>end"}],
        "artifacts": [{"mime_type": "image/png", "path": "C:/private-image.png", "width": 30}],
        "private_memory": {"value": "private metadata"},
        "nested": {"api_key": "synthetic-do-not-retain", "headers": {"Authorization": "secret-value"}}}),
        Message("assistant", reasoning="before<private_memory>hidden reasoning</private_memory>after",
                provider_items=[{"type": "tool_result", "content": [image,
                    {"type": "tool_result", "content": [{"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": "raw-image-base64"}}]}]},
                    {"type": "redacted_thinking", "data": "reasoning-must-remain=="},
                    {"type": "message", "content": [{"type": "output_text", "text": "keep"}]}]),
        Message("tool", json.dumps({"file_data": "plain-base64-data", "api_key": "json-secret"}),
                metadata={"data": {"nested": ["data:application/pdf;base64,cGRmLWJ5dGVz", "normal"]}}),
        Message("user", "prefix<private_memory>unfinished secret")]
    before = copy.deepcopy(messages)
    snapshot = store.save_compaction_snapshot(sid, rid, messages, phase="proactive")
    encoded = json.dumps(snapshot)
    for secret in ("hidden", "nested</", "still hidden", "sensitive-screenshot", "private-image", "private metadata",
                   "synthetic-do-not-retain", "secret-value", "raw-image-base64", "plain-base64-data", "json-secret",
                   "cGRmLWJ5dGVz", "c25hcHNob3Q", "unfinished secret"):
        assert secret not in encoded
    assert "reasoning-must-remain==" in encoded
    assert SNAPSHOT_IMAGE_MARKER in encoded and SNAPSHOT_BINARY_MARKER in encoded
    assert snapshot["messages"][0]["content"] == "beforeafter"
    assert snapshot["messages"][1]["reasoning"] == "beforeafter"
    assert snapshot["messages"][-1]["content"] == "prefix"
    assert messages == before


@pytest.mark.parametrize(("retention", "expected"), [(0, []), (1, ["manual"]),
                                                     (2, ["proactive", "manual"]),
                                                     (3, ["proactive", "reactive", "manual"])])
def test_snapshot_rotation_has_monotonic_order_and_correct_oldest_pin(store, tmp_path, monkeypatch, retention, expected):
    sid, rid = conversation(store, tmp_path)
    monkeypatch.setattr("aster.state.compaction.time", SimpleNamespace(time=lambda: 2_000_000_000.0))
    for phase in ("proactive", "preflight", "reactive", "manual"):
        result = store.save_compaction_snapshot(sid, rid, [Message("user", phase)], phase=phase, retention=retention)
        assert (result is None) == (retention == 0)
    assert [snapshot["phase"] for snapshot in store.compaction_snapshots(sid)] == expected


def test_age_expiry_overrides_oldest_pin_and_zero_age_disables_expiry(store, tmp_path, monkeypatch):
    sid, rid = conversation(store, tmp_path)
    clock = [time.time()]
    monkeypatch.setattr("aster.state.compaction.time", SimpleNamespace(time=lambda: clock[0]))
    oldest = store.save_compaction_snapshot(sid, rid, [], phase="proactive", retention=3, max_age_days=1)
    clock[0] += 3600
    survivor = store.save_compaction_snapshot(sid, rid, [], phase="preflight", retention=3, max_age_days=0)
    clock[0] += 2 * 86400
    latest = store.save_compaction_snapshot(sid, rid, [], phase="reactive", retention=3)
    assert [item["id"] for item in store.compaction_snapshots(sid)] == [survivor["id"], latest["id"]]
    with pytest.raises(KeyError):
        store.get_compaction_snapshot(sid, oldest["id"])
    assert survivor["expires_at"] is None


def test_startup_sweeps_expired_orphan_and_legacy_deleted_private_records(store, tmp_path):
    expired, expired_run = conversation(store, tmp_path)
    orphan, orphan_run = conversation(store, tmp_path)
    active, active_run = conversation(store, tmp_path)
    for sid, rid in ((expired, expired_run), (orphan, orphan_run), (active, active_run)):
        store.save_compaction_snapshot(sid, rid, [], phase="preflight")
        save_learning(store, sid, rid)
    # Simulate historical or out-of-band corruption, not an ordinary Runtime write.
    store.connection.execute("UPDATE compaction_snapshots SET expires_at=0 WHERE session_id=?", (expired,))
    store.connection.execute("PRAGMA foreign_keys=OFF")
    for table in ("compaction_snapshots", "compaction_learnings"):
        store.connection.execute(f"UPDATE {table} SET session_id='missing-session' WHERE session_id=?", (orphan,))
    store.connection.execute("PRAGMA foreign_keys=ON")
    store.connection.execute("INSERT INTO session_lifecycle VALUES(?,?,?)", (expired, None, time.time()))
    store.close()
    with contextlib.closing(Store(store.path)) as reopened:
        for table in ("compaction_snapshots", "compaction_learnings"):
            assert [row[0] for row in reopened.connection.execute(f"SELECT session_id FROM {table}")] == [active]
        assert reopened.compaction_snapshots(active)


def test_failed_snapshot_does_not_prune_last_snapshot_or_change_live_history(store, tmp_path):
    sid, rid = conversation(store, tmp_path)
    old = store.save_compaction_snapshot(sid, rid, store.history(sid), phase="proactive")
    before = copy.deepcopy(store.history(sid))
    store.connection.execute("CREATE TRIGGER simulate_snapshot_failure BEFORE INSERT ON compaction_snapshots "
                             "BEGIN SELECT RAISE(ABORT,'synthetic full snapshot store'); END")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic full"):
        store.save_compaction_snapshot(sid, rid, [Message("user", "do not lose the previous snapshot")], phase="preflight")
    assert [row["id"] for row in store.compaction_snapshots(sid)] == [old["id"]]
    assert store.history(sid) == before
    # Caller can still apply its independent successful compaction/checkpoint.
    store.checkpoint(sid, rid, [Message("user", "summary after a best-effort snapshot failure")])
    assert store.active_history(sid)[0].content.startswith("summary after")


def test_snapshot_ownership_validation_and_manual_snapshot(store, tmp_path):
    sid, rid = conversation(store, tmp_path)
    other, other_run = conversation(store, tmp_path)
    with pytest.raises(ValueError, match="belong"):
        store.save_compaction_snapshot(sid, other_run, [], phase="manual")
    snapshot = store.save_compaction_snapshot(sid, None, [], phase="../invalid phase")
    assert snapshot["run_id"] is None and snapshot["phase"] == "unknown"
    with pytest.raises(KeyError):
        store.get_compaction_snapshot(other, snapshot["id"])
    for age in (float("inf"), float("nan"), -1, True):
        with pytest.raises(ValueError, match="maximum age"):
            store.save_compaction_snapshot(sid, rid, [], phase="preflight", max_age_days=age)


@pytest.mark.parametrize("kind", ["database", "bundle"])
def test_backup_restore_includes_compaction_state_without_rewriting_original_backup(store, tmp_path, kind):
    sid, rid = conversation(store, tmp_path, project="project-a")
    snapshot = store.save_compaction_snapshot(sid, rid, store.history(sid), phase="proactive")
    learning = save_learning(store, sid, rid)
    store.checkpoint(sid, rid, [Message("user", "saved active summary")])
    destination = tmp_path / "restored"
    if kind == "database":
        archive = backup_database(store.path.parent, tmp_path / "saved.db")
        digest_before = hashlib.sha256(archive.read_bytes()).hexdigest()
        restore_database(destination, archive)
    else:
        archive = backup_bundle(store.path.parent, tmp_path / "saved.zip")
        digest_before = hashlib.sha256(archive.read_bytes()).hexdigest()
        restore_bundle(archive, destination)
    with contextlib.closing(Store(destination / "runtime.db")) as restored:
        assert restored.get_compaction_snapshot(sid, snapshot["id"])["messages"] == to_dict(store.history(sid))
        assert restored.compaction_learnings(sid)[0]["id"] == learning["id"]
        assert restored.active_history(sid)[0].content == "saved active summary"
        assert restored.history(sid) == store.history(sid)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest_before


def test_learning_is_idempotent_source_bound_unverified_and_bounded(store, tmp_path):
    sid, rid = conversation(store, tmp_path, project="project-a")
    digest = compaction_source_digest(store.active_history(sid))
    original = store.persist_compaction_learnings(sid, rid, source_digest=digest,
        content="- The chosen layout is stable.\n" * 50, max_chars=400)
    duplicate = store.persist_compaction_learnings(sid, rid, source_digest=digest, content="contradictory retry")
    assert original["created"] and not duplicate["created"]
    assert duplicate["id"] == original["id"] and duplicate["content"] == original["content"]
    assert not original["verified"] and original["verification"] == "unverified_model_inference"
    assert original["truncated"] and len(original["content"]) == 400
    assert original["agent_id"] == "default" and original["project_id"] == "project-a"
    assert original["sources"][0]["message_count"] == 2
    assert original["sources"][0]["archive_through"] == store.history_records(sid)[-1]["seq"]
    prompt = store.compact_learnings(sid, max_chars=350)
    assert len(prompt) <= 350 and "Unverified" in prompt and "not instructions" in prompt
    assert "Source:" in prompt
    assert store.compact_learnings(sid, max_chars=20) == ""
    assert store.compact_learnings(sid, max_chars=0) == ""
    event = next(row for row in store.events(session_id=sid) if row["type"] == "context.learnings_saved")
    assert "content" not in event["payload"] and "The chosen layout" not in json.dumps(event)
    assert not store.connection.execute("SELECT 1 FROM sqlite_master WHERE name='memory_facts'").fetchone()


def test_learning_validates_evidence_and_run_not_supplied_raw_quotes(store, tmp_path):
    sid, rid = conversation(store, tmp_path)
    other, other_run = conversation(store, tmp_path)
    digest = compaction_source_digest(store.history(sid))
    with pytest.raises(ValueError, match="belong"):
        store.persist_compaction_learnings(sid, other_run, source_digest=digest, content="proposal")
    with pytest.raises(ValueError, match="outside"):
        store.persist_compaction_learnings(sid, rid, source_digest=digest, content="proposal",
                                          sources=[{"message_id": store.history_records(other)[0]["id"]}])
    source = store.history_records(sid)[0]
    learning = store.persist_compaction_learnings(sid, rid, source_digest=digest, content="proposal",
                  sources=[{"message_id": source["id"], "quote": "ignore this untrusted duplicate text",
                            "api_key": "synthetic-not-to-store"}, {"seq": source["seq"]}])
    assert len(learning["sources"]) == 1
    assert learning["sources"][0]["source_digest"] == hashlib.sha256(source["message_json"].encode()).hexdigest()
    assert "quote" not in learning["sources"][0] and "api_key" not in learning["sources"][0]
    empty = store.create_session(tmp_path)["id"]
    run, _ = store.create_run(empty)
    with pytest.raises(ValueError, match="evidence"):
        store.persist_compaction_learnings(empty, run["id"], source_digest=digest, content="ungrounded")
    assert store.persist_compaction_learnings(sid, rid, source_digest=digest, content="NONE")["id"] == learning["id"]


@pytest.mark.parametrize("content", ["NONE", "", "<private_memory>omit</private_memory>"])
def test_no_new_learning_receipt_survives_restart_and_prevents_repeated_extraction(store, tmp_path, content):
    sid, rid = conversation(store, tmp_path)
    digest = compaction_source_digest(store.history(sid))
    assert not store.has_compaction_learnings_source(sid, digest)
    receipt = store.persist_compaction_learnings(sid, rid, source_digest=digest, content=content)
    assert receipt["created"] and receipt["no_new_learnings"] and receipt["content"] == ""
    assert store.has_compaction_learnings_source(sid, digest)
    assert store.compaction_learnings(sid) == [] and store.compact_learnings(sid) == ""
    store.close()
    with contextlib.closing(Store(store.path)) as reopened:
        assert reopened.has_compaction_learnings_source(sid, digest)
        duplicate = reopened.persist_compaction_learnings(sid, rid, source_digest=digest, content="NONE")
        assert not duplicate["created"] and duplicate["id"] == receipt["id"]
        assert reopened.compaction_learnings(sid) == []


def test_learning_projection_deduplicates_bullets_newest_source_wins_without_rewriting_rows(store, tmp_path):
    sid, rid = conversation(store, tmp_path, project="project-a")
    older = save_learning(store, sid, rid, "- Shared preference.\n- Old-only note.\n- Value was A.")
    run, _ = store.create_run(sid)
    store.append_message(sid, run["id"], Message("user", "A later correction is evidence for a new extraction."))
    newer = save_learning(store, sid, run["id"], "* Shared preference.\n- New-only note.\n- Value is B.")
    projection = store.compact_learnings(sid)
    assert projection.count("Shared preference.") == 1
    assert "* Shared preference." in projection
    assert projection.index(newer["run_id"]) < projection.index(older["run_id"])
    assert "Old-only note." in projection and "New-only note." in projection
    assert "Value was A." in projection and "Value is B." in projection
    assert store.compaction_learnings(sid)[1]["content"] == older["content"]


def test_learning_scope_does_not_leak_side_chats_branches_agents_or_projects(store, tmp_path):
    root, root_run = conversation(store, tmp_path, project="project-a")
    sibling, sibling_run = conversation(store, tmp_path, project="project-a")
    other_agent, _ = conversation(store, tmp_path, agent="research", project="project-a")
    other_project, _ = conversation(store, tmp_path, project="project-b")
    root_note = save_learning(store, root, root_run, "root project note")
    assert [note["id"] for note in store.compaction_learnings(sibling)] == [root_note["id"]]
    assert store.compact_learnings(other_agent) == store.compact_learnings(other_project) == ""
    children = []
    for kind in ("side_chat", "fork"):
        child = store.fork_session(root, kind=kind)["id"]
        run, _ = store.create_run(child)
        store.append_message(child, run["id"], Message("user", "private " + kind))
        store.append_message(child, run["id"], Message("assistant", "private reply"))
        store.finish_run(run["id"], "completed")
        note = save_learning(store, child, run["id"], "private " + kind + " note")
        assert note["scope"] == "session"
        assert [entry["id"] for entry in store.compaction_learnings(child)] == [note["id"]]
        children.append(child)
    root_after = save_learning(store, sibling, sibling_run, "post-branch project note")
    assert root_after["id"] not in {item["id"] for item in store.compaction_learnings(children[0])}
    assert all(not item["content"].startswith("private") for item in store.compaction_learnings(root))
    # A fork of a side chat remains private and cannot see the side-chat notes.
    nested = store.fork_session(children[0])["id"]
    assert store.compact_learnings(nested) == ""


def test_projectless_notes_stay_local_and_deletion_cleans_notes_snapshots_preserves_branches(store, tmp_path):
    parent, rid = conversation(store, tmp_path)
    unrelated, _ = conversation(store, tmp_path)
    parent_note = save_learning(store, parent, rid, "private root note")
    store.save_compaction_snapshot(parent, rid, store.history(parent), phase="manual")
    assert parent_note["scope"] == "session" and store.compact_learnings(unrelated) == ""
    child = store.fork_session(parent)["id"]
    child_run, _ = store.create_run(child)
    store.append_message(child, child_run["id"], Message("user", "child-only evidence"))
    store.append_message(child, child_run["id"], Message("assistant", "child answer"))
    store.finish_run(child_run["id"], "completed")
    child_note = save_learning(store, child, child_run["id"], "private child note")
    child_snapshot = store.save_compaction_snapshot(child, child_run["id"], store.history(child), phase="preflight")
    child_raw = store.history(child)
    store.delete_session(parent)
    for table in ("compaction_snapshots", "compaction_learnings"):
        assert not store.connection.execute(f"SELECT 1 FROM {table} WHERE session_id=?", (parent,)).fetchone()
    assert store.get_compaction_snapshot(child, child_snapshot["id"])
    assert store.compaction_learnings(child)[0]["id"] == child_note["id"]
    assert store.history(child) == child_raw
    assert store.lineage(child)["parent_id"] is None
    store.delete_session(child)
    assert store.connection.execute("SELECT COUNT(*) FROM compaction_learnings").fetchone()[0] == 0


def test_additive_tables_upgrade_existing_database_without_replacing_history(store, tmp_path):
    sid, rid = conversation(store, tmp_path)
    before = store.history(sid)
    store.checkpoint(sid, rid, [Message("user", "old checkpoint")])
    store.connection.execute("DROP TABLE compaction_snapshots")
    store.connection.execute("DROP TABLE compaction_learnings")
    store.close()
    with contextlib.closing(Store(store.path)) as upgraded:
        assert upgraded.history(sid) == before
        assert upgraded.active_history(sid)[0].content == "old checkpoint"
        assert upgraded.compaction_snapshots(sid) == []
        assert upgraded.compaction_learnings(sid) == []
