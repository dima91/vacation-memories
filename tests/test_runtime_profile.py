import json
import logging
from pathlib import Path

import pytest
from system_fakes import (
    GIB,
    MODEL,
    MODEL_SIZE,
    ollama_with_model,
    make_env,
    write_cgroup_v1,
    write_cgroup_v2,
)

from vacation_memories.cache_manager import CacheManager
from vacation_memories.config import resolve_settings
from vacation_memories.media_scanner import MediaFile
from vacation_memories.metadata_extractor import metadata_params
from vacation_memories.runtime_profile import (
    CONSERVATIVE_CORES,
    CONSERVATIVE_RAM_BYTES,
    DetectionEnv,
    detect_profile,
    format_profile,
    write_runtime_profile,
)

UNLIMITED_V1 = 9223372036854771712


def settings_for(tmp_path, **cli):
    cli.setdefault("no_ai", True)
    return resolve_settings({"workdir": tmp_path / "work", **cli}, {})


def profile_for(tmp_path, env=None, **cli):
    return detect_profile(settings_for(tmp_path, **cli), env or make_env(tmp_path / "sys"))


class TestCores:
    def test_affinity_without_cgroup_limit(self, tmp_path):
        assert profile_for(tmp_path, make_env(tmp_path / "sys", cores=6)).usable_cores == 6

    @pytest.mark.parametrize(
        "cpu_max, expected",
        [("200000 100000", 2), ("max 100000", 8), ("50000 100000", 1), ("150000 100000", 1), ("900000 100000", 8)],
    )
    def test_cgroup_v2_quota_lowers_the_cores(self, tmp_path, cpu_max, expected):
        env = make_env(tmp_path / "sys", cores=8)
        write_cgroup_v2(env, cpu_max=cpu_max)
        assert profile_for(tmp_path, env).usable_cores == expected

    @pytest.mark.parametrize("quota, expected", [(300_000, 3), (-1, 8), (20_000, 1)])
    def test_cgroup_v1_quota_lowers_the_cores(self, tmp_path, quota, expected):
        env = make_env(tmp_path / "sys", cores=8)
        write_cgroup_v1(env, cfs_quota=quota)
        assert profile_for(tmp_path, env).usable_cores == expected

    def test_affinity_lower_than_the_quota_wins(self, tmp_path):
        env = make_env(tmp_path / "sys", cores=2)
        write_cgroup_v2(env, cpu_max="800000 100000")
        assert profile_for(tmp_path, env).usable_cores == 2

    def test_unreadable_detection_falls_back_to_one_core_with_a_warning(self, tmp_path, caplog):
        env = make_env(tmp_path / "sys", cores=OSError("no affinity"))
        with caplog.at_level(logging.WARNING):
            profile = profile_for(tmp_path, env)
        assert profile.usable_cores == CONSERVATIVE_CORES
        assert "cpu" in caplog.text.lower()

    def test_malformed_cgroup_file_is_ignored_with_a_warning(self, tmp_path, caplog):
        env = make_env(tmp_path / "sys", cores=4)
        write_cgroup_v2(env, cpu_max="garbage")
        with caplog.at_level(logging.WARNING):
            assert profile_for(tmp_path, env).usable_cores == 4
        assert "cpu.max" in caplog.text


