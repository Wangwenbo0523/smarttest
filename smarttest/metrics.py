"""度量：把执行结果换算成能写进报告、也能写进简历的数字。"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .ir import ApiSpec
from .rules import ValidationCase, case_field_path
from .runner import RunSummary
from .triage import CASE_ISSUE, REAL_DEFECT, Triage


@dataclass
class MetricsSnapshot:
    spec_title: str = ""
    spec_version: str = ""
    operations_total: int = 0
    operations_covered: int = 0
    cases_total: int = 0
    cases_passed: int = 0
    cases_failed: int = 0
    cases_errors: int = 0
    duration: float = 0.0
    basis_summary: dict[str, int] = field(default_factory=dict)
    field_coverage: dict[str, list[str]] = field(default_factory=dict)
    findings: list = field(default_factory=list)
    defects: list[dict] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.cases_passed / self.cases_total * 100 if self.cases_total else 0.0

    @property
    def api_coverage(self) -> float:
        return self.operations_covered / self.operations_total * 100 if self.operations_total else 0.0

    @property
    def defect_count(self) -> int:
        return len(self.defects)

    @property
    def suspected_false_positives(self) -> int:
        return sum(1 for f in self.findings if f.category == CASE_ISSUE)

    @property
    def field_count(self) -> int:
        return len(self.field_coverage)


class MetricsReporter:
    def __init__(self, spec: ApiSpec, cases: list[ValidationCase], summary: RunSummary, triage: Triage):
        self.spec = spec
        self.cases = cases
        self.summary = summary
        self.triage = triage

    def build(self) -> MetricsSnapshot:
        findings = self.triage.classify(self.summary)

        covered_ops = {c.operation_id for c in self.cases}
        basis_counter: Counter[str] = Counter()
        field_coverage: dict[str, set[str]] = {}
        for case in self.cases:
            basis = case.design_basis.split("=")[0]
            basis_counter[basis] += 1
            if basis.startswith("baseline:"):
                continue  # 基准用例不对应具体字段
            field = case_field_path(case.case_id)
            field_coverage.setdefault(field, set()).add(basis)

        return MetricsSnapshot(
            spec_title=self.spec.title,
            spec_version=self.spec.version,
            operations_total=len(self.spec.operations),
            operations_covered=len(covered_ops),
            cases_total=self.summary.total or len(self.cases),
            cases_passed=self.summary.passed,
            cases_failed=self.summary.failed,
            cases_errors=self.summary.errors,
            duration=self.summary.duration,
            basis_summary=dict(sorted(basis_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
            field_coverage={k: sorted(v) for k, v in sorted(field_coverage.items())},
            findings=findings,
            defects=self.triage.merge_by_root_cause(findings),
        )

    def render_markdown(self, snapshot: MetricsSnapshot) -> str:
        m = snapshot
        lines: list[str] = []
        add = lines.append

        add("# SmartTest 质量报告")
        add("")
        add(f"- 契约来源：{m.spec_title} v{m.spec_version}")
        add(f"- 用例总数：{m.cases_total}（通过 {m.cases_passed} / 失败 {m.cases_failed + m.cases_errors}）")
        add(f"- 用例通过率：{m.pass_rate:.1f}%")
        add(f"- 接口覆盖率：{m.operations_covered}/{m.operations_total} = {m.api_coverage:.1f}%")
        add(f"- 发现缺陷：{m.defect_count} 个")
        add(f"- 涉及字段：{m.field_count} 个")
        add(f"- 执行耗时：{m.duration:.2f}s")
        add("")

        add("## 一、发现缺陷明细")
        add("")
        if not m.defects:
            add("无。")
        else:
            add("| # | 严重度 | 缺陷 | 设计依据 | 命中用例数 | 修复建议 |")
            add("|---|---|---|---|---|---|")
            for i, d in enumerate(m.defects, 1):
                add(
                    f"| {i} | {d['severity']} | {d['title']} | `{d['basis']}` "
                    f"| {len(d['cases'])} | {d['suggestion']} |"
                )
        add("")

        add("## 二、命中用例")
        add("")
        if not m.defects:
            add("无。")
        else:
            for d in m.defects:
                add(f"- **{d['title']}**：{', '.join('`' + c + '`' for c in d['cases'])}")
        add("")

        add("## 三、其余失败（非真实缺陷）")
        add("")
        others = [f for f in m.findings if f.category != REAL_DEFECT]
        if not others:
            add("无。")
        else:
            for f in others:
                add(f"- `{f.case_id}` → {f.category}：{f.title}")
        add("")

        add("## 四、设计方法覆盖")
        add("")
        add("| 设计依据 | 用例数 |")
        add("|---|---|")
        for basis, count in m.basis_summary.items():
            add(f"| `{basis}` | {count} |")
        add("")

        add("## 五、字段级覆盖")
        add("")
        add("| 字段 | 覆盖的设计方法 |")
        add("|---|---|")
        for name, bases in m.field_coverage.items():
            add(f"| `{name}` | {', '.join(bases)} |")
        add("")

        add("## 六、如何解读这份报告")
        add("")
        add("- **真实缺陷**：期望被拦截却放行，或期望成功却失败。这类需要建单跟进。")
        add("- **契约变更**：实现了但契约没写（或反之），需要产品/开发确认以哪边为准。")
        add("- **环境问题**：连接失败、依赖超时，重跑即可，不计入缺陷。")
        add("- **用例问题**：生成器自身的假阳性，需要回修规则引擎 —— 这个数字越低越好。")
        add("")
        add(f"> 本次疑似用例问题（假阳性）：{m.suspected_false_positives} 条。")
        add("")

        return "\n".join(lines)

    def write(self, snapshot: MetricsSnapshot, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.render_markdown(snapshot), encoding="utf-8")
        return target
