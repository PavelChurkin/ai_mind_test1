# Issue #21 / PR #22 checklist

Objective: implement every bot9 requirement in one PR, preserving bot8 behavior.

## Research
- [x] Read the full issue; there are no comments, sub-issues, PR comments or reviews.
- [x] Read bot8, its RAG, tests, server, deployment and merged PR #20.
- [x] Check ProxyAPI embeddings in its documentation: https://proxyapi.ru/docs/openai-embeddings

## Requirements and evidence
- [x] Standalone `Прочее/bot9` based on bot8; old folders unchanged (`git diff main -- Прочее/bot8` is empty).
- [x] `rag.py` kept (one header comment changed); `rag2.py`, `rag2_index.db`, folder `RAG2` added.
- [x] Fragments go to the LLM for 3-4 sentence summaries with entities, facts and logical link: `TestSummaryPrompt`.
- [x] Vectors come only from summaries, each tied to its original: `TestSummaryRetrieval.test_vectors_come_only_from_summaries`, `test_vector_is_linked_to_its_fragment`.
- [x] Question → vector → summary vectors → original fragment in context, summary hidden: `test_search_compares_with_summaries_but_returns_original_fragment`, `TestRag2Mode`, `smoke_bot9_rag2_main.py`.
- [x] `/раг2 вкл` searches index 2: `test_rag2_mode_searches_index2_by_summary_and_gives_original_fragment`.
- [x] `/раг вкл` unchanged, with the same message and index 1 only: `test_rag_mode_still_searches_only_index1`, inherited `test_rag_bot9.py`, `test_bot9*.py`.
- [x] rag2 keeps rag functions (off, one-off, status, sources, fallback, sync, chunk limit): `TestRag2OneOff`, `TestRag2Mode`, `TestIncrementalBuild`.
- [x] Group name is a variable in `.env` with value `грань`: `TestDefaultGroupName`, `TestCustomGroupName`, `TestEnvFile`, `TestNameIsNotHardcoded`, Ansible `env.j2`.

## Assumptions
- Hybrid search: dense over summaries plus BM25 over originals, so exact quotes and an unfinished index still work.
- `/раг вкл` and `/раг2 вкл` replace each other in a chat.
- Books for `/раг2` go into `RAG2` separately; only reflection is mirrored.
- Summaries are built by a background thread and do not use the per-chat daily limit.

## Delivery
- [x] Automated checks: `experiments/check_bot9.py` and `.github/workflows/bot9.yml`.
- [ ] Not verified, requires live services: real VK community, paid LLM, ProxyAPI embeddings, summary quality, `RAG2_DENSE_MIN_SIM`, real cost and time.
