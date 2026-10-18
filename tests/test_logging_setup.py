import io
import json
import logging

import pytest

from vacation_memories.logging_setup import setup_logging


@pytest.fixture(autouse=True)
def clean_root_logger():
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        if handler not in saved_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(saved_level)


def test_text_lines_go_to_the_file_and_the_console_without_ansi_or_carriage_returns(tmp_path):
    console = io.StringIO()
    log_file = tmp_path / "run.log"
    setup_logging(log_file, "text", quiet=False, stream=console)
    logging.getLogger("vacation_memories.test").info("phase %s started", "scan")
    for text in (log_file.read_text(encoding="utf-8"), console.getvalue()):
        assert text.endswith("\n")
        assert "INFO" in text and "vacation_memories.test" in text and "phase scan started" in text
        assert "\r" not in text and "\x1b" not in text
        assert text[:4].isdigit()  # starts with a timestamp


def test_json_format_writes_one_object_per_line(tmp_path):
    console = io.StringIO()
    log_file = tmp_path / "run.log"
    setup_logging(log_file, "json", quiet=False, stream=console)
    logger = logging.getLogger("vacation_memories.test")
    logger.warning("slow disk: %d MB/s", 12)
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed")
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["level"] == "WARNING"
    assert lines[0]["message"] == "slow disk: 12 MB/s"
    assert lines[0]["logger"] == "vacation_memories.test"
    assert "ts" in lines[0]
    assert lines[1]["level"] == "ERROR"
    assert "ValueError: boom" in lines[1]["exception"]
    assert [json.loads(line)["message"] for line in console.getvalue().splitlines()] == ["slow disk: 12 MB/s", "failed"]


def test_quiet_keeps_warnings_on_the_console_but_everything_in_the_file(tmp_path):
    console = io.StringIO()
    log_file = tmp_path / "run.log"
    setup_logging(log_file, "text", quiet=True, stream=console)
    logger = logging.getLogger("vacation_memories.test")
    logger.info("routine message")
    logger.warning("something odd")
    assert "routine message" not in console.getvalue()
    assert "something odd" in console.getvalue()
    assert "routine message" in log_file.read_text(encoding="utf-8")


def test_undecodable_file_names_are_logged_without_crashing(tmp_path):
    console = io.StringIO()
    log_file = tmp_path / "run.log"
    setup_logging(log_file, "text", quiet=False, stream=console)
    name = b"caf\xe9.jpg".decode("utf-8", errors="surrogateescape")
    logging.getLogger("vacation_memories.test").warning("skipping %s", name)
    assert "skipping caf" in log_file.read_text(encoding="utf-8")
    assert "skipping caf" in console.getvalue()


def test_without_a_log_file_only_the_console_is_used(tmp_path):
    console = io.StringIO()
    setup_logging(None, "text", quiet=False, stream=console)
    logging.getLogger("vacation_memories.test").info("console only")
    assert "console only" in console.getvalue()
    assert list(tmp_path.iterdir()) == []


def test_calling_setup_twice_does_not_duplicate_lines(tmp_path):
    console = io.StringIO()
    log_file = tmp_path / "run.log"
    setup_logging(log_file, "text", quiet=False, stream=console)
    setup_logging(log_file, "text", quiet=False, stream=console)
    logging.getLogger("vacation_memories.test").info("once")
    assert console.getvalue().count("once") == 1
    assert log_file.read_text(encoding="utf-8").count("once") == 1
