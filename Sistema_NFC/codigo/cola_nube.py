"""Cola de envíos a Firebase.

Si hay conexión, el cambio se envía al momento. Si no la hay, se guarda en un archivo
local y se reenvía en orden cuando vuelve Firebase. Así los datos locales nunca se pierden
y la nube queda al día al reconectar.
"""
import json
import os
import threading
import time

import requests

MAX_ENVIOS_POR_VEZ = 50


class ColaNube:
    def __init__(self, ruta_archivo, base_url, espera_tras_fallo=20):
        self.ruta = ruta_archivo
        self.base = base_url.rstrip("/")
        self.espera_tras_fallo = espera_tras_fallo  # segundos sin reintentar tras un fallo
        self._lock = threading.Lock()
        self._ultimo_fallo = 0.0

    def _leer(self):
        try:
            with open(self.ruta, encoding="utf-8") as f:
                datos = json.load(f)
            return datos if isinstance(datos, list) else []
        except (OSError, ValueError):
            return []

    def _escribir(self, items):
        tmp = self.ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, self.ruta)

    def _enviar_uno(self, metodo, ruta, cuerpo):
        r = requests.request(metodo, self.base + ruta, json=cuerpo, timeout=2.5)
        r.raise_for_status()

    def enviar(self, metodo, ruta, cuerpo=None):
        """Envía ahora si hay conexión; si no, lo deja pendiente. Devuelve True si se envió."""
        with self._lock:
            hay_pendientes = bool(self._leer())
        sin_conexion_reciente = time.time() - self._ultimo_fallo < self.espera_tras_fallo
        if not hay_pendientes and not sin_conexion_reciente:
            try:
                self._enviar_uno(metodo, ruta, cuerpo)
                return True
            except Exception:
                self._ultimo_fallo = time.time()
        # Se encola (si ya hay pendientes, se conserva el orden)
        with self._lock:
            items = self._leer()
            items.append({"m": metodo, "r": ruta, "c": cuerpo})
            self._escribir(items)
        return False

    def vaciar(self):
        """Reenvía los pendientes en orden. Se detiene al primer fallo. Devuelve cuántos se enviaron."""
        with self._lock:
            items = self._leer()
            if not items:
                return 0
            enviados = 0
            while items and enviados < MAX_ENVIOS_POR_VEZ:
                it = items[0]
                try:
                    self._enviar_uno(it["m"], it["r"], it.get("c"))
                except Exception:
                    self._ultimo_fallo = time.time()
                    break
                items.pop(0)
                enviados += 1
            if enviados:
                self._escribir(items)
            return enviados

    def pendientes(self):
        with self._lock:
            return len(self._leer())
