"""Unit tests for the OpenHands agent cutover behavior."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from slop_code.agent_runner.agents.cli_utils import AgentCommandResult
from slop_code.agent_runner.agents.openhands import OpenHandsAgent
from slop_code.agent_runner.agents.openhands import OpenHandsConfig
from slop_code.agent_runner.credentials import CredentialType
from slop_code.agent_runner.credentials import ProviderCredential
from slop_code.agent_runner.models import AgentCostLimits
from slop_code.common.llms import APIPricing
from slop_code.common.llms import ModelDefinition
from slop_code.execution import Session


class FakeRuntime:
    """Minimal runtime stub for setup/cleanup tests."""

    def __init__(self) -> None:
        self.cleaned = False

    def cleanup(self) -> None:
        self.cleaned = True


@dataclass
class FakeSession:
    """Fake session that captures spawn kwargs."""

    runtime: FakeRuntime
    working_dir: Path
    spec: object | None = None
    last_spawn_env_vars: dict[str, str] | None = None
    last_spawn_mounts: dict[str, dict[str, str] | str] | None = None

    def spawn(self, **kwargs: object) -> FakeRuntime:
        self.last_spawn_env_vars = cast(
            "dict[str, str] | None", kwargs.get("env_vars")
        )
        self.last_spawn_mounts = cast(
            "dict[str, dict[str, str] | str] | None", kwargs.get("mounts")
        )
        return self.runtime


def _make_credential(provider: str, key_name: str) -> ProviderCredential:
    return ProviderCredential(
        provider=provider,
        value="test-key",
        source=key_name,
        destination_key=key_name,
        credential_type=CredentialType.ENV_VAR,
    )


def test_from_config_uses_provider_slug_and_openrouter_base_url() -> None:
    config = OpenHandsConfig(
        type="openhands",
        version="1.1.0",
        cost_limits=AgentCostLimits(
            step_limit=0,
            cost_limit=10.0,
            net_cost_limit=0.0,
        ),
    )
    model = ModelDefinition(
        internal_name="gpt-5",
        provider="openai",
        pricing=APIPricing(input=1.0, output=2.0),
        provider_slugs={"openrouter": "openai/gpt-5"},
    )
    credential = _make_credential("openrouter", "OPENROUTER_API_KEY")

    agent = OpenHandsAgent._from_config(
        config=config,
        model=model,
        credential=credential,
        problem_name="p",
        verbose=False,
        image="img",
    )
    cast_agent = cast("OpenHandsAgent", agent)

    assert cast_agent.model == "openai/gpt-5"
    assert cast_agent.base_url == "https://openrouter.ai/api/v1"


def test_from_config_openrouter_base_url_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_BASE", "http://gx01:8000/v1")
    monkeypatch.setenv("API_BASE", "http://gx02:8000/v1")
    config = OpenHandsConfig(
        type="openhands",
        version="1.1.0",
        cost_limits=AgentCostLimits(
            step_limit=0,
            cost_limit=10.0,
            net_cost_limit=0.0,
        ),
    )
    model = ModelDefinition(
        internal_name="gpt-5",
        provider="openai",
        pricing=APIPricing(input=1.0, output=2.0),
        provider_slugs={"openrouter": "openai/gpt-5"},
    )
    credential = _make_credential("openrouter", "OPENROUTER_API_KEY")

    agent = OpenHandsAgent._from_config(
        config=config,
        model=model,
        credential=credential,
        problem_name="p",
        verbose=False,
        image="img",
    )
    cast_agent = cast("OpenHandsAgent", agent)

    assert cast_agent.base_url == "https://openrouter.ai/api/v1"


def test_from_config_uses_openhands_model_name_and_openai_api_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_BASE", "http://gx01:8000/v1")
    monkeypatch.delenv("API_BASE", raising=False)
    config = OpenHandsConfig(
        type="openhands",
        version="1.1.0",
        cost_limits=AgentCostLimits(
            step_limit=0,
            cost_limit=10.0,
            net_cost_limit=0.0,
        ),
    )
    model = ModelDefinition(
        internal_name="qwen3827b",
        provider="openai",
        pricing=APIPricing(input=0.0, output=0.0),
        agent_specific={"openhands": {"model_name": "openai/qwen3.8-27b"}},
    )
    credential = _make_credential("openai", "OPENAI_API_KEY")

    agent = OpenHandsAgent._from_config(
        config=config,
        model=model,
        credential=credential,
        problem_name="p",
        verbose=False,
        image="img",
    )
    cast_agent = cast("OpenHandsAgent", agent)

    assert cast_agent.model == "openai/qwen3.8-27b"
    assert cast_agent.base_url == "http://gx01:8000/v1"


def test_from_config_reads_api_base_when_openai_api_base_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.setenv("API_BASE", "http://gx02:8000/v1")
    config = OpenHandsConfig(
        type="openhands",
        version="1.1.0",
        cost_limits=AgentCostLimits(
            step_limit=0,
            cost_limit=10.0,
            net_cost_limit=0.0,
        ),
    )
    model = ModelDefinition(
        internal_name="qwen3827b",
        provider="openai",
        pricing=APIPricing(input=0.0, output=0.0),
    )
    credential = _make_credential("openai", "OPENAI_API_KEY")

    agent = OpenHandsAgent._from_config(
        config=config,
        model=model,
        credential=credential,
        problem_name="p",
        verbose=False,
        image="img",
    )
    cast_agent = cast("OpenHandsAgent", agent)

    assert cast_agent.model == "qwen3827b"
    assert cast_agent.base_url == "http://gx02:8000/v1"


def test_prepare_runtime_execution_uses_local_runtime_contract() -> None:
    agent = OpenHandsAgent(
        problem_name="p",
        verbose=False,
        image="img",
        cost_limits=AgentCostLimits(
            step_limit=12,
            cost_limit=3.5,
            net_cost_limit=0.0,
        ),
        pricing=APIPricing(input=1.0, output=1.0),
        credential=_make_credential("openai", "OPENAI_API_KEY"),
        model="gpt-5",
        base_url="https://api.openai.com/v1",
        timeout=None,
        env={},
    )

    command, env = agent._prepare_runtime_execution("solve this task")

    assert env["RUNTIME"] == "local"
    assert env["SU_TO_USER"] == "false"
    assert env["FILE_STORE"] == "local"
    assert env["SAVE_TRAJECTORY_PATH"] == "/openhands/output/trajectory.json"
    assert env["LLM_MODEL"] == "openai/gpt-5"
    assert env["LLM_BASE_URL"] == "https://api.openai.com/v1"
    assert env["MAX_ITERATIONS"] == "12"
    assert env["MAX_BUDGET_PER_TASK"] == "3.5"
    assert "SANDBOX_VOLUMES=${PWD}:/workspace:rw" in command
    assert "USER=$(id -un | tr -c 'A-Za-z0-9_-' '_')" in command
    assert "/opt/openhands-venv/bin/python -u -m openhands.core.main" in command
    assert "--task='solve this task'" in command


def test_reset_clears_output_dir_between_checkpoints(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    session = FakeSession(runtime=runtime, working_dir=tmp_path)
    agent = OpenHandsAgent(
        problem_name="p",
        verbose=False,
        image="img",
        cost_limits=AgentCostLimits(
            step_limit=0,
            cost_limit=10.0,
            net_cost_limit=0.0,
        ),
        pricing=APIPricing(input=1.0, output=1.0),
        credential=_make_credential("openai", "OPENAI_API_KEY"),
        model="gpt-5",
        base_url=None,
        timeout=None,
        env={},
    )

    agent.setup(cast("Session", session))
    output_dir = agent._get_output_dir()
    (output_dir / "trajectory.json").write_text("[]")
    nested_dir = output_dir / "completions"
    nested_dir.mkdir()
    (nested_dir / "a.json").write_text("{}")
    agent._last_prompt = "stale"
    agent._events = [{"event": 1}]
    agent._stdout_lines = ["line"]

    agent.reset()

    assert list(output_dir.iterdir()) == []
    assert agent._last_prompt == ""
    assert agent._events == []
    assert agent._stdout_lines == []
    agent.cleanup()


def test_save_artifacts_copies_output_tree_and_trajectory_alias(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    session = FakeSession(runtime=runtime, working_dir=tmp_path)
    agent = OpenHandsAgent(
        problem_name="p",
        verbose=False,
        image="img",
        cost_limits=AgentCostLimits(
            step_limit=0,
            cost_limit=10.0,
            net_cost_limit=0.0,
        ),
        pricing=APIPricing(input=1.0, output=1.0),
        credential=_make_credential("openai", "OPENAI_API_KEY"),
        model="gpt-5",
        base_url=None,
        timeout=None,
        env={},
    )
    agent.setup(cast("Session", session))
    output_dir = agent._get_output_dir()
    (output_dir / "openhands.trajectory.json").write_text('[{"ok": true}]')
    completion_dir = output_dir / "completions"
    completion_dir.mkdir()
    (completion_dir / "1.json").write_text('{"x": 1}')
    agent._last_prompt = "do it"
    agent._last_command = AgentCommandResult(
        result=None,
        steps=[],
        usage_totals={},
        stdout="stdout text",
        stderr="stderr text",
    )

    save_dir = tmp_path / "artifacts"
    agent.save_artifacts(save_dir)

    assert (save_dir / "completions" / "1.json").exists()
    assert (save_dir / "trajectory.json").read_text() == '[{"ok": true}]'
    assert (save_dir / "prompt.txt").read_text() == "do it"
    assert (save_dir / "stdout.log").read_text() == "stdout text"
    assert (save_dir / "stderr.log").read_text() == "stderr text"
    agent.cleanup()


def test_setup_passes_home_override_and_mounts(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    session = FakeSession(runtime=runtime, working_dir=tmp_path)
    agent = OpenHandsAgent(
        problem_name="p",
        verbose=False,
        image="img",
        cost_limits=AgentCostLimits(
            step_limit=0,
            cost_limit=10.0,
            net_cost_limit=0.0,
        ),
        pricing=APIPricing(input=1.0, output=1.0),
        credential=_make_credential("openai", "OPENAI_API_KEY"),
        model="gpt-5",
        base_url=None,
        timeout=None,
        env={},
    )

    agent.setup(cast("Session", session))

    assert session.last_spawn_env_vars is not None
    assert session.last_spawn_env_vars["HOME"] == "/opt/agent_home"
    assert session.last_spawn_mounts is not None
    assert any(
        isinstance(mapping, dict) and mapping.get("bind") == "/openhands/output"
        for mapping in session.last_spawn_mounts.values()
    )
    agent.cleanup()
