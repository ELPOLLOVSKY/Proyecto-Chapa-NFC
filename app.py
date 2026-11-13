import os
import csv
import json
import math
import time
import base64
import socket
import requests
import qrcode
try:
    import numpy as np
    import cv2
except ImportError as _e:
    # El servidor debe arrancar aunque falte OpenCV; el reconocimiento facial avisará el problema
    np = None
    cv2 = None
    _ERROR_IMPORT_OPENCV = str(_e)
else:
    _ERROR_IMPORT_OPENCV = None
from datetime import datetime
from flask import Flask, render_template, jsonify, request, make_response, send_from_directory

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_CSV_LOGS = os.path.join(BASE_DIR, "registros_locales.csv")
ARCHIVO_JSON_USUARIOS = os.path.join(BASE_DIR, "usuarios_locales.json")
ARCHIVO_COLA_DEBUG = os.path.join(BASE_DIR, "cola_debug.json")
CARPETA_ROSTROS = os.path.join(BASE_DIR, "rostros_usuarios")
CARPETA_MODELOS = os.path.join(BASE_DIR, "modelos")
ARCHIVO_YUNET = os.path.join(CARPETA_MODELOS, "face_detection_yunet_2023mar.onnx")
ARCHIVO_SFACE = os.path.join(CARPETA_MODELOS, "face_recognition_sface_2021dec.onnx")

URL_FIREBASE = "https://registros-acceso-nfc-default-rtdb.firebaseio.com"
CAMPOS_LOG = ["fecha_hora", "timestamp", "uid", "usuario", "estado", "detalle"]
COMANDOS_DEBUG = {"DBG_START", "DBG_CHAPA_ON", "DBG_CHAPA_OFF", "OK"}
UMBRAL_SIMILITUD = 0.363  # Umbral de coseno recomendado por OpenCV para SFace
SEGUNDOS_APERTURA = 3

os.makedirs(CARPETA_ROSTROS, exist_ok=True)

_detector = None
_reconocedor = None
_cache_rasgos = {}  # uid -> (mtime de la foto, rasgos faciales)


# ---------------------------------------------------------------------------
# Utilidades generales
# ---------------------------------------------------------------------------
def obtener_ip_local():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


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
    """Replica un cambio en la nube. Si no hay conexión, se ignora (queda el respaldo local)."""
    try:
        requests.request(metodo, f"{URL_FIREBASE}{ruta}", json=cuerpo, timeout=2.5)
    except Exception:
        pass


def leer_logs():
    logs = []
    if os.path.exists(ARCHIVO_CSV_LOGS):
        try:
            with open(ARCHIVO_CSV_LOGS, mode='r', encoding='utf-8', errors='ignore') as f:
                for row in csv.DictReader(f):
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


# ---------------------------------------------------------------------------
# Reconocimiento facial (OpenCV: YuNet para detectar, SFace para comparar)
# ---------------------------------------------------------------------------
def cargar_modelos_rostro():
    global _detector, _reconocedor
    if cv2 is None:
        raise RuntimeError(f"Falta OpenCV en este Python ({_ERROR_IMPORT_OPENCV}). Instala: pip install opencv-python numpy")
    if _detector is not None and _reconocedor is not None:
        return
    if not (os.path.exists(ARCHIVO_YUNET) and os.path.exists(ARCHIVO_SFACE)):
        raise RuntimeError("Faltan los modelos de rostro en la carpeta 'modelos'")
    _detector = cv2.FaceDetectorYN.create(ARCHIVO_YUNET, "", (320, 320), 0.8, 0.3, 5000)
    _reconocedor = cv2.FaceRecognizerSF.create(ARCHIVO_SFACE, "")


def decodificar_imagen(data_url):
    if cv2 is None:
        raise RuntimeError(f"Falta OpenCV en este Python ({_ERROR_IMPORT_OPENCV}). Instala: pip install opencv-python numpy")
    _, encoded = data_url.split(",", 1)
    buffer = np.frombuffer(base64.b64decode(encoded), dtype=np.uint8)
    return cv2.imdecode(buffer, cv2.IMREAD_COLOR)


def extraer_rasgos(imagen):
    """Devuelve los rasgos faciales de la cara más grande de la imagen, o None si no hay cara."""
    cargar_modelos_rostro()
    alto, ancho = imagen.shape[:2]
    _detector.setInputSize((ancho, alto))
    _, caras = _detector.detect(imagen)
    if caras is None or len(caras) == 0:
        return None
    cara = max(caras, key=lambda c: c[2] * c[3])
    alineada = _reconocedor.alignCrop(imagen, cara)
    return _reconocedor.feature(alineada)


def rasgos_de_foto_usuario(uid):
    ruta = ruta_foto(uid)
    if not os.path.exists(ruta):
        return None
    mtime = os.path.getmtime(ruta)
    cache = _cache_rasgos.get(uid)
    if cache and cache[0] == mtime:
        return cache[1]
    imagen = cv2.imread(ruta)
    rasgos = extraer_rasgos(imagen) if imagen is not None else None
    _cache_rasgos[uid] = (mtime, rasgos)
    return rasgos


def similitud(rasgos_a, rasgos_b):
    return _reconocedor.match(rasgos_a, rasgos_b, cv2.FaceRecognizerSF_FR_COSINE)


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
# Rutas: estado para el teléfono y el administrador
# ---------------------------------------------------------------------------
@app.route('/api/estado')
def api_estado():
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
        foto_url = f"/rostros_usuarios/{uid}.jpg" if uid and os.path.exists(ruta_foto(uid)) else None
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

    payload = {
        "usuarios": usuarios,
        "logs": logs[-15:][::-1],
        "ultimo": ultimo,
        "chapa_abierta": chapa_abierta,
        "tiempo_restante": tiempo_restante,
    }
    response = make_response(jsonify(payload))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


