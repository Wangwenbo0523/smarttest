"""第三靶场（内容服务）的泛化测试。

前两个靶场各有照不到的形态：订单服务没有查询参数，用户服务没有嵌套对象与
数组。这份契约专门把它们补齐，用同一套规则引擎再跑一遍。这里钉住：

  1. 生成结果与人工标注集完全对齐（召回率与精确率都是 100%）；
  2. 查询参数类用例带上正确的 params（含必填参数的联动）；
  3. 嵌套对象字段被展开成用例，且父对象不会被整块摘掉；
  4. 契约声明必填认证头时，每条用例都带上它；
  5. 渲染出来的契约测试确实会把 params 发出去（模板漏传就等于白生成）。
"""
from __future__ import annotations

import pytest

from smarttest.dataprovider import DataProvider
from smarttest.generator import PytestGenerator
from smarttest.parser import OpenApiParser
from smarttest.rules import RuleEngine
from smarttest.targets import get_target


@pytest.fixture(scope="module")
def articles_target():
    return get_target("articles")


@pytest.fixture(scope="module")
def articles_spec(articles_target):
    return OpenApiParser.from_file(articles_target.contract).parse()


@pytest.fixture(scope="module")
def articles_dp(articles_target):
    return DataProvider.load(articles_target.dataset)


@pytest.fixture(scope="module")
def articles_cases(articles_spec, articles_dp):
    return RuleEngine(articles_dp).generate(articles_spec)


class TestContractShape:
    """第三份契约的形态必须真的带来新东西，否则泛化验证是空的。"""

    def test_operations_are_parsed(self, articles_spec):
        assert [op.operation_id for op in articles_spec.operations] == [
            "listArticles",
            "createArticle",
            "getArticle",
        ]
        assert articles_spec.version == "3.0.0"

    def test_contract_has_query_parameters(self, articles_spec):
        names = [p.name for p in articles_spec.find("listArticles").query_params()]
        assert names == ["workspace", "sort", "tag", "keyword", "page", "page_size"]
        # 必填的查询参数是新的联动点：其余用例都得分摊到它
        required = [p.name for p in articles_spec.find("listArticles").query_params() if p.required]
        assert required == ["workspace"]

    def test_contract_has_nested_object_and_array(self, articles_spec):
        properties = articles_spec.find("createArticle").body_schema["properties"]
        assert properties["author"]["type"] == "object"
        assert properties["author"]["required"] == ["name", "email"]
        assert properties["tags"]["type"] == "array"

    def test_contract_declares_bearer_auth_header(self, articles_spec):
        authorization = next(
            p for p in articles_spec.find("createArticle").header_params() if p.name == "Authorization"
        )
        assert authorization.required is True
        # 令牌取值来自契约里写的 example —— 必填头不能凭空造一个过不了校验的值
        assert authorization.schema["example"].startswith("Bearer ")


class TestGenerationParity:
    def test_eval_signatures_match_exactly(self):
        from smarttest import evaluation

        target = get_target("articles")
        expected = evaluation.load_ground_truth(target)
        generated = evaluation.collect_generated(target)
        assert set(expected) - set(generated) == set(), "有该测却没生成的用例"
        assert set(generated) - set(expected) == set(), "生成了标注集以外的用例"
        assert len(expected) == 57

    def test_total_case_count_is_stable(self, articles_cases):
        assert len(articles_cases) == 61

    def test_dataset_values_respect_declared_length_bounds(
        self, articles_spec, articles_dp, dataset_bounds_checker
    ):
        assert dataset_bounds_checker(articles_spec, articles_dp) == []


class TestQueryParamCoverage:
    def test_every_list_case_carries_the_required_workspace(self, articles_cases):
        list_cases = [c for c in articles_cases if c.operation_id == "listArticles"]
        assert list_cases
        for case in list_cases:
            if case.case_id == "listArticles.workspace.required_missing":
                assert case.params == {}
                continue
            if case.case_id.startswith("listArticles.workspace."):
                # 专门测 workspace 自身的用例会改取值，但不会不传
                assert case.params.get("workspace"), case.case_id
                continue
            assert case.params.get("workspace") == "aa", case.case_id

    def test_query_case_values_keep_their_declared_types(self, articles_cases):
        cases = {c.case_id: c for c in articles_cases}
        # 数值参数用真正的数字，字符串参数用字符串 —— 生成的就是将要发出去的东西
        assert cases["listArticles.page.minimum"].params["page"] == 1
        assert cases["listArticles.page_size.maximum"].params["page_size"] == 100
        assert cases["listArticles.keyword.maxLength"].params["keyword"] == "a" * 32
        assert cases["listArticles.workspace.maxLength"].params["workspace"] == "a" * 24

    def test_rendered_contract_test_actually_sends_params(self, articles_spec, articles_cases, tmp_path):
        # 模板漏传 params 的话，用例生成了也不会生效 —— 这种「静默失效」必须被钉住
        generator = PytestGenerator(base_url="http://127.0.0.1:8125")
        path = generator.render_contract_tests(articles_spec, articles_cases, tmp_path)
        code = path.read_text(encoding="utf-8")
        assert 'params=case["params"]' in code
        assert "'workspace': 'aa'" in code


class TestNestedFieldCoverage:
    def test_nested_fields_are_generated(self, articles_cases):
        ids = {c.case_id for c in articles_cases}
        for expected in (
            "createArticle.author.name.required_missing",
            "createArticle.author.name.maxLength+1",
            "createArticle.author.email.minLength-1",
        ):
            assert expected in ids

    def test_nested_required_missing_keeps_the_parent(self, articles_cases):
        cases = {c.case_id: c for c in articles_cases}
        payload = cases["createArticle.author.name.required_missing"].payload
        assert set(payload["author"]) == {"email"}
        assert "author" not in cases["createArticle.author.required_missing"].payload

    def test_author_header_present_on_every_create_case(self, articles_cases):
        create_cases = [c for c in articles_cases if c.operation_id == "createArticle"]
        assert create_cases
        for case in create_cases:
            if case.case_id == "createArticle.Authorization.required_missing":
                assert "Authorization" not in case.headers
                continue
            if case.case_id.startswith("createArticle.Authorization."):
                # 长度边界用例会改令牌取值，但仍然是「带上了认证头」
                assert case.headers.get("Authorization"), case.case_id
                continue
            # 取值来自契约的 example，服务端能通过 Bearer 校验
            assert case.headers.get("Authorization") == "Bearer smarttest-token", case.case_id
