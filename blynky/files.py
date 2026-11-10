"""Writing a file so that a reader only ever sees the old one or the new one."""

import os
import secrets


def write_atomic(path, data, mode=0o666):
    """Write data (str or bytes) to path by way of a temporary file.

    The temporary file is a dot-name beside the target, which the archive
    scanner skips, and unique per call, so two writes of the same file at once
    cannot interleave: the last rename wins whole. A crash leaves the old file
    and, at worst, a stray .tmp, never half of either.

    mode is filtered through the umask as usual; 0o600 makes a file only its
    owner can read from the first byte, with no moment in between.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
