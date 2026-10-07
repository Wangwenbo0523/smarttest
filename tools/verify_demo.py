"""演示层门禁：证明界面不是「另写一套」。

演示界面必须复用同一条流水线，否则演示得再好看也不能说明工具本身可用。
这个脚本把演示服务真的拉起来，走一遍 HTTP 接口，断言：
    1. `/api/health` 可访问，且靶场被自动拉起
    2. `GET /` 返回静态页面
    3. `POST /api/run`（修复版靶场 + 规则推导）报 0 个缺陷、假阳性 0
    4. 返回结构里带有生成代码与质量报告，界面要的数据后端都给齐了

不依赖任何 API Key：走规则推导通路，保证在 CI 上确定性通过。
退出码非 0 表示门禁失败。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 用一个独立端口跑靶场，避免和本机正在运行的 run_demo.py / 演示服务抢 8123；
# 也避免「复用外部进程」导致模式切换失效、门禁结论失真。
os.environ["SMARTTEST_TARGET_PORT"] = "8612"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from demo.server import app  # noqa: E402

HOST = "127.0.0.1"
PORT = 8611
BASE = f"http://{HOST}:{PORT}"


def _fail(message: str) -> int:
    print(f"[FAIL] {message}")
    return 1


def main() -> int:
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()

    try:
        # 1. 等服务起来（lifespan 里会在后台预热靶场）
        deadline = time.time() + 30
        health: dict = {}
        while time.time() < deadline:
            try:
                resp = httpx.get(f"{BASE}/api/health", timeout=2.0)
                if resp.status_code == 200:
                    health = resp.json()
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        if not health:
            return _fail("演示服务未在 30s 内就绪")
        print(f"[OK] /api/health 可用：靶场 {health['target_url']}，模型已配置={health['llm_configured']}")

        deadline = time.time() + 40
        while time.time() < deadline and not health.get("target_ready"):
            time.sleep(0.5)
            health = httpx.get(f"{BASE}/api/health", timeout=2.0).json()
        if not health.get("target_ready"):
            return _fail("靶场未被演示服务自动拉起")
        print(f"[OK] 靶场已自动就绪（模式={health['target_mode']}）")

        # 2. 静态页面
        index = httpx.get(f"{BASE}/", timeout=5.0)
        if index.status_code != 200 or "SmartTest" not in index.text:
            return _fail(f"GET / 异常：HTTP {index.status_code}")
        print(f"[OK] 演示页面可访问（{len(index.text)} 字节）")

        js = httpx.get(f"{BASE}/static/app.js", timeout=5.0)
        css = httpx.get(f"{BASE}/static/styles.css", timeout=5.0)
        if js.status_code != 200 or css.status_code != 200:
            return _fail("静态资源不可访问")
        print(f"[OK] 静态资源可访问（app.js {len(js.text)} 字节 / styles.css {len(css.text)} 字节）")

        # 3. 走一遍真实流水线：修复版靶场必须零缺陷、零假阳性
        resp = httpx.post(
            f"{BASE}/api/run",
            json={"mode": "fixed", "use_llm": False},
            timeout=300.0,
        )
        if resp.status_code != 200:
            return _fail(f"POST /api/run 异常：HTTP {resp.status_code}")
        result = resp.json()
        if not result.get("ok"):
            return _fail(f"流水线执行失败：{result.get('reason')}")

        metrics = result["metrics"]
        if metrics["defect_count"] != 0:
            return _fail(f"修复版靶场应报 0 个缺陷，实际 {metrics['defect_count']} 个")
        if metrics["suspected_false_positives"] != 0:
            return _fail(f"修复版靶场应报 0 条假阳性，实际 {metrics['suspected_false_positives']} 条")
        print(
            f"[OK] 流水线跑通：用例 {metrics['cases_total']} 条、"
            f"通过率 {metrics['pass_rate']}%、缺陷 {metrics['defect_count']} 个、"
            f"假阳性 {metrics['suspected_false_positives']} 条"
        )

        # 4. 界面需要的结构化数据都要在
        required = ["steps", "contract", "cases", "scenarios", "results", "findings",
                    "defects", "metrics", "report_markdown", "code"]
        missing = [key for key in required if key not in result]
        if missing:
            return _fail(f"返回结构缺少字段：{', '.join(missing)}")
        if not result["code"].get("contract") or not result["report_markdown"]:
            return _fail("生成代码或质量报告为空")
        print(
            f"[OK] 返回结构完整：接口 {len(result['contract']['operations'])} 个、"
            f"契约用例 {len(result['cases'])} 条、场景 {len(result['scenarios']['accepted'])} 个、"
            f"生成代码 {len(result['code'])} 个文件"
        )

        print("")
        print("[PASS] 演示层门禁通过：界面、流水线、数据契约三项一致")
        return 0
    finally:
        server.should_exit = True
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
