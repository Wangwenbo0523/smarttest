"""SmartTest 演示服务。

零额外依赖的本地 Web 界面：FastAPI 暴露流水线接口，前端是同一目录下的纯静态页面。
不引入 Streamlit / pandas，是因为演示要能在任何一台装了
`requirements.txt` 的机器上直接跑起来，而不是先卡在依赖安装上。
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from demo.pipeline import (
    BASE_URL,
    TARGET_PORT,
    TargetService,
    llm_configured,
    llm_model,
    run_pipeline,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

_service = TargetService()
_run_lock = threading.Lock()
_state: dict = {"last": None}


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 后台预热靶场：让用户点「开始执行」时不用等冷启动
    threading.Thread(target=lambda: _service.ensure("buggy"), daemon=True).start()
    yield
    _service.shutdown()


app = FastAPI(
    title="SmartTest Demo",
    description="接口契约驱动的测试用例智能生成与失败归因平台 · 演示服务",
    version="1.0.0",
    lifespan=lifespan,
)


class RunRequest(BaseModel):
    mode: str = "buggy"
    use_llm: bool = True


@app.get("/api/health")
def health() -> dict:
    """界面启动时调用，用于显示靶场与模型配置状态。"""
    return {
        "target_port": TARGET_PORT,
        "target_url": BASE_URL,
        "target_ready": _service.healthy(),
        "target_mode": _service.mode,
        "llm_configured": llm_configured(),
        "llm_model": llm_model() if llm_configured() else "",
        "has_result": _state["last"] is not None,
    }


@app.post("/api/run")
def run(request: RunRequest) -> dict:
    """跑一次流水线。同一时刻只允许一次运行，避免两个 pytest 抢同一个靶场。"""
    with _run_lock:
        result = run_pipeline(request.mode, use_llm=request.use_llm, service=_service)
        _state["last"] = result
        return result


@app.post("/api/compare")
def compare(request: RunRequest) -> dict:
    """一键对比：缺陷版靶场应报出缺陷，修复版靶场必须一个都不报（零误报自证）。"""
    with _run_lock:
        buggy = run_pipeline("buggy", use_llm=request.use_llm, service=_service)
        fixed = run_pipeline("fixed", use_llm=request.use_llm, service=_service)
        _state["last"] = buggy
        return {"buggy": buggy, "fixed": fixed}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
