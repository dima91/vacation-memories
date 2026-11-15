"""Projects of the editor: folders, ids, settings and the state of a project. Synthetic media only."""

import json
import os

import pytest
from editor_fakes import editor_settings, make_media

from vacation_memories.editor.errors import ApiError
from vacation_memories.editor.projects import Projects, project_id


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    return make_media(tmp_path_factory.mktemp("editor-media"))


@pytest.fixture
def projects(media, tmp_path):
    music = tmp_path / "music"
    (music / "album").mkdir(parents=True)
    (music / "album" / "song.mp3").write_bytes(b"x")
    return Projects(editor_settings(media, tmp_path / "work", music))


def test_the_id_comes_from_the_folder_name_and_its_relative_path():
    first, second = project_id("a/beach"), project_id("b/beach")
    assert first.startswith("beach-") and second.startswith("beach-")
    assert first != second
    assert project_id("a/beach") == first
    assert project_id(".").startswith("media-")


def test_folders_are_listed_with_the_media_of_their_subfolders(projects):
    folders = {folder["path"]: folder for folder in projects.folders()}
    assert folders["trip"]["photos"] == 10 and folders["trip"]["videos"] == 1
    assert folders["other"]["photos"] == 2 and folders["other"]["videos"] == 0
    assert "empty" not in folders
    days = [path for path in folders if path.startswith("trip/")]
    assert len(days) == 2
    assert sum(folders[path]["photos"] for path in days) == 10
    assert all(folder["state"] == "not_analyzed" and not folder["project"] for folder in folders.values())


def test_the_root_of_the_media_is_a_folder_too(projects):
    assert projects.folders()[0]["path"] == "."


def test_opening_a_folder_creates_its_project_once(projects):
    first = projects.open("trip")
    again = projects.open("trip")
    assert first.id == again.id
    assert first.workdir.parent.name == "projects"
    stored = json.loads((first.workdir / "project.json").read_text())
    assert stored["input"] == "trip"
    assert {folder["path"]: folder for folder in projects.folders()}["trip"]["project"] is True


@pytest.mark.parametrize("folder", ["../outside", "/etc", "trip/../../x", "nowhere", "trip/day\x00"])
def test_folders_outside_the_media_root_or_missing_are_refused(projects, folder):
    with pytest.raises(ApiError) as error:
        projects.open(folder)
    assert error.value.status in (400, 404)


def test_a_symlink_out_of_the_media_root_is_refused(projects, media, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = media / "link-out"
    link.symlink_to(outside)
    try:
        with pytest.raises(ApiError) as error:
            projects.open("link-out")
        assert error.value.code == "invalid_folder"
    finally:
        link.unlink()


def test_an_unknown_or_malformed_project_id_is_not_found(projects):
    for identifier in ("nope-12345678", "../x", "a/b", ""):
        with pytest.raises(ApiError) as error:
            projects.get(identifier)
        assert error.value.status == 404


class TestSettings:
    def test_a_new_project_has_the_defaults(self, projects):
        settings = projects.open("trip").settings()
        assert settings["duration"] == "5:00" and settings["structure"] == "narrative"
        assert settings["aspect"] == "16:9" and settings["resolution"] == "1080p"
        assert settings["ai"] is None and settings["music"] == []

    def test_they_are_saved_and_say_what_they_require(self, projects):
        project = projects.open("trip")
        result = project.save_settings({"duration": "2:00", "resolution": "720p", "ai_time_budget": 120})
        assert result["requires"] == ["analysis", "plan", "render"]
        saved = project.settings()
        assert saved["duration"] == "2:00" and saved["resolution"] == "720p" and saved["ai_time_budget"] == 120
        assert saved["structure"] == "narrative"  # what was not sent stays

    def test_music_alone_requires_only_the_music_block(self, projects):
        project = projects.open("trip")
        assert project.save_settings({"music": ["album/song.mp3"]})["requires"] == ["music"]

    def test_the_automatic_preview_is_on_by_default_and_requires_nothing(self, projects):
        project = projects.open("trip")
        assert project.settings()["auto_preview"] is True
        saved = project.save_settings({"auto_preview": False})
        assert saved["requires"] == [] and project.settings()["auto_preview"] is False

    def test_the_analysis_options_require_the_analysis(self, projects):
        project = projects.open("trip")
        assert project.save_settings({"ai": True})["requires"] == ["analysis"]

    @pytest.mark.parametrize(("settings", "field"), [
        ({"duration": "soon"}, "duration"),
        ({"duration": "5"}, "duration"),  # below the 15 s minimum
        ({"structure": "random"}, "structure"),
        ({"aspect": "7:5"}, "aspect"),
        ({"resolution": "huge"}, "resolution"),
        ({"ai_frames": 10, "ai_time_budget": 60}, None),
        ({"nice": 99}, "nice"),
        ({"colour": "red"}, "colour"),
        ({"ai": "yes"}, "ai"),
        ({"auto_preview": "yes"}, "auto_preview"),
        ({"music": ["../secret.mp3"]}, "music"),
        ({"music": ["album/missing.mp3"]}, "music"),
        ({"music": "album/song.mp3"}, "music"),
    ])
    def test_invalid_values_name_the_field(self, projects, settings, field):
        project = projects.open("trip")
        before = project.settings()
        with pytest.raises(ApiError) as error:
            project.save_settings(settings)
        assert error.value.status == 400 and error.value.code == "invalid_settings"
        assert error.value.field == field
        assert project.settings() == before

    def test_the_message_is_the_one_of_the_cli(self, projects):
        with pytest.raises(ApiError) as error:
            projects.open("trip").save_settings({"duration": "soon"})
        assert "duration" in error.value.message

    def test_they_are_written_atomically_and_privately(self, projects):
        project = projects.open("trip")
        project.save_settings({"duration": "1:00"})
        assert [path.name for path in project.workdir.glob(".project.json*")] == []
        assert os.stat(project.workdir / "project.json").st_mode & 0o077 == 0
