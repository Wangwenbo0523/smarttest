"""用例生成质量评测（仓库根目录的薄壳）。

真正的实现在 `smarttest.evaluation`：

    smarttest eval --target users       # 装了包之后的写法
    python run_evals.py --target users  # 这个文件，等价
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smarttest.evaluation import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
