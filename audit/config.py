"""Load per-stage configuration from config/stages.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class StageConfig:
    name: str
    model: str
    concurrency: int
    tools: list[str]
    max_turns: int
    permission_mode: str
    repair_attempts: int


@dataclass
class FileCountConfig:
    """Rules for counting source files (feeds dynamic sizing caps)."""
    source_extensions: list[str] = field(default_factory=list)


@dataclass
class HarnessConfig:
    stages: dict[str, StageConfig] = field(default_factory=dict)
    gapfill_iterations: int = 2
    feedback_iterations: int = 1
    file_count: FileCountConfig = field(default_factory=FileCountConfig)

    def get(self, stage: str) -> StageConfig:
        try:
            return self.stages[stage]
        except KeyError:
            raise KeyError(
                f"Unknown stage {stage!r}. Known: {sorted(self.stages)}"
            ) from None

    def cap_concurrency(self, cap: int) -> None:
        """Mutate every stage's concurrency to min(current, cap). Useful
        for cost-contained test runs."""
        if cap < 1:
            raise ValueError("concurrency cap must be >= 1")
        for sc in self.stages.values():
            sc.concurrency = min(sc.concurrency, cap)

    def cap_max_turns(self, cap: int) -> None:
        """Mutate every stage's max_turns to min(current, cap).

        Called by the orchestrator for small repos where the default
        25-turn allowance would waste tokens on tool-loop overhead.
        """
        if cap < 1:
            raise ValueError("max_turns cap must be >= 1")
        for sc in self.stages.values():
            sc.max_turns = min(sc.max_turns, cap)

    def cap_loop_iterations(self, gapfill: int, feedback: int) -> None:
        """Reduce the Gapfill and Feedback loop counts.

        Called for small repos where extra hunt iterations are wasteful.
        """
        self.gapfill_iterations = min(self.gapfill_iterations, gapfill)
        self.feedback_iterations = min(self.feedback_iterations, feedback)


def load_config(path: Path | None = None) -> HarnessConfig:
    if path is None:
        path = Path(__file__).resolve().parent.parent / "config" / "stages.yaml"
    raw = yaml.safe_load(path.read_text())
    defaults = raw.get("defaults", {}) or {}
    stages: dict[str, StageConfig] = {}
    for name, spec in (raw.get("stages") or {}).items():
        stages[name] = StageConfig(
            name=name,
            model=spec["model"],
            concurrency=int(spec["concurrency"]),
            tools=list(spec["tools"]),
            max_turns=int(spec.get("max_turns", defaults.get("max_turns", 25))),
            permission_mode=spec.get(
                "permission_mode", defaults.get("permission_mode", "acceptEdits")
            ),
            repair_attempts=int(
                spec.get("repair_attempts", defaults.get("repair_attempts", 1))
            ),
        )
    loops = raw.get("loops", {}) or {}
    sizing = raw.get("sizing", {}) or {}
    file_count = FileCountConfig(
        source_extensions=list(sizing.get("source_extensions", [])),
    )
    return HarnessConfig(
        stages=stages,
        gapfill_iterations=int(loops.get("gapfill_iterations", 2)),
        feedback_iterations=int(loops.get("feedback_iterations", 1)),
        file_count=file_count,
    )
