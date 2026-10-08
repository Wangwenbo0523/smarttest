"""规则引擎单元测试。

规则引擎是整条流水线的核心：它决定「生成哪些用例、期望什么状态码」。
端到端门禁（run_demo / run_evals）只能给出「4 个缺陷、召回 100%」这类汇总结论，
某条规则被改坏时定位成本很高。这里把规则逐条钉住，让改动在 diff 里显形。

被钉住的三条设计原则（见 smarttest/rules.py 模块文档）：
1. 只做能被 schema 推导的用例；
2. 每个用例都带 design_basis 标签；
3. 区分「拒绝侧」与「接受侧」，资源引用字段不生成接受侧用例。
"""
from __future__ import annotations

import pytest

from smarttest.rules import RuleEngine, _valid_value, _wrong_type_value


def cases_of(engine: RuleEngine, spec, operation_id: str):
    """按 case_id 索引某接口的全部契约用例。"""
    return {c.case_id: c for c in engine.generate(spec) if c.operation_id == operation_id}


def ids_of(engine: RuleEngine, spec, operation_id: str) -> set[str]:
    return set(cases_of(engine, spec, operation_id))


class TestValueConstruction:
    """取值构造：错误类型的取值必须真的类型不对，合法取值必须真的合法。"""

    @pytest.mark.parametrize(
        ("schema_type", "expected_type"),
        [
            ("integer", str),
            ("number", str),
            ("string", int),
            ("boolean", str),
            ("array", str),
            ("object", str),
        ],
    )
    def test_wrong_type_value_is_actually_wrong(self, schema_type, expected_type):
        assert isinstance(_wrong_type_value({"type": schema_type}), expected_type)

    def test_wrong_type_value_falls_back_for_unknown_type(self):
        # 契约里写了没见过的 type 时不能返回 None：字段一旦变成 None，
        # 用例就从「类型错误」退化成「传了个 null」，断言依据全变。
        assert _wrong_type_value({"type": "quantum"}) is not None
        assert _wrong_type_value({}) is not None

    def test_valid_value_prefers_real_dataset_over_schema(self, dp):
        # product_id 是资源引用：必须取真实存在的值，不能按 minLength 造 "a"。
        assert _valid_value("product_id", {"type": "string", "minLength": 1}, dp) == "P001"

    def test_valid_value_prefers_enum_then_default_then_example(self, dp):
        assert _valid_value("x", {"type": "string", "enum": ["A", "B"], "default": "B"}, dp) == "A"
        assert _valid_value("x", {"type": "string", "default": "B", "example": "C"}, dp) == "B"
        assert _valid_value("x", {"type": "string", "example": "C"}, dp) == "C"

    @pytest.mark.parametrize(
        ("schema", "expected"),
        [
            ({"type": "integer", "minimum": 5}, 5),
            ({"type": "integer"}, 1),
            ({"type": "number", "minimum": 2.5}, 2.5),
            ({"type": "boolean"}, True),
            ({"type": "array"}, []),
            ({"type": "object"}, {}),
            ({"type": "string", "minLength": 3}, "aaa"),
            ({"type": "string"}, "a"),
        ],
    )
    def test_valid_value_type_defaults(self, schema, expected, dp):
        assert _valid_value("unknown_field", schema, dp) == expected

    def test_missing_dataset_value_falls_back_to_type_default(self, dp):
        # order_id 在数据字典里没有真实取值，此时按类型下界兜底。
        assert dp.first("order_id") is None
        assert _valid_value("order_id", {"type": "string"}, dp) == "a"


