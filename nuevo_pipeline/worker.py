import argparse
import hashlib
import json
import logging
import os
import random
import sys
import time
from logging.handlers import RotatingFileHandler

if __package__ in (None, ""):
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, ROOT)

from nuevo_pipeline.constants import PRIORITY_ORDER, TARGET_KEYS
from nuevo_pipeline.database import (
    connect, event, get_metadata, initialize, set_metadata, summary, transaction,
)
from nuevo_pipeline.dobnow_client import BlockedError, DOBNowClient, RequestError


LOG = logging.getLogger("dobnow_pipeline")


class RequestPacer:
    def __init__(self, minimum, maximum, risk_fn=None, think_probability=0.05):
        self.minimum = minimum
        self.maximum = maximum
        self.risk_fn = risk_fn or (lambda: 0)
        self.think_probability = think_probability
        self.next_at = 0.0

    def wait(self):
        delay = self.next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        risk = 0
        try:
            risk = int(self.risk_fn())
        except Exception:
            risk = 0
        factor = {0: 1.0, 1: 2.0, 2: 4.0}.get(risk, 1.0)
        base = random.uniform(self.minimum, self.maximum) * factor
        if self.think_probability > 0 and random.random() < self.think_probability:
            base += random.uniform(20, 60)
        self.next_at = time.monotonic() + base


def now():
    return time.time()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value, default=None):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def configure_logging(path, verbose=False):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(
        path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    LOG.addHandler(file_handler)
    LOG.addHandler(console_handler)


def close_logging():
    for handler in list(LOG.handlers):
        handler.flush()
        handler.close()
        LOG.removeHandler(handler)


def marker_path(args):
    return args.needs_session_file or os.path.join(
        os.path.dirname(os.path.abspath(args.db)), "NEEDS_SESSION"
    )


def clear_session_block(conn, marker):
    with transaction(conn):
        set_metadata(conn, {
            "consecutive_blocks": 0,
            "session_state": "HEALTHY",
            "last_session_recovery_at": now(),
        })
    if os.path.exists(marker):
        os.remove(marker)


def record_session_block(conn, args, message):
    count = int(get_metadata(conn, "consecutive_blocks", 0) or 0) + 1
    needs_session = count >= args.block_threshold
    state = "NEEDS_SESSION" if needs_session else "COOLDOWN"
    with transaction(conn):
        set_metadata(conn, {
            "consecutive_blocks": count,
            "session_state": state,
            "last_block_at": now(),
            "last_block_message": str(message),
        })
        event(conn, "BLOCK", "session", state, str(message))
    if needs_session:
        payload = {
            "state": state,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "consecutive_blocks": count,
            "message": str(message),
            "database": os.path.abspath(args.db),
            "profile": os.path.abspath(args.profile),
            "progress_saved": True,
        }
        temp = marker_path(args) + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp, marker_path(args))
    return count, needs_session


def mark_task_success(conn):
    if int(get_metadata(conn, "consecutive_blocks", 0) or 0):
        with transaction(conn):
            set_metadata(conn, {"consecutive_blocks": 0, "session_state": "HEALTHY"})


def priority_clause(priorities, alias=""):
    prefix = f"{alias}." if alias else ""
    placeholders = ",".join("?" for _ in priorities)
    return f"{prefix}priority IN ({placeholders})"


def claim_bin(conn, priorities):
    with transaction(conn, immediate=True):
        row = conn.execute(f"""
            SELECT * FROM bins
            WHERE status IN ('pending', 'retry')
              AND next_attempt_at <= ?
              AND {priority_clause(priorities)}
            ORDER BY CASE priority WHEN 'A' THEN 0 WHEN 'B' THEN 1 ELSE 2 END,
                     source_order
            LIMIT 1
        """, (now(), *priorities)).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE bins SET status='running', attempts=attempts+1, updated_at=? WHERE bin=?",
            (now(), row["bin"]),
        )
        return dict(row)


def ready_filing_count(conn, priorities):
    return conn.execute(f"""
        SELECT COUNT(*) FROM filings f
        WHERE f.guid IS NOT NULL
          AND f.search_status='done'
          AND (f.pw1_status!='done' OR f.zd1wd_status!='done' OR f.portal_status!='done' OR f.normalized=0)
          AND f.next_attempt_at <= ?
          AND {priority_clause(priorities, 'f')}
    """, (now(), *priorities)).fetchone()[0]


