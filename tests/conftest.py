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


@pytest.fixture(autouse=True)
def isolated_scenario_storage(tmp_path, monkeypatch):
    """Keep every provider's new controls/resources away from local payment data."""
    from mock_controls import DEFAULTS
    from paypal import paypal as mock_paypal
    from virtualpos import virtualpos as mock_virtualpos

    for store in [
        mock_paypal.ORDERS_PAYPAL,
        mock_virtualpos.PAYMENTS,
        *DEFAULTS.values(),
    ]:
        monkeypatch.setattr(store, "path", tmp_path / "scenarios.sqlite3")
    monkeypatch.setattr(mock_paypal, "DATA_PAYPAL", dict(mock_paypal.DATA_PAYPAL))
    monkeypatch.setattr(mock_paypal, "WEBHOOK_DELIVERIES", [])
    monkeypatch.setattr(mock_virtualpos, "WEBHOOK_DELIVERIES", [])
