# AIOS-inspired scoped service kernel acceptance

Core source: 88fa614fbd236482a682852f1206ac779e35fe61. Baseline: deployed v0.24 03bd90be6de6b98f2120e5105bc5ed600d363d40.
The reference AIOS commit and reviewed modules are in reference.json. The implementation is independent.

Linux full regression: 437 cases, 435 passed, two Node-dependent cases skipped and separately passed on Windows. Actual Chromium ran the kernel demo over authenticated synthetic HTTP with cgroup v2; six real Attempts, no page errors, no supplier calls.

The fixed replay uses the same 50 recorded official airline dialogues twice, with baseline/candidate/direct order then reverse order. Official tools, tasks, evaluator and native bridge are unchanged. All request hashes, dialogues and rewards must match. This is a framework-overhead replay, not a new model score or leaderboard rank. Optional durable checkpoint is disabled in this comparison. RSS values are per-process inherited high-water marks and do not prove aggregate memory savings.

Reproduce from a delegated private-network unit:
`python tools/compare_runtime_replay.py --baseline BASELINE --candidate CANDIDATE --trajectory PRIVATE_FIXED_FIXTURE --output NEW_REPORT_DIR`

UI fixture screenshots are not required to run the source. Raw conversations, keys, checkpoint state and private run folders are not part of this public evidence. Setup failures remain recorded separately from passing regression.

Mean seconds: baseline 59.667072; candidate 58.596532; direct 38.830156.
