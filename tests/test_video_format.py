import pytest

from vacation_memories.config import ConfigError, resolve_settings
from vacation_memories.video_format import (
    FormatError,
    frame_size,
    parse_aspect,
    parse_duration,
    parse_resolution,
    resize_keeping_short_side,
)


class TestDuration:
    @pytest.mark.parametrize(
        "text, seconds",
        [
            ("300", 300), ("15", 15), ("90.5", 90.5), ("90s", 90), ("5m", 300), ("1m30s", 90), ("1m", 60),
            ("05:00", 300), ("0:15", 15), ("90:00", 5400), ("1:05:00", 3900), ("0:00:20", 20), (" 2M ", 120),
        ],
    )
    def test_accepted_formats(self, text, seconds):
        assert parse_duration(text) == seconds

    def test_a_number_from_the_configuration_file_is_seconds(self):
        assert parse_duration(120) == 120.0
        assert parse_duration(45.5) == 45.5

    @pytest.mark.parametrize("text", ["", "abc", "5 min", "1h", "1h30m", "-30", "1:2:3:4", "5:7", "1m5", "m30s", "30ss"])
    def test_other_formats_are_errors_with_examples(self, text):
        with pytest.raises(FormatError, match="300, 90s, 5m, 1m30s, 05:00, 1:05:00"):
            parse_duration(text)

    @pytest.mark.parametrize("text", ["1m60s", "5:60", "1:60:00", "1:00:75"])
    def test_minutes_and_seconds_must_be_below_60(self, text):
        with pytest.raises(FormatError, match="below 60"):
            parse_duration(text)

    @pytest.mark.parametrize("value", ["14", "0", "0:14", 10, True, None, float("inf"), float("nan")])
    def test_below_the_minimum_or_not_a_duration(self, value):
        with pytest.raises(FormatError):
            parse_duration(value)

    def test_the_minimum_is_15_seconds(self):
        with pytest.raises(FormatError, match="at least 15"):
            parse_duration("14s")

    def test_long_durations_are_accepted(self):
        assert parse_duration("1:30:00") == 5400  # only a warning, given by the pipeline


class TestResolutionAndAspect:
    @pytest.mark.parametrize(
        "resolution, aspect, size",
        [
            ("1080p", "16:9", (1920, 1080)),
            ("1080p", "9:16", (1080, 1920)),
            ("1080p", "1:1", (1080, 1080)),
            ("1080p", "4:3", (1440, 1080)),
            ("4k", "16:9", (3840, 2160)),
            ("2160p", "16:9", (3840, 2160)),
            ("4K", "1:1", (2160, 2160)),
            ("720p", "16:9", (1280, 720)),
            ("720p", "9:16", (720, 1280)),
            ("480p", "16:9", (854, 480)),  # 853.3 rounded to an even value
            ("480p", "4:3", (640, 480)),
            ("1440p", "16:9", (2560, 1440)),
            ("1440p", "4:3", (1920, 1440)),
        ],
    )
    def test_a_preset_is_the_short_side(self, resolution, aspect, size):
        assert (frame_size(resolution, aspect).width, frame_size(resolution, aspect).height) == size

    def test_defaults_are_1080p_16_9(self):
        size = frame_size("1080p", None)
        assert (size.width, size.height) == (1920, 1080)

    def test_explicit_dimensions_keep_their_ratio(self):
        size = frame_size("1080x1350", None)
        assert (size.width, size.height) == (1080, 1350)

    @pytest.mark.parametrize("text, size", [("1921x1081", (1920, 1080)), ("1279x723", (1280, 724)), ("643x481", (644, 480))])
    def test_explicit_dimensions_are_rounded_to_even_values(self, text, size):
        result = frame_size(text, None)
        assert (result.width, result.height) == size
        assert result.width % 2 == 0 and result.height % 2 == 0

    @pytest.mark.parametrize("text, aspect", [("1920x1080", "16:9"), ("854x480", "16:9"), ("1080x1920", "9:16"),
                                              ("1440x1080", "4:3"), ("720x720", "1:1")])
    def test_explicit_dimensions_with_a_consistent_aspect(self, text, aspect):
        frame_size(text, aspect)

    @pytest.mark.parametrize("text, aspect", [("1920x1080", "9:16"), ("1920x1080", "4:3"), ("1080x1080", "16:9")])
    def test_explicit_dimensions_with_an_inconsistent_aspect_are_an_error(self, text, aspect):
        with pytest.raises(FormatError, match="does not have the aspect"):
            frame_size(text, aspect)

    @pytest.mark.parametrize("text", ["318x320", "320x200", "4000x2200", "5000x2160", "100x100"])
    def test_limits_of_the_explicit_dimensions(self, text):
        with pytest.raises(FormatError):
            frame_size(text, None)

    @pytest.mark.parametrize("text", ["320x320", "568x320", "2160x2160", "3840x2160"])
    def test_sizes_at_the_limits_are_accepted(self, text):
        frame_size(text, None)

    @pytest.mark.parametrize("text", ["1080", "1080i", "8k", "360p", "1920*1080", "1920x", "x1080", "full hd", ""])
    def test_invalid_resolutions_list_the_accepted_formats(self, text):
        with pytest.raises(FormatError, match="480p, 720p, 1080p, 1440p, 4k"):
            parse_resolution(text)

    @pytest.mark.parametrize("text", ["16/9", "3:4", "21:9", "", "16:10"])
    def test_invalid_aspects_list_the_accepted_values(self, text):
        with pytest.raises(FormatError, match="16:9, 9:16, 1:1, 4:3"):
            parse_aspect(text)

    def test_a_new_aspect_keeps_the_short_side(self):
        size = resize_keeping_short_side(1920, 1080, "9:16")
        assert (size.width, size.height) == (1080, 1920)


class TestThroughTheConfiguration:
    def test_values_are_parsed_and_the_combination_checked(self):
        settings = resolve_settings({"duration": "1m30s", "resolution": "720p", "aspect": "9:16"}, {})
        assert settings.duration == 90
        assert settings.output_size == (720, 1280)

    def test_defaults(self):
        settings = resolve_settings({}, {})
        assert settings.duration == 300 and settings.output_size == (1920, 1080)
        assert settings.structure == "narrative"

    @pytest.mark.parametrize(
        "options",
        [{"duration": "10"}, {"duration": "five minutes"}, {"resolution": "1920x1080", "aspect": "1:1"},
         {"resolution": "8k"}, {"aspect": "2:1"}, {"structure": "random"}],
    )
    def test_invalid_values_are_configuration_errors(self, options):
        with pytest.raises(ConfigError):
            resolve_settings(options, {})

    def test_duration_from_the_environment_and_the_file(self, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text('duration = "2m"\nresolution = "720p"\n')
        settings = resolve_settings({}, {"VACATION_MEMORIES_CONFIG": str(config)})
        assert settings.duration == 120 and settings.output_size == (1280, 720)
        settings = resolve_settings({}, {"VACATION_MEMORIES_CONFIG": str(config), "VACATION_MEMORIES_DURATION": "45"})
        assert settings.duration == 45