def claim_filing(conn, priorities):
    with transaction(conn, immediate=True):
        row = conn.execute(f"""
            SELECT f.*, b.street_name, b.borough AS bin_borough
            FROM filings f JOIN bins b ON b.bin=f.bin
            WHERE f.guid IS NOT NULL
              AND f.search_status='done'
              AND (f.pw1_status!='done' OR f.zd1wd_status!='done' OR f.portal_status!='done' OR f.normalized=0)
              AND f.next_attempt_at <= ?
              AND {priority_clause(priorities, 'f')}
            ORDER BY CASE f.priority WHEN 'A' THEN 0 WHEN 'B' THEN 1 ELSE 2 END,
                     b.last_filing_at, f.source_order
            LIMIT 1
        """, (now(), *priorities)).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE filings SET attempts=attempts+1, updated_at=? WHERE id=?",
            (now(), row["id"]),
        )
        conn.execute("UPDATE bins SET last_filing_at=? WHERE bin=?", (now(), row["bin"]))
        return dict(row)


def claim_phase_filing(conn, priorities, phase):
    if phase == "zoning":
        pending = "f.zd1wd_status!='done'"
    elif phase == "portal":
        pending = "(f.pw1_status!='done' OR f.portal_status!='done')"
    else:
        raise ValueError(f"Fase de filing invalida: {phase}")
    with transaction(conn, immediate=True):
        row = conn.execute(f"""
            SELECT f.*, b.street_name, b.borough AS bin_borough
            FROM filings f JOIN bins b ON b.bin=f.bin
            WHERE f.guid IS NOT NULL
              AND f.search_status='done'
              AND {pending}
              AND f.next_attempt_at <= ?
              AND {priority_clause(priorities, 'f')}
            ORDER BY CASE f.priority WHEN 'A' THEN 0 WHEN 'B' THEN 1 ELSE 2 END,
                     b.last_filing_at, f.source_order
            LIMIT 1
        """, (now(), *priorities)).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE filings SET attempts=attempts+1, updated_at=? WHERE id=?",
            (now(), row["id"]),
        )
        conn.execute("UPDATE bins SET last_filing_at=? WHERE bin=?", (now(), row["bin"]))
        return dict(row)


def claim_download(conn, priorities):
    with transaction(conn, immediate=True):
        row = conn.execute(f"""
            SELECT d.*, f.bin, f.priority, f.input_json
            FROM documents d JOIN filings f ON f.id=d.filing_id
            WHERE d.download_status IN ('pending', 'retry')
              AND d.next_attempt_at <= ?
              AND {priority_clause(priorities, 'f')}
            ORDER BY CASE f.priority WHEN 'A' THEN 0 WHEN 'B' THEN 1 ELSE 2 END, d.id
            LIMIT 1
        """, (now(), *priorities)).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE documents SET download_status='running', attempts=attempts+1, updated_at=? WHERE id=?",
            (now(), row["id"]),
        )
        return dict(row)


def http_search(client, bin_num, street, pacer):
    pacer.wait()
    return client.search_bin(bin_num, street)


