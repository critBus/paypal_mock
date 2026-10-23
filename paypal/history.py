"""PayPal hosted-link controls and durable recovery APIs, independent of callback delivery."""

from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlencode

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel

from app import app
from mock_controls import (
    ControlRequest,
    configure,
    consume_fault,
    fault_response,
    note,
    paypal_error,
)
from paypal import paypal as core
from paypal.orders import global_fault

EVENTS_PATH = "/v1/notifications/webhooks-events"
TRANSACTIONS_PATH = "/v1/reporting/transactions"


def parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Timezone required")
        return result.astimezone(timezone.utc)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            400, "Invalid timestamp; use an ISO-8601 date with timezone"
        ) from exc


def window(start: str | None, end: str | None, *, reporting=False):
    finish = parse_time(end) if end else core.utc_now()
    begin = parse_time(start) if start else finish - timedelta(days=30)
    if begin > finish or (reporting and finish - begin > timedelta(days=31)):
        raise HTTPException(
            400, "Invalid date range; reporting accepts at most 31 days"
        )
    return begin, finish


def visible(resource: dict, timestamp: str, setting: str) -> bool:
    delay = resource.get("mock", {}).get(setting, 0)
    return core.utc_now() >= parse_time(timestamp) + timedelta(seconds=delay)


def event_resource(event: dict) -> dict:
    data = event.get("resource", {})
    identifier = data.get("payment_resource_id") or data.get("id")
    return core.PAYMENT_RESOURCES.get(identifier, {})


def event_visible(event: dict) -> bool:
    return visible(
        event_resource(event), event["create_time"], "event_list_delay_seconds"
    )


