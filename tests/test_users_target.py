"""第二靶场（用户与订阅服务）的泛化测试。

这个靶场存在的全部理由，是回答「换一份契约还灵不灵」。
只在一个契约上跑出召回 100% 说明不了什么 —— 那可能是给订单服务量身定做的。
这里钉住四件事：

  1. 生成结果与人工标注集**完全**对齐（召回率与精确率都是 100%）；
  2. 用例清单稳定（数量 + 签名集合，改规则会在这里显形）；
  3. 契约声明了必填请求头时，每一条用例都带上它；
  4. 数据字典里的每个取值都满足契约声明的长度约束 ——
     值超界会让「资源不存在」用例变成「参数非法」用例，制造假缺陷。
"""
from __future__ import annotations

import importlib

import pytest

from smarttest.dataprovider import DataProvider
from smarttest.generator import TEMPLATE_DIR
from smarttest.parser import OpenApiParser
from smarttest.rules import RuleEngine
from smarttest.targets import TARGETS, get_target, target_keys


@pytest.fixture(scope="module")
def users_target():
    return get_target("users")


@pytest.fixture(scope="module")
def users_spec(users_target):
    return OpenApiParser.from_file(users_target.contract).parse()


@pytest.fixture(scope="module")
def users_dp(users_target):
    return DataProvider.load(users_target.dataset)


@pytest.fixture(scope="module")
def users_cases(users_spec, users_dp):
    return RuleEngine(users_dp).generate(users_spec)


class TestTargetRegistry:
    """注册表是流水线与具体领域之间唯一的接口，登记错了后面全错。"""

    def test_both_targets_are_registered(self):
        assert target_keys() == ["articles", "orders", "users"]
        assert set(TARGETS) == {"orders", "users", "articles"}

    @pytest.mark.parametrize("key", ["orders", "users", "articles"])
    def test_registered_files_exist(self, key):
        target = get_target(key)
        assert target.contract.exists(), target.contract
        assert target.dataset.exists(), target.dataset
        assert target.ground_truth.exists(), target.ground_truth
        assert (TEMPLATE_DIR / target.business_template).exists(), target.business_template

    @pytest.mark.parametrize("key", ["orders", "users", "articles"])
    def test_app_module_is_importable(self, key):
        module_name = get_target(key).app.split(":")[0]
        assert importlib.import_module(module_name) is not None

    def test_targets_use_distinct_ports_and_datasets(self):
        ports = [t.port for t in TARGETS.values()]
        datasets = [t.dataset for t in TARGETS.values()]
        contracts = [t.contract for t in TARGETS.values()]
        assert len(set(ports)) == len(ports), "两个靶场抢同一个端口会让门禁互相打架"
        assert len(set(datasets)) == len(datasets)
        assert len(set(contracts)) == len(contracts)

    def test_unknown_target_key_exits(self):
        with pytest.raises(SystemExit, match="未知靶场"):
            get_target("no-such-target")


class TestContractShape:
    """第二份契约的形态必须真的和第一份不同，否则泛化验证是空的。"""

    def test_operations_are_parsed(self, users_spec):
        assert [op.operation_id for op in users_spec.operations] == [
            "createUser",
            "getUser",
            "getPlan",
        ]
        assert users_spec.version == "2.0.0"

    def test_contract_declares_a_required_header(self, users_spec):
        # 第一靶场没有任何必填请求头 —— 这正是它照不出那个缺口的原因。
        required_headers = [
            p.name for p in users_spec.find("createUser").header_params() if p.required
        ]
        assert required_headers == ["X-Tenant-Id"]

    def test_contract_has_field_shapes_the_first_target_lacks(self, users_spec):
        properties = users_spec.find("createUser").body_schema["properties"]
        assert properties["channel"]["enum"] == ["WEB", "APP", "PARTNER"]   # 枚举
        assert properties["marketing_opt_in"]["type"] == "boolean"          # 布尔
        assert properties["age"]["type"] == "integer"                       # 数值范围
        # 路径参数下界大于 1：订单契约的路径参数下界都是 1，不会生成下界-1 用例
        assert users_spec.find("getUser").path_params()[0].schema["minLength"] == 4


class TestGenerationParity:
    """生成结果与人工标注集必须一一对上，多一条少一条都算回退。"""

    def test_eval_signatures_match_exactly(self):
        # 直接复用评测脚本的实现，保证「门禁里跑的那套」和「单测里跑的」是同一套口径。
        import run_evals

        target = get_target("users")
        expected = run_evals.load_ground_truth(target)
        generated = run_evals.collect_generated(target)
        assert set(expected) - set(generated) == set(), "有该测却没生成的用例"
        assert set(generated) - set(expected) == set(), "生成了标注集以外的用例"
        assert len(expected) == 41

    def test_total_case_count_is_stable(self, users_cases):
        assert len(users_cases) == 44

    def test_every_case_carries_a_design_basis_and_unique_id(self, users_cases):
        assert all(case.design_basis for case in users_cases)
        ids = [case.case_id for case in users_cases]
        assert len(ids) == len(set(ids))


class TestRequiredHeaderCoverage:
    def test_every_create_user_case_carries_the_tenant_header(self, users_cases):
        create_cases = [c for c in users_cases if c.operation_id == "createUser"]
        assert create_cases
        for case in create_cases:
            if case.case_id == "createUser.X-Tenant-Id.required_missing":
                # 专门测「缺必填头」的那一条，本来就该把它摘掉 —— 唯一允许缺席的用例
                assert "X-Tenant-Id" not in case.headers
                continue
            # 其余用例都必须带上它；针对该请求头自身的用例会改取值，但不会不传
            assert case.headers.get("X-Tenant-Id"), case.case_id

    def test_normal_cases_use_the_canonical_header_value(self, users_cases):
        cases = {c.case_id: c for c in users_cases}
        assert cases["createUser.happy_path"].headers == {"X-Tenant-Id": "aaaa"}
        # 边界用例只改被测的那一个请求头，别的照旧
        assert len(cases["createUser.X-Tenant-Id.maxLength+1"].headers["X-Tenant-Id"]) == 17

    def test_optional_header_cases_keep_the_required_header(self, users_cases):
        cases = {c.case_id: c for c in users_cases if c.operation_id == "createUser"}
        assert cases["createUser.X-Request-Source.maxLength"].headers["X-Tenant-Id"] == "aaaa"


class TestDatasetConsistency:
    """数据字典里的取值必须满足契约约束。

    这里踩过一个真实的坑：「资源不存在」用的值如果超出契约声明的 maxLength，
    正确实现会先返回 422（参数非法）而不是 404（资源不存在）——
    用例期望错了，修复版靶场就会报出一条不存在的缺陷。
    """

    def test_dataset_values_respect_declared_length_bounds(
        self, users_spec, users_dp, dataset_bounds_checker
    ):
        assert dataset_bounds_checker(users_spec, users_dp) == []

    def test_resource_lookups_have_both_known_and_unknown_values(self, users_dp):
        # resource:exists 与 resource:not_found 是成对的：
        # 少了任何一侧，覆盖都不完整，而且不会报错。
        assert users_dp.first("user_id") is not None
        assert users_dp.unknown_value("user_id") is not None
        assert users_dp.first("plan_code") is not None
        assert users_dp.unknown_value("plan_code") is not None
