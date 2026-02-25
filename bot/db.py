"""
MÓDULO DE BASE DE DATOS
=======================

Funciones auxiliares para interactuar con PostgreSQL.
Maneja conexiones, configuración y registro de ejecuciones del bot.

Añadido:
- Advisory locks (pg_try_advisory_lock) para evitar ejecuciones concurrentes en Railway.
"""

import os
import psycopg


def get_conn():
    """
    Crea y retorna una conexión a la base de datos PostgreSQL.

    Requiere variable de entorno: DATABASE_URL
    Ejemplo: "postgresql://user:password@host:port/database"
    """
    return psycopg.connect(os.environ["DATABASE_URL"])


def get_setting(conn, key: str, default: str) -> str:
    """
    Obtiene un valor de configuración desde la tabla 'settings'.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
        row = cur.fetchone()
        return row[0] if row else default


def set_run_status(conn, run_id: int, status: str, message: str | None = None):
    """
    Actualiza el estado de una ejecución del bot.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE bot_runs SET finished_at=NOW(), status=%s, message=%s WHERE id=%s",
            (status, message, run_id),
        )
    conn.commit()


def create_run(conn) -> int:
    """
    Crea un nuevo registro de ejecución del bot.
    """
    with conn.cursor() as cur:
        cur.execute("INSERT INTO bot_runs(status) VALUES('running') RETURNING id")
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def try_advisory_lock(conn, lock_key: str) -> bool:
    """
    Intenta adquirir un advisory lock (no bloqueante).
    Devuelve True si lo adquiere; False si ya está cogido por otro proceso.

    Usamos hashtext(lock_key) para mapear a int4.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (lock_key,))
        got = cur.fetchone()[0]
    conn.commit()
    return bool(got)


def release_advisory_lock(conn, lock_key: str) -> None:
    """
    Libera el advisory lock.
    Nota: si el proceso muere y la conexión se cierra, Postgres libera el lock igualmente.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_key,))
    conn.commit()