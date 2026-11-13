import os
import csv
import json
import time
from datetime import datetime
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

# Rutas base
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_CSV_LOGS = os.path.join(BASE_DIR, "registros_locales.csv")
ARCHIVO_JSON_USUARIOS = os.path.join(BASE_DIR, "usuarios_locales.json")

# Variable global de estado sincronizado para la chapa y el celular
ESTADO_SISTEMA = {
    "puerto": "COM_ACTIVO",
    "chapa_abierta": False,
    "tiempo_restante": 0,
    "ultimo_evento": "Sistema sincronizado"
}

def cargar_usuarios_locales():
    if os.path.exists(ARCHIVO_JSON_USUARIOS):
        try:
            with open(ARCHIVO_JSON_USUARIOS, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

def guardar_usuarios_locales(usuarios_dict):
    try:
        with open(ARCHIVO_JSON_USUARIOS, 'w', encoding='utf-8') as f:
            json.dump(usuarios_dict, f, ensure_ascii=False, indent=4)
    except:
        pass

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
    if os.path.exists(ARCHIVO_CSV_LOGS):
        try:
            with open(ARCHIVO_CSV_LOGS, mode='r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    logs.append(row)
        except:
            pass
            
    # Calculamos el temporizador en tiempo real para el celular (3 segundos)
    global ESTADO_SISTEMA
    if ESTADO_SISTEMA["chapa_abierta"]:
        tiempo_transcurrido = time.time() - ESTADO_SISTEMA.get("inicio_apertura", time.time())
        restante = max(0, 3 - int(tiempo_transcurrido))
        if restante == 0:
            ESTADO_SISTEMA["chapa_abierta"] = False
        ESTADO_SISTEMA["tiempo_restante"] = restante

    return jsonify({
        "usuarios": usuarios,
        "logs": logs[-15:][::-1],
        "chapa_abierta": ESTADO_SISTEMA["chapa_abierta"],
        "tiempo_restante": ESTADO_SISTEMA["tiempo_restante"]
    })

@app.route('/api/activar_apertura', methods=['POST'])
def api_activar_apertura():
    """Endpoint que sincroniza la apertura de 3 segundos con el celular en tiempo real"""
    global ESTADO_SISTEMA
    ESTADO_SISTEMA["chapa_abierta"] = True
    ESTADO_SISTEMA["inicio_apertura"] = time.time()
    ESTADO_SISTEMA["tiempo_restante"] = 3
    return jsonify({"success": True, "tiempo": 3})

@app.route('/api/usuario/guardar', methods=['POST'])
def api_guardar_usuario():
    data = request.json
    uid = data.get('uid', '').strip().upper()
    nombre = data.get('nombre', '').strip()
    if not uid or not nombre:
        return jsonify({"success": False, "error": "Datos incompletos"})
    
    usuarios = cargar_usuarios_locales()
    usuarios[uid] = nombre
    guardar_usuarios_locales(usuarios)
    return jsonify({"success": True})

@app.route('/api/usuario/eliminar', methods=['POST'])
def api_eliminar_usuario():
    data = request.json
    uid = data.get('uid', '').strip().upper()
    usuarios = cargar_usuarios_locales()
    if uid in usuarios:
        del usuarios[uid]
        guardar_usuarios_locales(usuarios)
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Usuario no encontrado"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)