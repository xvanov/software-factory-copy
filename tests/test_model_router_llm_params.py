"""llm_params_for / preset_for resolution (routes.yaml ``llm_params``/``presets``)."""

from __future__ import annotations

from pathlib import Path

from factory.model_router import llm_params_for, preset_for


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "routes.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_defaults_merge_with_model_limits() -> None:
    # Production routes.yaml: gpt-5.4 cap 16384, defaults pin caching_prompt.
    params = llm_params_for("pm", "azure/gpt-5.4")
    assert params["max_output_tokens"] == 16384
    assert params["caching_prompt"] is True


def test_difficulty_mapped_persona_entry() -> None:
    std = llm_params_for("dev", "azure/deepseek-v4-pro", difficulty="standard")
    assert std["reasoning_effort"] == "none"
    assert std["max_output_tokens"] == 8192
    hard = llm_params_for("dev", "openrouter/moonshotai/kimi-k2.7-code", difficulty="hard")
    assert hard["reasoning_effort"] == "high"
    assert hard["max_output_tokens"] == 32768


def test_flat_persona_entry() -> None:
    params = llm_params_for("reviewer", "openrouter/z-ai/glm-5.2")
    assert params["reasoning_effort"] == "high"


def test_unknown_keys_are_dropped(tmp_path: Path) -> None:
    routes = _write(
        tmp_path,
        "llm_params:\n"
        "  defaults:\n"
        "    caching_prompt: true\n"
        "    api_key: oops-not-allowed\n"
        "  personas:\n"
        "    dev:\n"
        "      standard: { temperature: 0.2, model: also-not-allowed }\n",
    )
    params = llm_params_for("dev", "some/model", routes_path=routes)
    assert set(params) == {"max_output_tokens", "caching_prompt", "temperature"}
    assert params["temperature"] == 0.2


def test_missing_block_yields_only_output_cap(tmp_path: Path) -> None:
    routes = _write(tmp_path, "routes:\n  pm: some/model\n")
    params = llm_params_for("pm", "some/model", routes_path=routes)
    assert params == {"max_output_tokens": 8192}  # hard fallback cap


def test_persona_overrides_beat_defaults(tmp_path: Path) -> None:
    routes = _write(
        tmp_path,
        "llm_params:\n"
        "  defaults: { reasoning_effort: low }\n"
        "  personas:\n"
        "    security: { reasoning_effort: xhigh }\n",
    )
    assert llm_params_for("security", "m/x", routes_path=routes)["reasoning_effort"] == "xhigh"
    assert llm_params_for("pm", "m/x", routes_path=routes)["reasoning_effort"] == "low"


def test_preset_default_is_none() -> None:
    assert preset_for("dev") is None


def test_preset_for_reads_presets_block(tmp_path: Path) -> None:
    routes = _write(tmp_path, "presets:\n  dev: planning\n")
    assert preset_for("dev", routes_path=routes) == "planning"
    assert preset_for("reviewer", routes_path=routes) is None
