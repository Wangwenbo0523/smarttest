"""SmartTest 命令行入口。

    smarttest run --target users     # 跑完整流水线（生成 -> 执行 -> 归因 -> 报告）
    smarttest eval --target users    # 只跑用例生成质量评测
    smarttest targets                # 列出已登记的靶场

三个子命令都直接复用既有实现，没有另写一套：
CI 里跑的 `python run_demo.py ...` 与这里的 `smarttest run ...` 是同一条链路。
"""
from __future__ import annotations

import sys

from . import __version__
from .targets import TARGETS

USAGE = f"""SmartTest v{__version__} —— 接口契约驱动的测试用例智能生成与失败归因

用法：
    smarttest run  [--target 靶场] [--target-mode buggy|fixed] [...]     跑完整流水线
    smarttest eval [--target 靶场] [--min-recall N] [--min-precision N]  只跑生成质量评测
    smarttest targets                                                    列出已登记的靶场
    smarttest version

run / eval 的参数与 python run_demo.py / python run_evals.py 完全一致，
加 -h 可以看各自完整的选项。默认靶场是 orders。
"""


def _print_targets() -> int:
    width = max(len(target.key) for target in TARGETS.values())
    print(f"{'靶场'.ljust(width)}  端口   契约                         数据字典")
    for target in sorted(TARGETS.values(), key=lambda item: item.key):
        print(
            f"{target.key.ljust(width)}  {target.port}   "
            f"{target.contract.name:26s}  {target.dataset.name}"
        )
    print("")
    print(f"共 {len(TARGETS)} 个靶场；明细见 smarttest/targets.py")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    command, rest = args[0], args[1:]

    if command == "version":
        print(__version__)
        return 0

    if command == "targets":
        return _print_targets()

    if command == "run":
        from .pipeline import main as run_pipeline

        return run_pipeline(rest)

    if command == "eval":
        from .evaluation import main as run_evaluation

        return run_evaluation(rest)

    print(f"未知子命令：{command}\n")
    print(USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
