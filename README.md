# aws-dynamodb-gsi-optional-attribute

A minimal AWS SAM stack — one DynamoDB table, one Lambda, and one
`update-table` call that adds a GSI mid-walkthrough — that reproduces a
DynamoDB gotcha that doesn't show up in your first hundred writes, doesn't
show up in a unit test that only checks what got built, and only detonates
once something tries to update a record that was written before the index
it now violates ever existed.

## Why this repo exists: the sparse-GSI NULL gotcha

`UsersTable` is going to get a GSI, `EmailIndex`, on an optional `email`
attribute — not every user has one. `email` will be declared `String`-typed
in the GSI's key schema. Writing a user with no email as `"email": None`
(Python) looks harmless. It isn't.

There's an easier failure mode this repo doesn't reproduce: a table where
the GSI is already `ACTIVE` when the buggy code first writes a bad record.
In that case DynamoDB rejects the write outright, right where the bug is,
and you fix it in five minutes. It's the same mechanic — an `ACTIVE` GSI
validates every write against its key schema — just applied at the moment
the bad record is being written rather than to one already sitting in the
table.

But shipping a table with all its future GSIs already attached from
`CREATE` isn't how this usually happens. Indexes get added later, as query
patterns emerge, against tables that already hold real data. That's the
scenario this repo reproduces.

When you add a GSI to a table that already contains items violating its key
schema — a `NULL` where the index expects a `String`, here — DynamoDB
doesn't reject the backfill, and it doesn't reject those items. It silently
excludes them from the new index and leaves them exactly as they were. This
is **grandfathering**, and it's documented AWS behavior, not a DynamoDB bug
(see [Detecting and correcting index key violations](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/GSI.OnlineOps.ViolationDetection.html)).
A grandfathered record looks completely normal in the base table — same
attributes, same values, nothing flags it. It's just silently missing from
the index, indistinguishable from a genuinely sparse record unless you go
looking.

Then, later, any write that touches that specific record — `PutItem` or
`UpdateItem`, whether or not the write touches `email` at all — is
rejected. Once the GSI is `ACTIVE`, DynamoDB validates the *post-write*
state of the whole item against every GSI's key schema on every write to
that item, not just the write being made. The record still has `email`
present as `NULL`; that's the state that gets checked, regardless of
whether this particular call is the one that put it there.

```
An error occurred (ValidationException) when calling the UpdateItem operation: The update expression attempted to update the secondary index key to unsupported type
```

That error text is misleading in a specific way worth internalizing: "the
update expression attempted to update the secondary index key" implies the
expression tried to write a bad value to `email`. It didn't — the
expression above only touches `name`. What DynamoDB is really rejecting is
the item's *post-write state*, but the error phrases it as if the caller's
expression is at fault. The clue that points at the real cause — a
pre-existing `NULL` on a GSI key attribute, from a write that happened
before the GSI existed — isn't in the error at all. That's part of why this
bug is hard to trace back from a stack trace alone.

Two things get a specific record out of this state:

- `PutItem` that overwrites the whole item with a corrected value.
- `UpdateItem` with `REMOVE` targeting the offending attribute specifically
  (what `scripts/migrate_remove_null_attribute.py` does, below).

Both work for the same reason: both leave the item's post-write state
clean. Neither is what an *unrelated* update naturally does — a caller
fixing someone's display name has no reason to also `REMOVE email` on an
attribute they never touched.

So the sequence that actually ships this bug: a record gets written before
the GSI exists, when there's nothing yet to violate. The GSI gets added
later and silently grandfathers the record in. The failure doesn't surface
until the *next* write to that specific record — often a different code
path than the one that created it (an admin edit, a status change, a
retry), run by someone with no reason to suspect this table has a GSI on an
optional field at all. The gap between cause (a write from before the index
existed) and symptom (an unrelated update months later) is what makes this
hard to trace back.

A genuinely **absent** `email` key is what makes a sparse index correctly
sparse, whether or not a GSI existed yet when the record was written. A
**present-but-NULL** `email` is fine, silently, right up until a GSI
notices it — and by then the write that caused it is long gone.

## The fix

`functions/ingest/app.py`'s `_build_item`:

```python
# DON'T: None becomes an explicit DynamoDB NULL, not an omitted attribute.
item = {
    "user_id": event["user_id"],
    "name": event["name"],
    "email": event.get("email"),
}

# DO: omit the key entirely when there's no value.
email = event.get("email")
item = {
    "user_id": event["user_id"],
    "name": event["name"],
}
if email:
    item["email"] = email
```

