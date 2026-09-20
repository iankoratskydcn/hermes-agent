from hermes_cli.kanban_provider_failover import select_failover_route


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
