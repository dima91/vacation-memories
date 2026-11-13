import subprocess
import sys

from vacation_memories import __version__


def test_main_prints_version():
    result = subprocess.run(
        [sys.executable, "-m", "vacation_memories", "--version"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=True,
    )
    assert result.stdout.strip() == f"vacation-memories {__version__}"
