"""Synthetic documented BMSPay notifications; delivery controls belong to the mock only."""

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import httpx


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def new_event(link, outcome):
    event_id = str(uuid4())
    occurred_at = timestamp()
    approved = outcome == "success"
    payload = {
        "Request": {
            "Amount": float(Decimal(link["Amount"])),
            "PaymentLinkId": link["Id"],
            "InvoiceNumber": link["InvoiceNumber"],
            "UserTransactionNumber": event_id,
        },
        "Response": {
            "ResponseCode": 200 if approved else 3,
            "Msg": ["APPROVED" if approved else "DECLINED"],
            "Verbiage": "APPROVED" if approved else "DECLINED",
        },
    }
    if os.getenv("BMS_MOCK_WEBHOOK_VERSION", "1") == "2":
        payload.update(
            SchemaVersion=2,
            EventId=event_id,
            EventType="Sale",
            OccurredAtUtc=occurred_at,
            MerchantId=str(link["MerchantId"]),
            IsTest=link.get("_is_test", True),
            Source="Portal",
            Stage="Completed",
            Outcome="Succeeded" if approved else "Failed",
        )
        payload["Response"]["ServiceReferenceNumber"] = link["ServiceReferenceNumber"]
    return {
        "id": event_id,
        "created_at": occurred_at,
        "payload": payload,
        "status": "pending",
        "attempts": 0,
        "last_http_status": None,
        "last_error": None,
        "delivered_at": None,
    }


def find_event(link, event_id):
    return next(
        (event for event in link.get("_webhooks", []) if event["id"] == event_id), None
    )


async def deliver(storage, link_id, event_id):
    """Retry without rolling back payment or delaying the checkout HTTP response."""
    destination = os.getenv("BMS_MOCK_WEBHOOK_URL", "http://localhost:8001/api/bms/webhooks/notifications/")
    token = os.getenv("BMS_MOCK_WEBHOOK_TOKEN", "whtoken")
    link = storage.get(link_id)
    event = find_event(link, event_id) if link else None
    if not event:
        return
    if not destination or not token:

        def unconfigured(current):
            find_event(current, event_id)["status"] = "unconfigured"

        storage.update(link_id, unconfigured)
        return
    try:
        attempts = max(1, min(10, int(os.getenv("BMS_MOCK_WEBHOOK_ATTEMPTS", "3"))))
    except ValueError:
        attempts = 3
    for index in range(attempts):
        code, error = None, None
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                response = await client.post(
                    destination, json=event["payload"], headers={"x-signature": token}
                )
                code = response.status_code
        except httpx.HTTPError as exc:
            # Exception strings may include destinations or credentials.
            error = type(exc).__name__
        success = code is not None and 200 <= code < 300

        def record(current):
            saved = find_event(current, event_id)
            saved["attempts"] += 1
            saved.update(
                last_http_status=code,
                last_error=error,
                status="delivered" if success else "failed",
            )
            if success:
                saved["delivered_at"] = timestamp()

        storage.update(link_id, record)
        if success:
            break
        if index + 1 < attempts:
            await asyncio.sleep(1)
