"""Bounded subprocess transport: allowlisted executable/argv, never a shell."""

import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

from .errors import DomainError
from .models import ErrorCode


class BoundedRunner:
    def __init__(self, timeout: float = 10, max_bytes: int = 1048576):
        self.timeout = timeout
        self.max_bytes = max_bytes

    def run(self, argv: tuple[str, ...]) -> str:
        if not argv or not Path(argv[0]).is_absolute():
            raise DomainError(ErrorCode.FORBIDDEN, "Executable must be absolute")
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/",
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                start_new_session=True,
            )
        except OSError:
            raise DomainError(ErrorCode.UNAVAILABLE, "Command unavailable") from None
        output = bytearray()
        total = 0
        deadline = time.monotonic() + self.timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ, True)
                selector.register(proc.stderr, selectors.EVENT_READ, False)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DomainError(ErrorCode.TIMEOUT, "Command timed out")
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fd, min(65536, self.max_bytes + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise DomainError(
                                ErrorCode.OUTPUT_LIMIT, "Command output exceeded limit"
                            )
                        if key.data:
                            output.extend(chunk)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DomainError(ErrorCode.TIMEOUT, "Command timed out")
                try:
                    rc = proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    raise DomainError(ErrorCode.TIMEOUT, "Command timed out") from None
                if rc:
                    raise DomainError(ErrorCode.UNAVAILABLE, "Command returned nonzero status")
                try:
                    return output.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    raise DomainError(ErrorCode.PARSE_ERROR, "Invalid command encoding") from None
        finally:
            # Kill the group even if the parent exited; a child may retain pipe FDs.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()
