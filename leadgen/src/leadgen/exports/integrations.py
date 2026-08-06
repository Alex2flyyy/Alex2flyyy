"""Third-party export targets: Google Sheets, Notion, Airtable, CRM formats.

Each is optional and self-contained. A missing credential disables that target
with a clear message rather than raising — a broken Notion token must not stop
the CSV from being written.

Batching is not optional at these APIs' rate limits: Airtable accepts 10
records per request at 5 requests/second, Notion has no bulk create at all
(one request per page), and Sheets prefers one large write over many small
ones. Each function below is written to the shape its API actually wants.
"""

from __future__ import annotations

import asyncio
from typing import Any

from leadgen.config import get_settings
from leadgen.http import HttpFetcher
from leadgen.logging import get_logger

log = get_logger(__name__)


class ExportUnavailable(Exception):
    """The destination is not configured."""


# =============================================================================
# Google Sheets
# =============================================================================


def export_to_google_sheets(
    rows: list[dict[str, Any]],
    *,
    spreadsheet_id: str | None = None,
    worksheet_name: str | None = None,
    columns: list[str] | None = None,
) -> str:
    """Replace a worksheet's contents with the given rows.

    Uses a service account. Remember to share the target spreadsheet with the
    service account's email address — this is the step everyone forgets, and
    the resulting error ("caller does not have permission") does not mention it.
    """
    from leadgen.reports.daily_report import EXPORT_COLUMNS

    settings = get_settings()
    spreadsheet_id = spreadsheet_id or settings.google_sheets_spreadsheet_id
    credentials_file = settings.google_sheets_credentials_file

    if not spreadsheet_id or not credentials_file:
        raise ExportUnavailable(
            "set LEADGEN_GOOGLE_SHEETS_SPREADSHEET_ID and "
            "LEADGEN_GOOGLE_SHEETS_CREDENTIALS_FILE to enable Sheets export"
        )

    import gspread

    columns = columns or EXPORT_COLUMNS
    worksheet_name = worksheet_name or "Leads"

    client = gspread.service_account(filename=str(credentials_file))
    spreadsheet = client.open_by_key(spreadsheet_id)

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
        worksheet.clear()
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=worksheet_name, rows=max(len(rows) + 10, 100), cols=len(columns)
        )

    header = [c.replace("_", " ").title() for c in columns]
    body = [[_stringify(row.get(c, "")) for c in columns] for row in rows]

    # One update call for everything: Sheets quota is per-request, not per-cell.
    worksheet.update(values=[header, *body], range_name="A1")
    worksheet.freeze(rows=1)

    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
    log.info("export.sheets", rows=len(rows), worksheet=worksheet_name)
    return url


# =============================================================================
# Notion
# =============================================================================

NOTION_VERSION = "2022-06-28"

# Maps our row keys onto Notion property names and types. The target database
# must already have these properties; Notion will reject unknown ones.
NOTION_PROPERTY_MAP: list[tuple[str, str, str]] = [
    ("business_name", "Name", "title"),
    ("lead_score", "Lead Score", "number"),
    ("phone", "Phone", "phone_number"),
    ("email", "Email", "email"),
    ("website", "Website", "url"),
    ("website_status", "Website Status", "select"),
    ("city", "City", "rich_text"),
    ("state", "State", "select"),
    ("niche", "Niche", "select"),
    ("google_rating", "Rating", "number"),
    ("review_count", "Reviews", "number"),
    ("why_they_need_a_website", "Why", "rich_text"),
    ("outreach_angle", "Outreach Angle", "rich_text"),
    ("lead_status", "Status", "select"),
]


