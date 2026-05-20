"""Generate minimal daily ops XLSX without third-party spreadsheet deps."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from typing import Any
from xml.sax.saxutils import escape


def _col_letter(n: int) -> str:
    s = ""
    while n >= 0:
        s = chr(65 + (n % 26)) + s
        n = n // 26 - 1
    return s


def _sheet_xml(rows: list[list[Any]]) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]
    for r_idx, row in enumerate(rows, start=1):
        parts.append(f'<row r="{r_idx}">')
        for c_idx, val in enumerate(row):
            ref = f"{_col_letter(c_idx)}{r_idx}"
            text = escape("" if val is None else str(val))
            parts.append(f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>')
        parts.append("</row>")
    parts.extend(["</sheetData>", "</worksheet>"])
    return "".join(parts)


def build_daily_report_xlsx(
    *,
    resource_snapshot: dict[str, Any] | None,
    recent_logs: list[dict[str, Any]] | None,
    ops_status: dict[str, Any] | None,
) -> bytes:
    """Build a single-sheet XLSX summarizing ops metrics and recent log lines."""
    snap = dict(resource_snapshot or {})
    logs = list(recent_logs or [])
    status = dict(ops_status or {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    rows: list[list[Any]] = [
        ["QuantBot Daily Ops Report", now],
        [],
        ["Resource snapshot"],
        ["Field", "Value"],
    ]
    for k in (
        "created_at",
        "process_cpu_pct",
        "process_memory_mb",
        "system_memory_pct",
        "disk_used_pct",
        "quantbot_db_mb",
        "ai_memory_db_mb",
        "ops_db_mb",
        "logs_dir_mb",
        "uptime_seconds",
        "worker_health",
        "broker_connection_health",
    ):
        rows.append([k, snap.get(k)])

    railway = dict(status.get("railway") or {})
    rows.extend(
        [
            [],
            ["Railway"],
            ["railway_api_connected", railway.get("railway_api_connected")],
            ["safe_error", railway.get("safe_error") or railway.get("note")],
            [],
            ["Recent ops logs (up to 50)"],
            ["Time", "Level", "Type", "Message"],
        ]
    )
    for lg in logs[:50]:
        rows.append(
            [
                lg.get("created_at"),
                lg.get("level"),
                lg.get("event_type"),
                lg.get("message"),
            ]
        )

    sheet = _sheet_xml(rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="DailyOps" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>",
        )
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()
