import os
import sys
import csv
import json
import math
import time
import base64
import socket
import threading
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

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_CSV_LOGS = os.path.join(BASE_DIR, "registros_locales.csv")
ARCHIVO_JSON_USUARIOS = os.path.join(BASE_DIR, "usuarios_locales.json")
ARCHIVO_COLA_DEBUG = os.path.join(BASE_DIR, "cola_debug.json")
ARCHIVO_ULTIMA_TARJETA = os.path.join(BASE_DIR, "ultima_tarjeta.json")
CARPETA_ROSTROS = os.path.join(BASE_DIR, "rostros_usuarios")
CARPETA_MODELOS = os.path.join(BASE_DIR, "modelos")
ARCHIVO_YUNET = os.path.join(CARPETA_MODELOS, "face_detection_yunet_2023mar.onnx")
ARCHIVO_SFACE = os.path.join(CARPETA_MODELOS, "face_recognition_sface_2021dec.onnx")

PUERTO = 5000
URL_FIREBASE = "https://registros-acceso-nfc-default-rtdb.firebaseio.com"
CAMPOS_LOG = ["fecha_hora", "timestamp", "uid", "usuario", "estado", "detalle"]
COMANDOS_DEBUG = {"DBG_START", "DBG_CHAPA_ON", "DBG_CHAPA_OFF", "OK"}
UMBRAL_SIMILITUD = 0.363  # Umbral de coseno recomendado por OpenCV para SFace
SEGUNDOS_APERTURA = 3
SEGUNDOS_ENVIO_ESTADO = 1.0  # Aunque no cambie nada, se reenvía el estado cada segundo (cuenta regresiva)

os.makedirs(CARPETA_ROSTROS, exist_ok=True)

from sincronizacion_firebase import Sincronizacion  # noqa: E402
sync = Sincronizacion(URL_FIREBASE)

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
    """Replica un cambio en la nube. Si no hay conexión, se ignora (queda el respaldo local)."""
    try:
        requests.request(metodo, f"{URL_FIREBASE}{ruta}", json=cuerpo, timeout=2.5)
    except Exception:
        pass


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

    return {
        "usuarios": usuarios,
        "logs": logs[-15:][::-1],
        "ultimo": ultimo,
        "chapa_abierta": chapa_abierta,
        "tiempo_restante": tiempo_restante,
        "dispositivos": sync.estado_dispositivos(),
        "firebase_ok": sync.ultimo_error is None,
        "latencia_firebase_ms": sync.latencia_ms,
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

    try:
        imagen = decodificar_imagen(imagen_data)
        if imagen is None:
            return jsonify({"success": False, "mensaje": "La imagen no es válida"})

        rasgos_vivo = extraer_rasgos(imagen)
        if rasgos_vivo is None:
            # Sin rostro frente a la cámara: no es un evento, no se registra
            return jsonify({"success": False, "sin_rostro": True, "mensaje": "Sin rostro"})

        usuarios = cargar_usuarios_locales()
        mejor_uid, mejor_similitud = None, -1.0
        for uid in usuarios:
            rasgos = rasgos_de_foto_usuario(uid)
            if rasgos is None:
                continue
            s = similitud(rasgos_vivo, rasgos)
            if s > mejor_similitud:
                mejor_uid, mejor_similitud = uid, s

        if mejor_uid is not None and mejor_similitud >= UMBRAL_SIMILITUD:
            nombre = nombre_de(usuarios[mejor_uid])
            # Si la misma persona acaba de entrar, no se vuelve a abrir ni a registrar
            if not ultimo_evento_coincide(mejor_uid, "PERMITIDO", "RECONOCIMIENTO_FACIAL", 8):
                registrar_log(mejor_uid, nombre, "PERMITIDO", "RECONOCIMIENTO_FACIAL")
                # Abrir la chapa: el motor recibe la orden y la envía al Arduino
                encolar_comando({"cmd": "OK", "origen": "FACIAL", "uid": mejor_uid,
                                 "nombre": nombre, "ts": time.time()})
            return jsonify({"success": True, "mensaje": f"✅ Autorizado: {nombre}"})

        # Rostro desconocido frente a la cámara: se registra como máximo una vez cada 5 segundos
        if not ultimo_evento_coincide("", "DENEGADO", "ROSTRO_NO_RECONOCIDO", 5):
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


def iniciar_http_local(ip_local):
    """Computadora: HTTP en el puerto 5000. 'localhost' es origen seguro, así que la cámara funciona igual."""
    print(f"💻 Administrador en la computadora: http://127.0.0.1:{PUERTO}/")
    app.run(host='0.0.0.0', port=PUERTO, debug=False, threaded=True)


if __name__ == '__main__':
    for p in (PUERTO, PUERTO_HTTPS):
        if puerto_ocupado(p):
            print(f"⚠️ El puerto {p} ya está en uso: hay otro servidor web abierto.")
            print("   Cierra la otra ventana o reinicia el equipo, y vuelve a iniciar el sistema.")
            sys.exit(1)

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

    # HTTP en segundo plano para la computadora
    hilo_http = threading.Thread(target=iniciar_http_local, args=(ip_local,), daemon=True)
    hilo_http.start()

    # Sincronización con Firebase (latidos, usuarios, tarjetas de otros equipos)
    threading.Thread(target=bucle_firebase, daemon=True).start()

    if tiene_https:
        try:
            app.run(host='0.0.0.0', port=PUERTO_HTTPS, debug=False, threaded=True, ssl_context='adhoc')
        except Exception as e:
            print(f"⚠️ HTTPS no disponible en el puerto {PUERTO_HTTPS} ({e}). El administrador sigue funcionando en http://127.0.0.1:{PUERTO}/")
            hilo_http.join()
    else:
        hilo_http.join()
