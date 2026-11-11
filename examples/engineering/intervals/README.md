# time-intervals

这是有意包含缺陷的受控小项目。

All inputs are integers. overlap_seconds: length of intersection of half-open intervals; reject end<start; non-overlap or empty interval gives zero. bucket_index: floor((timestamp-origin)/width), including negatives; width must be positive. remaining_ttl: ttl minus elapsed time, clamped to 0..ttl; future-created item retains ttl; reject ttl<0. Invalid values raise ValueError. Keep three signatures. Only integer arithmetic //, comparisons, if/return, ValueError/min/max; no imports, attributes, loops or other calls.

补丁只能返回单字段 JSON source，不得修改测试或选择文件路径。独立检查不包含在给模型的 context 中。
