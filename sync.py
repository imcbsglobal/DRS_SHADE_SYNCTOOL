"""
HMS Sync Tool  -  sync.py
Source : SQL Anywhere (HMS)  ->  Target : SQLite
Tables : hms_doctors, hms_doctorstiming, misel
"""

import json
import logging
import sys
import time
import argparse
import sqlite3
import decimal
from datetime import datetime, date, timezone
from pathlib import Path

try:
    import pyodbc
except ImportError:
    print("ERROR: pyodbc not installed. Run: pip install pyodbc")
    sys.exit(1)

CONFIG_FILE = Path(__file__).parent / "config.json"


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def setup_logging(cfg):
    sc    = cfg.get("sync", {})
    level = getattr(logging, sc.get("log_level", "INFO").upper(), logging.INFO)
    fmt   = "%(asctime)s [%(levelname)s] %(message)s"
    handlers = [logging.StreamHandler(sys.stdout)]
    lf = sc.get("log_file")
    if lf:
        handlers.append(logging.FileHandler(lf, encoding="utf-8"))
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)
    return logging.getLogger("hms_sync")


# ─────────────────────────────────────────────────────────────
#  Source DB connection — tries multiple methods
# ─────────────────────────────────────────────────────────────

def get_source_connection(cfg, logger):
    src      = cfg["source_db"]
    drv      = src.get("driver", "SQL Anywhere 17")
    database = src.get("database", "medimall")
    uid      = src.get("uid", "DBA")
    pwd      = src.get("pwd", "")
    port     = src.get("port", 2638)
    server   = src.get("server", "").strip()

    attempts = []

    # Method 1: ServerName from config
    if server:
        attempts.append((
            f"ServerName={server}",
            f"DRIVER={{{drv}}};ServerName={server};DatabaseName={database};UID={uid};PWD={pwd};"
        ))

    # Method 2: DatabaseName as ServerName (most common SA local setup)
    attempts.append((
        f"ServerName={database}",
        f"DRIVER={{{drv}}};ServerName={database};DatabaseName={database};UID={uid};PWD={pwd};"
    ))

    # Method 3: localhost TCP
    attempts.append((
        f"Host=localhost:{port}",
        f"DRIVER={{{drv}}};Host=localhost:{port};DatabaseName={database};UID={uid};PWD={pwd};"
    ))

    # Method 4: 127.0.0.1 TCP
    attempts.append((
        f"Host=127.0.0.1:{port}",
        f"DRIVER={{{drv}}};Host=127.0.0.1:{port};DatabaseName={database};UID={uid};PWD={pwd};"
    ))

    last_error = None
    for label, conn_str in attempts:
        logger.info(f"Trying {label} ...")
        try:
            conn = pyodbc.connect(conn_str, autocommit=True, timeout=5)
            logger.info(f"Connected via {label}!")
            return conn
        except Exception as e:
            logger.warning(f"  Failed: {str(e)[:80]}")
            last_error = e

    raise ConnectionError(f"All connection attempts failed. Last error: {last_error}")


def rows_to_dicts(cursor):
    columns = [col[0].lower() for col in cursor.description]
    result  = []
    for row in cursor.fetchall():
        d = {}
        for k, v in zip(columns, row):
            if isinstance(v, decimal.Decimal):
                v = float(v)
            elif isinstance(v, (date, datetime)):
                v = v.isoformat()
            elif isinstance(v, str):
                v = v.strip()
            d[k] = v
        result.append(d)
    return result


def fetch_doctors(conn, logger):
    logger.info("Fetching hms_doctors ...")
    cur = conn.cursor()
    cur.execute("""
        SELECT code, name, rate, department, avgcontime, qualification
        FROM DBA.hms_doctors
    """)
    rows = rows_to_dicts(cur)
    logger.info(f"  -> {len(rows)} doctor(s) fetched")
    return rows


def fetch_timings(conn, logger):
    logger.info("Fetching hms_doctorstiming ...")
    cur = conn.cursor()
    cur.execute("""
        SELECT slno, code, t1, t2
        FROM DBA.hms_doctorstiming
    """)
    rows = rows_to_dicts(cur)
    logger.info(f"  -> {len(rows)} timing record(s) fetched")
    return rows


def fetch_misel(conn, logger):
    logger.info("Fetching misel (hospital info) ...")
    cur = conn.cursor()
    cur.execute("""
        SELECT TOP 1 firm_name, address1
        FROM DBA.misel
    """)
    rows = rows_to_dicts(cur)
    row  = rows[0] if rows else {}
    logger.info(f"  -> Hospital: {row.get('firm_name', '(empty)')}")
    return row

def fetch_department(conn, logger):
    logger.info("Fetching hms_department ...")
    cur = conn.cursor()
    cur.execute("""
        SELECT code, name
        FROM DBA.hms_department
    """)
    rows = rows_to_dicts(cur)
    logger.info(f"  -> {len(rows)} department(s) fetched")
    return rows



# ─────────────────────────────────────────────────────────────
#  SQLite target
# ─────────────────────────────────────────────────────────────