def resolve_bin(conn, item, client, pacer, retry_delay):
    streets = loads(item["street_variants_json"], []) or [item["street_name"]]
    try:
        filing_rows = conn.execute(
            "SELECT id, job_filing_number FROM filings WHERE bin=?",
            (item["bin"],),
        ).fetchall()
        targets = {row["job_filing_number"] for row in filing_rows}
        by_filing = {}
        responses = []
        for street in streets:
            found_jobs, response = http_search(client, item["bin"], street, pacer)
            responses.append(response)
            for job in found_jobs:
                number = str(job.get("JobNumber_FilingNumber") or "").strip()
                if number:
                    by_filing[number] = job
            if targets.issubset(by_filing):
                break
        with transaction(conn):
            for filing in filing_rows:
                job = by_filing.get(filing["job_filing_number"])
                if job:
                    conn.execute("""
                        UPDATE filings
                        SET guid=?, job_json=?, search_status='done', last_error='', updated_at=?
                        WHERE id=?
                    """, (str(job.get("BuildID") or ""), dumps(job), now(), filing["id"]))
                else:
                    conn.execute("""
                        UPDATE filings SET search_status='job_not_found', last_error='JOB_NOT_FOUND', updated_at=?
                        WHERE id=?
                    """, (now(), filing["id"]))
            conn.execute("""
                UPDATE bins SET status='done', response_json=?, last_error='', next_attempt_at=0, updated_at=?
                WHERE bin=?
            """, (dumps({"responses": responses, "jobs": list(by_filing.values())}), now(), item["bin"]))
            event(conn, "INFO", "bin", item["bin"], f"resolved jobs={len(by_filing)}")
        LOG.info("BIN %s resuelto; jobs=%s", item["bin"], len(by_filing))
        return True
    except BlockedError as exc:
        with transaction(conn):
            conn.execute(
                "UPDATE bins SET status='retry', next_attempt_at=?, last_error=?, updated_at=? WHERE bin=?",
                (now() + retry_delay, str(exc), now(), item["bin"]),
            )
            event(conn, "BLOCK", "bin", item["bin"], str(exc))
        raise
    except Exception as exc:
        with transaction(conn):
            conn.execute(
                "UPDATE bins SET status='retry', next_attempt_at=?, last_error=?, updated_at=? WHERE bin=?",
                (now() + retry_delay, str(exc), now(), item["bin"]),
            )
            event(conn, "ERROR", "bin", item["bin"], str(exc))
        LOG.error("BIN %s pasa a retry: %s", item["bin"], exc)
        return False


def fetch_pw1(client, guid, pacer):
    pacer.wait()
    return client.get_pw1(guid)


def fetch_zd1wd(client, guid, pacer):
    pacer.wait()
    return client.get_zd1wd(guid)


def fetch_portal(client, guid, pw1, pacer):
    pacer.wait()
    return client.get_portal_documents(guid, pw1)


def document_key(document):
    url = str(document.get("DocumentURL") or "").strip()
    if url:
        return "url:" + url
    fallback = "|".join(str(document.get(name) or "").strip() for name in (
        "Name", "DocumentTypeGUID", "DocumentTypeName", "CreateOn", "UploadedDate"
    ))
    return "meta:" + hashlib.sha256(fallback.encode("utf-8")).hexdigest()


def is_target(document):
    value = " ".join(str(document.get(name) or "") for name in ("Name", "DocumentTypeName")).upper()
    return any(key in value for key in TARGET_KEYS)


def normalize_documents(conn, filing):
    zd_docs = loads(filing.get("zd1wd_json"), []) or []
    portal_docs = loads(filing.get("portal_json"), []) or []
    combined = {}
    for source, documents in (("zd1wd", zd_docs), ("portal", portal_docs)):
        for document in documents:
            if not isinstance(document, dict):
                continue
            key = document_key(document)
            entry = combined.setdefault(key, {"document": document, "sources": [], "variants": []})
            if source not in entry["sources"]:
                entry["sources"].append(source)
            entry["variants"].append(document)
            if source == "portal":
                entry["document"] = document
    with transaction(conn):
        for key, entry in combined.items():
            document = entry["document"]
            matched = int(is_target(document))
            conn.execute("""
                INSERT INTO documents(
                    filing_id, document_key, document_url, name, description, category,
                    type_name, status_label, create_on, sources_json, variants_json,
                    matched, download_status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(filing_id, document_key) DO UPDATE SET
                    sources_json=excluded.sources_json,
                    variants_json=excluded.variants_json,
                    matched=excluded.matched,
                    updated_at=excluded.updated_at
            """, (
                filing["id"], key, str(document.get("DocumentURL") or ""),
                str(document.get("Name") or ""), str(document.get("Name") or ""),
                str(document.get("DocumentCategory") or ""),
                str(document.get("DocumentTypeName") or ""),
                str(document.get("RequiredItemStatusLabel") or ""),
                str(document.get("CreateOn") or ""), dumps(entry["sources"]),
                dumps(entry["variants"]), matched,
                "pending" if matched and document.get("DocumentURL") else "skipped", now(),
            ))
        conn.execute("UPDATE filings SET normalized=1, last_error='', next_attempt_at=0, updated_at=? WHERE id=?", (now(), filing["id"]))
        event(conn, "INFO", "filing", filing["id"], f"normalized documents={len(combined)}")


