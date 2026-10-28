---
name: webapp-testing
description: Test a local web application with scoped browser tools and objective artifact checks.
license: Apache-2.0; see LICENSE.txt and NOTICE.md
---

# Web application testing for Aster

Adapted from the Apache-2.0 Anthropic/Kocoro workflow. Changes: Windows paths, Aster tools, durable approvals, no unmanaged browser/server processes.

1. Record the URL, desired behavior, test data and objective assertions in `references/test-plan.md` format. Separate observations from assumptions.
2. Discover `browser_open,browser_snapshot,browser_locate,browser_click,browser_fill,browser_screenshot,browser_download` through `tool_search`. Start with `browser_open(url=...)`; keep the returned page/context IDs.
3. Inspect the rendered page and use returned selectors/refs. Never invent an unseen selector or reuse a stale snapshot version after navigation. Capture a screenshot when visual behavior matters. Local deliverable previews are static by default: JavaScript will not run. To test a local artifact's JavaScript, explicitly request `present_deliverable(path=..., interactive=true)` and follow the normal tool approval flow; never approve that request on the user's behalf. Use its returned preview URL. Interactive mode does not guarantee complete network isolation, including WebRTC/UDP.
4. Let normal Runtime approvals govern mutating browser calls. Download into registered artifacts; verify bytes/content before claiming success. An interrupted mutation is an unknown outcome, not permission to repeat it.
5. Use `scripts/check_download.py --help` through `process_exec` for an independent size/SHA256 assertion. The skill result exposes `skill_directory`; pass the explicit script path, never guess a Unix /tmp path.
6. Report each expected behavior as pass/fail/unsupported with the evidence path. Close the owned browser context when finished. Do not launch background servers with raw detached Shell commands; use an already running fixture or a supervised process.

The script checks files only. It does not judge a screenshot or substitute for browser interaction.
