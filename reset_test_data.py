import os
import sys
import psycopg2
from psycopg2 import sql

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
CONFIRMATION = "RESET NEXORA TEST DATA"

def table_exists(cur, name):
    cur.execute("""SELECT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='public' AND table_name=%s
    )""", (name,))
    return cur.fetchone()[0]

def count_rows(cur, name):
    cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(name)))
    return cur.fetchone()[0]

def main():
    print("=" * 72)
    print("NEXORA LEGAL - ONE-TIME PRE-PILOT PRODUCTION CLEANUP")
    print("=" * 72)

    if not DATABASE_URL:
        raise SystemExit(
            "STOPPED: DATABASE_URL is not configured. "
            "Run this only in the Render service connected to production PostgreSQL."
        )

    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            if not table_exists(cur, "organizations"):
                raise RuntimeError("organizations table not found. Reset stopped.")

            orgs_before = count_rows(cur, "organizations")
            print(f"\nOrganizations/firms currently stored: {orgs_before}")

            admin_before = None
            if table_exists(cur, "super_admin_users"):
                admin_before = count_rows(cur, "super_admin_users")
                print(f"Super Admin records: {admin_before}  <-- PRESERVED")

            print("\nThis will:")
            print("  - remove ALL current firms/test organizations")
            print("  - remove firm-dependent data through PostgreSQL foreign keys")
            print("  - reset affected identity sequences")
            print("  - keep the database schema")
            print("  - keep super_admin_users")
            print("\nUSE THIS ONLY BEFORE THE SIX REAL PILOT FIRMS ARE LOADED.")

            entered = input(
                f"\nType exactly '{CONFIRMATION}' to continue: "
            ).strip()

            if entered != CONFIRMATION:
                conn.rollback()
                print("\nCANCELLED. Nothing was deleted.")
                return

            final = input(
                "Type YES for the final permanent deletion confirmation: "
            ).strip()

            if final != "YES":
                conn.rollback()
                print("\nCANCELLED. Nothing was deleted.")
                return

            # PostgreSQL discovers and clears FK-dependent firm tables itself.
            # The separate super_admin_users table is not targeted.
            cur.execute(
                "TRUNCATE TABLE organizations RESTART IDENTITY CASCADE"
            )
            conn.commit()

            orgs_after = count_rows(cur, "organizations")
            print(f"\nOrganizations/firms remaining: {orgs_after}")

            if table_exists(cur, "super_admin_users"):
                admin_after = count_rows(cur, "super_admin_users")
                print(f"Super Admin records remaining: {admin_after}")
                if admin_before is not None and admin_after != admin_before:
                    raise RuntimeError(
                        "Super Admin verification failed. "
                        "The count changed unexpectedly."
                    )

            if orgs_after != 0:
                raise RuntimeError("Cleanup verification failed.")

            print("\n" + "=" * 72)
            print("SUCCESS - TEST FIRM DATA CLEARED")
            print("=" * 72)
            print("Schema preserved.")
            print("Super Admin preserved.")
            print("You may now register the six real pilot firms.")
            print("Delete reset_test_data.py from GitHub after this one-time use.")

    except Exception as exc:
        conn.rollback()
        print(f"\nERROR: {exc}")
        print("Cleanup stopped/rolled back where possible.")
        sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
