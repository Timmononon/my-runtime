---
name: mcp-builder
description: Build and verify a Python MCP integration with focused schemas, bounded calls and explicit side effects.
license: Apache-2.0; see LICENSE.txt and NOTICE.md
---

# MCP integration workflow for Aster

Adapted from the Apache-2.0 Anthropic/Kocoro MCP builder. Changes: Python SDK examples, Aster managed MCP lifecycle, explicit credential references.

1. Define the minimum real workflow and source API contract before implementation. Specify read/write/execute risk, pagination, timeouts, idempotency and the evidence needed to confirm an effect.
2. Use descriptive prefixed tool names and strict JSON schemas. Return concise data plus source IDs and actionable errors; preserve full artifacts outside the prompt when needed.
3. Start from `templates/server.py`. It exposes a deterministic read-only calculation as a runnable transport example, not a production business integration. Replace it with verified API calls; never mark writes readOnly/idempotent merely to bypass approval.
4. Configure the server through Aster's `mcp_servers`: explicit executable/argv/cwd, bounded timeout and `env:`/`keyring:` references. Do not embed credentials in a skill or server template.
5. Test initialize/list/call/close locally, then a timeout and post-write disconnect. A disconnected material call must be unknown and must not be replayed. Confirm child process cleanup on Windows.
6. Create fixture-backed evaluation cases from `references/evaluation.md`. Distinguish a working transport from correct real service behavior. Describe unsupported features explicitly.
