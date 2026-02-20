import os
import psycopg

def get_conn():
    return psycopg.connect(os.environ["DATABASE_URL"])

def get_setting(conn, key: str, default: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
        row = cur.fetchone()
        return row[0] if row else default

def set_run_status(conn, run_id: int, status: str, message: str | None = None):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE bot_runs SET finished_at=NOW(), status=%s, message=%s WHERE id=%s",
            (status, message, run_id),
        )
    conn.commit()

def create_run(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO bot_runs(status) VALUES('running') RETURNING id")
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id
