import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from knowledge_mcp.container_entrypoint import account_environment


@pytest.mark.parametrize("uid,gid", [(1000, 1000), (12345, 54321), (1000, 54321)])
def test_runtime_account_lookup(tmp_path, uid, gid):
    library = Path(os.environ.get("NSS_WRAPPER_TEST_LIBRARY", "/usr/local/lib/libnss_wrapper.so"))
    if not library.is_file():
        pytest.skip("nss_wrapper shared library is required")
    passwd, groups = tmp_path / "passwd", tmp_path / "group"
    passwd.write_text("root:x:0:0::/root:/bin/sh\n")
    groups.write_text("root:x:0:\n")
    env = os.environ | account_environment(uid, gid, passwd, groups, library)
    result = subprocess.run(
        [sys.executable, "-c", (
            "import pwd,grp,json; "
            f"p=pwd.getpwuid({uid}); g=grp.getgrgid({gid}); "
            "print(json.dumps([p.pw_uid,p.pw_gid,g.gr_gid]))"
        )],
        env=env, capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == [uid, gid, gid]
    assert passwd.read_text() == "root:x:0:0::/root:/bin/sh\n"


def test_existing_accounts_need_no_wrapper(tmp_path):
    passwd, groups = tmp_path / "passwd", tmp_path / "group"
    passwd.write_text("app:x:1000:2000::/tmp:/bin/sh\n")
    groups.write_text("app:x:2000:\n")
    assert account_environment(1000, 2000, passwd, groups, tmp_path / "missing.so") == {}
