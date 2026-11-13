"""Process exit codes (see "Monitoring and exit codes" in the specification)."""

import sys

OK = 0
ERROR = 1
USAGE = 2  # invalid usage or configuration
VERIFICATION_FAILED = 3
REQUIREMENTS = 4  # missing FFmpeg, space; Ollama or model only with --require-ai
WORKDIR_IN_USE = 5
SIGNAL_BASE = 128  # an orderly interruption exits with 128 + signal number


def usage_error(error: Exception) -> int:
    """Report an invalid usage or configuration on stderr and return its exit code."""
    print(f"error: {error}", file=sys.stderr)
    return USAGE
