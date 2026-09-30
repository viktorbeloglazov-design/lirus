"""Реестр узлов сценариев."""
from __future__ import annotations

from . import apps, core
from .base import NodeError, NodeType, Param  # noqa: F401

NODES: list[NodeType] = core.NODES + apps.NODES
BY_KEY: dict[str, NodeType] = {n.key: n for n in NODES}
BY_N8N: dict[str, NodeType] = {t: n for n in NODES for t in n.n8n_types}

GROUP_ORDER = [core.G_TRIGGER, apps.G_AI, apps.G_APPS, core.G_FLOW, core.G_DATA]


def get(key: str) -> NodeType | None:
    return BY_KEY.get(key)
