"""
Sincronización del PC con Firebase Realtime Database.

Estructura en Firebase:
  /usuarios/{UID}            -> nombre (se sincroniza en ambos sentidos)
  /dispositivos/{id}         -> latido de cada equipo: nombre, tipo, ip, latencia_ms, usb_conectado, ts
  /eventos/{push}            -> tarjetas leídas por un equipo (ej. el totem Android): uid, dispositivo, ts
  /comandos/{dispositivo}/{push} -> órdenes del PC para un equipo (ej. "OK" para abrir la chapa)
  /logs/{push}               -> historial de accesos (solo se sube desde el PC)

Notas:
- Las fechas que escribe el servidor de Firebase se piden con {".sv": "timestamp"}, así no
  dependemos del reloj del equipo que escribe.
- Para saber si un equipo está en línea se usa el momento en que el PC VIO llegar el último
  latido (no la hora que trae el equipo), así los relojes desfasados no afectan.
"""
import time
import threading
import requests

VENTANA_EVENTO_MS = 6000       # una tarjeta leída hace más de 6 s ya no se procesa
SEGUNDOS_EN_LINEA = 15         # sin latido en 15 s => sin conexión
ID_PC = "pc_principal"


class Sincronizacion:
    def __init__(self, base_url, id_pc=ID_PC, nombre_pc="PC principal"):
        self.base = base_url.rstrip('/')
        self.id_pc = id_pc
        self.nombre_pc = nombre_pc
        self.latencia_ms = None
        self.ultimo_error = None
        self.visto = {}                  # id -> {"ts": valor del latido, "llegada": hora local, "datos": {...}}
        self.eventos_vistos = {}         # clave -> True (orden de llegada, para recortar)
        self.primera_lectura = True      # lo que ya existía al arrancar no se procesa
        self._ultima_tarjeta = {}        # (uid, dispositivo) -> hora local, anti-rebote
        self.lock = threading.Lock()

    # ------------------------------------------------------------------
    # Comunicación básica
    # ------------------------------------------------------------------
    def _req(self, metodo, ruta, cuerpo=None, timeout=3.0):
        """Devuelve (ok, dato). Mide la latencia de cada petición."""
        inicio = time.time()
        try:
            r = requests.request(metodo, f"{self.base}{ruta}", json=cuerpo, timeout=timeout)
            r.raise_for_status()
            dato = r.json() if r.content else None
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            return False, None
        self.latencia_ms = round((time.time() - inicio) * 1000)
        self.ultimo_error = None
        return True, dato

    # ------------------------------------------------------------------
    # Latido del PC y estado de los equipos
    # ------------------------------------------------------------------
    def latir(self, ip):
        self._req("PUT", f"/dispositivos/{self.id_pc}.json", {
            "nombre": self.nombre_pc,
            "tipo": "pc",
            "ip": ip,
            "latencia_ms": self.latencia_ms,
            "usb_conectado": None,
            "ts": time.time(),
        })

    def actualizar_visto(self, dispositivos):
        ahora = time.time()
        with self.lock:
            for did, datos in (dispositivos or {}).items():
                if not isinstance(datos, dict):
                    continue
                ts = datos.get("ts")
                previo = self.visto.get(did)
                if previo is None or previo["ts"] != ts:
                    self.visto[did] = {"ts": ts, "llegada": ahora, "datos": datos}
                else:
                    previo["datos"] = datos

    def estado_dispositivos(self):
        ahora = time.time()
        lista = []
        with self.lock:
            for did, v in self.visto.items():
                d = v["datos"]
                espera = ahora - v["llegada"]
                lista.append({
                    "id": did,
                    "nombre": d.get("nombre", did),
                    "tipo": d.get("tipo", ""),
                    "ip": d.get("ip", "—"),
                    "latencia_ms": d.get("latencia_ms"),
                    "usb_conectado": d.get("usb_conectado"),
                    "en_linea": espera < SEGUNDOS_EN_LINEA,
                    "visto_hace_s": round(espera),
                })
        lista.sort(key=lambda x: (not x["en_linea"], x["nombre"]))
        return lista

    # ------------------------------------------------------------------
    # Usuarios: sincronización en ambos sentidos
    # ------------------------------------------------------------------
    def pull_usuarios(self, local):
        """Trae los usuarios de Firebase y sube los que solo existen en el PC.
        Devuelve (usuarios_combinados, hubo_cambios_locales)."""
        ok, remoto = self._req("GET", "/usuarios.json")
        if not ok:
            return local, False
        remoto = remoto or {}
        cambio = False
        for uid, valor in remoto.items():
            if local.get(uid) != valor:
                local[uid] = valor
                cambio = True
        for uid, valor in list(local.items()):
            if uid not in remoto:
                self._req("PUT", f"/usuarios/{uid}.json", valor)
        return local, cambio

    # ------------------------------------------------------------------
    # Tarjetas leídas por otros equipos (totem Android)
    # ------------------------------------------------------------------
    def procesar_eventos(self, decidir, registrar):
        """decidir(uid) -> (permitido: bool, nombre: str)
        registrar(uid, nombre, dispositivo, permitido: bool)"""
        ok, data = self._req("GET", '/eventos.json?orderBy=%22%24key%22&limitToLast=30')
        if not ok or not isinstance(data, dict):
            return

        nuevos = []
        for clave, ev in data.items():
            if clave not in self.eventos_vistos:
                self.eventos_vistos[clave] = True
                nuevos.append((clave, ev))
        while len(self.eventos_vistos) > 500:
            self.eventos_vistos.pop(next(iter(self.eventos_vistos)))

        if self.primera_lectura:
            self.primera_lectura = False
            return

        ahora_ms = time.time() * 1000
        for _clave, ev in nuevos:
            if not isinstance(ev, dict):
                continue
            ts = ev.get("ts")
            if not isinstance(ts, (int, float)) or ahora_ms - ts > VENTANA_EVENTO_MS:
                continue  # evento viejo (la red o el PC estuvieron caídos)
            uid = str(ev.get("uid", "")).strip().upper()
            dispositivo = str(ev.get("dispositivo", "")).strip()
            if not uid or not dispositivo:
                continue

            # Anti-rebote: la misma tarjeta en el mismo equipo dentro de 2 s cuenta una vez
            clave_rebote = (uid, dispositivo)
            hora = time.time()
            if hora - self._ultima_tarjeta.get(clave_rebote, 0) < 2.0:
                continue
            self._ultima_tarjeta[clave_rebote] = hora

            permitido, nombre = decidir(uid)
            registrar(uid, nombre, dispositivo, permitido)
            if permitido:
                self._req("POST", f"/comandos/{dispositivo}.json", {
                    "cmd": "OK",
                    "origen": "TARJETA",
                    "uid": uid,
                    "nombre": nombre,
                    "ts": {".sv": "timestamp"},
                })
