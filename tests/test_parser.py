"""Spec Parser 单元测试：OpenAPI 3.x -> IR。

解析是整条流水线的入口。契约被解析错（$ref 没展开、参数丢了、状态码认错），
下游生成的用例再漂亮也没有意义 —— 而且这类错误往往表现为「少生成几条用例」，
不会报错，所以必须用测试兜住。
"""
from __future__ import annotations

import json

import pytest

from smarttest.parser import OpenApiParser


def minimal_document(paths: dict, components: dict | None = None) -> dict:
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "测试契约", "version": "9.9.9"},
        "paths": paths,
    }
    if components is not None:
        doc["components"] = components
    return doc


class TestRealContract:
    """对靶场契约做结构断言：这些是下游规则的输入前提。"""

    def test_document_metadata(self, spec):
        assert spec.title == "订单服务（设计契约）"
        assert spec.version == "1.0.0"

    def test_all_operations_are_parsed(self, spec):
        assert [op.operation_id for op in spec.operations] == [
            "createOrder",
            "getOrder",
            "payOrder",
            "getProduct",
        ]

    def test_ref_is_expanded_into_body_schema(self, spec):
        operation = spec.find("createOrder")
        assert operation.body_schema["required"] == ["product_id", "quantity"]
        assert set(operation.body_schema["properties"]) == {"product_id", "quantity", "coupon_code"}
        assert operation.body_schema["properties"]["quantity"]["maximum"] == 999

    def test_success_status_comes_from_documented_responses(self, spec):
        assert spec.find("createOrder").success_status == 201
        assert spec.find("getOrder").success_status == 200

    def test_documented_errors_exclude_success_codes(self, spec):
        assert spec.find("createOrder").documented_errors == [404, 422]
        assert spec.find("payOrder").documented_errors == [404, 409]

    def test_header_and_path_params_are_classified(self, spec):
        create = spec.find("createOrder")
        assert [p.name for p in create.header_params()] == ["Idempotency-Key"]
        assert create.path_params() == []
        assert [p.name for p in spec.find("getOrder").path_params()] == ["order_id"]

    def test_path_parameter_keeps_its_schema(self, spec):
        param = spec.find("getProduct").path_params()[0]
        assert param.location == "path"
        assert param.required is True
        assert param.schema == {"type": "string", "minLength": 1, "maxLength": 32}

    def test_response_properties_helper_reads_success_schema(self, spec):
        # Order 的 $ref 也应被展开，供场景/业务断言层使用。
        properties = spec.find("createOrder").response_properties(201)
        assert "total_price" in properties
        assert properties["status"]["enum"] == ["CREATED", "PAID", "CANCELLED"]

    def test_find_returns_none_for_unknown_operation(self, spec):
        assert spec.find("noSuchOperation") is None
        assert spec.find("getOrder").signature == "GET /api/v1/orders/{order_id}"


class TestOperationIdFallback:
    def test_operation_id_is_derived_when_missing(self):
        doc = minimal_document(
            {
                "/api/v1/orders/{order_id}/pay": {
                    "post": {"responses": {"200": {"description": "ok"}}}
                }
            }
        )
        operation = OpenApiParser(doc).parse().operations[0]
        assert operation.operation_id == "post_api_v1_orders_order_id_pay"
        assert operation.method == "POST"
        assert operation.tag == "default"

    def test_methods_outside_the_supported_set_are_skipped(self):
        doc = minimal_document(
            {
                "/x": {
                    "head": {"responses": {"200": {"description": "ok"}}},
                    "get": {"operationId": "ping", "responses": {"200": {"description": "ok"}}},
                }
            }
        )
        assert [op.operation_id for op in OpenApiParser(doc).parse().operations] == ["ping"]