def process_filing(conn, filing, client, pacer, retry_delay):
    current_endpoint = ""
    try:
        pw1 = loads(filing.get("pw1_json"), {})
        if filing["pw1_status"] != "done":
            current_endpoint = "pw1_status"
            pw1 = fetch_pw1(client, filing["guid"], pacer)
            with transaction(conn):
                conn.execute("UPDATE filings SET pw1_status='done', pw1_json=?, last_error='', updated_at=? WHERE id=?", (dumps(pw1), now(), filing["id"]))
            filing["pw1_status"] = "done"
            filing["pw1_json"] = dumps(pw1)
        if filing["zd1wd_status"] != "done":
            current_endpoint = "zd1wd_status"
            docs = fetch_zd1wd(client, filing["guid"], pacer)
            with transaction(conn):
                conn.execute("UPDATE filings SET zd1wd_status='done', zd1wd_json=?, last_error='', updated_at=? WHERE id=?", (dumps(docs), now(), filing["id"]))
            filing["zd1wd_status"] = "done"
            filing["zd1wd_json"] = dumps(docs)
        if filing["portal_status"] != "done":
            current_endpoint = "portal_status"
            docs = fetch_portal(client, filing["guid"], pw1, pacer)
            with transaction(conn):
                conn.execute("UPDATE filings SET portal_status='done', portal_json=?, last_error='', updated_at=? WHERE id=?", (dumps(docs), now(), filing["id"]))
            filing["portal_status"] = "done"
            filing["portal_json"] = dumps(docs)
        if not filing["normalized"]:
            current_endpoint = ""
            normalize_documents(conn, filing)
        LOG.info("Filing %s: metadata completada", filing["job_filing_number"])
        return True
    except BlockedError as exc:
        with transaction(conn):
            if current_endpoint:
                conn.execute(f"UPDATE filings SET {current_endpoint}='retry', next_attempt_at=?, last_error=?, updated_at=? WHERE id=?", (now() + retry_delay, str(exc), now(), filing["id"]))
            event(conn, "BLOCK", "filing", filing["id"], str(exc))
        raise
    except Exception as exc:
        with transaction(conn):
            if current_endpoint:
                conn.execute(f"UPDATE filings SET {current_endpoint}='retry', next_attempt_at=?, last_error=?, updated_at=? WHERE id=?", (now() + retry_delay, str(exc), now(), filing["id"]))
            event(conn, "ERROR", "filing", filing["id"], str(exc))
        LOG.error("Filing %s pasa a retry: %s", filing["job_filing_number"], exc)
        return False


def process_zoning(conn, filing, client, pacer, retry_delay):
    try:
        docs = fetch_zd1wd(client, filing["guid"], pacer)
        with transaction(conn):
            conn.execute("""
                UPDATE filings SET zd1wd_status='done', zd1wd_json=?, normalized=0,
                    last_error='', next_attempt_at=0, updated_at=? WHERE id=?
            """, (dumps(docs), now(), filing["id"]))
        filing["zd1wd_status"] = "done"
        filing["zd1wd_json"] = dumps(docs)
        filing["normalized"] = 0
        normalize_documents(conn, filing)
        LOG.info("Filing %s: zoning completado; documentos=%s", filing["job_filing_number"], len(docs))
        return True
    except BlockedError as exc:
        with transaction(conn):
            conn.execute("UPDATE filings SET zd1wd_status='retry', next_attempt_at=?, last_error=?, updated_at=? WHERE id=?", (now() + retry_delay, str(exc), now(), filing["id"]))
            event(conn, "BLOCK", "filing", filing["id"], str(exc))
        raise
    except Exception as exc:
        with transaction(conn):
            conn.execute("UPDATE filings SET zd1wd_status='retry', next_attempt_at=?, last_error=?, updated_at=? WHERE id=?", (now() + retry_delay, str(exc), now(), filing["id"]))
            event(conn, "ERROR", "filing", filing["id"], str(exc))
        LOG.error("Filing %s zoning pasa a retry: %s", filing["job_filing_number"], exc)
        return False


