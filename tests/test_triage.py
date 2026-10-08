"""失败归因单元测试。

这个模块决定「报出来的东西算不算缺陷」。它算错的方式有两种，都很致命：

  - 把环境问题、数据缺失算成缺陷 → 假阳性，报告没人敢信；
  - 把真缺陷归成「待人工确认」→ 假阴性，工具白装。

端到端门禁只能给出「4 个缺陷 / 假阳性 0」的汇总结论，
定位不到是「哪一条规则被改坏了」，所以这里逐条钉住。
"""
from __future__ import annotations

import pytest

from smarttest.triage import (
    CASE_ISSUE,
    CONTRACT_DRIFT,
    DATA_ISSUE,
    ENV_ISSUE,
    NEEDS_REVIEW,
    REAL_DEFECT,
    Finding,
    Triage,
    _coarse,
    _field_of,
    _lookup_pattern,
)


@pytest.fixture
def triage() -> Triage:
    return Triage()


class TestEnvironmentIssues:
    """环境问题绝不能被算成缺陷 —— 这是假阳性最大的来源。"""

    @pytest.mark.parametrize(
        "message",
        [
            "httpx.ConnectError: [Errno 111] Connection refused",
            "httpx.ConnectTimeout: timed out",
            "httpx.ReadTimeout: read timeout",
            "Max retries exceeded with url",
        ],
    )
    def test_connection_failures_are_env_issues(self, triage, make_summary, message):
        findings = triage.classify(make_summary(("test_x", message)))
        assert len(findings) == 1
        assert findings[0].category == ENV_ISSUE
        assert findings[0].severity == "low"
        assert findings[0].basis == "environment"

    def test_env_keyword_wins_over_a_real_marker(self, triage, make_summary, marker):
        # 消息里同时出现连接错误和用例标记时，按环境问题处理：
        # 连不上服务的时候，断言本身没被执行，谈不上「契约不符」。
        message = marker("createOrder.quantity.minimum-1", "boundary:minimum-1=0", 422, 0)
        message += " | httpx.ConnectError: Connection refused"
        findings = triage.classify(make_summary(("test_x", message)))
        assert findings[0].category == ENV_ISSUE

    def test_unrecognised_failure_goes_to_manual_review(self, triage, make_summary):
        findings = triage.classify(make_summary(("test_x", "AssertionError: 说不清哪里错了")))
        assert findings[0].category == NEEDS_REVIEW
        assert findings[0].basis == "unknown"
        assert findings[0].severity == "medium"


class TestContractFindings:
    """契约层用例的期望值与实际值，决定这条失败是「缺陷」还是「契约漂移」。"""

    def test_expected_reject_but_got_through_is_a_real_defect(self, triage, make_summary, marker):
        message = marker("createOrder.quantity.minimum-1", "boundary:minimum-1=0", 422, 201)
        finding = triage.classify(make_summary(("test_x", message)))[0]
        assert finding.category == REAL_DEFECT
        assert finding.severity == "high"
        assert finding.title == "数值下界未校验"
        assert finding.field == "quantity"
        assert finding.coarse == "range"

    def test_expected_success_but_failed_is_a_real_defect(self, triage, make_summary, marker):
        message = marker("createOrder.happy_path", "baseline:valid_payload", 201, 422)
        finding = triage.classify(make_summary(("test_x", message)))[0]
        assert finding.category == REAL_DEFECT
        assert finding.title == "基准用例失败"

    def test_status_conflict_is_contract_drift(self, triage, make_summary, marker):
        # 4xx 对 4xx：服务拒绝了，只是拒绝的理由和契约写的不一样。
        # 这属于「以哪边为准」的契约问题，不是缺陷。
        message = marker("getOrder.order_id.not_found", "resource:not_found", 404, 422)
        finding = triage.classify(make_summary(("test_x", message)))[0]
        assert finding.category == CONTRACT_DRIFT
        assert finding.severity == "medium"

    def test_missing_actual_code_defaults_to_contract_drift(self, triage, make_summary, marker):
        # 连实际状态码都没解析出来，归因没有依据，只能按契约问题处理。
        message = marker("createOrder.coupon_code.null", "nullable:null", 201)
        finding = triage.classify(make_summary(("test_x", message)))[0]
        assert finding.category == CONTRACT_DRIFT

    def test_5xx_is_a_real_defect_regardless_of_expectation(self, triage, make_summary, marker):
        message = marker("createOrder.happy_path", "baseline:valid_payload", 201, 500)
        finding = triage.classify(make_summary(("test_x", message)))[0]
        assert finding.category == REAL_DEFECT
        assert finding.title == "服务内部异常"

    def test_5xx_without_expected_code_is_still_a_real_defect(self, triage, make_summary, marker):
        message = marker("createOrder.happy_path", "baseline:valid_payload", actual=503)
        finding = triage.classify(make_summary(("test_x", message)))[0]
        assert finding.category == REAL_DEFECT
        assert finding.title == "服务内部异常"

    def test_unknown_basis_falls_back_to_manual_confirmation(self, triage, make_summary, marker):
        message = marker("createOrder.x.some_new_rule", "brand_new:rule", 422, 201)
        finding = triage.classify(make_summary(("test_x", message)))[0]
        assert finding.title == "契约不符"
        assert "人工确认" in finding.suggestion

    def test_evidence_is_truncated(self, triage, make_summary, marker):
        message = marker("createOrder.happy_path", "baseline:valid_payload", 201, 422) + "x" * 1000
        finding = triage.classify(make_summary(("test_x", message)))[0]
        assert len(finding.evidence) == 400


