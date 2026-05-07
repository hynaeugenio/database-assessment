#!/usr/bin/env python3
"""
Oracle to PostgreSQL Migration Assessment Tool
Version: 1.0.0

Connects to an Oracle database (DBA or user mode) and produces:
  - HTML report  (human-readable, standalone)
  - JSON export  (machine-readable full data)
  - CSV summary  (object/type inventory)

Requirements:
    pip install oracledb

Usage:
    python oracle_assessment.py \\
        --host myhost --port 1521 --service ORCL \\
        --user assessuser --password secret \\
        [--schemas HR,SALES] \\
        [--output-dir ./reports] \\
        [--mode dba|user]
"""

import argparse
import csv
import datetime
import html as _html
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import oracledb
except ImportError:
    print("ERROR: 'oracledb' not installed.  Run:  pip install oracledb")
    sys.exit(1)

# ============================================================
# CONSTANTS
# ============================================================

VERSION = "1.0.0"

SYSTEM_SCHEMAS = frozenset([
    "SYS", "SYSTEM", "DBSNMP", "SYSMAN", "OUTLN", "MDSYS", "ORDSYS",
    "EXFSYS", "DMSYS", "WMSYS", "CTXSYS", "ANONYMOUS", "XDB", "ORDPLUGINS",
    "ORDDATA", "LBACSYS", "APEX_PUBLIC_USER", "FLOWS_FILES", "XS$NULL",
    "SPATIAL_CSW_ADMIN_USR", "SPATIAL_WFS_ADMIN_USR", "OLAPSYS", "ORACLE_OCM",
    "MDDATA", "DIP", "OWBSYS", "OWBSYS_AUDIT", "MGMT_VIEW", "SYSKM",
    "SYSBACKUP", "SYSDG", "GSMADMIN_INTERNAL", "GSMUSER", "GSMCATUSER",
    "APPQOSSYS", "DBSFWUSER", "REMOTE_SCHEDULER_AGENT", "SYS$UMF",
    "OJVMSYS", "DVF", "DVSYS", "AUDSYS", "PDBADMIN", "PERFSTAT",
    "SQLTXADMIN", "RMAN$CATALOG",
])

# effort: LOW / MEDIUM / HIGH
DATA_TYPE_MAP = {
    "VARCHAR2":                     ("VARCHAR/TEXT",            "LOW",    "Direct mapping"),
    "NVARCHAR2":                    ("VARCHAR/TEXT",            "LOW",    "Unicode is native in PostgreSQL"),
    "CHAR":                         ("CHAR/VARCHAR",            "LOW",    "Direct mapping"),
    "NCHAR":                        ("CHAR/VARCHAR",            "LOW",    "Unicode is native in PostgreSQL"),
    "INTEGER":                      ("INTEGER",                 "LOW",    "Direct mapping"),
    "FLOAT":                        ("DOUBLE PRECISION",        "LOW",    "Direct mapping"),
    "BINARY_FLOAT":                 ("REAL",                    "LOW",    "Direct mapping"),
    "BINARY_DOUBLE":                ("DOUBLE PRECISION",        "LOW",    "Direct mapping"),
    "CLOB":                         ("TEXT",                    "LOW",    "Direct mapping"),
    "NCLOB":                        ("TEXT",                    "LOW",    "Direct mapping"),
    "BLOB":                         ("BYTEA",                   "LOW",    "Direct mapping"),
    "BOOLEAN":                      ("BOOLEAN",                 "LOW",    "Direct mapping (Oracle 23c+)"),
    "TIMESTAMP":                    ("TIMESTAMP",               "LOW",    "Direct mapping"),
    "TIMESTAMP WITH TIME ZONE":     ("TIMESTAMPTZ",             "LOW",    "Direct mapping"),
    "NUMBER":                       ("NUMERIC/INTEGER",         "MEDIUM", "NUMBER(p,0) → INTEGER/BIGINT; NUMBER(p,s) → NUMERIC(p,s)"),
    "DATE":                         ("TIMESTAMP",               "MEDIUM", "Oracle DATE stores time; PostgreSQL DATE does not — use TIMESTAMP"),
    "TIMESTAMP WITH LOCAL TIME ZONE": ("TIMESTAMPTZ",          "MEDIUM", "Review session-timezone conversion behaviour"),
    "INTERVAL YEAR TO MONTH":       ("INTERVAL",                "MEDIUM", "PostgreSQL INTERVAL syntax differs slightly"),
    "INTERVAL DAY TO SECOND":       ("INTERVAL",                "MEDIUM", "PostgreSQL INTERVAL syntax differs slightly"),
    "RAW":                          ("BYTEA",                   "MEDIUM", "Hex-encoding differences; review client code"),
    "LONG RAW":                     ("BYTEA",                   "MEDIUM", "Deprecated Oracle type; migrate to BYTEA"),
    "LONG":                         ("TEXT",                    "MEDIUM", "Deprecated Oracle type; migrate to TEXT"),
    "XMLTYPE":                      ("XML or JSONB",            "HIGH",   "XML processing differs; consider converting to JSON"),
    "SDO_GEOMETRY":                 ("geometry (PostGIS)",      "HIGH",   "Requires PostGIS; spatial functions need rewrite"),
    "ROWID":                        ("N/A",                     "HIGH",   "No equivalent; replace with primary key or ctid"),
    "UROWID":                       ("N/A",                     "HIGH",   "No equivalent; replace with primary key"),
    "ANYTYPE":                      ("N/A",                     "HIGH",   "Oracle-specific; requires schema redesign"),
    "ANYDATA":                      ("N/A",                     "HIGH",   "Oracle-specific; requires schema redesign"),
    "ANYDATASET":                   ("N/A",                     "HIGH",   "Oracle-specific; requires schema redesign"),
}

# (regex, label, effort, recommendation)
PLSQL_PATTERNS = [
    (r"\bROWNUM\b",                       "ROWNUM pseudocolumn",              "MEDIUM", "Use LIMIT / FETCH FIRST n ROWS ONLY"),
    (r"\bCONNECT\s+BY\b",                 "CONNECT BY hierarchy",             "HIGH",   "Rewrite with WITH RECURSIVE CTE"),
    (r"\bSTART\s+WITH\b",                 "START WITH clause",                "HIGH",   "Part of CONNECT BY; rewrite as recursive CTE"),
    (r"\bLEVEL\b",                        "LEVEL pseudocolumn",               "HIGH",   "Used with CONNECT BY; track depth in recursive CTE"),
    (r"\bSYSDATE\b",                      "SYSDATE",                          "LOW",    "Replace with NOW() or CURRENT_TIMESTAMP"),
    (r"\bSYSTIMESTAMP\b",                 "SYSTIMESTAMP",                     "LOW",    "Replace with CURRENT_TIMESTAMP"),
    (r"\bDECODE\s*\(",                    "DECODE function",                  "LOW",    "Replace with CASE WHEN … END"),
    (r"\bNVL\s*\(",                       "NVL function",                     "LOW",    "Replace with COALESCE()"),
    (r"\bNVL2\s*\(",                      "NVL2 function",                    "LOW",    "CASE WHEN x IS NOT NULL THEN a ELSE b END"),
    (r"\bDUAL\b",                         "DUAL table",                       "LOW",    "Remove FROM DUAL (not needed in PostgreSQL)"),
    (r"\bMERGE\s+INTO\b",                 "MERGE statement",                  "MEDIUM", "Rewrite as INSERT … ON CONFLICT DO UPDATE"),
    (r"\bPRAGMA\s+AUTONOMOUS_TRANSACTION\b", "PRAGMA AUTONOMOUS_TRANSACTION", "HIGH",   "No direct equivalent; use dblink or a separate connection"),
    (r"\bBULK\s+COLLECT\b",               "BULK COLLECT",                     "MEDIUM", "Rewrite with standard loops or set-based operations"),
    (r"\bFORALL\b",                       "FORALL statement",                 "MEDIUM", "Rewrite with FOREACH or set-based INSERT/UPDATE"),
    (r"\bDBMS_\w+",                       "DBMS_* Oracle package",            "HIGH",   "No direct PG equivalent; find extension or rewrite"),
    (r"\bUTL_\w+",                        "UTL_* Oracle package",             "HIGH",   "No direct PG equivalent; find extension or rewrite"),
    (r"\bPIPE\s+ROW\b",                   "PIPE ROW (pipelined function)",    "MEDIUM", "Replace with RETURN NEXT"),
    (r"\bIS\s+(VARRAY|TABLE\s+OF)\b",     "Oracle collection type",           "HIGH",   "Use PostgreSQL arrays or separate normalised tables"),
    (r"\bEXECUTE\s+IMMEDIATE\b",          "EXECUTE IMMEDIATE",                "MEDIUM", "Replace with EXECUTE … in PL/pgSQL"),
    (r"\bKEEP\s*\(",                      "KEEP aggregate (first/last)",      "MEDIUM", "Rewrite with FIRST_VALUE/LAST_VALUE window functions"),
    (r"\bMODEL\b",                        "MODEL clause",                     "HIGH",   "No PostgreSQL equivalent; full rewrite required"),
    (r"\bFLASHBACK\b",                    "FLASHBACK query",                  "HIGH",   "No direct equivalent; use audit tables or temporal tables"),
    (r"\bSAMPLE\s*\(",                    "SAMPLE clause",                    "LOW",    "Replace with TABLESAMPLE SYSTEM(n) or BERNOULLI(n)"),
    (r"\bWM_CONCAT\b",                    "WM_CONCAT function",               "LOW",    "Replace with STRING_AGG()"),
    (r"\bLISTAGG\s*\(",                   "LISTAGG function",                 "LOW",    "Replace with STRING_AGG()"),
    (r"\bSYS_GUID\s*\(",                  "SYS_GUID()",                       "LOW",    "Replace with gen_random_uuid()"),
    (r"\b\w+\.NEXTVAL\b",                 "Sequence .NEXTVAL",                "LOW",    "Replace with NEXTVAL('seq_name')"),
    (r"\b\w+\.CURRVAL\b",                 "Sequence .CURRVAL",                "LOW",    "Replace with CURRVAL('seq_name')"),
    (r"\bCREATE\s+OR\s+REPLACE\s+PACKAGE\b", "Package definition",           "HIGH",   "Split into schema + individual functions/procedures"),
    (r"\bTO_DATE\s*\(",                   "TO_DATE function",                 "LOW",    "Replace with TO_TIMESTAMP() or CAST"),
    (r"\bTO_NUMBER\s*\(",                 "TO_NUMBER function",               "LOW",    "Replace with CAST(x AS NUMERIC) or x::NUMERIC"),
    (r"\bADD_MONTHS\s*\(",                "ADD_MONTHS function",              "LOW",    "Use date + INTERVAL 'n months'"),
    (r"\bMONTHS_BETWEEN\s*\(",            "MONTHS_BETWEEN function",          "LOW",    "Use EXTRACT or AGE() with date arithmetic"),
    (r"\bLAST_DAY\s*\(",                  "LAST_DAY function",                "LOW",    "DATE_TRUNC('month', d) + INTERVAL '1 month - 1 day'"),
    (r"\bINSTR\s*\(",                     "INSTR function",                   "LOW",    "Replace with STRPOS() or POSITION()"),
    (r"\bSUBSTR\s*\(",                    "SUBSTR function",                  "LOW",    "Replace with SUBSTRING()"),
    (r"\bTRUNC\s*\(",                     "TRUNC function",                   "LOW",    "DATE_TRUNC for dates; TRUNC for numbers (both exist in PG)"),
    (r"\bREGEXP_LIKE\s*\(",               "REGEXP_LIKE",                      "LOW",    "Replace with ~ operator: col ~ 'pattern'"),
    (r"\bSYS_CONNECT_BY_PATH\b",          "SYS_CONNECT_BY_PATH",              "HIGH",   "Rewrite with recursive CTE string concatenation"),
    (r"/\*\+",                            "Oracle optimizer hint",            "LOW",    "Remove or use pg_hint_plan extension"),
    (r"\bMATCH_RECOGNIZE\b",              "MATCH_RECOGNIZE",                  "HIGH",   "No PG equivalent; rewrite with window functions"),
    (r"\bXMLTYPE\b",                      "XMLTYPE usage in code",            "HIGH",   "Rewrite XML handling for PostgreSQL xml type"),
    (r"\bUTL_HTTP\b|\bHTTPS?_REQUEST\b",  "HTTP calls from database",        "HIGH",   "Move to application layer or use pg_net extension"),
    (r"\bROWIDTOCHAR\b|\bCHARTOROWID\b", "ROWID conversion functions",      "HIGH",   "ROWID has no PostgreSQL equivalent"),
    (r"\bTYPE\s+\w+\s+AS\s+OBJECT\b",    "Oracle OBJECT type",              "HIGH",   "Rewrite as composite types or relational tables"),
    (r"\bLNNVL\s*\(",                     "LNNVL function",                   "LOW",    "Use NOT (cond) OR cond IS NULL"),
    (r"\bNUMTODSINTERVAL\b|\bNUMTOYMINTERVAL\b", "NUMTO*INTERVAL functions","LOW",   "Use n * INTERVAL '1 day' / '1 month' arithmetic"),
]

