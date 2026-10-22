import os
from pathlib import Path

import pytest

from brush_bridge.formats import procreate_builtin


@pytest.fixture(autouse=True)
def _isolated_procreate_library(monkeypatch):
    """Ignore any Procreate library installed on this machine; tests opt in via the env variable."""
    monkeypatch.delenv(procreate_builtin.LIBRARY_ENV, raising=False)
    monkeypatch.setattr(procreate_builtin, "library_dirs", lambda: [
        Path(x) for x in os.environ.get(procreate_builtin.LIBRARY_ENV, "").split(os.pathsep)
        if x.strip() and Path(x).is_dir()])
