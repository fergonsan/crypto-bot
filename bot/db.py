"""
MÓDULO DE BASE DE DATOS
=======================

Funciones auxiliares para interactuar con PostgreSQL.
Maneja conexiones, configuración y registro de ejecuciones del bot.
"""

import os
import psycopg

def get_conn():
    """
    Crea y retorna una conexión a la base de datos PostgreSQL.
    
    Requiere variable de entorno: DATABASE_URL
    Ejemplo: "postgresql://user:password@host:port/database"
    
    Returns:
        Conexión a PostgreSQL
    """
    return psycopg.connect(os.environ["DATABASE_URL"])

def get_setting(conn, key: str, default: str) -> str:
    """
    Obtiene un valor de configuración desde la tabla 'settings'.
    
    ⚠️ MODIFICAR CONFIGURACIÓN: Actualiza valores directamente en la tabla 'settings' de la BD
    
    Configuraciones disponibles:
    - trading_enabled: "true" o "false" - Habilita/deshabilita trading
    - max_order_notional_usdc: Máximo valor de una orden (ej: "300")
    - max_asset_exposure_pct: Máximo % del equity en un activo (ej: "0.50" = 50%)
    - max_orders_per_day: Máximo número de órdenes por día (ej: "2")
    
    Args:
        conn: Conexión a la base de datos
        key: Clave de la configuración
        default: Valor por defecto si no existe la clave
    
    Returns:
        Valor de la configuración como string
    """
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
        row = cur.fetchone()
        return row[0] if row else default

def set_run_status(conn, run_id: int, status: str, message: str | None = None):
    """
    Actualiza el estado de una ejecución del bot.
    
    Estados típicos:
    - "running": Ejecución en curso
    - "ok": Ejecución completada exitosamente
    - "error": Ejecución falló
    
    Args:
        conn: Conexión a la base de datos
        run_id: ID de la ejecución
        status: Estado nuevo
        message: Mensaje opcional con detalles
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
    
    Cada vez que el bot se ejecuta, se crea un registro en bot_runs
    para llevar seguimiento de todas las ejecuciones.
    
    Args:
        conn: Conexión a la base de datos
    
    Returns:
        ID del nuevo registro de ejecución
    """
    with conn.cursor() as cur:
        cur.execute("INSERT INTO bot_runs(status) VALUES('running') RETURNING id")
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id
