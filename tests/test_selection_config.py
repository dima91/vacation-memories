import pytest

from vacation_memories.config import ConfigError, resolve_settings
from vacation_memories.selection_config import SelectionConfig


def settings_from(tmp_path, text):
    config = tmp_path / "config.toml"
    config.write_text(text)
    return resolve_settings({"config": config}, {})


def test_the_defaults_are_the_initial_values_of_the_specification():
    config = SelectionConfig()
    assert (config.narrative_weight, config.technical_weight) == (0.65, 0.35)
    shares = {name: (s.share, s.min, s.max) for name, s in config.sections.items()}
    assert shares == {
        "opening": (0.10, 0.05, 0.15), "beginning": (0.20, 0.15, 0.25), "development": (0.45, 0.35, 0.55),
        "finale": (0.20, 0.15, 0.25), "closing": (0.05, 0.03, 0.08),
    }
    assert config.max_same_moment_run == 3 and config.transition_time_share == 0.10


def test_the_selection_table_of_the_configuration_file_changes_weights_shares_and_durations(tmp_path):
    settings = settings_from(tmp_path, """
[selection]
technical_threshold = 0.3
narrative_weight = 0.7
technical_weight = 0.3
max_same_moment_run = 2

[selection.sections.opening]
share = 0.08
[selection.sections.development]
share = 0.47

[selection.moment_shares]
detail = 0.2
""")
    selection = settings.selection
    assert selection.technical_threshold == 0.3 and selection.narrative_weight == 0.7
    assert selection.max_same_moment_run == 2
    assert selection.sections["opening"].share == 0.08 and selection.sections["opening"].max == 0.15
    assert selection.moment_shares["detail"] == 0.2 and selection.moment_shares["scenery"] == 0.25


def test_without_the_table_the_defaults_apply(tmp_path):
    assert settings_from(tmp_path, "nice = 1\n").selection == SelectionConfig()


@pytest.mark.parametrize(
    "text",
    [
        "[selection]\nunknown_weight = 1\n",
        "[selection]\nnarrative_weight = 0.9\n",  # no longer adds up to 1 with the technical weight
        "[selection]\ntechnical_threshold = 1.5\n",
        "[selection]\ntechnical_threshold = 'high'\n",
        "[selection]\nmax_same_moment_run = 2.5\n",
        "[selection]\nphoto_min_seconds = 5.0\n",  # above photo_max_seconds
        "[selection.sections.opening]\nshare = 0.3\n",  # shares no longer add up to 1
        "[selection.sections.prologue]\nshare = 0.1\n",
        "[selection.sections.opening]\nweight = 0.1\n",
        "[selection.moment_shares]\nportrait = 0.1\n",
        "selection = 3\n",
    ],
)
def test_invalid_values_are_configuration_errors(tmp_path, text):
    with pytest.raises(ConfigError):
        settings_from(tmp_path, text)
