import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolated_revolut_storage(tmp_path, monkeypatch):
    from revolut import revolut as mock_revolut
    from revolut.storage import RevolutStorage

    monkeypatch.setattr(
        mock_revolut, "storage", RevolutStorage(tmp_path / "revolut.sqlite3")
    )
