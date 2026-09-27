"""Producer seams emit canonical intervention events without blocking callers."""

from pathlib import Path

import yaml

from agent import intervention_events
from hermes_cli import model_switch


def _result(**kwargs):
    values = {
        "success": True,
        "new_model": "same-model",
        "target_provider": "same-provider",
        "base_url": "https://new.example/v1",
        "api_mode": "chat_completions",
    }
    values.update(kwargs)
    return model_switch.ModelSwitchResult(**values)


def test_switch_model_captures_endpoint_only_intervention(monkeypatch):
    captured = []
    monkeypatch.setattr(model_switch, "_route_from_model_input", lambda _state: None)
    monkeypatch.setattr(model_switch, "_resolve_switch_credentials", lambda _state: None)
    monkeypatch.setattr(model_switch, "_validate_switch", lambda _state: None)
    monkeypatch.setattr(model_switch, "_build_switch_result", lambda _state: _result())
    monkeypatch.setattr(
        intervention_events,
        "capture_model_intervention",
        lambda **kwargs: captured.append(kwargs),
    )

    result = model_switch.switch_model(
        "same-model", "same-provider", "same-model", current_base_url="https://old.example/v1"
    )

    assert result.success
    assert captured
    assert captured[0]["before"]["base_url"] == "https://old.example/v1"
    assert captured[0]["after"]["base_url"] == "https://new.example/v1"


def test_switch_model_capture_failure_does_not_block(monkeypatch):
    monkeypatch.setattr(model_switch, "_route_from_model_input", lambda _state: None)
    monkeypatch.setattr(model_switch, "_resolve_switch_credentials", lambda _state: None)
    monkeypatch.setattr(model_switch, "_validate_switch", lambda _state: None)
    monkeypatch.setattr(model_switch, "_build_switch_result", lambda _state: _result())
    monkeypatch.setattr(
        intervention_events,
        "capture_model_intervention",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("capture down")),
    )

    assert model_switch.switch_model("same-model", "same-provider", "same-model").success


def test_persist_model_selection_captures_config_transition(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"model": {"default": "old", "provider": "old-provider"}}))
    captured = []
    monkeypatch.setattr(
        intervention_events,
        "capture_config_intervention",
        lambda **kwargs: captured.append(kwargs),
    )

    model_switch.persist_model_selection(
        _result(new_model="new", target_provider="new-provider", base_url="https://new.example/v1"),
        config_path=config_path,
    )

    assert captured
    assert captured[0]["before"] == {"default": "old", "provider": "old-provider"}
    assert captured[0]["after"]["default"] == "new"
    assert captured[0]["after"]["provider"] == "new-provider"