class TestBodyRules:
    """请求体规则：必填、类型、长度、数值、枚举、可空、资源引用。"""

    def test_happy_path_is_generated_and_expected_to_succeed(self, engine, spec):
        case = cases_of(engine, spec, "createOrder")["createOrder.happy_path"]
        assert case.expected_status == 201
        assert case.design_basis == "baseline:valid_payload"
        assert case.method == "POST"
        assert case.path == "/api/v1/orders"

    def test_required_missing_is_422(self, engine, spec):
        case = cases_of(engine, spec, "createOrder")["createOrder.quantity.required_missing"]
        assert case.expected_status == 422
        assert case.design_basis == "required:missing"
        assert "quantity" not in case.payload
        # 其余字段必须保留，否则测的是「多个字段同时缺失」，归因会指错方向。
        assert case.payload["product_id"] == "P001"

    def test_type_mismatch_is_422_and_keeps_other_fields(self, engine, spec):
        case = cases_of(engine, spec, "createOrder")["createOrder.quantity.type_mismatch"]
        assert case.expected_status == 422
        assert case.design_basis == "type:integer=wrong"
        assert not isinstance(case.payload["quantity"], int)
        assert case.payload["product_id"] == "P001"

    def test_string_length_reject_side_values(self, engine, spec):
        cases = cases_of(engine, spec, "createOrder")
        short = cases["createOrder.product_id.minLength-1"]
        long = cases["createOrder.coupon_code.maxLength+1"]
        assert short.expected_status == 422
        assert short.payload["product_id"] == ""       # minLength=1，下界-1 即空串
        assert long.expected_status == 422
        assert len(long.payload["coupon_code"]) == 17  # maxLength=16，上界+1
        assert long.design_basis == "boundary:maxLength+1=17"

    def test_string_length_accept_side_value(self, engine, spec):
        case = cases_of(engine, spec, "createOrder")["createOrder.coupon_code.maxLength"]
        assert case.expected_status == 201
        assert len(case.payload["coupon_code"]) == 16
        assert case.design_basis == "boundary:maxLength=16"

    def test_numeric_boundaries_reject_and_accept(self, engine, spec):
        cases = cases_of(engine, spec, "createOrder")
        assert cases["createOrder.quantity.minimum-1"].payload["quantity"] == 0
        assert cases["createOrder.quantity.minimum-1"].expected_status == 422
        assert cases["createOrder.quantity.maximum+1"].payload["quantity"] == 1000
        assert cases["createOrder.quantity.maximum+1"].expected_status == 422
        assert cases["createOrder.quantity.minimum"].payload["quantity"] == 1
        assert cases["createOrder.quantity.minimum"].expected_status == 201
        assert cases["createOrder.quantity.maximum"].payload["quantity"] == 999
        assert cases["createOrder.quantity.maximum"].expected_status == 201

    def test_enum_valid_values_accepted_and_invalid_rejected(self, make_operation):
        operation = make_operation(
            body_schema={
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"type": "string", "enum": ["CREATED", "PAID"]}},
            }
        )
        cases = {c.case_id: c for c in RuleEngine()._cases_for_body(operation)}
        assert cases["op.status.enum.CREATED"].payload["status"] == "CREATED"
        assert cases["op.status.enum.CREATED"].expected_status == 201
        assert cases["op.status.enum.PAID"].payload["status"] == "PAID"
        assert cases["op.status.enum.invalid"].payload["status"] == "__INVALID_ENUM__"
        assert cases["op.status.enum.invalid"].expected_status == 422

    def test_nullable_optional_field_accepts_null(self, engine, spec):
        case = cases_of(engine, spec, "createOrder")["createOrder.coupon_code.null"]
        assert case.payload["coupon_code"] is None
        assert case.expected_status == 201
        assert case.design_basis == "nullable:null"

    def test_required_nullable_field_does_not_get_null_case(self, make_operation):
        # 必填 + nullable 是自相矛盾的契约，规则引擎不猜，直接不生成该用例。
        operation = make_operation(
            body_schema={
                "type": "object",
                "required": ["note"],
                "properties": {"note": {"type": "string", "nullable": True}},
            }
        )
        assert "op.note.null" not in {c.case_id for c in RuleEngine()._cases_for_body(operation)}

    def test_resource_reference_gets_not_found_case(self, engine, spec):
        case = cases_of(engine, spec, "createOrder")["createOrder.product_id.not_found"]
        assert case.payload["product_id"] == "P-UNKNOWN-9999"
        assert case.expected_status == 404
        assert case.design_basis == "resource:not_found"

    def test_resource_reference_skips_accept_side_boundaries(self, engine, spec):
        """核心设计原则：资源引用字段不生成接受侧用例。

        product_id 的合法格式不代表资源存在，所以「长度刚好等于上界」这类
        接受侧用例不该出现 —— 它只会拿到 404，制造假阳性。
        但拒绝侧（上界+1）和「资源不存在」必须保留，否则覆盖就漏了。
        """
        ids = ids_of(engine, spec, "createOrder")
        assert "createOrder.product_id.maxLength" not in ids
        assert "createOrder.product_id.minLength" not in ids
        assert "createOrder.product_id.maxLength+1" in ids
        assert "createOrder.product_id.minLength-1" in ids
        assert "createOrder.product_id.not_found" in ids

    def test_non_resource_field_keeps_accept_side_boundaries(self, engine, spec):
        # 对照组：同一次运行里非资源字段必须照常生成接受侧用例，
        # 否则上面那条测试可能因为「规则被整体删掉」而假通过。
        ids = ids_of(engine, spec, "createOrder")
        assert "createOrder.coupon_code.maxLength" in ids
        assert "createOrder.quantity.minimum" in ids

    def test_operation_without_body_produces_no_body_cases(self, engine, spec):
        ids = ids_of(engine, spec, "getOrder")
        assert not any("happy_path" in case_id for case_id in ids)


