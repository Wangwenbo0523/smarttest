"""SmartTest 一键演示。

流水线：启动靶场 -> 拉取契约 -> 解析 -> 规则引擎生成用例 -> 渲染 pytest
        -> 执行 -> 失败归因 -> 输出质量报告

用法：
    python run_demo.py              # 自动启动被测服务
    python run_demo.py --no-server  # 假定被测服务已在 8123 端口运行
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，中文会变成乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from smarttest.dataprovider import DataProvider  # noqa: E402
from smarttest.generator import PytestGenerator  # noqa: E402
from smarttest.metrics import MetricsReporter  # noqa: E402
from smarttest.parser import OpenApiParser  # noqa: E402
from smarttest.rules import RuleEngine  # noqa: E402
from smarttest.runner import PytestRunner  # noqa: E402
from smarttest.semantic import SemanticEnhancer, build_provider  # noqa: E402
from smarttest.triage import Triage  # noqa: E402

BASE_URL = "http://127.0.0.1:8123"
BUILD_DIR = ROOT / "build"
GENERATED_DIR = BUILD_DIR / "generated_tests"
REPORT_DIR = BUILD_DIR / "reports"

STEP = "  {n}. {text}"


def say(text: str) -> None:
    print(text, flush=True)


def wait_for_service(timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/openapi.json", timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def start_service(mode: str = "buggy") -> subprocess.Popen:
    env = {**os.environ, "SMARTTEST_TARGET_MODE": mode}
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "target_service.app:app",
            "--host", "127.0.0.1", "--port", "8123", "--log-level", "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def fetch_products(product_ids: list[str]) -> list[dict]:
    """生成期从被测服务实时探测商品数据，避免测试数据与被测系统脱节。"""
    products: list[dict] = []
    with httpx.Client(timeout=5.0) as client:
        for pid in product_ids:
            resp = client.get(f"{BASE_URL}/api/v1/products/{pid}")
            if resp.status_code == 200:
                products.append(resp.json())
    return products


def main() -> int:
    arg_parser = argparse.ArgumentParser(description="SmartTest 演示流水线")
    arg_parser.add_argument("--no-server", action="store_true", help="假定被测服务已在运行")
    arg_parser.add_argument(
        "--target-mode",
        choices=["buggy", "fixed"],
        default="buggy",
        help="靶场模式：buggy 保留注入缺陷；fixed 已修复（用于验证零误报）",
    )
    arg_parser.add_argument(
        "--expect-defects",
        type=int,
        default=None,
        help="期望检出的缺陷数，不符则以非 0 退出（CI 门禁用）",
    )
    arg_parser.add_argument(
        "--report-tag",
        default="",
        help="报告子目录名。CI 里两次运行分别落盘，避免互相覆盖",
    )
    args = arg_parser.parse_args()

    service = None
    if not args.no_server:
        say(STEP.format(n=1, text=f"启动被测服务（订单服务靶场，模式={args.target_mode}）..."))
        service = start_service(args.target_mode)
    else:
        say(STEP.format(n=1, text="跳过启动，复用已在运行的服务"))

    try:
        if not wait_for_service():
            say("       [失败] 被测服务未就绪，请检查端口 8123 是否被占用")
            return 1
        say("       [OK] 服务已就绪")

        say(STEP.format(n=2, text=f"拉取设计契约 {BASE_URL}/openapi.json ..."))
        spec = OpenApiParser.from_url(f"{BASE_URL}/openapi.json").parse()
        say(f"       [OK] 解析出 {len(spec.operations)} 个接口："
            + ", ".join(op.operation_id for op in spec.operations))

        say(STEP.format(n=3, text="规则引擎从 JSON Schema 推导参数级用例..."))
        dp = DataProvider.load()
        engine = RuleEngine(dp)
        cases = engine.generate(spec)
        say(f"       [OK] 生成 {len(cases)} 条契约用例，覆盖 {len(engine.coverage)} 个字段")

        say(STEP.format(n=4, text="语义增强：推导跨接口业务场景..."))
        enhancer = SemanticEnhancer(build_provider(), dp)
        enhancement = enhancer.enhance(spec)
        say(f"       [OK] 提供方={enhancement.provider_name} | 采纳 {len(enhancement.scenarios)} 个场景"
            f" | 拒绝 {enhancement.rejected_count} 个")
        for rejected in enhancement.rejected:
            say(f"       [!] 拒绝 {rejected.scenario_id}：{rejected.reason}")

        say(STEP.format(n=5, text="渲染 pytest 代码（Jinja2 模板 + 真实测试数据）..."))
        generator = PytestGenerator(base_url=BASE_URL)
        generator.render_conftest(GENERATED_DIR)
        contract_file = generator.render_contract_tests(spec, cases, GENERATED_DIR)
        products = fetch_products(dp.values("product_id"))
        business_file = generator.render_business_tests(products, GENERATED_DIR)
        scenario_file = generator.render_scenario_tests(
            enhancement.scenarios,
            GENERATED_DIR,
            provider_name=enhancement.provider_name,
            degraded_reason=enhancement.degraded_reason,
        )
        say(f"       [OK] {contract_file.name}")
        say(f"       [OK] {business_file.name}（探测到 {len(products)} 个真实商品作为夹具）")
        say(f"       [OK] {scenario_file.name}（{len(enhancement.scenarios)} 个跨接口场景）")

        report_dir = REPORT_DIR / args.report_tag if args.report_tag else REPORT_DIR

        say(STEP.format(n=6, text="执行测试..."))
        summary = PytestRunner(GENERATED_DIR, report_dir).run()
        say(f"       用例 {summary.total} | 通过 {summary.passed} | 失败 {summary.failed + summary.errors}"
            f" | 耗时 {summary.duration:.2f}s")

        say(STEP.format(n=7, text="失败归因..."))
        triage = Triage()
        reporter = MetricsReporter(spec, cases, summary, triage)
        snapshot = reporter.build()
        report_path = reporter.write(snapshot, report_dir / "quality_report.md")
        say(f"       [OK] 发现 {snapshot.defect_count} 个真实缺陷，"
            f"疑似假阳性 {snapshot.suspected_false_positives} 条")

        say("")
        say("=" * 68)
        say(f"用例通过率 {snapshot.pass_rate:.1f}% | 接口覆盖率 {snapshot.api_coverage:.1f}% | "
            f"发现缺陷 {snapshot.defect_count} 个 | 耗时 {snapshot.duration:.2f}s")
        say("=" * 68)
        say("")
        for i, defect in enumerate(snapshot.defects, 1):
            say(f"[缺陷 {i}] {defect['title']}  (严重度 {defect['severity']})")
            say(f"         设计依据: {defect['basis']}")
            say(f"         命中用例: {', '.join(defect['cases'])}")
            say(f"         修复建议: {defect['suggestion']}")
            say("")
        say(f"完整报告: {report_path}")

        if args.expect_defects is not None and snapshot.defect_count != args.expect_defects:
            say("")
            say(f"[门禁失败] 期望检出 {args.expect_defects} 个缺陷，实际 {snapshot.defect_count} 个")
            return 1

        return 0

    finally:
        if service is not None:
            service.terminate()
            try:
                service.wait(timeout=5)
            except Exception:
                service.kill()


if __name__ == "__main__":
    raise SystemExit(main())