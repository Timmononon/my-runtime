# Pinned BFCL V4 components

These components originate from [ShishirPatil/gorilla](https://github.com/ShishirPatil/gorilla),
revision `6ea57973c7a6097fd7c5915698c54c17c5b1b6c8`, under the accompanying Apache-2.0 LICENSE.
`SOURCE_MANIFEST.json` records every original and adapted SHA256. `_sources/` retains
the corresponding original bytes for review and differential checks.

The score checker bodies and simulator bodies are preserved. Imports are redirected
to this private package; Ruff is suppressed for unchanged upstream code. The
multiturn checker contains only pure state/response/irrelevance helper functions.
The upstream `eval` executor, process-global simulator cache, provider registries,
model handlers and web API are deliberately not included. Vector-memory method
bodies are included with the eager model import/construction replaced as below.

The AST checker receives original function names after Aster reverses wire aliases;
its provider-name registry is replaced by one static entry that does not rewrite
those restored names. Java/JavaScript type converters consume already-decoded
language literals and do not parse or execute arbitrary Java/JavaScript programs.

Memory's `overrides.final` import is replaced with the standard-library `typing.final`
marker. A minimal memory-path module replaces the global BFCL utils import. It
returns the same relative memory backend grouping for accepted IDs and rejects path
syntax. The caller must provide a validated isolated `model_result_dir` and scenario;
these values are never taken from model tool arguments.

The isolated Aster harness enforces allowed methods, JSON arguments, cancellation,
policy, budgets and temporary state before any simulator method is called. The
filesystem simulator's subprocess method must not be admitted. Math precision must
be restored by the caller. KV memory additionally requires the exact BM25Plus
dependency; no keyword-search replacement is supplied.

Vector memory retains the original `MemoryAPI_vector` and `VectorStore` bodies.
The eager SentenceTransformer import and global model construction are removed;
`ENCODER` is a ContextVar-backed proxy and `ENCODER_DIM` is fixed to the official
encoder's 384 dimensions. The trusted caller must explicitly admit an offline
adapter for `sentence-transformers/all-MiniLM-L6-v2` before construction or method
execution. No encoder is downloaded, loaded or called during admission. Proxy
calls preserve `normalize_embeddings=True` and validate finite output shape
before FAISS. The optional CPU FAISS dependency is loaded only after admission.
Fixture encoders may verify this protocol, but are not genuine model inference
or evidence of retrieval quality. The optional `bfcl-vector` dependency group
provides FAISS only and is used alongside `bfcl`; it installs no encoder weights.

The pinned agentic checker differs from prose in the release blog: it normalizes
selected punctuation/case and searches for an accepted answer with word boundaries
in the final non-tool assistant message. It does not extract an `answer` field or
require whole-message exact matching. This package preserves that source behavior.

The pinned full `eval_runner.py` imports `multi_turn_irrelevance_checker` but never
invokes it; its main checker skips state/response grading on empty-gold turns.
Aster deliberately calls that unchanged official irrelevance subchecker for those
turns, implementing the documented missing-function/missing-parameter rule. This
is explicit harness wiring that fixes an upstream omission, not byte-for-byte
execution of the unchanged full upstream runner. No legacy bypass is supplied.
