"""Helpers for the tests that read .github/workflows/ci.yml as text (there is no YAML library in the dev image)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def workflow_text():
    return WORKFLOW.read_text(encoding="utf-8")


def job_block(name):
    """The lines of one job of the workflow, from the line after `  name:` up to the next job."""
    match = re.search(rf"^  {re.escape(name)}:\n(?P<body>(?:(?:    .*)?\n)+)", workflow_text(), re.M)
    assert match, f"job {name} not found in ci.yml"
    return match.group("body")


def run_scripts(block):
    """The shell text of every `run:` step of a block."""
    scripts = re.findall(r"^        run: \|\n((?:          .*\n|\n)+)", block, re.M)
    scripts += re.findall(r"^        run: (?!\|)(.+)$", block, re.M)
    return scripts