def process_portal_phase(conn, filing, client, pacer, retry_delay):
    current_endpoint = ""
    try:
        pw1 = loads(filing.get("pw1_json"), {})
        if filing["pw1_status"] != "done":
            current_endpoint = "pw1_status"
            pw1 = fetch_pw1(client, filing["guid"], pacer)
            with transaction(conn):
                conn.execute("UPDATE filings SET pw1_status='done', pw1_json=?, last_error='', updated_at=? WHERE id=?", (dumps(pw1), now(), filing["id"]))
            filing["pw1_status"] = "done"
            filing["pw1_json"] = dumps(pw1)
        if filing["portal_status"] != "done":
            current_endpoint = "portal_status"
            docs = fetch_portal(client, filing["guid"], pw1, pacer)
            with transaction(conn):
                conn.execute("""
                    UPDATE filings SET portal_status='done', portal_json=?, normalized=0,
                        last_error='', next_attempt_at=0, updated_at=? WHERE id=?
                """, (dumps(docs), now(), filing["id"]))
            filing["portal_status"] = "done"
            filing["portal_json"] = dumps(docs)
            filing["normalized"] = 0
        normalize_documents(conn, filing)
        LOG.info("Filing %s: portal completado", filing["job_filing_number"])
        return True
    except BlockedError as exc:
        with transaction(conn):
            if current_endpoint:
                conn.execute(f"UPDATE filings SET {current_endpoint}='retry', next_attempt_at=?, last_error=?, updated_at=? WHERE id=?", (now() + retry_delay, str(exc), now(), filing["id"]))
            event(conn, "BLOCK", "filing", filing["id"], str(exc))
        raise
    except Exception as exc:
        with transaction(conn):
            if current_endpoint:
                conn.execute(f"UPDATE filings SET {current_endpoint}='retry', next_attempt_at=?, last_error=?, updated_at=? WHERE id=?", (now() + retry_delay, str(exc), now(), filing["id"]))
            event(conn, "ERROR", "filing", filing["id"], str(exc))
        LOG.error("Filing %s portal pasa a retry: %s", filing["job_filing_number"], exc)
        return False


def process_download(conn, document, client, pacer, retry_delay):
    try:
        input_row = loads(document["input_json"], {}) or {}
        borough = input_row.get("Borough") or ""
        pacer.wait()
        download_url = client.get_download_url(document["document_url"], borough)
        with transaction(conn):
            conn.execute("""
                UPDATE documents SET download_status='done', download_url=?, last_error='',
                    next_attempt_at=0, updated_at=? WHERE id=?
            """, (download_url, now(), document["id"]))
            event(conn, "INFO", "document", document["id"], "download URL resolved")
        LOG.info("Documento %s: URL de descarga resuelta", document["id"])
        return True
    except BlockedError as exc:
        with transaction(conn):
            conn.execute("UPDATE documents SET download_status='retry', next_attempt_at=?, last_error=?, updated_at=? WHERE id=?", (now() + retry_delay, str(exc), now(), document["id"]))
            event(conn, "BLOCK", "document", document["id"], str(exc))
        raise
    except Exception as exc:
        with transaction(conn):
            conn.execute("UPDATE documents SET download_status='retry', next_attempt_at=?, last_error=?, updated_at=? WHERE id=?", (now() + retry_delay, str(exc), now(), document["id"]))
            event(conn, "ERROR", "document", document["id"], str(exc))
        LOG.error("Documento %s pasa a retry: %s", document["id"], exc)
        return False


def has_any_work(conn, priorities):
    bin_count = conn.execute(f"SELECT COUNT(*) FROM bins WHERE status IN ('pending','retry','running') AND {priority_clause(priorities)}", priorities).fetchone()[0]
    filing_count = conn.execute(f"""
        SELECT COUNT(*) FROM filings f WHERE f.search_status='done'
        AND (f.pw1_status!='done' OR f.zd1wd_status!='done' OR f.portal_status!='done' OR f.normalized=0)
        AND {priority_clause(priorities, 'f')}
    """, priorities).fetchone()[0]
    download_count = conn.execute(f"""
        SELECT COUNT(*) FROM documents d JOIN filings f ON f.id=d.filing_id
        WHERE d.download_status IN ('pending','retry','running') AND {priority_clause(priorities, 'f')}
    """, priorities).fetchone()[0]
    return bool(bin_count or filing_count or download_count)


