# Oracle to PostgreSQL Migration Assessment Tool

A Python script that connects to an Oracle database and generates a comprehensive migration assessment report to help plan an Oracle → PostgreSQL migration.

## What It Produces

Running the tool generates three report files in the output directory:

| File | Format | Purpose |
|---|---|---|
| `oracle_assessment_<timestamp>.html` | HTML | Full human-readable report — open in any browser |
| `oracle_assessment_<timestamp>.json` | JSON | Machine-readable full data dump |
| `objects_<timestamp>.csv` | CSV | Object inventory summary |
| `data_types_<timestamp>.csv` | CSV | Data type compatibility matrix |
| `plsql_findings_<timestamp>.csv` | CSV | PL/SQL pattern findings |

## What It Assesses

- **Database info** — version, character set, NLS parameters, platform
- **Object inventory** — tables, views, procedures, functions, packages, triggers, sequences, synonyms, types, and more — with valid/invalid counts
- **Data type compatibility** — maps every Oracle type in use to its PostgreSQL equivalent with a LOW / MEDIUM / HIGH effort rating
- **Index analysis** — detects bitmap indexes (unsupported in PostgreSQL) and function-based indexes
- **Partitioned tables** — count and partitioning strategies
- **PL/SQL source scan** — searches all stored code for 50+ Oracle-specific patterns: `ROWNUM`, `CONNECT BY`, `DBMS_*`, `UTL_*`, `MERGE`, `BULK COLLECT`, packages, collection types, and more
- **Database links** — flags the need for Foreign Data Wrappers
- **Materialized views** — flags fast-refresh limitations
- **Scheduler jobs** — flags migration to `pg_cron` or an external scheduler
- **Storage sizing** — segment size breakdown by schema and type

A weighted complexity score (LOW / MEDIUM / HIGH) is calculated from all findings and shown at the top of the HTML report.

## Requirements

- Python 3.8 or later
- Oracle database access (DBA role recommended; see `--mode` below)

## Installation

```bash
git clone https://github.com/hynaeugenio/database-assessment.git
cd database-assessment
pip install -r requirements.txt
```

## Usage

### Basic

```bash
python oracle_assessment.py \
  --host myoracle.example.com \
  --port 1521 \
  --service ORCL \
  --user assessor \
  --password secret
```

### Assess a single schema

```bash
python oracle_assessment.py \
  --host myoracle.example.com \
  --service ORCL \
  --user assessor \
  --password secret \
  --schemas HR
```

### Assess multiple schemas

Pass a comma-separated list of schema names (no spaces). The report will cover all named schemas combined.

```bash
python oracle_assessment.py \
  --host myoracle.example.com \
  --service ORCL \
  --user assessor \
  --password secret \
  --schemas HR,SALES,FINANCE
```

If `--schemas` is omitted, **all non-system schemas** are assessed automatically.

### Multiple schemas with custom output directory

```bash
python oracle_assessment.py \
  --host myoracle.example.com \
  --service ORCL \
  --user assessor \
  --password secret \
  --schemas HR,SALES,FINANCE \
  --output-dir ./reports/my_assessment
```

### Use a TNS connection string

```bash
python oracle_assessment.py \
  --tns "myoracle.example.com:1521/ORCL" \
  --user assessor \
  --password secret
```

### Save reports to a custom directory

```bash
python oracle_assessment.py \
  --host myoracle.example.com \
  --service ORCL \
  --user assessor \
  --password secret \
  --output-dir /path/to/reports
```

### Non-DBA account (uses ALL_ views instead of DBA_ views)

```bash
python oracle_assessment.py \
  --host myoracle.example.com \
  --service ORCL \
  --user hr_owner \
  --password secret \
  --mode user
```

### Skip PL/SQL source scan (faster on large databases)

```bash
python oracle_assessment.py \
  --host myoracle.example.com \
  --service ORCL \
  --user assessor \
  --password secret \
  --no-source
```

## All Options

```
Connection (use --tns OR --host/--port/--service):
  --host HOST           Oracle host                   (default: localhost)
  --port PORT           Listener port                 (default: 1521)
  --service SERVICE     Service name                  (default: ORCL)
  --tns TNS             Full TNS string: host:port/service

Required:
  --user USER           Oracle username
  --password PASSWORD   Oracle password

Optional:
  --schemas SCHEMAS     Comma-separated list of schemas to assess
                        (default: all non-system schemas)
  --output-dir DIR      Directory for output reports   (default: ./reports)
  --mode {dba,user}     dba  = use DBA_ views (full picture, requires DBA role)
                        user = use ALL_ views (schema owner access only)
                        (default: dba)
  --no-source           Skip PL/SQL source collection and analysis
```

## Access Requirements

| Mode | Minimum Oracle Privileges |
|---|---|
| `dba` (default) | `DBA` role, or `SELECT_CATALOG_ROLE` + `SELECT ANY DICTIONARY` |
| `user` | Schema owner access; `SELECT` on `ALL_*` views |

For the most complete assessment, connect with a user that has the `DBA` role or `SELECT ANY DICTIONARY` privilege.

## Complexity Rating

The tool calculates an overall migration complexity:

| Rating | Score | Meaning |
|---|---|---|
| **LOW** | < 100 | Standard schema, common data types, minimal PL/SQL |
| **MEDIUM** | 100 – 999 | Some Oracle-specific features require adaptation |
| **HIGH** | 1000+ | Significant Oracle-specific features — packages, CONNECT BY, DBMS_*, XML, etc. |

Each finding is weighted: LOW effort = 1 point, MEDIUM = 3 points, HIGH = 9 points, multiplied by frequency.

## Common Oracle → PostgreSQL Migration Notes

| Oracle | PostgreSQL |
|---|---|
| `VARCHAR2` | `VARCHAR` / `TEXT` |
| `NUMBER(p,0)` | `INTEGER` / `BIGINT` |
| `NUMBER(p,s)` | `NUMERIC(p,s)` |
| `DATE` (includes time) | `TIMESTAMP` |
| `CLOB` / `NCLOB` | `TEXT` |
| `BLOB` | `BYTEA` |
| `SYSDATE` | `NOW()` / `CURRENT_TIMESTAMP` |
| `NVL(a, b)` | `COALESCE(a, b)` |
| `DECODE(x, v, r)` | `CASE WHEN x = v THEN r END` |
| `ROWNUM` | `LIMIT n` / `FETCH FIRST n ROWS ONLY` |
| `CONNECT BY` | `WITH RECURSIVE` CTE |
| `.NEXTVAL` / `.CURRVAL` | `NEXTVAL('seq')` / `CURRVAL('seq')` |
| `FROM DUAL` | Not needed in PostgreSQL |
| `DBMS_*` / `UTL_*` | Find extension or rewrite |
| Database Link | Foreign Data Wrapper (`postgres_fdw`) |
| Package | Schema + individual functions |
| Bitmap index | B-tree or partial index |

## Recommended Migration Tools

After running the assessment, consider these tools for the actual migration:

- **[ora2pg](https://ora2pg.darold.net/)** — open-source schema and data migration tool
- **[AWS Schema Conversion Tool (SCT)](https://aws.amazon.com/dms/schema-conversion-tool/)** — automated schema conversion
- **[pgloader](https://pgloader.io/)** — data loading from Oracle to PostgreSQL
- **[AWS DMS](https://aws.amazon.com/dms/)** — managed data migration service
