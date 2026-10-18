"""Command line: global options, start-up (private files, workdir, logging) and the subcommands."""

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from vacation_memories import __version__, exit_codes
from vacation_memories.config import ConfigError, Settings, ollama_url_is_local, resolve_settings
from vacation_memories.doctor import format_report, has_blocking_failures, run_doctor
from vacation_memories.logging_setup import setup_logging
from vacation_memories.runtime_profile import DetectionEnv, detect_profile, write_runtime_profile
from vacation_memories.workdir import prepare_workdir

logger = logging.getLogger(__name__)


def _common_options() -> argparse.ArgumentParser:
    """Options valid before and after the subcommand. Only the ones given appear in the namespace,
    so the layers of the configuration (CLI, environment, file, defaults) stay distinguishable."""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    unset = argparse.SUPPRESS
    parser.add_argument("--config", type=Path, default=unset, help="TOML configuration file")
    parser.add_argument("--input", type=Path, default=unset, help="folder with the photos and clips")
    parser.add_argument("--workdir", type=Path, default=unset, help="working folder (default ./work)")
    parser.add_argument("--log-file", type=Path, default=unset, help="log file (default <workdir>/run.log)")
    parser.add_argument("--log-format", default=unset, help="text or json")
    parser.add_argument("--quiet", action="store_const", const=True, default=unset, help="only warnings on the console")
    parser.add_argument("--nice", default=unset, help="lower the priority of the process and its children (0-19)")
    parser.add_argument("--assume-tz", default=unset, help="time zone for the mtime fallback, e.g. Europe/Rome")
    parser.add_argument("--jobs", default=unset, help="parallel processes: auto or a number")
    parser.add_argument("--hwaccel", default=unset, help="auto, none or an accelerator name")
    parser.add_argument("--ollama-url", default=unset, help="Ollama server URL")
    parser.add_argument("--allow-remote-ollama", action="store_const", const=True, default=unset)
    parser.add_argument("--require-ai", action="store_const", const=True, default=unset)
    parser.add_argument("--no-ai", action="store_const", const=True, default=unset)
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(prog="vacation-memories", parents=[common], allow_abbrev=False)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("doctor", parents=[common], allow_abbrev=False, help="check the installation")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    detection_env: DetectionEnv | None = None,
) -> int:
    """Run the CLI; `environ` and `detection_env` let tests replace the process environment and the machine."""
    args = build_parser().parse_args(argv)
    if args.command is None:
        print(f"vacation-memories {__version__}")
        return exit_codes.OK
    return _run(args, os.environ if environ is None else environ, detection_env)


def _run(args: argparse.Namespace, environ: Mapping[str, str], detection_env: DetectionEnv | None) -> int:
    os.umask(0o077)
    _force_utf8_streams()

    options = {key: value for key, value in vars(args).items() if key != "command"}
    try:
        settings = resolve_settings(options, environ)
    except ConfigError as error:
        return _usage_error(error)

    workdir_error = None
    try:
        prepare_workdir(settings.workdir)
    except OSError as error:
        workdir_error = error

    log_file = None if workdir_error and settings.sources["log_file"] == "default" else settings.log_file
    try:
        if log_file is not None:
            _check_log_folder(log_file)
    except ConfigError as error:
        return _usage_error(error)
    setup_logging(log_file, settings.log_format, settings.quiet)

    if workdir_error:
        logger.error("cannot use the workdir %s: %s", settings.workdir, workdir_error)
    _apply_nice(settings.nice)
    if not settings.no_ai and settings.allow_remote_ollama and not ollama_url_is_local(settings.ollama_url):
        logger.warning(
            "the Ollama URL %s is not local: the images travel in base64 over unencrypted HTTP", settings.ollama_url
        )

    return _doctor(settings, detection_env, record_profile=workdir_error is None)


def _doctor(settings: Settings, detection_env: DetectionEnv | None, *, record_profile: bool) -> int:
    profile = detect_profile(settings, detection_env)
    if record_profile:
        write_runtime_profile(profile, settings.workdir / "runtime_profile.json")
    checks = run_doctor(settings, profile)
    for line in format_report(checks, profile):
        print(line)
    return exit_codes.REQUIREMENTS if has_blocking_failures(checks) else exit_codes.OK


def _usage_error(error: Exception) -> int:
    print(f"error: {error}", file=sys.stderr)
    return exit_codes.USAGE


def _check_log_folder(log_file: Path) -> None:
    folder = log_file.parent
    if not folder.is_dir() or not os.access(folder, os.W_OK | os.X_OK):
        raise ConfigError(f"the folder of the log file does not exist or is not writable: {folder}")


def _apply_nice(increment: int) -> None:
    if increment:
        try:
            os.nice(increment)
        except OSError as error:
            logger.warning("cannot lower the priority by %d: %s", increment, error.strerror)


def _force_utf8_streams() -> None:
    """Text I/O is UTF-8 whatever the locale (`LANG=C` included); what cannot be encoded is escaped."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass
