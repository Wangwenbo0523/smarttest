"""度量模块单元测试。

报告里对外承诺的每个数字（通过率、接口覆盖率、缺陷数、假阳性数、字段数）
都由这个模块算出来。数字错了不会报错，只会安静地写进报告 ——
所以边界（空集、除零、只有失败）必须被测试钉住。
"""
from __future__ import annotations

import pytest

from smarttest.metrics import MetricsReporter, MetricsSnapshot
from smarttest.rules import RuleEngine
from smarttest.triage import Triage


@pytest.fixture
def generated(spec, dp):
    """第一靶场的全部契约用例（25 条）。"""
    return RuleEngine(dp).generate(spec)


def build(spec, cases, summary) -> MetricsSnapshot:
    return MetricsReporter(spec, cases, summary, Triage()).build()


class TestEmptyAndBoundaryInputs:
    """空输入不能炸，也不能算出误导性的数字。"""

    def test_pass_rate_is_zero_without_cases(self):
        assert MetricsSnapshot().pass_rate == 0.0

    def test_api_coverage_is_zero_without_operations(self):
        assert MetricsSnapshot().api_coverage == 0.0

    def test_pass_rate_with_all_cases_failing(self, spec, generated, make_summary):
        summary = make_summary(("test_x", "whatever"))
        # 用例数为 0 时用生成数兜底，通过数 0 → 通过率 0%
        snapshot = build(spec, generated, summary)
        assert snapshot.cases_passed == 0
        assert snapshot.pass_rate == 0.0

    def test_zero_defects_and_zero_false_positives(self, spec, generated, make_summary):
        snapshot = build(spec, generated, make_summary())
        assert snapshot.defect_count == 0
        assert snapshot.suspected_false_positives == 0


class TestCounting:
    def test_cases_total_falls_back_to_generated_cases(self, spec, generated, make_summary):
        # 执行器报告 0 条（例如 junit 没落盘）时，用生成数量兜底，
        # 否则通过率会变成 0/0 这种看不出问题的数字。
        snapshot = build(spec, generated, make_summary())
        assert snapshot.cases_total == len(generated) == 25

    def test_cases_total_prefers_executed_count(self, spec, generated, make_summary):
        summary = make_summary(
            ("a", "", "passed"), ("b", "", "passed"), ("c", "", "errors")
        )
        snapshot = build(spec, generated, summary)
        assert snapshot.cases_total == 3
        assert snapshot.cases_passed == 2
        assert snapshot.cases_errors == 1
        assert snapshot.pass_rate == pytest.approx(66.7, abs=0.1)

    def test_api_coverage_counts_covered_operations(self, spec, generated, make_summary):
        only_create = [c for c in generated if c.operation_id == "createOrder"]
        snapshot = build(spec, only_create, make_summary())
        assert snapshot.operations_total == 4
        assert snapshot.operations_covered == 1
        assert snapshot.api_coverage == 25.0

    def test_full_coverage_when_every_operation_has_cases(self, spec, generated, make_summary):
        snapshot = build(spec, generated, make_summary())
        assert snapshot.operations_covered == 4
        assert snapshot.api_coverage == 100.0

    def test_spec_metadata_is_carried_over(self, spec, generated, make_summary):
        snapshot = build(spec, generated, make_summary())
        assert snapshot.spec_title == spec.title
        assert snapshot.spec_version == spec.version


class TestDesignMethodCoverage:
    def test_basis_summary_counts_each_design_method(self, spec, generated, make_summary):
        snapshot = build(spec, generated, make_summary())
        assert snapshot.basis_summary["baseline:valid_payload"] == 1
        # 上界+1 命中 6 条：createOrder 的 product_id / coupon_code / Idempotency-Key，
        # 加上 getOrder、payOrder、getProduct 各一条路径参数。
        assert snapshot.basis_summary["boundary:maxLength+1"] == 6
        assert sum(snapshot.basis_summary.values()) == len(generated)

    def test_basis_summary_is_sorted_by_count_desc(self, spec, generated, make_summary):
        snapshot = build(spec, generated, make_summary())
        counts = list(snapshot.basis_summary.values())
        assert counts == sorted(counts, reverse=True)

    def test_field_coverage_groups_design_methods_by_field(self, spec, generated, make_summary):
        snapshot = build(spec, generated, make_summary())
        assert "quantity" in snapshot.field_coverage
        assert snapshot.field_coverage["quantity"] == sorted(snapshot.field_coverage["quantity"])
        assert "boundary:maximum+1" in snapshot.field_coverage["quantity"]
        assert "required:missing" in snapshot.field_coverage["quantity"]

    def test_baseline_case_is_not_counted_as_a_field(self, spec, generated, make_summary):
        # happy_path 不对应任何字段，不能污染字段级覆盖
        snapshot = build(spec, generated, make_summary())
        assert "happy_path" not in snapshot.field_coverage
        assert snapshot.field_count == len(snapshot.field_coverage)


