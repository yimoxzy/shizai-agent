"""Standard-stream guards for frozen stdio applications."""

import sys
from typing import Any


class ShutdownSafeStderr:
    """Keep late interpreter-shutdown writes from becoming a traceback."""

    def __init__(self, stream: Any):
        self._stream = stream

    def write(self, value: str) -> int:
        try:
            return self._stream.write(value)
        except (OSError, ValueError):
            return len(value)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        # The interpreter owns the real standard stream.  Do not close it from
        # a callback that may run after the stdio transport is gone.
        self.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def install_shutdown_safe_stderr() -> None:
    """Guard frozen-app stderr while preserving normal source-mode behavior."""
    if getattr(sys, "frozen", False) and not isinstance(sys.stderr, ShutdownSafeStderr):
        sys.stderr = ShutdownSafeStderr(sys.stderr)
