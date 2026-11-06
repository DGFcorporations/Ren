# Live integration status: UNVERIFIED

G1 is an offline controller/fixture implementation, not a live voice assistant.
There is no live-mode CLI flag and no `.env`/provider-key loader. The status endpoint
and `workbench-pilot status` enumerate the same disabled capabilities.

| Area | G1 implementation | Still needed after separate approval |
|---|---|---|
| Voice input | Three authored transcript fixtures | Real microphone transport and actual STT adapter/permissions/tests |
| Voice output | Deterministic 250-ms PCM tone | Actual TTS adapter, chosen voice, cancellation/synchronization and acoustic tests |
| Planning | Deterministic fixture-to-action composition | Model adapter and full streamed tool-call/usage/cancellation qualification |
| Memory | Real SQLite exact/keyword recall, revisions and tombstones | Mem0/embedding adapter, ingestion approval, semantic staleness/deletion tests |
| Browser | Real sandboxed Chromium on one owned loopback fixture | Explicitly authorized target sites/accounts and site-specific safety/receipt strategy |
| Persistence | Local run/action/fact ledger and safe-stop recovery | Production backup, authentication and any separately approved unattended resumption |

`live-candidates.in` records the proposed future SDK versions from the design.
`live-candidates.lock` successfully resolves their package metadata for Python 3.12,
with artifact hashes. This is **dependency-resolution evidence only**; the live
candidate packages were not installed/imported into the G1 app environment and no
provider protocol or account access was tested. The frontend Pipecat client pair
remains a documented candidate, not an installed/bundled G1 client.

G1 instead uses a small local HTML/JS interface with no external CDN. It is important
not to describe its test-tone playback as natural speech or its fixture transcript
selection as speech recognition.

Future adapters must go through the same central action ledger, never directly
execute provider-generated instructions, and keep the real-account/paid-call gate
closed until expressly approved. The current owned-fixture atomic save guard cannot
be assumed to exist on a third-party chat application.
