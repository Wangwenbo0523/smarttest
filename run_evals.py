# -*- coding: utf-8 -*-
"""用例生成质量评测。

没有评测集的生成器无法迭代 —— 改了规则、换了模型，
只能凭感觉说「好像好一点」。这个脚本把感觉换成召回率和精确率。

标注集（evals/ground_truth*.yaml）是人工从契约推导的必测清单，与规则引擎相互独立：
生成器漏了什么，评测会直接点出来。每个靶场一份标注集 ——
两个不同领域上都拿到 100%，才说明推导能力是通用的，
而不是给某一份契约量身定做的。

用法：
    python run_evals.py                    # 订单靶场
    python run_evals.py --target users     # 用户与订阅靶场
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import yaml  # noqa: E402

from smarttest.dataprovider import DataProvider  # noqa: E402
from smarttest.parser import OpenApiParser  # noqa: E402
from smarttest.rules import RuleEngine  # noqa: E402
from smarttest.targets import Target, get_target, target_keys  # noqa: E402


def load_ground_truth(target: Target) -> dict[str, str]:
    raw = yaml.safe_load(target.ground_truth.read_text(encoding="utf-8")) or {}
    signatures: dict[str, str] = {}
    for operation_id, fields in raw.items():
        for field_name, bases in (fields or {}).items():
            for basis in bases:
                signatures[f"{operation_id}::{field_name}::{basis}"] = (
                    f"{operation_id}.{field_name}.{basis}"
                )
    return signatures


def collect_generated(target: Target) -> dict[str, str]:
    spec = OpenApiParser.from_file(target.contract).parse()
    cases = RuleEngine(DataProvider.load(target.dataset)).generate(spec)

    signatures: dict[str, str] = {}
    for case in cases:
        basis = case.design_basis.split("=")[0]
        if basis.startswith("baseline:"):
            continue  # 基准用例不属于参数覆盖范畴
        field_name = case.case_id.split(".")[1] if "." in case.case_id else case.case_id
        signatures[f"{case.operation_id}::{field_name}::{basis}"] = case.case_id
    return signatures


def main() -> int:
    arg_parser = argparse.ArgumentParser(description="用例生成质量评测")
    arg_parser.add_argument(
        "--target",
        choices=target_keys(),
        default="orders",
        help="靶场：orders=订单服务，users=用户与订阅服务",
    )
    arg_parser.add_argument("--min-recall", type=float, default=0.0, help="召回率下限（%%），低于则退出码非 0")
    arg_parser.add_argument("--min-precision", type=float, default=0.0, help="精确率下限（%%），低于则退出码非 0")
    args = arg_parser.parse_args()

    target = get_target(args.target)
    report_path = ROOT / "build" / "reports" / f"eval_report_{target.key}.md"

    expected = load_ground_truth(target)
    generated = collect_generated(target)

    matched = sorted(set(expected) & set(generated))
    missing = sorted(set(expected) - set(generated))
    extra = sorted(set(generated) - set(expected))

    recall = len(matched) / len(expected) * 100 if expected else 0.0
    precision = len(matched) / len(generated) * 100 if generated else 0.0

    lines: list[str] = []
    add = lines.append
    add("# SmartTest 用例生成质量评测")
    add("")
    add(f"- 靶场：{target.label}")
    add(f"- 契约：`{target.contract.name}`")
    add(f"- 标注集：`{target.ground_truth.name}`")
    add(f"- 标注集必测项：{len(expected)}")
    add(f"- 实际生成项：{len(generated)}")
    add(f"- **召回率**：{len(matched)}/{len(expected)} = **{recall:.1f}%**")
    add(f"- **精确率**：{len(matched)}/{len(generated)} = **{precision:.1f}%**")
    add("")
    add("召回率 = 该测的有没有测到；精确率 = 生成的是不是都该测。")
    add("")

    add("## 一、覆盖缺口（该测但没生成）")
    add("")
    if not missing:
        add("无。")
    else:
        add("| 缺口 | 说明 |")
        add("|---|---|")
        for key in missing:
            add(f"| `{key}` | {_explain(key)} |")
    add("")

    add("## 二、多余生成（不在标注集内）")
    add("")
    if not extra:
        add("无。")
    else:
        for key in extra:
            add(f"- `{key}`")
    add("")

    add("## 三、已覆盖")
    add("")
    for key in matched:
        add(f"- `{key}`")
    add("")

    report = "\n".join(lines)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")

    print(report)
    print(f"报告已写入: {report_path}")

    failed: list[str] = []
    if recall < args.min_recall:
        failed.append(f"召回率 {recall:.1f}% 低于门禁 {args.min_recall:.1f}%")
    if precision < args.min_precision:
        failed.append(f"精确率 {precision:.1f}% 低于门禁 {args.min_precision:.1f}%")

    if failed:
        print()
        for item in failed:
            print(f"[门禁失败] {target.label}：{item}")
        return 1

    if args.min_recall or args.min_precision:
        print(f"\n[门禁通过] {target.label} 召回率 >= {args.min_recall:.1f}%，"
              f"精确率 >= {args.min_precision:.1f}%")
    return 0


def _explain(key: str) -> str:
    """缺口说明：把签名翻译成人能读的一句话，方便直接定位要补哪条规则。"""
    _, field, basis = (key.split("::") + ["", ""])[:3]
    if basis == "required:missing_header":
        return "规则引擎没有为必填请求头生成用例"
    if basis == "boundary:maxLength":
        return "字符串只生成了上界+1（拒绝侧），漏了上界本身（接受侧）"
    if basis == "boundary:minLength":
        return "字符串只生成了下界-1（拒绝侧），漏了下界本身（接受侧）"
    if basis.startswith("resource:"):
        return f"资源类用例没生成（字段 {field}），检查数据字典里有没有该字段的取值"
    if basis.startswith("enum:"):
        return f"枚举类用例没生成（字段 {field}）"
    if basis.startswith("nullable:"):
        return f"可空字段用例没生成（字段 {field}）"
    return "待补充"


if __name__ == "__main__":
    raise SystemExit(main())
