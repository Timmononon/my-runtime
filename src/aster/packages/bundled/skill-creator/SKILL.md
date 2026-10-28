---
name: skill-creator
description: Create an Aster skill with clear trigger, source references, limited tools and measurable acceptance.
license: Apache-2.0; see LICENSE.txt and NOTICE.md
---

# Skill authoring and improvement for Aster

Adapted from the Apache-2.0 Anthropic/Kocoro skill-creator workflow. Changes: Aster skill schema, explicit package digests and evaluation boundaries; no Claude-specific CLI.

1. Define the user's repeated task, trigger, expected deliverable and constraints. Inspect available Aster tools before naming them.
2. Draft SKILL.md using `templates/SKILL.template.md`; put long references in `references/` and executable helpers in `scripts/`. Keep paths relative to the skill directory.
3. Set allowed-tools to the workflow's real needs. `tool_search` and `use_skill` remain infrastructure; other calls stay subject to Runtime policy. Secrets must be env:/keyring: references, scoped to an explicit activated skill invocation.
4. Write representative success, incomplete-input and failure cases using `references/evaluation-template.json`. Define output/file/effect assertions before executing the skill.
5. Run local helper and protocol tests without a paid model. This proves implementation contracts only. Real model comparisons require an independently authorized request budget and should record model/config/dataset digests, usage and failures.
6. Preview the package, review file changes and its digest, then install that exact digest. Re-preview after any edit. Keep license/NOTICE and never bundle secrets, symlinks or the excluded proprietary Office skill packages.
7. Improve based on actual failed cases; do not fill an oracle's expected answer into a scripted model and report it as model capability.
