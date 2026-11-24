import os
import sys
import csv
import json
import math
import time
import base64
import socket
import threading
import traceback
import requests
import qrcode
from datetime import datetime
from flask import Flask, render_template, jsonify, request, make_response, send_from_directory, Response

try:
    import numpy as np
    import cv2
    _ERROR_IMPORT_OPENCV = None
except ImportError as _e:
    # El servidor debe arrancar aunque falte OpenCV; el reconocimiento facial avisará el problema
    np = None
    cv2 = None
    _ERROR_IMPORT_OPENCV = str(_e)

# Salida en UTF-8 aunque se redirija a un archivo (en Windows, cp1252 no tiene emojis)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))   # Sistema_NFC/codigo
RAIZ_SISTEMA = os.path.dirname(BASE_DIR)                # Sistema_NFC
DATOS_DIR = os.path.join(RAIZ_SISTEMA, 'datos')         # usuarios, registros, rostros, certificado
MODELOS_DIR = os.path.join(RAIZ_SISTEMA, 'modelos')
app = Flask(__name__, template_folder=os.path.join(RAIZ_SISTEMA, 'interfaz', 'templates'))

ARCHIVO_CSV_LOGS = os.path.join(DATOS_DIR, "registros_locales.csv")
ARCHIVO_JSON_USUARIOS = os.path.join(DATOS_DIR, "usuarios_locales.json")
ARCHIVO_COLA_DEBUG = os.path.join(DATOS_DIR, "cola_debug.json")
ARCHIVO_ULTIMA_TARJETA = os.path.join(DATOS_DIR, "ultima_tarjeta.json")
CARPETA_ROSTROS = os.path.join(DATOS_DIR, "rostros_usuarios")
CARPETA_MODELOS = MODELOS_DIR
ARCHIVO_YUNET = os.path.join(CARPETA_MODELOS, "face_detection_yunet_2023mar.onnx")
ARCHIVO_SFACE = os.path.join(CARPETA_MODELOS, "face_recognition_sface_2021dec.onnx")

PUERTO = 5000
URL_FIREBASE = "https://registros-acceso-nfc-default-rtdb.firebaseio.com"
CAMPOS_LOG = ["fecha_hora", "timestamp", "uid", "usuario", "estado", "detalle"]
COMANDOS_DEBUG = {"DBG_START", "DBG_CHAPA_ON", "DBG_CHAPA_OFF", "OK"}
# Reconocimiento facial: calidad mínima y decisión
UMBRAL_SIMILITUD = 0.45    # coseno mínimo para aceptar (OpenCV sugiere 0.363; se sube para evitar confusiones)
MARGEN_SIMILITUD = 0.06    # el mejor debe superar al segundo usuario por este margen; si no, es dudoso
SCORE_MINIMO_CARA = 0.90   # confianza mínima del detector de caras
ANCHO_MINIMO_CARA = 90     # px: una cara más chica significa que la persona está muy lejos
NITIDEZ_MINIMA = 40.0      # varianza del Laplaciano: menor valor = imagen borrosa
MENSAJES_CALIDAD = {
    'sin_rostro': 'Sin rostro frente a la cámara',
    'varias_caras': 'Hay más de una persona frente a la cámara',
    'lejos': 'Acércate a la cámara',
    'borroso': 'Imagen borrosa: mantente quieto',
    'centrar': 'Centra tu rostro en el óvalo',
    'cerca': 'Aléjate un poco de la cámara',
}
TIEMPO_MAX_CAPTURA = 180    # s: una captura de rostro se cancela sola si queda colgada
FOTOS_POR_CAPTURA = 3
INTERVALO_FOTOS = 1.0       # s entre fotos, para que no sean casi iguales
SEGUNDOS_APERTURA = 3
SEGUNDOS_ENVIO_ESTADO = 1.0  # Aunque no cambie nada, se reenvía el estado cada segundo (cuenta regresiva)

os.makedirs(CARPETA_ROSTROS, exist_ok=True)

from sincronizacion_firebase import Sincronizacion  # noqa: E402
sync = Sincronizacion(URL_FIREBASE)

from cola_nube import ColaNube  # noqa: E402
# Cambios de usuarios hechos sin conexión: se guardan aquí y se suben al volver Firebase
cola_nube_app = ColaNube(os.path.join(DATOS_DIR, "pendientes_nube_app.json"), URL_FIREBASE)

