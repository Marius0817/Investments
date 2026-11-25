from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stopguard import fake_sc  # noqa: E402
from stopguard.config import load_config  # noqa: E402
from stopguard.demo import init_demo  # noqa: E402
from stopguard.engine import Engine  # noqa: E402
from stopguard.notify import Notifier  # noqa: E402
from stopguard.sc import ScClient  # noqa: E402
from stopguard.state import StateStore  # noqa: E402


def inprocess_runner(cmd, capture_output=True, text=True, timeout=None):  # noqa: ARG001
    """Führt die simulierte CLI im selben Prozess aus (schneller als Subprozesse)."""
    args = cmd[cmd.index("stopguard.fake_sc") + 1 :]
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = fake_sc.main(args)
    return subprocess.CompletedProcess(cmd, code, buf.getvalue(), "")


class Demo:
    def __init__(self, tmp_path: Path):
        self.config_path = init_demo(tmp_path / "demo")
        self.world_path = tmp_path / "demo" / "demo-world.json"
        self.cfg = load_config(self.config_path)
        self.store = StateStore(self.cfg.state_dir)
        self.client = ScClient(command=self.cfg.scalable.sc_command, runner=inprocess_runner)
        self.notifier = Notifier(self.cfg.notify, dry_run=True)

    def world(self) -> dict:
        return json.loads(self.world_path.read_text())

    def edit_world(self, fn) -> None:
        w = self.world()
        fn(w)
        self.world_path.write_text(json.dumps(w, indent=2))

    def move(self, isin: str, pct: float) -> None:
        w = self.world()
        fake_sc.sim_move(w, isin, pct)
        self.world_path.write_text(json.dumps(w, indent=2))

    def analyse(self, state, isins=None):
        return Engine(self.cfg, self.client, self.store, state).analyse(isins=isins)


@pytest.fixture()
def demo(tmp_path: Path) -> Demo:
    return Demo(tmp_path)
