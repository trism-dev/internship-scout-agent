import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_dependencies_match_requirements():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    lines = (ROOT / "requirements.txt").read_text().splitlines()
    pinned = [x.strip() for x in lines if x.strip() and not x.lstrip().startswith("#")]
    assert project["dependencies"] == pinned
