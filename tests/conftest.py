import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures"

SILOBENCH_SEED_GOLDEN = "38d60e95a46f0f488c7a594b045df7110b774ed83db6aac35670b4720369a866"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