def has_phase_work(conn, priorities, phase):
    if phase == "all":
        return has_any_work(conn, priorities)
    if phase == "bins":
        sql = f"SELECT COUNT(*) FROM bins WHERE status IN ('pending','retry','running') AND {priority_clause(priorities)}"
        return bool(conn.execute(sql, priorities).fetchone()[0])
    if phase in ("zoning", "portal"):
        pending = "f.zd1wd_status!='done'" if phase == "zoning" else "(f.pw1_status!='done' OR f.portal_status!='done')"
        sql = f"""
            SELECT COUNT(*) FROM filings f WHERE f.guid IS NOT NULL
              AND f.search_status='done' AND {pending}
              AND {priority_clause(priorities, 'f')}
        """
        return bool(conn.execute(sql, priorities).fetchone()[0])
    sql = f"""
        SELECT COUNT(*) FROM documents d JOIN filings f ON f.id=d.filing_id
        WHERE d.download_status IN ('pending','retry','running')
          AND {priority_clause(priorities, 'f')}
    """
    return bool(conn.execute(sql, priorities).fetchone()[0])


def recover_in_progress(conn):
    """Devuelve a retry tareas que quedaron running por un cierre abrupto."""
    with transaction(conn):
        conn.execute("""
            UPDATE bins SET status='retry', next_attempt_at=0,
                last_error=CASE WHEN last_error='' THEN 'RECOVERED_AFTER_RESTART' ELSE last_error END
            WHERE status='running'
        """)
        conn.execute("""
            UPDATE documents SET download_status='retry', next_attempt_at=0,
                last_error=CASE WHEN last_error='' THEN 'RECOVERED_AFTER_RESTART' ELSE last_error END
            WHERE download_status='running'
        """)


