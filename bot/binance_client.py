"""
CLIENTE DE BINANCE
==================

Módulo para crear la conexión con Binance usando la librería CCXT.
Maneja autenticación y configuración del exchange.
"""

import os
import ccxt

def make_exchange():
    """
    Crea y configura una instancia del exchange Binance.
    
    Requiere variables de entorno:
    - BINANCE_API_KEY: Tu API key de Binance
    - BINANCE_API_SECRET: Tu API secret de Binance
    
    ⚠️ IMPORTANTE: 
    - Nunca compartas tus API keys
    - Usa solo permisos necesarios (spot trading, lectura de balance)
    - Considera usar IP whitelist en Binance para mayor seguridad
    
    Returns:
        Instancia configurada de ccxt.binance para trading spot
    """
    ex = ccxt.binance({
        "apiKey": os.environ["BINANCE_API_KEY"],
        "secret": os.environ["BINANCE_API_SECRET"],
        "enableRateLimit": True,  # Respeta límites de rate de Binance automáticamente
        "options": {
            "defaultType": "spot",  # Trading spot (no futuros)
        },
    })
    return ex
