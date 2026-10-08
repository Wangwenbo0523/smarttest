"""被测服务：用户与订阅服务（第二靶场）。

存在的意义是**泛化验证**：第一靶场只有一个领域、一种字段形态，
在它身上跑出「召回 100% / 假阳性 0」，说明不了规则引擎是不是通用。
这个靶场的契约在形态上刻意与订单契约错开（见 contract/users.yaml 的说明），
把「规则引擎其实只适配了订单服务」这类问题逼出来。

注意：本服务**故意注入了 3 个缺陷**，与第一靶场同样的注入方式
（只注入「契约声明了、实现没做到」的契约漂移类缺陷）：

  缺陷 A｜age 未校验 minimum/maximum
        契约声明 18-120，实现只校验了类型。

  缺陷 B｜email 未校验 maxLength
        契约声明 maxLength=64，实现只校验了 minLength。

  缺陷 C｜必填请求头 X-Tenant-Id 未校验必填
        契约声明 required=true，实现只在「传了但长度不对」时拦截；
        完全不传也能注册成功。（长度校验是有的 —— 所以这个缺陷
        只会在「缺必填头」这一条用例上显形，不会连带刷出长度类缺陷。）

除这 3 处以外，实现与契约一致；fixed 模式下这 3 处全部修复，应当报 0 个缺陷。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Header, HTTPException, Path as PathParam, Request
from fastapi.responses import JSONResponse

CONTRACT_PATH = Path(__file__).resolve().parent / "contract" / "users.yaml"

TARGET_MODE = os.environ.get("SMARTTEST_TARGET_MODE", "buggy").strip().lower()
IS_FIXED = TARGET_MODE == "fixed"

app = FastAPI(title="User Service", version="2.0.0")
app.openapi = lambda: yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))

TENANT_MIN, TENANT_MAX = 4, 16
SOURCE_MAX = 24
NAME_MIN, NAME_MAX = 2, 32
EMAIL_MIN, EMAIL_MAX = 6, 64
AGE_MIN, AGE_MAX = 18, 120
CHANNELS = ("WEB", "APP", "PARTNER")

PLANS: dict[str, dict[str, Any]] = {
    "PLAN-BASIC": {"plan_code": "PLAN-BASIC", "name": "基础版", "monthly_price": 0.0, "seat_limit": 1},
    "PLAN-PRO": {"plan_code": "PLAN-PRO", "name": "专业版", "monthly_price": 99.0, "seat_limit": 20},
}

USERS: dict[str, dict[str, Any]] = {}


def _seed() -> None:
    """预置两个用户，供「资源存在」类用例使用（对应 datasets/users_testdata.json）。"""
    for user_id, name, email in (
        ("U1001", "预置用户一", "seed1@example.com"),
        ("U1002", "预置用户二", "seed2@example.com"),
    ):
        USERS[user_id] = {
            "user_id": user_id,
            "name": name,
            "email": email,
            "plan_code": "PLAN-BASIC",
            "age": 30,
            "marketing_opt_in": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


_seed()


def _header_errors(tenant: str | None, source: str | None) -> list[str]:
    errors: list[str] = []

    if tenant is None or not tenant.strip():
        if IS_FIXED:
            # 修复 C：契约声明 X-Tenant-Id required=true
            errors.append("X-Tenant-Id 为必填请求头")
        # 缺陷 C（buggy 模式）：完全不传也能过
    elif not (TENANT_MIN <= len(tenant) <= TENANT_MAX):
        errors.append(f"X-Tenant-Id 长度必须在 {TENANT_MIN}-{TENANT_MAX} 之间")

    if source is not None and len(source) > SOURCE_MAX:
        errors.append(f"X-Request-Source 长度不能超过 {SOURCE_MAX}")

    return errors


def _body_errors(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["请求体必须是 JSON 对象"]

    errors: list[str] = []

    name = payload.get("name")
    if name is None:
        errors.append("name 为必填")
    elif not isinstance(name, str):
        errors.append("name 类型必须为 string")
    elif not (NAME_MIN <= len(name) <= NAME_MAX):
        errors.append(f"name 长度必须在 {NAME_MIN}-{NAME_MAX} 之间")

    email = payload.get("email")
    if email is None:
        errors.append("email 为必填")
    elif not isinstance(email, str):
        errors.append("email 类型必须为 string")
    elif len(email) < EMAIL_MIN:
        errors.append(f"email 长度不能小于 {EMAIL_MIN}")
    elif IS_FIXED and len(email) > EMAIL_MAX:
        # 修复 B：按契约补齐 maxLength 校验
        errors.append(f"email 长度不能超过 {EMAIL_MAX}")
    # 缺陷 B（buggy 模式）：契约声明 maxLength=64，此处刻意不校验上界

    plan_code = payload.get("plan_code")
    if plan_code is None:
        errors.append("plan_code 为必填")
    elif not isinstance(plan_code, str):
        errors.append("plan_code 类型必须为 string")
    elif not (2 <= len(plan_code) <= 16):
        errors.append("plan_code 长度必须在 2-16 之间")

    age = payload.get("age")
    if age is not None:
        if isinstance(age, bool) or not isinstance(age, int):
            errors.append("age 类型必须为 integer")
        elif IS_FIXED and not (AGE_MIN <= age <= AGE_MAX):
            # 修复 A：按契约补齐 minimum / maximum 校验
            errors.append(f"age 必须在 {AGE_MIN}-{AGE_MAX} 之间")
        # 缺陷 A（buggy 模式）：契约声明 18-120，此处刻意不做范围校验

    channel = payload.get("channel")
    if channel is not None:
        if not isinstance(channel, str):
            errors.append("channel 类型必须为 string")
        elif channel not in CHANNELS:
            errors.append("channel 必须是 " + " / ".join(CHANNELS) + " 之一")

    opt_in = payload.get("marketing_opt_in")
    if opt_in is not None and not isinstance(opt_in, bool):
        errors.append("marketing_opt_in 类型必须为 boolean 或 null")

    return errors


def _user_view(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": user["user_id"],
        "name": user["name"],
        "email": user["email"],
        "plan_code": user["plan_code"],
        "age": user["age"],
        "marketing_opt_in": user["marketing_opt_in"],
        "created_at": user["created_at"],
    }


@app.post("/api/v1/users", status_code=201)
async def create_user(
    request: Request,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_request_source: str | None = Header(default=None, alias="X-Request-Source"),
):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=422, content={"detail": ["请求体必须是合法 JSON"]})

    errors = _header_errors(x_tenant_id, x_request_source) + _body_errors(payload)
    if errors:
        return JSONResponse(status_code=422, content={"detail": errors})

    plan = PLANS.get(payload["plan_code"])
    if plan is None:
        raise HTTPException(status_code=404, detail="套餐不存在")

    user_id = f"U{uuid.uuid4().hex[:10].upper()}"
    user = {
        "user_id": user_id,
        "name": payload["name"],
        # 契约声明：邮箱统一按小写存储
        "email": payload["email"].strip().lower(),
        "plan_code": plan["plan_code"],
        "age": payload.get("age"),
        # 契约声明：不传或传 null 时视为 false
        "marketing_opt_in": bool(payload.get("marketing_opt_in")),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    USERS[user_id] = user
    return _user_view(user)


@app.get("/api/v1/users/{user_id}")
async def get_user(user_id: str = PathParam(..., min_length=4, max_length=36)):
    user = USERS.get(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return _user_view(user)


@app.get("/api/v1/plans/{plan_code}")
async def get_plan(plan_code: str = PathParam(..., min_length=2, max_length=16)):
    plan = PLANS.get(plan_code)
    if plan is None:
        raise HTTPException(status_code=404, detail="套餐不存在")
    return plan
