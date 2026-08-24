import argparse
import csv
import hashlib
import json
import os
import sys
from collections import OrderedDict

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nuevo_pipeline.constants import PRIORITY_ORDER
from nuevo_pipeline.database import connect, initialize, set_metadata, transaction


INPUT_FIELDS = [
    "Job Filing Number", "Filing Status", "Filing Date", "House No",
    "Street Name", "Borough", "Block", "LOT", "Bin", "Job Description",
    "Filing Review Type", "Job Type", "Building Type", "Existing Stories",
    "Existing Height", "Existing Dwelling Units", "Proposed No of Stories",
    "Proposed Height", "Proposed Dwelling Units",
]


def text(value):
    return (value or "").strip()


def number(value):
    try:
        return float(text(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def increased(row, proposed, existing):
    p = number(row.get(proposed))
    e = number(row.get(existing))
    return p is not None and e is not None and p > e


def classify_priority(row):
    job_type = text(row.get("Job Type"))
    description = text(row.get("Job Description")).lower()
    description_signal = any(word in description for word in (
        "enlarg", "addition", "add floor", "add story", "vertical", "horizontal"
    ))
    numeric_signal = any((
        increased(row, "Proposed No of Stories", "Existing Stories"),
        increased(row, "Proposed Height", "Existing Height"),
        increased(row, "Proposed Dwelling Units", "Existing Dwelling Units"),
    ))
    if (
        job_type in {
            "New Building",
            "ALT-CO - New Building with Existing Elements to Remain",
        }
        or description_signal
        or numeric_signal
    ):
        return "A"
    if job_type == "Alteration CO":
        return "B"
    return "C"


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_partition(value):
    try:
        part, total = (int(item) for item in value.split("/", 1))
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError("Usa formato N/TOTAL, por ejemplo 1/2")
    if total < 1 or part < 1 or part > total:
        raise argparse.ArgumentTypeError("La particion debe cumplir 1 <= N <= TOTAL")
    return part, total


def choose_contiguous_partitions(groups, total_parts):
    if not groups:
        raise ValueError("No hay BIN seleccionados para las prioridades solicitadas")
    if len(groups) < total_parts:
        raise ValueError("Hay menos BIN seleccionados que particiones solicitadas")
    weights = [1 + 3 * len(group["filings"]) for group in groups]
    total_weight = sum(weights)
    prefix = []
    current = 0
    for weight in weights:
        current += weight
        prefix.append(current)
    boundaries = [0]
    last = 0
    for part in range(1, total_parts):
        target = total_weight * part / total_parts
        candidates = range(last + 1, len(groups) - (total_parts - part) + 1)
        boundary = min(candidates, key=lambda index: abs(prefix[index - 1] - target))
        boundaries.append(boundary)
        last = boundary
    boundaries.append(len(groups))
    assignments = []
    for part in range(total_parts):
        assignments.extend([part + 1] * (boundaries[part + 1] - boundaries[part]))
    return assignments, weights, total_weight


def read_selected(path, allowed_priorities):
    bins = OrderedDict()
    seen_pairs = set()
    duplicate_pairs = 0
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"Bin", "Job Filing Number", "Job Type", "Job Description"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Faltan columnas obligatorias: {sorted(missing)}")
        for source_order, row in enumerate(reader):
            bin_num = text(row.get("Bin"))
            filing = text(row.get("Job Filing Number"))
            if not bin_num or not filing:
                continue
            priority = classify_priority(row)
            if priority not in allowed_priorities:
                continue
            pair = (bin_num, filing)
            if pair in seen_pairs:
                duplicate_pairs += 1
                continue
            seen_pairs.add(pair)
            compact = {field: text(row.get(field)) for field in INPUT_FIELDS}
            group = bins.setdefault(bin_num, {
                "bin": bin_num,
                "source_order": source_order,
                "priority": priority,
                "house_no": text(row.get("House No")),
                "street_name": text(row.get("Street Name")),
                "borough": text(row.get("Borough")),
                "block": text(row.get("Block")),
                "lot": text(row.get("LOT")),
                "streets": [],
                "filings": [],
            })
            street = text(row.get("Street Name"))
            if street and street not in group["streets"]:
                group["streets"].append(street)
            if PRIORITY_ORDER[priority] < PRIORITY_ORDER[group["priority"]]:
                group["priority"] = priority
            group["filings"].append({
                "job_filing_number": filing,
                "source_order": source_order,
                "priority": priority,
                "input": compact,
            })
    return list(bins.values()), duplicate_pairs


def prepare(args):
    part, total_parts = parse_partition(args.partition)
    priorities = tuple(dict.fromkeys(item.strip().upper() for item in args.priorities.split(",") if item.strip()))
    invalid = set(priorities) - set(PRIORITY_ORDER)
    if invalid:
        raise ValueError(f"Prioridades invalidas: {sorted(invalid)}")
    if os.path.exists(args.db) and not args.force:
        raise FileExistsError(f"La base ya existe: {args.db}. Usa --force para reemplazarla.")
    groups, duplicates = read_selected(args.input, set(priorities))
    assignments, weights, total_weight = choose_contiguous_partitions(groups, total_parts)
    selected = [group for group, assigned in zip(groups, assignments) if assigned == part]
    selected_weight = sum(weight for weight, assigned in zip(weights, assignments) if assigned == part)

    if os.path.exists(args.db):
        os.remove(args.db)
    for suffix in ("-wal", "-shm"):
        sidecar = args.db + suffix
        if os.path.exists(sidecar):
            os.remove(sidecar)
    conn = connect(args.db)
    initialize(conn)
    with transaction(conn):
        set_metadata(conn, {
            "input_path": os.path.abspath(args.input),
            "input_sha256": file_sha256(args.input),
            "partition": args.partition,
            "priorities": priorities,
            "selected_bins": len(selected),
            "selected_filings": sum(len(group["filings"]) for group in selected),
            "selected_weight": selected_weight,
            "all_selected_bins": len(groups),
            "all_selected_weight": total_weight,
            "duplicate_pairs_ignored": duplicates,
        })
        for group in selected:
            conn.execute("""
                INSERT INTO bins(
                    bin, source_order, partition_no, priority, house_no, street_name,
                    borough, block, lot, street_variants_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (
                group["bin"], group["source_order"], part, group["priority"],
                group["house_no"], group["street_name"], group["borough"],
                group["block"], group["lot"], json.dumps(group["streets"], ensure_ascii=False),
            ))
            for filing in group["filings"]:
                conn.execute("""
                    INSERT INTO filings(
                        bin, job_filing_number, source_order, priority, input_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 0)
                """, (
                    group["bin"], filing["job_filing_number"], filing["source_order"],
                    filing["priority"], json.dumps(filing["input"], ensure_ascii=False),
                ))
    conn.close()
    return {
        "partition": args.partition,
        "priorities": ",".join(priorities),
        "bins": len(selected),
        "filings": sum(len(group["filings"]) for group in selected),
        "estimated_weight": selected_weight,
        "all_bins": len(groups),
        "all_estimated_weight": total_weight,
        "duplicates_ignored": duplicates,
        "database": os.path.abspath(args.db),
    }


def main():
    parser = argparse.ArgumentParser(description="Clasifica, agrupa y divide el CSV original por BIN completo")
    parser.add_argument("--input", required=True, help="CSV original sin agrupar")
    parser.add_argument("--partition", required=True, help="Particion N/TOTAL, por ejemplo 1/2")
    parser.add_argument("--priorities", default="A,B", help="Prioridades a cargar: A, A,B o A,B,C")
    parser.add_argument("--db", required=True, help="SQLite local que se creara")
    parser.add_argument("--force", action="store_true", help="Reemplaza una base existente")
    args = parser.parse_args()
    try:
        result = prepare(args)
    except Exception as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
