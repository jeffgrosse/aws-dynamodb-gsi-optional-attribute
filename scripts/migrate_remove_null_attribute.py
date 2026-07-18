#!/usr/bin/env python3
"""
Removes the `email` attribute entirely from UsersTable records where it's
currently stored as an explicit NULL, rather than genuinely absent.

Root cause (see README.md): EmailIndex declares `email` as a String-typed
GSI key. DynamoDB validates an item's post-write state against every GSI key
schema on any write, not just the attributes a given write touches - a
present-but-NULL email violates the S-type schema, where a genuinely absent
attribute is what makes the index correctly sparse. Records written before
the write path omitted the key on empty email (or before this fix was
deployed) are stuck in this broken state: any UpdateItem against them - an
unrelated field change, a retry, an admin fix - fails until this migration
runs once.

Scans the whole table rather than querying an index - there's no index to
query for "email is NULL" on a String-typed GSI key; that's the problem
this script exists to fix.

Idempotent: only acts on records where `email` is present with a NULL
value. A record where the key is already absent, or already holds a real
string, is never a target - safe to re-run. The update itself is
additionally guarded by a condition that the value is still NULL at write
time, in case something else resolved it between the scan and this script
reaching that record.

Usage:
    python scripts/migrate_remove_null_attribute.py --table-name <name>              # dry-run
    python scripts/migrate_remove_null_attribute.py --table-name <name> --apply       # writes
"""
import argparse
import sys

import boto3
from botocore.exceptions import ClientError


def _table(table_name: str, region: str | None):
    resource = boto3.resource("dynamodb", region_name=region) if region else boto3.resource("dynamodb")
    return resource.Table(table_name)


def _scan_all(table) -> list[dict]:
    items: list[dict] = []
    kwargs: dict = {}
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items


def _is_null_email_present(item: dict) -> bool:
    """True only for the broken state this migration fixes: `email` present
    on the item with a NULL value. Deliberately not `not item.get("email")` -
    that would also match a record where the key is already absent (already
    fine, no-op target) or holds an empty string (a different, unrelated
    data issue this script has no business touching)."""
    return "email" in item and item["email"] is None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table-name", required=True, help="DynamoDB table name (UsersTableName stack output)")
    parser.add_argument("--region", default=None, help="AWS region (defaults to your configured region)")
    parser.add_argument(
        "--apply", action="store_true", help="Actually write updates (default is dry-run)"
    )
    args = parser.parse_args()
    mode = "APPLY" if args.apply else "DRY-RUN"

    table = _table(args.table_name, args.region)
    items = _scan_all(table)
    print(f"Scanned {len(items)} record(s).")

    targets = [item for item in items if _is_null_email_present(item)]
    print(f"Found {len(targets)} record(s) with email present as NULL.\n")

    successes = 0
    race_skipped_user_ids: list[str] = []
    failed_user_ids: list[str] = []

    for item in targets:
        user_id = item["user_id"]
        name = item.get("name", "<unknown>")
        print(f"[{mode}] {user_id} ({name}): REMOVE email")

        if not args.apply:
            successes += 1
            continue

        try:
            table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="REMOVE email",
                ConditionExpression="attribute_type(email, :nulltype)",
                ExpressionAttributeValues={":nulltype": "NULL"},
            )
            successes += 1
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Not a bug - something else resolved the record's email
                # between the scan and this write. Re-running won't fix
                # this, because it's not broken.
                print(f"[RACE] Skipped {user_id} ({name}): email no longer NULL")
                race_skipped_user_ids.append(user_id)
            else:
                print(f"[FAIL] {user_id} ({name}): {exc}")
                failed_user_ids.append(user_id)

    print(
        f"\n{mode}: {successes} success(es), {len(failed_user_ids)} failure(s), "
        f"{len(race_skipped_user_ids)} race-skipped of {len(targets)} target(s)."
    )
    if failed_user_ids:
        print(f"Failed user_ids (re-run this script to retry): {failed_user_ids}")
    if race_skipped_user_ids:
        print(f"Race-skipped user_ids (email no longer NULL, not a bug): {race_skipped_user_ids}")
    if failed_user_ids:
        sys.exit(1)


if __name__ == "__main__":
    main()
