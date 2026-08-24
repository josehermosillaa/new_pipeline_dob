import argparse
import csv
import json
import logging
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from nuevo_pipeline.constants import OUTPUT_COLUMNS
from nuevo_pipeline.database import connect, get_metadata, initialize
from nuevo_pipeline.export_results import export_database
from nuevo_pipeline.merge_results import merge
from nuevo_pipeline.prepare_inputs import classify_priority, prepare
from nuevo_pipeline.worker import (
    clear_session_block, close_logging, configure_logging, record_session_block,
)


FIELDS = [
    "Job Filing Number", "Filing Status", "Filing Date", "House No", "Street Name",
    "Borough", "Block", "LOT", "Bin", "Job Description", "Filing Review Type",
    "Job Type", "Building Type", "Existing Stories", "Existing Height",
    "Existing Dwelling Units", "Proposed No of Stories", "Proposed Height",
    "Proposed Dwelling Units",
]


def row(bin_num, filing, job_type="Alteration", description="", existing="1", proposed="1"):
    result = {field: "" for field in FIELDS}
    result.update({
        "Bin": bin_num,
        "Job Filing Number": filing,
        "Job Type": job_type,
        "Job Description": description,
        "Street Name": "MAIN ST",
        "Borough": "Queens",
        "Existing Stories": existing,
        "Proposed No of Stories": proposed,
    })
    return result


class PipelineTests(unittest.TestCase):
    def test_priority_rules(self):
        self.assertEqual(classify_priority(row("1", "A", "New Building")), "A")
        self.assertEqual(classify_priority(row("1", "B", "Alteration CO")), "B")
        self.assertEqual(classify_priority(row("1", "C", proposed="2")), "A")
        self.assertEqual(classify_priority(row("1", "D", description="horizontal enlargement")), "A")
        self.assertEqual(classify_priority(row("1", "E")), "C")

    def test_partitions_do_not_share_bins_and_export_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            input_path = os.path.join(temp, "input.csv")
            rows = [
                row("100", "Q1-I1", "New Building"),
                row("100", "Q2-I1", "Alteration CO"),
                row("200", "Q3-I1", "Alteration CO"),
                row("300", "Q4-I1", description="vertical addition"),
                row("400", "Q5-I1", "Alteration CO"),
                row("400", "Q6-I1", "Alteration CO"),
            ]
            with open(input_path, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(rows)

            databases = []
            bin_sets = []
            for part in (1, 2):
                db = os.path.join(temp, f"state{part}.sqlite")
                databases.append(db)
                prepare(argparse.Namespace(
                    input=input_path,
                    partition=f"{part}/2",
                    priorities="A,B",
                    db=db,
                    force=False,
                ))
                conn = connect(db)
                bin_sets.append({item[0] for item in conn.execute("SELECT bin FROM bins")})
                conn.close()
            self.assertFalse(bin_sets[0] & bin_sets[1])
            self.assertEqual(bin_sets[0] | bin_sets[1], {"100", "200", "300", "400"})

            conn = connect(databases[0])
            filing = conn.execute("SELECT * FROM filings LIMIT 1").fetchone()
            conn.execute("""
                UPDATE filings SET search_status='done', guid='guid-test',
                    job_json=?, pw1_status='done', pw1_json=?, zd1wd_status='done',
                    zd1wd_json='[]', portal_status='done', portal_json='[]', normalized=1
                WHERE id=?
            """, (
                json.dumps({"JobNumber_FilingNumber": filing["job_filing_number"], "Bin": filing["bin"]}),
                json.dumps({"CurrentFilingStatusValue": "6"}),
                filing["id"],
            ))
            conn.commit()
            conn.close()
            output = os.path.join(temp, "result.csv")
            export_database(databases[0], output)
            with open(output, encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames, OUTPUT_COLUMNS)
                exported = list(reader)
            self.assertTrue(exported)

            merged = os.path.join(temp, "merged.csv")
            stats = merge([output, output], merged)
            self.assertGreater(stats["exact_duplicates_removed"], 0)
            with open(merged, encoding="utf-8", newline="") as handle:
                self.assertEqual(csv.DictReader(handle).fieldnames, OUTPUT_COLUMNS)

    def test_needs_session_marker_and_log(self):
        with tempfile.TemporaryDirectory() as temp:
            db = os.path.join(temp, "state.sqlite")
            marker = os.path.join(temp, "NEEDS_SESSION")
            log = os.path.join(temp, "worker.log")
            conn = connect(db)
            initialize(conn)
            args = argparse.Namespace(
                block_threshold=2,
                needs_session_file=marker,
                db=db,
                profile=os.path.join(temp, "profile"),
            )
            count, needs_session = record_session_block(conn, args, "Access Denied")
            self.assertEqual((count, needs_session), (1, False))
            self.assertFalse(os.path.exists(marker))
            count, needs_session = record_session_block(conn, args, "Access Denied")
            self.assertEqual((count, needs_session), (2, True))
            self.assertTrue(os.path.exists(marker))
            configure_logging(log)
            logging.getLogger("dobnow_pipeline").info("test log")
            self.assertTrue(os.path.exists(log))
            close_logging()
            clear_session_block(conn, marker)
            self.assertFalse(os.path.exists(marker))
            self.assertEqual(get_metadata(conn, "session_state"), "HEALTHY")
            conn.close()


if __name__ == "__main__":
    unittest.main()
