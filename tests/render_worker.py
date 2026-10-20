"""Run a render in its own process, the way the CLI will: stdin closed, no terminal and no DISPLAY
(the tests start it that way), signals turned into an exit code.

Usage: python render_worker.py INPUT_DIR WORKDIR OUTPUT PHOTO...
Renders one slow clip (a Ken Burns photo at 720p) per PHOTO name, one job at a time, so a test has
time to send a signal in the middle of it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from render_fakes import make_plan, photo_element  # noqa: E402

from vacation_memories.process import Interrupted, ShutdownController  # noqa: E402
from vacation_memories.render_config import RenderConfig  # noqa: E402
from vacation_memories.renderer import RenderOptions, render_plan  # noqa: E402
from vacation_memories.workdir import StatusWriter  # noqa: E402


def main() -> int:
    input_dir, workdir, output, *photos = sys.argv[1:]
    status = StatusWriter(Path(workdir) / "status.json")
    status.start()
    plan = make_plan([photo_element(name, 3.0) for name in photos], 1280, 720)
    options = RenderOptions(jobs=1, config=RenderConfig(fade_in_seconds=0, fade_out_seconds=0))
    with ShutdownController() as controller:
        try:
            result = render_plan(plan, input_dir=Path(input_dir), workdir=Path(workdir), output=Path(output),
                                 options=options, controller=controller, status=status)
        except Interrupted as interruption:
            return interruption.exit_code
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