# ---------------------------------------------------------------------------
# Rutas: reconocimiento facial
# ---------------------------------------------------------------------------
@app.route('/api/verificar_rostro', methods=['POST'])
def api_verificar_rostro():
    data = request.get_json(silent=True) or {}
    imagen_data = data.get('imagen', '')
    if not imagen_data:
        return jsonify({"success": False, "mensaje": "No se recibió imagen"})

    try:
        imagen = decodificar_imagen(imagen_data)
        if imagen is None:
            return jsonify({"success": False, "mensaje": "La imagen no es válida"})

        rasgos_vivo = extraer_rasgos(imagen)
        if rasgos_vivo is None:
            registrar_log("", "Desconocido", "DENEGADO", "ROSTRO_NO_DETECTADO")
            return jsonify({"success": False, "mensaje": "❌ No se detectó un rostro"})

        mejor_uid, mejor_similitud = None, -1.0
        for uid in cargar_usuarios_locales():
            rasgos = rasgos_de_foto_usuario(uid)
            if rasgos is None:
                continue
            s = similitud(rasgos_vivo, rasgos)
            if s > mejor_similitud:
                mejor_uid, mejor_similitud = uid, s

        if mejor_uid is not None and mejor_similitud >= UMBRAL_SIMILITUD:
            nombre = nombre_de(cargar_usuarios_locales()[mejor_uid])
            registrar_log(mejor_uid, nombre, "PERMITIDO", "RECONOCIMIENTO_FACIAL")
            return jsonify({"success": True, "mensaje": f"✅ Autorizado: {nombre}"})

        registrar_log("", "Desconocido", "DENEGADO", "ROSTRO_NO_RECONOCIDO")
        return jsonify({"success": False, "mensaje": "❌ Rostro no reconocido"})

    except RuntimeError as e:
        return jsonify({"success": False, "mensaje": str(e)})
    except Exception as e:
        return jsonify({"success": False, "mensaje": f"Error al verificar: {e}"})


@app.route('/api/usuario/foto', methods=['POST'])
def api_usuario_foto():
    """Guarda la foto de registro de un usuario ya asociado a una tarjeta."""
    data = request.get_json(silent=True) or {}
    uid = (data.get('uid') or '').strip().upper()
    if uid not in cargar_usuarios_locales():
        return jsonify({"success": False, "mensaje": "Primero guarda el usuario con su tarjeta"})

    try:
        imagen = decodificar_imagen(data.get('imagen', ''))
        if imagen is None:
            return jsonify({"success": False, "mensaje": "La imagen no es válida"})
        if extraer_rasgos(imagen) is None:
            return jsonify({"success": False, "mensaje": "No se detectó un rostro en la foto"})

        cv2.imwrite(ruta_foto(uid), imagen)
        _cache_rasgos.pop(uid, None)
        return jsonify({"success": True, "mensaje": "✅ Rostro guardado para este usuario"})
    except RuntimeError as e:
        return jsonify({"success": False, "mensaje": str(e)})
    except Exception as e:
        return jsonify({"success": False, "mensaje": f"Error al guardar la foto: {e}"})


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
    if os.path.exists(ruta_foto(uid)):
        os.remove(ruta_foto(uid))
    _cache_rasgos.pop(uid, None)
    sincronizar_firebase("DELETE", f"/usuarios/{uid}.json")
    return jsonify({"success": True, "mensaje": "Usuario eliminado"})


@app.route('/api/debug', methods=['POST'])
def api_debug():
    """Deja el comando en una cola. El motor (Proyecto Chapa NFC.py) lo envía al Arduino."""
    data = request.get_json(silent=True) or {}
    cmd = (data.get('cmd') or '').strip().upper()
    if cmd not in COMANDOS_DEBUG:
        return jsonify({"success": False, "mensaje": "Comando no permitido"})

    cola = []
    if os.path.exists(ARCHIVO_COLA_DEBUG):
        try:
            with open(ARCHIVO_COLA_DEBUG, 'r', encoding='utf-8') as f:
                cola = json.load(f)
        except Exception:
            cola = []
    cola.append({"cmd": cmd, "ts": time.time()})
    with open(ARCHIVO_COLA_DEBUG, 'w', encoding='utf-8') as f:
        json.dump(cola, f)
    return jsonify({"success": True, "mensaje": f"Comando {cmd} enviado al lector"})


if __name__ == '__main__':
    ip_local = obtener_ip_local()
    # La cámara del navegador en el celular solo funciona con HTTPS (o localhost).
    # Si está disponible 'cryptography', se usa un certificado temporal autofirmado.
    try:
        import cryptography  # noqa: F401
        ssl_context = 'adhoc'
    except ImportError:
        ssl_context = None
    protocolo = "https" if ssl_context else "http"
    url_terminal = f"{protocolo}://{ip_local}:5000/celular"
    mostrar_qr_servidor(url_terminal)
    try:
        app.run(host='0.0.0.0', port=5000, debug=False, ssl_context=ssl_context)
    except Exception as e:
        # Si HTTPS falla (por ejemplo, certificado no creable), se sirve por HTTP para no dejar el sistema sin web
        print(f"⚠️ HTTPS no disponible ({e}). Sirviendo por HTTP.")
        mostrar_qr_servidor(f"http://{ip_local}:5000/celular")
        app.run(host='0.0.0.0', port=5000, debug=False)
