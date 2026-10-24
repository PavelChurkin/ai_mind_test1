# Issue #19 / PR #20 checklist

Objective: implement every bot8 requirement in one PR, preserving bot7 behavior.

## Research
- [x] Read full issue, comments and sub-issues (none), PR conversation, inline comments and reviews (none).
- [x] Read bot7, its tests, server, deployment and recent merged PR #18.
- [x] Verify branch and upstream ancestry; no contributing/AGENTS instructions, release version or existing CI.

## Implementation and verification
- [x] Copy bot7 into a standalone bot8 folder, including RAG, philosopher, data, server and deployment.
- [x] First reproduce missing behavior with mocked VK/LLM tests in experiments.
- [x] Two forwarded messages from this chat select an inclusive time interval containing ALL messages; only the next addressed conversational request uses it. No LLM call on selection. Restore normal recent context afterward, including queued requests and errors.
- [x] Context and attention commands raise character budget only when necessary; expire after 60 seconds, preserving previously configured base; no indefinite auto-renewal.
- [x] Append reflection by default; explicit “запомни с нуля” replaces, including forwarding/replies and split messages. Reindex and retain backup/admin checks.
- [x] Attention forwards persist per chat, supplement recent history and request, list dates via a system response, and clear with /внимание сброс.
- [x] Terminal supports ordinary commands, peer selection and privileged administration; replies and delayed notices stay in terminal. Keep chat management and stop behavior. Stdin reader queues work for main thread (SQLite/thread safety).
- [x] Preserve prior bot features and migration, document exact command semantics and assumptions in Russian, add RAG ideas in same folder.
- [x] Add automated CI; run local inherited/new tests, smoke main/server, lint, syntax and markdown checks. Keep large logs outside tracked code; bound test/CI runtime.

## Delivery
- [ ] Commit useful atomic steps and push only issue-19-305883210651.
- [ ] Review full PR diff; ensure no old features removed, main is included, worktree clean.
- [ ] Update PR title/body with reproduction, requirement/test mapping, limitations and Fixes PavelChurkin/ai_mind_test1#19.
- [ ] Check fresh CI runs by SHA/time; preserve failed logs in ci-logs and investigate exact errors if any.
- [ ] Mark PR #20 ready once all checks pass; leave unmerged and report URL.

## Evidence
- Reproductions: `test_bot8_new.py` initially failed (9 failures, 5 errors). Additional failing tests reproduced split system responses entering history and an interval question joining an older long request; both now pass.
- Interval: `TestInterval` covers reversed/inclusive bounds, same-second messages, >500 rows, missing local history with three VK pages, API failure preserving selection, queued questions, long-message boundary, failed answers and manual/auto context restoration.
- Attention and characters: `TestAttention` and `TestTemporaryCharacters` cover dates/system output, independent chats, reopened SQLite, duplicates, images, RAG fallback, exact 60-second expiry, custom base and deadlines that reads/answers cannot extend.
- Reflection: `TestReflection` plus inherited `TestRemember` cover append/reset with inline, forwarded and replied text, admin rejection, backup, reindex, empty reset and split text.
- Terminal: `TestTerminal` plus `smoke_bot8_main.py` cover stdin/main-thread dispatch, peer selection, operator privileges, commands after pauses, token-expiry notices and no VK/history pollution. Inherited stop/server tests remain enabled.
- Local final run (Python 3.12): `experiments/check_bot8.py` passed 31 checks, 627 unittest cases, five main-loop smoke scripts, structure/link checks, focused lint, Python compilation and Ansible syntax. Full logs: `/tmp/issue19-logs/final/`.
- A pre-existing structural test incorrectly looked for module-level `main()` inside class source. It now checks `inspect.iscoroutinefunction(ai_mind3.main)`; the application is unchanged. Ansible cannot import under the container's default Python 3.15, so the full run uses installed Python 3.12; CI covers 3.11 and 3.13.
- Live VK/paid LLM calls and remote systemd deployment were not exercised. Tests use realistic API mocks; this is stated in the README and PR.
