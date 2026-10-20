import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def pdfs(tmp_path_factory):
    import fixtures
    return fixtures.make_all(str(tmp_path_factory.mktemp("pdfs")))


@pytest.fixture()
def out(tmp_path):
    return str(tmp_path / "out")
