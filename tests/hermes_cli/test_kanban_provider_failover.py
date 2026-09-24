import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_provider_failover import select_failover_route


def _quota_task(tmp_path, *, assignee="old", status="ready"):
    path = tmp_path / "kanban.db"
    kbc.init_db(path)
    conn = kbc.connect(path)
    task_id = kb.create_task(
        conn, title="quota", assignee=assignee, model_override="old-model",
        provider_override="old-provider", initial_status="running",
    )
    claimed = kb.claim_task(conn, task_id, claimer="worker")
    assert claimed is not None
    conn.execute(
        "UPDATE tasks SET status=?, claim_lock=NULL WHERE id=?",
        (status, task_id),
    )
    conn.execute(
        "UPDATE task_runs SET outcome='rate_limited', ended_at=?, status='rate_limited', "
        "metadata=? WHERE id=?",
        (100.0, "{}", claimed.current_run_id),
    )
    conn.commit()
    return conn, task_id


def _failover_config(*, allow_review=False):
    return {
        "kanban": {
            "provider_failover_allow_review": allow_review,
            "provider_failover_routes": [{
                "assignee": "target", "provider": "new-provider", "model": "new-model",
                "operator_attested": True, "authenticated": True, "available": True,
            }],
        }
    }


def _mock_profile_runtime(monkeypatch, home):
    seen = []
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)
    monkeypatch.setattr("hermes_cli.profiles.normalize_profile_name", lambda name: name)
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env", lambda _name: str(home))
    def providers():
        from hermes_constants import get_hermes_home
        seen.append(str(get_hermes_home()))
        return [{"id": "new-provider", "authenticated": True}]
    monkeypatch.setattr("hermes_cli.models.list_available_providers", providers)
    monkeypatch.setattr("hermes_cli.models.provider_model_ids", lambda _provider: ["new-model"])
    return seen


def test_dispatch_failover_uses_target_profile_scope_and_records_event(tmp_path, monkeypatch):
    conn, task_id = _quota_task(tmp_path)
    home = tmp_path / "target-home"
    seen = _mock_profile_runtime(monkeypatch, home)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: _failover_config())
    monkeypatch.setattr(kbd.time, "time", lambda: 200.0)

    assert kbd._apply_provider_failover(conn, task_id) == "target"
    task = kb.get_task(conn, task_id)
    assert (task.assignee, task.provider_override, task.model_override) == (
        "target", "new-provider", "new-model"
    )
    assert seen == [str(home)]
    event = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()
    assert event["kind"] == "provider_failover"
    metadata = conn.execute("SELECT metadata FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0]
    assert json.loads(metadata)["failover_applied"] is True


def test_dispatch_failover_preserves_cas_on_repeated_dispatch(tmp_path, monkeypatch):
    conn, task_id = _quota_task(tmp_path)
    _mock_profile_runtime(monkeypatch, tmp_path / "target-home")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: _failover_config())

    assert kbd._apply_provider_failover(conn, task_id) == "target"
    assert kbd._apply_provider_failover(conn, task_id) is None
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='provider_failover'", (task_id,)
    ).fetchone()[0] == 1


def test_dispatch_failover_requires_review_opt_in(tmp_path, monkeypatch):
    conn, task_id = _quota_task(tmp_path, status="review")
    _mock_profile_runtime(monkeypatch, tmp_path / "target-home")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: _failover_config())
    assert kbd._apply_provider_failover(conn, task_id) is None
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: _failover_config(allow_review=True))
    assert kbd._apply_provider_failover(conn, task_id) == "target"


def test_selects_first_ordered_authenticated_route_and_dedupes():
    routes = [
        {"assignee": "b", "provider": "openai", "model": "m"},
        {"assignee": "b", "provider": "openai", "model": "m"},
        {"assignee": "a", "provider": "anthropic", "model": "x"},
    ]
    status = {
        ("b", "openai", "m"): {"operator_attested": True, "authenticated": True, "available": True, "profile_dispatchable": True, "provider_available": True, "model_available": True},
        ("a", "anthropic", "x"): {"operator_attested": True, "authenticated": True, "available": True, "profile_dispatchable": True, "provider_available": True, "model_available": True},
    }
    assert select_failover_route(routes, ("old", "old", "old"), status) == routes[0]


def test_skips_quarantined_unavailable_and_unknown_routes():
    routes = [
        {"assignee": "a", "provider": "p", "model": "m"},
        {"assignee": "b", "provider": "p", "model": "m"},
        {"assignee": "c", "provider": "p", "model": "m"},
    ]
    status = {
        ("a", "p", "m"): {"operator_attested": True, "profile_dispatchable": True, "provider_available": True, "model_available": True, "quota_until": 99},
        ("b", "p", "m"): {"operator_attested": True, "profile_dispatchable": True, "provider_available": False, "model_available": False},
        # c deliberately unknown
    }
    assert select_failover_route(routes, ("old", "p", "m"), status, now=50) is None


def test_rejects_same_route_and_all_exhausted():
    route = {"assignee": "a", "provider": "p", "model": "m"}
    assert select_failover_route([route], ("a", "p", "m"), {
        ("a", "p", "m"): {"operator_attested": True, "profile_dispatchable": True, "provider_available": True, "model_available": True},
    }) is None


def test_requires_explicit_authenticated_and_available_evidence():
    route = {"assignee": "a", "provider": "p", "model": "m"}
    assert select_failover_route([route], ("old", "p", "m"), {
        ("a", "p", "m"): {"operator_attested": True, "available": True, "profile_dispatchable": True, "provider_available": True, "model_available": True},
    }) is None
    assert select_failover_route([route], ("old", "p", "m"), {
        ("a", "p", "m"): {"operator_attested": True, "authenticated": True, "profile_dispatchable": True, "provider_available": True},
    }) is None


def test_requires_operator_attestation_even_with_runtime_evidence():
    route = {"assignee": "a", "provider": "p", "model": "m"}
    assert select_failover_route([route], ("old", "p", "m"), {
        ("a", "p", "m"): {"profile_dispatchable": True, "provider_available": True, "model_available": True},
    }) is None