class TestGeneratedCaseFindings:
    """生成器自己的问题必须单独归类，否则会被算成被测系统的缺陷。"""

    def test_unresolved_variable_is_a_case_issue(self, triage, make_summary, marker):
        message = marker("scn_round_trip", "consistency:round_trip", kind="generator",
                         tail="第 2 步存在未解析的变量引用")
        finding = triage.classify(make_summary(("test_x", message)))[0]
        assert finding.category == CASE_ISSUE
        assert finding.severity == "low"
        assert finding.coarse == "generator_unresolved"
        assert "用例问题" in finding.suggestion

    def test_missing_fixture_data_is_a_data_issue_not_a_defect(self, triage, make_summary, marker):
        # 这条是实测踩过的坑：模型编造了不存在的商品 ID，
        # 场景拿到 404 被算成「服务有 bug」。
        message = marker("scn_create_order", "state:lifecycle:payOrder", kind="business",
                         tail="第 2 步 payOrder 期望 201 实际 404 | 商品不存在")
        finding = triage.classify(make_summary(("test_x", message)))[0]
        assert finding.category == DATA_ISSUE
        assert finding.severity == "medium"
        assert finding.coarse == "data_missing"
        assert "测试数据字典" in finding.suggestion

    def test_404_without_missing_data_keyword_stays_a_defect(self, triage, make_summary, marker):
        # 404 本身不代表「数据没准备好」：契约文档里 404 也是合法语义。
        # 只有消息里明确说「不存在」时才降级为数据问题。
        message = marker("scn_pay", "state:lifecycle:payOrder", kind="business",
                         tail="第 2 步 payOrder 期望 409 实际 404")
        finding = triage.classify(make_summary(("test_x", message)))[0]
        assert finding.category == REAL_DEFECT

    def test_business_rule_failure_is_a_real_defect(self, triage, make_summary, marker):
        message = marker("total_price_precision", "money:exact_to_cent", kind="business",
                         tail="单价 0.1 x 3 期望 0.3，实际 0.30000000000000004")
        finding = triage.classify(make_summary(("test_x", message)))[0]
        assert finding.category == REAL_DEFECT
        assert finding.title == "金额精度错误"
        assert finding.coarse == "money"


