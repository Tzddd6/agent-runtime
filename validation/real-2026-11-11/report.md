# Real workload experiment results

Live provider calls, fixed-result replay and synthetic fixtures are separate groups. Missing measurements are shown as —, not zero. Token-derived costs are estimates; actual vendor billing is unknown.

| Kind | Scenario | Workers | Policy | Cache | n | Completed | Wall median (s) | Ready wait mean median (s) | CPU median (s) | Workflow peak RAM median (bytes) | Estimated API cost |
|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| live | empty-content-diagnostic | 2 | resource_fit | lru2 | 4 | 0/4 | 8.95408 | 0.0122158 | 0.188179 | 1.66154e+07 | 0.0418469 |
| live | empty-content-diagnostic | 1 | aging_fit | lru2 | 2 | 0/2 | 11.0537 | 0.599034 | 0.178537 | 7.33594e+06 | 0.0243669 |
| live | empty-content-diagnostic | 1 | fifo | lru2 | 2 | 0/2 | 11.7121 | 0.629029 | 0.175196 | 7.1721e+06 | 0.0273774 |
| live | empty-content-diagnostic | 1 | resource_fit | lru2 | 2 | 0/2 | 10.7896 | 0.614265 | 0.179094 | 7.20896e+06 | 0.0243669 |
| live | empty-content-diagnostic | 2 | aging_fit | lru2 | 1 | 0/1 | 9.3674 | 0.00930905 | 0.19807 | 17281024 | 0.0121834 |
| live | empty-content-diagnostic | 2 | fifo | lru2 | 2 | 0/2 | 9.24374 | 0.0122545 | 0.191772 | 1.66134e+07 | 0.0243669 |
| live | collaboration-team | 2 | resource_fit | lru2 | 1 | 1/1 | 4.71166 | 0.00689942 | 0.208479 | 17649664 | 0.003744 |
| live | collaboration-review | 2 | resource_fit | lru2 | 1 | 1/1 | 12.9784 | 0.0125522 | 0.260453 | 6991872 | 0.00479512 |
| live | integer-pricing | 1 | fifo | lru2 | 1 | 1/1 | 13.5781 | 0.31845 | 0.387273 | 22503424 | 0.014868 |
| live | integer-pricing | 2 | aging_fit | lru2 | 1 | 1/1 | 7.77343 | 0.0104947 | 0.406866 | 23212032 | 0.00908176 |
| live | integer-pricing | 1 | resource_fit | lru2 | 1 | 1/1 | 8.27932 | 0.317898 | 0.390636 | 23183360 | 0.0109399 |
| live | integer-pricing | 2 | fifo | lru2 | 1 | 1/1 | 7.76458 | 0.0116621 | 0.396927 | 21909504 | 0.00928176 |
| live | integer-pricing | 1 | aging_fit | lru2 | 1 | 1/1 | 8.52639 | 0.340207 | 0.387865 | 23740416 | 0.011275 |
| live | integer-pricing | 2 | resource_fit | lru2 | 1 | 1/1 | 7.25058 | 0.0135175 | 0.39397 | 23175168 | 0.00913776 |
| replay | integer-pricing | 1 | fixed_trace | disabled | 3 | 3/3 | 0.089757 | — | — | — | 0 |
| replay | integer-pricing | 1 | fixed_trace | lru2 | 3 | 3/3 | 0.0828333 | — | — | — | 0 |

**Small-sample limitation:** groups with fewer than three observations, especially n=1, cannot establish statistically reliable speedup. Provider/network latency and nondeterministic answers remain confounders.

Replay token counts describe the original model outputs; only new live requests consume additional provider quota. CPU/memory values require OS measurements; sums of individual task memory peaks are not workflow peak memory.

**Cost completeness:** estimates include failed attempts, empty-content responses and diagnostic runs when provider token usage is present. A failed functional result does not imply a free API call. Completion tokens already include reasoning tokens; do not add them twice.

**Comparison limitations:** worker count may also change the controller admission CPU budget; in that case serial/parallel is a joint intervention, not isolated parallel speedup. Remote latency, vendor prefix-cache warming, request order and nondeterministic answers remain separate confounders. Vendor cache is different from the application JSON LRU-2 cache. One controlled project fixture cannot establish results for arbitrary repositories.

Rate currency: CNY.
live: 21 records, 13 failed; estimated API cost including failures = 0.227632; actual bill unknown.
replay: 6 records, 0 failed; estimated API cost including failures = 0; actual bill unknown.
