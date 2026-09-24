"""Deterministic, fail-closed route selection for Kanban quota failover."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

RouteKey = tuple[str, str, str]


def route_key(route: Mapping[str, Any]) -> RouteKey:
    return tuple(str(route.get(k) or "").strip().casefold() for k in ("assignee", "provider", "model"))  # type: ignore[return-value]


def normalize_failover_routes(raw: Any) -> list[dict[str, str]]:
    """Keep configured order, discard malformed entries, and deduplicate routes."""
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    routes: list[dict[str, str]] = []
    seen: set[RouteKey] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        route = {k: str(item.get(k) or "").strip() for k in ("assignee", "provider", "model")}
        if not all(route.values()):
            continue
        key = route_key(route)
        if key not in seen:
            seen.add(key)
            routes.append(route)
    return routes


def select_failover_route(
    routes: Any,
    original: RouteKey,
    evidence: Mapping[RouteKey, Mapping[str, Any]],
    *,
    now: float = 0,
) -> dict[str, str] | None:
    """Select first route with explicit authenticated/available evidence.

    Missing or malformed evidence is unknown and therefore rejected. A route
    remains quarantined through ``quota_until``; this gives repeated quota
    exits hysteresis without probing a known-empty route.
    """
    original_key = tuple(str(v or "").strip().casefold() for v in original)
    seen: set[RouteKey] = set()
    for route in normalize_failover_routes(routes):
        key = route_key(route)
        if key in seen or key == original_key:
            continue
        seen.add(key)
        status = evidence.get(key)
        if not isinstance(status, Mapping):
            continue
        # ``authenticated``/``available`` are only operator attestations.  The
        # dispatcher must also prove the route is actually dispatchable and its
        # provider/model are usable; absent runtime evidence is unknown.
        if status.get("operator_attested") is not True:
            continue
        if status.get("authenticated") is not True or status.get("available") is not True:
            continue
        if any(status.get(k) is not True for k in (
            "profile_dispatchable", "provider_available", "model_available",
        )):
            continue
        try:
            quota_until = float(status.get("quota_until") or 0)
        except (TypeError, ValueError):
            continue
        if quota_until > now:
            continue
        return dict(route)
    return None


def route_evidence_from_config(raw: Any) -> dict[RouteKey, dict[str, Any]]:
    """Build non-secret evidence from explicit route attestations.

    Dispatcher config must opt a route in with ``authenticated: true`` and
    ``available: true``. Missing values stay unknown; no credential is read or
    emitted by this seam.
    """
    result: dict[RouteKey, dict[str, Any]] = {}
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return result
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        route = {k: str(item.get(k) or "").strip() for k in ("assignee", "provider", "model")}
        if not all(route.values()):
            continue
        result[route_key(route)] = {
            "operator_attested": item.get("operator_attested") is True,
            "authenticated": item.get("authenticated") is True,
            "available": item.get("available") is True,
            "quota_until": item.get("quota_until", 0),
        }
    return result
