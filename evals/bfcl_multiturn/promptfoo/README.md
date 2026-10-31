# promptfoo: Aster BFCL V4 Multi-Turn saved-result adapter

Pinned package: **promptfoo 0.125.1**, full npm transitive lock in
`package-lock.json`. Verified host: Node **24.18.0**, npm **11.16.0**;
promptfoo requires Node `>=22.22.0`. Installation stays under
`.eval-tools/promptfoo`, separate from frontend dependencies and Aster's Python
environment. Python providers use the existing project `.venv` and the new
stdlib-only Aster saved-result export module; they add no Python dependencies.

From the repository root, reproduce the dependency installation with:

```powershell
& 'evals/bfcl_multiturn/promptfoo/install.ps1'
```

The installer uses `npm ci`, the committed lock, a project-local npm cache,
disabled lifecycle scripts, and no global installation. The Python-provider
offline workflow does not need a browser, native model backend, model weights,
TypeScript transforms or an esbuild lifecycle download.

Run from `D:\Aster` with a **new** output directory:

```powershell
& '.\.venv\Scripts\python.exe' 'evals/bfcl_multiturn/promptfoo/run_offline.py' `
  --bundle 'artifacts/bfcl/multiturn-frameworks-20261029/bundle' `
  --output 'artifacts/bfcl/multiturn-frameworks-20261029/promptfoo-new'
```

`--generate-only` writes the native config and comparison projection without
starting promptfoo. Repeating `--bundle PATH` compares genuinely saved configs,
whose labels come from their records. A label cannot merge different recorded
source/config/model identities. One native test is one **complete task**, not
one user message or one tool call. Configs pair only matching task IDs,
question hashes, subcategories and scoring revisions/rules. Missing counterparts
and unscoreable data-conflict/infrastructure/not-run records remain explicitly
listed and are excluded from the paired score denominator. Unit-test alternate
labels are fixtures, not an actual optimization experiment.

The provider does not regenerate the recorded conversation. Aster's existing
`bfcl_v4_*` chain owns the complete multi-turn state, execution and budgets;
this provider reads a hash-verified export of that chain's saved results. Its
custom Python assertion binds the output to the configured record digest and
preserves the saved BFCL pass/fail. It does not create a replacement agent loop.
Live execution is intentionally absent from this adapter.

Files in a run directory:

- `promptfooconfig.json`, `assertion.py`: native generated configuration and
  the exact assertion copy (needed because pinned promptfoo parses a colon in
  an absolute Windows assertion path as a function suffix).
- `results.json`: native promptfoo report. Its provider token usage is **0**.
  `numRequests` counts local Python-provider invocations, not model requests;
  native `latencyMs` is replay overhead, not recorded Agent duration.
- `comparison.json`: per-label historical success rate, requests, token counts,
  elapsed time with unknown counts, reasons, paired/unpaired/excluded IDs, and
  unredacted source/config/model identities from the sanitized unified bundle.
  Native promptfoo export may redact long hex identity strings; the unified
  bundle and this projection remain authoritative for those identities.
- `run-receipt.json`: child command, native exit code, consistency checks,
  guard receipts and new-model/new-judge counts (both 0).

The runner strips inherited credentials, provides an empty env file and an
isolated cwd/home/cache/config directory. Sharing, telemetry, update checks and
OTEL are disabled. Python children deny network and subprocess creation.
The Node preload denies TCP/TLS/DNS/UDP/fetch, allowing only tsx's exact parent
named pipe in this run's dedicated temporary directory. Pinned promptfoo still
tries to send a one-time "telemetry disabled" event: the preload marks that
event as already recorded through its process singleton before CLI callbacks.
Dependency files are not modified. This workaround is version-checked; any
other network attempt is blocked, counted, and makes the launcher fail.

Expected saved task failures do not mean integration failure. Native promptfoo
returns 100 for such failures. The wrapper succeeds only if the full expected
task/configuration matrix is present, every pass/fail matches the saved BFCL
score, the custom assertion completed, framework errors and new tokens are 0,
and both Node and provider/assertion Python guards are present and clean.

Current evidence contains one actual as-run configuration and four official
Multi-Turn tasks (2 saved passes, 2 saved failures). It contains no implemented
optimization variant, new model capability measurement, model/judge request,
weight download or uploaded transcript. Early debugging runs remain preserved;
their guards blocked attempted telemetry before transport. Final successful
runs must have zero blocked external attempts.
