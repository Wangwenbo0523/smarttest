"""SmartTest 演示界面启动器。

用法：
    python run_demo_app.py                       # 启动后自动打开浏览器
    python run_demo_app.py --port 8600           # 换端口
    python run_demo_app.py --no-browser          # 不自动打开浏览器

启动后浏览器里是完整流水线：拉契约 → 生成用例 → 语义增强 → 渲染 pytest
→ 执行 → 失败归因 → 质量报告，并支持「缺陷版 / 修复版」一键对比。
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="SmartTest 演示界面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8500)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    import uvicorn

    url = f"http://{args.host}:{args.port}"
    print(f"SmartTest 演示界面：{url}")
    print("按 Ctrl+C 退出；靶场进程会一并关闭。")

    if not args.no_browser:
        def _open() -> None:
            time.sleep(1.5)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run("demo.server:app", host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
