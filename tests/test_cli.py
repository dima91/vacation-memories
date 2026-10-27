import json
import os
import subprocess
import sys

import pytest
from system_fakes import FULL_FILTERS, make_env, ollama_with_model

from vacation_memories import cli, exit_codes
from vacation_memories.workdir import WorkdirLock

pytestmark = pytest.mark.usefixtures("isolate_process_state")


def run(tmp_path, *args, env=None, environ=None):
    code = cli.main(list(args), environ=environ or {}, detection_env=env or make_env(tmp_path / "sys"))
    return code


def test_version_flag_prints_the_version(capsys):
    assert cli.main(["--version"], environ={}) == 0
    assert capsys.readouterr().out.startswith("vacation-memories ")


class TestWithoutSubcommand:
    """Without a subcommand the pipeline runs (its own tests are in test_pipeline.py)."""

    def test_options_are_validated(self, tmp_path, capsys):
        assert run(tmp_path, "--config", "/nonexistent.toml") == exit_codes.USAGE
        assert capsys.readouterr().err.startswith("error:")

    def test_contradictory_flags_exit_2(self, tmp_path):
        assert run(tmp_path, "--require-ai", "--no-ai") == exit_codes.USAGE

    def test_the_input_folder_is_required(self, tmp_path, capsys):
        assert run(tmp_path, "--no-ai", "--workdir", str(tmp_path / "work")) == exit_codes.USAGE
        assert "--input is required" in capsys.readouterr().err

    def test_the_version_flag_does_not_look_at_the_environment(self, tmp_path):
        assert cli.main(["--version"], environ={"OLLAMA_HOST": "http://remote-host:1"}) == exit_codes.OK


class TestUnusableFiles:
    @pytest.fixture
    def read_only_workdir(self, tmp_path, monkeypatch):
        """An existing workdir we cannot write to (e.g. a bind mount of another user)."""
        if os.geteuid() == 0:
            pytest.skip("root ignores permissions")
        workdir = tmp_path / "work"
        (workdir / "tmp").mkdir(parents=True)
        workdir.chmod(0o500)
        monkeypatch.setattr(cli, "prepare_workdir", lambda path: None)  # it would simply chmod it back
        yield workdir
        workdir.chmod(0o700)

    def test_read_only_workdir_gives_a_report_and_exit_4_not_a_traceback(self, tmp_path, capsys, read_only_workdir):
        code = run(tmp_path, "doctor", "--no-ai", "--workdir", str(read_only_workdir))
        assert code == exit_codes.REQUIREMENTS
        out = capsys.readouterr()
        assert "[FAIL] workdir" in out.out
        assert "Traceback" not in out.err

    def test_default_log_file_that_cannot_be_used_falls_back_to_the_console(self, tmp_path, capsys, read_only_workdir):
        run(tmp_path, "doctor", "--no-ai", "--workdir", str(read_only_workdir))
        assert "console only" in capsys.readouterr().err

    def test_failure_to_save_the_profile_is_a_warning(self, tmp_path, capsys, monkeypatch):
        def broken(profile, path):
            raise PermissionError("read-only file system")

        monkeypatch.setattr(cli, "write_runtime_profile", broken)
        assert run(tmp_path, "doctor", "--no-ai", "--workdir", str(tmp_path / "work")) == exit_codes.OK
        assert "runtime_profile.json" in capsys.readouterr().err

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
    def test_existing_read_only_default_log_file_falls_back_to_the_console(self, tmp_path, capsys):
        workdir = tmp_path / "work"
        workdir.mkdir()
        (workdir / "run.log").write_text("old")
        (workdir / "run.log").chmod(0o400)
        assert run(tmp_path, "doctor", "--no-ai", "--workdir", str(workdir)) == exit_codes.OK
        assert "console only" in capsys.readouterr().err
        assert (workdir / "run.log").read_text() == "old"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
    def test_explicit_log_file_that_cannot_be_written_exits_2(self, tmp_path, capsys):
        log = tmp_path / "job.log"
        log.write_text("")
        log.chmod(0o400)
        code = run(tmp_path, "doctor", "--no-ai", "--workdir", str(tmp_path / "work"), "--log-file", str(log))
        assert code == exit_codes.USAGE
        assert "log file" in capsys.readouterr().err

    def test_explicit_log_file_that_is_a_folder_exits_2(self, tmp_path):
        folder = tmp_path / "logs"
        folder.mkdir()
        code = run(tmp_path, "doctor", "--no-ai", "--workdir", str(tmp_path / "work"), "--log-file", str(folder))
        assert code == exit_codes.USAGE


