"""检索质量门禁：与基线对比，指标回归超过阈值即失败（CI / 本地预提交用）。

设计（为什么需要）：
- RAG 的改动很容易「看起来没坏、实际变差」（换分块、改权重、升级模型）；
- 单测只能保证「功能正确」，保证不了「效果不回归」；
- 因此在 CI 里把**离线评测**做成硬门禁：指标较基线下降超过容忍度就阻断合并。

用法：
    python scripts/check_metrics.py                      # 重新跑评测后与基线对比（需 API Key）
    python scripts/check_metrics.py --from-last-run      # 用 eval/last_run.json 对比（不调用 API）
    python scripts/check_metrics.py --update-baseline    # 将当前结果写入基线
    python scripts/check_metrics.py --update-baseline --from-last-run

退出码：0 = 通过 / 跳过；1 = 回归或数据集变更。
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASELINE_PATH = ROOT / "eval" / "baseline.json"
LAST_RUN_PATH = ROOT / "eval" / "last_run.json"

# 指标回归容忍度（百分点）。超过即视为回归。
DEFAULT_THRESHOLDS = {
    "hit@3": 2.0,
    "hit@5": 2.0,
    "mrr@5": 3.0,
    "top1": 5.0,
    "doc_hit@5": 2.0,
}
METRIC_KEYS = ["hit@3", "hit@5", "mrr@5", "top1", "doc_hit@5"]


def load_last_run() -> dict:
    if not LAST_RUN_PATH.exists():
        print(f"[FAIL] 找不到 {LAST_RUN_PATH}，请先运行 python scripts/eval_rag.py")
        sys.exit(1)
    return json.loads(LAST_RUN_PATH.read_text(encoding="utf-8"))


def run_eval() -> dict:
    """调用评测脚本（真实 API）并读取机器可读结果。"""
    from app.config import settings

    if not settings.API_KEY:
        print("[SKIP] 未配置 SILICONFLOW_API_KEY，跳过质量门禁（单元测试仍会执行）")
        sys.exit(0)

    print("[1/2] 运行离线评测（scripts/eval_rag.py）...")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "eval_rag.py")],
        cwd=str(ROOT),
        check=True,
    )
    return load_last_run()


def compare(current: dict, baseline: dict, allow_dataset_change: bool) -> bool:
    ok = True
    base_dataset = baseline.get("dataset_md5")
    if base_dataset and current.get("dataset_md5") != base_dataset:
        print("[FAIL] 评测集已变更（dataset_md5 不一致），基线可能失效；确认后使用 --allow-dataset-change 或更新基线")
        if not allow_dataset_change:
            return False

    thresholds = baseline.get("thresholds_pp") or DEFAULT_THRESHOLDS
    print(f"\n{'策略':<8} {'指标':<10} {'基线':>7} {'当前':>7} {'变化':>8}  结果")
    print("-" * 62)
    for strategy, base_metrics in baseline.get("metrics", {}).items():
        current_metrics = current.get("metrics", {}).get(strategy)
        if current_metrics is None:
            print(f"{strategy:<8} 缺失（当前结果中没有该策略）")
            continue
        for metric in METRIC_KEYS:
            base_val = base_metrics.get(metric)
            if base_val is None:
                continue
            cur_val = float(current_metrics.get(metric, 0.0))
            delta = round(cur_val - float(base_val), 1)
            limit = float(thresholds.get(metric, 3.0))
            if delta < -limit:
                flag, ok = "FAIL", False
            elif delta < 0:
                flag = "warn"
            else:
                flag = "ok"
            print(
                f"{strategy:<8} {metric:<10} {float(base_val):>7.1f} {cur_val:>7.1f} "
                f"{delta:>+7.1f}pp  {flag}"
            )
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="检索质量门禁")
    parser.add_argument("--from-last-run", action="store_true", help="使用上次评测结果，不重新调用 API")
    parser.add_argument("--update-baseline", action="store_true", help="把当前结果写为基线")
    parser.add_argument("--allow-dataset-change", action="store_true", help="允许评测集变更（仍会告警）")
    args = parser.parse_args()

    current = load_last_run() if args.from_last_run else run_eval()

    if args.update_baseline:
        baseline = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "note": "由 scripts/check_metrics.py --update-baseline 生成；请勿手改数值",
            "dataset_md5": current.get("dataset_md5"),
            "chunk_strategy": current.get("chunk_strategy"),
            "chunk_size": current.get("chunk_size"),
            "chunk_overlap": current.get("chunk_overlap"),
            "thresholds_pp": DEFAULT_THRESHOLDS,
            "metrics": current.get("metrics", {}),
        }
        BASELINE_PATH.write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[OK] 基线已更新：{BASELINE_PATH}")
        return

    if not BASELINE_PATH.exists():
        print(f"[SKIP] 尚未建立基线（{BASELINE_PATH}），请先执行 --update-baseline")
        sys.exit(0)

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    print(f"[2/2] 对比基线（生成于 {baseline.get('created_at', '未知')}）...")
    ok = compare(current, baseline, args.allow_dataset_change)
    print("\n结果：" + ("✅ 通过（指标未回归）" if ok else "❌ 失败（存在指标回归，请排查或说明原因）"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