EFFORT_SCORE = {"LOW": 1, "MEDIUM": 3, "HIGH": 9}

# ============================================================
# SQL QUERIES (DBA mode)
# ============================================================

SQL_DB_VERSION = "SELECT banner FROM v$version WHERE ROWNUM = 1"

# CON_NAME is only available on Oracle 12c+; the fallback uses the DB name
SQL_PDB_NAME          = "SELECT SYS_CONTEXT('USERENV','CON_NAME') AS pdb_name FROM dual"
SQL_PDB_NAME_FALLBACK = "SELECT name AS pdb_name FROM v$database"

# cdb column only exists on Oracle 12c+; use the simpler form for compatibility
SQL_DB_INFO = """
SELECT d.name            AS db_name,
       d.db_unique_name,
       d.platform_name,
       d.log_mode,
       p.value           AS character_set
FROM   v$database d,
       nls_database_parameters p
WHERE  p.parameter = 'NLS_CHARACTERSET'
"""

SQL_NLS = """
SELECT parameter, value
FROM   nls_database_parameters
WHERE  parameter IN (
    'NLS_CHARACTERSET','NLS_NCHAR_CHARACTERSET',
    'NLS_LANGUAGE','NLS_TERRITORY',
    'NLS_DATE_FORMAT','NLS_TIMESTAMP_FORMAT'
)
ORDER BY parameter
"""

SQL_SCHEMAS = """
SELECT username,
       account_status,
       TO_CHAR(created, 'YYYY-MM-DD') AS created,
       default_tablespace
FROM   dba_users
WHERE  {filter}
ORDER BY username
"""

SQL_OBJECT_SUMMARY = """
SELECT owner,
       object_type,
       COUNT(*)                                                    AS total,
       SUM(CASE WHEN status = 'INVALID' THEN 1 ELSE 0 END)        AS invalid_count
FROM   dba_objects
WHERE  {filter}
GROUP BY owner, object_type
ORDER BY owner, object_type
"""

SQL_DATA_TYPES = """
SELECT data_type,
       COUNT(*)                                                AS column_count,
       COUNT(DISTINCT owner || '.' || table_name)             AS table_count
FROM   dba_tab_columns
WHERE  {filter}
GROUP BY data_type
ORDER BY COUNT(*) DESC
"""

SQL_TABLE_COUNT = """
SELECT owner,
       COUNT(*)           AS table_count,
       SUM(num_rows)      AS approx_rows
FROM   dba_tables
WHERE  {filter}
GROUP BY owner
ORDER BY owner
"""

SQL_INDEX_TYPES = """
SELECT owner,
       index_type,
       COUNT(*) AS cnt
FROM   dba_indexes
WHERE  {filter}
GROUP BY owner, index_type
ORDER BY owner, index_type
"""

SQL_PARTITIONS = """
SELECT owner,
       table_name,
       partitioning_type,
       subpartitioning_type,
       partition_count
FROM   dba_part_tables
WHERE  {filter}
ORDER BY owner, table_name
"""

SQL_CONSTRAINTS = """
SELECT owner,
       constraint_type,
       COUNT(*) AS cnt
FROM   dba_constraints
WHERE  {filter}
  AND  constraint_type IN ('P','U','R','C')
GROUP BY owner, constraint_type
ORDER BY owner, constraint_type
"""

SQL_TRIGGERS = """
SELECT owner,
       trigger_type,
       triggering_event,
       COUNT(*) AS cnt
FROM   dba_triggers
WHERE  {filter}
GROUP BY owner, trigger_type, triggering_event
ORDER BY owner
"""

SQL_DB_LINKS = """
SELECT owner, db_link, username, host
FROM   dba_db_links
WHERE  {filter}
ORDER BY owner, db_link
"""

SQL_SEQUENCES = """
SELECT sequence_owner AS owner, COUNT(*) AS cnt
FROM   dba_sequences
WHERE  {filter_seq}
GROUP BY sequence_owner
ORDER BY sequence_owner
"""

SQL_MAT_VIEWS = """
SELECT owner, mview_name, refresh_method, refresh_mode
FROM   dba_mviews
WHERE  {filter}
ORDER BY owner, mview_name
"""

SQL_SCHEDULER_JOBS = """
SELECT owner, COUNT(*) AS cnt
FROM   dba_scheduler_jobs
WHERE  {filter}
GROUP BY owner
ORDER BY owner
"""

SQL_SOURCE = """
SELECT owner, name, type, text
FROM   dba_source
WHERE  {filter}
ORDER BY owner, name, type, line
"""

SQL_SEGMENT_SIZES = """
SELECT owner,
       segment_type,
       ROUND(SUM(bytes)/1024/1024, 2) AS size_mb
FROM   dba_segments
WHERE  {filter}
GROUP BY owner, segment_type
ORDER BY owner
"""

SQL_TOP_TABLES = """
SELECT owner, table_name, num_rows, blocks, size_mb, last_analyzed
FROM (
    SELECT t.owner,
           t.table_name,
           t.num_rows,
           t.blocks,
           ROUND(s.bytes / 1024 / 1024, 2)          AS size_mb,
           TO_CHAR(t.last_analyzed, 'YYYY-MM-DD')    AS last_analyzed
    FROM   dba_tables t
    LEFT JOIN dba_segments s
           ON s.owner        = t.owner
          AND s.segment_name = t.table_name
          AND s.segment_type = 'TABLE'
    WHERE  {filter}
      AND  t.num_rows IS NOT NULL
    ORDER BY t.num_rows DESC
)
WHERE ROWNUM <= 50
"""

SQL_LOB_TABLES = """
SELECT c.owner,
       c.table_name,
       c.column_name,
       c.data_type,
       l.segment_name                          AS lob_segment,
       ROUND(SUM(s.bytes) / 1024 / 1024, 2)   AS lob_size_mb
FROM   dba_tab_columns c
LEFT JOIN dba_lobs l
       ON  l.owner       = c.owner
      AND  l.table_name  = c.table_name
      AND  l.column_name = c.column_name
LEFT JOIN dba_segments s
       ON  s.owner        = l.owner
      AND  s.segment_name = l.segment_name
WHERE  c.data_type IN ('CLOB','NCLOB','BLOB','LONG','LONG RAW','XMLTYPE','RAW')
  AND  {filter}
GROUP BY c.owner, c.table_name, c.column_name, c.data_type, l.segment_name
ORDER BY c.owner, c.table_name, c.column_name
"""

SQL_PART_TABLE_SUMMARY = """
SELECT pt.owner,
       pt.table_name,
       pt.partitioning_type,
       pt.subpartitioning_type,
       pt.partition_count,
       SUM(tp.num_rows)                        AS total_rows,
       ROUND(SUM(s.bytes) / 1024 / 1024, 2)   AS total_size_mb
FROM   dba_part_tables pt
JOIN   dba_tab_partitions tp
       ON  tp.table_owner = pt.owner
      AND  tp.table_name  = pt.table_name
LEFT JOIN dba_segments s
       ON  s.owner          = tp.table_owner
      AND  s.segment_name   = tp.table_name
      AND  s.partition_name = tp.partition_name
      AND  s.segment_type   IN ('TABLE PARTITION','TABLE SUBPARTITION')
WHERE  {filter}
GROUP BY pt.owner, pt.table_name, pt.partitioning_type,
         pt.subpartitioning_type, pt.partition_count
ORDER BY SUM(s.bytes) DESC NULLS LAST, pt.owner, pt.table_name
"""

