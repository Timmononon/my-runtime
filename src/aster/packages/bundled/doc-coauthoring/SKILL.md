---
name: doc-coauthoring
description: Coauthor a source-grounded document through context gathering, structured drafting and reader checks.
license: Apache-2.0; see LICENSE.txt and NOTICE.md
---

# Document coauthoring

Use for a proposal, design note, decision record or substantial explanatory document. This is a writing workflow, not an Office layout engine. Deliver Markdown unless the user requested another supported format.

## Gather context

Identify the audience, the decision or action the document supports, the required format, length and deadline. Read supplied files with `file_read` or `document_extract`; record exact paths and page/paragraph coordinates in `templates/document.md`. Separate established facts, proposals and unresolved questions. Ask only questions that block meaningful progress with `ask_user_question`; use explicit assumptions for minor choices. Never manufacture a citation or business result.

## Structure and iterate

Propose the few sections needed to serve the reader. Draft the section with the greatest uncertainty first, then the body, introduction and summary. Each section should have a claim, supporting evidence and practical implication. Offer alternative wording only where it changes meaning or a decision. Preserve the user's accepted wording and explicitly flag a contradictory source. Use `file_write` for a new document; use `file_read` followed by `file_edit(expected_hash=...)` for edits so concurrent changes cannot be silently overwritten.

## Reader check

Read `references/reader-check.md`. Check the complete draft for unexplained terms, missing prerequisites, unsupported claims and decision ambiguity. If no separate reader has actually reviewed it, call this a self-review; do not claim a blind-reader test. A separate agent/model review requires the Runtime's normal admission and budget. Repair the draft, verify citations against the actual sources and report remaining open questions. Run the optional `scripts/check_sources.py DOCUMENT` through `process_exec` to list source markers and unresolved TODOs; this is a mechanical check, not factual verification.