class TestDoctorCommand:
    def test_healthy_machine_exits_0_and_prints_the_report(self, tmp_path, capsys):
        assert run(tmp_path, "doctor", "--no-ai", "--workdir", str(tmp_path / "work")) == exit_codes.OK
        out = capsys.readouterr().out
        assert "[ OK ] python" in out and "CPU:" in out

    def test_missing_libx264_exits_4(self, tmp_path, capsys):
        env = make_env(tmp_path / "sys", encoders=("aac",))
        assert run(tmp_path, "doctor", "--no-ai", "--workdir", str(tmp_path / "work"), env=env) == exit_codes.REQUIREMENTS
        assert "[FAIL] ffmpeg-encoders" in capsys.readouterr().out

    def test_missing_filter_exits_4(self, tmp_path):
        env = make_env(tmp_path / "sys", filters=[f for f in FULL_FILTERS if f != "xfade"])
        assert run(tmp_path, "doctor", "--no-ai", "--workdir", str(tmp_path / "work"), env=env) == exit_codes.REQUIREMENTS

    def test_require_ai_without_ollama_exits_4_but_no_ai_does_not(self, tmp_path):
        env = make_env(tmp_path / "sys", ollama=None)
        workdir = str(tmp_path / "work")
        assert run(tmp_path, "doctor", "--require-ai", "--workdir", workdir, env=env) == exit_codes.REQUIREMENTS
        assert run(tmp_path, "doctor", "--workdir", workdir, env=env) == exit_codes.OK
        assert run(tmp_path, "doctor", "--no-ai", "--workdir", workdir, env=env) == exit_codes.OK

    def test_require_ai_with_ollama_exits_0(self, tmp_path):
        env = make_env(tmp_path / "sys", ollama=ollama_with_model())
        assert run(tmp_path, "doctor", "--require-ai", "--workdir", str(tmp_path / "work"), env=env) == exit_codes.OK

    def test_writes_runtime_profile_and_log_in_a_private_workdir(self, tmp_path):
        workdir = tmp_path / "work"
        run(tmp_path, "doctor", "--no-ai", "--workdir", str(workdir))
        assert json.loads((workdir / "runtime_profile.json").read_text())["cpu"]["usable_cores"] == 8
        assert (workdir / "run.log").exists()
        assert (workdir / "tmp").is_dir()
        assert workdir.stat().st_mode & 0o777 == 0o700
        assert os.environ["TMPDIR"] == str(workdir / "tmp")

    def test_does_not_need_the_workdir_lock(self, tmp_path):
        workdir = tmp_path / "work"
        workdir.mkdir()
        with WorkdirLock(workdir):
            assert run(tmp_path, "doctor", "--no-ai", "--workdir", str(workdir)) == exit_codes.OK

    def test_unusable_workdir_exits_4_with_a_report(self, tmp_path, capsys):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        assert run(tmp_path, "doctor", "--no-ai", "--workdir", str(blocker / "work")) == exit_codes.REQUIREMENTS
        assert "[FAIL] workdir" in capsys.readouterr().out

    def test_json_log_format(self, tmp_path):
        workdir = tmp_path / "work"
        run(tmp_path, "doctor", "--no-ai", "--workdir", str(workdir), "--log-format", "json")
        lines = [json.loads(line) for line in (workdir / "run.log").read_text().splitlines()]
        assert all({"ts", "level", "message"} <= line.keys() for line in lines)

    def test_log_file_relative_path_is_inside_the_workdir(self, tmp_path):
        workdir = tmp_path / "work"
        (workdir / "logs").mkdir(parents=True)
        run(tmp_path, "doctor", "--no-ai", "--workdir", str(workdir), "--log-file", "logs/job.log")
        assert (workdir / "logs" / "job.log").exists()
        assert not (workdir / "run.log").exists()

    def test_absolute_log_file_outside_the_workdir_is_allowed(self, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        run(tmp_path, "doctor", "--no-ai", "--workdir", str(tmp_path / "work"), "--log-file", str(elsewhere / "a.log"))
        assert (elsewhere / "a.log").exists()


class TestOptions:
    def test_options_work_before_and_after_the_subcommand(self, tmp_path, isolate_process_state):
        workdir = str(tmp_path / "work")
        run(tmp_path, "--nice", "3", "--workdir", workdir, "--no-ai", "doctor")
        run(tmp_path, "doctor", "--nice", "5", "--workdir", workdir, "--no-ai")
        assert isolate_process_state == [3, 5]

    def test_nice_zero_does_not_touch_the_priority(self, tmp_path, isolate_process_state):
        run(tmp_path, "doctor", "--no-ai", "--workdir", str(tmp_path / "work"))
        assert isolate_process_state == []

    def test_private_umask_is_set(self, tmp_path):
        run(tmp_path, "doctor", "--no-ai", "--workdir", str(tmp_path / "work"))
        previous = os.umask(0)
        os.umask(previous)
        assert previous == 0o077

    def test_configuration_file_and_environment_are_honored(self, tmp_path, isolate_process_state):
        config = tmp_path / "config.toml"
        config.write_text(f'workdir = "{tmp_path / "from-file"}"\nno_ai = true\nnice = 2\n')
        run(tmp_path, "doctor", environ={"VACATION_MEMORIES_CONFIG": str(config), "VACATION_MEMORIES_NICE": "4"})
        assert (tmp_path / "from-file" / "runtime_profile.json").exists()
        assert isolate_process_state == [4]

    @pytest.mark.parametrize(
        "args",
        [
            ["--nice", "99"],
            ["--require-ai", "--no-ai"],
            ["--config", "/nonexistent/config.toml"],
            ["--assume-tz", "Mars/Olympus"],
            ["--log-format", "xml"],
            ["--ollama-url", "http://ollama:11434"],
            ["--ollama-url", "ollama:11434", "--allow-remote-ollama"],
            ["--jobs", "0"],
        ],
    )
    def test_invalid_configuration_exits_2_with_a_message(self, tmp_path, capsys, args):
        code = run(tmp_path, "doctor", "--workdir", str(tmp_path / "work"), *args)
        assert code == exit_codes.USAGE
        assert capsys.readouterr().err.startswith("error:")

    def test_unknown_option_exits_2(self, tmp_path):
        with pytest.raises(SystemExit) as raised:
            run(tmp_path, "doctor", "--frobnicate")
        assert raised.value.code == exit_codes.USAGE

    def test_log_file_in_a_missing_folder_exits_2_before_doing_any_work(self, tmp_path, capsys):
        code = run(tmp_path, "doctor", "--no-ai", "--workdir", str(tmp_path / "work"), "--log-file", "nope/job.log")
        assert code == exit_codes.USAGE
        assert "log file" in capsys.readouterr().err

    def test_remote_ollama_with_permission_logs_a_warning(self, tmp_path):
        workdir = tmp_path / "work"
        env = make_env(tmp_path / "sys", ollama=ollama_with_model())
        run(tmp_path, "doctor", "--workdir", str(workdir), "--ollama-url", "http://ollama:11434", "--allow-remote-ollama", env=env)
        assert "not local" in (workdir / "run.log").read_text()


def test_real_machine_in_a_headless_subprocess(tmp_path):
    """No terminal, no display, `LANG=C`, unusable TMPDIR, non-ASCII workdir: doctor still works."""
    workdir = tmp_path / "città"
    environment = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    environment.update(LANG="C", LC_ALL="C", TZ="UTC", TMPDIR="/nonexistent", PYTHONPATH=os.getcwd())
    result = subprocess.run(
        [sys.executable, "-m", "vacation_memories", "doctor", "--no-ai", "--workdir", str(workdir)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=environment,
        timeout=120,
    )
    out = result.stdout.decode("utf-8")
    assert result.returncode == 0, out + result.stderr.decode("utf-8", "replace")
    assert "libx264" in out and "[ OK ] ffmpeg" in out
    profile = json.loads((workdir / "runtime_profile.json").read_text())
    assert profile["cpu"]["usable_cores"] >= 1 and profile["ffmpeg"]["has_libx264"] is True
    assert (workdir / "run.log").exists()
    assert b"\r" not in result.stderr and b"\x1b" not in result.stderr
