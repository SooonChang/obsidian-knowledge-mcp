"""Provide account lookup for arbitrary Compose UIDs without root privileges."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def account_environment(
    uid: int,
    gid: int,
    passwd_path: Path = Path("/etc/passwd"),
    group_path: Path = Path("/etc/group"),
    library: Path = Path("/usr/local/lib/libnss_wrapper.so"),
) -> dict[str, str]:
    passwd = passwd_path.read_text()
    groups = group_path.read_text()
    has_user = any(line.split(":")[2] == str(uid) for line in passwd.splitlines() if line)
    has_group = any(line.split(":")[2] == str(gid) for line in groups.splitlines() if line)
    if has_user and has_group:
        return {}
    if not library.is_file():
        raise RuntimeError("libnss_wrapper is required for this container UID/GID")

    # /tmp is writable even with Compose's read-only filesystem. Keep these
    # files for the process lifetime; SSH inherits the same account lookup.
    directory = Path(tempfile.mkdtemp(prefix="knowledge-account-", dir="/tmp"))
    if not has_user:
        passwd = passwd.rstrip("\n") + f"\nknowledge-{uid}:x:{uid}:{gid}::{directory}:/bin/sh\n"
    if not has_group:
        groups = groups.rstrip("\n") + f"\nknowledge-{gid}:x:{gid}:\n"
    (directory / "passwd").write_text(passwd)
    (directory / "group").write_text(groups)
    environment = {
        "NSS_WRAPPER_PASSWD": str(directory / "passwd"),
        "NSS_WRAPPER_GROUP": str(directory / "group"),
        "LD_PRELOAD": " ".join(filter(None, (str(library), os.environ.get("LD_PRELOAD")))),
    }
    if not has_user:
        environment["HOME"] = str(directory)
    return environment


def main() -> None:
    os.environ.update(account_environment(os.getuid(), os.getgid()))
    os.execvp("knowledge-mcp", ["knowledge-mcp", *sys.argv[1:]])


if __name__ == "__main__":
    main()