class TestDefectsAndFalsePositives:
    def test_defect_count_counts_merged_defects_not_failed_cases(
        self, spec, generated, make_summary, marker
    ):
        # 同一个字段的下界-1 和上界+1 属于一个缺陷，报告只能算 1 条 ——
        # 否则一个 bug 会刷出好几行，可信度直接崩塌。
        summary = make_summary(
            (
                "test_min",
                marker("createOrder.quantity.minimum-1", "boundary:minimum-1=0", 422, 201),
            ),
            (
                "test_max",
                marker("createOrder.quantity.maximum+1", "boundary:maximum+1=1000", 422, 201),
            ),
        )
        snapshot = build(spec, generated, summary)
        assert len(snapshot.findings) == 2
        assert snapshot.defect_count == 1
        assert snapshot.defects[0]["cases"] == [
            "createOrder.quantity.minimum-1",
            "createOrder.quantity.maximum+1",
        ]

    def test_case_issues_are_counted_as_suspected_false_positives(
        self, spec, generated, make_summary, marker
    ):
        summary = make_summary(
            (
                "test_bad",
                marker("createOrder.quantity.minimum-1", "boundary:minimum-1=0", 422, 201),
            ),
            (
                "test_gen",
                marker("scn_x", "consistency:round_trip", kind="generator", tail="未解析变量"),
            ),
        )
        snapshot = build(spec, generated, summary)
        assert snapshot.defect_count == 1
        assert snapshot.suspected_false_positives == 1

    def test_env_failures_do_not_become_defects(self, spec, generated, make_summary):
        summary = make_summary(("test_env", "httpx.ConnectError: Connection refused"))
        snapshot = build(spec, generated, summary)
        assert snapshot.defect_count == 0
        assert snapshot.suspected_false_positives == 0
        assert snapshot.findings[0].category == "环境问题"


class TestMarkdownRendering:
    def test_report_contains_the_required_sections(self, spec, generated, make_summary):
        snapshot = build(spec, generated, make_summary())
        text = MetricsReporter(spec, generated, make_summary(), Triage()).render_markdown(snapshot)
        for section in (
            "# SmartTest 质量报告",
            "## 一、发现缺陷明细",
            "## 二、命中用例",
            "## 三、其余失败（非真实缺陷）",
            "## 四、设计方法覆盖",
            "## 五、字段级覆盖",
            "## 六、如何解读这份报告",
        ):
            assert section in text

    def test_report_states_no_defects_when_clean(self, spec, generated, make_summary):
        text = MetricsReporter(spec, generated, make_summary(), Triage()).render_markdown(
            build(spec, generated, make_summary())
        )
        assert "- 发现缺陷：0 个" in text
        assert "本次疑似用例问题（假阳性）：0 条。" in text

    def test_report_lists_defects_and_hit_cases(self, spec, generated, make_summary, marker):
        summary = make_summary(
            ("test_a", marker("createOrder.quantity.minimum-1", "boundary:minimum-1=0", 422, 201))
        )
        reporter = MetricsReporter(spec, generated, summary, Triage())
        text = reporter.render_markdown(reporter.build())
        assert "数值范围未校验" in text
        assert "`createOrder.quantity.minimum-1`" in text
        assert "- 发现缺陷：1 个" in text

    def test_report_lists_non_defect_failures_separately(self, spec, generated, make_summary):
        summary = make_summary(("test_env", "httpx.ConnectError: Connection refused"))
        reporter = MetricsReporter(spec, generated, summary, Triage())
        text = reporter.render_markdown(reporter.build())
        assert "环境问题" in text

    def test_write_creates_missing_directories(self, spec, generated, make_summary, tmp_path):
        summary = make_summary()
        reporter = MetricsReporter(spec, generated, summary, Triage())
        target = tmp_path / "nested" / "deeper" / "quality_report.md"
        written = reporter.write(reporter.build(), target)
        assert written == target
        assert target.exists()
        assert "SmartTest 质量报告" in target.read_text(encoding="utf-8")
