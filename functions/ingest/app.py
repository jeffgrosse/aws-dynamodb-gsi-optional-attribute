import logging
import os
from typing import Any, Optional

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_dynamodb = boto3.resource("dynamodb")


def _table():
    return _dynamodb.Table(os.environ["TABLE_NAME"])


def _build_item(event: dict) -> dict:
    email: Optional[str] = event.get("email")

    item = {
        "user_id": event["user_id"],
        "name": event["name"],
    }

    # This table gets a GSI, EmailIndex, on email - see README.md. A
    # present-but-NULL email is fine right up until that GSI exists: once it
    # does, DynamoDB validates the post-write state of the whole item
    # against every GSI key schema on any write to that item, and a
    # NULL-typed value against a String-typed key fails that check even on
    # an update that never touches email. Omitting the key entirely, rather
    # than writing None, keeps the item out of that state altogether - a
    # genuinely absent key is what makes the index correctly sparse.
    #
    # DON'T:
    #   item["email"] = email   # None serializes to an explicit DynamoDB
    #                           # NULL, not an omitted attribute.
    if email:
        item["email"] = email

    return item


def _ingest(event: dict) -> dict:
    item = _build_item(event)
    table = _table()

    table.put_item(Item=item)

    logger.info("Ingested user_id=%s email_present=%s", item["user_id"], "email" in item)
    return {"user_id": item["user_id"], "email_present": "email" in item}


def lambda_handler(event: dict, context: Any) -> dict:
    """
    Expected event shape:
    {
        "user_id": "...",
        "name": "...",
        "email": "..." | null
    }

    Fails loudly (raises KeyError) if user_id or name is missing.
    """
    return _ingest(event)
