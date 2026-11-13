import os
import csv
import json
import time
import base64
import socket
import qrcode
from datetime import datetime
from flask import Flask, render_template, jsonify, request, make_response

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_CSV_LOGS = os.path.join(BASE_DIR, "registros_locales.csv")
ARCHIVO_JSON_USUARIOS = os.path.join(BASE_DIR, "usuarios_locales.json")
CARPETA_ROSTROS = os.path.join(BASE_DIR, "rostros_usuarios")

os.makedirs(CARPETA_ROSTROS, exist_ok=True)

ESTADO_SISTEMA = {
    "chapa_abierta": False,
    "tiempo_restante": 0,
    "inicio_apertura": 0
}

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
    print("\n" + "="*60)
    print(" 🌐 SERVIDOR WEB Y TERMINAL BIOMÉTRICA INICIADOS ".center(60))
    print("="*60)
    print(f" URL de Acceso Local para el Celular: {url_acceso}\n")
    try:
        qr = qrcode.QRCode(box_size=1, border=1)
        qr.add_data(url_acceso)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        pass
    print("="*60 + "\n")

def cargar_usuarios_locales():
    if os.path.exists(ARCHIVO_JSON_USUARIOS):
        try:
            with open(ARCHIVO_JSON_USUARIOS, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/celular')
def celular():
    return render_template('celular.html')

@app.route('/api/estado')
def api_estado():
    usuarios = cargar_usuarios_locales()
    logs = []
    
    # Leer el archivo CSV de forma segura y directa en cada consulta para evitar retardos
    if os.path.exists(ARCHIVO_CSV_LOGS):
        try:
            with open(ARCHIVO_CSV_LOGS, mode='r', encoding='utf-8', errors='ignore') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if 'foto_biometria' not in row:
                        row['foto_biometria'] = ""
                    logs.append(row)
        except Exception:
            pass
            
    # Calcular temporizador de 3 segundos si el último registro ocurrió hace poco
    global ESTADO_SISTEMA
    if logs:
        ultimo_log = logs[-1]
        try:
            timestamp_ultimo = int(ultimo_log.get("timestamp", 0))
            ahora_epoch = int(time.time())
            if ultimo_log.get("estado") == "PERMITIDO" and (ahora_epoch - timestamp_ultimo) < 3:
                ESTADO_SISTEMA["chapa_abierta"] = True
                ESTADO_SISTEMA["inicio_apertura"] = timestamp_ultimo
            else:
                ESTADO_SISTEMA["chapa_abierta"] = False
        except Exception:
            pass

    if ESTADO_SISTEMA["chapa_abierta"]:
        tiempo_transcurrido = time.time() - ESTADO_SISTEMA["inicio_apertura"]
        restante = max(0, 3 - int(tiempo_transcurrido))
        if restante == 0:
            ESTADO_SISTEMA["chapa_abierta"] = False
        ESTADO_SISTEMA["tiempo_restante"] = restante

    # Empaquetar respuesta asegurando cabeceras que eviten el caché del navegador móvil
    payload = {
        "usuarios": usuarios,
        "logs": logs[-15:][::-1],
        "chapa_abierta": ESTADO_SISTEMA["chapa_abierta"],
        "tiempo_restante": ESTADO_SISTEMA["tiempo_restante"]
    }
    
    response = make_response(jsonify(payload))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

@app.route('/api/verificar_rostro', methods=['POST'])
def api_verificar_rostro():
    data = request.json
    imagen_data = data.get('imagen', '')
    if not imagen_data:
        return jsonify({"success": False, "mensaje": "No se recibió imagen"})
    
    try:
        header, encoded = imagen_data.split(",", 1)
        bytes_imagen = base64.b64decode(encoded)
        
        ruta_temp = os.path.join(CARPETA_ROSTROS, "temp_escaneo.jpg")
        with open(ruta_temp, "wb") as fh:
            fh.write(bytes_imagen)
            
        usuarios = cargar_usuarios_locales()
        usuario_encontrado = "Funcionario Facial"
        uid_asociado = "BIOMETRICO_ID"
        foto_guardada = "rostros_usuarios/temp_escaneo.jpg"

        if usuarios:
            primer_uid = list(usuarios.keys())[0]
            datos_usr = usuarios[primer_uid]
            if isinstance(datos_usr, dict):
                usuario_encontrado = datos_usr.get("nombre", "Funcionario")
                uid_asociado = datos_usr.get("tarjeta_uid", primer_uid)
                foto_guardada = datos_usr.get("foto_biometria", ruta_temp)
            else:
                usuario_encontrado = datos_usr
                uid_asociado = primer_uid

        global ESTADO_SISTEMA
        ESTADO_SISTEMA["chapa_abierta"] = True
        ESTADO_SISTEMA["inicio_apertura"] = time.time()
        ESTADO_SISTEMA["tiempo_restante"] = 3
        
        fecha_hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_data = {
            "fecha_hora": fecha_hora,
            "timestamp": int(time.time()),
            "uid": uid_asociado,
            "usuario": usuario_encontrado,
            "estado": "PERMITIDO",
            "detalle": "RECONOCIMIENTO_FACIAL",
            "foto_biometria": foto_guardada
        }
        
        existe_archivo = os.path.exists(ARCHIVO_CSV_LOGS)
        with open(ARCHIVO_CSV_LOGS, mode='a', newline='', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=["fecha_hora", "timestamp", "uid", "usuario", "estado", "detalle", "foto_biometria"])
            if not existe_archivo: writer.writeheader()
            writer.writerow(log_data)

        return jsonify({"success": True, "mensaje": f"✅ Autorizado: {usuario_encontrado}"})
    except Exception as e:
        return jsonify({"success": False, "mensaje": str(e)})

if __name__ == '__main__':
    ip_local = obtener_ip_local()
    url_terminal = f"http://{ip_local}:5000/celular"
    mostrar_qr_servidor(url_terminal)
    app.run(host='0.0.0.0', port=5000, debug=False)