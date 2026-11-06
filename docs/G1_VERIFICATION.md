# G1 verification record

**Date:** 2026-11-06. **Scope:** the source in this branch and its containing commit.
**Disposition:** offline implementation/contract testing, not a live-integration release.

## Authorization and repository state

The owner approved G1 only, then explicitly selected `DGFcorporations/Ren`.
Ren was empty. The pilot branch was initialized directly with commit
`17b5d31a1b8425e60a69458a9bcf8fb8b663d400`; no existing default-branch commit was
replaced. All development is on `pilot/voice-memory-browser`; no merge to `main`
is part of this work. The prior workbench-agent repository is not the destination.

## What was executed

- **181 test functions/cases passed, plus 143 unittest subtests.** Zero failures,
  errors or skips in the final full run; elapsed time approximately 120.4 seconds.
- The JUnit suite's aggregate count is 324 because it includes the 143 subtests.
  Do not describe that as 324 independent end-to-end workflows.
- **51 tests are marked `browser`** and exercised real local Chromium behavior.
  The other 130 cover contracts, SQLite, API controls and related behavior.
- Four browser-UI tests drive the actual interface through its HTTP API to a
  second managed Chromium context and verify observed results.
- Twelve nominal fixture combinations exercise three authored requests across
  textarea, contenteditable, iframe and delayed composers.
- SQLite testing includes concurrent threads/connections and abrupt subprocess
  exits, not just clean in-memory shutdown.
- Ruff lint and format checks passed. Both JavaScript bundles passed syntax checks.
- The wheel built successfully and contains all seven required HTML/JS/CSS assets.
- Offline dependencies were synchronized from `requirements-g1.lock` with hashes.
- Future live candidate metadata resolved into `docs/live-candidates.lock`.
  Those candidate SDKs were **not installed or exercised** by the G1 runtime.

### Test groups

| Group | Cases | Scope |
|---|---:|---|
| adapters | 12 | Scripted transcript/tone boundaries; live integrations fail closed |
| application | 28 | Real loopback API and managed-browser workflows, approvals, session and cancellation checks |
| browser | 72 | 49 contract/policy cases and 23 real browser cases |
| hardening regressions | 5 | Rejected-context cleanup, session eviction/expiry and approved tombstone recreation |
| ledger | 60 + 143 subtests | Durable state, concurrency, corruption, provenance, deletion and crash behavior |
| user interface | 4 | Real browser UI through controller and independent DraftBox receipt |

The 51 browser-marked cases include 22 application cases, 23 browser cases, two
hardening cases and four UI cases. Skips are not treated as integration passes.

## Actual test environment

- Python 3.12.16; SQLite 3.53.1; Linux x86_64, Amazon Linux 2023.
- Playwright 1.64.1; Chrome for Testing 156.0.8078.4, revision 1248.
- Chromium sandbox explicitly **enabled**; no insecure launch retry or personal
  browser-profile fallback was used.
- Playwright warns that this host OS is not officially supported and downloads
  its Ubuntu 24.04 fallback build. The local test result is real, but does not
  establish vendor support for this OS or compatibility with the user's devices.
- The optional manual workflow targets Ubuntu 22.04/Python 3.12.16. It is retained
  in the downloadable source bundle but deliberately omitted from this repository
  upload, as authorized by the owner after the original batch write was denied.
  It was not dispatched; no hosted CI run or CI success is claimed. All verification
  reported here was performed locally.
- Phone-sized viewport rendering and horizontal overflow were checked in Chromium;
  this is not a real-phone, Safari, microphone or mobile-background test.
- Unicode content round-tripped exactly. Visual glyph coverage depends on the
  host's installed fonts and is not a language-quality certification.

## Defects found and fixed before commit

| Finding | Fix | Regression evidence |
|---|---|---|
| Wrong/expired approval decisions left browser contexts open; the sixth run could hit the five-context cap | Terminal decision errors release the context and active slot | More than five consecutive rejected/expired browser runs complete without exhausting contexts |
| Evicted or abandoned sessions could strand the global active task | Session eviction cancels pending work; new work first expires unused old approvals | Evict 32 sessions and test lost-session expiry followed by a fresh request |
| Explicit recreation of a deleted fact compared against revision zero | Owner-scoped tombstone metadata participates in the exact revision binding/CAS | Delete, reject bootstrap resurrection, explicitly approve recreation and verify the next revision |
| A target could change during the click event after an earlier snapshot check | Owned DraftBox validates the exact target/epoch/text atomically in its one-use save handler | Inject pointerdown recipient and content changes; saved count remains zero |

The last fix is an owned-fixture transaction boundary, **not a universal guarantee
for third-party websites**. A real site may autosave or lack an equivalent atomic
operation; that requires a separate authorized design and qualification.

Other verified failure cases include changed page/recipient/fact revision,
denied/expired/replayed approvals, duplicate request IDs, closed pages, worker
restart, post-save receipt loss, wrong local session, body-size limits, forbidden
network targets and disabled provider endpoints.

## Local retrieval microbenchmark

Separate synthetic benchmark: 1,000 SQLite facts; 1,000 exact lookups and 100
keyword queries using FTS5, all in one warm local process.

| Operation | p50 | p95 |
|---|---:|---:|
| Exact key lookup | 0.0133 ms | 0.0141 ms |
| Keyword lookup | 0.1076 ms | 0.1259 ms |

These timings exclude speech, embeddings, network, semantic retrieval and model
answer generation. They are **not Mem0 performance**, a production SLA, a cold-start
measurement, or evidence of natural-language recall accuracy. All expected test
keys were found; arbitrary questions are not handled by this deterministic pilot.

## Explicitly unverified / not implemented

- Live STT, live TTS, real microphone access and acoustic voice interaction.
- Pipecat/WebRTC client-server sessions, speech interruption quality and provider latency.
- LLM planning/tool calls and provider token/billing behavior.
- Mem0/Qdrant embeddings and semantic-memory ingestion/retrieval.
- Real browser accounts, arbitrary sites, desktop/phone app control or external sends.
- SMS/iMessage, other-agent connectors, hosting and scheduled unattended operation.
- Production authentication, multi-user isolation, encrypted backups or a hostile-host threat model.

Scripted transcripts and a synthetic PCM tone are labeled as such in the UI,
API status and README. No OpenAI, speech-provider or managed-memory API calls were
made by the prototype/tests, and no real messages were sent.

## Release interpretation

This work establishes G1 behavior in the stated local fixture environment. It does
not meet the design's live supervised-alpha or 300-trial qualified-pilot gates,
and its test count must not be converted into a field failure-rate guarantee.

G2 needs a separate owner approval, provider/data/privacy choices, secure setup,
integration implementation, account access and a test budget. Merely supplying a
key cannot enable a live mode in this code.
