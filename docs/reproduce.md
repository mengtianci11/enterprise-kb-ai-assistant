# 复现指南（Reproduce）

> 目标：任何人 clone 仓库后，能在本机把「检索 / 引用 / 冲突 / 门禁」四套评测跑出来，并得到与 README 一致的数字。

## 1. 环境

- Python **3.10**
- 有 `SILICONFLOW_API_KEY`（评测需要真实调用 embedding / chat / rerank）
- 无需 GPU；CPU 即可
- 首次运行需下载 jieba 词典与 SQLite 索引（自动完成）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env      # 填入 SILICONFLOW_API_KEY
```

## 2. 一键复现

```bash
python scripts/eval_all.py
```

约 **25–30 分钟**，依次执行：

| 步骤 | 脚本 | 预计耗时 |
|---|---|---:|
| 检索 v2（24 题，CI 口径） | `scripts/eval_rag.py` | ~3 min |
| 检索 v3（115 题，计分 105） | `scripts/eval_rag.py --dataset eval/dataset_v3.json ...` | ~15 min |
| 引用校验（30 可答 + 10 不可答） | `scripts/eval_citation.py` | ~6 min |
| 冲突与时效（15 用例） | `scripts/eval_conflict.py` | ~4 min |
| 质量门禁 | `scripts/check_metrics.py --from-last-run` | 秒级 |

产物：

- `eval/eval_all_summary.json`（机器可读）
- `docs/eval_all_report.md`（人读汇总）
- 各评测自己的报告与 last_run 文件

不想跑全量时：

```bash
python scripts/eval_all.py --skip-v3 --skip-citation      # v2 + 冲突 + 门禁（约 8 分钟）
python scripts/eval_all.py --skip-v3 --skip-citation --skip-conflict   # 只跑 v2 + 门禁
python scripts/eval_all.py --only-gate                    # 只用已有结果跑门禁
```

## 3. 预期数字（2026-10-25 实测）

| 评测 | 指标 | 预期 |
|---|---|---:|
| 检索 v2（24 题） | full Hit@3 / MRR@5 / Top-1 | 100.0% / 97.9% / 95.8% |
| 检索 v3（115 题，计分 105） | full Hit@3 / MRR@5 / Top-1 | 100.0% / 99.5% / 99.0% |
| 引用校验 | Citation Precision / Recall / 不可答拒答率 | 0.917 / 0.933 / 100% |
| 冲突与时效 | 检测率 / 归因准确率 | 100% / 100% |
| 单元测试 | pytest | 193 passed |

> LLM 查询改写与判定有随机性，`fusion` / 引用 P-R 可能有 ±2pp 级波动，属正常现象。

## 4. 分步命令

```bash
# 检索 v2（CI 门禁口径）
python scripts/eval_rag.py

# 检索 v3（扩展集）
python scripts/eval_rag.py \
  --dataset eval/dataset_v3.json \
  --report docs/eval_report_v3.md \
  --last-run eval/last_run_v3.json \
  --details eval/last_run_details_v3.json

# 从已有逐题明细重算（不调用 API）
python scripts/eval_rag.py --dataset eval/dataset_v3.json \
  --from-details eval/last_run_details_v3.json \
  --last-run eval/last_run_v3.json --report docs/eval_report_v3.md

# 引用级可验证
python scripts/eval_citation.py --limit 30

# 冲突与时效
python scripts/eval_conflict.py

# 评测集校验（文档/子串/分块可检索）
python scripts/validate_dataset.py eval/dataset_v3.json

# 分块 A/B
python scripts/eval_chunking.py

# 质量门禁（基线回归）
python scripts/check_metrics.py --from-last-run
```

## 5. 常见问题

| 现象 | 原因 / 解决 |
|---|---|
| `SILICONFLOW_API_KEY 未配置` | 没有 `.env` 或 key 为空；复制 `.env.example` 并填写 |
| `ModuleNotFoundError: jwt` | 旧版依赖清单；`pip install -r requirements.txt`（含 PyJWT） |
| `pandas` 缺失 | 仅影响终端表格展示，不影响评测结果 |
| 评测集校验报"evidence 不是原文子串" | 修改语料后未同步 `dataset_v3.json`；用 `scripts/expand_dataset.py` 重新生成 |
| 门禁失败但指标看起来正常 | 检查 `eval/baseline.json` 与 `eval/last_run.json` 是否来自同一评测集 |
| 缓存导致结果异常 | 评测脚本会重建索引；如仍异常，删除 `data/parsed/` 后重试 |

## 6. CI

GitHub Actions（`.github/workflows/ci.yml`）：

- `test`：依赖自检 + 193 项离线单测；
- `secret-scan`：硬编码密钥扫描；
- `eval-gate`：配了 `SILICONFLOW_API_KEY` secret 时真实跑 v2 检索并对比基线（约 4 分钟）。

手动触发：Actions → CI → Run workflow（`workflow_dispatch`）。

---

*对应文档：`docs/eval_expansion.md`（评测集）、`docs/failure_taxonomy.md`（失败分类）、
`docs/design_citation.md`（引用校验）、`docs/design_conflict.md`（冲突时效）。*