class TestMemory:
    def test_host_memory_without_cgroup_limit(self, tmp_path):
        profile = profile_for(tmp_path, make_env(tmp_path / "sys", mem_available_bytes=12 * GIB))
        assert profile.usable_ram_bytes == 12 * GIB

    def test_cgroup_v2_limit_lower_than_the_host_wins(self, tmp_path):
        env = make_env(tmp_path / "sys", mem_available_bytes=32 * GIB)
        write_cgroup_v2(env, memory_max=2 * GIB, memory_current=GIB)
        assert profile_for(tmp_path, env).usable_ram_bytes == GIB

    def test_host_lower_than_the_cgroup_limit_wins(self, tmp_path):
        env = make_env(tmp_path / "sys", mem_available_bytes=3 * GIB)
        write_cgroup_v2(env, memory_max=8 * GIB, memory_current=GIB)
        assert profile_for(tmp_path, env).usable_ram_bytes == 3 * GIB

    def test_page_cache_does_not_count_as_used_memory(self, tmp_path):
        env = make_env(tmp_path / "sys", mem_available_bytes=32 * GIB)
        # The limit is "full" only because of reclaimable file cache.
        write_cgroup_v2(env, memory_max=GIB, memory_current=GIB, inactive_file=int(0.9 * GIB))
        assert profile_for(tmp_path, env).usable_ram_bytes == int(0.9 * GIB)

    def test_no_cgroup_limit_when_memory_max_is_max(self, tmp_path):
        env = make_env(tmp_path / "sys", mem_available_bytes=5 * GIB)
        write_cgroup_v2(env, memory_max="max", memory_current=GIB)
        (env.cgroup_root / "memory.stat").write_text("inactive_file 0\n")
        assert profile_for(tmp_path, env).usable_ram_bytes == 5 * GIB

    def test_cgroup_v1_limit_uses_the_working_set(self, tmp_path):
        env = make_env(tmp_path / "sys", mem_available_bytes=32 * GIB)
        write_cgroup_v1(env, memory_limit=4 * GIB, memory_usage=3 * GIB, inactive_file=2 * GIB)
        assert profile_for(tmp_path, env).usable_ram_bytes == 3 * GIB

    def test_cgroup_v1_unlimited_value_means_no_limit(self, tmp_path):
        env = make_env(tmp_path / "sys", mem_available_bytes=7 * GIB)
        write_cgroup_v1(env, memory_limit=UNLIMITED_V1, memory_usage=GIB)
        assert profile_for(tmp_path, env).usable_ram_bytes == 7 * GIB

    def test_headroom_never_goes_negative(self, tmp_path):
        env = make_env(tmp_path / "sys")
        write_cgroup_v2(env, memory_max=GIB, memory_current=2 * GIB)
        assert profile_for(tmp_path, env).usable_ram_bytes == 0

    def test_unreadable_meminfo_falls_back_to_conservative_ram_with_a_warning(self, tmp_path, caplog):
        env = make_env(tmp_path / "sys", mem_available_bytes=None)
        with caplog.at_level(logging.WARNING):
            profile = profile_for(tmp_path, env)
        assert profile.usable_ram_bytes == CONSERVATIVE_RAM_BYTES
        assert "memory" in caplog.text.lower()


class TestDisk:
    def test_free_space_of_workdir_and_tmp(self, tmp_path):
        profile = profile_for(tmp_path, make_env(tmp_path / "sys", disk_free=42 * GIB))
        assert profile.workdir_free_bytes == 42 * GIB
        assert profile.tmp_free_bytes == 42 * GIB

    def test_failure_is_unknown_not_a_crash(self, tmp_path, caplog):
        env = make_env(tmp_path / "sys", disk_free=OSError("no such folder"))
        with caplog.at_level(logging.WARNING):
            profile = profile_for(tmp_path, env)
        assert profile.workdir_free_bytes is None and profile.tmp_free_bytes is None
        assert "disk" in caplog.text.lower()


class TestFFmpeg:
    def test_version_encoders_filters_and_accelerations(self, tmp_path):
        env = make_env(
            tmp_path / "sys",
            encoders=("libx264", "aac", "h264_vaapi", "h264_qsv"),
            hwaccels=("vaapi", "qsv"),
        )
        ffmpeg = profile_for(tmp_path, env).ffmpeg
        assert ffmpeg.ffmpeg_version == "5.1.6-0+deb12u1"
        assert ffmpeg.ffprobe_version == "5.1.6-0+deb12u1"
        assert {"libx264", "aac"} <= ffmpeg.encoders
        assert {"xfade", "zscale"} <= ffmpeg.filters
        assert ffmpeg.hwaccels == ("vaapi", "qsv")
        assert ffmpeg.hw_encoders == ("h264_qsv", "h264_vaapi")

    def test_ffmpeg_without_accelerations(self, tmp_path):
        ffmpeg = profile_for(tmp_path, make_env(tmp_path / "sys", hwaccels=())).ffmpeg
        assert ffmpeg.hwaccels == () and ffmpeg.hw_encoders == ()

    def test_every_ffmpeg_call_is_non_interactive(self, tmp_path):
        env = make_env(tmp_path / "sys")
        profile_for(tmp_path, env)
        assert env.commands
        for args in env.commands:
            assert "-hide_banner" in args
            if args[0] == "ffmpeg":
                assert "-nostdin" in args

    def test_missing_ffmpeg_is_reported_not_raised(self, tmp_path, caplog):
        env = make_env(tmp_path / "sys", ffmpeg=False, ffprobe=False)
        with caplog.at_level(logging.WARNING):
            ffmpeg = profile_for(tmp_path, env).ffmpeg
        assert ffmpeg.ffmpeg_version is None and ffmpeg.ffprobe_version is None
        assert ffmpeg.encoders == frozenset() and ffmpeg.filters == frozenset()
        assert "ffmpeg" in caplog.text.lower()


