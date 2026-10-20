"""Earlier-iteration instructions are a file the agent can choose to read."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from slop_code.agent_runner import runner
from slop_code.agent_runner.resume import _generate_expected_prompt
from slop_code.agent_runner.runner import AgentRunnerError
from slop_code.common import PREVIOUS_REQUIREMENTS_FILENAME
from slop_code.common import render
from slop_code.entrypoints.commands import render_prompts
from slop_code.evaluation import CheckpointConfig
from slop_code.evaluation import ProblemConfig
from slop_code.execution.local_streaming import LocalEnvironmentSpec
from slop_code.execution.models import CommandConfig

REPO = Path(__file__).resolve().parents[2]
PROMPTS = REPO / "configs" / "prompts"
SPEC_1 = (
    "<!-- SENTINEL PHRASE: VIOLET ASTEROID -->\n"
    "Run %%%ENTRYPOINT:entry_command%%% SPEC_ALPHA_9f3a\n"
)
SPEC_2 = "SPEC_BETA_9f3a uses %%%ENTRYPOINT:entry_file%%%\n"
SPEC_3 = "SPEC_GAMMA_9f3a\n"
REFERENCE = (
    "The instructions from earlier iterations are in "
    "`previous_requirements.md` in the current directory. You can "
    "read that file to better understand the previous requirements."
)


def _environment() -> LocalEnvironmentSpec:
    return LocalEnvironmentSpec(
        type="local",
        name="test",
        commands=CommandConfig(
            command="uv run",
            agent_command="python",
            entry_file="{entry_file}.py",
        ),
    )


def _problem(items: list[tuple[str, int, str]]) -> ProblemConfig:
    checkpoints = {
        name: CheckpointConfig(
            name=name,
            version=1,
            order=order,
            spec_override=spec,
        )
        for name, order, spec in items
    }
    return ProblemConfig(
        name="demo",
        path=Path("/unused"),
        version=1,
        description="demo",
        entry_file="main",
        tags=["test"],
        checkpoints=checkpoints,
    )


def _just_solve() -> str:
    return (PROMPTS / "just-solve.jinja").read_text(encoding="utf-8")


def test_just_solve_first_checkpoint_prompt_stays_the_same() -> None:
    rendered = render.render_prompt(
        spec_text="SPEC_ALPHA_9f3a\n",
        context=render.task_prompt_context(is_first_checkpoint=True),
        prompt_template=_just_solve(),
        entry_file="main.py",
        entry_command="python main.py",
    )
    assert rendered == (
        "Implement a program that 100% solves the specification. "
        "That is all you need to do.\n"
        "Use a virtual environment and ensure that a `requirements.txt` "
        "is present with any dependencies you need to solve the problem.\n"
        "Your task is:\n"
        "SPEC_ALPHA_9f3a\n"
    )
    assert PREVIOUS_REQUIREMENTS_FILENAME not in rendered


def test_just_solve_continuation_names_the_file_after_the_spec() -> None:
    rendered = render.render_prompt(
        spec_text="SPEC_ALPHA_9f3a\n",
        context=render.task_prompt_context(is_first_checkpoint=False),
        prompt_template=_just_solve(),
        entry_file="main.py",
        entry_command="python main.py",
    )
    assert "Keep using the same virtual environment" in rendered
    assert rendered.split("SPEC_ALPHA_9f3a", 1)[1].strip() == REFERENCE
    assert "Use a virtual environment and ensure" not in rendered


@pytest.mark.parametrize(
    "name",
    ["just-solve", "anti_slop", "plan-and-test", "plan_first"],
)
def test_shipped_prompts_reference_previous_requirements(name: str) -> None:
    template = (PROMPTS / f"{name}.jinja").read_text(encoding="utf-8")
    first = render.render_prompt(
        spec_text="SPEC_ALPHA_9f3a",
        context=render.task_prompt_context(is_first_checkpoint=True),
        prompt_template=template,
        entry_file="main.py",
        entry_command="python main.py",
    )
    later = render.render_prompt(
        spec_text="SPEC_ALPHA_9f3a",
        context=render.task_prompt_context(is_first_checkpoint=False),
        prompt_template=template,
        entry_file="main.py",
        entry_command="python main.py",
    )
    assert "SPEC_ALPHA_9f3a" in first
    assert "SPEC_ALPHA_9f3a" in later
    assert PREVIOUS_REQUIREMENTS_FILENAME not in first
    assert later.split("SPEC_ALPHA_9f3a", 1)[1].strip() == REFERENCE


def test_priors_follow_checkpoint_order() -> None:
    problem = _problem(
        [
            ("checkpoint_2", 2, SPEC_2),
            ("checkpoint_1", 1, SPEC_1),
            ("checkpoint_3", 3, SPEC_3),
        ]
    )
    assert runner.prior_checkpoint_names(problem, "checkpoint_1") == []
    assert runner.prior_checkpoint_names(problem, "checkpoint_3") == [
        "checkpoint_1",
        "checkpoint_2",
    ]


def test_unknown_checkpoint_is_rejected() -> None:
    problem = _problem([("checkpoint_1", 1, SPEC_1)])
    with pytest.raises(AgentRunnerError, match="checkpoint_9"):
        runner.prior_checkpoint_names(problem, "checkpoint_9")


def test_continuation_without_earlier_checkpoints_raises(
    tmp_path: Path,
) -> None:
    problem = _problem([("checkpoint_1", 1, SPEC_1)])
    with pytest.raises(AgentRunnerError, match="no earlier"):
        runner.sync_previous_requirements(
            tmp_path,
            problem,
            _environment(),
            _just_solve(),
            current_checkpoint="checkpoint_1",
            is_first_checkpoint=False,
        )


def test_history_file_holds_earlier_prompts_only(tmp_path: Path) -> None:
    problem = _problem(
        [
            ("checkpoint_1", 1, SPEC_1),
            ("checkpoint_2", 2, SPEC_2),
            ("checkpoint_3", 3, SPEC_3),
        ]
    )
    runner.sync_previous_requirements(
        tmp_path,
        problem,
        _environment(),
        _just_solve(),
        current_checkpoint="checkpoint_3",
        is_first_checkpoint=False,
    )
    text = (tmp_path / PREVIOUS_REQUIREMENTS_FILENAME).read_text(
        encoding="utf-8"
    )
    sections = text.split("\n## ")
    assert sections[0].startswith("# Instructions from previous iterations")
    assert sections[1].startswith("checkpoint_1\n")
    assert sections[2].startswith("checkpoint_2\n")
    assert len(sections) == 3
    assert "SENTINEL" not in text
    assert "SPEC_GAMMA_9f3a" not in text
    assert "SPEC_ALPHA_9f3a" in sections[1]
    assert "python main.py" in sections[1]
    assert REFERENCE not in sections[1]
    assert "SPEC_BETA_9f3a" in sections[2]
    assert "main.py" in sections[2]
    assert REFERENCE in sections[2]


def test_first_checkpoint_removes_a_leftover_file(tmp_path: Path) -> None:
    dest = tmp_path / PREVIOUS_REQUIREMENTS_FILENAME
    dest.write_text("stale\n", encoding="utf-8")
    problem = _problem([("checkpoint_1", 1, SPEC_1)])
    runner.sync_previous_requirements(
        tmp_path,
        problem,
        _environment(),
        _just_solve(),
        current_checkpoint="checkpoint_1",
        is_first_checkpoint=True,
    )
    assert not dest.exists()


def test_run_checkpoint_publishes_the_file_before_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    problem = _problem(
        [
            ("checkpoint_1", 1, SPEC_1),
            ("checkpoint_2", 2, SPEC_2),
        ]
    )
    save_dir = tmp_path / "checkpoint_2"
    save_dir.mkdir()
    seen: dict[str, object] = {}

    def _infer(**kwargs: object) -> tuple[Path, Mock, Mock]:
        dest = workspace / PREVIOUS_REQUIREMENTS_FILENAME
        seen["task"] = kwargs["task"]
        seen["history"] = dest.read_text(encoding="utf-8")
        return tmp_path / "snap", Mock(), Mock()

    monkeypatch.setattr(runner, "_run_inference", _infer)
    session = Mock()
    session.working_dir = workspace
    runner.run_checkpoint(
        agent=Mock(),
        session=session,
        save_dir=save_dir,
        checkpoint=problem.checkpoints["checkpoint_2"],
        problem=problem,
        environment=_environment(),
        template=_just_solve(),
        is_first_checkpoint=False,
    )

    task = str(seen["task"])
    history = str(seen["history"])
    assert "SPEC_BETA_9f3a" in task
    assert REFERENCE in task
    assert "SPEC_ALPHA_9f3a" in history
    assert "SPEC_BETA_9f3a" not in history
    assert "SPEC_BETA_9f3a" in (save_dir / "prompt.txt").read_text(
        encoding="utf-8"
    )


def test_run_checkpoint_on_the_first_iteration_has_no_history_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dest = workspace / PREVIOUS_REQUIREMENTS_FILENAME
    dest.write_text("stale\n", encoding="utf-8")
    problem = _problem([("checkpoint_1", 1, SPEC_1)])
    save_dir = tmp_path / "checkpoint_1"
    save_dir.mkdir()
    seen: dict[str, object] = {}

    def _infer(**kwargs: object) -> tuple[Path, Mock, Mock]:
        seen["task"] = kwargs["task"]
        seen["exists"] = dest.exists()
        return tmp_path / "snap", Mock(), Mock()

    monkeypatch.setattr(runner, "_run_inference", _infer)
    session = Mock()
    session.working_dir = workspace
    runner.run_checkpoint(
        agent=Mock(),
        session=session,
        save_dir=save_dir,
        checkpoint=problem.checkpoints["checkpoint_1"],
        problem=problem,
        environment=_environment(),
        template=_just_solve(),
        is_first_checkpoint=True,
    )

    assert seen["exists"] is False
    assert PREVIOUS_REQUIREMENTS_FILENAME not in str(seen["task"])
    assert "SPEC_ALPHA_9f3a" in str(seen["task"])


def test_resume_expects_the_prompt_the_runner_saves(tmp_path: Path) -> None:
    problem = _problem(
        [
            ("checkpoint_1", 1, SPEC_1),
            ("checkpoint_2", 2, SPEC_2),
        ]
    )
    environment = _environment()
    template = _just_solve()
    cases = [(True, "checkpoint_1"), (False, "checkpoint_2")]
    for is_first, name in cases:
        output_path = tmp_path / name
        output_path.mkdir()
        saved = runner.get_task_for_checkpoint(
            checkpoint_name=name,
            spec_text=problem.get_checkpoint_spec(name),
            template=template,
            entry_file=problem.entry_file,
            environment=environment,
            is_first_checkpoint=is_first,
            output_path=output_path,
        )
        expected = _generate_expected_prompt(
            problem,
            problem.checkpoints[name],
            template,
            environment,
            problem.entry_file,
            is_first_checkpoint=is_first,
        )
        assert saved == expected


def test_render_prompts_command_uses_the_same_reference() -> None:
    environment = _environment()
    template = _just_solve()
    first = render_prompts._render_checkpoint_prompt(
        spec_text="SPEC_ALPHA_9f3a",
        prompt_template=template,
        environment=environment,
        entry_file="main",
        is_continuation=False,
    )
    later = render_prompts._render_checkpoint_prompt(
        spec_text="SPEC_ALPHA_9f3a",
        prompt_template=template,
        environment=environment,
        entry_file="main",
        is_continuation=True,
    )
    assert PREVIOUS_REQUIREMENTS_FILENAME not in first
    assert later.split("SPEC_ALPHA_9f3a", 1)[1].strip() == REFERENCE