The difference is invisible in Python (`None` either way) but not in what
boto3 puts on the wire. Look at the actual `AttributeValue` shapes DynamoDB
receives:

```
# DON'T — email key present, NULL type. Fails EmailIndex's String schema
# on the item's next UpdateItem, whenever that happens.
{"user_id": {"S": "u123"}, "name": {"S": "Jane"}, "email": {"NULL": true}}

# DO — email key absent entirely. Sparse, correctly excluded from
# EmailIndex, and never a problem for any future write to this item.
{"user_id": {"S": "u123"}, "name": {"S": "Jane"}}
```

Any consumer reading this item back treats an absent key and an explicit
`None` identically via `.get("email")` — so this is a write-shape-only
change with no downstream read-side impact.

## The migration

Records written before this fix are stuck with `email` present as `NULL`.
`scripts/migrate_remove_null_attribute.py` cleans them up:

- **Scans the whole table**, not a filtered subset — a broken record could
  be in any state, and there's no index to query for "email is NULL"
  (that's the whole problem).
- **Idempotent**: the only records it touches are ones where `email` is
  present with a `NULL` value. A record where the key is already absent, or
  already holds a real string, is never a target — safe to re-run.
- **Condition-guarded**: the `UpdateItem` itself carries
  `ConditionExpression="attribute_type(email, :nulltype)"`, so if something
  else resolved the record between the scan and this script reaching it,
  the write is skipped rather than clobbering a real value.
- **Dry-run by default.** `--apply` is required to actually write.
- **Exits non-zero on partial failure**: if any record fails to migrate,
  the process exits with code 1, so scripted or automated invocations can't
  read partial failure as success.

```bash
python scripts/migrate_remove_null_attribute.py --table-name <TableName>             # dry-run
python scripts/migrate_remove_null_attribute.py --table-name <TableName> --apply      # writes
```

## Checklist: adding a GSI on an optional attribute

- **Omit the key on write when the value is absent.** Never set it to
  `None`/`null` — boto3 will happily serialize that to an explicit
  DynamoDB `NULL`, which is not the same thing as "not present."
- **Test a write against a pre-existing NULL-attribute record, not just a
  fresh insert.** A test suite that only checks the shape of a newly-built
  item can't catch this — the failure only appears on `UpdateItem` against
  a record that's already in the broken state. Write that regression test
  first; it's the other half of the fix, not an afterthought.
  (`tests/integration/test_gsi_grandfathering.py`'s
  `test_benign_update_on_grandfathered_record_fails_then_migration_fixes_it`
  is exactly this test — write a record before the GSI exists, add the GSI,
  prove the benign update fails, run the migration, prove it then succeeds.
  This specific mechanic can't be reproduced under `moto`, so this test
  hits real AWS behind an env var gate — see the "Development" section.)
- **If you already have existing NULL-attribute records, migrate before
  anything tries to update them.** The fix only changes what gets written
  going forward; it does nothing for records already stuck in the broken
  state.

## Prerequisites

- An AWS account, and the AWS SAM CLI installed
  (`sam --version`; see [AWS's install docs](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)).
- AWS CLI configured with credentials that can create a DynamoDB table and a
  Lambda function.

## Validate the template

```bash
sam validate --lint
```

Catches YAML syntax errors and structural template mistakes before you
spend time on a real deploy. Requires no AWS credentials and creates or
modifies nothing.

## Deploy

```bash
sam build
sam deploy --guided
```

`--guided` will prompt for:

| Parameter | Example | Notes |
|---|---|---|
| `TableName` | `gsi-optional-attribute-users` | must be globally unique to your account/region |
| `LogLevel` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

Answers are saved to `samconfig.toml` (gitignored — see
`samconfig.toml.example` for the format if you'd rather write it by hand and
skip `--guided` on subsequent deploys).

## Verify

This is the repro — it reproduces the actual bug against a real deployed
stack, not a description of one. `template.yaml` deploys `UsersTable`
*without* `EmailIndex` on purpose: this walkthrough adds the GSI the same
way it happens in practice, against a table that already has data, rather
than baking it into the initial `CREATE`.

```bash
# 1. Deploy the stack. UsersTable exists; EmailIndex doesn't yet.
sam build && sam deploy --guided

# 2. Ingest a user with no email through the real Lambda path - the
# correct, fixed code. Nothing to violate yet; there's no GSI.
aws lambda invoke --function-name <IngestFunctionName-from-stack-outputs> \
  --payload '{"user_id": "demo-1", "name": "Jane Doe"}' /tmp/out.json
cat /tmp/out.json

# 3. Simulate what a pre-fix _build_item would have written, before the fix
# existed: PutItem with email explicitly NULL. Succeeds - there's no
# EmailIndex yet to violate. This is exactly how the real broken records
# this repo is modeled on came to exist.
aws dynamodb put-item --table-name <TableName> --item \
  '{"user_id": {"S": "demo-broken"}, "name": {"S": "Broken Record"}, "email": {"NULL": true}}'

# 4. Confirm it: a completely normal-looking item in the base table.
aws dynamodb get-item --table-name <TableName> \
  --key '{"user_id": {"S": "demo-broken"}}'

# 5. Now add EmailIndex to the live table - the way this actually happens
# in production, via the API, against data that already exists.
aws dynamodb update-table --table-name <TableName> \
  --attribute-definitions AttributeName=email,AttributeType=S \
  --global-secondary-index-updates \
  '[{"Create": {"IndexName": "EmailIndex", "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}], "Projection": {"ProjectionType": "ALL"}}}]'

# Wait for the GSI to finish backfilling (may take a minute or two) -
# poll until this prints "ACTIVE":
aws dynamodb describe-table --table-name <TableName> \
  --query 'Table.GlobalSecondaryIndexes[0].IndexStatus'

# 6. Confirm the grandfathered record is silently absent from the new
# index - not an error, just missing, same as a genuinely sparse record.
aws dynamodb query --table-name <TableName> --index-name EmailIndex \
  --key-condition-expression "email = :e" \
  --expression-attribute-values '{":e": {"S": "does-not-matter"}}'
# Returns zero items. demo-broken is nowhere in this result, silently.

# 7. Try an ordinary update against demo-broken - any attribute, doesn't
# have to be email. This is where it fails.
aws dynamodb update-item --table-name <TableName> \
  --key '{"user_id": {"S": "demo-broken"}}' \
  --update-expression "SET #n = :n" \
  --expression-attribute-names '{"#n": "name"}' \
  --expression-attribute-values '{":n": {"S": "Still Broken"}}'
```

Step 7 fails with:

```
An error occurred (ValidationException) when calling the UpdateItem operation: The update expression attempted to update the secondary index key to unsupported type
```

Then confirm the fix and the migration both work:

```bash
# 8. Run the migration against demo-broken (dry-run, then --apply).
python scripts/migrate_remove_null_attribute.py --table-name <TableName>
python scripts/migrate_remove_null_attribute.py --table-name <TableName> --apply

# 9. Retry step 7's update. Succeeds now.
aws dynamodb update-item --table-name <TableName> \
  --key '{"user_id": {"S": "demo-broken"}}' \
  --update-expression "SET #n = :n" \
  --expression-attribute-names '{"#n": "name"}' \
  --expression-attribute-values '{":n": {"S": "Fixed Record"}}'
```

## Development

```bash
pip install -r requirements.txt
pytest
```

`pytest` alone only runs `tests/unit/` (no AWS credentials needed). One
mechanic in this repo can't be proven there: `moto` (the standard DynamoDB
mocking library) doesn't simulate the grandfather-then-reject-on-next-write
mechanic — a pre-existing NULL record survives a GSI add and permits
subsequent writes under `moto`, where real DynamoDB rejects them. That's
why the regression test that actually proves this repo's fix lives under
`tests/integration/` and hits real AWS:

```bash
RUN_AWS_INTEGRATION_TESTS=1 pytest tests/integration/
```

Creates and tears down its own throwaway table (uniquely named per run, no
manual cleanup needed). Requires AWS credentials with `CreateTable`,
`DescribeTable`, `UpdateTable`, `PutItem`, `GetItem`, `UpdateItem`, `Query`,
and `DeleteTable` on DynamoDB. Defaults to `us-east-1`; override with
`AWS_REGION`. Not wired into any CI — this repo doesn't ship one — so it
only runs when you opt in.

Unit tests under `tests/unit/` cover what `moto` *does* simulate faithfully:
the write-shape logic (`_build_item` omits/includes `email` correctly) and
the migration script's targeting and dry-run/apply logic.

## Cost

Effectively free at demo volume: `PAY_PER_REQUEST` DynamoDB billing (no
idle cost, no provisioned capacity) and one Lambda invoked a handful of
times while you run the Verify steps above.

## Cleanup

```bash
sam delete
```

## License

MIT — see [LICENSE](LICENSE).
