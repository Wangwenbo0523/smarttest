"""SmartTest 演示流水线。

`run_demo.py` 面向命令行（打印日志、返回退出码），本模块面向界面（返回结构化数据）。
两者复用同一套 `smarttest` 组件，保证「界面上看到的」和「CI 里跑的」是同一条链路 ——
演示界面不能是另一套实现，否则演示就失去意义了。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from smarttest.dataprovider import DataProvider  # noqa: E402
from smarttest.generator import PytestGenerator  # noqa: E402
from smarttest.localenv import load_local_env  # noqa: E402
from smarttest.metrics import MetricsReporter  # noqa: E402
from smarttest.parser import OpenApiParser  # noqa: E402
from smarttest.rules import RuleEngine  # noqa: E402
from smarttest.runner import PytestRunner, RunSummary  # noqa: E402
from smarttest.semantic import SemanticEnhancer, build_provider  # noqa: E402
from smarttest.semantic.providers import RuleBasedScenarioProvider  # noqa: E402
from smarttest.triage import Triage  # noqa: E402

load_local_env(ROOT)

TARGET_MODES = ("buggy", "fixed")
TARGET_PORT = int(os.environ.get("SMARTTEST_TARGET_PORT", "8123"))
BASE_URL = f"http://127.0.0.1:{TARGET_PORT}"
BUILD_DIR = ROOT / "build"
GENERATED_DIR = BUILD_DIR / "generated_tests"
REPORT_DIR = BUILD_DIR / "reports"


def llm_configured() -> bool:
    return bool(os.environ.get("SMARTTEST_LLM_API_KEY", "").strip())


def llm_model() -> str:
    return os.environ.get("SMARTTEST_LLM_MODEL", "").strip()


# --------------------------------------------------------------------------
# 序列化辅助
# --------------------------------------------------------------------------
def _jsonable(value: Any) -> Any:
    """把任意结构压成 JSON 可序列化的形式，界面不该因为一个 dataclass 报错。"""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _schema_hint(schema: dict[str, Any] | None) -> str:
    """把 JSON Schema 压成一行人能读的约束描述。"""
    if not schema:
        return "-"
    parts: list[str] = []
    type_name = schema.get("type")
    if isinstance(type_name, list):
        type_name = "|".join(str(t) for t in type_name)
    if type_name:
        parts.append(str(type_name))
    if schema.get("enum"):
        parts.append("enum=" + "/".join(str(v) for v in schema["enum"]))
    for key, label in (("minimum", "min"), ("maximum", "max"), ("minLength", "minLen"),
                       ("maxLength", "maxLen"), ("pattern", "pattern"), ("format", "format")):
        if key in schema:
            parts.append(f"{label}={schema[key]}")
    if schema.get("default") is not None:
        parts.append(f"default={schema['default']}")
    return " · ".join(parts) if parts else "-"


def _serialize_spec(spec) -> dict[str, Any]:
    operations = []
    for op in spec.operations:
        body_schema = op.body_schema or {}
        required_body = set(body_schema.get("required", []) or [])
        operations.append(
            {
                "operation_id": op.operation_id,
                "method": op.method.upper(),
                "path": op.path,
                "tag": op.tag,
                "summary": op.summary,
                "signature": op.signature,
                "success_status": op.success_status,
                "documented_errors": op.documented_errors,
                "parameters": [
                    {
                        "name": p.name,
                        "location": p.location,
                        "required": p.required,
                        "hint": _schema_hint(p.schema),
                    }
                    for p in op.parameters
                ],
                "body_fields": [
                    {"name": name, "required": name in required_body, "hint": _schema_hint(field)}
                    for name, field in (body_schema.get("properties") or {}).items()
                ],
                "response_fields": [
                    {"name": name, "hint": _schema_hint(field)}
                    for name, field in op.response_properties().items()
                ],
            }
        )
    return {"title": spec.title, "version": spec.version, "operations": operations}


def _serialize_scenario(scenario) -> dict[str, Any]:
    return {
        "scenario_id": scenario.scenario_id,
        "title": scenario.title,
        "rationale": scenario.rationale,
        "source": scenario.source,
        "steps": [
            {
                "operation_id": step.operation_id,
                "expect_status": step.expect_status,
                "path_params": step.path_params,
                "body": step.body,
                "capture": step.capture,
                "expect_body": step.expect_body,
            }
            for step in scenario.steps
        ],
    }


# --------------------------------------------------------------------------
# 被测服务进程管理
# --------------------------------------------------------------------------
class TargetService:
    """管理被测靶场进程，支持 buggy / fixed 热切换。

    演示界面要能一键对比「有缺陷」和「已修复」两次运行，
    所以靶场进程必须可复用、可切换，而不是每次重新冷启动。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._mode: str | None = None
        self._external = False

    @property
    def mode(self) -> str | None:
        return self._mode

    @property
    def managed(self) -> bool:
        """靶场是否由本进程拉起（外部占用时无法热切换）。"""
        return self._proc is not None

    def healthy(self, timeout: float = 1.0) -> bool:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/openapi.json", timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _spawn(self, mode: str) -> None:
        self._proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "target_service.app:app",
                "--host", "127.0.0.1", "--port", str(TARGET_PORT), "--log-level", "warning",
            ],
            cwd=str(ROOT),
            env={**os.environ, "SMARTTEST_TARGET_MODE": mode},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _await_ready(self, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.healthy():
                return True
            time.sleep(0.3)
        return False

    def _stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
        self._proc = None
        self._mode = None

    def ensure(self, mode: str) -> tuple[bool, str]:
        """确保靶场以指定模式就绪，返回 (是否就绪, 一行说明)。"""
        if mode not in TARGET_MODES:
            mode = "buggy"
        with self._lock:
            if self._proc is not None and self._proc.poll() is not None:
                self._proc, self._mode = None, None

            if self.healthy():
                if self._mode == mode:
                    return True, f"复用已就绪的靶场（{mode} 模式）"
                if self._mode is None or self._external:
                    self._external = True
                    return True, "端口已被外部服务占用，沿用该服务（模式由外部决定，无法热切换）"
                self._stop()
                self._spawn(mode)
                if not self._await_ready():
                    return False, "靶场重启超时"
                self._mode = mode
                return True, f"靶场已重启为 {mode} 模式"

            self._external = False
            self._spawn(mode)
            if not self._await_ready():
                return False, f"靶场启动超时，请检查端口 {TARGET_PORT} 是否被占用"
            self._mode = mode
            return True, f"靶场已启动（{mode} 模式）"

    def shutdown(self) -> None:
        with self._lock:
            self._stop()


# --------------------------------------------------------------------------
# 流水线
# --------------------------------------------------------------------------
def _fetch_products(product_ids: list[str]) -> list[dict]:
    """生成期从被测服务实时探测商品数据，避免测试数据与被测系统脱节。"""
    products: list[dict] = []
    with httpx.Client(timeout=5.0) as client:
        for pid in product_ids:
            try:
                resp = client.get(f"{BASE_URL}/api/v1/products/{pid}")
            except httpx.HTTPError:
                continue
            if resp.status_code == 200:
                products.append(resp.json())
    return products


def _case_to_dict(case) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "operation_id": case.operation_id,
        "description": case.description,
        "method": case.method.upper(),
        "path": case.path,
        "expected_status": case.expected_status,
        "design_basis": case.design_basis,
        "payload": _jsonable(case.payload),
        "headers": _jsonable(case.headers),
    }


