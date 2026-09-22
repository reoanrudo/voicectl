"""判定エンジンの共通インターフェース。Jev（公式 API）とキーワード照合を差し替えられるようにする。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..candidates import Lookup
from ..context import Snapshot
from ..schema import ACTION_PARAMS, SOFT_PARAMS, Decision


class DecisionEngine(ABC):
    name = "base"

    @abstractmethod
    def decide(self, text: str, snap: Snapshot, lookup: Lookup, previous: Decision | None,
               facts: dict | None = None, recent: list | None = None, session: dict | None = None) -> Decision:
        ...

    def close(self) -> None:
        pass


def overall_confidence(action: str, detail: dict[str, float]) -> float:
    """action と、その action に必須のパラメータの自信度の最小値。"""
    vals = [detail.get("action", 0.0)]
    for p in ACTION_PARAMS.get(action, []):
        if p not in SOFT_PARAMS:
            vals.append(detail.get(p, 0.0))
    return min(vals)
