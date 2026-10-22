from pathlib import Path

import pytest

from vacation_memories.config import ConfigError, resolve_settings


def resolve(cli=None, env=None):
    return resolve_settings(cli or {}, env or {})


def write_config(tmp_path, text, name="config.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults():
    settings = resolve()
    assert settings.workdir == Path("work")
    assert settings.log_format == "text"
    assert settings.quiet is False
    assert settings.nice == 0
    assert settings.assume_tz is None
    assert settings.jobs == "auto"
    assert settings.hwaccel == "none"
    assert settings.ollama_url == "http://localhost:11434"
    assert settings.require_ai is False
    assert settings.no_ai is False


def test_precedence_is_cli_then_env_then_file_then_default(tmp_path):
    config = write_config(tmp_path, 'nice = 3\nlog_format = "json"\nassume_tz = "Europe/Rome"\n')
    env = {"VACATION_MEMORIES_CONFIG": str(config), "VACATION_MEMORIES_NICE": "7", "VACATION_MEMORIES_LOG_FORMAT": "text"}
    settings = resolve(cli={"nice": 11}, env=env)
    assert settings.nice == 11  # CLI beats env and file
    assert settings.log_format == "text"  # env beats file
    assert settings.assume_tz == "Europe/Rome"  # file beats default
    assert settings.sources["nice"] == "cli"
    assert settings.sources["log_format"] == "env"
    assert settings.sources["assume_tz"] == "config"
    assert settings.sources["quiet"] == "default"


def test_cli_config_option_beats_the_environment_variable(tmp_path):
    from_cli = write_config(tmp_path, "nice = 1\n", "cli.toml")
    from_env = write_config(tmp_path, "nice = 2\n", "env.toml")
    settings = resolve(cli={"config": from_cli}, env={"VACATION_MEMORIES_CONFIG": str(from_env)})
    assert settings.nice == 1


def test_empty_environment_variable_counts_as_unset(tmp_path):
    config = write_config(tmp_path, 'assume_tz = "Europe/Rome"\n')
    env = {"VACATION_MEMORIES_CONFIG": str(config), "VACATION_MEMORIES_ASSUME_TZ": ""}
    assert resolve(env=env).assume_tz == "Europe/Rome"
    assert resolve(env={"VACATION_MEMORIES_CONFIG": ""}).assume_tz is None


def test_env_booleans(tmp_path):
    assert resolve(env={"VACATION_MEMORIES_QUIET": "true"}).quiet is True
    assert resolve(env={"VACATION_MEMORIES_QUIET": "0"}).quiet is False
    with pytest.raises(ConfigError, match="quiet"):
        resolve(env={"VACATION_MEMORIES_QUIET": "maybe"})


@pytest.mark.parametrize(
    "text",
    [
        "nice = ",  # invalid TOML
        "unknown_option = 1\n",
        'nice = "high"\n',
        "nice = 20\n",
        "nice = -1\n",
        'log_format = "xml"\n',
        'assume_tz = "Mars/Olympus"\n',
        "jobs = 0\n",
        'hwaccel = "Not A Name"\n',
        "quiet = 1\n",
    ],
)
def test_invalid_config_file_is_a_config_error(tmp_path, text):
    config = write_config(tmp_path, text)
    with pytest.raises(ConfigError):
        resolve(cli={"config": config})


def test_missing_config_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        resolve(cli={"config": tmp_path / "nope.toml"})


def test_jobs_accepts_auto_or_a_positive_integer():
    assert resolve(cli={"jobs": "auto"}).jobs == "auto"
    assert resolve(cli={"jobs": "3"}).jobs == 3
    assert resolve(env={"VACATION_MEMORIES_JOBS": "2"}).jobs == 2


def test_compose_configuration_file_is_accepted():
    compose = Path(__file__).resolve().parent.parent / "docker" / "compose.toml"
    settings = resolve(cli={"config": compose})
    assert settings.input == Path("/media")
    assert settings.workdir == Path("/work")
    assert settings.ollama_url == "http://ollama:11434"
    assert settings.allow_remote_ollama is True
    assert settings.require_ai is True


def test_log_file_defaults_to_run_log_in_the_workdir_and_relative_paths_stay_inside_it(tmp_path):
    assert resolve(cli={"workdir": tmp_path}).log_file == tmp_path / "run.log"
    assert resolve(cli={"workdir": tmp_path, "log_file": "logs/a.log"}).log_file == tmp_path / "logs" / "a.log"
    absolute = tmp_path / "elsewhere" / "a.log"
    assert resolve(cli={"workdir": tmp_path / "w", "log_file": absolute}).log_file == absolute


class TestOllamaUrl:
    def test_precedence_cli_env_file_then_ollama_host_then_default(self, tmp_path):
        config = write_config(tmp_path, 'ollama_url = "http://localhost:2"\n')
        env = {"VACATION_MEMORIES_CONFIG": str(config), "OLLAMA_HOST": "localhost:4"}
        assert resolve(cli={"ollama_url": "http://localhost:1"}, env=env).ollama_url == "http://localhost:1"
        env["VACATION_MEMORIES_OLLAMA_URL"] = "http://localhost:3"
        assert resolve(env=env).ollama_url == "http://localhost:3"
        del env["VACATION_MEMORIES_OLLAMA_URL"]
        assert resolve(env=env).ollama_url == "http://localhost:2"
        del env["VACATION_MEMORIES_CONFIG"]
        assert resolve(env=env).ollama_url == "http://localhost:4"
        assert resolve(env={"OLLAMA_HOST": ""}).ollama_url == "http://localhost:11434"

    @pytest.mark.parametrize(
        "host, expected",
        [
            ("localhost", "http://localhost:11434"),
            ("127.0.0.1:9999", "http://127.0.0.1:9999"),
            ("0.0.0.0", "http://localhost:11434"),
            ("0.0.0.0:5000", "http://localhost:5000"),
            ("http://localhost:7", "http://localhost:7"),
            ("[::1]:8", "http://[::1]:8"),
        ],
    )
    def test_ollama_host_is_normalized_like_ollama_does(self, host, expected):
        assert resolve(env={"OLLAMA_HOST": host}).ollama_url == expected

    def test_trailing_slash_is_removed(self):
        assert resolve(cli={"ollama_url": "http://localhost:1/"}).ollama_url == "http://localhost:1"

    def test_explicit_url_without_scheme_is_rejected(self):
        with pytest.raises(ConfigError, match="ollama"):
            resolve(cli={"ollama_url": "localhost:11434"})

    @pytest.mark.parametrize("url", ["http://ollama:11434", "http://10.0.0.5:11434", "http://example.com"])
    def test_non_local_url_needs_explicit_permission(self, url):
        with pytest.raises(ConfigError, match="allow-remote-ollama"):
            resolve(cli={"ollama_url": url})
        assert resolve(cli={"ollama_url": url, "allow_remote_ollama": True}).ollama_url == url

    @pytest.mark.parametrize("url", ["http://localhost:1", "http://127.0.0.1:1", "http://127.9.9.9:1", "http://[::1]:1"])
    def test_local_urls_need_no_permission(self, url):
        assert resolve(cli={"ollama_url": url}).ollama_url == url

    def test_non_local_url_is_not_checked_without_ai(self):
        settings = resolve(cli={"ollama_url": "http://ollama:11434", "no_ai": True})
        assert settings.ollama_url == "http://ollama:11434"

    def test_non_local_ollama_host_follows_the_same_rule(self):
        with pytest.raises(ConfigError, match="allow-remote-ollama"):
            resolve(env={"OLLAMA_HOST": "gpu-box:11434"})


class TestAiFlags:
    def test_require_ai_and_no_ai_together_on_the_command_line_are_contradictory(self):
        with pytest.raises(ConfigError, match="require-ai"):
            resolve(cli={"require_ai": True, "no_ai": True})

    def test_both_flags_in_the_same_environment_are_contradictory(self):
        env = {"VACATION_MEMORIES_REQUIRE_AI": "1", "VACATION_MEMORIES_NO_AI": "1"}
        with pytest.raises(ConfigError, match="contradictory"):
            resolve(env=env)

    def test_both_flags_in_the_same_configuration_file_are_contradictory(self, tmp_path):
        config = write_config(tmp_path, "require_ai = true\nno_ai = true\n")
        with pytest.raises(ConfigError, match="contradictory"):
            resolve(cli={"config": config})

    def test_malformed_ollama_host_is_ignored_without_ai(self):
        assert resolve(cli={"no_ai": True}, env={"OLLAMA_HOST": "host:abc"}).no_ai is True

    def test_malformed_ollama_host_is_an_error_with_ai(self):
        with pytest.raises(ConfigError, match="OLLAMA_HOST"):
            resolve(env={"OLLAMA_HOST": "host:abc"})

    def test_no_ai_on_the_command_line_beats_require_ai_from_the_configuration(self, tmp_path):
        config = write_config(tmp_path, "require_ai = true\n")
        settings = resolve(cli={"config": config, "no_ai": True})
        assert settings.no_ai is True
        assert settings.require_ai is False

    def test_require_ai_on_the_command_line_beats_no_ai_from_the_configuration(self, tmp_path):
        config = write_config(tmp_path, "no_ai = true\n")
        settings = resolve(cli={"config": config, "require_ai": True})
        assert settings.require_ai is True
        assert settings.no_ai is False

    def test_require_ai_from_the_configuration_is_kept_without_no_ai(self, tmp_path):
        config = write_config(tmp_path, "require_ai = true\n")
        assert resolve(cli={"config": config}).require_ai is True


def test_rendering_defaults_come_from_one_place():
    from vacation_memories.render_config import RenderConfig
    from vacation_memories.transitions import TransitionRules

    settings = resolve()
    assert (settings.transition_duration, settings.transition_max_fraction, settings.transition_min_duration) == (0.8, 0.25, 0.2)
    assert (settings.fade_in_seconds, settings.fade_out_seconds, settings.duration_tolerance) == (1.0, 3.0, 0.2)
    assert RenderConfig.from_settings(settings) == RenderConfig()
    assert RenderConfig().rules == TransitionRules()


def test_rendering_options_follow_the_usual_precedence(tmp_path):
    from vacation_memories.render_config import RenderConfig

    config = write_config(tmp_path, "transition_duration = 1.2\ntransition_min_duration = 0.1\nfade_out_seconds = 5\n")
    env = {"VACATION_MEMORIES_CONFIG": str(config), "VACATION_MEMORIES_FADE_OUT_SECONDS": "2.5"}
    settings = resolve(cli={"duration_tolerance": 0.5}, env=env)
    render = RenderConfig.from_settings(settings)
    assert render.rules.default_seconds == 1.2 and render.rules.min_seconds == 0.1
    assert render.fade_out_seconds == 2.5 and render.duration_tolerance == 0.5
    assert settings.sources["transition_duration"] == "config" and settings.sources["fade_out_seconds"] == "env"


@pytest.mark.parametrize(
    "text",
    ["transition_duration = -1\n", "transition_max_fraction = 0\n", "transition_max_fraction = 0.6\n",
     'transition_min_duration = "short"\n', "fade_in_seconds = -0.5\n", "duration_tolerance = nan\n"],
)
def test_invalid_rendering_options_are_config_errors(tmp_path, text):
    with pytest.raises(ConfigError):
        resolve(cli={"config": write_config(tmp_path, text)})


class TestAiSettings:
    def test_defaults(self):
        settings = resolve()
        assert settings.ai_frames is None
        assert settings.ai_time_budget is None
        assert settings.ai_resolution == 512
        assert settings.ai_max_concurrency == 1
        assert settings.ai_unload_model is True
        assert settings.ai_timeout > 0

    def test_the_default_time_budget_is_defined_in_one_place(self):
        from vacation_memories.config import DEFAULT_AI_TIME_BUDGET_SECONDS

        assert DEFAULT_AI_TIME_BUDGET_SECONDS > 0

    def test_model_follows_cli_then_variable_then_file(self, tmp_path):
        config = write_config(tmp_path, 'model = "from-file"\n')
        env = {"VACATION_MEMORIES_CONFIG": str(config), "VACATION_MEMORIES_MODEL": "from-env"}
        assert resolve(env=env).model == "from-env"
        assert resolve(cli={"model": "from-cli"}, env=env).model == "from-cli"
        assert resolve(env={"VACATION_MEMORIES_CONFIG": str(config)}).model == "from-file"

    def test_values_from_the_environment_and_the_file(self, tmp_path):
        config = write_config(tmp_path, "ai_frames = 40\nai_resolution = 768\nai_max_concurrency = 2\n")
        settings = resolve(env={"VACATION_MEMORIES_CONFIG": str(config), "VACATION_MEMORIES_AI_TIMEOUT": "30"})
        assert (settings.ai_frames, settings.ai_resolution, settings.ai_max_concurrency) == (40, 768, 2)
        assert settings.ai_timeout == 30

    @pytest.mark.parametrize("text", [
        "ai_frames = 0",
        "ai_frames = -3",
        "ai_time_budget = 0",
        "ai_time_budget = -1.5",
        "ai_resolution = 800",
        "ai_resolution = 100",
        "ai_max_concurrency = 0",
        "ai_timeout = 0",
        'ai_unload_model = "maybe"',
    ])
    def test_invalid_values_are_config_errors(self, tmp_path, text):
        config = write_config(tmp_path, text + "\n")
        with pytest.raises(ConfigError):
            resolve(cli={"config": config})

    def test_frames_and_time_budget_together_on_the_command_line_are_contradictory(self):
        with pytest.raises(ConfigError, match="mutually exclusive"):
            resolve(cli={"ai_frames": 10, "ai_time_budget": 60})

    def test_frames_and_time_budget_in_the_same_file_are_contradictory(self, tmp_path):
        config = write_config(tmp_path, "ai_frames = 10\nai_time_budget = 60\n")
        with pytest.raises(ConfigError, match="mutually exclusive"):
            resolve(cli={"config": config})

    def test_the_command_line_beats_the_other_option_from_the_configuration(self, tmp_path):
        config = write_config(tmp_path, "ai_frames = 10\n")
        settings = resolve(cli={"config": config, "ai_time_budget": 60})
        assert (settings.ai_frames, settings.ai_time_budget) == (None, 60)
        config = write_config(tmp_path, "ai_time_budget = 60\n")
        settings = resolve(cli={"config": config, "ai_frames": 10})
        assert (settings.ai_frames, settings.ai_time_budget) == (10, None)

    def test_the_environment_beats_the_configuration(self, tmp_path):
        config = write_config(tmp_path, "ai_frames = 10\n")
        settings = resolve(env={"VACATION_MEMORIES_CONFIG": str(config), "VACATION_MEMORIES_AI_TIME_BUDGET": "90"})
        assert (settings.ai_frames, settings.ai_time_budget) == (None, 90)