class TestHeaderRules:
    """请求头参数规则。这一类曾经整块缺失，是被评测集点出来的。"""

    def test_optional_header_gets_boundary_cases_only(self, engine, spec):
        cases = cases_of(engine, spec, "createOrder")
        assert "createOrder.Idempotency-Key.required_missing" not in cases
        rejected = cases["createOrder.Idempotency-Key.maxLength+1"]
        assert rejected.expected_status == 422
        assert len(rejected.headers["Idempotency-Key"]) == 65
        accepted = cases["createOrder.Idempotency-Key.maxLength"]
        assert accepted.expected_status == 201
        assert len(accepted.headers["Idempotency-Key"]) == 64

    def test_required_header_missing_is_422(self, make_operation):
        from smarttest.ir import Parameter

        operation = make_operation(
            parameters=[
                Parameter(
                    name="X-Trace-Id",
                    location="header",
                    required=True,
                    schema={"type": "string", "maxLength": 32},
                )
            ]
        )
        cases = {c.case_id: c for c in RuleEngine()._cases_for_header_params(operation)}
        assert cases["op.X-Trace-Id.required_missing"].expected_status == 422
        assert cases["op.X-Trace-Id.required_missing"].headers == {}
        # 契约没写 minLength 时不该凭空生成下界用例。
        assert "op.X-Trace-Id.minLength-1" not in cases

    def test_header_min_length_case_requires_lower_bound_above_one(self, make_operation):
        from smarttest.ir import Parameter

        operation = make_operation(
            parameters=[
                Parameter(name="X-Len", location="header", required=False,
                          schema={"type": "string", "minLength": 4}),
                Parameter(name="X-One", location="header", required=False,
                          schema={"type": "string", "minLength": 1}),
            ]
        )
        cases = {c.case_id: c for c in RuleEngine()._cases_for_header_params(operation)}
        assert cases["op.X-Len.minLength-1"].expected_status == 422
        assert len(cases["op.X-Len.minLength-1"].headers["X-Len"]) == 3
        # 下界为 1 时「下界-1」是空串，而空串意味请求头没传，
        # 与「传了但太短」不是一回事，因此不生成。
        assert "op.X-One.minLength-1" not in cases

    def test_operation_without_header_params_produces_nothing(self, spec):
        operation = spec.find("getProduct")
        assert operation is not None
        assert RuleEngine()._cases_for_header_params(operation) == []