def _findings_to_dicts(findings) -> list[dict[str, Any]]:
    return [
        {
            "case_id": f.case_id,
            "category": f.category,
            "severity": f.severity,
            "title": f.title,
            "basis": f.basis,
            "suggestion": f.suggestion,
            "evidence": f.evidence,
            "field": f.field,
        }
        for f in findings
    ]


def _run_aborted(mode: str, steps: list[dict], provider: str, reason: str, started: float) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "mode": mode,
        "provider": provider,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": round(time.perf_counter() - started, 2),
        "steps": steps,
        "contract": {"title": "", "version": "", "operations": []},
        "cases": [],
        "case_count": 0,
        "basis_summary": {},
        "field_coverage": {},
        "field_count": 0,
        "coverage_detail": [],
        "scenarios": {"provider": provider, "accepted": [], "rejected": [], "degraded_reason": "",
                      "retry_attempted": False, "retry_recovered": 0},
        "results": [],
        "totals": {"total": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0},
        "metrics": {"pass_rate": 0.0, "api_coverage": 0.0, "defect_count": 0,
                    "suspected_false_positives": 0, "field_count": 0,
                    "operations_total": 0, "operations_covered": 0, "duration": 0.0},
        "findings": [],
        "defects": [],
        "code": {},
        "report_markdown": "",
    }


