import os
import re
from pathlib import Path

import pytest
from system_fakes import FULL_FILTERS, GIB, MODEL, make_env, ollama_with_model

from vacation_memories.config import resolve_settings
from vacation_memories.doctor import (
    MIN_FREE_BYTES,
    OPTIONAL_FFMPEG_FILTERS,
    REQUIRED_FFMPEG_FILTERS,
    format_report,
    has_blocking_failures,
    run_doctor,
)
from vacation_memories.runtime_profile import detect_profile


def doctor(tmp_path, env=None, *, environ=None, hooks=None, **options):
    """Run the checks on a prepared workdir; `hooks` replace the injectable inputs of `run_doctor`."""
    options.setdefault("no_ai", True)
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    (workdir / "tmp").mkdir(exist_ok=True)
    settings = resolve_settings({"workdir": workdir, **options}, environ or {})
    profile = detect_profile(settings, env or make_env(tmp_path / "sys"))
    return {check.name: check for check in run_doctor(settings, profile, **(hooks or {}))}, profile


def test_healthy_machine_has_no_failure_or_warning(tmp_path):
    checks, _ = doctor(tmp_path)
    assert {c.name: c.status for c in checks.values() if c.status != "ok"} == {}
    assert not has_blocking_failures(checks.values())


def test_python_older_than_3_11_is_blocking_with_a_clear_message(tmp_path):
    checks, _ = doctor(tmp_path, hooks={"python_version": (3, 10, 12)})
    assert checks["python"].status == "fail"
    assert "3.11" in checks["python"].message and "3.10.12" in checks["python"].message


def test_python_version_is_reported_when_supported(tmp_path):
    checks, _ = doctor(tmp_path, hooks={"python_version": (3, 12, 1)})
    assert checks["python"].status == "ok" and "3.12.1" in checks["python"].message


def test_image_version_is_reported(tmp_path):
    checks, _ = doctor(tmp_path, hooks={"image_version": "1.4.0"})
    assert "1.4.0" in checks["image"].message
    assert checks["image"].status == "ok"


def test_outside_a_release_image_the_version_is_reported_as_unknown(tmp_path):
    checks, _ = doctor(tmp_path, hooks={"image_version": ""})
    assert checks["image"].status == "ok" and "not" in checks["image"].message


class TestFFmpeg:
    def test_versions_are_reported(self, tmp_path):
        checks, _ = doctor(tmp_path)
        assert "5.1.6" in checks["ffmpeg"].message
        assert "5.1.6" in checks["ffprobe"].message

    def test_missing_ffmpeg_is_blocking(self, tmp_path):
        checks, _ = doctor(tmp_path, make_env(tmp_path / "sys", ffmpeg=False))
        assert checks["ffmpeg"].status == "fail"
        assert has_blocking_failures(checks.values())

    def test_missing_ffprobe_is_blocking(self, tmp_path):
        checks, _ = doctor(tmp_path, make_env(tmp_path / "sys", ffprobe=False))
        assert checks["ffprobe"].status == "fail"

    @pytest.mark.parametrize("missing", ["libx264", "aac"])
    def test_missing_required_encoder_is_blocking(self, tmp_path, missing):
        encoders = [name for name in ("libx264", "aac", "mpeg4") if name != missing]
        checks, _ = doctor(tmp_path, make_env(tmp_path / "sys", encoders=encoders))
        assert checks["ffmpeg-encoders"].status == "fail"
        assert missing in checks["ffmpeg-encoders"].message
        assert has_blocking_failures(checks.values())

    @pytest.mark.parametrize("missing", REQUIRED_FFMPEG_FILTERS)
    def test_missing_required_filter_is_blocking(self, tmp_path, missing):
        filters = [name for name in FULL_FILTERS if name != missing]
        checks, _ = doctor(tmp_path, make_env(tmp_path / "sys", filters=filters))
        assert checks["ffmpeg-filters"].status == "fail"
        assert missing in checks["ffmpeg-filters"].message

    def test_missing_optional_filters_only_warn(self, tmp_path):
        filters = [name for name in FULL_FILTERS if name not in OPTIONAL_FFMPEG_FILTERS]
        checks, _ = doctor(tmp_path, make_env(tmp_path / "sys", filters=filters))
        assert checks["ffmpeg-filters"].status == "ok"
        assert checks["ffmpeg-optional-filters"].status == "warn"
        assert "zscale" in checks["ffmpeg-optional-filters"].message
        assert not has_blocking_failures(checks.values())

    def test_filter_list_is_the_same_as_the_image_build_check(self):
        dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
        build_filters = re.search(r"for f in ([^;]+); do", dockerfile).group(1).split()
        assert sorted(build_filters) == sorted(REQUIRED_FFMPEG_FILTERS)


class TestLibraries:
    def test_heic_decoder_is_registered_in_the_dev_image(self, tmp_path):
        assert doctor(tmp_path)[0]["pillow-heif"].status == "ok"

    def test_heic_decoder_missing_only_warns(self, tmp_path):
        checks, _ = doctor(tmp_path, hooks={"heic_available": lambda: False})
        assert checks["pillow-heif"].status == "warn"
        assert "HEIC" in checks["pillow-heif"].message
        assert not has_blocking_failures(checks.values())

    def test_missing_required_python_library_is_blocking(self, tmp_path):
        checks, _ = doctor(tmp_path, hooks={"module_available": lambda name: name != "numpy"})
        assert checks["python-libraries"].status == "fail"
        assert "numpy" in checks["python-libraries"].message
        assert has_blocking_failures(checks.values())


