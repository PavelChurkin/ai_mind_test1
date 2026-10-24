# Issue #19 / PR #20 checklist

Objective: implement every bot8 requirement in one PR, preserving bot7 behavior.

## Research
- [x] Read full issue, comments and sub-issues (none), PR conversation, inline comments and reviews (none).
- [x] Read bot7, its tests, server, deployment and recent merged PR #18.
- [x] Verify branch and upstream ancestry; no contributing/AGENTS instructions, release version or existing CI.

## Implementation and verification
- [ ] Copy bot7 into a standalone bot8 folder, including RAG, philosopher, data, server and deployment.
- [ ] First reproduce missing behavior with mocked VK/LLM tests in experiments.
- [ ] Two forwarded messages from this chat select an inclusive time interval containing ALL messages; only the next addressed conversational request uses it. No LLM call on selection. Restore normal recent context afterward, including queued requests and errors.
- [ ] Context and attention commands raise character budget only when necessary; expire after 60 seconds, preserving previously configured base; no indefinite auto-renewal.
- [ ] Append reflection by default; explicit “запомни с нуля” replaces, including forwarding/replies and split messages. Reindex and retain backup/admin checks.
- [ ] Attention forwards persist per chat, supplement recent history and request, list dates via a system response, and clear with /внимание сброс.
- [ ] Terminal supports ordinary commands, peer selection and privileged administration; replies and delayed notices stay in terminal. Keep chat management and stop behavior. Stdin reader queues work for main thread (SQLite/thread safety).
- [ ] Preserve prior bot features and migration, document exact command semantics and assumptions in Russian, add RAG ideas in same folder.
- [ ] Add automated CI; run local inherited/new tests, smoke main/server, lint, syntax and markdown checks. Keep large logs outside tracked code; bound test/CI runtime.

## Delivery
- [ ] Commit useful atomic steps and push only issue-19-305883210651.
- [ ] Review full PR diff; ensure no old features removed, main is included, worktree clean.
- [ ] Update PR title/body with reproduction, requirement/test mapping, limitations and Fixes PavelChurkin/ai_mind_test1#19.
- [ ] Check fresh CI runs by SHA/time; preserve failed logs in ci-logs and investigate exact errors if any.
- [ ] Mark PR #20 ready once all checks pass; leave unmerged and report URL.
