"""
Integration test for the GSI-grandfathering mechanic this repo exists to
teach - not reproducible under moto (see README.md's "Development" section
for why: moto doesn't simulate a GSI silently grandfathering in pre-existing
non-conforming data, then rejecting the next write to it).

Scripts the DynamoDB-level portion of README.md's "Verify" walkthrough
(steps 3-9: it doesn't `sam deploy` a stack or invoke a Lambda, just the
raw table operations that actually demonstrate the bug and the fix)
directly against real AWS: creates a throwaway table, writes a
present-but-NULL record before any GSI exists, adds EmailIndex to the live
table, confirms a benign UpdateItem against that record fails, runs the
real migration script via subprocess, confirms the same update then
succeeds. Table is always torn down, even on failure.

Skipped by default - opt in with:

    RUN_AWS_INTEGRATION_TESTS=1 pytest tests/integration/

Requires AWS credentials with dynamodb:CreateTable, DescribeTable,
UpdateTable, PutItem, GetItem, UpdateItem, Query, DeleteTable in whatever
region you're targeting. Defaults to us-east-1; override with AWS_REGION.

GSI backfill time is not consistent - observed anywhere from ~1 minute to
10+ minutes on a table with a single item, seemingly tied to WarmThroughput
pre-provisioning rather than actual backfill work. If this test (or any
polling loop watching an IndexStatus) seems to hang, that's DynamoDB, not a
bug here - give it up to ~15 minutes before assuming something's wrong.
"""
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_AWS_INTEGRATION_TESTS") != "1",
    reason="set RUN_AWS_INTEGRATION_TESTS=1 to run against real AWS",
)

REGION = os.environ.get("AWS_REGION", "us-east-1")
MIGRATE_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "migrate_remove_null_attribute.py"


def _wait_for_gsi_active(client, table_name: str, index_name: str = "EmailIndex", timeout_s: int = 900) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        table = client.describe_table(TableName=table_name)["Table"]
        statuses = {i["IndexName"]: i["IndexStatus"] for i in table.get("GlobalSecondaryIndexes", [])}
        if statuses.get(index_name) == "ACTIVE":
            return
        time.sleep(3)
    raise TimeoutError(f"{index_name} on {table_name} did not reach ACTIVE within {timeout_s}s")


@pytest.fixture
def table_name():
    name = f"gsi-optional-attribute-inttest-{uuid.uuid4().hex[:8]}"
    client = boto3.client("dynamodb", region_name=REGION)
    created = False
    try:
        client.create_table(
            TableName=name,
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        created = True
        client.get_waiter("table_exists").wait(TableName=name)
        yield name
    finally:
        if created:
            # Table may still be CREATING/UPDATING (e.g. Ctrl-C mid-GSI-backfill
            # or a timeout in _wait_for_gsi_active) - DynamoDB rejects
            # DeleteTable until it settles. Retry until it's deletable rather
            # than leaking a real table into the reader's AWS account.
            for _ in range(60):  # up to ~5 min at 5s intervals
                try:
                    client.delete_table(TableName=name)
                    break
                except client.exceptions.ResourceInUseException:
                    time.sleep(5)


def test_benign_update_on_grandfathered_record_fails_then_migration_fixes_it(table_name):
    client = boto3.client("dynamodb", region_name=REGION)
    table = boto3.resource("dynamodb", region_name=REGION).Table(table_name)

    # Write a record with email explicitly NULL, before EmailIndex exists -
    # exactly how the real broken records this repo is modeled on came to
    # exist. Succeeds: there's nothing yet to violate.
    table.put_item(Item={"user_id": "demo-broken", "name": "Broken Record", "email": None})

    # Add EmailIndex to the live table, against data that already exists -
    # the way this actually happens in production.
    client.update_table(
        TableName=table_name,
        AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
        GlobalSecondaryIndexUpdates=[
            {
                "Create": {
                    "IndexName": "EmailIndex",
                    "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            }
        ],
    )
    _wait_for_gsi_active(client, table_name)

    # The grandfathered record is silently absent from the new index.
    query_result = table.query(
        IndexName="EmailIndex",
        KeyConditionExpression=Key("email").eq("does-not-matter"),
    )
    assert query_result["Items"] == []

    # A benign update - doesn't touch email at all - fails.
    with pytest.raises(ClientError) as exc_info:
        table.update_item(
            Key={"user_id": "demo-broken"},
            UpdateExpression="SET #n = :n",
            ExpressionAttributeNames={"#n": "name"},
            ExpressionAttributeValues={":n": "Still Broken"},
        )
    assert exc_info.value.response["Error"]["Code"] == "ValidationException"

    # Run the real migration script, not a reimplementation of its logic.
    subprocess.run(
        [sys.executable, str(MIGRATE_SCRIPT), "--table-name", table_name, "--region", REGION, "--apply"],
        check=True,
        capture_output=True,
        text=True,
    )

    # The same update now succeeds.
    table.update_item(
        Key={"user_id": "demo-broken"},
        UpdateExpression="SET #n = :n",
        ExpressionAttributeNames={"#n": "name"},
        ExpressionAttributeValues={":n": "Fixed Record"},
    )
    assert table.get_item(Key={"user_id": "demo-broken"})["Item"]["name"] == "Fixed Record"
