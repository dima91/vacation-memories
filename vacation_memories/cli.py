"""Command line: global options, start-up (private files, workdir, logging) and the subcommands."""

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from vacation_memories import __version__, exit_codes
from vacation_memories.bench import DEFAULT_IMAGE_COUNT, run_bench_vision
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
    parser.add_argument("--model", default=unset, help="vision model served by Ollama")
    parser.add_argument("--ai-frames", default=unset, help="analyze exactly this many frames with the vision model")
    parser.add_argument("--ai-time-budget", default=unset, help="seconds the AI analysis may take (not with --ai-frames)")
    parser.add_argument("--allow-remote-ollama", action="store_const", const=True, default=unset)
    parser.add_argument("--require-ai", action="store_const", const=True, default=unset)
    parser.add_argument("--no-ai", action="store_const", const=True, default=unset)
    return parser


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(prog="vacation-memories", parents=[common], allow_abbrev=False)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("doctor", parents=[common], allow_abbrev=False, help="check the installation")
    bench = commands.add_parser("bench", parents=[common], allow_abbrev=False, help="measure the machine")
    bench_commands = bench.add_subparsers(dest="bench_command", required=True)
    vision = bench_commands.add_parser(
        "vision", parents=[common], allow_abbrev=False, help="seconds per frame of the vision model"
    )
    vision.add_argument("--images", type=Path, help="folder with photos to use instead of synthetic images")
    vision.add_argument("--count", type=int, default=DEFAULT_IMAGE_COUNT, help="number of images (default 5)")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    detection_env: DetectionEnv | None = None,
) -> int:
    """Run the CLI; `environ` and `detection_env` let tests replace the process environment and the machine."""
    args = build_parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    options = {key: value for key, value in vars(args).items() if key not in _COMMAND_ARGUMENTS}
    if args.command is None:
        # Nothing to run yet, but options that were given are still checked, not silently ignored.
        if options:
            try:
                resolve_settings(options, environ)
            except ConfigError as error:
                return _usage_error(error)
        print(f"vacation-memories {__version__}")
        return exit_codes.OK
    return _run(args, options, environ, detection_env)


def _run(
    args: argparse.Namespace, options: dict, environ: Mapping[str, str], detection_env: DetectionEnv | None
) -> int:
    os.umask(0o077)
    _force_utf8_streams()

    try:
        settings = resolve_settings(options, environ)
    except ConfigError as error:
        return _usage_error(error)

    workdir_error = None
    try:
        prepare_workdir(settings.workdir)
    except OSError as error:
        workdir_error = error

    log_file = settings.log_file
    console_only_reason = None
    try:
        _check_log_file(log_file)
        setup_logging(log_file, settings.log_format, settings.quiet)
    except (ConfigError, OSError) as error:
        if settings.sources["log_file"] != "default":
            return _usage_error(error)
        # The default log file lives in the workdir: if that cannot be used, `doctor` must still
        # report it instead of failing here.
        console_only_reason = error
        setup_logging(None, settings.log_format, settings.quiet)

    if console_only_reason:
        logger.warning("logging to the console only: %s", console_only_reason)
    if workdir_error:
        logger.error("cannot use the workdir %s: %s", settings.workdir, workdir_error)
    _apply_nice(settings.nice)
    if not settings.no_ai and settings.allow_remote_ollama and not ollama_url_is_local(settings.ollama_url):
        logger.warning(
            "the Ollama URL %s is not local: the images travel in base64 over unencrypted HTTP", settings.ollama_url
        )

    return _COMMANDS[args.command](args, settings, detection_env, record_profile=workdir_error is None)


def _detect_and_record(settings: Settings, detection_env: DetectionEnv | None, *, record_profile: bool):
    profile = detect_profile(settings, detection_env)
    if record_profile:
        target = settings.workdir / "runtime_profile.json"
        try:
            write_runtime_profile(profile, target)
        except OSError as error:
            logger.warning("cannot save %s: %s", target, error.strerror or error)
    return profile


def _doctor(
    args: argparse.Namespace, settings: Settings, detection_env: DetectionEnv | None, *, record_profile: bool
) -> int:
    profile = _detect_and_record(settings, detection_env, record_profile=record_profile)
    checks = run_doctor(settings, profile)
    for line in format_report(checks, profile):
        print(line)
    return exit_codes.REQUIREMENTS if has_blocking_failures(checks) else exit_codes.OK


def _bench(
    args: argparse.Namespace, settings: Settings, detection_env: DetectionEnv | None, *, record_profile: bool
) -> int:
    profile = _detect_and_record(settings, detection_env, record_profile=record_profile)
    try:
        return run_bench_vision(settings, profile, detection_env, images=args.images, count=args.count)
    except ConfigError as error:
        return _usage_error(error)


_COMMANDS = {"doctor": _doctor, "bench": _bench}
_COMMAND_ARGUMENTS = frozenset({"command", "bench_command", "images", "count"})  # not settings


def _usage_error(error: Exception) -> int:
    print(f"error: {error}", file=sys.stderr)
    return exit_codes.USAGE


def _check_log_file(log_file: Path) -> None:
    """The folder must exist and be writable, and an existing file must be a writable regular file."""
    folder = log_file.parent
    if not folder.is_dir() or not os.access(folder, os.W_OK | os.X_OK):
        raise ConfigError(f"the folder of the log file does not exist or is not writable: {folder}")
    if log_file.exists() and (not log_file.is_file() or not os.access(log_file, os.W_OK)):
        raise ConfigError(f"the log file is not a writable file: {log_file}")


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