async def export_to_notion(
    rows: list[dict[str, Any]], *, database_id: str | None = None
) -> dict[str, Any]:
    """Create one Notion page per lead.

    Notion has no bulk-create endpoint, so this is one request per row, rate
    limited to roughly 3/second per their documented average.
    """
    settings = get_settings()
    api_key = settings.notion_api_key
    database_id = database_id or settings.notion_database_id

    if not api_key or not database_id:
        raise ExportUnavailable(
            "set LEADGEN_NOTION_API_KEY and LEADGEN_NOTION_DATABASE_ID to enable Notion export"
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    created = 0
    failed = 0

    async with HttpFetcher(max_attempts=3) as fetcher:
        for row in rows:
            payload = {
                "parent": {"database_id": database_id},
                "properties": _notion_properties(row),
            }
            response = await fetcher.post_json(
                "https://api.notion.com/v1/pages", payload, headers=headers
            )
            if response.ok:
                created += 1
            else:
                failed += 1
                if failed <= 3:
                    log.warning(
                        "export.notion_row_failed",
                        business=row.get("business_name"),
                        status=response.status_code,
                        body=response.text[:300],
                    )
            await asyncio.sleep(0.34)

    log.info("export.notion", created=created, failed=failed)
    return {"created": created, "failed": failed, "database_id": database_id}


def _notion_properties(row: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for key, prop_name, prop_type in NOTION_PROPERTY_MAP:
        value = row.get(key)
        if value in (None, "", []):
            continue
        if prop_type == "title":
            properties[prop_name] = {"title": [{"text": {"content": str(value)[:2000]}}]}
        elif prop_type == "rich_text":
            properties[prop_name] = {"rich_text": [{"text": {"content": str(value)[:2000]}}]}
        elif prop_type == "number":
            try:
                properties[prop_name] = {"number": float(value)}
            except (TypeError, ValueError):
                continue
        elif prop_type == "select":
            properties[prop_name] = {"select": {"name": str(value)[:100]}}
        elif prop_type == "url":
            properties[prop_name] = {"url": str(value)[:2000]}
        elif prop_type == "email":
            properties[prop_name] = {"email": str(value)[:200]}
        elif prop_type == "phone_number":
            properties[prop_name] = {"phone_number": str(value)[:100]}
    return properties


# =============================================================================
# Airtable
# =============================================================================

AIRTABLE_FIELD_MAP = {
    "business_name": "Business Name",
    "lead_score": "Lead Score",
    "phone": "Phone",
    "email": "Email",
    "website": "Website",
    "website_status": "Website Status",
    "website_score": "Website Score",
    "why_they_need_a_website": "Why",
    "street_address": "Address",
    "city": "City",
    "state": "State",
    "zip_code": "ZIP",
    "niche": "Niche",
    "google_rating": "Rating",
    "review_count": "Reviews",
    "google_maps_url": "Maps",
    "outreach_angle": "Outreach Angle",
    "lead_status": "Status",
}


async def export_to_airtable(
    rows: list[dict[str, Any]],
    *,
    base_id: str | None = None,
    table_name: str | None = None,
) -> dict[str, Any]:
    """Create Airtable records, 10 per request as the API requires."""
    settings = get_settings()
    api_key = settings.airtable_api_key
    base_id = base_id or settings.airtable_base_id
    table_name = table_name or settings.airtable_table_name

    if not api_key or not base_id:
        raise ExportUnavailable(
            "set LEADGEN_AIRTABLE_API_KEY and LEADGEN_AIRTABLE_BASE_ID to enable Airtable export"
        )

    url = f"https://api.airtable.com/v0/{base_id}/{table_name}"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    created = 0
    failed = 0

    async with HttpFetcher(max_attempts=3) as fetcher:
        for start in range(0, len(rows), 10):
            batch = rows[start : start + 10]
            payload = {
                "records": [
                    {
                        "fields": {
                            field: _airtable_value(row.get(key))
                            for key, field in AIRTABLE_FIELD_MAP.items()
                            if row.get(key) not in (None, "", [])
                        }
                    }
                    for row in batch
                ],
                # Tolerate a schema that doesn't have every field yet, rather
                # than failing the whole batch on one missing column.
                "typecast": True,
            }
            response = await fetcher.post_json(url, payload, headers=headers)
            if response.ok:
                created += len(batch)
            else:
                failed += len(batch)
                log.warning(
                    "export.airtable_batch_failed",
                    status=response.status_code,
                    body=response.text[:300],
                )
            await asyncio.sleep(0.25)  # 5 req/s limit, with headroom

    log.info("export.airtable", created=created, failed=failed)
    return {"created": created, "failed": failed, "base_id": base_id, "table": table_name}


def _airtable_value(value: Any) -> Any:
    if isinstance(value, list | tuple):
        return " | ".join(str(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {v}" for k, v in value.items())
    return value


# =============================================================================
# CRM-compatible CSV
# =============================================================================

# HubSpot and Salesforce both import a plain CSV; what differs is the column
# names they auto-map. These mappings mean the file drops straight into the
# import wizard without hand-mapping 30 fields.
CRM_MAPPINGS: dict[str, dict[str, str]] = {
    "hubspot": {
        "business_name": "Company name",
        "phone": "Phone Number",
        "email": "Email",
        "website": "Website URL",
        "street_address": "Street Address",
        "city": "City",
        "state": "State/Region",
        "zip_code": "Postal Code",
        "niche": "Industry",
        "lead_score": "HubSpot Score",
        "why_they_need_a_website": "Notes",
        "owner_name": "Contact owner",
        "lead_status": "Lead Status",
    },
    "salesforce": {
        "business_name": "Company",
        "owner_name": "LastName",
        "phone": "Phone",
        "email": "Email",
        "website": "Website",
        "street_address": "Street",
        "city": "City",
        "state": "State",
        "zip_code": "PostalCode",
        "niche": "Industry",
        "lead_score": "Rating",
        "why_they_need_a_website": "Description",
        "lead_status": "Status",
    },
    "pipedrive": {
        "business_name": "Organization",
        "owner_name": "Person Name",
        "phone": "Phone",
        "email": "Email",
        "website": "Website",
        "city": "City",
        "lead_score": "Lead Score",
        "why_they_need_a_website": "Note",
    },
    "generic": {
        "business_name": "company",
        "owner_name": "first_name",
        "phone": "phone",
        "email": "email",
        "website": "website",
        "street_address": "address",
        "city": "city",
        "state": "state",
        "zip_code": "zip",
        "niche": "industry",
        "lead_score": "score",
        "why_they_need_a_website": "notes",
    },
}


def export_to_crm(rows: list[dict[str, Any]], crm: str, path: Any) -> Any:
    """Write a CSV whose headers match a specific CRM's import format."""
    from pathlib import Path

    from leadgen.exports.csv_export import write_csv

    mapping = CRM_MAPPINGS.get(crm.lower())
    if mapping is None:
        raise ExportUnavailable(
            f"unknown CRM {crm!r}. Supported: {', '.join(sorted(CRM_MAPPINGS))}"
        )

    mapped_rows = [
        {target: row.get(source, "") for source, target in mapping.items()} for row in rows
    ]
    return write_csv(mapped_rows, Path(path), columns=list(mapping.values()))


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return " | ".join(str(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {v}" for k, v in value.items())
    return str(value)
