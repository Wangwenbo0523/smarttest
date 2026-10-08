"""靶场注册表：一套流水线，多份契约。

第一靶场（订单服务）证明「能发现缺陷」，但它只有一个领域、一种字段形态。
只在一个契约上跑出「召回 100% / 假阳性 0」，说服力是有上限的 ——
换一个契约还灵不灵，才是规则引擎是不是真的通用的证据。

所以这里把「靶场」抽成一个数据描述：契约在哪、数据字典在哪、标注集在哪、
业务层用哪个模板、由哪个 ASGI 应用提供服务。流水线代码不再认识具体领域。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Target:
    key: str
    label: str
    app: str
    contract: Path
    dataset: Path
    ground_truth: Path
    business_template: str

    @property
    def default_port(self) -> int:
        return 8123 if self.key == "orders" else 8124


TARGETS: dict[str, Target] = {
    "orders": Target(
        key="orders",
        label="订单服务",
        app="target_service.app:app",
        contract=ROOT / "target_service" / "contract" / "openapi.yaml",
        dataset=ROOT / "datasets" / "testdata.json",
        ground_truth=ROOT / "evals" / "ground_truth.yaml",
        business_template="business_test.py.j2",
    ),
    "users": Target(
        key="users",
        label="用户与订阅服务",
        app="target_service.user_app:app",
        contract=ROOT / "target_service" / "contract" / "users.yaml",
        dataset=ROOT / "datasets" / "users_testdata.json",
        ground_truth=ROOT / "evals" / "ground_truth_users.yaml",
        business_template="business_user_test.py.j2",
    ),
}


def get_target(key: str) -> Target:
    try:
        return TARGETS[key]
    except KeyError:
        raise SystemExit(f"未知靶场 {key!r}，可选：{', '.join(sorted(TARGETS))}") from None


def target_keys() -> list[str]:
    return sorted(TARGETS)