class TestPathRules:
    """路径参数规则：真实资源、资源不存在、长度越界。"""

    def test_existing_resource_uses_real_value_in_path(self, engine, spec):
        case = cases_of(engine, spec, "getProduct")["getProduct.product_id.exists"]
        assert case.path == "/api/v1/products/P001"
        assert case.expected_status == 200
        assert case.design_basis == "resource:exists"

    def test_missing_resource_is_404(self, engine, spec):
        case = cases_of(engine, spec, "getProduct")["getProduct.product_id.not_found"]
        assert case.path == "/api/v1/products/P-UNKNOWN-9999"
        assert case.expected_status == 404
        assert case.design_basis == "resource:not_found"

    def test_path_length_boundary_is_422(self, engine, spec):
        case = cases_of(engine, spec, "getProduct")["getProduct.product_id.maxLength+1"]
        assert case.expected_status == 422
        assert len(case.path.rsplit("/", 1)[-1]) == 33

    def test_no_real_data_skips_exists_case_but_keeps_the_rest(self, engine, spec):
        # order_id 在数据字典里没有真实值：只跳过「资源存在」这一条，
        # 不能因此把该接口的覆盖整个丢掉。
        ids = ids_of(engine, spec, "getOrder")
        assert "getOrder.order_id.exists" not in ids
        assert "getOrder.order_id.not_found" in ids
        assert "getOrder.order_id.maxLength+1" in ids

    def test_path_min_length_case_requires_lower_bound_above_one(self, engine, spec):
        # getProduct 的 product_id minLength=1，下界-1 会得到空路径段，不生成。
        assert "getProduct.product_id.minLength-1" not in ids_of(engine, spec, "getProduct")


class TestRequiredHeaderParams:
    """必填请求头必须出现在每一条用例里。

    这个缺口是第二靶场（用户与订阅服务）逼出来的：第一靶场唯一的请求头
    Idempotency-Key 是可选的，所以「必填头没带上」这个问题一直没显形。
    契约一旦声明了必填头，漏带它的用例会被服务先拦成 422，
    于是被测字段的真实行为根本没被执行到 —— 报出来的全是假阳性。
    """

    @staticmethod
    def _users_like(make_operation):
        from smarttest.ir import Parameter

        return make_operation(
            parameters=[
                Parameter(name="X-Tenant-Id", location="header", required=True,
                          schema={"type": "string", "minLength": 4, "maxLength": 16}),
                Parameter(name="X-Request-Source", location="header", required=False,
                          schema={"type": "string", "maxLength": 24}),
            ],
            body_schema={
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string", "minLength": 2, "maxLength": 8}},
            },
        )

    def test_required_header_value_is_derived_from_schema(self, make_operation):
        operation = self._users_like(make_operation)
        engine = RuleEngine()
        assert engine._required_headers(operation) == {"X-Tenant-Id": "aaaa"}

    def test_every_body_case_carries_the_required_header(self, make_operation):
        operation = self._users_like(make_operation)
        cases = {c.case_id: c for c in RuleEngine()._cases_for_body(operation)}
        assert cases
        for case in cases.values():
            assert case.headers.get("X-Tenant-Id") == "aaaa", case.case_id

    def test_path_cases_carry_the_required_header(self, make_operation):
        from smarttest.ir import Parameter

        operation = make_operation(
            method="GET",
            path="/api/v1/things/{thing_id}",
            responses={"200": {"description": "ok"}},
            parameters=[
                Parameter(name="X-Tenant-Id", location="header", required=True,
                          schema={"type": "string", "minLength": 4, "maxLength": 16}),
                Parameter(name="thing_id", location="path", required=True,
                          schema={"type": "string", "minLength": 1, "maxLength": 8}),
            ],
        )
        cases = RuleEngine()._cases_for_path_params(operation)
        assert cases
        assert all(c.headers.get("X-Tenant-Id") == "aaaa" for c in cases)

    def test_optional_header_case_keeps_the_required_one(self, make_operation):
        operation = self._users_like(make_operation)
        cases = {c.case_id: c for c in RuleEngine()._cases_for_header_params(operation)}
        assert cases["op.X-Request-Source.maxLength"].headers == {
            "X-Tenant-Id": "aaaa",
            "X-Request-Source": "a" * 24,
        }

    def test_missing_required_header_case_omits_only_that_header(self, make_operation):
        from smarttest.ir import Parameter

        operation = make_operation(
            parameters=[
                Parameter(name="X-A", location="header", required=True, schema={"type": "string"}),
                Parameter(name="X-B", location="header", required=True, schema={"type": "string"}),
            ]
        )
        cases = {c.case_id: c for c in RuleEngine()._cases_for_header_params(operation)}
        # 只摘掉被测的那一个，其余的必填头照常带上 ——
        # 否则这条用例同时验证了两件事，归因会指错方向。
        assert cases["op.X-A.required_missing"].headers == {"X-B": "a"}
        assert cases["op.X-B.required_missing"].headers == {"X-A": "a"}

    def test_operation_without_required_headers_is_unchanged(self, make_operation):
        # 订单靶场就是这个形态：唯一的请求头可选，用例不该被塞进任何请求头。
        from smarttest.ir import Parameter

        operation = make_operation(
            parameters=[
                Parameter(name="Idempotency-Key", location="header", required=False,
                          schema={"type": "string", "maxLength": 64}),
            ],
            body_schema={
                "type": "object",
                "required": ["quantity"],
                "properties": {"quantity": {"type": "integer", "minimum": 1}},
            },
        )
        cases = {c.case_id: c for c in RuleEngine()._cases_for_operation(operation)}
        assert cases["op.happy_path"].headers == {}
        assert cases["op.quantity.minimum-1"].headers == {}
        assert cases["op.Idempotency-Key.maxLength+1"].headers == {
            "Idempotency-Key": "a" * 65
        }


