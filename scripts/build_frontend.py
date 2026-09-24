"""构建前端并拷入 `moneytool/static/`（release-packaging skill）。

用法：python scripts/build_frontend.py [--skip-install] [--no-build]
  --skip-install  不执行 npm ci（本地已装依赖）
  --no-build      只拷贝现有 frontend/dist
"""

from __future__ import annotations

import argparse
import datetime as dt
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
DIST = FRONTEND / "dist"
STATIC = ROOT / "moneytool" / "static"


def run(cmd: list[str], cwd: Path) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, check=True, capture_output=True
        )
        return out.stdout.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def node_version() -> str:
    try:
        return (
            subprocess.run(["node", "--version"], check=True, capture_output=True)
            .stdout.decode()
            .strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-install", action="store_true")
    ap.add_argument("--no-build", action="store_true")
    args = ap.parse_args()

    npm = shutil.which("npm")
    if not args.no_build:
        if npm is None:
            print(
                "未找到 npm；安装 Node.js 22+ 后重试，或用 --no-build 拷贝已有 dist",
                file=sys.stderr,
            )
            return 2
        if not args.skip_install:
            lock = FRONTEND / "package-lock.json"
            run([npm, "ci" if lock.exists() else "install", "--no-audit", "--no-fund"], FRONTEND)
        run([npm, "run", "build"], FRONTEND)

    if not (DIST / "index.html").exists():
        print(f"缺少 {DIST / 'index.html'}", file=sys.stderr)
        return 2

    if STATIC.exists():
        shutil.rmtree(STATIC)
    shutil.copytree(DIST, STATIC)
    (STATIC / "BUILD_INFO").write_text(
        "\n".join(
            [
                f"git_sha={git_sha()}",
                f"built_at={dt.datetime.now(dt.UTC).isoformat(timespec='seconds')}",
                f"node={node_version()}",
                f"python={platform.python_version()}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    files = sum(1 for p in STATIC.rglob("*") if p.is_file())
    print(f"已拷贝 {files} 个文件到 {STATIC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
