"""Enable the temporary parser cache only for workflow CLI subprocesses."""

import atexit
from functools import cache
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory


@cache
def _cache_directory():
    directory = TemporaryDirectory(prefix='p2p-test-mermaid-')
    atexit.register(directory.cleanup)
    return directory.name


def mermaid_cache_environment(env):
    if os.name != 'posix':
        return env
    path = env.get('PATH', os.defpath)
    node = shutil.which('node', path=path)
    if not node:
        return env
    shim_bin = Path(__file__).resolve().with_name('mermaid-bin')
    return {
        **env,
        'PATH': os.pathsep.join((str(shim_bin), path)),
        'P2P_TEST_REAL_NODE': os.path.abspath(node),
        'P2P_TEST_MERMAID_CACHE': _cache_directory(),
    }