class TestCaseMetadata:
    """用例自身的元数据：命名、标记、覆盖统计。"""

    def test_case_id_is_namespaced_by_operation(self, engine, spec):
        ids = ids_of(engine, spec, "createOrder")
        assert ids
        assert all(case_id.startswith("createOrder.") for case_id in ids)

    def test_marker_is_machine_readable(self, engine, spec):
        case = cases_of(engine, spec, "createOrder")["createOrder.quantity.minimum-1"]
        # 标记里带的是完整 design_basis（含具体取值），归因模块据此反推是哪条规则、
        # 取到了哪个值；而覆盖统计里只留设计方法，两者的粒度是刻意不同的。
        assert case.marker() == (
            "[kind=contract][case_id=createOrder.quantity.minimum-1]"
            "[basis=boundary:minimum-1=0][expected=422]"
        )

    def test_coverage_records_basis_without_value(self, engine, spec):
        engine.generate(spec)
        bases = engine.coverage["quantity"].bases
        assert "required:missing" in bases
        assert "boundary:minimum-1" in bases
        # 覆盖统计记的是「设计方法」而不是「具体取值」，所以不带 = 后缀。
        assert all("=" not in basis for basis in bases)
        assert engine.coverage["Idempotency-Key"].operation_id == "createOrder"

    def test_every_case_carries_a_design_basis(self, engine, spec):
        cases = engine.generate(spec)
        assert cases
        assert all(case.design_basis for case in cases)
        assert all(case.expected_status >= 200 for case in cases)

    def test_generation_is_deterministic_with_unique_ids(self, spec):
        first = [c.case_id for c in RuleEngine().generate(spec)]
        second = [c.case_id for c in RuleEngine().generate(spec)]
        assert first == second
        assert len(first) == len(set(first))


class TestGoldenCaseSet:
    """黄金集合：把 createOrder 的用例清单钉死。

    规则引擎的任何改动都会在这里显形 —— 要么是有意新增（同步更新本清单），
    要么是无意改坏（本测试失败），不会悄悄溜进 CI。
    """

    EXPECTED_CREATE_ORDER_CASES = {
        "createOrder.happy_path",
        "createOrder.product_id.required_missing",
        "createOrder.product_id.type_mismatch",
        "createOrder.product_id.minLength-1",
        "createOrder.product_id.maxLength+1",
        "createOrder.product_id.not_found",
        "createOrder.quantity.required_missing",
        "createOrder.quantity.type_mismatch",
        "createOrder.quantity.minimum-1",
        "createOrder.quantity.maximum+1",
        "createOrder.quantity.minimum",
        "createOrder.quantity.maximum",
        "createOrder.coupon_code.type_mismatch",
        "createOrder.coupon_code.maxLength+1",
        "createOrder.coupon_code.maxLength",
        "createOrder.coupon_code.null",
        "createOrder.Idempotency-Key.maxLength+1",
        "createOrder.Idempotency-Key.maxLength",
    }

    def test_create_order_case_set_is_stable(self, engine, spec):
        assert ids_of(engine, spec, "createOrder") == self.EXPECTED_CREATE_ORDER_CASES

    def test_contract_case_count_is_stable(self, engine, spec):
        # 25 条契约用例 + 场景/业务用例 = 靶场报告的 31 条。
        assert len(engine.generate(spec)) == 25
