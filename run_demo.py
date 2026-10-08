"""SmartTest 一键演示（仓库根目录的薄壳）。

真正的实现在 `smarttest.pipeline`，这样它才能被装进包里、由统一入口调用：

    smarttest run --target users       # 装了包之后的写法
    python run_demo.py --target users  # 这个文件，等价

保留这个文件是为了不破坏既有用法（README 与 CI 都还在用）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smarttest.pipeline import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