def run_pipeline(mode: str = "buggy", use_llm: bool = True,
                 service: TargetService | None = None) -> dict[str, Any]:
    """跑一次完整流水线，返回界面直接可用的结构化结果。"""
    if mode not in TARGET_MODES:
        mode = "buggy"
    service = service or TargetService()
    steps: list[dict[str, Any]] = []
    started = time.perf_counter()
    provider_label = "llm" if use_llm else "rule-based"

    def record(n: int, title: str, status: str, detail: str = "") -> None:
        steps.append({"n": n, "title": title, "status": status, "detail": detail})

    ready, note = service.ensure(mode)
    record(1, f"启动被测服务（订单服务靶场 · {mode} 模式）", "ok" if ready else "fail", note)
    if not ready:
        return _run_aborted(mode, steps, provider_label, note, started)

    try:
        spec = OpenApiParser.from_url(f"{BASE_URL}/openapi.json").parse()
    except Exception as exc:  # 网络/契约解析失败都归到这一步，界面要能看见原因
        detail = f"{type(exc).__name__}: {exc}"
        record(2, "拉取并解析设计契约（OpenAPI 3.x → IR）", "fail", detail)
        return _run_aborted(mode, steps, provider_label, detail, started)
    record(
        2, "拉取并解析设计契约（OpenAPI 3.x → IR）", "ok",
        f"{len(spec.operations)} 个接口：" + "、".join(op.operation_id for op in spec.operations),
    )

    dp = DataProvider.load()
    engine = RuleEngine(dp)
    cases = engine.generate(spec)
    record(
        3, "规则引擎按 JSON Schema 推导参数级用例", "ok",
        f"{len(cases)} 条契约用例，覆盖 {len(engine.coverage)} 个字段（必填 / 类型 / 边界 / 枚举 / 长度）",
    )

    if use_llm:
        provider = build_provider(verbose=False)
    else:
        provider = RuleBasedScenarioProvider()
    enhancer = SemanticEnhancer(provider, dp)
    enhancement = enhancer.enhance(spec)
    provider_label = enhancement.provider_name
    detail = (f"提供方 {enhancement.provider_name}；采纳 {len(enhancement.scenarios)} 个场景、"
              f"拒绝 {enhancement.rejected_count} 个")
    if enhancement.retry_attempted:
        detail += f"；回灌拒绝原因重试补回 {enhancement.retry_recovered} 个"
    if enhancement.degraded_reason:
        detail += f"；已降级：{enhancement.degraded_reason}"
    record(4, "语义增强：推导跨接口业务场景", "ok", detail)

    generator = PytestGenerator(base_url=BASE_URL)
    generator.render_conftest(GENERATED_DIR)
    contract_file = generator.render_contract_tests(spec, cases, GENERATED_DIR)
    products = _fetch_products(dp.values("product_id"))
    business_file = generator.render_business_tests(
        GENERATED_DIR,
        "business_test.py.j2",
        {
            "products": products,
            "default_product_id": products[0]["product_id"] if products else "P001",
        },
    )
    scenario_file = generator.render_scenario_tests(
        enhancement.scenarios,
        GENERATED_DIR,
        provider_name=enhancement.provider_name,
        degraded_reason=enhancement.degraded_reason,
    )
    record(
        5, "渲染 pytest 代码（Jinja2 模板 + 真实测试数据）", "ok",
        f"{contract_file.name} / {business_file.name}"
        f"（探测到 {len(products)} 个真实商品作为夹具）/ {scenario_file.name}",
    )

    report_dir = REPORT_DIR / f"demo-{mode}"
    try:
        summary: RunSummary = PytestRunner(GENERATED_DIR, report_dir).run()
    except subprocess.TimeoutExpired as exc:
        detail = f"测试执行超时：{exc}"
        record(6, "执行测试", "fail", detail)
        return _run_aborted(mode, steps, provider_label, detail, started)
    record(
        6, "执行测试", "ok",
        f"用例 {summary.total} | 通过 {summary.passed} | 失败 {summary.failed + summary.errors}"
        f" | 耗时 {summary.duration:.2f}s",
    )

    triage = Triage()
    reporter = MetricsReporter(spec, cases, summary, triage)
    snapshot = reporter.build()
    report_path = reporter.write(snapshot, report_dir / "quality_report.md")
    record(
        7, "失败归因：把「测试失败」翻译成「谁的问题」", "ok",
        f"真实缺陷 {snapshot.defect_count} 个；疑似假阳性 {snapshot.suspected_false_positives} 条；"
        f"报告已落盘 {report_path.relative_to(ROOT)}",
    )

    code = {}
    for key, path in (
        ("contract", contract_file),
        ("business", business_file),
        ("scenario", scenario_file),
        ("scenarios_json", GENERATED_DIR / "scenarios.json"),
        ("conftest", GENERATED_DIR / "conftest.py"),
    ):
        try:
            code[key] = path.read_text(encoding="utf-8")
        except OSError:
            code[key] = ""

    return {
        "ok": True,
        "reason": "",
        "mode": mode,
        "provider": provider_label,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": round(time.perf_counter() - started, 2),
        "steps": steps,
        "contract": _serialize_spec(spec),
        "cases": [_case_to_dict(c) for c in cases],
        "case_count": len(cases),
        "basis_summary": snapshot.basis_summary,
        "field_coverage": snapshot.field_coverage,
        "field_count": snapshot.field_count,
        "coverage_detail": [
            {"field": name, "operation_id": cov.operation_id, "bases": sorted(cov.bases)}
            for name, cov in engine.coverage.items()
        ],
        "scenarios": {
            "provider": enhancement.provider_name,
            "accepted": [_serialize_scenario(s) for s in enhancement.scenarios],
            "rejected": [
                {"scenario_id": r.scenario_id, "reason": r.reason} for r in enhancement.rejected
            ],
            "degraded_reason": enhancement.degraded_reason,
            "retry_attempted": enhancement.retry_attempted,
            "retry_recovered": enhancement.retry_recovered,
        },
        "results": [
            {"name": r.name, "status": r.status, "duration": round(r.duration, 3),
             "message": r.message[:600], "has_message": bool(r.message)}
            for r in summary.results
        ],
        "totals": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "errors": summary.errors,
            "skipped": summary.skipped,
        },
        "metrics": {
            "pass_rate": round(snapshot.pass_rate, 1),
            "api_coverage": round(snapshot.api_coverage, 1),
            "cases_total": snapshot.cases_total,
            "cases_passed": snapshot.cases_passed,
            "cases_failed": snapshot.cases_failed + snapshot.cases_errors,
            "operations_total": snapshot.operations_total,
            "operations_covered": snapshot.operations_covered,
            "defect_count": snapshot.defect_count,
            "suspected_false_positives": snapshot.suspected_false_positives,
            "field_count": snapshot.field_count,
            "duration": round(snapshot.duration, 2),
            "basis_summary": snapshot.basis_summary,
        },
        "findings": _findings_to_dicts(snapshot.findings),
        "defects": _jsonable(snapshot.defects),
        "report_markdown": reporter.render_markdown(snapshot),
        "code": code,
    }
