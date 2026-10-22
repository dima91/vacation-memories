import pytest

from vacation_memories.vision_schema import (
    CATEGORIES,
    MAX_DESCRIPTION_CHARS,
    MAX_DESCRIPTION_WORDS,
    MOMENT_TYPES,
    PROMPT,
    RESPONSE_SCHEMA,
    VisionResponseError,
    VisionResult,
    prompt_fingerprint,
)

VALID = {
    "description": "Two friends laughing on a sunny beach",
    "category": "sea",
    "moment_type": "social",
    "narrative_value": 4,
    "people": True,
}


def test_a_valid_response_keeps_every_field():
    result = VisionResult.from_response(VALID)
    assert result == VisionResult("Two friends laughing on a sunny beach", "sea", "social", 4, True)


def test_a_moment_type_outside_the_enum_becomes_null_and_the_rest_is_kept():
    result = VisionResult.from_response({**VALID, "moment_type": "party"})
    assert result.moment_type is None
    assert (result.category, result.narrative_value, result.people) == ("sea", 4, True)


@pytest.mark.parametrize("value", [0, 6, -1, 3.5, "4", True, None, [4]])
def test_a_narrative_value_out_of_range_or_of_the_wrong_type_becomes_null(value):
    result = VisionResult.from_response({**VALID, "narrative_value": value})
    assert result.narrative_value is None
    assert result.moment_type == "social"


@pytest.mark.parametrize("value", [1, 5])
def test_the_narrative_value_limits_are_accepted(value):
    assert VisionResult.from_response({**VALID, "narrative_value": value}).narrative_value == value


@pytest.mark.parametrize("field, value", [
    ("category", "beach"),
    ("category", 3),
    ("people", "yes"),
    ("people", 1),
    ("description", ""),
    ("description", "   "),
    ("description", 12),
    ("description", " ".join(["word"] * 16)),
])
def test_an_invalid_field_becomes_null_without_discarding_the_others(field, value):
    result = VisionResult.from_response({**VALID, field: value})
    assert getattr(result, field) is None
    assert sum(value is None for value in result.to_dict().values()) == 1


def test_a_description_of_exactly_fifteen_words_is_accepted():
    text = " ".join(["word"] * 15)
    assert VisionResult.from_response({**VALID, "description": text}).description == text


def test_missing_fields_become_null():
    result = VisionResult.from_response({"category": "city"})
    assert result == VisionResult(None, "city", None, None, None)


def test_extra_fields_are_ignored():
    assert "mood" not in VisionResult.from_response({**VALID, "mood": "happy"}).to_dict()


@pytest.mark.parametrize("raw", [None, "text", 3, [VALID]])
def test_a_response_that_is_not_an_object_is_an_error(raw):
    with pytest.raises(VisionResponseError):
        VisionResult.from_response(raw)


def test_the_dictionary_form_round_trips():
    result = VisionResult.from_response(VALID)
    assert VisionResult.from_response(result.to_dict()) == result


def test_the_schema_requires_the_five_fields_and_uses_the_same_enums_as_the_validator():
    properties = RESPONSE_SCHEMA["properties"]
    assert set(RESPONSE_SCHEMA["required"]) == {"description", "category", "moment_type", "narrative_value", "people"}
    assert set(properties) == set(RESPONSE_SCHEMA["required"])
    assert properties["category"]["enum"] == list(CATEGORIES)
    assert properties["moment_type"]["enum"] == list(MOMENT_TYPES)
    assert properties["narrative_value"]["type"] == "integer"
    assert (properties["narrative_value"]["minimum"], properties["narrative_value"]["maximum"]) == (1, 5)
    assert properties["people"]["type"] == "boolean"


def test_the_description_has_a_length_limit_so_the_model_cannot_ramble_until_the_answer_is_cut_off():
    description = RESPONSE_SCHEMA["properties"]["description"]
    assert description["maxLength"] == MAX_DESCRIPTION_CHARS
    assert MAX_DESCRIPTION_CHARS >= MAX_DESCRIPTION_WORDS * 8  # room for fifteen ordinary words


def test_the_specified_enums():
    assert CATEGORIES == ("nature", "sea", "city", "monument", "food", "animals", "indoor", "other")
    assert MOMENT_TYPES == ("scenery", "social", "action", "detail", "transition", "other")


class TestPrompt:
    @pytest.mark.parametrize("moment_type", [m for m in MOMENT_TYPES if m != "other"])
    def test_every_moment_type_is_defined(self, moment_type):
        assert f"`{moment_type}`" in PROMPT or f"{moment_type}:" in PROMPT

    def test_it_tells_the_model_to_leave_technical_quality_out(self):
        text = PROMPT.lower()
        assert "do not judge" in text
        for word in ("sharpness", "exposure", "composition"):
            assert word in text

    def test_it_forbids_guessing_identities_emotions_and_places(self):
        text = PROMPT.lower()
        assert "identit" in text and "emotion" in text and "place" in text

    def test_the_fingerprint_changes_with_the_prompt_and_the_schema(self):
        assert prompt_fingerprint() == prompt_fingerprint()
        assert prompt_fingerprint(prompt=PROMPT + " ") != prompt_fingerprint()
        assert prompt_fingerprint(schema={**RESPONSE_SCHEMA, "title": "x"}) != prompt_fingerprint()
