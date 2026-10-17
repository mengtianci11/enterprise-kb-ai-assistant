"""敏感信息扫描：防止 API Key 等机密被提交进仓库（CI 与本地预提交用）。

背景：本项目的历史遗留文件（原版备份、早期练习脚本）里曾硬编码过真实 Key。
这个脚本把「不该进仓库的东西」变成可自动检查的规则：
- 优先扫描 git 跟踪的文件（CI 中即真实要发布的内容）；
- 没有 git 仓库时，扫描「应提交白名单」（与 README 的上传清单一致）；
- 命中即返回非 0，阻断提交/合并。

用法：python scripts/scan_secrets.py [--all]
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# OpenAI 兼容服务的 Key 形态：sk- 后跟 20+ 位字母数字/下划线/横线
KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{20,}")

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".idea", "data", "samples", "node_modules"}
SKIP_SUFFIXES = {".pyc", ".log", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".xlsx", ".docx", ".pptx", ".pdf"}

# 无 git 仓库时扫描的白名单（与「上传到 GitHub 的清单」保持一致）
WHITELIST_DIRS = ["app", "tests", "scripts", "eval", "docs"]
WHITELIST_FILES = [
    "企业知识库 AI 助手 - 综合毕业项目.py",
    "README.md",
    "resume_material.md",
    "requirements.txt",
    "requirements-dev.txt",
    "Dockerfile",
    "docker-compose.yml",
    "index.html",
    ".env.example",
    "pytest.ini",
    "LICENSE",
]


def tracked_files() -> list[Path] | None:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=str(ROOT), capture_output=True, text=True, check=True
        ).stdout
        files = [ROOT / line for line in out.splitlines() if line.strip()]
        return files or None
    except Exception:  # noqa: BLE001
        return None


def whitelist_files() -> list[Path]:
    files: list[Path] = []
    for d in WHITELIST_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        files.extend(p for p in base.rglob("*") if p.is_file())
    files.extend(ROOT / f for f in WHITELIST_FILES if (ROOT / f).exists())
    return files


def should_scan(path: Path) -> bool:
    parts = {p.name for p in path.parents}
    if parts & SKIP_DIRS or path.suffix.lower() in SKIP_SUFFIXES:
        return False
    if path.name in {"last_run.json", "baseline.json"}:
        return False
    return True


def main() -> None:
    if "--all" in sys.argv:
        files = [p for p in ROOT.rglob("*") if p.is_file()]
        source = "全部文件（--all）"
    else:
        files = tracked_files()
        source = "git 跟踪文件" if files else "白名单文件"
        files = files or whitelist_files()

    findings: list[tuple[Path, int, str]] = []
    for path in files:
        if not path.exists() or not should_scan(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = KEY_PATTERN.search(line)
            if match:
                rel = path.relative_to(ROOT)
                masked = match.group(0)[:6] + "***"
                findings.append((rel, lineno, masked))

    print(f"扫描范围：{source}（{len(files)} 个文件）")
    if findings:
        print(f"\n❌ 发现 {len(findings)} 处疑似密钥：")
        for rel, lineno, masked in findings:
            print(f"  {rel}:{lineno}  ->  {masked}")
        print("\n处理建议：删除硬编码、改用 .env / 环境变量；若文件与项目无关请勿提交。")
        sys.exit(1)

    print("✅ 未发现硬编码密钥")


if __name__ == "__main__":
    main()
