# Integer Pricing · 受控工程修复项目

这里保留的是待修复原版，不是正确答案。项目使用整数分表示金额，包含折扣计算、免邮边界、退款舍入三个缺陷。

## 需求

Inputs are integers; money is non-negative cents. line_total_cents(unit_cents,quantity,discount_pct): unit_cents and quantity >=0, discount_pct in 0..100; apply percentage discount to the whole line and floor once to integer cents. shipping_cents(subtotal_cents): reject negatives, free shipping for subtotal >=5000, otherwise 599. refund_cents(paid_cents,returned_qty,total_qty): paid_cents>=0, total_qty>0, 0<=returned_qty<=total_qty; prorate the whole paid amount and floor once. Invalid values must raise ValueError. Keep exactly these three signatures. No imports, attributes, subscripts, loops, decorators or arbitrary calls; only integer arithmetic, comparisons, if/return, and ValueError/min/max are supported.

## 执行检查

从仓库根目录运行：

```bash
python3 -m agent_runtime.engineering baseline --workspace runs/engineering
python3 -m agent_runtime.engineering context
python3 -m agent_runtime.engineering check --response model-response.json --workspace runs/engineering
```

baseline 命令预期返回非零，报告原始失败用例。补丁仅接受 `{"source":"完整 pricing.py 源码"}`，不能指定路径、改测试或添加其他模块。只有通过严格 AST 子集检查的源码会进入固定测试执行器。公开用例位于 public_cases.json，另有未放进模型 context 的独立 heldout 用例；报告分别列出结果。

每次检查使用独立工作目录与 `python -I -S`，不继承供应商密钥，并有五秒本地截止。仍需外层 cgroup 约束 CPU/内存/pids；这是固定纯整数 Python 子集，不是任意仓库或任意代码的通用沙箱。模型参考修复只在验证器自身的测试文件中，不能作为真实修复实验输入。
