import os
import re
from datetime import datetime, date

import psycopg2
from psycopg2 import IntegrityError
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


# ============================================================
# POSTGRESQL CONNECTION / SQLITE-COMPATIBILITY HELPERS
# ============================================================

def _translate_sql(sql):
    sql = str(sql)
    sql = sql.replace("?", "%s")
    was_ignore = bool(re.search(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", sql, re.I))
    if was_ignore:
        sql = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", sql, flags=re.I)
        stripped = sql.rstrip().rstrip(";")
        if not re.search(r"\bON\s+CONFLICT\b", stripped, re.I):
            sql = stripped + " ON CONFLICT DO NOTHING"
    return sql


class PGCursorCompat:
    def __init__(self, raw_cursor):
        self.raw = raw_cursor
        self.lastrowid = None

    def execute(self, sql, params=None):
        translated = _translate_sql(sql)
        is_insert = bool(re.match(r"^\s*INSERT\b", translated, re.I))
        if is_insert and not re.search(r"\bRETURNING\b", translated, re.I):
            translated = translated.rstrip().rstrip(";") + " RETURNING id"
        if params is None:
            self.raw.execute(translated)
        else:
            self.raw.execute(translated, params)
        if is_insert:
            row = self.raw.fetchone()
            if row:
                self.lastrowid = row.get("id") if isinstance(row, dict) else row[0]
        return self

    def fetchone(self):
        return self.raw.fetchone()

    def fetchall(self):
        return self.raw.fetchall()

    def close(self):
        self.raw.close()


class PGConnectionCompat:
    def __init__(self, raw):
        self.raw = raw

    def cursor(self):
        return PGCursorCompat(self.raw.cursor(cursor_factory=RealDictCursor))

    def execute(self, sql, params=None):
        cur = self.cursor()
        return cur.execute(sql, params)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def get_connection():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not configured. Set it to your Render PostgreSQL URL."
        )
    raw = psycopg2.connect(DATABASE_URL, sslmode="require")
    return PGConnectionCompat(raw)


def normalize_cell(cell):
    return re.sub(r"[\s\-\(\)]", "", str(cell or "")).strip()


def normalize_firm_number(firm_number):
    return str(firm_number or "").strip().upper()


def row_to_dict(row):
    return dict(row) if row else None


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def _column_exists(conn, table, column):
    row = conn.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s AND column_name=%s
        LIMIT 1
        """,
        (table, column),
    ).fetchone()
    return bool(row)


def _add_column_if_missing(conn, table, column, definition):
    if not _column_exists(conn, table, column):
        definition = definition.replace("REAL", "DOUBLE PRECISION")
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _next_firm_number(cursor):
    rows = cursor.execute(
        "SELECT firm_number FROM organizations WHERE firm_number LIKE 'NEX-%'"
    ).fetchall()
    nums = []
    for row in rows:
        try:
            nums.append(int(str(row["firm_number"]).split("-")[1]))
        except Exception:
            pass
    return f"NEX-{max(nums, default=0) + 1:03d}"


def _next_number(cursor, table, column, org_id, prefix, width):
    rows = cursor.execute(
        f"SELECT {column} FROM {table} WHERE org_id=? AND {column} LIKE ?",
        (org_id, prefix + "%")
    ).fetchall()

    nums = []
    for row in rows:
        try:
            nums.append(int(str(row[column]).split("-")[-1]))
        except Exception:
            pass

    return f"{prefix}{max(nums, default=0) + 1:0{width}d}"


# ============================================================
# DEFAULT NEXORA SETUP DATA
# ============================================================

DEFAULT_MATTER_TYPES = [
    "Litigation",
    "Road Accident Fund (RAF)",
    "Criminal Law",
    "Divorce / Family Law",
    "Labour Law",
    "Medical Negligence",
    "Commercial Law",
    "Debt Collection",
    "Estates / Wills",
    "Property / Conveyancing",
    "Corporate / Company Law",
    "Insurance Law",
    "Personal Injury",
    "Administrative Law",
    "Immigration",
    "Tax",
    "Other",
]

DEFAULT_SERVICES = [
    ("Taking Instructions", "Initial instructions received from the client or correspondent.", "Hour"),
    ("Consultation", "Client, witness, expert or other professional consultation.", "Hour"),
    ("Drafting Correspondence", "Drafting letters, notices and formal correspondence.", "Hour"),
    ("Drafting Pleadings", "Drafting pleadings, notices, affidavits and litigation documents.", "Hour"),
    ("Court Preparation", "Preparation for a hearing, trial, motion or court appearance.", "Hour"),
    ("Court Appearance", "Attendance and appearance at court or tribunal proceedings.", "Hour"),
    ("Legal Research", "Legal research, authorities and case-law preparation.", "Hour"),
    ("Telephone Call", "Billable telephone consultation or matter-related call.", "Hour"),
    ("Email Correspondence", "Billable email correspondence relating to the matter.", "Hour"),
    ("Review of Documents", "Review and analysis of documents, records or evidence.", "Hour"),
    ("File Administration", "Matter administration, file management and procedural administration.", "Hour"),
    ("Settlement Negotiation", "Settlement discussions, negotiations and related preparation.", "Hour"),
    ("Client Update", "Providing a substantive progress update to the client.", "Hour"),
    ("Briefing Counsel", "Preparation of a brief and instructions to counsel or another specialist.", "Hour"),
    ("Attending Consultation", "Attendance at a consultation, meeting, inspection or conference.", "Hour"),
    ("Travel / Attendance", "Matter-related travel or attendance where billable under the firm's rules.", "Hour"),
    ("Other", "Other billable legal service not covered by the standard service list.", "Hour"),
]


def _seed_default_setup(conn, org_id):
    """Add Nexora defaults without overwriting a firm's existing setup."""
    for name in DEFAULT_MATTER_TYPES:
        conn.execute(
            "INSERT OR IGNORE INTO matter_types(org_id, name) VALUES (?, ?)",
            (org_id, name),
        )

    for name, description, default_unit in DEFAULT_SERVICES:
        conn.execute(
            """
            INSERT OR IGNORE INTO services(
                org_id, name, description, default_unit, active
            )
            VALUES (?, ?, ?, ?, 1)
            """,
            (org_id, name, description, default_unit),
        )


# ============================================================
# INIT
# ============================================================

def init_db():
    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS organizations (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            firm_number TEXT UNIQUE NOT NULL,
            registered_cell TEXT,
            address TEXT,
            email TEXT,
            phone TEXT,
            website TEXT,
            registration_number TEXT,
            vat_number TEXT,
            bank_name TEXT,
            bank_account_name TEXT,
            bank_account_number TEXT,
            bank_branch_code TEXT,
            invoice_payment_terms TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS practitioner_types (
            id BIGSERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(org_id, name),
            FOREIGN KEY(org_id) REFERENCES organizations(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL,
            firm_number TEXT NOT NULL,
            name TEXT NOT NULL,
            cell TEXT NOT NULL,
            role TEXT DEFAULT 'Lawyer',
            practitioner_type_id INTEGER,
            attorney_level TEXT,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(cell),
            FOREIGN KEY(org_id) REFERENCES organizations(id),
            FOREIGN KEY(practitioner_type_id) REFERENCES practitioner_types(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id BIGSERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            default_unit TEXT DEFAULT 'Hour',
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(org_id, name),
            FOREIGN KEY(org_id) REFERENCES organizations(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS service_fees (
            id BIGSERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            practitioner_type_id INTEGER NOT NULL,
            fee DOUBLE PRECISION DEFAULT 0,
            unit TEXT DEFAULT 'Hour',
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            UNIQUE(org_id, service_id, practitioner_type_id),
            FOREIGN KEY(org_id) REFERENCES organizations(id),
            FOREIGN KEY(service_id) REFERENCES services(id),
            FOREIGN KEY(practitioner_type_id) REFERENCES practitioner_types(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id BIGSERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL,
            client_number TEXT NOT NULL,
            name TEXT NOT NULL,
            client_type TEXT,
            email TEXT,
            phone TEXT,
            address TEXT,
            reference TEXT,
            notes TEXT,
            billing_type TEXT DEFAULT 'General',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(org_id, client_number),
            FOREIGN KEY(org_id) REFERENCES organizations(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS client_sla_rates (
            id BIGSERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL,
            client_id INTEGER NOT NULL,
            practitioner_type_id INTEGER NOT NULL,
            rate DOUBLE PRECISION DEFAULT 0,
            unit TEXT DEFAULT 'Hour',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            UNIQUE(org_id, client_id, practitioner_type_id),
            FOREIGN KEY(org_id) REFERENCES organizations(id),
            FOREIGN KEY(client_id) REFERENCES clients(id),
            FOREIGN KEY(practitioner_type_id) REFERENCES practitioner_types(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS matter_types (
            id BIGSERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(org_id, name),
            FOREIGN KEY(org_id) REFERENCES organizations(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS matters (
            id BIGSERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL,
            client_id INTEGER NOT NULL,
            matter_type_id INTEGER,
            matter_number TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT DEFAULT 'Open',
            priority TEXT DEFAULT 'Normal',
            particulars TEXT,
            opened_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP,
            UNIQUE(org_id, matter_number),
            FOREIGN KEY(org_id) REFERENCES organizations(id),
            FOREIGN KEY(client_id) REFERENCES clients(id),
            FOREIGN KEY(matter_type_id) REFERENCES matter_types(id),
            FOREIGN KEY(opened_by) REFERENCES users(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id BIGSERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL,
            matter_id INTEGER NOT NULL,
            task_number TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            practitioner_type_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            tat_days INTEGER DEFAULT 5,
            due_date TEXT,
            status TEXT DEFAULT 'Pending',
            billing_status TEXT DEFAULT 'Unbilled',
            billable_quantity DOUBLE PRECISION,
            rate_applied DOUBLE PRECISION,
            billable_amount DOUBLE PRECISION,
            disbursement_amount DOUBLE PRECISION DEFAULT 0,
            completion_notes TEXT,
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(org_id, task_number),
            FOREIGN KEY(org_id) REFERENCES organizations(id),
            FOREIGN KEY(matter_id) REFERENCES matters(id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(service_id) REFERENCES services(id),
            FOREIGN KEY(practitioner_type_id) REFERENCES practitioner_types(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id BIGSERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL,
            matter_id INTEGER,
            client_id INTEGER,
            document_name TEXT,
            file_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(org_id) REFERENCES organizations(id),
            FOREIGN KEY(matter_id) REFERENCES matters(id),
            FOREIGN KEY(client_id) REFERENCES clients(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS call_logs (
            id BIGSERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL,
            timestamp TEXT,
            phone TEXT,
            client_name TEXT,
            details TEXT,
            status TEXT,
            FOREIGN KEY(org_id) REFERENCES organizations(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS email_queue (
            id BIGSERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL,
            recipient TEXT,
            subject TEXT,
            body TEXT,
            timestamp TEXT,
            status TEXT DEFAULT 'Queued',
            FOREIGN KEY(org_id) REFERENCES organizations(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS manual_billing_items (
            id BIGSERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL,
            client_id INTEGER NOT NULL,
            matter_id INTEGER NOT NULL,
            user_id INTEGER,
            practitioner_type_id INTEGER,
            description TEXT NOT NULL,
            billing_date TEXT,
            quantity DOUBLE PRECISION DEFAULT 1,
            rate DOUBLE PRECISION DEFAULT 0,
            total_amount DOUBLE PRECISION DEFAULT 0,
            notes TEXT,
            billing_status TEXT DEFAULT 'Unbilled',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(org_id) REFERENCES organizations(id),
            FOREIGN KEY(client_id) REFERENCES clients(id),
            FOREIGN KEY(matter_id) REFERENCES matters(id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(practitioner_type_id) REFERENCES practitioner_types(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id BIGSERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL,
            client_id INTEGER NOT NULL,
            invoice_number TEXT NOT NULL,
            invoice_date TEXT NOT NULL,
            due_date TEXT,
            status TEXT DEFAULT 'Draft',
            subtotal DOUBLE PRECISION DEFAULT 0,
            fees_subtotal DOUBLE PRECISION DEFAULT 0,
            disbursement_total DOUBLE PRECISION DEFAULT 0,
            vat_rate DOUBLE PRECISION DEFAULT 0,
            vat_amount DOUBLE PRECISION DEFAULT 0,
            total_amount DOUBLE PRECISION DEFAULT 0,
            notes TEXT,
            payment_terms TEXT,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            issued_at TIMESTAMP,
            paid_at TIMESTAMP,
            UNIQUE(org_id, invoice_number),
            FOREIGN KEY(org_id) REFERENCES organizations(id),
            FOREIGN KEY(client_id) REFERENCES clients(id),
            FOREIGN KEY(created_by) REFERENCES users(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS invoice_items (
            id BIGSERIAL PRIMARY KEY,
            invoice_id INTEGER NOT NULL,
            org_id INTEGER NOT NULL,
            client_id INTEGER NOT NULL,
            matter_id INTEGER,
            task_id INTEGER,
            manual_billing_id INTEGER,
            item_type TEXT NOT NULL,
            description TEXT,
            practitioner_type_id INTEGER,
            billing_date TEXT,
            quantity DOUBLE PRECISION DEFAULT 1,
            rate DOUBLE PRECISION DEFAULT 0,
            amount DOUBLE PRECISION DEFAULT 0,
            disbursement_amount DOUBLE PRECISION DEFAULT 0,
            FOREIGN KEY(invoice_id) REFERENCES invoices(id),
            FOREIGN KEY(org_id) REFERENCES organizations(id),
            FOREIGN KEY(client_id) REFERENCES clients(id),
            FOREIGN KEY(matter_id) REFERENCES matters(id),
            FOREIGN KEY(task_id) REFERENCES tasks(id),
            FOREIGN KEY(manual_billing_id) REFERENCES manual_billing_items(id),
            FOREIGN KEY(practitioner_type_id) REFERENCES practitioner_types(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS invoice_payments (
            id BIGSERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL,
            invoice_id INTEGER NOT NULL,
            payment_date TEXT,
            amount DOUBLE PRECISION NOT NULL,
            reference TEXT,
            notes TEXT,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(org_id) REFERENCES organizations(id),
            FOREIGN KEY(invoice_id) REFERENCES invoices(id),
            FOREIGN KEY(created_by) REFERENCES users(id)
        )
    """)

    # Backward-compatible migrations for existing Nexora databases.
    _add_column_if_missing(conn, "tasks", "disbursement_amount", "REAL DEFAULT 0")
    _add_column_if_missing(conn, "invoice_items", "disbursement_amount", "REAL DEFAULT 0")
    _add_column_if_missing(conn, "invoices", "fees_subtotal", "REAL DEFAULT 0")
    _add_column_if_missing(conn, "invoices", "disbursement_total", "REAL DEFAULT 0")

    # Firm lifecycle: additive, non-destructive migrations.
    _add_column_if_missing(conn, "organizations", "status", "TEXT DEFAULT 'ACTIVE'")
    _add_column_if_missing(conn, "organizations", "approved_at", "TIMESTAMP")
    _add_column_if_missing(conn, "organizations", "approved_by", "BIGINT")
    _add_column_if_missing(conn, "organizations", "rejection_reason", "TEXT")
    _add_column_if_missing(conn, "organizations", "suspended_at", "TIMESTAMP")
    conn.execute("""
        UPDATE organizations
        SET status='ACTIVE'
        WHERE status IS NULL OR TRIM(status)=''
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS super_admin_users (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            cell TEXT UNIQUE NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login_at TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS platform_subscriptions (
            id BIGSERIAL PRIMARY KEY,
            org_id BIGINT UNIQUE NOT NULL,
            package_name TEXT DEFAULT 'Standard',
            monthly_fee DOUBLE PRECISION DEFAULT 0,
            status TEXT DEFAULT 'Trial',
            start_date TEXT,
            next_billing_date TEXT,
            notes TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(org_id) REFERENCES organizations(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS support_queries (
            id BIGSERIAL PRIMARY KEY,
            org_id BIGINT NOT NULL,
            user_id BIGINT,
            subject TEXT NOT NULL,
            description TEXT NOT NULL,
            priority TEXT DEFAULT 'Normal',
            status TEXT DEFAULT 'Open',
            admin_notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(org_id) REFERENCES organizations(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS platform_audit_log (
            id BIGSERIAL PRIMARY KEY,
            super_admin_id BIGINT,
            action TEXT NOT NULL,
            org_id BIGINT,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(super_admin_id) REFERENCES super_admin_users(id),
            FOREIGN KEY(org_id) REFERENCES organizations(id)
        )
    """)

    # Ensure both new and existing firms have Nexora's standard
    # matter types and services. Existing firm-defined records are
    # preserved because the inserts use each table's UNIQUE rule.
    org_rows = c.execute("SELECT id FROM organizations").fetchall()
    for org_row in org_rows:
        _seed_default_setup(conn, org_row["id"])

    conn.commit()
    conn.close()


# ============================================================
# FIRM / AUTH
# ============================================================

def register_new_firm(firm_name, admin_name, admin_cell):
    firm_name = str(firm_name or "").strip()
    admin_name = str(admin_name or "").strip()
    admin_cell = normalize_cell(admin_cell)

    if not firm_name:
        return False, "Firm name is required.", None

    if not admin_name:
        return False, "Administrator name is required.", None

    if not admin_cell:
        return False, "Administrator cellphone is required.", None

    conn = get_connection()

    try:
        if conn.execute("SELECT id FROM users WHERE cell=?", (admin_cell,)).fetchone():
            return False, "That cellphone number is already registered.", None

        firm_number = _next_firm_number(conn.cursor())

        cur = conn.execute("""
            INSERT INTO organizations(
                name,
                firm_number,
                registered_cell,
                status
            )
            VALUES (?, ?, ?, 'PENDING')
        """, (firm_name, firm_number, admin_cell))

        org_id = cur.lastrowid

        pt_cur = conn.execute("""
            INSERT INTO practitioner_types(org_id, name, description)
            VALUES (?, 'Director', 'Firm Director / Administrator')
        """, (org_id,))

        practitioner_type_id = pt_cur.lastrowid

        user_cur = conn.execute("""
            INSERT INTO users(
                org_id,
                firm_number,
                name,
                cell,
                role,
                practitioner_type_id,
                attorney_level,
                active
            )
            VALUES (?, ?, ?, ?, 'Admin', ?, 'Director', 1)
        """, (
            org_id,
            firm_number,
            admin_name,
            admin_cell,
            practitioner_type_id
        ))

        user_id = user_cur.lastrowid

        # New firms start with Nexora's standard matter types and services.
        # The Admin can still add firm-specific options later.
        _seed_default_setup(conn, org_id)

        conn.commit()

        return True, "Firm registered successfully.", {
            "id": user_id,
            "user_id": user_id,
            "org_id": org_id,
            "firm_number": firm_number,
            "firm_name": firm_name,
            "name": admin_name,
            "cell": admin_cell,
            "role": "Admin",
            "attorney_level": "Director",
            "practitioner_type": "Director",
            "practitioner_type_id": practitioner_type_id,
            "organization_status": "PENDING",
        }

    except Exception as e:
        conn.rollback()
        return False, str(e), None

    finally:
        conn.close()


def authenticate_user(firm_number, cell):
    conn = get_connection()

    row = conn.execute("""
        SELECT
            u.*,
            o.name AS firm_name,
            o.status AS organization_status,
            pt.name AS practitioner_type
        FROM users u
        JOIN organizations o ON o.id=u.org_id
        LEFT JOIN practitioner_types pt ON pt.id=u.practitioner_type_id
        WHERE u.firm_number=?
          AND u.cell=?
          AND u.active=1
        LIMIT 1
    """, (
        normalize_firm_number(firm_number),
        normalize_cell(cell)
    )).fetchone()

    conn.close()

    if not row:
        return None

    data = dict(row)
    data["attorney_level"] = data.get("practitioner_type") or data.get("attorney_level")
    return data


def get_user(user_id):
    conn = get_connection()

    row = conn.execute("""
        SELECT
            u.*,
            o.name AS firm_name,
            o.status AS organization_status,
            pt.name AS practitioner_type
        FROM users u
        JOIN organizations o ON o.id=u.org_id
        LEFT JOIN practitioner_types pt ON pt.id=u.practitioner_type_id
        WHERE u.id=?
    """, (user_id,)).fetchone()

    conn.close()

    if not row:
        return None

    data = dict(row)
    data["attorney_level"] = data.get("practitioner_type") or data.get("attorney_level")
    return data


def get_organization(org_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM organizations WHERE id=?", (org_id,)).fetchone()
    conn.close()
    return row_to_dict(row)


def update_organization_details(
    org_id,
    name=None,
    address=None,
    email=None,
    phone=None,
    website=None,
    registration_number=None,
    vat_number=None,
    bank_name=None,
    bank_account_name=None,
    bank_account_number=None,
    bank_branch_code=None,
    invoice_payment_terms=None
):
    conn = get_connection()

    conn.execute("""
        UPDATE organizations
        SET name=?,
            address=?,
            email=?,
            phone=?,
            website=?,
            registration_number=?,
            vat_number=?,
            bank_name=?,
            bank_account_name=?,
            bank_account_number=?,
            bank_branch_code=?,
            invoice_payment_terms=?
        WHERE id=?
    """, (
        name,
        address,
        email,
        phone,
        website,
        registration_number,
        vat_number,
        bank_name,
        bank_account_name,
        bank_account_number,
        bank_branch_code,
        invoice_payment_terms,
        org_id
    ))

    conn.commit()
    conn.close()


# ============================================================
# PRACTITIONER TYPES / USERS
# ============================================================

def add_practitioner_type(org_id, name, description=""):
    name = str(name or "").strip()
    if not name:
        raise ValueError("Practitioner type is required.")

    conn = get_connection()
    try:
        cur = conn.execute("""
            INSERT INTO practitioner_types(org_id, name, description)
            VALUES (?, ?, ?)
        """, (org_id, name, description))
        conn.commit()
        return cur.lastrowid
    except IntegrityError:
        conn.rollback()
        raise ValueError(f"Practitioner type '{name}' already exists.")
    finally:
        conn.close()


def list_practitioner_types(org_id, active_only=True):
    conn = get_connection()
    sql = "SELECT * FROM practitioner_types WHERE org_id=?"
    params = [org_id]
    if active_only:
        sql += " AND active=1"
    sql += " ORDER BY name"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def create_user_with_credentials(
    org_id,
    name,
    cell,
    role="Lawyer",
    practitioner_type_name=None,
    practitioner_type_id=None
):
    name = str(name or "").strip()
    cell = normalize_cell(cell)

    if not name or not cell:
        raise ValueError("Name and cellphone number are required.")

    conn = get_connection()

    try:
        org = conn.execute(
            "SELECT id, firm_number FROM organizations WHERE id=?",
            (org_id,)
        ).fetchone()

        if not org:
            raise ValueError("Firm not found.")

        if conn.execute("SELECT id FROM users WHERE cell=?", (cell,)).fetchone():
            raise ValueError("That cellphone number is already registered.")

        if practitioner_type_id is None and practitioner_type_name:
            pt = conn.execute("""
                SELECT id
                FROM practitioner_types
                WHERE org_id=? AND name=? AND active=1
            """, (org_id, practitioner_type_name)).fetchone()
            if pt:
                practitioner_type_id = pt["id"]

        if practitioner_type_id is None:
            raise ValueError("Practitioner type is required.")

        pt = conn.execute("""
            SELECT name
            FROM practitioner_types
            WHERE id=? AND org_id=? AND active=1
        """, (practitioner_type_id, org_id)).fetchone()

        if not pt:
            raise ValueError("Practitioner type does not belong to this firm.")

        cur = conn.execute("""
            INSERT INTO users(
                org_id,
                firm_number,
                name,
                cell,
                role,
                practitioner_type_id,
                attorney_level,
                active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            org_id,
            org["firm_number"],
            name,
            cell,
            role,
            practitioner_type_id,
            pt["name"]
        ))

        conn.commit()
        return cur.lastrowid

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def list_users(org_id):
    conn = get_connection()

    rows = conn.execute("""
        SELECT
            u.*,
            pt.name AS practitioner_type
        FROM users u
        LEFT JOIN practitioner_types pt ON pt.id=u.practitioner_type_id
        WHERE u.org_id=?
        ORDER BY CASE WHEN u.role='Admin' THEN 0 ELSE 1 END, u.name
    """, (org_id,)).fetchall()

    conn.close()
    return rows_to_dicts(rows)


# ============================================================
# SERVICES / GENERAL FEES
# ============================================================

def add_service(org_id, name, description="", default_unit="Hour"):
    name = str(name or "").strip()
    if not name:
        raise ValueError("Service name is required.")

    conn = get_connection()
    try:
        cur = conn.execute("""
            INSERT INTO services(org_id, name, description, default_unit)
            VALUES (?, ?, ?, ?)
        """, (org_id, name, description, default_unit))
        conn.commit()
        return cur.lastrowid
    except IntegrityError:
        conn.rollback()
        raise ValueError(f"Service '{name}' already exists.")
    finally:
        conn.close()


def list_services(org_id, active_only=True):
    conn = get_connection()
    sql = "SELECT * FROM services WHERE org_id=?"
    params = [org_id]
    if active_only:
        sql += " AND active=1"
    sql += " ORDER BY name"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def save_service_fee(org_id, service_id, practitioner_type_id, fee, unit="Hour"):
    conn = get_connection()

    try:
        conn.execute("""
            INSERT INTO service_fees(
                org_id,
                service_id,
                practitioner_type_id,
                fee,
                unit
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(org_id, service_id, practitioner_type_id)
            DO UPDATE SET
                fee=excluded.fee,
                unit=excluded.unit,
                active=1,
                updated_at=CURRENT_TIMESTAMP
        """, (
            org_id,
            service_id,
            practitioner_type_id,
            float(fee),
            unit
        ))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_service_fees(org_id):
    conn = get_connection()

    rows = conn.execute("""
        SELECT
            sf.*,
            s.name AS service_name,
            pt.name AS practitioner_type
        FROM service_fees sf
        JOIN services s ON s.id=sf.service_id
        JOIN practitioner_types pt ON pt.id=sf.practitioner_type_id
        WHERE sf.org_id=? AND sf.active=1
        ORDER BY s.name, pt.name
    """, (org_id,)).fetchall()

    conn.close()
    return rows_to_dicts(rows)


# ============================================================
# CLIENTS / SLA
# ============================================================

def create_client(
    org_id,
    name,
    client_type="Individual",
    email="",
    phone="",
    address="",
    reference="",
    notes="",
    billing_type="General",
    sla_rates=None
):
    name = str(name or "").strip()
    if not name:
        raise ValueError("Client name is required.")

    conn = get_connection()

    try:
        org = conn.execute(
            "SELECT firm_number, name FROM organizations WHERE id=?",
            (org_id,)
        ).fetchone()

        if not org:
            raise ValueError("Firm not found.")

        year = date.today().year
        client_number = _next_number(
            conn.cursor(),
            "clients",
            "client_number",
            org_id,
            f"{org['firm_number']}-CLI-{year}-",
            4
        )

        cur = conn.execute("""
            INSERT INTO clients(
                org_id,
                client_number,
                name,
                client_type,
                email,
                phone,
                address,
                reference,
                notes,
                billing_type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            org_id,
            client_number,
            name,
            client_type,
            email,
            phone,
            address,
            reference,
            notes,
            billing_type
        ))

        client_id = cur.lastrowid

        if billing_type == "SLA":
            practitioner_types = conn.execute("""
                SELECT id FROM practitioner_types
                WHERE org_id=? AND active=1
            """, (org_id,)).fetchall()

            if not practitioner_types:
                raise ValueError(
                    "The firm must define Practitioner Types before registering an SLA client."
                )

            if not sla_rates:
                raise ValueError(
                    "SLA rates are required for every practitioner type."
                )

            required_ids = {int(r["id"]) for r in practitioner_types}
            supplied_ids = {int(k) for k in sla_rates.keys()}

            if required_ids != supplied_ids:
                raise ValueError(
                    "Enter an SLA rate for every active practitioner type."
                )

            for practitioner_type_id, rate in sla_rates.items():
                rate = float(rate)
                if rate <= 0:
                    raise ValueError(
                        "Every SLA practitioner type must have a rate greater than zero."
                    )

                conn.execute("""
                    INSERT INTO client_sla_rates(
                        org_id,
                        client_id,
                        practitioner_type_id,
                        rate,
                        unit
                    )
                    VALUES (?, ?, ?, ?, 'Hour')
                """, (
                    org_id,
                    client_id,
                    practitioner_type_id,
                    rate
                ))

        conn.commit()
        return client_id, client_number

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def list_clients(org_id):
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM clients
        WHERE org_id=?
        ORDER BY id DESC
    """, (org_id,)).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def get_client(org_id, client_id):
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM clients
        WHERE id=? AND org_id=?
    """, (client_id, org_id)).fetchone()
    conn.close()
    return row_to_dict(row)


def list_client_sla_rates(org_id, client_id):
    conn = get_connection()

    rows = conn.execute("""
        SELECT
            csr.*,
            pt.name AS practitioner_type
        FROM client_sla_rates csr
        JOIN practitioner_types pt ON pt.id=csr.practitioner_type_id
        WHERE csr.org_id=? AND csr.client_id=?
        ORDER BY pt.name
    """, (org_id, client_id)).fetchall()

    conn.close()
    return rows_to_dicts(rows)


# ============================================================
# MATTER TYPES / MATTERS
# ============================================================

def add_matter_type(org_id, name):
    name = str(name or "").strip()
    if not name:
        raise ValueError("Matter type is required.")

    conn = get_connection()
    try:
        cur = conn.execute("""
            INSERT INTO matter_types(org_id, name)
            VALUES (?, ?)
        """, (org_id, name))
        conn.commit()
        return cur.lastrowid
    except IntegrityError:
        conn.rollback()
        raise ValueError(f"Matter type '{name}' already exists.")
    finally:
        conn.close()


def list_matter_types(org_id, active_only=True):
    conn = get_connection()
    sql = "SELECT * FROM matter_types WHERE org_id=?"
    params = [org_id]
    if active_only:
        sql += " AND active=1"
    sql += " ORDER BY name"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def create_matter(
    org_id,
    client_id,
    matter_type_id,
    title,
    priority,
    particulars,
    opened_by
):
    conn = get_connection()

    try:
        org = conn.execute(
            "SELECT firm_number FROM organizations WHERE id=?",
            (org_id,)
        ).fetchone()

        if not org:
            raise ValueError("Firm not found.")

        if not conn.execute(
            "SELECT id FROM clients WHERE id=? AND org_id=?",
            (client_id, org_id)
        ).fetchone():
            raise ValueError("Client does not belong to this firm.")

        if not conn.execute("""
            SELECT id FROM matter_types
            WHERE id=? AND org_id=? AND active=1
        """, (matter_type_id, org_id)).fetchone():
            raise ValueError("Matter type does not belong to this firm.")

        year = date.today().year
        matter_number = _next_number(
            conn.cursor(),
            "matters",
            "matter_number",
            org_id,
            f"{org['firm_number']}-MAT-{year}-",
            4
        )

        cur = conn.execute("""
            INSERT INTO matters(
                org_id,
                client_id,
                matter_type_id,
                matter_number,
                title,
                status,
                priority,
                particulars,
                opened_by
            )
            VALUES (?, ?, ?, ?, ?, 'Open', ?, ?, ?)
        """, (
            org_id,
            client_id,
            matter_type_id,
            matter_number,
            title,
            priority,
            particulars,
            opened_by
        ))

        conn.commit()
        return cur.lastrowid, matter_number

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def list_matters(org_id, client_id=None):
    conn = get_connection()

    sql = """
        SELECT
            m.*,
            c.name AS client_name,
            c.client_number,
            mt.name AS matter_type
        FROM matters m
        JOIN clients c ON c.id=m.client_id
        LEFT JOIN matter_types mt ON mt.id=m.matter_type_id
        WHERE m.org_id=?
    """
    params = [org_id]

    if client_id is not None:
        sql += " AND m.client_id=?"
        params.append(client_id)

    sql += " ORDER BY m.id DESC"

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def close_matter(org_id, matter_id):
    """Close a matter only when it has no outstanding tasks."""
    conn = get_connection()
    try:
        matter = conn.execute(
            "SELECT id, status FROM matters WHERE id=? AND org_id=?",
            (matter_id, org_id)
        ).fetchone()
        if not matter:
            raise ValueError("Matter not found.")

        open_tasks = conn.execute("""
            SELECT COUNT(*) AS count
            FROM tasks
            WHERE matter_id=? AND org_id=? AND status!='Complete'
        """, (matter_id, org_id)).fetchone()["count"]

        if int(open_tasks or 0) > 0:
            raise ValueError(
                f"This matter still has {open_tasks} open task(s). Complete them before closing the matter."
            )

        conn.execute("""
            UPDATE matters
            SET status='Closed', closed_at=CURRENT_TIMESTAMP
            WHERE id=? AND org_id=?
        """, (matter_id, org_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reopen_matter(org_id, matter_id):
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE matters
            SET status='Open', closed_at=NULL
            WHERE id=? AND org_id=?
        """, (matter_id, org_id))
        if cur.rowcount == 0:
            raise ValueError("Matter not found.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_matter_status(matter_id, status):
    """Backward-compatible status helper used by older app versions."""
    conn = get_connection()
    if status == "Closed":
        conn.execute("""
            UPDATE matters SET status='Closed', closed_at=CURRENT_TIMESTAMP WHERE id=?
        """, (matter_id,))
    else:
        conn.execute("""
            UPDATE matters SET status=?, closed_at=NULL WHERE id=?
        """, (status, matter_id))
    conn.commit()
    conn.close()


# ============================================================
# TASKS
# ============================================================

def create_task(
    org_id,
    matter_id,
    user_id,
    service_id,
    title,
    tat_days,
    due_date
):
    conn = get_connection()

    try:
        org = conn.execute(
            "SELECT firm_number FROM organizations WHERE id=?",
            (org_id,)
        ).fetchone()

        if not org:
            raise ValueError("Firm not found.")

        if not conn.execute(
            "SELECT id FROM matters WHERE id=? AND org_id=?",
            (matter_id, org_id)
        ).fetchone():
            raise ValueError("Matter does not belong to this firm.")

        user = conn.execute("""
            SELECT id, practitioner_type_id
            FROM users
            WHERE id=? AND org_id=? AND active=1
        """, (user_id, org_id)).fetchone()

        if not user:
            raise ValueError("Practitioner does not belong to this firm.")

        if not user["practitioner_type_id"]:
            raise ValueError("Practitioner type is not configured.")

        if not conn.execute("""
            SELECT id
            FROM services
            WHERE id=? AND org_id=? AND active=1
        """, (service_id, org_id)).fetchone():
            raise ValueError("Task Type / Service does not belong to this firm.")

        year = date.today().year
        task_number = _next_number(
            conn.cursor(),
            "tasks",
            "task_number",
            org_id,
            f"{org['firm_number']}-TSK-{year}-",
            6
        )

        cur = conn.execute("""
            INSERT INTO tasks(
                org_id,
                matter_id,
                task_number,
                user_id,
                service_id,
                practitioner_type_id,
                title,
                tat_days,
                due_date,
                status,
                billing_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', 'Unbilled')
        """, (
            org_id,
            matter_id,
            task_number,
            user_id,
            service_id,
            user["practitioner_type_id"],
            title,
            tat_days,
            due_date
        ))

        conn.commit()
        return cur.lastrowid, task_number

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def list_tasks(org_id, matter_id=None, user_id=None):
    conn = get_connection()

    sql = """
        SELECT
            t.*,
            m.matter_number,
            m.title AS matter_title,
            m.client_id,
            c.name AS client_name,
            c.client_number,
            c.billing_type,
            u.name AS practitioner_name,
            pt.name AS practitioner_type,
            s.name AS service_name
        FROM tasks t
        JOIN matters m ON m.id=t.matter_id
        JOIN clients c ON c.id=m.client_id
        JOIN users u ON u.id=t.user_id
        JOIN practitioner_types pt ON pt.id=t.practitioner_type_id
        JOIN services s ON s.id=t.service_id
        WHERE t.org_id=?
    """
    params = [org_id]

    if matter_id is not None:
        sql += " AND t.matter_id=?"
        params.append(matter_id)

    if user_id is not None:
        sql += " AND t.user_id=?"
        params.append(user_id)

    sql += " ORDER BY t.id DESC"

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def complete_task(org_id, task_id, quantity, completion_notes, disbursement_amount=0):
    quantity = float(quantity)
    disbursement_amount = float(disbursement_amount or 0)

    if quantity <= 0:
        raise ValueError("Billable hours / units must be greater than zero.")

    if disbursement_amount < 0:
        raise ValueError("Disbursement amount cannot be negative.")

    if not str(completion_notes or "").strip():
        raise ValueError("Completion notes are required.")

    conn = get_connection()

    try:
        task = conn.execute("""
            SELECT
                t.*,
                m.client_id,
                c.billing_type,
                s.name AS service_name,
                pt.name AS practitioner_type
            FROM tasks t
            JOIN matters m ON m.id=t.matter_id
            JOIN clients c ON c.id=m.client_id
            JOIN services s ON s.id=t.service_id
            JOIN practitioner_types pt ON pt.id=t.practitioner_type_id
            WHERE t.id=? AND t.org_id=?
        """, (task_id, org_id)).fetchone()

        if not task:
            raise ValueError("Task not found.")

        if task["status"] == "Complete":
            raise ValueError("This task is already completed.")

        if task["billing_type"] == "SLA":
            rate_row = conn.execute("""
                SELECT rate, unit
                FROM client_sla_rates
                WHERE org_id=?
                  AND client_id=?
                  AND practitioner_type_id=?
            """, (
                org_id,
                task["client_id"],
                task["practitioner_type_id"]
            )).fetchone()

            if not rate_row:
                raise ValueError(
                    "No SLA rate is configured for this client and practitioner type."
                )

            rate = float(rate_row["rate"])
            rate_source = "Client SLA Rate"

        else:
            rate_row = conn.execute("""
                SELECT fee, unit
                FROM service_fees
                WHERE org_id=?
                  AND service_id=?
                  AND practitioner_type_id=?
                  AND active=1
            """, (
                org_id,
                task["service_id"],
                task["practitioner_type_id"]
            )).fetchone()

            if not rate_row:
                raise ValueError(
                    "No General fee is configured for this Task Type / Service and practitioner type."
                )

            rate = float(rate_row["fee"])
            rate_source = "General Fee Schedule"

        amount = quantity * rate

        conn.execute("""
            UPDATE tasks
            SET status='Complete',
                billable_quantity=?,
                rate_applied=?,
                billable_amount=?,
                disbursement_amount=?,
                completion_notes=?,
                completed_at=CURRENT_TIMESTAMP,
                billing_status='Unbilled'
            WHERE id=? AND org_id=?
        """, (
            quantity,
            rate,
            amount,
            disbursement_amount,
            str(completion_notes).strip(),
            task_id,
            org_id
        ))

        conn.commit()

        return {
            "quantity": quantity,
            "rate": rate,
            "amount": amount,
            "disbursement_amount": disbursement_amount,
            "line_total": amount + disbursement_amount,
            "billing_type": task["billing_type"],
            "rate_source": rate_source,
            "service_name": task["service_name"],
            "practitioner_type": task["practitioner_type"]
        }

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def list_unbilled_tasks(org_id, client_id=None):
    conn = get_connection()

    sql = """
        SELECT
            t.*,
            m.client_id,
            m.matter_number,
            c.name AS client_name,
            u.name AS practitioner_name,
            pt.name AS practitioner_type,
            s.name AS service_name
        FROM tasks t
        JOIN matters m ON m.id=t.matter_id
        JOIN clients c ON c.id=m.client_id
        JOIN users u ON u.id=t.user_id
        JOIN practitioner_types pt ON pt.id=t.practitioner_type_id
        JOIN services s ON s.id=t.service_id
        WHERE t.org_id=?
          AND t.status='Complete'
          AND t.billing_status='Unbilled'
    """
    params = [org_id]

    if client_id is not None:
        sql += " AND m.client_id=?"
        params.append(client_id)

    sql += " ORDER BY t.id"

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows_to_dicts(rows)


# ============================================================
# DOCUMENTS / COMMS
# ============================================================

def add_document(org_id, document_name, file_path, matter_id=None, client_id=None):
    conn = get_connection()
    cur = conn.execute("""
        INSERT INTO documents(
            org_id,
            matter_id,
            client_id,
            document_name,
            file_path
        )
        VALUES (?, ?, ?, ?, ?)
    """, (org_id, matter_id, client_id, document_name, file_path))
    conn.commit()
    conn.close()
    return cur.lastrowid


def get_documents_for_client(org_id, client_id):
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM documents
        WHERE org_id=? AND client_id=?
        ORDER BY id DESC
    """, (org_id, client_id)).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def get_documents_for_matter(org_id, matter_id):
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM documents
        WHERE org_id=? AND matter_id=?
        ORDER BY id DESC
    """, (org_id, matter_id)).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def save_call_log(org_id, timestamp, phone, client_name, details, status):
    conn = get_connection()
    conn.execute("""
        INSERT INTO call_logs(
            org_id,
            timestamp,
            phone,
            client_name,
            details,
            status
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (org_id, timestamp, phone, client_name, details, status))
    conn.commit()
    conn.close()


def fetch_call_logs(org_id):
    import pandas as pd
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM call_logs
        WHERE org_id=?
        ORDER BY id DESC
    """, (org_id,)).fetchall()
    conn.close()
    return pd.DataFrame(rows_to_dicts(rows))


def queue_email(org_id, recipient, subject, body, timestamp):
    conn = get_connection()
    conn.execute("""
        INSERT INTO email_queue(
            org_id,
            recipient,
            subject,
            body,
            timestamp,
            status
        )
        VALUES (?, ?, ?, ?, ?, 'Queued')
    """, (org_id, recipient, subject, body, timestamp))
    conn.commit()
    conn.close()


def fetch_email_logs(org_id):
    import pandas as pd
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM email_queue
        WHERE org_id=?
        ORDER BY id DESC
    """, (org_id,)).fetchall()
    conn.close()
    return pd.DataFrame(rows_to_dicts(rows))


# ============================================================
# INVOICES
# ============================================================

def get_client_unbilled_billing_items(org_id, client_id):
    return {
        "tasks": list_unbilled_tasks(org_id, client_id=client_id),
        "manual_items": []
    }


def create_invoice(
    org_id,
    client_id,
    invoice_date,
    due_date,
    created_by,
    notes="",
    vat_rate=0,
    payment_terms=""
):
    conn = get_connection()

    try:
        org = conn.execute(
            "SELECT firm_number FROM organizations WHERE id=?",
            (org_id,)
        ).fetchone()

        if not org:
            raise ValueError("Firm not found.")

        if not conn.execute(
            "SELECT id FROM clients WHERE id=? AND org_id=?",
            (client_id, org_id)
        ).fetchone():
            raise ValueError("Client does not belong to this firm.")

        year = date.today().year
        invoice_number = _next_number(
            conn.cursor(),
            "invoices",
            "invoice_number",
            org_id,
            f"{org['firm_number']}-INV-{year}-",
            4
        )

        cur = conn.execute("""
            INSERT INTO invoices(
                org_id,
                client_id,
                invoice_number,
                invoice_date,
                due_date,
                status,
                subtotal,
                fees_subtotal,
                disbursement_total,
                vat_rate,
                vat_amount,
                total_amount,
                notes,
                payment_terms,
                created_by
            )
            VALUES (?, ?, ?, ?, ?, 'Draft', 0, 0, 0, ?, 0, 0, ?, ?, ?)
        """, (
            org_id,
            client_id,
            invoice_number,
            invoice_date,
            due_date,
            vat_rate,
            notes,
            payment_terms,
            created_by
        ))

        conn.commit()
        return cur.lastrowid

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def add_task_to_invoice(org_id, invoice_id, task_id):
    conn = get_connection()

    try:
        invoice = conn.execute("""
            SELECT * FROM invoices
            WHERE id=? AND org_id=?
        """, (invoice_id, org_id)).fetchone()

        if not invoice:
            raise ValueError("Invoice not found.")

        if invoice["status"] != "Draft":
            raise ValueError("Only Draft invoices can be modified.")

        task = conn.execute("""
            SELECT
                t.*,
                m.client_id
            FROM tasks t
            JOIN matters m ON m.id=t.matter_id
            WHERE t.id=? AND t.org_id=?
        """, (task_id, org_id)).fetchone()

        if not task:
            raise ValueError("Task not found.")

        if int(task["client_id"]) != int(invoice["client_id"]):
            raise ValueError("Task belongs to another client.")

        if task["status"] != "Complete":
            raise ValueError("Only completed tasks can be invoiced.")

        if task["billing_status"] != "Unbilled":
            raise ValueError("Task is already billed.")

        conn.execute("""
            INSERT INTO invoice_items(
                invoice_id,
                org_id,
                client_id,
                matter_id,
                task_id,
                manual_billing_id,
                item_type,
                description,
                practitioner_type_id,
                billing_date,
                quantity,
                rate,
                amount,
                disbursement_amount
            )
            VALUES (?, ?, ?, ?, ?, NULL, 'Task', ?, ?, ?, ?, ?, ?, ?)
        """, (
            invoice_id,
            org_id,
            invoice["client_id"],
            task["matter_id"],
            task_id,
            task["title"],
            task["practitioner_type_id"],
            task["completed_at"] or str(date.today()),
            task["billable_quantity"],
            task["rate_applied"],
            task["billable_amount"],
            task["disbursement_amount"] or 0
        ))

        conn.execute("""
            UPDATE tasks
            SET billing_status='Billed'
            WHERE id=? AND org_id=? AND billing_status='Unbilled'
        """, (task_id, org_id))

        recalculate_invoice_totals(org_id, invoice_id, connection=conn)
        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def recalculate_invoice_totals(org_id, invoice_id, connection=None):
    own = False
    if connection is None:
        connection = get_connection()
        own = True

    try:
        invoice = connection.execute("""
            SELECT vat_rate FROM invoices
            WHERE id=? AND org_id=?
        """, (invoice_id, org_id)).fetchone()

        if not invoice:
            raise ValueError("Invoice not found.")

        sums = connection.execute("""
            SELECT
                COALESCE(SUM(amount), 0) AS fees_subtotal,
                COALESCE(SUM(disbursement_amount), 0) AS disbursement_total
            FROM invoice_items
            WHERE invoice_id=? AND org_id=?
        """, (invoice_id, org_id)).fetchone()

        fees_subtotal = float(sums["fees_subtotal"] or 0)
        disbursement_total = float(sums["disbursement_total"] or 0)
        subtotal = fees_subtotal + disbursement_total

        vat_rate = float(invoice["vat_rate"] or 0)
        vat_amount = subtotal * vat_rate / 100
        total_amount = subtotal + vat_amount

        connection.execute("""
            UPDATE invoices
            SET subtotal=?,
                fees_subtotal=?,
                disbursement_total=?,
                vat_amount=?,
                total_amount=?
            WHERE id=? AND org_id=?
        """, (
            subtotal,
            fees_subtotal,
            disbursement_total,
            vat_amount,
            total_amount,
            invoice_id,
            org_id
        ))

        if own:
            connection.commit()

        return {
            "subtotal": subtotal,
            "fees_subtotal": fees_subtotal,
            "disbursement_total": disbursement_total,
            "vat_rate": vat_rate,
            "vat_amount": vat_amount,
            "total_amount": total_amount
        }

    finally:
        if own:
            connection.close()


def list_invoices(org_id):
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            i.*,
            c.name AS client_name,
            c.client_number
        FROM invoices i
        JOIN clients c ON c.id=i.client_id
        WHERE i.org_id=?
        ORDER BY i.id DESC
    """, (org_id,)).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def get_invoice(org_id, invoice_id):
    conn = get_connection()
    row = conn.execute("""
        SELECT
            i.*,
            c.name AS client_name,
            c.client_number
        FROM invoices i
        JOIN clients c ON c.id=i.client_id
        WHERE i.id=? AND i.org_id=?
    """, (invoice_id, org_id)).fetchone()
    conn.close()
    return row_to_dict(row)


def list_invoice_items(org_id, invoice_id):
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            ii.*,
            m.matter_number,
            pt.name AS practitioner_type
        FROM invoice_items ii
        LEFT JOIN matters m ON m.id=ii.matter_id
        LEFT JOIN practitioner_types pt ON pt.id=ii.practitioner_type_id
        WHERE ii.invoice_id=? AND ii.org_id=?
        ORDER BY ii.id
    """, (invoice_id, org_id)).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def issue_invoice(org_id, invoice_id):
    conn = get_connection()

    try:
        invoice = conn.execute("""
            SELECT * FROM invoices
            WHERE id=? AND org_id=?
        """, (invoice_id, org_id)).fetchone()

        if not invoice:
            raise ValueError("Invoice not found.")

        if invoice["status"] != "Draft":
            raise ValueError("Only Draft invoices can be issued.")

        count = conn.execute("""
            SELECT COUNT(*) AS count
            FROM invoice_items
            WHERE invoice_id=? AND org_id=?
        """, (invoice_id, org_id)).fetchone()["count"]

        if count == 0:
            raise ValueError("Cannot issue an invoice with no items.")

        conn.execute("""
            UPDATE invoices
            SET status='Issued', issued_at=CURRENT_TIMESTAMP
            WHERE id=? AND org_id=?
        """, (invoice_id, org_id))

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def void_invoice(org_id, invoice_id):
    conn = get_connection()

    try:
        invoice = conn.execute("""
            SELECT status FROM invoices
            WHERE id=? AND org_id=?
        """, (invoice_id, org_id)).fetchone()

        if not invoice:
            raise ValueError("Invoice not found.")

        if invoice["status"] == "Paid":
            raise ValueError("A Paid invoice cannot be voided.")

        if invoice["status"] == "Void":
            return

        conn.execute("""
            UPDATE tasks
            SET billing_status='Unbilled'
            WHERE id IN (
                SELECT task_id
                FROM invoice_items
                WHERE invoice_id=?
                  AND org_id=?
                  AND task_id IS NOT NULL
            )
            AND org_id=?
        """, (invoice_id, org_id, org_id))

        conn.execute("""
            UPDATE invoices
            SET status='Void'
            WHERE id=? AND org_id=?
        """, (invoice_id, org_id))

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def add_invoice_payment(
    org_id,
    invoice_id,
    amount,
    payment_date,
    reference="",
    notes="",
    created_by=None
):
    amount = float(amount)

    if amount <= 0:
        raise ValueError("Payment amount must be greater than zero.")

    conn = get_connection()

    try:
        invoice = conn.execute("""
            SELECT * FROM invoices
            WHERE id=? AND org_id=?
        """, (invoice_id, org_id)).fetchone()

        if not invoice:
            raise ValueError("Invoice not found.")

        if invoice["status"] == "Void":
            raise ValueError("Cannot pay a Void invoice.")

        conn.execute("""
            INSERT INTO invoice_payments(
                org_id,
                invoice_id,
                payment_date,
                amount,
                reference,
                notes,
                created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            org_id,
            invoice_id,
            payment_date,
            amount,
            reference,
            notes,
            created_by
        ))

        paid = float(
            conn.execute("""
                SELECT COALESCE(SUM(amount), 0) AS paid
                FROM invoice_payments
                WHERE invoice_id=? AND org_id=?
            """, (invoice_id, org_id)).fetchone()["paid"] or 0
        )

        status = (
            "Paid"
            if paid >= float(invoice["total_amount"] or 0)
            else "Partially Paid"
        )

        conn.execute("""
            UPDATE invoices
            SET status=?,
                paid_at=CASE
                    WHEN ?='Paid'
                    THEN CURRENT_TIMESTAMP
                    ELSE paid_at
                END
            WHERE id=? AND org_id=?
        """, (status, status, invoice_id, org_id))

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def get_invoice_amount_paid(org_id, invoice_id):
    conn = get_connection()
    row = conn.execute("""
        SELECT COALESCE(SUM(amount), 0) AS paid
        FROM invoice_payments
        WHERE org_id=? AND invoice_id=?
    """, (org_id, invoice_id)).fetchone()
    conn.close()
    return float(row["paid"] or 0)


def get_billing_summary(org_id):
    conn = get_connection()

    result = {}

    result["unbilled_tasks"] = conn.execute("""
        SELECT COUNT(*) AS count
        FROM tasks
        WHERE org_id=? AND status='Complete' AND billing_status='Unbilled'
    """, (org_id,)).fetchone()["count"]

    for key, status in [
        ("draft_invoices", "Draft"),
        ("issued_invoices", "Issued"),
        ("paid_invoices", "Paid")
    ]:
        result[key] = conn.execute("""
            SELECT COUNT(*) AS count
            FROM invoices
            WHERE org_id=? AND status=?
        """, (org_id, status)).fetchone()["count"]

    result["total_invoiced"] = float(
        conn.execute("""
            SELECT COALESCE(SUM(total_amount), 0) AS total
            FROM invoices
            WHERE org_id=? AND status!='Void'
        """, (org_id,)).fetchone()["total"] or 0
    )

    result["total_paid"] = float(
        conn.execute("""
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM invoice_payments
            WHERE org_id=?
        """, (org_id,)).fetchone()["total"] or 0
    )

    result["total_outstanding"] = result["total_invoiced"] - result["total_paid"]

    conn.close()
    return result


if __name__ == "__main__":
    init_db()
    print("Nexora database ready:")
    print(os.path.abspath(DB_NAME))


# ============================================================
# PLATFORM / SUPER ADMIN (PostgreSQL)
# Ported from the tested SQLite implementation.
# ============================================================

def ensure_super_admin(name, cell):
    """Create or refresh the one platform-level Super Admin from secure config."""
    name = str(name or "Nexora Super Admin").strip()
    cell = normalize_cell(cell)
    if not cell:
        return None

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT * FROM super_admin_users WHERE cell=? LIMIT 1",
            (cell,)
        ).fetchone()

        if existing:
            conn.execute("""
                UPDATE super_admin_users
                SET name=?, active=1
                WHERE id=?
            """, (name, existing["id"]))
            conn.commit()
            return get_super_admin(existing["id"])

        cur = conn.execute("""
            INSERT INTO super_admin_users(name, cell, active)
            VALUES (?, ?, 1)
        """, (name, cell))
        conn.commit()
        return get_super_admin(cur.lastrowid)
    finally:
        conn.close()

def get_super_admin(super_admin_id):
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM super_admin_users
        WHERE id=? AND active=1
        LIMIT 1
    """, (super_admin_id,)).fetchone()
    conn.close()
    return row_to_dict(row)

def get_super_admin_by_cell(cell):
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM super_admin_users
        WHERE cell=? AND active=1
        LIMIT 1
    """, (normalize_cell(cell),)).fetchone()
    conn.close()
    return row_to_dict(row)

def mark_super_admin_login(super_admin_id):
    conn = get_connection()
    conn.execute("""
        UPDATE super_admin_users
        SET last_login_at=?
        WHERE id=?
    """, (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        super_admin_id
    ))
    conn.commit()
    conn.close()

def _platform_audit(super_admin_id, action, org_id=None, details=""):
    conn = get_connection()
    conn.execute("""
        INSERT INTO platform_audit_log(
            super_admin_id, action, org_id, details
        )
        VALUES (?, ?, ?, ?)
    """, (super_admin_id, action, org_id, str(details or "")))
    conn.commit()
    conn.close()

def list_platform_audit(limit=200):
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            a.*,
            s.name AS super_admin_name,
            o.name AS firm_name,
            o.firm_number
        FROM platform_audit_log a
        LEFT JOIN super_admin_users s ON s.id=a.super_admin_id
        LEFT JOIN organizations o ON o.id=a.org_id
        ORDER BY a.id DESC
        LIMIT ?
    """, (int(limit),)).fetchall()
    conn.close()
    return rows_to_dicts(rows)

def list_platform_firms(status=None):
    conn = get_connection()
    sql = """
        SELECT
            o.*,
            (
                SELECT COUNT(*)
                FROM users u
                WHERE u.org_id=o.id AND u.active=1
            ) AS active_users,
            s.package_name,
            s.monthly_fee,
            s.status AS subscription_status,
            s.start_date,
            s.next_billing_date
        FROM organizations o
        LEFT JOIN platform_subscriptions s ON s.org_id=o.id
    """
    params = []
    if status:
        sql += " WHERE o.status=?"
        params.append(str(status).upper())
    sql += " ORDER BY o.id DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows_to_dicts(rows)

def get_platform_firm(org_id):
    conn = get_connection()
    row = conn.execute("""
        SELECT
            o.*,
            s.package_name,
            s.monthly_fee,
            s.status AS subscription_status,
            s.start_date,
            s.next_billing_date,
            s.notes AS subscription_notes
        FROM organizations o
        LEFT JOIN platform_subscriptions s ON s.org_id=o.id
        WHERE o.id=?
        LIMIT 1
    """, (org_id,)).fetchone()
    conn.close()
    return row_to_dict(row)

def list_platform_firm_users(org_id):
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            u.id,
            u.name,
            u.cell,
            u.role,
            u.active,
            u.created_at,
            pt.name AS practitioner_type
        FROM users u
        LEFT JOIN practitioner_types pt ON pt.id=u.practitioner_type_id
        WHERE u.org_id=?
        ORDER BY u.id
    """, (org_id,)).fetchall()
    conn.close()
    return rows_to_dicts(rows)

def approve_firm(org_id, super_admin_id):
    conn = get_connection()
    try:
        firm = conn.execute(
            "SELECT * FROM organizations WHERE id=?",
            (org_id,)
        ).fetchone()
        if not firm:
            raise ValueError("Firm not found.")

        conn.execute("""
            UPDATE organizations
            SET status='ACTIVE',
                approved_at=?,
                approved_by=?,
                rejection_reason=NULL,
                suspended_at=NULL
            WHERE id=?
        """, (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            super_admin_id,
            org_id
        ))

        conn.execute("""
            INSERT OR IGNORE INTO platform_subscriptions(
                org_id, package_name, monthly_fee, status, start_date
            )
            VALUES (?, 'Standard', 0, 'Trial', ?)
        """, (
            org_id,
            date.today().isoformat()
        ))

        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()

    _platform_audit(
        super_admin_id,
        "APPROVE_FIRM",
        org_id,
        f"Approved {firm['firm_number']} - {firm['name']}"
    )
    return get_platform_firm(org_id)

def reject_firm(org_id, super_admin_id, reason=""):
    conn = get_connection()
    firm = conn.execute(
        "SELECT * FROM organizations WHERE id=?",
        (org_id,)
    ).fetchone()
    if not firm:
        conn.close()
        raise ValueError("Firm not found.")

    conn.execute("""
        UPDATE organizations
        SET status='REJECTED',
            rejection_reason=?,
            suspended_at=NULL
        WHERE id=?
    """, (str(reason or "").strip(), org_id))
    conn.commit()
    conn.close()
    _platform_audit(super_admin_id, "REJECT_FIRM", org_id, reason)
    return get_platform_firm(org_id)

def suspend_firm(org_id, super_admin_id, reason=""):
    conn = get_connection()
    firm = conn.execute(
        "SELECT * FROM organizations WHERE id=?",
        (org_id,)
    ).fetchone()
    if not firm:
        conn.close()
        raise ValueError("Firm not found.")

    conn.execute("""
        UPDATE organizations
        SET status='SUSPENDED',
            suspended_at=?,
            rejection_reason=?
        WHERE id=?
    """, (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        str(reason or "").strip(),
        org_id
    ))
    conn.commit()
    conn.close()
    _platform_audit(super_admin_id, "SUSPEND_FIRM", org_id, reason)
    return get_platform_firm(org_id)

def reactivate_firm(org_id, super_admin_id):
    conn = get_connection()
    conn.execute("""
        UPDATE organizations
        SET status='ACTIVE',
            suspended_at=NULL,
            rejection_reason=NULL
        WHERE id=?
    """, (org_id,))
    conn.commit()
    conn.close()
    _platform_audit(super_admin_id, "REACTIVATE_FIRM", org_id, "")
    return get_platform_firm(org_id)

def upsert_platform_subscription(
    org_id,
    super_admin_id,
    package_name,
    monthly_fee,
    status,
    start_date=None,
    next_billing_date=None,
    notes=""
):
    conn = get_connection()
    existing = conn.execute(
        "SELECT id FROM platform_subscriptions WHERE org_id=?",
        (org_id,)
    ).fetchone()

    if existing:
        conn.execute("""
            UPDATE platform_subscriptions
            SET package_name=?,
                monthly_fee=?,
                status=?,
                start_date=?,
                next_billing_date=?,
                notes=?,
                updated_at=?
            WHERE org_id=?
        """, (
            str(package_name or "Standard").strip(),
            float(monthly_fee or 0),
            str(status or "Trial").strip(),
            start_date,
            next_billing_date,
            str(notes or "").strip(),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            org_id
        ))
    else:
        conn.execute("""
            INSERT INTO platform_subscriptions(
                org_id,
                package_name,
                monthly_fee,
                status,
                start_date,
                next_billing_date,
                notes,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            org_id,
            str(package_name or "Standard").strip(),
            float(monthly_fee or 0),
            str(status or "Trial").strip(),
            start_date,
            next_billing_date,
            str(notes or "").strip(),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))

    conn.commit()
    conn.close()

    _platform_audit(
        super_admin_id,
        "UPDATE_SUBSCRIPTION",
        org_id,
        f"{package_name} | {status} | R {float(monthly_fee or 0):.2f}"
    )

def platform_dashboard_metrics():
    conn = get_connection()
    status_rows = conn.execute("""
        SELECT status, COUNT(*) AS total
        FROM organizations
        GROUP BY status
    """).fetchall()
    status_counts = {
        str(r["status"] or "ACTIVE").upper(): int(r["total"])
        for r in status_rows
    }

    active_users = conn.execute("""
        SELECT COUNT(*) AS total
        FROM users
        WHERE active=1
    """).fetchone()["total"]

    mrr = conn.execute("""
        SELECT COALESCE(SUM(monthly_fee), 0) AS total
        FROM platform_subscriptions
        WHERE status IN ('Active', 'Trial')
    """).fetchone()["total"]

    open_support = conn.execute("""
        SELECT COUNT(*) AS total
        FROM support_queries
        WHERE status != 'Resolved'
    """).fetchone()["total"]

    conn.close()

    return {
        "total_firms": sum(status_counts.values()),
        "pending_firms": status_counts.get("PENDING", 0),
        "active_firms": status_counts.get("ACTIVE", 0),
        "suspended_firms": status_counts.get("SUSPENDED", 0),
        "rejected_firms": status_counts.get("REJECTED", 0),
        "active_users": int(active_users or 0),
        "mrr": float(mrr or 0),
        "open_support": int(open_support or 0),
    }

def create_support_query(
    org_id,
    user_id,
    subject,
    description,
    priority="Normal"
):
    subject = str(subject or "").strip()
    description = str(description or "").strip()
    if not subject or not description:
        raise ValueError("Subject and description are required.")

    conn = get_connection()
    cur = conn.execute("""
        INSERT INTO support_queries(
            org_id,
            user_id,
            subject,
            description,
            priority,
            status
        )
        VALUES (?, ?, ?, ?, ?, 'Open')
    """, (
        org_id,
        user_id,
        subject,
        description,
        str(priority or "Normal")
    ))
    conn.commit()
    query_id = cur.lastrowid
    conn.close()
    return query_id

def list_support_queries(org_id=None):
    conn = get_connection()
    sql = """
        SELECT
            q.*,
            o.name AS firm_name,
            o.firm_number,
            u.name AS user_name,
            u.cell AS user_cell
        FROM support_queries q
        JOIN organizations o ON o.id=q.org_id
        LEFT JOIN users u ON u.id=q.user_id
    """
    params = []
    if org_id is not None:
        sql += " WHERE q.org_id=?"
        params.append(org_id)
    sql += """
        ORDER BY
            CASE q.status
                WHEN 'Open' THEN 1
                WHEN 'In Progress' THEN 2
                ELSE 3
            END,
            q.id DESC
    """
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows_to_dicts(rows)

def update_support_query(query_id, super_admin_id, status, admin_notes=""):
    conn = get_connection()
    row = conn.execute(
        "SELECT org_id FROM support_queries WHERE id=?",
        (query_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise ValueError("Support query not found.")

    conn.execute("""
        UPDATE support_queries
        SET status=?,
            admin_notes=?,
            updated_at=?
        WHERE id=?
    """, (
        str(status or "Open"),
        str(admin_notes or "").strip(),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        query_id
    ))
    conn.commit()
    conn.close()

    _platform_audit(
        super_admin_id,
        "UPDATE_SUPPORT_QUERY",
        row["org_id"],
        f"Query #{query_id} -> {status}"
    )
