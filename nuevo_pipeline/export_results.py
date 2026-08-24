import argparse
import csv
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nuevo_pipeline.constants import OUTPUT_COLUMNS
from nuevo_pipeline.database import connect, initialize


def loads(value, default=None):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def empty_row():
    return {column: "" for column in OUTPUT_COLUMNS}


def base_row(filing):
    source = loads(filing["input_json"], {}) or {}
    job = loads(filing["job_json"], {}) or {}
    pw1 = loads(filing["pw1_json"], {}) or {}
    row = empty_row()
    mappings = {
        "Job Filing Number": ("JobNumber_FilingNumber", "Job Filing Number"),
        "Filing Status": ("FilingStatusDescription", "Filing Status"),
        "Filing Date": ("FilingDate", "Filing Date"),
        "House No": ("HouseNo", "House No"),
        "Street Name": ("StreetName", "Street Name"),
        "Borough": ("Borough", "Borough"),
        "Block": ("Block", "Block"),
        "LOT": ("LOT", "LOT"),
        "Bin": ("Bin", "Bin"),
        "Job Description": ("JobDescription", "Job Description"),
        "Filing Review Type": ("FilingReviewType", "Filing Review Type"),
    }
    for output, (job_key, source_key) in mappings.items():
        row[output] = str(job.get(job_key) or source.get(source_key) or "")
    row["Job Filing Number"] = row["Job Filing Number"] or filing["job_filing_number"]
    row["Bin"] = row["Bin"] or filing["bin"]
    row["guid"] = filing["guid"] or ""
    row["filing_status"] = str(pw1.get("CurrentFilingStatusValue") or "")
    return row


def zoning_status(filing):
    if filing["zd1wd_status"] != "done":
        return "BLOCKED" if "BLOCK" in (filing["last_error"] or "").upper() else ""
    return "HAS ZONING DOCUMENTS" if (loads(filing["zd1wd_json"], []) or []) else "NO ZONING DOCUMENTS"


def rows_for_filing(conn, filing):
    base = base_row(filing)
    zone = zoning_status(filing)
    documents = conn.execute("SELECT * FROM documents WHERE filing_id=? ORDER BY id", (filing["id"],)).fetchall()
    if documents:
        for document in documents:
            row = dict(base)
            row.update({
                "doc_description": document["description"],
                "doc_name": document["name"],
                "doc_url_original": document["document_url"],
                "download_url": document["download_url"],
                "result_status": (
                    "OK" if document["matched"] and document["download_status"] == "done"
                    else "DOWNLOAD_PENDING" if document["matched"] and document["download_status"] in ("pending", "running")
                    else "DOWNLOAD_ERROR" if document["matched"] and document["download_status"] == "retry"
                    else "DOWNLOAD_ERROR" if document["matched"]
                    else "FILTERED"
                ),
                "error_body": document["last_error"],
                "zoning_status": zone,
                "doc_create_on": document["create_on"],
                "doc_category": document["category"],
                "doc_type_name": document["type_name"],
                "doc_status_label": document["status_label"],
            })
            yield row
        return

    row = dict(base)
    row["zoning_status"] = zone
    row["error_body"] = filing["last_error"] or ""
    if filing["search_status"] == "job_not_found":
        row["result_status"] = "JOB_NOT_FOUND"
    elif filing["search_status"] != "done":
        row["result_status"] = "AKAMAI_BLOCKED" if "BLOCK" in row["error_body"].upper() else "PENDING"
    elif filing["pw1_status"] == filing["zd1wd_status"] == filing["portal_status"] == "done" and filing["normalized"]:
        row["result_status"] = "NO DOCUMENTS"
    else:
        row["result_status"] = "AKAMAI_BLOCKED" if "BLOCK" in row["error_body"].upper() else "PENDING"
    yield row


def export_database(db_path, output_path):
    conn = connect(db_path)
    initialize(conn)
    temp_path = output_path + ".tmp"
    counts = {"rows": 0, "filings": 0}
    with open(temp_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        filings = conn.execute("SELECT * FROM filings ORDER BY source_order, id")
        for filing in filings:
            counts["filings"] += 1
            for row in rows_for_filing(conn, filing):
                writer.writerow(row)
                counts["rows"] += 1
    conn.close()
    os.replace(temp_path, output_path)
    return counts


def main():
    parser = argparse.ArgumentParser(description="Exporta SQLite al esquema CSV compatible de 24 columnas")
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    counts = export_database(args.db, args.output)
    print(json.dumps({**counts, "output": os.path.abspath(args.output)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
