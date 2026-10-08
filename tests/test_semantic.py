"""语义增强层单元测试。

这一层是「模型只做加法、不做减法」的落点：模型产出的场景必须先过结构校验
与契约交叉核对才能进测试套件。它的价值全在**拦截**上 ——
拦住幻觉、拦住重复、拦住不一致的期望值。拦不住，模型编出来的接口就会变成
一条指向被测系统的假缺陷，而这是最难查的一类假阳性。

所以在单测里，重点不是「正常场景能过」，而是「各种不合格的场景必须被拦下，
并且带上可读的原因」。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from smarttest.semantic.enhancer import SemanticEnhancer
from smarttest.semantic.models import RESPONSE_SCHEMA, ScenarioCase, ScenarioStep
from smarttest.semantic.providers import (
    LLMUnavailable,
    OpenAICompatibleProvider,
    RuleBasedScenarioProvider,
    build_context,
    build_provider,
)


def make_scenario(**overrides) -> dict:
    """一份结构上完全合格的场景，用例按需覆盖其中一项来制造不合格。"""
    scenario = {
        "scenario_id": "round_trip_createOrder_getOrder",
        "title": "创建订单后查询详情",
        "rationale": "consistency:round_trip:getOrder",
        "source": "llm",
        "steps": [
            {
                "operation_id": "createOrder",
                "body": {"product_id": "P001", "quantity": 1},
                "expect_status": 201,
                "capture": {"order_id": "$.order_id"},
            },
            {
                "operation_id": "getOrder",
                "path_params": {"order_id": "{{order_id}}"},
                "expect_status": 200,
            },
        ],
    }
    scenario.update(overrides)
    return scenario


class FakeProvider:
    """假模型：按脚本逐个返回批次，用不着真的连模型。"""

    name = "llm"

    def __init__(self, *batches):
        self.batches = list(batches)
        self.feedbacks: list[list[str] | None] = []

    def generate(self, spec, dp, feedback=None):
        self.feedbacks.append(feedback)
        if not self.batches:
            return []
        batch = self.batches.pop(0)
        if isinstance(batch, Exception):
            raise batch
        return batch


class TestScenarioModel:
    """结构校验：模型输出必须是数据，且必须落在封闭的表达空间里。"""

    def test_valid_scenario_is_accepted(self):
        case = ScenarioCase.model_validate(make_scenario())
        assert case.scenario_id == "round_trip_createOrder_getOrder"
        assert len(case.steps) == 2

    def test_reversed_capture_direction_is_corrected(self):
        # 实测模型会输出 {"$.order_id": "order_id"}，方向反了。
        # JSONPath 一定以 $ 开头，所以这种错可以安全纠正。
        step = ScenarioStep.model_validate(
            {
                "operation_id": "createOrder",
                "expect_status": 201,
                "capture": {"$.order_id": "order_id"},
            }
        )
        assert step.capture == {"order_id": "$.order_id"}

    def test_capture_direction_is_left_alone_when_correct(self):
        step = ScenarioStep.model_validate(
            {
                "operation_id": "createOrder",
                "expect_status": 201,
                "capture": {"order_id": "$.order_id"},
            }
        )
        assert step.capture == {"order_id": "$.order_id"}

    def test_blank_operation_id_is_rejected(self):
        with pytest.raises(ValidationError, match="operation_id 不能为空"):
            ScenarioStep.model_validate({"operation_id": "   ", "expect_status": 200})

    def test_single_step_scenario_is_rejected(self):
        # 单步验证属于契约层，不该混进场景层
        with pytest.raises(ValidationError, match="至少需要 2 步"):
            ScenarioCase.model_validate(make_scenario(steps=make_scenario()["steps"][:1]))

    def test_too_many_steps_are_rejected(self):
        steps = [make_scenario()["steps"][1] for _ in range(9)]
        with pytest.raises(ValidationError, match="不能超过 8 步"):
            ScenarioCase.model_validate(make_scenario(steps=steps))

    def test_free_text_rationale_is_normalized(self):
        # rationale 会被归因模块拿去匹配缺陷模式库，中文长句会让它落进兜底分支
        case = ScenarioCase.model_validate(make_scenario(rationale="验证一下创建之后再查询"))
        assert case.rationale == "scenario:llm_generated"

    def test_response_schema_requires_the_essential_fields(self):
        items = RESPONSE_SCHEMA["properties"]["scenarios"]["items"]
        assert set(items["required"]) == {"scenario_id", "title", "rationale", "steps"}


class TestEnhancerSelection:
    """交叉核对：编造的接口、没声明的状态码、缺路径参数，一个都不能放行。"""

    def test_valid_scenario_is_kept(self, spec, dp):
        provider = FakeProvider([make_scenario()])
        result = SemanticEnhancer(provider, dp).enhance(spec)
        assert [s.scenario_id for s in result.scenarios] == ["round_trip_createOrder_getOrder"]
        assert result.rejected == []

    def test_unknown_operation_id_is_rejected(self, spec, dp):
        broken = make_scenario()
        broken["steps"][1]["operation_id"] = "getOrderDetailV2"
        result = SemanticEnhancer(FakeProvider([broken]), dp).enhance(spec)
        assert result.scenarios == []
        assert "不存在的 operationId" in result.rejected[0].reason

    def test_undeclared_status_code_is_rejected(self, spec, dp):
        broken = make_scenario()
        broken["steps"][1]["expect_status"] = 418
        result = SemanticEnhancer(FakeProvider([broken]), dp).enhance(spec)
        assert "未在契约中声明" in result.rejected[0].reason

    def test_missing_path_param_is_rejected(self, spec, dp):
        broken = make_scenario()
        broken["steps"][1]["path_params"] = {}
        result = SemanticEnhancer(FakeProvider([broken]), dp).enhance(spec)
        assert "缺少必需的路径参数" in result.rejected[0].reason

    def test_structural_failure_is_rejected_with_location(self, spec, dp):
        result = SemanticEnhancer(FakeProvider([{"scenario_id": "x"}]), dp).enhance(spec)
        assert "结构校验失败" in result.rejected[0].reason

    def test_duplicate_id_in_the_same_batch_is_rejected(self, spec, dp):
        result = SemanticEnhancer(FakeProvider([make_scenario(), make_scenario()]), dp).enhance(spec)
        assert len(result.scenarios) == 1
        assert "重复" in result.rejected[0].reason

    def test_same_shape_with_a_new_id_is_rejected(self, spec, dp):
        # 换个 ID、换个数量重复讲同一个故事：ID 去重拦不住，骨架去重才拦得住
        first = make_scenario()
        second = make_scenario(
            scenario_id="round_trip_createOrder_getOrder_copy",
            title="换个说法再讲一遍",
        )
        second["steps"][0]["body"] = {"product_id": "P002", "quantity": 3}
        result = SemanticEnhancer(FakeProvider([first, second]), dp).enhance(spec)
        assert len(result.scenarios) == 1
        assert "步骤骨架一致" in result.rejected[0].reason

    def test_already_accepted_id_is_skipped_silently(self, spec, dp):
        # 回灌重试时模型经常把上一轮已经合格的场景再交一遍 —— 这是正常重复，
        # 不该报成「被拒绝」，否则报告里全是噪声。
        good = make_scenario()
        hallucinated = make_scenario(scenario_id="hallucinated")
        hallucinated["steps"][1]["operation_id"] = "编造的接口"
        recovered = make_scenario(
            scenario_id="lifecycle_createOrder_payOrder",
            title="支付并重复支付",
            rationale="state:lifecycle:payOrder",
            steps=[
                {
                    "operation_id": "createOrder",
                    "body": {"product_id": "P001", "quantity": 1},
                    "expect_status": 201,
                    "capture": {"order_id": "$.order_id"},
                },
                {
                    "operation_id": "payOrder",
                    "path_params": {"order_id": "{{order_id}}"},
                    "expect_status": 200,
                },
            ],
        )
        provider = FakeProvider([good, hallucinated], [good, recovered])

        result = SemanticEnhancer(provider, dp).enhance(spec)

        assert [s.scenario_id for s in result.scenarios] == [
            "round_trip_createOrder_getOrder",
            "lifecycle_createOrder_payOrder",
        ]
        assert result.retry_recovered == 1
        # 关键：已采纳的 ID 不会被记成「被拒绝」
        assert [r.scenario_id for r in result.rejected] == ["hallucinated"]

    def test_enum_expectation_is_normalized_case_insensitively(self, spec, dp):
        scenario = make_scenario(
            scenario_id="status_enum",
            steps=[
                {
                    "operation_id": "createOrder",
                    "body": {"product_id": "P001", "quantity": 1},
                    "expect_status": 201,
                    "expect_body": {"$.status": "created"},
                },
                {
                    "operation_id": "getOrder",
                    "path_params": {"order_id": "O-NOT-EXIST-9999"},
                    "expect_status": 404,
                },
            ],
        )
        result = SemanticEnhancer(FakeProvider([scenario]), dp).enhance(spec)
        assert result.rejected == []
        assert result.scenarios[0].steps[0].expect_body["$.status"] == "CREATED"

    def test_unmatched_enum_expectation_is_rejected(self, spec, dp):
        scenario = make_scenario(
            scenario_id="status_enum_bad",
            steps=[
                {
                    "operation_id": "createOrder",
                    "body": {"product_id": "P001", "quantity": 1},
                    "expect_status": 201,
                    "expect_body": {"$.status": "PAID_MAYBE"},
                },
                {
                    "operation_id": "getOrder",
                    "path_params": {"order_id": "O-NOT-EXIST-9999"},
                    "expect_status": 404,
                },
            ],
        )
        result = SemanticEnhancer(FakeProvider([scenario]), dp).enhance(spec)
        assert "不在契约声明的枚举" in result.rejected[0].reason


class TestEnhancerDegradation:
    """模型不可用时要降级而不是失败 —— 覆盖率不能因为模型挂了就塌成 0。"""

    def test_model_failure_degrades_to_rule_based(self, spec, dp):
        provider = FakeProvider(LLMUnavailable("模型接口返回 500"))
        result = SemanticEnhancer(provider, dp).enhance(spec)
        assert result.provider_name == "rule-based"
        assert "500" in result.degraded_reason
        assert result.scenarios, "降级后仍然要有场景，不能塌成 0"

    def test_retry_with_feedback_recovers_scenarios(self, spec, dp):
        bad = make_scenario()
        bad["steps"][1]["operation_id"] = "编造的接口"
        provider = FakeProvider([bad], [make_scenario()])
        result = SemanticEnhancer(provider, dp).enhance(spec)
        assert result.retry_attempted is True
        assert result.retry_recovered == 1
        assert result.rejected == []
        assert provider.feedbacks[1], "重试时必须把「上一轮哪里不合格」带回去"

    def test_failed_retry_keeps_the_first_round(self, spec, dp):
        bad = make_scenario(scenario_id="bad_one")
        bad["steps"][1]["operation_id"] = "编造的接口"
        provider = FakeProvider([bad], LLMUnavailable("重试时模型也挂了"))
        result = SemanticEnhancer(provider, dp).enhance(spec)
        assert result.retry_attempted is True
        assert result.retry_recovered == 0
        assert "重试失败" in result.degraded_reason
        assert result.rejected  # 首轮被拒的场景仍然如实上报

    def test_degraded_reason_is_empty_for_rule_based_provider(self, spec, dp):
        result = SemanticEnhancer(RuleBasedScenarioProvider(), dp).enhance(spec)
        assert result.provider_name == "rule-based"
        assert result.degraded_reason == ""
        assert result.retry_attempted is False


class TestRuleBasedProvider:
    """规则推导必须是确定性兜底：不依赖模型，也能推出跨接口场景。"""

    def test_orders_contract_yields_lifecycle_and_round_trip(self, spec, dp):
        scenarios = RuleBasedScenarioProvider().generate(spec, dp)
        ids = {s["scenario_id"] for s in scenarios}
        assert ids == {
            "lifecycle_createOrder_payOrder",
            "round_trip_createOrder_getOrder",
        }
        rationales = {s["rationale"].split(":")[0] for s in scenarios}
        assert rationales == {"state", "consistency"}

    def test_lifecycle_uses_the_semantic_conflict_code(self, spec, dp):
        scenarios = {s["scenario_id"]: s for s in RuleBasedScenarioProvider().generate(spec, dp)}
        lifecycle = scenarios["lifecycle_createOrder_payOrder"]
        # 重复操作应被拒绝，这里必须挑 409 而不是「最小的 4xx」
        assert lifecycle["steps"][-1]["expect_status"] == 409

    def test_required_headers_are_written_into_scenario_steps(self, target_assets):
        articles_spec, articles_dp = target_assets("articles")
        scenarios = RuleBasedScenarioProvider().generate(articles_spec, articles_dp)
        assert len(scenarios) == 1
        creator_step = scenarios[0]["steps"][0]
        # 漏掉必填认证头，场景会先被 422 拦下，然后被误判成服务缺陷
        assert creator_step["headers"]["Authorization"].startswith("Bearer ")

    def test_contract_without_creator_yields_no_scenarios(self):
        from smarttest.ir import ApiSpec, Operation

        spec = ApiSpec(
            title="只有查询接口",
            version="1",
            operations=[
                Operation(
                    operation_id="getThing",
                    method="GET",
                    path="/api/v1/things/{thing_id}",
                    tag="t",
                    summary="",
                    responses={"200": {"description": "ok"}},
                )
            ],
        )
        assert RuleBasedScenarioProvider().generate(spec, None) == []


class TestProviderSelection:
    def test_without_api_key_it_falls_back_to_rules(self, monkeypatch, capsys):
        monkeypatch.delenv("SMARTTEST_LLM_API_KEY", raising=False)
        provider = build_provider()
        assert isinstance(provider, RuleBasedScenarioProvider)
        assert "未配置 SMARTTEST_LLM_API_KEY" in capsys.readouterr().out

    def test_blank_api_key_is_treated_as_missing(self, monkeypatch):
        monkeypatch.setenv("SMARTTEST_LLM_API_KEY", "   ")
        assert isinstance(build_provider(verbose=False), RuleBasedScenarioProvider)

    def test_with_api_key_it_builds_the_openai_provider(self, monkeypatch):
        monkeypatch.setenv("SMARTTEST_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("SMARTTEST_LLM_BASE_URL", "https://api.example.com/v1/")
        monkeypatch.setenv("SMARTTEST_LLM_MODEL", "deepseek-chat")
        provider = build_provider()
        assert isinstance(provider, OpenAICompatibleProvider)
        assert provider.base_url == "https://api.example.com/v1"  # 尾部斜杠被规整
        assert provider.model == "deepseek-chat"

    def test_unreachable_endpoint_raises_llm_unavailable(self, spec, dp):
        provider = OpenAICompatibleProvider(
            base_url="http://127.0.0.1:1/v1", api_key="sk-test", model="m", timeout=2
        )
        with pytest.raises(LLMUnavailable, match="无法访问模型接口"):
            provider.generate(spec, dp)

    def test_build_context_exposes_data_and_documented_statuses(self, spec, dp):
        context = build_context(spec, dp)
        operations = {op["operationId"]: op for op in context["operations"]}
        assert operations["createOrder"]["documented_statuses"] == [201, 404, 422]
        assert operations["createOrder"]["header_params"] == ["Idempotency-Key"]
        # 真实可用的测试数据必须显式给出，否则模型一定会自己编
        assert context["available_test_data"] == {"product_id": ["P001", "P002", "P003"]}