class TestWorkdir:
    def test_writable_workdir_and_tmp(self, tmp_path):
        checks, _ = doctor(tmp_path)
        assert checks["workdir"].status == "ok"
        assert checks["tmp"].status == "ok"
        assert [p for p in (tmp_path / "work").iterdir() if p.name.startswith(".doctor")] == []

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
    def test_read_only_workdir_is_blocking(self, tmp_path):
        env = make_env(tmp_path / "sys")
        workdir = tmp_path / "work"
        workdir.mkdir()
        (workdir / "tmp").mkdir()
        workdir.chmod(0o500)
        try:
            settings = resolve_settings({"workdir": workdir, "no_ai": True}, {})
            checks = {c.name: c for c in run_doctor(settings, detect_profile(settings, env))}
        finally:
            workdir.chmod(0o700)
        assert checks["workdir"].status == "fail"
        assert has_blocking_failures(checks.values())

    def test_missing_workdir_is_blocking_not_a_crash(self, tmp_path):
        settings = resolve_settings({"workdir": tmp_path / "absent", "no_ai": True}, {})
        checks = {c.name: c for c in run_doctor(settings, detect_profile(settings, make_env(tmp_path / "sys")))}
        assert checks["workdir"].status == "fail"

    def test_insufficient_free_space_is_blocking(self, tmp_path):
        env = make_env(tmp_path / "sys", disk_free=MIN_FREE_BYTES - 1)
        checks, _ = doctor(tmp_path, env)
        assert checks["free-space"].status == "fail"
        assert has_blocking_failures(checks.values())

    def test_unknown_free_space_only_warns(self, tmp_path):
        env = make_env(tmp_path / "sys", disk_free=OSError("nope"))
        checks, _ = doctor(tmp_path, env)
        assert checks["free-space"].status == "warn"

    def test_detected_limits_are_reported(self, tmp_path):
        from system_fakes import write_cgroup_v2

        env = make_env(tmp_path / "sys", cores=8)
        write_cgroup_v2(env, cpu_max="100000 100000", memory_max=GIB, memory_current=0)
        checks, _ = doctor(tmp_path, env)
        assert checks["limits"].status == "ok"
        assert "1 core" in checks["limits"].message
        assert "1.0 GiB" in checks["limits"].message


class TestOllama:
    def test_not_checked_with_no_ai_even_if_the_configuration_requires_it(self, tmp_path):
        env = make_env(tmp_path / "sys", ollama=None)
        checks, _ = doctor(tmp_path, env, no_ai=True, environ={"VACATION_MEMORIES_REQUIRE_AI": "1"})
        assert checks["ollama"].status == "ok" and "not checked" in checks["ollama"].message
        assert env.http_calls == []

    def test_unreachable_warns_by_default(self, tmp_path):
        checks, _ = doctor(tmp_path, make_env(tmp_path / "sys", ollama=None), no_ai=False)
        assert checks["ollama"].status == "warn"
        assert not has_blocking_failures(checks.values())

    def test_unreachable_is_blocking_with_require_ai(self, tmp_path):
        checks, _ = doctor(tmp_path, make_env(tmp_path / "sys", ollama=None), no_ai=False, require_ai=True)
        assert checks["ollama"].status == "fail"
        assert has_blocking_failures(checks.values())

    def test_missing_model_warns_or_blocks_like_an_unreachable_server(self, tmp_path):
        env = make_env(tmp_path / "sys", ollama=ollama_with_model(name="llava:7b"))
        warned, _ = doctor(tmp_path, env, no_ai=False)
        assert warned["ollama"].status == "warn" and MODEL in warned["ollama"].message
        blocked, _ = doctor(tmp_path, env, no_ai=False, require_ai=True)
        assert blocked["ollama"].status == "fail"

    def test_model_present_is_ok(self, tmp_path):
        env = make_env(tmp_path / "sys", ollama=ollama_with_model())
        checks, _ = doctor(tmp_path, env, no_ai=False, require_ai=True)
        assert checks["ollama"].status == "ok"
        assert not has_blocking_failures(checks.values())

    def test_where_a_loaded_model_runs_is_reported(self, tmp_path):
        env = make_env(tmp_path / "sys", ollama=ollama_with_model(loaded={"size_vram": 0}))
        checks, _ = doctor(tmp_path, env, no_ai=False)
        assert "cpu" in checks["ollama"].message.lower()


def test_report_lists_every_check_the_profile_and_the_verdict(tmp_path):
    env = make_env(tmp_path / "sys", encoders=("aac",))
    checks, profile = doctor(tmp_path, env)
    text = "\n".join(format_report(list(checks.values()), profile))
    assert "[ OK ] python" in text
    assert "[FAIL] ffmpeg-encoders" in text
    assert "CPU:" in text and "Derived:" in text
    assert text.rstrip().endswith("blocked")


def test_report_of_a_healthy_machine_says_ok(tmp_path):
    checks, profile = doctor(tmp_path)
    lines = format_report(list(checks.values()), profile)
    assert lines[-1].endswith("ok")
