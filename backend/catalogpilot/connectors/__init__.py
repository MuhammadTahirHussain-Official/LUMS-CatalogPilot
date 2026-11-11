"""Connector registry. Platform-specific logic lives only in this package (MAINT-02)."""

from __future__ import annotations

from .sdk import Connector

_REGISTRY: dict[str, Connector] = {}


def register(connector: Connector) -> None:
    _REGISTRY[connector.platform] = connector


def get_connector(platform: str) -> Connector:
    _load()
    if platform not in _REGISTRY:
        raise KeyError(f"no connector for platform '{platform}'")
    return _REGISTRY[platform]


def platforms() -> list[str]:
    _load()
    return sorted(_REGISTRY)


def _load() -> None:
    if _REGISTRY:
        return
    from . import csv_feed, mock, shopify, woocommerce

    for module in (mock, csv_feed, shopify, woocommerce):
        register(module.CONNECTOR)