class TestOllama:
    def test_not_contacted_without_ai(self, tmp_path):
        env = make_env(tmp_path / "sys", ollama=ollama_with_model())
        profile = profile_for(tmp_path, env, no_ai=True)
        assert profile.ollama is None
        assert env.http_calls == []

    def test_unreachable_server(self, tmp_path):
        profile = profile_for(tmp_path, make_env(tmp_path / "sys", ollama=None), no_ai=False)
        assert profile.ollama.reachable is False
        assert profile.ollama.model_present is False
        assert profile.ollama.placement == "unknown"

    def test_model_present_and_not_loaded(self, tmp_path):
        env = make_env(tmp_path / "sys", ollama=ollama_with_model())
        ollama = profile_for(tmp_path, env, no_ai=False).ollama
        assert ollama.reachable and ollama.model_present and not ollama.model_loaded
        assert ollama.model == MODEL
        assert ollama.model_size_bytes == MODEL_SIZE
        assert ollama.placement == "unknown"

    def test_model_missing(self, tmp_path):
        env = make_env(tmp_path / "sys", ollama=ollama_with_model(name="llava:7b"))
        ollama = profile_for(tmp_path, env, no_ai=False).ollama
        assert ollama.reachable is True
        assert ollama.model_present is False
        assert ollama.models == ("llava:7b",)

    def test_name_without_tag_matches_latest(self, tmp_path):
        env = make_env(tmp_path / "sys", ollama=ollama_with_model(name="llava:latest"))
        ollama = profile_for(tmp_path, env, no_ai=False, model="llava").ollama
        assert ollama.model_present is True

    @pytest.mark.parametrize(
        "loaded, placement",
        [
            ({"size_vram": MODEL_SIZE}, "gpu"),
            ({"size_vram": 0}, "cpu"),
            ({"size_vram": MODEL_SIZE // 2}, "mixed"),
        ],
    )
    def test_where_the_loaded_model_runs(self, tmp_path, loaded, placement):
        env = make_env(tmp_path / "sys", ollama=ollama_with_model(loaded=loaded))
        ollama = profile_for(tmp_path, env, no_ai=False).ollama
        assert ollama.model_loaded is True
        assert ollama.placement == placement

    def test_another_loaded_model_does_not_count(self, tmp_path):
        ollama_state = ollama_with_model()
        ollama_state["ps"] = [{"name": "llava:7b", "size": 5, "size_vram": 5}]
        ollama = profile_for(tmp_path, make_env(tmp_path / "sys", ollama=ollama_state), no_ai=False).ollama
        assert ollama.model_loaded is False and ollama.placement == "unknown"

    def test_unloaded_model_memory_is_taken_from_the_host_ram(self, tmp_path):
        env = make_env(tmp_path / "sys", mem_available_bytes=10 * GIB, ollama=ollama_with_model())
        profile = profile_for(tmp_path, env, no_ai=False)
        assert profile.model_reserved_bytes == MODEL_SIZE
        assert profile.usable_ram_bytes == 10 * GIB - MODEL_SIZE

    def test_loaded_model_memory_is_not_subtracted_again(self, tmp_path):
        env = make_env(
            tmp_path / "sys", mem_available_bytes=10 * GIB, ollama=ollama_with_model(loaded={"size_vram": 0})
        )
        profile = profile_for(tmp_path, env, no_ai=False)
        assert profile.model_reserved_bytes == 0
        assert profile.usable_ram_bytes == 10 * GIB

    def test_model_memory_does_not_weigh_on_the_app_cgroup_limit(self, tmp_path):
        env = make_env(tmp_path / "sys", mem_available_bytes=64 * GIB, ollama=ollama_with_model())
        write_cgroup_v2(env, memory_max=2 * GIB, memory_current=0)
        assert profile_for(tmp_path, env, no_ai=False).usable_ram_bytes == 2 * GIB

    def test_reservation_cannot_make_the_ram_negative(self, tmp_path):
        env = make_env(tmp_path / "sys", mem_available_bytes=GIB, ollama=ollama_with_model())
        assert profile_for(tmp_path, env, no_ai=False).usable_ram_bytes == 0


class TestDerivedParameters:
    @pytest.mark.parametrize("cores", [1, 2, 4, 8, 16, 64])
    @pytest.mark.parametrize("ram_gib", [0.5, 1, 2, 8, 64])
    def test_jobs_times_threads_never_exceeds_the_cores(self, tmp_path, cores, ram_gib):
        env = make_env(tmp_path / "sys", cores=cores, mem_available_bytes=int(ram_gib * GIB))
        profile = profile_for(tmp_path, env)
        assert profile.jobs >= 1 and profile.threads_per_job >= 1
        assert profile.jobs * profile.threads_per_job <= cores

    def test_small_machine(self, tmp_path):
        profile = profile_for(tmp_path, make_env(tmp_path / "sys", cores=2, mem_available_bytes=2 * GIB))
        assert profile.jobs <= 2
        assert profile.block_size < profile_for(tmp_path, make_env(tmp_path / "big")).block_size

    def test_large_machine_uses_more_parallelism(self, tmp_path):
        small = profile_for(tmp_path, make_env(tmp_path / "s", cores=2, mem_available_bytes=2 * GIB))
        large = profile_for(tmp_path, make_env(tmp_path / "l", cores=32, mem_available_bytes=64 * GIB))
        assert large.jobs > small.jobs
        assert large.jobs <= 32
        assert large.cache_memory_mb > small.cache_memory_mb

    def test_little_ram_limits_the_jobs_even_with_many_cores(self, tmp_path):
        profile = profile_for(tmp_path, make_env(tmp_path / "sys", cores=32, mem_available_bytes=2 * GIB))
        assert profile.jobs < 32

    def test_very_little_ram_still_gives_a_usable_profile(self, tmp_path):
        profile = profile_for(tmp_path, make_env(tmp_path / "sys", cores=1, mem_available_bytes=64 * 1024**2))
        assert (profile.jobs, profile.threads_per_job) == (1, 1)
        assert profile.block_size >= 1 and profile.cache_memory_mb >= 1

    def test_overrides_replace_the_derived_values_and_are_recorded(self, tmp_path):
        profile = profile_for(
            tmp_path, make_env(tmp_path / "sys", cores=8), jobs="2", threads_per_job=3, block_size=7, cache_memory_mb=99
        )
        assert (profile.jobs, profile.threads_per_job, profile.block_size, profile.cache_memory_mb) == (2, 3, 7, 99)
        assert profile.overrides == {"jobs": 2, "threads_per_job": 3, "block_size": 7, "cache_memory_mb": 99}

    def test_override_of_jobs_alone_adapts_the_threads(self, tmp_path):
        profile = profile_for(tmp_path, make_env(tmp_path / "sys", cores=8), jobs="4")
        assert (profile.jobs, profile.threads_per_job) == (4, 2)

    def test_oversubscription_is_honored_with_a_warning(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            profile = profile_for(tmp_path, make_env(tmp_path / "sys", cores=2), jobs="6")
        assert profile.jobs == 6 and profile.threads_per_job == 1
        assert "cores" in caplog.text

    def test_hwaccel_is_recorded_as_requested(self, tmp_path):
        assert profile_for(tmp_path).hwaccel == "none"
        assert profile_for(tmp_path, hwaccel="auto").hwaccel == "auto"


class TestRecordAndReport:
    def test_profile_is_written_atomically_as_json(self, tmp_path):
        env = make_env(tmp_path / "sys", ollama=ollama_with_model(loaded={"size_vram": 0}))
        profile = profile_for(tmp_path, env, no_ai=False)
        target = tmp_path / "runtime_profile.json"
        write_runtime_profile(profile, target)
        data = json.loads(target.read_text())
        assert data["cpu"]["usable_cores"] == 8
        assert data["memory"]["usable_bytes"] == profile.usable_ram_bytes
        assert data["disk"]["workdir_free_bytes"] == 500 * GIB
        assert data["ffmpeg"]["ffmpeg_version"] == "5.1.6-0+deb12u1"
        assert data["ollama"]["placement"] == "cpu"
        assert data["derived"]["jobs"] == profile.jobs
        assert data["derived"]["hwaccel"] == "none"
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []

    def test_profile_without_ai_has_no_ollama_section(self, tmp_path):
        target = tmp_path / "runtime_profile.json"
        write_runtime_profile(profile_for(tmp_path), target)
        assert json.loads(target.read_text())["ollama"] is None

    def test_report_lines_show_the_main_values(self, tmp_path):
        env = make_env(tmp_path / "sys", cores=4, mem_available_bytes=8 * GIB)
        text = "\n".join(format_profile(profile_for(tmp_path, env)))
        assert "4 core" in text
        assert "8.0 GiB" in text
        assert "jobs" in text


def test_changing_the_profile_does_not_invalidate_the_cache(tmp_path):
    media = MediaFile(Path("/media/a.jpg"), Path("a.jpg"), "photo", 10, 1_000)
    small = profile_for(tmp_path, make_env(tmp_path / "small", cores=1, mem_available_bytes=GIB), assume_tz="Europe/Rome")
    large = profile_for(tmp_path, make_env(tmp_path / "large", cores=32), assume_tz="Europe/Rome", jobs="8")
    assert (small.jobs, small.block_size) != (large.jobs, large.block_size)
    with CacheManager(tmp_path / "work") as cache:
        cache.put_metadata(media, metadata_params("Europe/Rome"), {"width": 1})
        assert cache.get_metadata(media, metadata_params("Europe/Rome")) == {"width": 1}


def test_detection_env_defaults_point_to_the_real_system():
    env = DetectionEnv()
    assert env.meminfo_path == Path("/proc/meminfo")
    assert env.cgroup_root == Path("/sys/fs/cgroup")