def get_sqlite_connection(cfg):
    db_path = Path(cfg["target_db"].get("path", "db.sqlite3"))
    conn    = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_tables(conn, logger):
    logger.info("Ensuring SQLite tables exist ...")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sync_doctors (
            code TEXT PRIMARY KEY, name TEXT, rate REAL,
            department TEXT, avgcontime INTEGER,
            qualification TEXT, synced_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sync_doctorstiming (
            slno INTEGER PRIMARY KEY, code TEXT,
            t1 REAL, t2 REAL, synced_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sync_misel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            firm_name TEXT, address1 TEXT, synced_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sync_department (
            code TEXT PRIMARY KEY,
            name TEXT,
            synced_at TEXT
        )
    """)
    conn.commit()
    logger.info("  -> Tables ready")


def upsert_doctors(conn, doctors, logger):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    for d in doctors:
        cur.execute("""
            INSERT INTO sync_doctors
                (code, name, rate, department, avgcontime, qualification, synced_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name, rate=excluded.rate,
                department=excluded.department, avgcontime=excluded.avgcontime,
                qualification=excluded.qualification, synced_at=excluded.synced_at
        """, (d.get("code"), d.get("name"), d.get("rate"), d.get("department"),
              d.get("avgcontime"), d.get("qualification"), now))
    conn.commit()
    logger.info(f"  -> {len(doctors)} doctor(s) saved to SQLite")


def upsert_timings(conn, timings, logger):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    for t in timings:
        cur.execute("""
            INSERT INTO sync_doctorstiming (slno, code, t1, t2, synced_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(slno) DO UPDATE SET
                code=excluded.code, t1=excluded.t1,
                t2=excluded.t2, synced_at=excluded.synced_at
        """, (t.get("slno"), t.get("code"), t.get("t1"), t.get("t2"), now))
    conn.commit()
    logger.info(f"  -> {len(timings)} timing(s) saved to SQLite")


def upsert_misel(conn, misel, logger):
    if not misel:
        logger.warning("No misel data found.")
        return
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    cur.execute("DELETE FROM sync_misel")
    cur.execute(
        "INSERT INTO sync_misel (firm_name, address1, synced_at) VALUES (?,?,?)",
        (misel.get("firm_name"), misel.get("address1"), now)
    )
    conn.commit()
    logger.info(f"  -> Hospital saved: {misel.get('firm_name')}")

def upsert_department(conn, departments, logger):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    for d in departments:
        cur.execute("""
            INSERT INTO sync_department (code, name, synced_at)
            VALUES (?,?,?)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name,
                synced_at=excluded.synced_at
        """, (d.get("code"), d.get("name"), now))
    conn.commit()
    logger.info(f"  -> {len(departments)} department(s) saved to SQLite")




# ─────────────────────────────────────────────────────────────
#  Verify
# ─────────────────────────────────────────────────────────────

def verify_sqlite(cfg, logger):
    logger.info("=" * 55)
    logger.info("VERIFY - SQLite DB contents")
    logger.info("=" * 55)
    conn = get_sqlite_connection(cfg)
    cur  = conn.cursor()
    for table, cols in [
        ("sync_doctors",       "code, name, department, rate"),
        ("sync_doctorstiming", "slno, code, t1, t2"),
        ("sync_misel",         "firm_name, address1, synced_at"),
        ("sync_department",    "code, name, synced_at"),
    ]:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            count = cur.fetchone()[0]
            logger.info(f"{table}: {count} record(s)")
            cur.execute(f"SELECT {cols} FROM {table} LIMIT 3")
            for row in cur.fetchall():
                logger.info(f"  {row}")
        except Exception as e:
            logger.warning(f"{table} error: {e}")
    conn.close()
    logger.info("=" * 55)


# ─────────────────────────────────────────────────────────────
#  Main sync cycle
# ─────────────────────────────────────────────────────────────

def run_sync(cfg, logger):
    logger.info("")
    logger.info("=" * 55)
    logger.info("HMS SYNC  |  SQL Anywhere  ->  SQLite")
    logger.info(f"Time      |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 55)

    # Connect
    try:
        src_conn = get_source_connection(cfg, logger)
    except Exception as e:
        logger.error(f"Connection FAILED: {e}")
        logger.error(f"Available ODBC drivers: {pyodbc.drivers()}")
        return False

    # Fetch
    doctors = timings = misel = departments = None
    try:
        doctors     = fetch_doctors(src_conn, logger)
        timings     = fetch_timings(src_conn, logger)
        misel       = fetch_misel(src_conn, logger)
        departments = fetch_department(src_conn, logger)
    except Exception as e:
        logger.error(f"Fetch FAILED: {e}")
        return False
    finally:
        try:
            src_conn.close()
        except Exception:
            pass

    # Write to SQLite
    try:
        sqlite_conn = get_sqlite_connection(cfg)
        ensure_tables(sqlite_conn, logger)
        upsert_doctors(sqlite_conn, doctors, logger)
        upsert_timings(sqlite_conn, timings, logger)
        upsert_misel(sqlite_conn, misel, logger)
        upsert_department(sqlite_conn, departments, logger)
        sqlite_conn.close()
    except Exception as e:
        logger.error(f"SQLite write FAILED: {e}")
        return False

    logger.info("")
    logger.info("SYNC COMPLETE!")
    logger.info(f"  Doctors : {len(doctors)}")
    logger.info(f"  Timings : {len(timings)}")
    logger.info(f"  Hospital    : {misel.get('firm_name', '?')}")
    logger.info(f"  Departments : {len(departments)}")
    return True


def main():
    global CONFIG_FILE

    parser = argparse.ArgumentParser(description="HMS Sync: SQL Anywhere -> SQLite")
    parser.add_argument("--watch",  action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--config", default=str(CONFIG_FILE))
    args = parser.parse_args()

    CONFIG_FILE = Path(args.config)
    cfg    = load_config()
    logger = setup_logging(cfg)

    if args.verify:
        verify_sqlite(cfg, logger)
        return

    if args.watch:
        interval = cfg["sync"].get("interval_seconds", 300)
        logger.info(f"Watch mode - syncing every {interval}s. Ctrl+C to stop.")
        while True:
            run_sync(cfg, logger)
            logger.info(f"Next sync in {interval}s ...")
            time.sleep(interval)
    else:
        success = run_sync(cfg, logger)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()