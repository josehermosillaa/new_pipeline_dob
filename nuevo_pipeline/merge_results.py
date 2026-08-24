import argparse
import csv
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nuevo_pipeline.constants import OUTPUT_COLUMNS


def merge(inputs, output):
    seen_rows = set()
    filing_sources = {}
    overlaps = set()
    temp = output + ".tmp"
    written = 0
    duplicates = 0
    with open(temp, "w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for source_index, path in enumerate(inputs, 1):
            with open(path, encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != OUTPUT_COLUMNS:
                    raise ValueError(f"Encabezado incompatible en {path}")
                for row in reader:
                    filing = (row.get("Job Filing Number") or "").strip()
                    if filing:
                        previous = filing_sources.setdefault(filing, source_index)
                        if previous != source_index:
                            overlaps.add(filing)
                    key = tuple(row.get(column, "") for column in OUTPUT_COLUMNS)
                    if key in seen_rows:
                        duplicates += 1
                        continue
                    seen_rows.add(key)
                    writer.writerow({column: row.get(column, "") for column in OUTPUT_COLUMNS})
                    written += 1
    os.replace(temp, output)
    return {
        "inputs": len(inputs),
        "rows_written": written,
        "exact_duplicates_removed": duplicates,
        "filings_in_multiple_inputs": len(overlaps),
        "overlap_examples": sorted(overlaps)[:20],
        "output": os.path.abspath(output),
    }


def main():
    parser = argparse.ArgumentParser(description="Consolida resultados de las PCs y valida las 24 columnas")
    parser.add_argument("--inputs", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = merge(args.inputs, args.output)
    except Exception as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

