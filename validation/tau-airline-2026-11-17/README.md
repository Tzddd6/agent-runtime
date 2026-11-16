# Airline paired evaluation, 2026-11-17

Official tau2-bench v1.0.1 source: fc0055dc4e0a316c3f83133267fbd6faaa770992.
Frozen evaluated Runtime source: cf4a11a0ed65d95b83b86971d1924093b007d554. This report commit adds export tools and budget input hardening AFTER the evaluation; it did not change the evaluated task executions.

50/50 official base tasks in each arm, one trial each. All outcomes retained. Both agent and user simulator used DeepSeek Flash, non-thinking, temperature 0, max_tokens 2048. Custom submission; no default-simulator or pass^2/3/4 claim. Only Agent inference is a Runtime Attempt; official tools execute in the coordinator. Every task verified 1,155 official source/data hashes before running. The pilot is excluded from scores.

| Measure | Runtime | Direct |
|---|---:|---:|
| Official successful tasks | 39/50 | 41/50 |
| Agent calls | 432 | 441 |
| Agent input tokens | 2978365 | 3355122 |
| Agent output tokens | 59792 | 64695 |
| Agent conservative peak CNY | 0.936780 | 1.049382 |
| Median task execution seconds | 24.050 | 20.290 |
| Coordinator CPU seconds | 223.649 | 212.718 |
| Children CPU seconds | 29.152 | 0.050 |

Single-trial results with potentially different sampled dialogue/tool paths do not establish framework superiority. Two-sided exact McNemar p=0.6875. RSS is a per-process-category maximum, not the aggregate system memory peak. CPU/RSS exclude external inference and the common protected model broker. Coordinator CPU and execution timing exclude fresh coordinator imports; no full-startup latency claim. Lower observed token usage is not evidence of framework compression because dialogue paths differed. This run did not demonstrate the target of lower latency with non-decreasing quality.

`paired-analysis.json` reports explicit conservative CNY at official peak list rates. Official trajectory `cost` fields are normalized to official peak USD list rates (all USD rates are 0.15 times their corresponding CNY list rates), NOT currency conversion or the actual off-peak invoice. All non-cost trajectory fields retain an identical SHA256. Costs of both agent and simulator are included separately in analysis.

Only synthetic official task data and reviewed trajectories are published. Attempt payloads, control tokens, private config, private contact email and supplier keys are excluded. GitHub contact: @Tzddd6.

Local official task/trial/format/metrics validation passed. Official maintainer review has NOT been submitted; no official leaderboard rank exists. One domain and one trial limit reliability and comparability. `submission.json` plus the trajectory file named in it are the candidate review materials; `direct-results.json` is a separate control and must not be placed in the candidate's trajectories folder.

Reproduction: install the pinned official source into its Python 3.12 environment, provide protected broker access without placing keys in source/CLI, run `tools/benchmark_tau_paired.py --official-root /path/to/official --domain airline --output /private/run --budget-ledger /private/budget.json --cny-limit 10`; use a delegated non-root service cgroup. See `docs/tau-bench评测协议.md` for the private launcher contract and the unchanged official environment/grader. One shared ledger covers pilot and both arms; errors are not automatically replayed.
