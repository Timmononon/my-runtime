---
name: web-artifacts-builder
description: Build a standalone interactive HTML artifact with local assets and objective browser checks.
license: Apache-2.0; see LICENSE.txt and NOTICE.md
---

# Web artifacts builder

Adapted from the upstream artifact workflow for a Windows/Python Runtime. The supported deliverable here is an independent HTML/CSS/JavaScript artifact. This package has no React/Node bundler, hosting service or backend.

1. Define the artifact's users, input, displayed state and observable interactions. Use `frontend-design` for composition or `theme-factory` for palettes when appropriate. Avoid external dependencies when the artifact can be self-contained.
2. Read `templates/interactive.html`. It demonstrates accessible filtering with real DOM state and an honest empty state. Copy/adapt it with file tools into an authorized workspace, preserving the user's existing work. Keep any local relative assets beside the output. Never embed secrets or private source text just because a page can run locally.
3. Use semantic controls, explicit labels and keyboard focus. Display calculations from actual inputs, validate edge cases, and distinguish sample data from real data. Do not claim persistence when state disappears on refresh.
4. Run `scripts/check_artifact.py PATH` through `process_exec`. The script checks local dependencies and basic document structure; it refuses remote asset references, missing relative assets and local paths that escape the artifact directory. It is not a JavaScript sandbox or a full accessibility audit.
5. Use `present_deliverable` for the Runtime's scoped local preview. The default preview is static and does not execute JavaScript. To verify JavaScript behavior, explicitly request `present_deliverable(path=..., interactive=true)` and let the normal tool approval flow decide; never automatically approve the request. Interactive mode does not guarantee complete network isolation, including WebRTC/UDP. After approval, use its returned preview URL with `browser_snapshot`, click/fill interactions and screenshots. Test at least default, changed and empty/error states. Do not claim interaction testing passed in static mode or replace objective browser assertions with a screenshot-only claim.
6. Deliver the output path, actual verified behavior and limitations. External publication or transmission requires a separate authorized step. If the user requires React/build tooling, inspect the existing project and state that this skill's standalone path does not supply it.