class TestRefResolution:
    def test_ref_chain_is_resolved(self):
        doc = minimal_document(
            {
                "/x": {
                    "post": {
                        "operationId": "x",
                        "requestBody": {
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/A"}}}
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
            components={"schemas": {"A": {"$ref": "#/components/schemas/B"}, "B": {"type": "object"}}},
        )
        assert OpenApiParser(doc).parse().operations[0].body_schema == {"type": "object"}

    def test_external_ref_is_rejected_with_actionable_message(self):
        doc = minimal_document(
            {
                "/x": {
                    "post": {
                        "operationId": "x",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "https://example.com/common.yaml#/A"}
                                }
                            }
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )
        with pytest.raises(ValueError, match="仅支持文档内部"):
            OpenApiParser(doc).parse()

    def test_circular_ref_is_rejected_instead_of_hanging(self):
        doc = minimal_document(
            {
                "/x": {
                    "post": {
                        "operationId": "x",
                        "requestBody": {
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/A"}}}
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
            components={"schemas": {"A": {"$ref": "#/components/schemas/A"}}},
        )
        with pytest.raises(ValueError, match="循环引用"):
            OpenApiParser(doc).parse()


class TestParametersAndResponses:
    def test_path_level_and_operation_level_parameters_are_merged(self):
        doc = minimal_document(
            {
                "/x": {
                    "parameters": [
                        {"name": "shared", "in": "header", "required": False, "schema": {"type": "string"}}
                    ],
                    "post": {
                        "operationId": "x",
                        "parameters": [
                            {"name": "own", "in": "query", "required": True, "schema": {"type": "string"}}
                        ],
                        "responses": {"200": {"description": "ok"}},
                    },
                }
            }
        )
        operation = OpenApiParser(doc).parse().operations[0]
        assert [p.name for p in operation.parameters] == ["shared", "own"]
        assert operation.header_params()[0].required is False

    def test_non_numeric_response_codes_are_kept_but_not_parsed_as_schemas(self):
        doc = minimal_document(
            {
                "/x": {
                    "get": {
                        "operationId": "x",
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {"application/json": {"schema": {"type": "object"}}},
                            },
                            "default": {"description": "兜底"},
                        },
                    }
                }
            }
        )
        operation = OpenApiParser(doc).parse().operations[0]
        assert set(operation.responses) == {"200", "default"}
        assert list(operation.response_schemas) == [200]
        assert operation.documented_errors == []

    def test_body_is_optional_when_contract_says_so(self):
        doc = minimal_document(
            {
                "/x": {
                    "post": {
                        "operationId": "x",
                        "requestBody": {
                            "required": False,
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )
        operation = OpenApiParser(doc).parse().operations[0]
        assert operation.body_required is False
        assert operation.body_schema == {"type": "object"}

    def test_first_content_type_is_used_when_json_is_absent(self):
        # 契约只声明了非 JSON 媒体类型时，仍然应该解析出 body，
        # 而不是静默丢掉整个请求体。
        doc = minimal_document(
            {
                "/x": {
                    "post": {
                        "operationId": "x",
                        "requestBody": {
                            "required": True,
                            "content": {"application/xml": {"schema": {"type": "object"}}},
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )
        assert OpenApiParser(doc).parse().operations[0].body_schema == {"type": "object"}


class TestLoading:
    def test_load_from_json_file(self, tmp_path):
        payload = minimal_document(
            {"/x": {"get": {"operationId": "x", "responses": {"200": {"description": "ok"}}}}}
        )
        path = tmp_path / "contract.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert OpenApiParser.from_file(path).parse().find("x") is not None

    def test_load_from_yaml_file(self, tmp_path):
        path = tmp_path / "contract.yaml"
        path.write_text(
            "openapi: 3.0.3\n"
            "info:\n  title: t\n  version: '1'\n"
            "paths:\n"
            "  /x:\n"
            "    get:\n"
            "      operationId: x\n"
            "      responses:\n"
            "        '200':\n"
            "          description: ok\n",
            encoding="utf-8",
        )
        spec = OpenApiParser.from_file(path).parse()
        assert spec.title == "t"
        assert spec.find("x").success_status == 200

    def test_empty_paths_produce_empty_spec(self):
        assert OpenApiParser(minimal_document({})).parse().operations == []

    def test_load_from_url(self, tmp_path):
        # 从 URL 拉契约走的是 urllib；用 file:// URI 就能覆盖这条路径，
        # 不必为了单测起一个 HTTP 服务
        payload = minimal_document(
            {"/x": {"get": {"operationId": "x", "responses": {"200": {"description": "ok"}}}}}
        )
        path = tmp_path / "contract.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        spec = OpenApiParser.from_url(path.as_uri()).parse()

        assert spec.find("x") is not None