class TestRootCauseMerging:
    """一个 bug 只能报一行 —— 归并错了，报告条数会虚高好几倍。"""

    def test_same_field_and_category_merge_into_one_entry(self, triage, make_summary, marker):
        summary = make_summary(
            ("test_a", marker("createOrder.quantity.minimum-1", "boundary:minimum-1=0", 422, 201)),
            ("test_b", marker("createOrder.quantity.maximum+1", "boundary:maximum+1=1000", 422, 201)),
        )
        defects = Triage.merge_by_root_cause(triage.classify(summary))
        assert len(defects) == 1
        assert defects[0]["title"] == "数值范围未校验"
        assert sorted(defects[0]["cases"]) == [
            "createOrder.quantity.maximum+1",
            "createOrder.quantity.minimum-1",
        ]

    def test_different_fields_do_not_merge(self, triage, make_summary, marker):
        summary = make_summary(
            ("test_a", marker("createOrder.quantity.minimum-1", "boundary:minimum-1=0", 422, 201)),
            ("test_b", marker("createOrder.coupon_code.maxLength+1", "boundary:maxLength+1=17", 422, 201)),
        )
        defects = Triage.merge_by_root_cause(triage.classify(summary))
        assert len(defects) == 2
        assert {d["title"] for d in defects} == {"数值范围未校验", "字符串长度未校验"}

    def test_only_real_defects_are_merged(self, triage, make_summary, marker):
        summary = make_summary(
            ("test_a", marker("createOrder.quantity.minimum-1", "boundary:minimum-1=0", 422, 201)),
            ("test_b", marker("getOrder.order_id.not_found", "resource:not_found", 404, 422)),
            ("test_c", "httpx.ConnectError: Connection refused"),
        )
        findings = triage.classify(summary)
        assert len(findings) == 3
        assert len(Triage.merge_by_root_cause(findings)) == 1

    def test_entries_sorted_by_hit_count_then_title(self, triage, make_summary, marker):
        summary = make_summary(
            ("test_a", marker("createOrder.coupon_code.maxLength+1", "boundary:maxLength+1=17", 422, 201)),
            ("test_b", marker("createOrder.quantity.minimum-1", "boundary:minimum-1=0", 422, 201)),
            ("test_c", marker("createOrder.quantity.maximum+1", "boundary:maximum+1=1000", 422, 201)),
        )
        defects = Triage.merge_by_root_cause(triage.classify(summary))
        assert [len(d["cases"]) for d in defects] == [2, 1]
        assert defects[0]["title"] == "数值范围未校验"

    def test_severity_is_taken_from_the_first_finding(self, triage, make_summary, marker):
        summary = make_summary(
            ("test_a", marker("createOrder.quantity.minimum-1", "boundary:minimum-1=0", 422, 201)),
        )
        defects = Triage.merge_by_root_cause(triage.classify(summary))
        assert defects[0]["severity"] == "high"


class TestSummaryScope:
    """只有失败/出错的用例才该进归因，通过的用例不该产生任何条目。"""

    def test_passed_and_skipped_cases_are_ignored(self, triage, make_summary, marker):
        summary = make_summary(
            ("test_ok", "", "passed"),
            ("test_skip", "", "skipped"),
            ("test_bad", marker("createOrder.quantity.minimum-1", "boundary:minimum-1=0", 422, 201)),
        )
        findings = triage.classify(summary)
        assert [f.case_id for f in findings] == ["createOrder.quantity.minimum-1"]

    def test_empty_summary_produces_nothing(self, triage, make_summary):
        assert triage.classify(make_summary()) == []


class TestHelpers:
    """归因用到的小工具：字段名切分、设计依据归一、模式库查表。"""

    @pytest.mark.parametrize(
        ("case_id", "expected"),
        [
            ("createOrder.quantity.minimum-1", "quantity"),
            ("createOrder.X-Tenant-Id.required_missing", "X-Tenant-Id"),
            ("scn_round_trip", "scn_round_trip"),
        ],
    )
    def test_field_of(self, case_id, expected):
        assert _field_of(case_id) == expected

    @pytest.mark.parametrize(
        ("basis", "expected"),
        [
            ("boundary:minimum-1", "range"),
            ("boundary:maxLength+1", "length"),
            ("required:missing_header", "required"),
            ("resource:not_found", "resource"),
            ("consistency:round_trip", "consistency:round_trip"),
        ],
    )
    def test_coarse_mapping(self, basis, expected):
        assert _coarse(basis) == expected

    def test_pattern_library_lookup(self):
        assert _lookup_pattern("idempotency:same_key")[0] == "幂等语义未实现"
        assert _lookup_pattern("state:lifecycle")[0] == "状态机流转错误"
        assert _lookup_pattern("something:else")[0] == "契约不符"

    def test_root_cause_key_combines_field_and_category(self):
        finding = Finding(
            case_id="createOrder.quantity.minimum-1",
            category=REAL_DEFECT,
            severity="high",
            title="数值下界未校验",
            basis="boundary:minimum-1=0",
            suggestion="补 minimum",
            evidence="...",
            field="quantity",
            coarse="range",
        )
        assert finding.root_cause_key == "quantity::range"
