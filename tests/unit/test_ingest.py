import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FUNCTIONS_DIR = Path(__file__).resolve().parents[2] / "functions" / "ingest"
sys.path.insert(0, str(FUNCTIONS_DIR))

_spec = importlib.util.spec_from_file_location("ingest_app", FUNCTIONS_DIR / "app.py")
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

EVENT = {"user_id": "u1", "name": "Jane Doe", "email": "jane@example.com"}


def test_build_item_includes_email_key_when_present():
    item = app._build_item(EVENT)
    assert item["email"] == "jane@example.com"


def test_build_item_omits_email_key_when_none():
    # Regression guard for the GSI schema violation this repo exists to
    # teach: a present-but-NULL email fails DynamoDB's GSI key-type
    # validation the moment a GSI exists on it. Omitting the key entirely
    # is what keeps a no-email user out of that state.
    item = app._build_item(dict(EVENT, email=None))
    assert "email" not in item


@patch.object(app, "_table")
def test_lambda_handler_ingests_user_with_email(mock_table_fn):
    mock_table = MagicMock()
    mock_table_fn.return_value = mock_table

    result = app.lambda_handler(EVENT, context=None)

    assert result == {"user_id": "u1", "email_present": True}
    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["email"] == "jane@example.com"


@patch.object(app, "_table")
def test_lambda_handler_ingests_user_with_no_email(mock_table_fn):
    mock_table = MagicMock()
    mock_table_fn.return_value = mock_table
    record = dict(EVENT, email=None)

    result = app.lambda_handler(record, context=None)

    assert result == {"user_id": "u1", "email_present": False}
    item = mock_table.put_item.call_args.kwargs["Item"]
    assert "email" not in item


@patch.object(app, "_table")
def test_lambda_handler_rejects_missing_required_field(mock_table_fn):
    with pytest.raises(KeyError):
        app.lambda_handler({"user_id": "u1"}, context=None)

    mock_table_fn.assert_not_called()