def main():
    parser = argparse.ArgumentParser(description="Worker reanudable DOB NOW con SQLite")
    parser.add_argument("--db", required=True)
    parser.add_argument("--priorities", default="A", help="A, B o A,B")
    parser.add_argument("--profile", default="chrome_profile", help="Perfil Chrome local")
    parser.add_argument("--cdp-port", type=int, default=0, help="Conectar a Chrome existente")
    parser.add_argument("--pause-min", type=float, default=6.0)
    parser.add_argument("--pause-max", type=float, default=15.0)
    parser.add_argument("--think-probability", type=float, default=0.05, help="Probabilidad de pausa de pensamiento larga (0 = desactivar)")
    parser.add_argument("--retry-delay", type=int, default=900, help="Segundos antes de reintentar error normal")
    parser.add_argument("--resolve-ahead", type=int, default=50, help="Filings con GUID que se mantienen en cola")
    parser.add_argument("--download-every", type=int, default=4, help="Intentar una descarga cada N tareas")
    parser.add_argument("--max-tasks", type=int, default=0, help="0 = sin limite")
    parser.add_argument(
        "--phase", choices=("all", "bins", "zoning", "portal", "downloads"),
        default="all", help="Limitar el worker a una fase; all conserva el flujo combinado",
    )
    parser.add_argument("--status", action="store_true", help="Mostrar estado y salir")
    parser.add_argument("--check-session", action="store_true", help="Validar sesion, limpiar NEEDS_SESSION y salir")
    parser.add_argument("--block-threshold", type=int, default=3, help="Bloqueos consecutivos antes de NEEDS_SESSION")
    parser.add_argument("--needs-session-file", default=None, help="Ruta opcional del marcador NEEDS_SESSION")
    parser.add_argument("--log-file", default=None, help="Log rotativo; default junto a la base")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    priorities = tuple(dict.fromkeys(item.strip().upper() for item in args.priorities.split(",") if item.strip()))
    if not priorities or set(priorities) - set(PRIORITY_ORDER):
        parser.error("Prioridades invalidas")
    if args.pause_min < 0 or args.pause_max < args.pause_min:
        parser.error("Pausas invalidas")
    if args.think_probability < 0 or args.think_probability > 1:
        parser.error("--think-probability debe estar entre 0 y 1")

    log_file = args.log_file or os.path.splitext(os.path.abspath(args.db))[0] + ".log"
    configure_logging(log_file, args.verbose)
    LOG.info("Inicio worker db=%s prioridades=%s fase=%s", os.path.abspath(args.db), ",".join(priorities), args.phase)

    conn = connect(args.db)
    initialize(conn)
    if args.status:
        print(json.dumps(summary(conn), ensure_ascii=False, indent=2))
        conn.close()
        return 0
    if args.download_every < 1:
        parser.error("--download-every debe ser >= 1")
    if args.block_threshold < 1:
        parser.error("--block-threshold debe ser >= 1")
    marker = marker_path(args)
    if os.path.exists(marker) and not args.check_session:
        LOG.error("Existe %s. Recupera y valida la sesion con --check-session.", marker)
        conn.close()
        return 4
    recover_in_progress(conn)

    client = DOBNowClient(args.profile, args.cdp_port, LOG)
    tasks = 0
    try:
        client.open()
        if args.check_session:
            client.assert_healthy(require_angular=True)
            clear_session_block(conn, marker)
            LOG.info("Sesion validada. Marcador NEEDS_SESSION eliminado.")
            return 0
        pacer = RequestPacer(
            args.pause_min, args.pause_max,
            risk_fn=client.risk_level, think_probability=args.think_probability,
        )
        while not args.max_tasks or tasks < args.max_tasks:
            task_done = False
            if args.phase == "bins":
                item = claim_bin(conn, priorities)
                if item:
                    resolve_bin(conn, item, client, pacer, args.retry_delay)
                    tasks += 1
                    task_done = True
            elif args.phase == "zoning":
                filing = claim_phase_filing(conn, priorities, "zoning")
                if filing:
                    process_zoning(conn, filing, client, pacer, args.retry_delay)
                    tasks += 1
                    task_done = True
            elif args.phase == "portal":
                filing = claim_phase_filing(conn, priorities, "portal")
                if filing:
                    process_portal_phase(conn, filing, client, pacer, args.retry_delay)
                    tasks += 1
                    task_done = True
            elif args.phase == "downloads":
                document = claim_download(conn, priorities)
                if document:
                    process_download(conn, document, client, pacer, args.retry_delay)
                    tasks += 1
                    task_done = True
            elif tasks > 0 and tasks % args.download_every == 0:
                document = claim_download(conn, priorities)
                if document:
                    process_download(conn, document, client, pacer, args.retry_delay)
                    tasks += 1
                    task_done = True
            if args.phase == "all" and ready_filing_count(conn, priorities) < args.resolve_ahead:
                item = None if task_done else claim_bin(conn, priorities)
                if item:
                    resolve_bin(conn, item, client, pacer, args.retry_delay)
                    tasks += 1
                    task_done = True
            if args.phase == "all" and not task_done:
                filing = claim_filing(conn, priorities)
                if filing:
                    process_filing(conn, filing, client, pacer, args.retry_delay)
                    tasks += 1
                    task_done = True
            if args.phase == "all" and not task_done:
                document = claim_download(conn, priorities)
                if document:
                    process_download(conn, document, client, pacer, args.retry_delay)
                    tasks += 1
                    task_done = True
            if task_done:
                mark_task_success(conn)
            if not task_done:
                if has_phase_work(conn, priorities, args.phase):
                    next_times = [row[0] for row in conn.execute("""
                        SELECT next_attempt_at FROM bins WHERE status='retry' AND next_attempt_at>?
                        UNION ALL SELECT next_attempt_at FROM filings WHERE next_attempt_at>?
                        UNION ALL SELECT next_attempt_at FROM documents WHERE download_status='retry' AND next_attempt_at>?
                    """, (now(), now(), now())).fetchall()]
                    if next_times:
                        wait = max(1, min(60, min(next_times) - now()))
                        LOG.info("Sin tareas elegibles. Esperando %.0fs", wait)
                        time.sleep(wait)
                        continue
                LOG.info("No quedan tareas elegibles para fase=%s y prioridades solicitadas", args.phase)
                break
        LOG.info("Resumen final: %s", json.dumps(summary(conn), ensure_ascii=False))
        return 0
    except BlockedError as exc:
        count, needs_session = record_session_block(conn, args, exc)
        LOG.error(
            "Sesion bloqueada; progreso guardado; consecutivos=%s estado=%s: %s",
            count, "NEEDS_SESSION" if needs_session else "COOLDOWN", exc,
        )
        return 4 if needs_session else 3
    except KeyboardInterrupt:
        LOG.warning("Interrumpido por usuario; estado guardado")
        return 130
    except Exception as exc:
        LOG.exception("Error fatal: %s", exc)
        return 1
    finally:
        client.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