SQL_PART_TABLE_DETAIL = """
SELECT tp.table_owner                           AS owner,
       tp.table_name,
       tp.partition_name,
       tp.partition_position                    AS position,
       tp.num_rows,
       ROUND(s.bytes / 1024 / 1024, 2)         AS size_mb,
       TO_CHAR(tp.last_analyzed, 'YYYY-MM-DD') AS last_analyzed
FROM   dba_tab_partitions tp
LEFT JOIN dba_segments s
       ON  s.owner          = tp.table_owner
      AND  s.segment_name   = tp.table_name
      AND  s.partition_name = tp.partition_name
      AND  s.segment_type   IN ('TABLE PARTITION','TABLE SUBPARTITION')
WHERE  {filter}
ORDER BY tp.table_owner, tp.table_name, tp.partition_position
"""

SQL_PART_INDEX_SUMMARY = """
SELECT pi.owner,
       pi.index_name,
       pi.table_name,
       pi.partitioning_type,
       pi.subpartitioning_type,
       pi.partition_count,
       SUM(ip.num_rows)                        AS total_rows,
       SUM(ip.leaf_blocks)                     AS total_leaf_blocks,
       ROUND(SUM(s.bytes) / 1024 / 1024, 2)   AS total_size_mb
FROM   dba_part_indexes pi
JOIN   dba_ind_partitions ip
       ON  ip.index_owner = pi.owner
      AND  ip.index_name  = pi.index_name
LEFT JOIN dba_segments s
       ON  s.owner          = ip.index_owner
      AND  s.segment_name   = ip.index_name
      AND  s.partition_name = ip.partition_name
      AND  s.segment_type   IN ('INDEX PARTITION','INDEX SUBPARTITION')
WHERE  {filter}
GROUP BY pi.owner, pi.index_name, pi.table_name,
         pi.partitioning_type, pi.subpartitioning_type, pi.partition_count
ORDER BY SUM(s.bytes) DESC NULLS LAST, pi.owner, pi.index_name
"""

SQL_PART_INDEX_DETAIL = """
SELECT ip.index_owner                           AS owner,
       ip.index_name,
       ip.partition_name,
       ip.partition_position                    AS position,
       ip.num_rows,
       ip.leaf_blocks,
       ip.blevel,
       ROUND(s.bytes / 1024 / 1024, 2)         AS size_mb,
       TO_CHAR(ip.last_analyzed, 'YYYY-MM-DD') AS last_analyzed
FROM   dba_ind_partitions ip
LEFT JOIN dba_segments s
       ON  s.owner          = ip.index_owner
      AND  s.segment_name   = ip.index_name
      AND  s.partition_name = ip.partition_name
      AND  s.segment_type   IN ('INDEX PARTITION','INDEX SUBPARTITION')
WHERE  {filter}
ORDER BY ip.index_owner, ip.index_name, ip.partition_position
"""

# ALL_ variants for non-DBA mode
SQL_ALL_OBJECT_SUMMARY = SQL_OBJECT_SUMMARY.replace("dba_objects", "all_objects")
SQL_ALL_DATA_TYPES     = SQL_DATA_TYPES.replace("dba_tab_columns", "all_tab_columns")
SQL_ALL_TABLE_COUNT    = SQL_TABLE_COUNT.replace("dba_tables", "all_tables")
SQL_ALL_INDEX_TYPES    = SQL_INDEX_TYPES.replace("dba_indexes", "all_indexes")
SQL_ALL_PARTITIONS     = SQL_PARTITIONS.replace("dba_part_tables", "all_part_tables")
SQL_ALL_CONSTRAINTS    = SQL_CONSTRAINTS.replace("dba_constraints", "all_constraints")
SQL_ALL_TRIGGERS       = SQL_TRIGGERS.replace("dba_triggers", "all_triggers")
SQL_ALL_DB_LINKS       = SQL_DB_LINKS.replace("dba_db_links", "all_db_links")
SQL_ALL_SEQUENCES      = SQL_SEQUENCES.replace("dba_sequences", "all_sequences")
SQL_ALL_MAT_VIEWS      = SQL_MAT_VIEWS.replace("dba_mviews", "all_mviews")
SQL_ALL_SOURCE         = SQL_SOURCE.replace("dba_source", "all_source")
SQL_ALL_TOP_TABLES     = (SQL_TOP_TABLES
                          .replace("dba_tables", "all_tables")
                          .replace("dba_segments", "user_segments")
                          .replace("s.owner = t.owner\n      AND ", ""))
SQL_ALL_LOB_TABLES     = (SQL_LOB_TABLES
                          .replace("dba_tab_columns", "all_tab_columns")
                          .replace("dba_lobs", "all_lobs")
                          .replace("dba_segments", "user_segments")
                          .replace("s.owner        = l.owner\n      AND  ", ""))

# ============================================================
# COLLECTOR
# ============================================================

