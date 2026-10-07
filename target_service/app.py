"""被测服务：订单服务（教学靶场）。

注意：本服务**故意注入了 3 个缺陷**，用于验证 SmartTest 的缺陷发现能力。
缺陷都藏在「契约声明了、但实现没做到」的地方，这正是真实项目里最常见的
契约漂移类缺陷。

  缺陷 A｜quantity 未校验 minimum/maximum
        契约声明 1-999，实现只校验了类型，未校验范围。

  缺陷 B｜未实现 Idempotency-Key 幂等语义
        契约声明「相同 key 重复提交只创建一笔订单」，实现直接忽略了该请求头。

  缺陷 C｜金额使用 float 运算，未精确到分
        契约要求 total_price 满足 multipleOf 0.01，实现用浮点数相乘，
        0.10 * 3 = 0.30000000000000004。

  缺陷 D｜Idempotency-Key 未校验 maxLength
        这一处**不是刻意注入的** —— 它是在补齐 header 用例覆盖之后才暴露出来的，
        属于典型的契约漂移：契约写了约束，实现没人去落。
        留着它比修掉它更有价值：它证明这套方法是能发现「你原本没意识到的问题」的。

除这 4 处以外，实现的其余行为都与契约一致 —— 这样演示才有意义：
找出的是真缺陷，而不是一堆误报。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Header, HTTPException, Path as PathParam, Request
from fastapi.responses import JSONResponse

CONTRACT_PATH = Path(__file__).resolve().parent / "contract" / "openapi.yaml"

# 靶场模式：
#   buggy（默认）= 保留注入缺陷，用于验证「能不能发现问题」
#   fixed        = 已修复版本，用于验证「是不是误报」
# 同一套用例跑两遍，buggy 报 3 个缺陷、fixed 报 0 个，才能证明结果可信。
TARGET_MODE = os.environ.get("SMARTTEST_TARGET_MODE", "buggy").strip().lower()
IS_FIXED = TARGET_MODE == "fixed"

app = FastAPI(title="Order Service", version="1.0.0")

# 服务对外的 OpenAPI 文档 = 设计契约本身。
# 契约描述「应该长什么样」，下面的实现是「实际长什么样」，
# 两者的偏差就是 SmartTest 要发现的东西。
app.openapi = lambda: yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


PRODUCTS: dict[str, dict[str, Any]] = {
    "P001": {"product_id": "P001", "name": "机械键盘", "price": 399.00, "stock": 100},
    "P002": {"product_id": "P002", "name": "人体工学椅", "price": 1299.00, "stock": 20},
    "P003": {"product_id": "P003", "name": "定制贴纸", "price": 0.10, "stock": 1000},
}

ORDERS: dict[str, dict[str, Any]] = {}

# 幂等键 -> order_id
IDEMPOTENCY_KEYS: dict[str, str] = {}


def _validation_errors(payload: Any) -> list[str]:
    """手写校验，故意与契约不完全对齐。"""
    if not isinstance(payload, dict):
        return ["请求体必须是 JSON 对象"]

    errors: list[str] = []

    product_id = payload.get("product_id")
    if product_id is None:
        errors.append("product_id 为必填")
    elif not isinstance(product_id, str):
        errors.append("product_id 类型必须为 string")
    elif not 1 <= len(product_id) <= 32:
        errors.append("product_id 长度必须在 1-32 之间")

    quantity = payload.get("quantity")
    if quantity is None:
        errors.append("quantity 为必填")
    elif isinstance(quantity, bool) or not isinstance(quantity, int):
        errors.append("quantity 类型必须为 integer")
    elif IS_FIXED and not (1 <= quantity <= 999):
        # 修复 A：按契约补齐 minimum / maximum 校验
        errors.append("quantity 必须在 1-999 之间")
    # 缺陷 A（buggy 模式）：契约声明 minimum=1 / maximum=999，此处刻意不做范围校验

    coupon = payload.get("coupon_code")
    if coupon is not None:
        if not isinstance(coupon, str):
            errors.append("coupon_code 类型必须为 string")
        elif len(coupon) > 16:
            errors.append("coupon_code 长度不能超过 16")

    return errors


@app.post("/api/v1/orders", status_code=201)
async def create_order(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=422, content={"detail": ["请求体必须是合法 JSON"]})

    if IS_FIXED and idempotency_key is not None and len(idempotency_key) > 64:
        # 修复 D：契约声明 Idempotency-Key maxLength=64。
        # 这一处不是刻意注入的缺陷 —— 它是在补齐 header 用例覆盖之后才暴露出来的，
        # 属于典型的契约漂移：契约写了约束，实现没人去落。
        return JSONResponse(
            status_code=422,
            content={"detail": ["Idempotency-Key 长度不能超过 64"]},
        )

    errors = _validation_errors(payload)
    if errors:
        return JSONResponse(status_code=422, content={"detail": errors})

    product = PRODUCTS.get(payload["product_id"])
    if product is None:
        raise HTTPException(status_code=404, detail="商品不存在")

    # 缺陷 B：完全没有使用 idempotency_key，
    #        相同 key 重复提交会创建出两笔不同的订单。
    if IS_FIXED and idempotency_key:
        # 修复 B：命中幂等键时直接返回首次创建的订单
        existing_id = IDEMPOTENCY_KEYS.get(idempotency_key)
        if existing_id and existing_id in ORDERS:
            return ORDERS[existing_id]

    if IS_FIXED:
        # 修复 C：金额用 Decimal 计算并 quantize 到分
        total_price = float(
            (Decimal(str(product["price"])) * Decimal(payload["quantity"])).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
    else:
        # 缺陷 C：金额用 float 直接相乘，没有用 Decimal 并 quantize 到分。
        total_price = product["price"] * payload["quantity"]

    order = {
        "order_id": f"O{uuid.uuid4().hex[:12].upper()}",
        "product_id": product["product_id"],
        "quantity": payload["quantity"],
        "unit_price": product["price"],
        "total_price": total_price,
        "status": "CREATED",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    ORDERS[order["order_id"]] = order
    if IS_FIXED and idempotency_key:
        IDEMPOTENCY_KEYS[idempotency_key] = order["order_id"]
    return order


@app.get("/api/v1/orders/{order_id}")
async def get_order(order_id: str = PathParam(..., min_length=1, max_length=64)):
    order = ORDERS.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order


@app.post("/api/v1/orders/{order_id}/pay")
async def pay_order(order_id: str = PathParam(..., min_length=1, max_length=64)):
    order = ORDERS.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order["status"] != "CREATED":
        raise HTTPException(status_code=409, detail="订单状态不允许支付")
    order["status"] = "PAID"
    return order


@app.get("/api/v1/products/{product_id}")
async def get_product(product_id: str = PathParam(..., min_length=1, max_length=32)):
    product = PRODUCTS.get(product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    return product