_detector = None
_reconocedor = None
_cache_rasgos = {}  # ruta de foto -> (mtime, rasgos faciales)
_captura = None     # captura de rostro en curso (o None)
_bloqueo_captura = threading.Lock()


# ---------------------------------------------------------------------------
# Utilidades generales
# ---------------------------------------------------------------------------
def _ip_hacia(destino):
    """IP que el sistema usaría para llegar a 'destino' (no envía nada)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((destino, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _es_red_local(ip):
    """Solo redes privadas (Wi‑Fi de casa, hotspot). Descarta 127.x y 169.254.x (sin red)."""
    if not ip or ip.startswith(("127.", "169.254.")):
        return False
    partes = ip.split(".")
    if len(partes) != 4:
        return False
    a, b = int(partes[0]), int(partes[1])
    return a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31)


def obtener_ip_local():
    """IP de la red local que usará el celular.
    1) Si existe 'ip_celular.txt' con una IP, se usa esa (sirve para VPN o adaptadores virtuales).
    2) Si no, busca una IP privada en las interfaces (funciona aunque el Wi‑Fi no tenga internet)."""
    ruta_fija = os.path.join(DATOS_DIR, "ip_celular.txt")
    if os.path.exists(ruta_fija):
        try:
            with open(ruta_fija, encoding="utf-8") as f:
                fijada = f.read().strip()
            if fijada:
                return fijada
        except OSError:
            pass

    candidatas = []
    for destino in ("192.168.1.1", "10.0.0.1", "8.8.8.8"):
        ip = _ip_hacia(destino)
        if ip:
            candidatas.append(ip)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidatas.append(info[4][0])
    except OSError:
        pass

    # Prioridad: 192.168.137.x (hotspot de Windows, IP fija) > otros 192.168.x > 10.x > 172.16-31
    def prioridad(ip):
        if ip.startswith("192.168.137."):
            return 0
        if ip.startswith("192.168."):
            return 1
        if ip.startswith("10."):
            return 2
        return 3

    locales = [ip for ip in candidatas if _es_red_local(ip)]
    if locales:
        return min(locales, key=prioridad)
    return "127.0.0.1"


def puerto_ocupado(puerto=PUERTO):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", puerto)) == 0


def mostrar_qr_servidor(url_acceso):
    print("\n" + "=" * 60)
    print(" 🌐 SERVIDOR WEB Y TERMINAL BIOMÉTRICA INICIADOS ".center(60))
    print("=" * 60)
    print(f" URL de Acceso Local para el Celular: {url_acceso}\n")
    try:
        qr = qrcode.QRCode(box_size=1, border=1)
        qr.add_data(url_acceso)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        pass
    print("=" * 60 + "\n")


def cargar_usuarios_locales():
    if os.path.exists(ARCHIVO_JSON_USUARIOS):
        try:
            with open(ARCHIVO_JSON_USUARIOS, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def guardar_usuarios_locales(usuarios):
    with open(ARCHIVO_JSON_USUARIOS, 'w', encoding='utf-8') as f:
        json.dump(usuarios, f, ensure_ascii=False, indent=4)


def nombre_de(valor):
    if isinstance(valor, dict):
        return valor.get("nombre", "")
    return str(valor)


def ruta_foto(uid):
    return os.path.join(CARPETA_ROSTROS, f"{uid}.jpg")


def sincronizar_firebase(metodo, ruta, cuerpo=None):
    """Replica un cambio en la nube. Si no hay conexión, queda pendiente y se sube después."""
    cola_nube_app.enviar(metodo, ruta, cuerpo)


def leer_logs():
    """Lee el historial usando siempre los nombres de columna del servidor (no depende de la cabecera del archivo)."""
    logs = []
    if os.path.exists(ARCHIVO_CSV_LOGS):
        try:
            with open(ARCHIVO_CSV_LOGS, mode='r', encoding='utf-8', errors='ignore') as f:
                for row in csv.DictReader(f, fieldnames=CAMPOS_LOG):
                    row.pop(None, None)  # columnas de más en una fila mal escrita
                    if not row.get("timestamp") or row.get("fecha_hora") == "fecha_hora":
                        continue  # fila vacía o cabecera repetida
                    logs.append(row)
        except Exception:
            pass
    return logs


def registrar_log(uid, usuario, estado, detalle):
    fila = {
        "fecha_hora": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp": int(time.time()),
        "uid": uid,
        "usuario": usuario,
        "estado": estado,
        "detalle": detalle,
    }
    existe = os.path.exists(ARCHIVO_CSV_LOGS)
    with open(ARCHIVO_CSV_LOGS, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=CAMPOS_LOG)
        if not existe:
            writer.writeheader()
        writer.writerow(fila)
    sincronizar_firebase("POST", "/logs.json", fila)


def ultimo_evento_coincide(uid, estado, detalle, segundos):
    """True si el último registro es igual a este evento y ocurrió hace menos de 'segundos'."""
    logs = leer_logs()
    if not logs:
        return False
    u = logs[-1]
    try:
        ts = int(u.get("timestamp") or 0)
    except ValueError:
        return False
    return (u.get("uid", "") == uid and u.get("estado") == estado
            and u.get("detalle") == detalle and time.time() - ts < segundos)


def encolar_comando(item):
    """Deja un comando para el motor (Proyecto Chapa NFC.py), que lo envía al Arduino."""
    cola = []
    if os.path.exists(ARCHIVO_COLA_DEBUG):
        try:
            with open(ARCHIVO_COLA_DEBUG, 'r', encoding='utf-8') as f:
                cola = json.load(f)
        except Exception:
            cola = []
    # Un mismo rostro no se encola dos veces mientras su acceso está pendiente
    if item.get("origen") == "FACIAL" and any(
            c.get("origen") == "FACIAL" and c.get("uid") == item.get("uid") for c in cola):
        return
    cola.append(item)
    with open(ARCHIVO_COLA_DEBUG, 'w', encoding='utf-8') as f:
        json.dump(cola, f)


# ---------------------------------------------------------------------------
# Reconocimiento facial (OpenCV: YuNet para detectar, SFace para comparar)
# ---------------------------------------------------------------------------
def verificar_opencv():
    if cv2 is None:
        raise RuntimeError(f"Falta OpenCV en este Python ({_ERROR_IMPORT_OPENCV}). Instala: pip install -r requirements.txt")


def cargar_modelos_rostro():
    global _detector, _reconocedor
    verificar_opencv()
    if _detector is not None and _reconocedor is not None:
        return
    if not (os.path.exists(ARCHIVO_YUNET) and os.path.exists(ARCHIVO_SFACE)):
        raise RuntimeError("Faltan los modelos de rostro en la carpeta 'modelos'")
    _detector = cv2.FaceDetectorYN.create(ARCHIVO_YUNET, "", (320, 320), 0.8, 0.3, 5000)
    _reconocedor = cv2.FaceRecognizerSF.create(ARCHIVO_SFACE, "")


def decodificar_imagen(data_url):
    verificar_opencv()
    _, encoded = data_url.split(",", 1)
    buffer = np.frombuffer(base64.b64decode(encoded), dtype=np.uint8)
    return cv2.imdecode(buffer, cv2.IMREAD_COLOR)


def fotos_de(uid):
    """Todas las fotos de un usuario (cada una es una plantilla de rostro)."""
    if not os.path.isdir(CARPETA_ROSTROS):
        return []
    encontradas = []
    for nombre in os.listdir(CARPETA_ROSTROS):
        base, ext = os.path.splitext(nombre)
        if ext.lower() == ".jpg" and (base == uid or base.startswith(uid + "_")):
            encontradas.append(os.path.join(CARPETA_ROSTROS, nombre))
    return sorted(encontradas)


def nueva_foto_path(uid):
    return os.path.join(CARPETA_ROSTROS, f"{uid}_{int(time.time() * 1000)}.jpg")


def normalizar_luz(imagen):
    """Iguala la luz (CLAHE sobre la luminancia): la misma cara se parece más
    aunque cambie la iluminación entre el registro y la entrada."""
    ycrcb = cv2.cvtColor(imagen, cv2.COLOR_BGR2YCrCb)
    ycrcb[:, :, 0] = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(ycrcb[:, :, 0])
    return cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2BGR)


def analizar_rostro(imagen):
    """Analiza la calidad de la cara y, si es válida, calcula sus rasgos.
    estado: ok | sin_rostro | varias_caras | lejos | borroso"""
    cargar_modelos_rostro()
    imagen = normalizar_luz(imagen)
    alto, ancho = imagen.shape[:2]
    _detector.setInputSize((ancho, alto))
    _, caras = _detector.detect(imagen)
    if caras is None or len(caras) == 0:
        return {"estado": "sin_rostro"}
    seguras = [c for c in caras if c[14] >= SCORE_MINIMO_CARA]
    if not seguras:
        return {"estado": "sin_rostro"}
    if len(seguras) > 1:
        return {"estado": "varias_caras"}
    cara = seguras[0]
    x, y, w, h = (int(v) for v in cara[:4])
    info = {"caja": (x, y, w, h), "ancho_imagen": ancho, "alto_imagen": alto}
    if w < ANCHO_MINIMO_CARA:
        return {"estado": "lejos", **info}
    recorte = imagen[max(0, y):max(0, y) + h, max(0, x):max(0, x) + w]
    if recorte.size == 0:
        return {"estado": "sin_rostro"}
    nitidez = cv2.Laplacian(cv2.cvtColor(recorte, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
    if nitidez < NITIDEZ_MINIMA:
        return {"estado": "borroso", **info}
    rasgos = _reconocedor.feature(_reconocedor.alignCrop(imagen, cara))
    return {"estado": "ok", "rasgos": rasgos, **info}


def rasgos_de_usuario(uid):
    """Rasgos de todas las fotos válidas del usuario. Se recalculan solo si cambia el archivo."""
    lista = []
    for ruta in fotos_de(uid):
        mtime = os.path.getmtime(ruta)
        cache = _cache_rasgos.get(ruta)
        if cache and cache[0] == mtime:
            rasgos = cache[1]
        else:
            imagen = cv2.imread(ruta)
            resultado = analizar_rostro(imagen) if imagen is not None else {"estado": "invalida"}
            rasgos = resultado.get("rasgos") if resultado["estado"] == "ok" else None
            _cache_rasgos[ruta] = (mtime, rasgos)
        if rasgos is not None:
            lista.append(rasgos)
    return lista


def similitud(rasgos_a, rasgos_b):
    return _reconocedor.match(rasgos_a, rasgos_b, cv2.FaceRecognizerSF_FR_COSINE)


def identificar(rasgos_vivo, usuarios):
    """Compara con todas las fotos de cada usuario (se toma la mejor de cada uno).
    Devuelve (uid, similitud, segunda_similitud, decision).
    decision: ok | sin_coincidencia | ambiguo"""
    puntajes = []
    for uid in usuarios:
        plantillas = rasgos_de_usuario(uid)
        if plantillas:
            puntajes.append((max(similitud(rasgos_vivo, r) for r in plantillas), uid))
    if not puntajes:
        return None, -1.0, -1.0, "sin_coincidencia"
    puntajes.sort(reverse=True)
    mejor, uid = puntajes[0]
    segundo = puntajes[1][0] if len(puntajes) > 1 else -1.0
    if mejor < UMBRAL_SIMILITUD:
        return uid, mejor, segundo, "sin_coincidencia"
    if mejor - segundo < MARGEN_SIMILITUD:
        return uid, mejor, segundo, "ambiguo"
    return uid, mejor, segundo, "ok"


# ---------------------------------------------------------------------------
# Estado (lo que ven el teléfono y el administrador)
# ---------------------------------------------------------------------------
def calcular_estado():
    usuarios = cargar_usuarios_locales()
    logs = leer_logs()
    ahora = time.time()

    ultimo = None
    chapa_abierta = False
    tiempo_restante = 0

    if logs:
        u = logs[-1]
        uid = u.get("uid", "")
        ts = int(u.get("timestamp") or 0)
        fotos_usuario = fotos_de(uid) if uid else []
        foto_url = f"/rostros_usuarios/{os.path.basename(fotos_usuario[-1])}" if fotos_usuario else None
        ultimo = {
            "fecha_hora": u.get("fecha_hora", ""),
            "uid": uid,
            "usuario": u.get("usuario", ""),
            "estado": u.get("estado", ""),
            "detalle": u.get("detalle", ""),
            "hace_segundos": round(ahora - ts, 1),
            "foto_url": foto_url,
        }
        if ultimo["estado"] == "PERMITIDO":
            restante = math.ceil(SEGUNDOS_APERTURA - (ahora - ts))
            if restante > 0:
                chapa_abierta = True
                tiempo_restante = restante

    return {
        "usuarios": usuarios,
        "logs": logs[-15:][::-1],
        "ultimo": ultimo,
        "chapa_abierta": chapa_abierta,
        "tiempo_restante": tiempo_restante,
        "dispositivos": sync.estado_dispositivos(),
        "firebase_ok": sync.ultimo_error is None,
        "latencia_firebase_ms": sync.latencia_ms,
        "pendientes_nube": cola_nube_app.pendientes(),
        "captura": captura_resumen(),
    }


def decidir_tarjeta(uid):
    """Lo usa la sincronización para tarjetas leídas por otros equipos (totem Android)."""
    usuarios = cargar_usuarios_locales()
    if uid in usuarios:
        return True, nombre_de(usuarios[uid])
    return False, "Desconocido"


def registrar_tarjeta(uid, nombre, dispositivo, permitido):
    if permitido:
        registrar_log(uid, nombre, "PERMITIDO", f"TARJETA_{dispositivo.upper()}")
    else:
        registrar_log(uid, "Desconocido", "DENEGADO", "NO_REGISTRADO")


def bucle_firebase():
    """Latidos, sincronización de usuarios y lectura de tarjetas de otros equipos."""
    t_latido = t_usuarios = t_dispositivos = 0.0
    while True:
        try:
            ahora = time.time()
            if ahora - t_latido >= 5:
                sync.latir(obtener_ip_local())
                t_latido = ahora
            if ahora - t_usuarios >= 5:
                # Primero se suben los cambios hechos sin conexión (así un usuario borrado
                # sin conexión no vuelve a aparecer al traer los datos de Firebase)
                cola_nube_app.vaciar()
                if cola_nube_app.pendientes() == 0:
                    usuarios, cambio = sync.pull_usuarios(cargar_usuarios_locales())
                    if cambio:
                        guardar_usuarios_locales(usuarios)
                t_usuarios = ahora
            if ahora - t_dispositivos >= 2:
                ok, datos = sync._req("GET", "/dispositivos.json")
                if ok:
                    sync.actualizar_visto(datos)
                t_dispositivos = ahora
            sync.procesar_eventos(decidir_tarjeta, registrar_tarjeta)
        except Exception as e:
            print(f"⚠️ Sincronización Firebase: {e}")
            traceback.print_exc()  # línea exacta del error, para diagnóstico
        time.sleep(0.8)


def marca_de_archivo(ruta):
    try:
        st = os.stat(ruta)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def firma_datos():
    """Cambia cuando cambian los registros o los usuarios (sirve para saber si hay novedades)."""
    return (marca_de_archivo(ARCHIVO_CSV_LOGS), marca_de_archivo(ARCHIVO_JSON_USUARIOS))


# ---------------------------------------------------------------------------
# Rutas: páginas
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/celular')
def celular():
    return render_template('celular.html')


@app.route('/rostros_usuarios/<path:archivo>')
def servir_rostro(archivo):
    return send_from_directory(CARPETA_ROSTROS, archivo, max_age=0)


# ---------------------------------------------------------------------------
# Rutas: estado en tiempo real
# ---------------------------------------------------------------------------
@app.route('/api/estado')
def api_estado():
    response = make_response(jsonify(calcular_estado()))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.route('/api/stream')
def api_stream():
    """Server-Sent Events: el servidor empuja el estado cuando cambia y cada segundo (cuenta regresiva)."""
    def generar():
        ultima_firma = None
        ultimo_envio = 0.0
        while True:
            firma = firma_datos()
            ahora = time.time()
            if firma != ultima_firma or ahora - ultimo_envio >= SEGUNDOS_ENVIO_ESTADO:
                ultima_firma = firma
                ultimo_envio = ahora
                yield f"data: {json.dumps(calcular_estado(), ensure_ascii=False)}\n\n"
            time.sleep(0.4)

    return Response(generar(), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Rutas: reconocimiento facial
# ---------------------------------------------------------------------------
@app.route('/api/verificar_rostro', methods=['POST'])
def api_verificar_rostro():
    data = request.get_json(silent=True) or {}
    imagen_data = data.get('imagen', '')
    if not imagen_data:
        return jsonify({"success": False, "mensaje": "No se recibió imagen"})
    if captura_resumen() is not None:
        # Mientras se registra un rostro no se escanea ni se abre la chapa
        return jsonify({"success": False, "en_captura": True, "mensaje": "Registro de rostro en curso"})

    try:
        imagen = decodificar_imagen(imagen_data)
        if imagen is None:
            return jsonify({"success": False, "mensaje": "La imagen no es válida"})

        analisis = analizar_rostro(imagen)
        if analisis["estado"] != "ok":
            # Calidad insuficiente: no es un evento, no se registra
            return jsonify({"success": False, "sin_rostro": True, "calidad": analisis["estado"],
                            "mensaje": MENSAJES_CALIDAD.get(analisis["estado"], "Sin rostro")})

        usuarios = cargar_usuarios_locales()
        uid, mejor, segundo, decision = identificar(analisis["rasgos"], usuarios)
        print(f"[FACIAL] decision={decision} usuario={uid} similitud={mejor:.3f} segundo={segundo:.3f}")

        if decision == "ok":
            nombre = nombre_de(usuarios[uid])
            # Si la misma persona acaba de entrar, no se vuelve a abrir ni a registrar
            if not ultimo_evento_coincide(uid, "PERMITIDO", "RECONOCIMIENTO_FACIAL", 8):
                registrar_log(uid, nombre, "PERMITIDO", "RECONOCIMIENTO_FACIAL")
                # Abrir la chapa: el motor recibe la orden y la envía al Arduino
                encolar_comando({"cmd": "OK", "origen": "FACIAL", "uid": uid,
                                 "nombre": nombre, "ts": time.time()})
            return jsonify({"success": True, "mensaje": f"✅ Autorizado: {nombre}"})

        # Rostro desconocido o dudoso: se registra como máximo una vez cada 5 segundos
        if not ultimo_evento_coincide("", "DENEGADO", "ROSTRO_NO_RECONOCIDO", 5):
            registrar_log("", "Desconocido", "DENEGADO", "ROSTRO_NO_RECONOCIDO")
        motivo = "❌ Rostro no reconocido" if decision == "sin_coincidencia" else "❌ Rostro dudoso, no se abre"
        return jsonify({"success": False, "mensaje": motivo})

    except RuntimeError as e:
        return jsonify({"success": False, "mensaje": str(e)})
    except Exception as e:
        return jsonify({"success": False, "mensaje": f"Error al verificar: {e}"})


@app.route('/api/usuario/foto', methods=['POST'])
def api_usuario_foto():
    """Guarda una foto de registro (se suma a las demás del usuario) con control de calidad."""
    data = request.get_json(silent=True) or {}
    uid = (data.get('uid') or '').strip().upper()
    if uid not in cargar_usuarios_locales():
        return jsonify({"success": False, "mensaje": "Primero guarda el usuario con su tarjeta"})

    try:
        imagen = decodificar_imagen(data.get('imagen', ''))
        if imagen is None:
            return jsonify({"success": False, "mensaje": "La imagen no es válida"})
        analisis = analizar_rostro(imagen)
        if analisis["estado"] != "ok":
            return jsonify({"success": False, "mensaje": MENSAJES_CALIDAD.get(analisis["estado"], "Sin rostro")})
        cv2.imwrite(nueva_foto_path(uid), imagen)
        return jsonify({"success": True, "mensaje": "✅ Rostro guardado para este usuario"})
    except RuntimeError as e:
        return jsonify({"success": False, "mensaje": str(e)})
    except Exception as e:
        return jsonify({"success": False, "mensaje": f"Error al guardar la foto: {e}"})


# ---------------------------------------------------------------------------
# Rutas: captura de rostro (desde el celular o la cámara de la computadora)
# ---------------------------------------------------------------------------
def _limpiar_captura_si_vencio():
    global _captura
    if _captura and time.time() - _captura["inicio"] > TIEMPO_MAX_CAPTURA:
        _captura = None


def captura_resumen():
    """Estado de la captura en curso para el celular y el admin (o None)."""
    with _bloqueo_captura:
        _limpiar_captura_si_vencio()
        if not _captura:
            return None
        return {k: _captura[k] for k in ("uid", "nombre", "origen", "centrar", "tomadas", "meta")}


@app.route('/api/captura/iniciar', methods=['POST'])
def api_captura_iniciar():
    global _captura
    data = request.get_json(silent=True) or {}
    uid = (data.get('uid') or '').strip().upper()
    origen = data.get('origen') if data.get('origen') in ('celular', 'computadora') else 'celular'
    centrar = bool(data.get('centrar'))
    usuarios = cargar_usuarios_locales()
    if uid not in usuarios:
        return jsonify({"success": False, "mensaje": "Primero guarda el usuario con su tarjeta"})
    with _bloqueo_captura:
        _captura = {"uid": uid, "nombre": nombre_de(usuarios[uid]), "origen": origen,
                    "centrar": centrar, "tomadas": 0, "meta": FOTOS_POR_CAPTURA,
                    "inicio": time.time(), "ultima": 0.0}
    lugar = "el celular" if origen == "celular" else "esta computadora"
    return jsonify({"success": True, "mensaje": f"Captura iniciada desde {lugar}"})


@app.route('/api/captura/cancelar', methods=['POST'])
def api_captura_cancelar():
    global _captura
    with _bloqueo_captura:
        _captura = None
    return jsonify({"success": True, "mensaje": "Captura cancelada"})


@app.route('/api/captura/frame', methods=['POST'])
def api_captura_frame():
    global _captura
    data = request.get_json(silent=True) or {}
    with _bloqueo_captura:
        _limpiar_captura_si_vencio()
        cap = dict(_captura) if _captura else None
    if cap is None:
        return jsonify({"success": False, "guia": "inactiva", "mensaje": "No hay captura activa"})

    try:
        imagen = decodificar_imagen(data.get('imagen', ''))
        if imagen is None:
            return jsonify({"success": False, "guia": "imagen", "mensaje": "La imagen no es válida"})

        analisis = analizar_rostro(imagen)
        if analisis["estado"] != "ok":
            return jsonify({"success": False, "guia": analisis["estado"],
                            "mensaje": MENSAJES_CALIDAD.get(analisis["estado"], "Sin rostro")})

        if cap["centrar"]:
            x, y, w, h = analisis["caja"]
            ancho, alto = analisis["ancho_imagen"], analisis["alto_imagen"]
            centro_x, centro_y = (x + w / 2) / ancho, (y + h / 2) / alto
            if w / ancho > 0.65:
                return jsonify({"success": False, "guia": "cerca", "mensaje": MENSAJES_CALIDAD["cerca"]})
            if abs(centro_x - 0.5) > 0.12 or abs(centro_y - 0.5) > 0.15:
                return jsonify({"success": False, "guia": "centrar", "mensaje": MENSAJES_CALIDAD["centrar"]})

        with _bloqueo_captura:
            if not _captura or _captura["uid"] != cap["uid"]:
                return jsonify({"success": False, "guia": "inactiva", "mensaje": "La captura ya terminó"})
            ahora = time.time()
            if ahora - _captura["ultima"] < INTERVALO_FOTOS:
                return jsonify({"success": True, "guia": "ok", "completada": False,
                                "tomadas": _captura["tomadas"], "meta": cap["meta"],
                                "mensaje": "Buena posición, mantente quieto"})
            cv2.imwrite(nueva_foto_path(cap["uid"]), imagen)
            _captura["tomadas"] += 1
            _captura["ultima"] = ahora
            tomadas = _captura["tomadas"]
            completada = tomadas >= _captura["meta"]
            if completada:
                _captura = None

        mensaje = "✅ Captura completa" if completada else f"Foto {tomadas} de {cap['meta']} guardada"
        return jsonify({"success": True, "guia": "ok", "completada": completada,
                        "tomadas": tomadas, "meta": cap["meta"], "mensaje": mensaje})
    except RuntimeError as e:
        return jsonify({"success": False, "guia": "error", "mensaje": str(e)})
    except Exception as e:
        return jsonify({"success": False, "guia": "error", "mensaje": f"Error al capturar: {e}"})


# ---------------------------------------------------------------------------
# Rutas: administración de usuarios y pruebas de hardware
# ---------------------------------------------------------------------------
@app.route('/api/usuario/guardar', methods=['POST'])
def api_usuario_guardar():
    data = request.get_json(silent=True) or {}
    uid = (data.get('uid') or '').strip().upper()
    nombre = (data.get('nombre') or '').strip()

    if len(uid) < 8 or not all(c in '0123456789ABCDEF' for c in uid):
        return jsonify({"success": False, "mensaje": "UID inválido (8 o más caracteres hexadecimales)"})
    if not nombre:
        return jsonify({"success": False, "mensaje": "El nombre no puede estar vacío"})

    usuarios = cargar_usuarios_locales()
    usuarios[uid] = nombre
    guardar_usuarios_locales(usuarios)
    sincronizar_firebase("PUT", f"/usuarios/{uid}.json", nombre)
    return jsonify({"success": True, "mensaje": f"Usuario {nombre} guardado"})


@app.route('/api/usuario/eliminar', methods=['POST'])
def api_usuario_eliminar():
    data = request.get_json(silent=True) or {}
    uid = (data.get('uid') or '').strip().upper()

    usuarios = cargar_usuarios_locales()
    if uid not in usuarios:
        return jsonify({"success": False, "mensaje": "Usuario no encontrado"})

    del usuarios[uid]
    guardar_usuarios_locales(usuarios)
    for ruta in fotos_de(uid):
        os.remove(ruta)
    sincronizar_firebase("DELETE", f"/usuarios/{uid}.json")
    return jsonify({"success": True, "mensaje": "Usuario eliminado"})


@app.route('/api/tarjeta/ultima')
def api_tarjeta_ultima():
    """Devuelve la última tarjeta leída por el lector si fue después de 'desde' (segundos epoch)."""
    try:
        desde = float(request.args.get('desde', 0) or 0)
    except ValueError:
        desde = 0.0
    try:
        with open(ARCHIVO_ULTIMA_TARJETA, 'r', encoding='utf-8') as f:
            dato = json.load(f)
    except Exception:
        return jsonify({"uid": None})
    if float(dato.get("ts", 0)) > desde:
        return jsonify({"uid": dato.get("uid"), "ts": dato.get("ts")})
    return jsonify({"uid": None})


@app.route('/api/debug', methods=['POST'])
def api_debug():
    data = request.get_json(silent=True) or {}
    cmd = (data.get('cmd') or '').strip().upper()
    if cmd not in COMANDOS_DEBUG:
        return jsonify({"success": False, "mensaje": "Comando no permitido"})

    encolar_comando({"cmd": cmd, "origen": "DEBUG", "ts": time.time()})
    return jsonify({"success": True, "mensaje": f"Comando {cmd} enviado al lector"})


PUERTO_HTTPS = 5443  # Para el celular (la cámara exige HTTPS fuera de localhost)


def contexto_ssl_celular():
    """Certificado autofirmado guardado en disco: se crea una sola vez.
    Así el celular no tiene que aceptar una advertencia nueva en cada arranque."""
    ruta_cert = os.path.join(DATOS_DIR, "cert_celular.pem")
    ruta_clave = os.path.join(DATOS_DIR, "clave_celular.pem")
    try:
        if not (os.path.exists(ruta_cert) and os.path.exists(ruta_clave)):
            import datetime as dt
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa

            clave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Proyecto Chapa NFC")])
            ahora = dt.datetime.now(dt.timezone.utc)
            certificado = (
                x509.CertificateBuilder()
                .subject_name(nombre)
                .issuer_name(nombre)
                .public_key(clave.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(ahora - dt.timedelta(days=1))
                .not_valid_after(ahora + dt.timedelta(days=3650))
                .sign(clave, hashes.SHA256())
            )
            with open(ruta_clave, "wb") as f:
                f.write(clave.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption()))
            with open(ruta_cert, "wb") as f:
                f.write(certificado.public_bytes(serialization.Encoding.PEM))
        return (ruta_cert, ruta_clave)
    except Exception as e:
        print(f"⚠️ No se pudo guardar el certificado del celular ({e}). Se usa uno temporal.")
        return 'adhoc'


def servir_con_reintentos(nombre, puerto, ssl_context=None):
    """Mantiene el servidor vivo: si el puerto está ocupado o el servidor se cae, vuelve a intentarlo."""
    while True:
        try:
            print(f"🌐 Servidor {nombre} en el puerto {puerto}")
            app.run(host='0.0.0.0', port=puerto, debug=False, threaded=True, ssl_context=ssl_context)
            motivo = "se detuvo"
        except Exception as e:
            motivo = f"no pudo iniciar ({e})"
        print(f"⚠️ Servidor {nombre} (puerto {puerto}) {motivo}. Reintentando en 5 s...")
        time.sleep(5)


if __name__ == '__main__':
    ip_local = obtener_ip_local()
    try:
        import cryptography  # noqa: F401
        tiene_https = True
    except ImportError:
        tiene_https = False

    if tiene_https:
        # Celular: HTTPS con certificado temporal autofirmado (el navegador pedirá aceptarlo una vez)
        mostrar_qr_servidor(f"https://{ip_local}:{PUERTO_HTTPS}/celular")
    else:
        print("⚠️ Falta 'cryptography': el celular no podrá usar la cámara. Instala requirements.txt.")
        mostrar_qr_servidor(f"http://{ip_local}:{PUERTO}/celular")

    # Administrador en la computadora (puerto 5000): se reintenta solo si se cae
    threading.Thread(target=servir_con_reintentos, args=("computadora", PUERTO), daemon=True).start()

    # Sincronización con Firebase (latidos, usuarios, tarjetas de otros equipos)
    threading.Thread(target=bucle_firebase, daemon=True).start()

    if tiene_https:
        servir_con_reintentos("celular", PUERTO_HTTPS, contexto_ssl_celular())
    else:
        while True:
            time.sleep(60)