class OracleAssessor:
    """Connects to Oracle and runs all assessment queries."""

    def __init__(self, conn, schemas=None, mode="dba"):
        self.conn            = conn
        self.target_schemas  = [s.upper() for s in schemas] if schemas else []
        self.mode            = mode  # "dba" or "user"
        self.errors          = []

    # ----------------------------------------------------------
    # helpers
    # ----------------------------------------------------------

    # Labels in this set are expected fallbacks — errors are swallowed silently
    _SILENT_LABELS = {"pdb_name", "pdb_name_fallback"}

    def _run(self, sql, label="query"):
        """Execute SQL and return list-of-dicts rows; records errors unless label is silent."""
        try:
            cur = self.conn.cursor()
            cur.execute(sql)
            cols = [d[0].lower() for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.close()
            return rows
        except Exception as exc:
            if label not in self._SILENT_LABELS:
                self.errors.append(f"{label}: {exc}")
            return []

    def _schema_filter(self, col="owner", alias=""):
        """Build a WHERE fragment that excludes system schemas."""
        prefix = f"{alias}." if alias else ""
        sys_list = ", ".join(f"'{s}'" for s in sorted(SYSTEM_SCHEMAS))
        parts = [
            f"{prefix}{col} NOT IN ({sys_list})",
            f"{prefix}{col} NOT LIKE 'APEX_%'",
            f"{prefix}{col} NOT LIKE 'FLOWS_%'",
        ]
        if self.target_schemas:
            in_list = ", ".join(f"'{s}'" for s in self.target_schemas)
            parts.append(f"{prefix}{col} IN ({in_list})")
        return " AND ".join(parts)

    def _pick(self, dba_sql, all_sql, label):
        return self._run(dba_sql if self.mode == "dba" else all_sql, label)

    # ----------------------------------------------------------
    # individual collectors
    # ----------------------------------------------------------

    def db_version(self):
        rows = self._run(SQL_DB_VERSION, "db_version")
        return rows[0]["banner"] if rows else "Unknown"

    def db_info(self):
        rows = self._run(SQL_DB_INFO, "db_info")
        return rows[0] if rows else {}

    def pdb_name(self):
        rows = self._run(SQL_PDB_NAME, "pdb_name")
        if rows and rows[0].get("pdb_name"):
            return rows[0]["pdb_name"]
        # Fallback for pre-12c where CON_NAME USERENV param does not exist
        rows = self._run(SQL_PDB_NAME_FALLBACK, "pdb_name_fallback")
        return rows[0]["pdb_name"] if rows else "N/A"

    def nls_params(self):
        return {r["parameter"]: r["value"] for r in self._run(SQL_NLS, "nls")}

    def schemas(self):
        if self.mode != "dba":
            return []
        flt = self._schema_filter("username")
        return self._run(SQL_SCHEMAS.format(filter=flt), "schemas")

    def object_summary(self):
        flt = self._schema_filter()
        dba = SQL_OBJECT_SUMMARY.format(filter=flt)
        all_ = SQL_ALL_OBJECT_SUMMARY.format(filter=flt)
        return self._pick(dba, all_, "object_summary")

    def data_types(self):
        flt = self._schema_filter()
        dba = SQL_DATA_TYPES.format(filter=flt)
        all_ = SQL_ALL_DATA_TYPES.format(filter=flt)
        return self._pick(dba, all_, "data_types")

    def table_counts(self):
        flt = self._schema_filter()
        dba = SQL_TABLE_COUNT.format(filter=flt)
        all_ = SQL_ALL_TABLE_COUNT.format(filter=flt)
        return self._pick(dba, all_, "table_counts")

    def index_types(self):
        flt = self._schema_filter()
        dba = SQL_INDEX_TYPES.format(filter=flt)
        all_ = SQL_ALL_INDEX_TYPES.format(filter=flt)
        return self._pick(dba, all_, "index_types")

    def partitions(self):
        flt = self._schema_filter()
        dba = SQL_PARTITIONS.format(filter=flt)
        all_ = SQL_ALL_PARTITIONS.format(filter=flt)
        return self._pick(dba, all_, "partitions")

    def constraints(self):
        flt = self._schema_filter()
        dba = SQL_CONSTRAINTS.format(filter=flt)
        all_ = SQL_ALL_CONSTRAINTS.format(filter=flt)
        return self._pick(dba, all_, "constraints")

    def triggers(self):
        flt = self._schema_filter()
        dba = SQL_TRIGGERS.format(filter=flt)
        all_ = SQL_ALL_TRIGGERS.format(filter=flt)
        return self._pick(dba, all_, "triggers")

    def db_links(self):
        flt = self._schema_filter()
        dba = SQL_DB_LINKS.format(filter=flt)
        all_ = SQL_ALL_DB_LINKS.format(filter=flt)
        return self._pick(dba, all_, "db_links")

    def sequences(self):
        flt_seq = self._schema_filter("sequence_owner")
        dba  = SQL_SEQUENCES.format(filter_seq=flt_seq)
        all_ = SQL_ALL_SEQUENCES.format(filter_seq=flt_seq)
        return self._pick(dba, all_, "sequences")

    def mat_views(self):
        flt = self._schema_filter()
        dba  = SQL_MAT_VIEWS.format(filter=flt)
        all_ = SQL_ALL_MAT_VIEWS.format(filter=flt)
        return self._pick(dba, all_, "mat_views")

    def scheduler_jobs(self):
        if self.mode != "dba":
            return []
        flt = self._schema_filter()
        return self._run(SQL_SCHEDULER_JOBS.format(filter=flt), "scheduler_jobs")

    def plsql_source(self):
        flt = self._schema_filter()
        dba  = SQL_SOURCE.format(filter=flt)
        all_ = SQL_ALL_SOURCE.format(filter=flt)
        return self._pick(dba, all_, "plsql_source")

    def segment_sizes(self):
        if self.mode != "dba":
            return []
        flt = self._schema_filter()
        return self._run(SQL_SEGMENT_SIZES.format(filter=flt), "segment_sizes")

    def top_tables(self):
        flt  = self._schema_filter(col="owner", alias="t")
        dba  = SQL_TOP_TABLES.format(filter=flt)
        all_ = SQL_ALL_TOP_TABLES.format(filter=flt)
        return self._pick(dba, all_, "top_tables")

    def lob_tables(self):
        flt  = self._schema_filter("c.owner")
        dba  = SQL_LOB_TABLES.format(filter=flt)
        all_ = SQL_ALL_LOB_TABLES.format(filter=flt)
        return self._pick(dba, all_, "lob_tables")

    def part_table_summary(self):
        if self.mode != "dba":
            return []
        flt = self._schema_filter(col="owner", alias="pt")
        return self._run(SQL_PART_TABLE_SUMMARY.format(filter=flt), "part_table_summary")

    def part_table_detail(self):
        if self.mode != "dba":
            return []
        flt = self._schema_filter(col="table_owner", alias="tp")
        return self._run(SQL_PART_TABLE_DETAIL.format(filter=flt), "part_table_detail")

    def part_index_summary(self):
        if self.mode != "dba":
            return []
        flt = self._schema_filter(col="owner", alias="pi")
        return self._run(SQL_PART_INDEX_SUMMARY.format(filter=flt), "part_index_summary")

    def part_index_detail(self):
        if self.mode != "dba":
            return []
        flt = self._schema_filter(col="index_owner", alias="ip")
        return self._run(SQL_PART_INDEX_DETAIL.format(filter=flt), "part_index_detail")

    # ----------------------------------------------------------
    # collect everything
    # ----------------------------------------------------------

    def collect_all(self):
        print("  Collecting database info …")
        data = {
            "version":       self.db_version(),
            "db_info":       self.db_info(),
            "pdb_name":      self.pdb_name(),
            "nls":           self.nls_params(),
            "schemas":       self.schemas(),
        }
        print("  Collecting object inventory …")
        data["objects"]        = self.object_summary()
        data["data_types"]     = self.data_types()
        data["table_counts"]   = self.table_counts()
        print("  Collecting indexes, partitions, constraints …")
        data["index_types"]    = self.index_types()
        data["partitions"]     = self.partitions()
        data["constraints"]    = self.constraints()
        data["triggers"]       = self.triggers()
        print("  Collecting links, sequences, jobs …")
        data["db_links"]       = self.db_links()
        data["sequences"]      = self.sequences()
        data["mat_views"]      = self.mat_views()
        data["scheduler_jobs"] = self.scheduler_jobs()
        print("  Collecting segment sizes …")
        data["segment_sizes"]  = self.segment_sizes()
        print("  Collecting top tables and LOB columns …")
        data["top_tables"]          = self.top_tables()
        data["lob_tables"]          = self.lob_tables()
        print("  Collecting partition details …")
        data["part_table_summary"]  = self.part_table_summary()
        data["part_table_detail"]   = self.part_table_detail()
        data["part_index_summary"]  = self.part_index_summary()
        data["part_index_detail"]   = self.part_index_detail()
        print("  Analysing PL/SQL source (this may take a moment) …")
        data["plsql_source"]   = self.plsql_source()
        data["errors"]         = self.errors
        return data


# ============================================================
# ANALYSER
# ============================================================

def analyse(data):
    """
    Examine collected data and return a findings dict with:
      - object counts per type
      - data-type compatibility issues
      - PL/SQL pattern hits
      - migration complexity score
      - recommendations
    """
    findings = {
        "object_totals":      Counter(),
        "invalid_objects":    Counter(),
        "type_compat":        [],   # {data_type, pg_type, effort, notes, column_count, table_count}
        "plsql_hits":         [],   # {label, effort, recommendation, hit_count, objects}
        "index_issues":       [],
        "partition_count":    0,
        "db_link_count":      0,
        "mat_view_count":     0,
        "scheduler_job_count": 0,
        "total_score":        0,
        "complexity":         "LOW",
        "recommendations":    [],
    }

    # --- object totals ---
    for row in data.get("objects", []):
        obj_type = row.get("object_type", "UNKNOWN")
        findings["object_totals"][obj_type]    += int(row.get("total", 0) or 0)
        findings["invalid_objects"][obj_type]  += int(row.get("invalid_count", 0) or 0)

    # --- data type compatibility ---
    type_effort_score = 0
    for row in data.get("data_types", []):
        dtype = (row.get("data_type") or "").upper()
        col_count = int(row.get("column_count", 0) or 0)
        tbl_count = int(row.get("table_count", 0) or 0)
        mapping = DATA_TYPE_MAP.get(dtype, None)
        if mapping:
            pg_type, effort, notes = mapping
        else:
            pg_type, effort, notes = "Review required", "MEDIUM", "Unknown Oracle type — manual review needed"
        findings["type_compat"].append({
            "data_type":    dtype,
            "pg_type":      pg_type,
            "effort":       effort,
            "notes":        notes,
            "column_count": col_count,
            "table_count":  tbl_count,
        })
        type_effort_score += EFFORT_SCORE[effort] * col_count

    # --- PL/SQL pattern analysis ---
    source_lines = data.get("plsql_source", [])
    # group lines back into per-object source text
    obj_source = defaultdict(list)
    for row in source_lines:
        key = f"{row.get('owner','')}.{row.get('name','')}.{row.get('type','')}"
        obj_source[key].append(row.get("text") or "")

    # compile patterns once
    compiled = [(re.compile(pat, re.IGNORECASE), label, effort, rec)
                for pat, label, effort, rec in PLSQL_PATTERNS]

    pattern_hits = defaultdict(lambda: {"hit_count": 0, "objects": set()})
    for obj_key, lines in obj_source.items():
        full_text = " ".join(lines)
        for (regex, label, effort, rec) in compiled:
            if regex.search(full_text):
                pattern_hits[label]["hit_count"]  += 1
                pattern_hits[label]["effort"]      = effort
                pattern_hits[label]["recommendation"] = rec
                pattern_hits[label]["objects"].add(obj_key)

    plsql_score = 0
    for label, info in sorted(pattern_hits.items(), key=lambda x: -EFFORT_SCORE[x[1]["effort"]]):
        findings["plsql_hits"].append({
            "label":          label,
            "effort":         info["effort"],
            "recommendation": info["recommendation"],
            "hit_count":      info["hit_count"],
            "object_count":   len(info["objects"]),
        })
        plsql_score += EFFORT_SCORE[info["effort"]] * info["hit_count"]

    # --- index issues ---
    bitmap_count = 0
    func_idx_count = 0
    for row in data.get("index_types", []):
        idx_type = (row.get("index_type") or "").upper()
        cnt      = int(row.get("cnt", 0) or 0)
        if "BITMAP" in idx_type:
            bitmap_count += cnt
        if "FUNCTION-BASED" in idx_type:
            func_idx_count += cnt
    if bitmap_count:
        findings["index_issues"].append({
            "issue": f"Bitmap indexes ({bitmap_count})",
            "effort": "HIGH",
            "notes": "Bitmap indexes are not supported in PostgreSQL — use regular B-tree or partial indexes."
        })
    if func_idx_count:
        findings["index_issues"].append({
            "issue": f"Function-based indexes ({func_idx_count})",
            "effort": "MEDIUM",
            "notes": "PostgreSQL supports expression indexes — syntax rewrite required."
        })

    # --- misc counts ---
    findings["partition_count"]      = len(data.get("partitions", []))
    findings["db_link_count"]        = len(data.get("db_links", []))
    findings["mat_view_count"]       = len(data.get("mat_views", []))
    findings["scheduler_job_count"]  = sum(int(r.get("cnt", 0) or 0) for r in data.get("scheduler_jobs", []))

    if findings["db_link_count"]:
        findings["index_issues"].append({
            "issue": f"Database links ({findings['db_link_count']})",
            "effort": "HIGH",
            "notes": "DB links have no direct PostgreSQL equivalent — use Foreign Data Wrappers (FDW) or restructure."
        })
    if findings["partition_count"]:
        findings["index_issues"].append({
            "issue": f"Partitioned tables ({findings['partition_count']})",
            "effort": "MEDIUM",
            "notes": "PostgreSQL supports declarative partitioning; syntax and strategy differ."
        })
    if findings["mat_view_count"]:
        findings["index_issues"].append({
            "issue": f"Materialized views ({findings['mat_view_count']})",
            "effort": "MEDIUM",
            "notes": "PostgreSQL MATERIALIZED VIEW requires manual REFRESH; fast-refresh is not supported."
        })
    if findings["scheduler_job_count"]:
        findings["index_issues"].append({
            "issue": f"Scheduler jobs ({findings['scheduler_job_count']})",
            "effort": "MEDIUM",
            "notes": "Replace with pg_cron extension or external scheduler (cron, Airflow, etc.)."
        })

    # --- overall score & complexity ---
    total_score = type_effort_score + plsql_score
    findings["total_score"] = total_score
    if total_score < 100:
        findings["complexity"] = "LOW"
    elif total_score < 1000:
        findings["complexity"] = "MEDIUM"
    else:
        findings["complexity"] = "HIGH"

    # --- partition analysis ---
    part_tables  = data.get("part_table_summary", [])
    part_indexes = data.get("part_index_summary", [])
    part_types   = {(r.get("partitioning_type") or "").upper() for r in part_tables}
    idx_types    = {(r.get("partitioning_type") or "").upper() for r in part_indexes}
    lob_types    = {(r.get("data_type") or "").upper() for r in data.get("lob_tables", [])}

    total_part_size  = sum(float(r.get("total_size_mb") or 0) for r in part_tables)
    total_idx_size   = sum(float(r.get("total_size_mb") or 0) for r in part_indexes)
    largest_part_tbl = max(part_tables, key=lambda r: float(r.get("total_size_mb") or 0), default=None)

    # --- recommendations ---
    recs = []

    # Schema / environment
    recs.append("Set up a dedicated dev/test PostgreSQL environment and validate each schema using ora2pg or AWS SCT before migrating production.")

    # Packages
    pkg_count = findings["object_totals"].get("PACKAGE", 0) + findings["object_totals"].get("PACKAGE BODY", 0)
    if pkg_count:
        recs.append(f"Refactor {pkg_count} Oracle Package(s) — PostgreSQL has no package concept; split into schemas + individual functions/procedures.")

    # Oracle types
    type_count = findings["object_totals"].get("TYPE", 0)
    if type_count:
        recs.append(f"Review {type_count} Oracle TYPE object(s) — migrate to PostgreSQL composite types or normalised relational tables.")

    # High-effort data types
    high_types = [t for t in findings["type_compat"] if t["effort"] == "HIGH"]
    if high_types:
        names = ", ".join(t["data_type"] for t in high_types[:5])
        recs.append(f"High-effort data types found ({names}) — plan a dedicated column-by-column migration strategy for each.")

    # DATE columns
    date_cols = next((t for t in findings["type_compat"] if t["data_type"] == "DATE"), None)
    if date_cols:
        recs.append(f"Oracle DATE stores both date and time ({date_cols['column_count']:,} columns found) — map to TIMESTAMP in PostgreSQL to avoid data loss.")

    # PL/SQL patterns
    connect_by = [h for h in findings["plsql_hits"] if "CONNECT BY" in h["label"]]
    if connect_by:
        recs.append(f"Rewrite {connect_by[0]['object_count']} object(s) using CONNECT BY hierarchy as PostgreSQL WITH RECURSIVE CTEs.")
    dbms_hits = [h for h in findings["plsql_hits"] if "DBMS_" in h["label"] or "UTL_" in h["label"]]
    if dbms_hits:
        recs.append("Audit all DBMS_* / UTL_* package calls — find PostgreSQL extension equivalents (e.g. pg_cron, pg_net, pgcrypto).")
    if any(h for h in findings["plsql_hits"] if "AUTONOMOUS_TRANSACTION" in h["label"]):
        recs.append("Replace PRAGMA AUTONOMOUS_TRANSACTION blocks — use dblink or a separate database connection in PostgreSQL.")

    # Indexes
    if bitmap_count:
        recs.append(f"Replace {bitmap_count} Bitmap index(es) — not supported in PostgreSQL; use B-tree or partial indexes instead.")
    if func_idx_count:
        recs.append(f"Convert {func_idx_count} function-based index(es) to PostgreSQL expression indexes — syntax differs.")

    # Partitioning
    if part_tables:
        type_list = ", ".join(sorted(part_types - {""})) or "UNKNOWN"
        recs.append(
            f"{len(part_tables)} partitioned table(s) found ({type_list} partitioning, {total_part_size:,.1f} MB total). "
            f"PostgreSQL supports RANGE, LIST, and HASH declarative partitioning — COMPOSITE partitioning requires restructuring."
        )
        if largest_part_tbl:
            recs.append(
                f"Largest partitioned table: {largest_part_tbl.get('owner')}.{largest_part_tbl.get('table_name')} "
                f"({float(largest_part_tbl.get('total_size_mb') or 0):,.1f} MB, {int(largest_part_tbl.get('partition_count') or 0)} partitions) — "
                f"plan migration in partition-by-partition batches to minimise downtime."
            )
        if "INTERVAL" in part_types:
            recs.append("INTERVAL partitioning detected — rewrite as RANGE partitioning in PostgreSQL (no direct INTERVAL equivalent).")
        if "COMPOSITE" in part_types or any("COMPOSITE" in t for t in part_types):
            recs.append("Composite partitioning detected — PostgreSQL supports sub-partitioning only in limited form; review and simplify strategy.")

    if part_indexes:
        type_list = ", ".join(sorted(idx_types - {""})) or "UNKNOWN"
        recs.append(
            f"{len(part_indexes)} partitioned index(es) found ({type_list}, {total_idx_size:,.1f} MB total). "
            f"Recreate as standard or partial indexes in PostgreSQL — local/global index partitioning is not a concept in PostgreSQL."
        )

    # LOBs
    if "XMLTYPE" in lob_types:
        recs.append("XMLTYPE columns found — evaluate migrating to PostgreSQL XML type or JSONB depending on query patterns.")
    if lob_types & {"CLOB", "NCLOB", "BLOB"}:
        lob_list = ", ".join(sorted(lob_types & {"CLOB", "NCLOB", "BLOB"}))
        recs.append(f"LOB columns ({lob_list}) map to TEXT/BYTEA in PostgreSQL — validate application layer handles large object streaming correctly.")
    if lob_types & {"LONG", "LONG RAW"}:
        recs.append("LONG / LONG RAW columns are deprecated in Oracle and must be migrated to CLOB/BLOB first, then to TEXT/BYTEA in PostgreSQL.")

    # DB links
    if findings["db_link_count"]:
        recs.append(f"Replace {findings['db_link_count']} Database Link(s) with PostgreSQL Foreign Data Wrappers (postgres_fdw or oracle_fdw).")

    # Scheduler
    if findings["scheduler_job_count"]:
        recs.append(f"Migrate {findings['scheduler_job_count']} Scheduler Job(s) to pg_cron extension or an external scheduler (cron, Airflow, etc.).")

    # Materialized views
    if findings["mat_view_count"]:
        recs.append(f"{findings['mat_view_count']} Materialized View(s) found — recreate in PostgreSQL; note that fast-refresh (ON COMMIT) is not supported.")

    # NLS / character set
    nls_charset = data.get("nls", {}).get("NLS_CHARACTERSET", "")
    if nls_charset and nls_charset != "AL32UTF8":
        recs.append(f"Source character set is {nls_charset} (not AL32UTF8) — validate character conversion to UTF-8 during export to avoid data corruption.")
    else:
        recs.append("Validate character-set compatibility and ensure the target PostgreSQL cluster is initialised with UTF-8 encoding.")

    # General
    recs.append("Replace NVL → COALESCE, DECODE → CASE WHEN, SYSDATE → NOW(), sequence .NEXTVAL → NEXTVAL('seq') throughout all migrated code.")
    recs.append("Run EXPLAIN ANALYZE on the top 20 queries post-migration to catch missing indexes or plan regressions.")

    findings["recommendations"] = recs
    return findings


# ============================================================
# HTML REPORTER
# ============================================================

EFFORT_COLOUR = {"LOW": "#28a745", "MEDIUM": "#fd7e14", "HIGH": "#dc3545"}
COMPLEXITY_COLOUR = {"LOW": "#28a745", "MEDIUM": "#fd7e14", "HIGH": "#dc3545"}


def _e(text):
    """HTML-escape a value for safe output."""
    return _html.escape(str(text) if text is not None else "")


def _badge(effort):
    colour = EFFORT_COLOUR.get(effort, "#6c757d")
    return f'<span style="background:{colour};color:#fff;padding:2px 8px;border-radius:4px;font-size:0.8em">{_e(effort)}</span>'


def generate_html(data, findings, generated_at, schemas=None):
    db_info      = data.get("db_info", {})
    pdb_name     = data.get("pdb_name", "N/A")
    complexity   = findings["complexity"]
    obj_totals   = findings["object_totals"]
    schema_label = ", ".join(schemas) if schemas else "All Schemas"

    total_objects = sum(obj_totals.values())
    total_tables  = obj_totals.get("TABLE", 0)
    total_procs   = obj_totals.get("PROCEDURE", 0) + obj_totals.get("FUNCTION", 0)
    total_pkgs    = obj_totals.get("PACKAGE", 0)

    parts = []

    # ---------- HEAD ----------
    parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Oracle to PostgreSQL Migration Assessment - {_e(schema_label)}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;font-size:14px;color:#212529;background:#f8f9fa}}
  .header{{background:linear-gradient(135deg,#1a1a2e,#16213e);color:#fff;padding:32px 40px}}
  .header h1{{font-size:2em;font-weight:700}}
  .header p{{opacity:.8;margin-top:6px}}
  .container{{max-width:1200px;margin:0 auto;padding:28px 24px}}
  .card{{background:#fff;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.08);padding:24px;margin-bottom:24px}}
  .card h2{{font-size:1.15em;margin-bottom:16px;color:#1a1a2e;border-bottom:2px solid #e9ecef;padding-bottom:8px}}
  .cards-row{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:16px;margin-bottom:24px}}
  .stat-card{{background:#fff;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.08);padding:20px;text-align:center}}
  .stat-card .num{{font-size:2.2em;font-weight:700;color:#1a1a2e}}
  .stat-card .lbl{{color:#6c757d;font-size:0.85em;margin-top:4px}}
  table{{width:100%;border-collapse:collapse;font-size:0.9em}}
  th{{background:#f1f3f5;text-align:left;padding:9px 12px;font-weight:600;color:#495057;border-bottom:2px solid #dee2e6}}
  td{{padding:8px 12px;border-bottom:1px solid #f1f3f5;vertical-align:top}}
  tr:hover td{{background:#f8f9fa}}
  .complexity-badge{{display:inline-block;padding:6px 18px;border-radius:20px;font-weight:700;font-size:1em;color:#fff;background:{COMPLEXITY_COLOUR[complexity]}}}
  .rec-list{{list-style:none;counter-reset:rec}}
  .rec-list li{{counter-increment:rec;padding:10px 12px 10px 48px;position:relative;border-bottom:1px solid #f1f3f5}}
  .rec-list li::before{{content:counter(rec);position:absolute;left:12px;top:10px;background:#1a1a2e;color:#fff;width:22px;height:22px;border-radius:50%;text-align:center;font-size:.8em;line-height:22px;font-weight:600}}
  .info-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}}
  .info-item .key{{font-weight:600;color:#495057;font-size:.8em;text-transform:uppercase;letter-spacing:.5px}}
  .info-item .val{{color:#212529;margin-top:2px}}
  .errors{{background:#fff3cd;border:1px solid #ffc107;border-radius:6px;padding:14px;font-size:.85em}}
  .section-note{{color:#6c757d;font-size:.85em;margin-bottom:12px}}
</style>
</head>
<body>
<div class="header">
  <h1>Oracle &rarr; PostgreSQL Migration Assessment</h1>
  <p>
    Database: <strong>{_e(db_info.get('db_name','N/A'))}</strong> &nbsp;|&nbsp;
    PDB: <strong>{_e(pdb_name)}</strong> &nbsp;|&nbsp;
    Schema(s): <strong>{_e(schema_label)}</strong> &nbsp;|&nbsp;
    Generated: <strong>{_e(generated_at)}</strong> &nbsp;|&nbsp;
    Tool version: <strong>{VERSION}</strong>
  </p>
</div>
<div class="container">
""")

    # ---------- ERRORS ----------
    if data.get("errors"):
        errs = "".join(f"<li>{_e(e)}</li>" for e in data["errors"])
        parts.append(f'<div class="errors"><strong>⚠ Collection warnings (some queries may have been skipped):</strong><ul>{errs}</ul></div><br>')

    # ---------- COMPLEXITY SUMMARY ----------
    parts.append(f"""<div class="card">
  <h2>Executive Summary</h2>
  <div style="display:flex;align-items:center;gap:24px;flex-wrap:wrap">
    <div>
      <div style="color:#6c757d;font-size:.85em;margin-bottom:6px">OVERALL MIGRATION COMPLEXITY</div>
      <span class="complexity-badge">{complexity}</span>
    </div>
    <div style="flex:1;min-width:220px;color:#495057;font-size:.92em">
      Complexity score: <strong>{findings['total_score']:,}</strong> (weighted by effort × frequency).
      Score &lt; 100 = LOW · 100–999 = MEDIUM · 1000+ = HIGH.
    </div>
  </div>
</div>
""")

    # ---------- STAT CARDS ----------
    parts.append('<div class="cards-row">')
    stats = [
        (total_objects, "Total Objects"),
        (total_tables,  "Tables"),
        (total_procs,   "Procedures / Functions"),
        (total_pkgs,    "Packages"),
        (findings["partition_count"],      "Partitioned Tables"),
        (findings["db_link_count"],        "Database Links"),
        (findings["mat_view_count"],       "Materialized Views"),
        (findings["scheduler_job_count"],  "Scheduler Jobs"),
    ]
    for num, lbl in stats:
        parts.append(f'<div class="stat-card"><div class="num">{num:,}</div><div class="lbl">{_e(lbl)}</div></div>')
    parts.append("</div>")

    # ---------- DATABASE INFO ----------
    parts.append('<div class="card"><h2>Database Information</h2><div class="info-grid">')
    info_items = [
        ("Version",         data.get("version", "N/A")),
        ("Database Name",   db_info.get("db_name", "N/A")),
        ("PDB Name",        pdb_name),
        ("Unique Name",     db_info.get("db_unique_name", "N/A")),
        ("Platform",        db_info.get("platform_name", "N/A")),
        ("Log Mode",        db_info.get("log_mode", "N/A")),
        ("Schema(s) Assessed", schema_label),
        ("Character Set",   db_info.get("character_set") or data.get("nls", {}).get("NLS_CHARACTERSET", "N/A")),
        ("NLS Language",    data.get("nls", {}).get("NLS_LANGUAGE", "N/A")),
        ("NLS Territory",   data.get("nls", {}).get("NLS_TERRITORY", "N/A")),
        ("Date Format",     data.get("nls", {}).get("NLS_DATE_FORMAT", "N/A")),
    ]
    for key, val in info_items:
        parts.append(f'<div class="info-item"><div class="key">{_e(key)}</div><div class="val">{_e(val)}</div></div>')
    parts.append("</div></div>")

    # ---------- SCHEMAS ----------
    schema_data = data.get("schemas", [])
    if schema_data:
        parts.append('<div class="card"><h2>Schemas / Users in Scope</h2>')
        parts.append('<table><thead><tr><th>Username</th><th>Status</th><th>Created</th><th>Default Tablespace</th></tr></thead><tbody>')
        for row in schema_data:
            parts.append(
                f"<tr><td><strong>{_e(row.get('username',''))}</strong></td>"
                f"<td>{_e(row.get('account_status',''))}</td>"
                f"<td>{_e(row.get('created',''))}</td>"
                f"<td>{_e(row.get('default_tablespace',''))}</td></tr>"
            )
        parts.append("</tbody></table></div>")

    # ---------- OBJECT INVENTORY ----------
    parts.append('<div class="card"><h2>Object Inventory</h2>')
    if findings["object_totals"]:
        parts.append('<table><thead><tr><th>Object Type</th><th>Count</th><th>Invalid</th><th>Migration Notes</th></tr></thead><tbody>')
        obj_notes = {
            "TABLE":          ("LOW",    "Mostly straightforward; review data types"),
            "VIEW":           ("LOW",    "Review for Oracle-specific SQL syntax"),
            "PROCEDURE":      ("MEDIUM", "Review PL/SQL for Oracle-specific constructs"),
            "FUNCTION":       ("MEDIUM", "Review PL/SQL for Oracle-specific constructs"),
            "PACKAGE":        ("HIGH",   "Split into schema + individual functions in PostgreSQL"),
            "PACKAGE BODY":   ("HIGH",   "Split into schema + individual functions in PostgreSQL"),
            "TRIGGER":        ("MEDIUM", "Rewrite in PL/pgSQL; verify event/timing semantics"),
            "SEQUENCE":       ("LOW",    "Recreate with CREATE SEQUENCE; update .NEXTVAL references"),
            "SYNONYM":        ("LOW",    "Use PostgreSQL search_path or views instead"),
            "TYPE":           ("HIGH",   "Review composite vs collection types; may need redesign"),
            "MATERIALIZED VIEW": ("MEDIUM", "Recreate; fast-refresh is not supported in PostgreSQL"),
            "DATABASE LINK":  ("HIGH",   "Replace with Foreign Data Wrapper (FDW)"),
            "JOB":            ("MEDIUM", "Replace with pg_cron or external scheduler"),
            "INDEX":          ("LOW",    "Recreate; review bitmap and function-based indexes"),
        }
        for obj_type, total in sorted(obj_totals.items()):
            invalid = findings["invalid_objects"].get(obj_type, 0)
            effort, note = obj_notes.get(obj_type, ("MEDIUM", "Review required"))
            inv_cell = f'<span style="color:#dc3545;font-weight:600">{invalid}</span>' if invalid else "0"
            parts.append(f"<tr><td><strong>{_e(obj_type)}</strong></td><td>{total:,}</td><td>{inv_cell}</td>"
                         f"<td>{_badge(effort)} {_e(note)}</td></tr>")
        parts.append("</tbody></table>")
    else:
        parts.append("<p>No object data collected.</p>")
    parts.append("</div>")

    # ---------- DATA TYPE COMPATIBILITY ----------
    parts.append('<div class="card"><h2>Data Type Compatibility</h2>')
    parts.append('<p class="section-note">Only Oracle-specific or non-trivial types are highlighted.</p>')
    if findings["type_compat"]:
        parts.append('<table><thead><tr><th>Oracle Type</th><th>PostgreSQL Equivalent</th><th>Effort</th><th>Columns</th><th>Tables</th><th>Notes</th></tr></thead><tbody>')
        for row in findings["type_compat"]:
            parts.append(
                f"<tr><td><code>{_e(row['data_type'])}</code></td>"
                f"<td><code>{_e(row['pg_type'])}</code></td>"
                f"<td>{_badge(row['effort'])}</td>"
                f"<td>{row['column_count']:,}</td>"
                f"<td>{row['table_count']:,}</td>"
                f"<td>{_e(row['notes'])}</td></tr>"
            )
        parts.append("</tbody></table>")
    else:
        parts.append("<p>No column data collected.</p>")
    parts.append("</div>")

    # ---------- PL/SQL FINDINGS ----------
    parts.append('<div class="card"><h2>PL/SQL Compatibility Findings</h2>')
    if findings["plsql_hits"]:
        high = [h for h in findings["plsql_hits"] if h["effort"] == "HIGH"]
        med  = [h for h in findings["plsql_hits"] if h["effort"] == "MEDIUM"]
        low  = [h for h in findings["plsql_hits"] if h["effort"] == "LOW"]
        for group, label in [(high, "HIGH Effort"), (med, "MEDIUM Effort"), (low, "LOW Effort")]:
            if not group:
                continue
            colour = EFFORT_COLOUR[label.split()[0]]
            parts.append(f'<h3 style="margin:16px 0 8px;color:{colour}">{label}</h3>')
            parts.append('<table><thead><tr><th>Pattern</th><th>Effort</th><th>Objects Affected</th><th>Occurrences</th><th>Recommendation</th></tr></thead><tbody>')
            for hit in group:
                parts.append(
                    f"<tr><td>{_e(hit['label'])}</td>"
                    f"<td>{_badge(hit['effort'])}</td>"
                    f"<td>{hit['object_count']:,}</td>"
                    f"<td>{hit['hit_count']:,}</td>"
                    f"<td>{_e(hit['recommendation'])}</td></tr>"
                )
            parts.append("</tbody></table>")
    else:
        parts.append("<p>No PL/SQL source was collected or no patterns matched.</p>")
    parts.append("</div>")

    # ---------- OTHER ISSUES ----------
    if findings["index_issues"]:
        parts.append('<div class="card"><h2>Additional Compatibility Issues</h2>')
        parts.append('<table><thead><tr><th>Issue</th><th>Effort</th><th>Notes</th></tr></thead><tbody>')
        for issue in findings["index_issues"]:
            parts.append(
                f"<tr><td><strong>{_e(issue['issue'])}</strong></td>"
                f"<td>{_badge(issue['effort'])}</td>"
                f"<td>{_e(issue['notes'])}</td></tr>"
            )
        parts.append("</tbody></table></div>")

    # ---------- SEGMENT SIZES ----------
    seg_data = data.get("segment_sizes", [])
    if seg_data:
        parts.append('<div class="card"><h2>Storage by Schema &amp; Segment Type (MB)</h2>')
        parts.append('<table><thead><tr><th>Owner</th><th>Segment Type</th><th>Size (MB)</th></tr></thead><tbody>')
        for row in seg_data:
            parts.append(f"<tr><td>{_e(row.get('owner',''))}</td><td>{_e(row.get('segment_type',''))}</td><td>{row.get('size_mb', 0):,.2f}</td></tr>")
        parts.append("</tbody></table></div>")

    # ---------- TOP TABLES ----------
    top_tables_data = data.get("top_tables", [])
    if top_tables_data:
        parts.append('<div class="card"><h2>Top 50 Tables by Row Count</h2>')
        parts.append('<p class="section-note">Row counts are from the last statistics gather. Run DBMS_STATS.GATHER_DATABASE_STATS for accurate figures.</p>')
        parts.append('<table><thead><tr><th>#</th><th>Owner</th><th>Table Name</th><th>Rows (approx)</th><th>Size (MB)</th><th>Last Analyzed</th></tr></thead><tbody>')
        for i, row in enumerate(top_tables_data, 1):
            num_rows     = row.get("num_rows")
            size_mb      = row.get("size_mb")
            rows_cell    = f"{int(num_rows):,}"       if num_rows is not None else "N/A"
            size_cell    = f"{float(size_mb):,.2f}"   if size_mb  is not None else "N/A"
            parts.append(
                f"<tr><td>{i}</td>"
                f"<td>{_e(row.get('owner',''))}</td>"
                f"<td><strong>{_e(row.get('table_name',''))}</strong></td>"
                f"<td>{rows_cell}</td>"
                f"<td>{size_cell}</td>"
                f"<td>{_e(row.get('last_analyzed','N/A'))}</td></tr>"
            )
        parts.append("</tbody></table></div>")

    # ---------- LOB TABLES ----------
    lob_data = data.get("lob_tables", [])
    if lob_data:
        lob_type_colour = {
            "CLOB": "#fd7e14", "NCLOB": "#fd7e14",
            "BLOB": "#6f42c1",
            "XMLTYPE": "#dc3545",
            "LONG": "#dc3545", "LONG RAW": "#dc3545",
            "RAW": "#6c757d",
        }
        parts.append('<div class="card"><h2>Tables with LOB / Large Object Columns</h2>')
        parts.append('<p class="section-note">LOB columns require special handling during migration: CLOB/NCLOB → TEXT, BLOB → BYTEA, XMLTYPE → XML or JSONB, LONG/LONG RAW → TEXT/BYTEA.</p>')
        parts.append('<table><thead><tr><th>Owner</th><th>Table Name</th><th>Column</th><th>Data Type</th><th>LOB Segment</th><th>LOB Size (MB)</th></tr></thead><tbody>')
        for row in lob_data:
            dtype   = (row.get("data_type") or "").upper()
            colour  = lob_type_colour.get(dtype, "#495057")
            size_mb = row.get("lob_size_mb")
            size_cell = f"{float(size_mb):,.2f}" if size_mb is not None else "N/A"
            parts.append(
                f"<tr><td>{_e(row.get('owner',''))}</td>"
                f"<td><strong>{_e(row.get('table_name',''))}</strong></td>"
                f"<td>{_e(row.get('column_name',''))}</td>"
                f"<td><span style='color:{colour};font-weight:600'>{_e(dtype)}</span></td>"
                f"<td>{_e(row.get('lob_segment') or 'N/A')}</td>"
                f"<td>{size_cell}</td></tr>"
            )
        parts.append("</tbody></table></div>")

    # ---------- PARTITIONED TABLES ----------
    pt_summary = data.get("part_table_summary", [])
    pt_detail  = data.get("part_table_detail",  [])
    if pt_summary:
        parts.append('<div class="card"><h2>Partitioned Tables</h2>')
        parts.append('<p class="section-note">Summary: one row per table. PostgreSQL supports RANGE, LIST, and HASH declarative partitioning. INTERVAL and COMPOSITE require restructuring.</p>')
        parts.append(
            '<table><thead><tr>'
            '<th>Owner</th><th>Table Name</th><th>Partition Type</th><th>Sub-Partition Type</th>'
            '<th>Partitions</th><th>Total Rows</th><th>Total Size (MB)</th>'
            '</tr></thead><tbody>'
        )
        for row in pt_summary:
            total_rows = row.get("total_rows")
            total_mb   = row.get("total_size_mb")
            rows_cell  = f"{int(total_rows):,}"     if total_rows is not None else "N/A"
            mb_cell    = f"{float(total_mb):,.2f}"  if total_mb   is not None else "N/A"
            ptype      = (row.get("partitioning_type")    or "N/A").upper()
            sptype     = (row.get("subpartitioning_type") or "NONE").upper()
            parts.append(
                f"<tr><td>{_e(row.get('owner',''))}</td>"
                f"<td><strong>{_e(row.get('table_name',''))}</strong></td>"
                f"<td>{_e(ptype)}</td><td>{_e(sptype)}</td>"
                f"<td>{_e(str(row.get('partition_count','N/A')))}</td>"
                f"<td>{rows_cell}</td><td>{mb_cell}</td></tr>"
            )
        parts.append("</tbody></table>")

        if pt_detail:
            parts.append('<h3 style="margin:20px 0 8px;font-size:1em;color:#495057">Partition Detail</h3>')
            parts.append(
                '<table><thead><tr>'
                '<th>Owner</th><th>Table Name</th><th>Partition Name</th>'
                '<th>Position</th><th>Rows</th><th>Size (MB)</th><th>Last Analyzed</th>'
                '</tr></thead><tbody>'
            )
            for row in pt_detail:
                num_rows = row.get("num_rows")
                size_mb  = row.get("size_mb")
                parts.append(
                    f"<tr><td>{_e(row.get('owner',''))}</td>"
                    f"<td>{_e(row.get('table_name',''))}</td>"
                    f"<td><strong>{_e(row.get('partition_name',''))}</strong></td>"
                    f"<td>{_e(str(row.get('position','N/A')))}</td>"
                    f"<td>{ f'{int(num_rows):,}' if num_rows is not None else 'N/A' }</td>"
                    f"<td>{ f'{float(size_mb):,.2f}' if size_mb is not None else 'N/A' }</td>"
                    f"<td>{_e(row.get('last_analyzed','N/A'))}</td></tr>"
                )
            parts.append("</tbody></table>")
        parts.append("</div>")

    # ---------- INDEX PARTITIONS ----------
    pi_summary = data.get("part_index_summary", [])
    pi_detail  = data.get("part_index_detail",  [])
    if pi_summary:
        parts.append('<div class="card"><h2>Partitioned Indexes</h2>')
        parts.append('<p class="section-note">Local/global index partitioning is not a concept in PostgreSQL — recreate as standard B-tree or partial indexes.</p>')
        parts.append(
            '<table><thead><tr>'
            '<th>Owner</th><th>Index Name</th><th>Table Name</th><th>Partition Type</th>'
            '<th>Partitions</th><th>Total Rows</th><th>Leaf Blocks</th><th>Total Size (MB)</th>'
            '</tr></thead><tbody>'
        )
        for row in pi_summary:
            total_rows = row.get("total_rows")
            total_mb   = row.get("total_size_mb")
            leaf_blk   = row.get("total_leaf_blocks")
            rows_cell  = f"{int(total_rows):,}"   if total_rows is not None else "N/A"
            mb_cell    = f"{float(total_mb):,.2f}" if total_mb   is not None else "N/A"
            lb_cell    = f"{int(leaf_blk):,}"      if leaf_blk   is not None else "N/A"
            ptype      = (row.get("partitioning_type") or "N/A").upper()
            parts.append(
                f"<tr><td>{_e(row.get('owner',''))}</td>"
                f"<td><strong>{_e(row.get('index_name',''))}</strong></td>"
                f"<td>{_e(row.get('table_name',''))}</td>"
                f"<td>{_e(ptype)}</td>"
                f"<td>{_e(str(row.get('partition_count','N/A')))}</td>"
                f"<td>{rows_cell}</td><td>{lb_cell}</td><td>{mb_cell}</td></tr>"
            )
        parts.append("</tbody></table>")

        if pi_detail:
            parts.append('<h3 style="margin:20px 0 8px;font-size:1em;color:#495057">Index Partition Detail</h3>')
            parts.append(
                '<table><thead><tr>'
                '<th>Owner</th><th>Index Name</th><th>Partition Name</th>'
                '<th>Position</th><th>Rows</th><th>Leaf Blocks</th><th>B-Level</th>'
                '<th>Size (MB)</th><th>Last Analyzed</th>'
                '</tr></thead><tbody>'
            )
            for row in pi_detail:
                num_rows  = row.get("num_rows")
                size_mb   = row.get("size_mb")
                leaf_blk  = row.get("leaf_blocks")
                parts.append(
                    f"<tr><td>{_e(row.get('owner',''))}</td>"
                    f"<td>{_e(row.get('index_name',''))}</td>"
                    f"<td><strong>{_e(row.get('partition_name',''))}</strong></td>"
                    f"<td>{_e(str(row.get('position','N/A')))}</td>"
                    f"<td>{ f'{int(num_rows):,}' if num_rows is not None else 'N/A' }</td>"
                    f"<td>{ f'{int(leaf_blk):,}' if leaf_blk is not None else 'N/A' }</td>"
                    f"<td>{_e(str(row.get('blevel','N/A')))}</td>"
                    f"<td>{ f'{float(size_mb):,.2f}' if size_mb is not None else 'N/A' }</td>"
                    f"<td>{_e(row.get('last_analyzed','N/A'))}</td></tr>"
                )
            parts.append("</tbody></table>")
        parts.append("</div>")

    # ---------- RECOMMENDATIONS ----------
    parts.append('<div class="card"><h2>Migration Recommendations</h2>')
    recs_html = "".join(f"<li>{_e(r)}</li>" for r in findings["recommendations"])
    parts.append(f'<ol class="rec-list">{recs_html}</ol></div>')

    # ---------- FOOTER ----------
    parts.append(f"""<div style="text-align:center;color:#adb5bd;font-size:.8em;padding:24px 0">
  Oracle → PostgreSQL Migration Assessment Tool v{VERSION} &nbsp;·&nbsp; Generated {_e(generated_at)}
</div>
</div>
</body>
</html>""")

    return "".join(parts)


# ============================================================
# CSV REPORTER
# ============================================================

def generate_csv(data, findings, output_dir):
    """Write CSV files for objects, data types, PL/SQL findings, top tables, and LOB tables."""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    obj_path = output_dir / f"objects_{ts}.csv"
    with obj_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["object_type", "total", "invalid"])
        for obj_type, total in sorted(findings["object_totals"].items()):
            w.writerow([obj_type, total, findings["invalid_objects"].get(obj_type, 0)])

    dt_path = output_dir / f"data_types_{ts}.csv"
    with dt_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["oracle_type", "pg_type", "effort", "column_count", "table_count", "notes"])
        for row in findings["type_compat"]:
            w.writerow([row["data_type"], row["pg_type"], row["effort"],
                        row["column_count"], row["table_count"], row["notes"]])

    plsql_path = output_dir / f"plsql_findings_{ts}.csv"
    with plsql_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["pattern", "effort", "occurrences", "objects_affected", "recommendation"])
        for hit in findings["plsql_hits"]:
            w.writerow([hit["label"], hit["effort"], hit["hit_count"],
                        hit["object_count"], hit["recommendation"]])

    top_tables_path = output_dir / f"top_tables_{ts}.csv"
    with top_tables_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "owner", "table_name", "num_rows", "size_mb", "last_analyzed"])
        for i, row in enumerate(data.get("top_tables", []), 1):
            w.writerow([i, row.get("owner"), row.get("table_name"),
                        row.get("num_rows"), row.get("size_mb"), row.get("last_analyzed")])

    lob_path = output_dir / f"lob_tables_{ts}.csv"
    with lob_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["owner", "table_name", "column_name", "data_type", "lob_segment", "lob_size_mb"])
        for row in data.get("lob_tables", []):
            w.writerow([row.get("owner"), row.get("table_name"), row.get("column_name"),
                        row.get("data_type"), row.get("lob_segment"), row.get("lob_size_mb")])

    part_tbl_path = output_dir / f"partition_tables_{ts}.csv"
    with part_tbl_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["owner", "table_name", "partitioning_type", "subpartitioning_type",
                    "partition_count", "total_rows", "total_size_mb"])
        for row in data.get("part_table_summary", []):
            w.writerow([row.get("owner"), row.get("table_name"), row.get("partitioning_type"),
                        row.get("subpartitioning_type"), row.get("partition_count"),
                        row.get("total_rows"), row.get("total_size_mb")])
        w.writerow([])
        w.writerow(["-- PARTITION DETAIL --"])
        w.writerow(["owner", "table_name", "partition_name", "position", "num_rows", "size_mb", "last_analyzed"])
        for row in data.get("part_table_detail", []):
            w.writerow([row.get("owner"), row.get("table_name"), row.get("partition_name"),
                        row.get("position"), row.get("num_rows"),
                        row.get("size_mb"), row.get("last_analyzed")])

    part_idx_path = output_dir / f"partition_indexes_{ts}.csv"
    with part_idx_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["owner", "index_name", "table_name", "partitioning_type", "subpartitioning_type",
                    "partition_count", "total_rows", "total_leaf_blocks", "total_size_mb"])
        for row in data.get("part_index_summary", []):
            w.writerow([row.get("owner"), row.get("index_name"), row.get("table_name"),
                        row.get("partitioning_type"), row.get("subpartitioning_type"),
                        row.get("partition_count"), row.get("total_rows"),
                        row.get("total_leaf_blocks"), row.get("total_size_mb")])
        w.writerow([])
        w.writerow(["-- INDEX PARTITION DETAIL --"])
        w.writerow(["owner", "index_name", "partition_name", "position", "num_rows",
                    "leaf_blocks", "blevel", "size_mb", "last_analyzed"])
        for row in data.get("part_index_detail", []):
            w.writerow([row.get("owner"), row.get("index_name"), row.get("partition_name"),
                        row.get("position"), row.get("num_rows"), row.get("leaf_blocks"),
                        row.get("blevel"), row.get("size_mb"), row.get("last_analyzed")])

    return obj_path, dt_path, plsql_path, top_tables_path, lob_path, part_tbl_path, part_idx_path


# ============================================================
# MAIN
# ============================================================

def build_dsn(args):
    if args.tns:
        return args.tns
    return oracledb.makedsn(args.host, args.port, service_name=args.service)


def parse_args():
    p = argparse.ArgumentParser(
        description="Oracle to PostgreSQL Migration Assessment Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python oracle_assessment.py --host db.example.com --port 1521 --service ORCL \\
      --user assessor --password secret

  python oracle_assessment.py --host db.example.com --service ORCL \\
      --user assessor --password secret --schemas HR,SALES --output-dir ./reports

  python oracle_assessment.py --tns "db.example.com:1521/ORCL" \\
      --user assessor --password secret --mode user
""",
    )
    conn = p.add_argument_group("Connection (use --tns OR --host/--port/--service)")
    conn.add_argument("--host",     default="localhost", help="Oracle host (default: localhost)")
    conn.add_argument("--port",     type=int, default=1521, help="Listener port (default: 1521)")
    conn.add_argument("--service",  default="ORCL", help="Service name (default: ORCL)")
    conn.add_argument("--tns",      help="Full TNS string, e.g. host:port/service (overrides --host/--port/--service)")
    conn.add_argument("--user",     required=True, help="Oracle username")
    conn.add_argument("--password", required=True, help="Oracle password")

    p.add_argument("--schemas",    help="Comma-separated list of schemas to assess (default: all non-system)")
    p.add_argument("--output-dir", default="./reports", help="Directory for reports (default: ./reports)")
    p.add_argument("--mode",       choices=["dba", "user"], default="dba",
                   help="'dba' uses DBA_ views (full picture); 'user' uses ALL_ views (default: dba)")
    p.add_argument("--no-source",  action="store_true",
                   help="Skip PL/SQL source collection (faster, but no PL/SQL analysis)")
    p.add_argument("--sysdba",     action="store_true",
                   help="Connect as SYSDBA (required when --user is SYS)")
    p.add_argument("--thick",      action="store_true",
                   help="Enable thick mode (required for older Oracle servers / unsupported auth protocols)")
    p.add_argument("--client-lib", default="",
                   help="Path to Oracle Instant Client directory (used with --thick)")
    return p.parse_args()


def _write_reports(data, findings, out_dir, schemas):
    """Write HTML, JSON, and CSV reports into out_dir."""
    ts           = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html_path = out_dir / f"oracle_assessment_{ts}.html"
    html_path.write_text(generate_html(data, findings, generated_at, schemas=schemas), encoding="utf-8")
    print(f"      HTML  → {html_path}")

    json_path = out_dir / f"oracle_assessment_{ts}.json"

    def _serialise(obj):
        if isinstance(obj, (set, frozenset)):
            return list(obj)
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    json_path.write_text(
        json.dumps({"metadata": {"generated_at": generated_at, "version": VERSION},
                    "data": data, "findings": {k: (dict(v) if isinstance(v, Counter) else v)
                                               for k, v in findings.items()}},
                   default=_serialise, indent=2),
        encoding="utf-8"
    )
    print(f"      JSON  → {json_path}")

    obj_csv, dt_csv, plsql_csv, top_csv, lob_csv, part_tbl_csv, part_idx_csv = generate_csv(data, findings, out_dir)
    print(f"      CSV   → {obj_csv.name}, {dt_csv.name}, {plsql_csv.name}, {top_csv.name}, {lob_csv.name}, {part_tbl_csv.name}, {part_idx_csv.name}")

    return html_path


def main():
    args     = parse_args()
    schemas  = [s.strip() for s in args.schemas.split(",")] if args.schemas else []
    base_dir = Path(args.output_dir)

    # ---- thick mode ----
    if args.thick:
        try:
            oracledb.init_oracle_client(lib_dir=args.client_lib or None)
            print("      Thick mode enabled (Oracle Instant Client loaded)")
        except Exception as exc:
            print(f"ERROR: Could not initialise thick mode — {exc}")
            print("       Make sure Oracle Instant Client is installed and pass its path with --client-lib")
            sys.exit(1)

    # ---- connect (single connection reused for all schema runs) ----
    dsn = build_dsn(args)
    sysdba_note = " AS SYSDBA" if args.sysdba else ""
    print(f"\n[1/4] Connecting to Oracle  ({dsn}{sysdba_note}) …")
    try:
        connect_kwargs = dict(user=args.user, password=args.password, dsn=dsn)
        if args.sysdba:
            connect_kwargs["mode"] = oracledb.AUTH_MODE_SYSDBA
        conn = oracledb.connect(**connect_kwargs)
        print("      Connected OK")
    except Exception as exc:
        print(f"ERROR: Could not connect — {exc}")
        sys.exit(1)

    # ---- resolve database name upfront ----
    _tmp    = OracleAssessor(conn, schemas=[], mode=args.mode)
    db_name = (_tmp.db_info().get("db_name") or args.service or "unknown").strip().upper()

    # ---- determine runs: one per schema, or a single all-schema run ----
    schema_runs = schemas if schemas else [None]

    html_paths = []
    total = len(schema_runs)
    for idx, schema in enumerate(schema_runs, 1):
        run_schemas = [schema] if schema else []
        label       = schema or "ALL SCHEMAS"

        if total > 1:
            print(f"\n--- [{idx}/{total}] Schema: {schema} ---")

        # output folder
        out_dir = base_dir / db_name / schema if schema else base_dir / db_name
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"      Output folder: {out_dir}")

        # collect
        print("\n[2/4] Collecting metadata …")
        assessor = OracleAssessor(conn, schemas=run_schemas, mode=args.mode)
        if args.no_source:
            assessor.plsql_source = lambda: []
        data = assessor.collect_all()
        if data["errors"]:
            print(f"      ⚠  {len(data['errors'])} collection warning(s) — check the HTML report for details.")

        # analyse
        print("\n[3/4] Analysing findings …")
        findings = analyse(data)
        print(f"      Complexity: {findings['complexity']}  (score {findings['total_score']:,})")

        # write reports
        print("\n[4/4] Writing reports …")
        html_path = _write_reports(data, findings, out_dir, run_schemas)
        html_paths.append(html_path)

    conn.close()

    print(f"\nDone.  {len(html_paths)} report(s) generated.")
    for p in html_paths:
        print(f"  → {p}")


if __name__ == "__main__":
    main()
