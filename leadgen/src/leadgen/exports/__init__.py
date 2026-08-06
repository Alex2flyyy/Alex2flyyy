"""Export destinations.

All exporters consume the row shape produced by
:func:`leadgen.reports.daily_report.lead_to_row`, so adding a field there makes
it available in every destination at once.
"""

from leadgen.exports.csv_export import write_csv
from leadgen.exports.excel import write_excel
from leadgen.exports.integrations import (
    CRM_MAPPINGS,
    ExportUnavailable,
    export_to_airtable,
    export_to_crm,
    export_to_google_sheets,
    export_to_notion,
)

DESTINATIONS = ("csv", "xlsx", "google_sheets", "notion", "airtable", "crm")

__all__ = [
    "CRM_MAPPINGS",
    "DESTINATIONS",
    "ExportUnavailable",
    "export_to_airtable",
    "export_to_crm",
    "export_to_google_sheets",
    "export_to_notion",
    "write_csv",
    "write_excel",
]
