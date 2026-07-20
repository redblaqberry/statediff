"""StateDiff: state oracle for agent evaluation over snapshot.v1 artifacts."""

__version__ = "0.1.0"

from .adapter import ArtifactError, load_event_log, load_pair, load_snapshot
from .engine import evaluate, evaluate_paths
from .evidence import render
from .gate import to_gate_checks
from .models import Verdict
from .scenario import Scenario, ScenarioError, load_scenario

__all__ = [
    "ArtifactError",
    "Scenario",
    "ScenarioError",
    "Verdict",
    "__version__",
    "evaluate",
    "evaluate_paths",
    "load_event_log",
    "load_pair",
    "load_scenario",
    "load_snapshot",
    "render",
    "to_gate_checks",
]