def page_response(request: Request, rows: list, key: str, page: int, size: int) -> dict:
    total = len(rows)
    pages = max(1, (total + size - 1) // size)
    start = (page - 1) * size
    links = []
    for rel, number in (("self", page), ("next", page + 1), ("previous", page - 1)):
        if rel == "next" and page >= pages or rel == "previous" and page <= 1:
            continue
        params = dict(request.query_params)
        params.update(page=number, page_size=size)
        links.append(
            {
                "rel": rel,
                "href": f"{str(request.url).split('?')[0]}?{urlencode(params)}",
                "method": "GET",
            }
        )
    return {
        key: rows[start : start + size],
        "total_items": total,
        "total_pages": pages,
        "links": links,
    }


@app.get(EVENTS_PATH)
async def list_events(
    request: Request,
    start_time: str | None = None,
    end_time: str | None = None,
    event_type: str | None = None,
    transaction_id: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
):
    if response := await global_fault("events"):
        return response
    begin, end = window(start_time, end_time)
    events = [row["event"] for row in core.WEBHOOK_EVENTS.values()]
    events = [
        event
        for event in events
        if begin <= parse_time(event["create_time"]) <= end
        and event_visible(event)
        and (not event_type or event["event_type"] == event_type)
        and (
            not transaction_id
            or event.get("resource", {}).get("transaction_id") == transaction_id
        )
    ]
    events.sort(key=lambda event: (event["create_time"], event["id"]), reverse=True)
    return page_response(request, events, "events", page, page_size)


@app.get(EVENTS_PATH + "/{event_id}")
async def event_details(event_id: str):
    saved = core.WEBHOOK_EVENTS.get(event_id)
    if not saved or not event_visible(saved["event"]):
        return paypal_error("RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", 404)
    return saved["event"]


@app.get(TRANSACTIONS_PATH)
async def search_transactions(
    request: Request,
    start_date: str,
    end_date: str,
    transaction_currency: str | None = None,
    transaction_id: str | None = None,
    fields: str = "transaction_info",
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
):
    if response := await global_fault("transactions"):
        return response
    begin, end = window(start_date, end_date, reporting=True)
    rows = []
    for resource in core.PAYMENT_RESOURCES.values():
        if not resource.get("payment_completed"):
            continue
        timestamp = resource["transaction_time"]
        if not begin <= parse_time(timestamp) <= end or not visible(
            resource, timestamp, "transaction_search_delay_seconds"
        ):
            continue
        total, currency = core._payment_resource_total(resource)
        if transaction_currency and transaction_currency != currency:
            continue
        if transaction_id and transaction_id != resource["transaction_id"]:
            continue
        rows.append(
            {
                "transaction_info": {
                    "transaction_id": resource["transaction_id"],
                    "transaction_event_code": "T0006",
                    "transaction_status": "S",
                    "transaction_initiation_date": timestamp,
                    "transaction_updated_date": timestamp,
                    "transaction_amount": {
                        "value": f"{total:.2f}",
                        "currency_code": currency,
                    },
                },
                "cart_info": {
                    "item_details": [
                        {
                            "item_code": item.get("product_id", ""),
                            "item_name": item["name"],
                            "item_quantity": "1",
                            "item_unit_price": item["unit_amount"],
                        }
                        for item in resource["line_items"]
                    ]
                },
                "payer_info": {
                    "email_address": resource.get("mock", {}).get(
                        "payer_email", "mock-buyer@personal.example.com"
                    )
                },
            }
        )
    rows.sort(
        key=lambda row: (
            row["transaction_info"]["transaction_updated_date"],
            row["transaction_info"]["transaction_id"],
        ),
        reverse=True,
    )
    result = page_response(request, rows, "transaction_details", page, page_size)
    result.update(
        page=page,
        start_date=core.paypal_datetime(begin),
        end_date=core.paypal_datetime(end),
    )
    return result


async def resource_fault(resource: dict, operation: str):
    config = resource.setdefault("mock", {})
    note(config, operation, resource["status"])
    fault = consume_fault(config, operation)
    core.PAYMENT_RESOURCES[resource["id"]] = resource
    if unavailable := config.get("unavailable"):
        return paypal_error(unavailable, "INVALID_RESOURCE_ID", 404)
    return await fault_response("paypal", fault)


def update_link_controls(data: ControlRequest) -> dict:
    resource = core.PAYMENT_RESOURCES.get(data.resource_id)
    if not resource:
        raise HTTPException(404, "Payment Resource not found")
    if data.status:
        raise HTTPException(
            422, "Use the checkout or DELETE API to change a hosted link's state"
        )
    if data.faults and set(data.faults) & {"events", "transactions"}:
        raise HTTPException(
            422,
            "Configure events/transactions faults on future payments (global API controls)",
        )
    resource["mock"] = configure("paypal", data, resource.get("mock"))
    core.PAYMENT_RESOURCES[data.resource_id] = resource
    return resource


class ReplayLinkEvent(BaseModel):
    event: Literal["created", "completed", "deleted"] = "completed"


@app.post("/mock/paypal/payment-resources/{resource_id}/replay")
async def replay_link_event(resource_id: str, data: ReplayLinkEvent):
    resource = core.PAYMENT_RESOURCES.get(resource_id)
    if not resource:
        raise HTTPException(404, "Payment Resource not found")
    kind = {
        "created": "CREATED",
        "completed": "PAYMENT-COMPLETED",
        "deleted": "DELETED",
    }[data.event]
    events = [row["event"] for row in core.WEBHOOK_EVENTS.values()]
    matches = [
        event
        for event in events
        if event["event_type"] == f"CHECKOUT.PAYMENT-RESOURCE.{kind}"
        and (
            event["resource"].get("payment_resource_id") or event["resource"].get("id")
        )
        == resource_id
    ]
    if not matches:
        raise HTTPException(409, "The requested event has not happened yet")
    event = max(matches, key=lambda value: value["create_time"])
    prepared = core._build_signed_webhook(
        event_type=event["event_type"],
        resource_type=event["resource_type"],
        summary=event["summary"],
        resource=event["resource"],
        event=event,
    )
    await core._deliver_webhook(
        event_type=event["event_type"],
        resource_type=event["resource_type"],
        summary=event["summary"],
        resource=event["resource"],
        prepared=prepared,
        mode=resource.get("mock", {}).get("webhook_mode", "normal"),
    )
    return {"event_id": event["id"], "payment_resource_id": resource_id}
