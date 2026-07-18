import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import boto3
from moto import mock_aws

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

_spec = importlib.util.spec_from_file_location(
    "migrate_remove_null_attribute", SCRIPTS_DIR / "migrate_remove_null_attribute.py"
)
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)

REGION = "us-east-1"
TABLE_NAME = "t"


def _create_table(client) -> None:
    client.create_table(
        TableName=TABLE_NAME,
        AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )


def test_is_null_email_present_returns_true_for_null_typed():
    assert migrate._is_null_email_present({"user_id": "u1", "email": None}) is True


def test_is_null_email_present_returns_false_for_absent_key():
    assert migrate._is_null_email_present({"user_id": "u1"}) is False


def test_is_null_email_present_returns_false_for_string_value():
    assert migrate._is_null_email_present({"user_id": "u1", "email": "jane@example.com"}) is False


@mock_aws
def test_dry_run_makes_no_update_calls():
    client = boto3.client("dynamodb", region_name=REGION)
    _create_table(client)
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    table.put_item(Item={"user_id": "u1", "name": "Broken", "email": None})

    with (
        patch.object(migrate, "_table", return_value=table),
        patch.object(table, "update_item", wraps=table.update_item) as mock_update,
        patch.object(sys, "argv", ["migrate_remove_null_attribute.py", "--table-name", TABLE_NAME]),
    ):
        migrate.main()

    mock_update.assert_not_called()
    assert table.get_item(Key={"user_id": "u1"})["Item"]["email"] is None


@mock_aws
def test_apply_makes_expected_update_call():
    client = boto3.client("dynamodb", region_name=REGION)
    _create_table(client)
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    table.put_item(Item={"user_id": "u1", "name": "Broken", "email": None})

    with (
        patch.object(migrate, "_table", return_value=table),
        patch.object(table, "update_item", wraps=table.update_item) as mock_update,
        patch.object(sys, "argv", ["migrate_remove_null_attribute.py", "--table-name", TABLE_NAME, "--apply"]),
    ):
        migrate.main()

    mock_update.assert_called_once_with(
        Key={"user_id": "u1"},
        UpdateExpression="REMOVE email",
        ConditionExpression="attribute_type(email, :nulltype)",
        ExpressionAttributeValues={":nulltype": "NULL"},
    )
    assert "email" not in table.get_item(Key={"user_id": "u1"})["Item"]
