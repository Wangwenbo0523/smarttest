"""被测服务：内容服务（第三靶场）。

前两个靶场各有照不到的形态：订单服务没有 query 参数，用户服务没有嵌套对象
与数组。这个靶场专门把这些补齐（见 contract/articles.yaml 的说明），
用同一套规则引擎再跑一遍 —— 第三个领域也成立，才谈得上「通用」。

注意：本服务**故意注入了 3 个缺陷**，且刻意都藏在新增能力覆盖的地方，
用来证明这些能力是真的在起作用、不是摆设：

  缺陷 A｜page_size 未校验 maximum=100（**查询参数**，契约声明 1-100）
        只校验了下界。这一处只有 query 参数用例存在时才会被发现。

  缺陷 B｜sort 未校验枚举（**查询参数**，契约声明 CREATED_AT / TITLE）
        非法排序字段被放行。同上，属于 query 覆盖才照得到的问题。

  缺陷 C｜author.name 未校验 maxLength（**嵌套对象字段**，契约声明 2-32）
        只校验了下界。这一处要求规则引擎能下钻到对象内部。

除这 3 处以外，实现与契约一致；fixed 模式下全部修复，应当报 0 个缺陷。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Header, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import JSONResponse

CONTRACT_PATH = Path(__file__).resolve().parent / "contract" / "articles.yaml"

TARGET_MODE = os.environ.get("SMARTTEST_TARGET_MODE", "buggy").strip().lower()
IS_FIXED = TARGET_MODE == "fixed"

app = FastAPI(title="Article Service", version="3.0.0")
app.openapi = lambda: yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))

WORKSPACE_MIN, WORKSPACE_MAX = 2, 24
KEYWORD_MIN, KEYWORD_MAX = 2, 32
TAG_MAX = 16
PAGE_SIZE_MAX = 100
TRACE_MAX = 64
TITLE_MIN, TITLE_MAX = 3, 80
SUMMARY_MAX = 200
AUTHOR_NAME_MIN, AUTHOR_NAME_MAX = 2, 32
AUTHOR_EMAIL_MIN, AUTHOR_EMAIL_MAX = 6, 64
AUTH_MIN = 8

STATUSES = ("DRAFT", "PUBLISHED", "ARCHIVED")
SORTS = ("CREATED_AT", "TITLE")

ARTICLES: dict[str, dict[str, Any]] = {}


def _seed() -> None:
    """预置两篇文章，供「资源存在」类用例使用（对应 datasets/articles_testdata.json）。"""
    for article_id, title in (("A1001", "预置文章一"), ("A1002", "预置文章二")):
        ARTICLES[article_id] = {
            "article_id": article_id,
            "title": title,
            "summary": None,
            "tags": [],
            "author": {"name": "预置作者", "email": "seed@example.com"},
            "status": "PUBLISHED",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }


_seed()


def _auth_errors(authorization: str | None, trace_id: str | None) -> list[str]:
    errors: list[str] = []

    if authorization is None or not authorization.startswith("Bearer "):
        errors.append("Authorization 必须是 Bearer <token> 形式")
    elif len(authorization) < AUTH_MIN:
        errors.append(f"Authorization 长度不能小于 {AUTH_MIN}")

    if trace_id is not None and len(trace_id) > TRACE_MAX:
        errors.append(f"X-Trace-Id 长度不能超过 {TRACE_MAX}")

    return errors


def _list_errors(
    workspace: str | None,
    sort: str | None,
    tag: str | None,
    keyword: str | None,
    page: str | None,
    page_size: str | None,
) -> list[str]:
    """列表接口的查询参数校验。"""
    errors: list[str] = []

    if workspace is None or not workspace.strip():
        errors.append("workspace 为必填查询参数")
    elif not (WORKSPACE_MIN <= len(workspace) <= WORKSPACE_MAX):
        errors.append(f"workspace 长度必须在 {WORKSPACE_MIN}-{WORKSPACE_MAX} 之间")

    if tag is not None and len(tag) > TAG_MAX:
        errors.append(f"tag 长度不能超过 {TAG_MAX}")

    if keyword is not None and not (KEYWORD_MIN <= len(keyword) <= KEYWORD_MAX):
        errors.append(f"keyword 长度必须在 {KEYWORD_MIN}-{KEYWORD_MAX} 之间")

    if sort is not None:
        if IS_FIXED and sort not in SORTS:
            # 修复 B：契约声明了枚举，非法排序字段必须被拒绝
            errors.append("sort 必须是 " + " / ".join(SORTS) + " 之一")
        # 缺陷 B（buggy 模式）：契约声明了枚举，实现不校验

    if page is not None:
        try:
            page_number = int(page)
        except ValueError:
            errors.append("page 必须为整数")
        else:
            if page_number < 1:
                errors.append("page 必须大于等于 1")

    if page_size is not None:
        try:
            size = int(page_size)
        except ValueError:
            errors.append("page_size 必须为整数")
        else:
            if size < 1:
                errors.append("page_size 必须大于等于 1")
            elif IS_FIXED and size > PAGE_SIZE_MAX:
                # 修复 A：按契约补齐上界校验
                errors.append(f"page_size 不能超过 {PAGE_SIZE_MAX}")
            # 缺陷 A（buggy 模式）：契约声明 maximum=100，此处刻意不校验上界

    return errors


def _article_errors(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["请求体必须是 JSON 对象"]

    errors: list[str] = []

    title = payload.get("title")
    if title is None:
        errors.append("title 为必填")
    elif not isinstance(title, str):
        errors.append("title 类型必须为 string")
    elif not (TITLE_MIN <= len(title) <= TITLE_MAX):
        errors.append(f"title 长度必须在 {TITLE_MIN}-{TITLE_MAX} 之间")

    summary = payload.get("summary")
    if summary is not None:
        if not isinstance(summary, str):
            errors.append("summary 类型必须为 string")
        elif len(summary) > SUMMARY_MAX:
            errors.append(f"summary 长度不能超过 {SUMMARY_MAX}")

    tags = payload.get("tags")
    if tags is not None and not isinstance(tags, list):
        errors.append("tags 类型必须为 array")

    author = payload.get("author")
    if author is None:
        errors.append("author 为必填")
    elif not isinstance(author, dict):
        errors.append("author 类型必须为 object")
    else:
        name = author.get("name")
        if name is None:
            errors.append("author.name 为必填")
        elif not isinstance(name, str):
            errors.append("author.name 类型必须为 string")
        elif len(name) < AUTHOR_NAME_MIN:
            errors.append(f"author.name 长度不能小于 {AUTHOR_NAME_MIN}")
        elif IS_FIXED and len(name) > AUTHOR_NAME_MAX:
            # 修复 C：嵌套对象字段的 maxLength 同样要校验
            errors.append(f"author.name 长度不能超过 {AUTHOR_NAME_MAX}")
        # 缺陷 C（buggy 模式）：嵌套字段的 maxLength 没有落

        email = author.get("email")
        if email is None:
            errors.append("author.email 为必填")
        elif not isinstance(email, str):
            errors.append("author.email 类型必须为 string")
        elif not (AUTHOR_EMAIL_MIN <= len(email) <= AUTHOR_EMAIL_MAX):
            errors.append(f"author.email 长度必须在 {AUTHOR_EMAIL_MIN}-{AUTHOR_EMAIL_MAX} 之间")

    status = payload.get("status")
    if status is not None:
        if not isinstance(status, str):
            errors.append("status 类型必须为 string")
        elif status not in STATUSES:
            errors.append("status 必须是 " + " / ".join(STATUSES) + " 之一")

    return errors


def _dedupe(values: list[Any]) -> list[Any]:
    """去重并保留首次出现的顺序（契约声明的 tags 存储语义）。"""
    seen: list[Any] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


@app.get("/api/v1/articles")
async def list_articles(
    workspace: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    page: str | None = Query(default=None),
    page_size: str | None = Query(default=None),
):
    errors = _list_errors(workspace, sort, tag, keyword, page, page_size)
    if errors:
        return JSONResponse(status_code=422, content={"detail": errors})

    items = list(ARTICLES.values())
    if tag:
        items = [a for a in items if tag in a["tags"]]
    if keyword:
        items = [a for a in items if keyword in a["title"]]

    return {"total": len(items), "items": items}


@app.post("/api/v1/articles", status_code=201)
async def create_article(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=422, content={"detail": ["请求体必须是合法 JSON"]})

    errors = _auth_errors(authorization, x_trace_id) + _article_errors(payload)
    if errors:
        return JSONResponse(status_code=422, content={"detail": errors})

    article_id = f"A{uuid.uuid4().hex[:10].upper()}"
    article = {
        "article_id": article_id,
        "title": payload["title"],
        "summary": payload.get("summary"),
        # 契约声明：tags 存储时去重并保留首次出现的顺序
        "tags": _dedupe(payload.get("tags") or []),
        "author": {"name": payload["author"]["name"], "email": payload["author"]["email"]},
        # 契约声明：不传 status 时默认 DRAFT
        "status": payload.get("status") or "DRAFT",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    ARTICLES[article_id] = article
    return article


@app.get("/api/v1/articles/{article_id}")
async def get_article(article_id: str = PathParam(..., min_length=4, max_length=36)):
    article = ARTICLES.get(article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    return article
