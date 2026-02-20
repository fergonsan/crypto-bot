"""
MÓDULO DE NOTIFICACIONES
========================

Envía notificaciones por Telegram cuando el bot ejecuta operaciones
o calcula señales.
"""

import os
import json
import urllib.request

def telegram_send(text: str):
    """
    Envía un mensaje de texto por Telegram.
    
    Requiere variables de entorno (opcionales):
    - TELEGRAM_BOT_TOKEN: Token del bot de Telegram
    - TELEGRAM_CHAT_ID: ID del chat donde enviar mensajes
    
    Si no están configuradas, la función simplemente retorna sin hacer nada
    (el bot funciona sin Telegram, solo no envía notificaciones).
    
    Para obtener un bot de Telegram:
    1. Habla con @BotFather en Telegram
    2. Crea un bot con /newbot
    3. Obtén el token
    4. Obtén tu chat_id hablando con @userinfobot
    
    Args:
        text: Texto del mensaje a enviar
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return  # Si no hay configuración, simplemente no envía nada
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)
