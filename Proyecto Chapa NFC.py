import serial
import serial.tools.list_ports
import time
from datetime import datetime
import os
import csv
import json
import msvcrt
import pyttsx3
import requests
import subprocess
import sys
import threading
from cola_nube import ColaNube

# =========================================================================
# CONFIGURACIÓN DE FIREBASE Y ARCHIVOS LOCALES
# =========================================================================
PUERTO_COM_ACTUAL = "DESCONOCIDO"
BAUD_RATE = 9600
TECLA_MENU_OCULTO = 'm'

URL_BASE = 'https://registros-acceso-nfc-default-rtdb.firebaseio.com'

def obtener_ruta_base():
    """Detecta la carpeta raíz real donde se ejecuta el script para asegurar portabilidad."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))

RUTA_BASE = obtener_ruta_base()

# Registros de acceso que no llegaron a Firebase (se reenvían al volver la conexión)
cola_nube_motor = ColaNube(os.path.join(RUTA_BASE, "pendientes_nube_motor.json"), URL_BASE)
# Cambios de usuarios pendientes en la app web: si existen, no se pisan con los datos de la nube
RUTA_COLA_APP = os.path.join(RUTA_BASE, "pendientes_nube_app.json")
ARCHIVO_CSV_LOGS = os.path.join(RUTA_BASE, "registros_locales.csv")
ARCHIVO_JSON_USUARIOS = os.path.join(RUTA_BASE, "usuarios_locales.json")
ARCHIVO_COLA_DEBUG = os.path.join(RUTA_BASE, "cola_debug.json")
COMANDOS_DEBUG_PERMITIDOS = {"DBG_START", "DBG_CHAPA_ON", "DBG_CHAPA_OFF", "OK"}

# Variables globales de contadores
cant_permitidos = 0
cant_denegados = 0
porcentaje_actual_carga = 0

# =========================================================================
# AUTO-ARRANQUE DEL SERVIDOR WEB EN SEGUNDO PLANO (HILO AUTOMÁTICO)
# =========================================================================
_proceso_web = None
_ultimo_intento_web = 0.0

def iniciar_servidor_web_automatico():
    """Lanza el servidor app.py automáticamente en segundo plano al iniciar el motor."""
    global _proceso_web
    script_web = os.path.join(RUTA_BASE, "app.py")
    if os.path.exists(script_web):
        try:
            python_exe = sys.executable.replace("pythonw.exe", "python.exe")
            # Los errores del servidor web se guardan en web_app.log para poder diagnosticarlos
            log_web = open(os.path.join(RUTA_BASE, "web_app.log"), "a", encoding="utf-8")
            log_web.write(f"\n--- Arranque {datetime.now()} ---\n")
            log_web.flush()
            _proceso_web = subprocess.Popen([python_exe, script_web], cwd=RUTA_BASE, stdout=log_web, stderr=subprocess.STDOUT)
        except Exception as e:
            print(f"⚠️ No se pudo iniciar el servidor web: {e}")

_espera_web = 5
_fallos_web = 0

def ultimas_lineas_log_web(n=15):
    try:
        with open(os.path.join(RUTA_BASE, "web_app.log"), encoding="utf-8", errors="ignore") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return "(no se pudo leer web_app.log)"

def vigilar_servidor_web():
    """Si el servidor web (app.py) se cerró, lo vuelve a levantar con esperas crecientes.
    Muestra el final de web_app.log para ver el error real."""
    global _ultimo_intento_web, _espera_web, _fallos_web
    if _proceso_web is None or _proceso_web.poll() is None:
        if _proceso_web is not None and _fallos_web and time.time() - _ultimo_intento_web > 60:
            _fallos_web = 0
            _espera_web = 5
        return
    if time.time() - _ultimo_intento_web < _espera_web:
        return
    _fallos_web += 1
    print(f"\n⚠️ El servidor web se cerró (código {_proceso_web.returncode}), intento {_fallos_web}.")
    print("   Últimas líneas de web_app.log:")
    print(ultimas_lineas_log_web())
    _espera_web = min(60, 5 * _fallos_web)
    _ultimo_intento_web = time.time()
    iniciar_servidor_web_automatico()

# =========================================================================
# AUTO-DETECCIÓN INTELIGENTE DE PUERTO COM SERIE
# =========================================================================
def autodetectar_puerto_arduino():
    """Escanea los puertos COM activos e identifica la placa Arduino automáticamente."""
    puertos = list(serial.tools.list_ports.comports())
    if not puertos:
        return None

    palabras_clave = ["arduino", "ch340", "ftdi", "usb-serial", "usb serial", "cp210x"]
    for p in puertos:
        desc = p.description.lower()
        hwid = p.hwid.lower()
        if any(clave in desc or clave in hwid for clave in palabras_clave):
            return p.device

    return puertos[0].device

# =========================================================================
# SÍNTESIS DE VOZ Y BARRA DE CARGA FLUIDA EN CONSOLA
# =========================================================================
_bloqueo_voz = threading.Lock()

def hablar(texto):
    """Inicializa y destruye el motor en cada llamada. Se ejecuta en un hilo aparte para no frenar la lectura."""
    with _bloqueo_voz:
        try:
            try:
                import pythoncom  # Windows: COM debe iniciarse en cada hilo que use la voz
                pythoncom.CoInitialize()
            except Exception:
                pass
            engine_local = pyttsx3.init()
            engine_local.setProperty('rate', 170)
            engine_local.say(texto)
            engine_local.runAndWait()
            engine_local.stop()
            del engine_local
        except Exception as e:
            print(f"⚠️ Error en sintetizador de voz: {e}")

def limpiar_pantalla():
    os.system('cls' if os.name == 'nt' else 'clear')

def renderizar_barra_grafica(porcentaje, texto_estado):
    limpiar_pantalla()
    ancho_barra = 30
    llenos = int(ancho_barra * porcentaje // 100)
    barra = '█' * llenos + '░' * (ancho_barra - llenos)
    
    print("\n" * 2)
    print("=========================================================".center(60))
    print("   INICIALIZANDO SISTEMA DE CONTROL DE ACCESO NFC 🚀    ".center(60))
    print("=========================================================".center(60))
    print("\n")
    print(f"[{barra}] {porcentaje}%".center(60))
    print("\n")
    print(f"Estado: {texto_estado}".center(60))
    print("\n" * 2)
    print("=========================================================".center(60))

def animar_carga_hasta(porcentaje_destino, texto_estado, pasitos=5, demora=0.015):
    global porcentaje_actual_carga
    if porcentaje_destino < porcentaje_actual_carga:
        porcentaje_actual_carga = porcentaje_destino
        renderizar_barra_grafica(porcentaje_actual_carga, texto_estado)
        return

    diferencia = porcentaje_destino - porcentaje_actual_carga
    if diferencia <= 0:
        renderizar_barra_grafica(porcentaje_actual_carga, texto_estado)
        return

    paso_val = diferencia / pasitos
    for _ in range(pasitos):
        porcentaje_actual_carga += paso_val
        p_entero = int(porcentaje_actual_carga)
        renderizar_barra_grafica(p_entero, texto_estado)
        time.sleep(demora)
        
    porcentaje_actual_carga = porcentaje_destino
    renderizar_barra_grafica(porcentaje_destino, texto_estado)

def precargar_estadisticas_iniciales():
    global cant_permitidos, cant_denegados
    cant_permitidos = 0
    cant_denegados = 0
    
    try:
        resp = requests.get(f"{URL_BASE}/logs.json", timeout=2.5)
        if resp.status_code == 200 and resp.json():
            logs_nube = resp.json()
            for key, val in logs_nube.items():
                est = val.get("estado", "")
                if est == "PERMITIDO":
                    cant_permitidos += 1
                elif est == "DENEGADO":
                    cant_denegados += 1
            return
    except Exception:
        pass

    if os.path.exists(ARCHIVO_CSV_LOGS):
        try:
            with open(ARCHIVO_CSV_LOGS, mode='r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for r in reader:
                    est = r.get("estado", "")
                    if est == "PERMITIDO":
                        cant_permitidos += 1
                    elif est == "DENEGADO":
                        cant_denegados += 1
        except Exception:
            pass

def cargar_usuarios_locales():
    if os.path.exists(ARCHIVO_JSON_USUARIOS):
        try:
            with open(ARCHIVO_JSON_USUARIOS, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def guardar_usuarios_locales(usuarios_dict):
    try:
        with open(ARCHIVO_JSON_USUARIOS, 'w', encoding='utf-8') as f:
            json.dump(usuarios_dict, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"⚠️ Error al guardar respaldo local de usuarios: {e}")

def obtener_usuarios_hibrido():
    try:
        response = requests.get(f"{URL_BASE}/usuarios.json", timeout=2.5)
        if response.status_code == 200:
            data = response.json()
            if data is None:
                data = {}
            if ColaNube(RUTA_COLA_APP, URL_BASE).pendientes() == 0:
                guardar_usuarios_locales(data)
            else:
                data = cargar_usuarios_locales()  # hay cambios locales sin subir: no se pisan
            return data, True
    except Exception:
        pass
    
    usuarios_locales = cargar_usuarios_locales()
    return usuarios_locales, False

# ---------------------------------------------------------------------------
# Tareas que no deben frenar la lectura del puerto (Firebase, voz, cuenta regresiva)
# ---------------------------------------------------------------------------
def en_segundo_plano(funcion, *args):
    threading.Thread(target=funcion, args=args, daemon=True).start()

_cache_usuarios = {"valor": None}
_bloqueo_cache = threading.Lock()

def _actualizar_cache_usuarios():
    valor = obtener_usuarios_hibrido()
    with _bloqueo_cache:
        _cache_usuarios["valor"] = valor

def usuarios_en_cache(uid=None):
    """Devuelve (usuarios, online) sin esperar a Firebase en cada tarjeta.
    Si la tarjeta no está en la copia, pide la lista actual antes de decidir."""
    with _bloqueo_cache:
        valor = _cache_usuarios["valor"]
    if valor is None:
        return cargar_usuarios_locales(), False
    if uid and uid not in valor[0]:
        _actualizar_cache_usuarios()
        with _bloqueo_cache:
            valor = _cache_usuarios["valor"]
    return valor

def _refrescar_cache_usuarios():
    while True:
        time.sleep(10)
        try:
            _actualizar_cache_usuarios()
        except Exception:
            pass

def _enviar_log_nube(log_data):
    # Si no hay conexión, el registro queda pendiente y se reenvía después
    cola_nube_motor.enviar("POST", "/logs.json", log_data)

def _vaciar_cola_motor():
    while True:
        time.sleep(10)
        try:
            cola_nube_motor.vaciar()
        except Exception:
            pass

def registrar_evento_local_y_nube(uid, nombre, estado, detalle=""):
    global cant_permitidos, cant_denegados
    if estado == "PERMITIDO":
        cant_permitidos += 1
    elif estado == "DENEGADO":
        cant_denegados += 1

    fecha_hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timestamp_epoch = int(time.time())
    
    log_data = {
        "fecha_hora": fecha_hora,
        "timestamp": timestamp_epoch,
        "uid": uid,
        "usuario": nombre,
        "estado": estado,
        "detalle": detalle
    }
    
    existe_archivo = os.path.exists(ARCHIVO_CSV_LOGS)
    try:
        with open(ARCHIVO_CSV_LOGS, mode='a', newline='', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=["fecha_hora", "timestamp", "uid", "usuario", "estado", "detalle"])
            if not existe_archivo:
                writer.writeheader()
            writer.writerow(log_data)
    except Exception as e:
        print(f"⚠️ Error al guardar en CSV local: {e}")

    try:
        threading.Thread(target=_enviar_log_nube, args=(log_data,), daemon=True).start()
    except Exception:
        pass
        
    return fecha_hora

def imprimir_encabezado_escucha():
    limpiar_pantalla()
    total_accesos = cant_permitidos + cant_denegados
    print("=========================================================")
    print("  CONTROL ACCESO NFC (GOOGLE FIREBASE + RESPALDO LOCAL 📁)")
    print("=========================================================")
    print(f" Status: MONITOREANDO LECTURAS EN TIEMPO REAL [{PUERTO_COM_ACTUAL}]")
    print("---------------------------------------------------------")
    print(f" 📊 CONTADOR ACUMULADO:")
    print(f"    • Total Lecturas : {total_accesos}")
    print(f"    • Permitidos (✅): {cant_permitidos}")
    print(f"    • Denegados  (❌): {cant_denegados}")
    print("---------------------------------------------------------")
    print(f" Tip: Presiona la tecla '{TECLA_MENU_OCULTO.upper()}' para entrar al MENÚ PRINCIPAL")
    print("=========================================================\n")

def ejecutar_cuenta_regresiva_chapa(segundos=3):  # <--- AJUSTADO A 3 SEGUNDOS
    for restante in range(segundos, 0, -1):
        print(f"\r⚡ [PULSOS DE CHAPA ACTIVOS] -> Bloqueando en: {restante} seg... ", end="", flush=True)
        time.sleep(1)
    print("\r🔒 [CHAPA DESACTIVADA] Estado normal restablecido.                    \n")

ARCHIVO_ULTIMA_TARJETA = os.path.join(RUTA_BASE, "ultima_tarjeta.json")
_ultima_lectura = {"uid": None, "ts": 0.0}

def guardar_ultima_tarjeta(uid):
    """Deja la última tarjeta leída para que la interfaz web pueda usarla al registrar."""
    try:
        with open(ARCHIVO_ULTIMA_TARJETA, 'w', encoding='utf-8') as f:
            json.dump({"uid": uid, "ts": time.time()}, f)
    except OSError:
        pass

def es_lectura_repetida(uid, ventana=2.0):
    """True si es la misma tarjeta leída hace menos de 'ventana' segundos (el lector la reenvía mientras está apoyada)."""
    ahora = time.time()
    repetida = uid == _ultima_lectura["uid"] and (ahora - _ultima_lectura["ts"]) < ventana
    _ultima_lectura["uid"] = uid
    _ultima_lectura["ts"] = ahora
    return repetida

def procesar_cola_debug(ser):
    """Envía al Arduino los comandos de prueba que la interfaz web deja en cola (app.py)."""
    if not os.path.exists(ARCHIVO_COLA_DEBUG):
        return
    # Se renombra primero para que la web pueda escribir una cola nueva sin pisar esta lectura
    en_proceso = ARCHIVO_COLA_DEBUG + ".proc"
    try:
        os.replace(ARCHIVO_COLA_DEBUG, en_proceso)
        with open(en_proceso, 'r', encoding='utf-8') as f:
            cola = json.load(f)
        os.remove(en_proceso)
    except Exception:
        return
    # Solo se ejecuta lo reciente: un acceso facial de hace más de 4 s o un comando de prueba
    # de hace más de 10 s ya no corresponde. Entre varios accesos faciales, solo el más nuevo.
    ahora = time.time()
    def vigente(item):
        limite = 4 if item.get("origen") == "FACIAL" else 10
        return ahora - float(item.get("ts", 0)) <= limite
    vigentes = [i for i in cola if vigente(i)]
    faciales = [i for i in vigentes if i.get("origen") == "FACIAL"]
    otros = [i for i in vigentes if i.get("origen") != "FACIAL"]
    if faciales:
        otros.append(max(faciales, key=lambda i: float(i.get("ts", 0))))

    for item in otros:
        cmd = str(item.get("cmd", "")).upper()
        if cmd not in COMANDOS_DEBUG_PERMITIDOS:
            continue
        try:
            ser.write(f"{cmd}\n".encode('utf-8'))
            ser.flush()
        except (serial.SerialException, OSError):
            return

        # Acceso por reconocimiento facial: el motor muestra y anuncia la persona, y cuenta la apertura
        if item.get("origen") == "FACIAL":
            nombre = str(item.get("nombre", "Usuario"))
            uid = str(item.get("uid", ""))
            fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            imprimir_encabezado_escucha()
            print(f"[{fecha}] 🔓 AUTORIZADO POR ROSTRO -> Usuario: {nombre} | UID: {uid}")
            hablar(f"Acceso autorizado. Bienvenido {nombre}.")
            ejecutar_cuenta_regresiva_chapa(3)
            # Lo que se leyó mientras tanto (tarjetas apoyadas, etc.) ya no corresponde: se descarta
            try:
                ser.reset_input_buffer()
            except (serial.SerialException, OSError):
                pass
            imprimir_encabezado_escucha()

def capturar_tecla_instantanea():
    if msvcrt.kbhit():
        return msvcrt.getch().decode('utf-8', errors='ignore').lower()
    return None

def esperar_tecla_instantanea():
    return msvcrt.getch().decode('utf-8', errors='ignore').lower()

def capturar_entrada_hibrida(ser, prompt_texto="Opción / Búsqueda / Tarjeta: "):
    print(prompt_texto, end="", flush=True)
    buffer_teclado = ""
    try:
        ser.reset_input_buffer()
    except Exception:
        pass

    while True:
        try:
            if ser.in_waiting > 0:
                linea = ser.readline().decode('utf-8', errors='ignore').strip()
                if linea.startswith("CARD:"):
                    uid = linea.split(":")[1].upper()
                    print(f"\n📡 [LECTOR NFC] Tarjeta detectada: {uid}")
                    return f"NFC:{uid}"
        except Exception:
            pass

        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in [b'\r', b'\n']:
                print()
                return buffer_teclado.strip()
            elif ch == b'\x08':
                if len(buffer_teclado) > 0:
                    buffer_teclado = buffer_teclado[:-1]
                    print("\b \b", end="", flush=True)
            else:
                try:
                    char_str = ch.decode('utf-8')
                    buffer_teclado += char_str
                    print(char_str, end="", flush=True)
                except Exception:
                    pass

        time.sleep(0.02)

def lanzar_script_analytics_cascada():
    limpiar_pantalla()
    print("==================================================")
    print("  INICIANDO SCRIPT DE ANALÍTICA EN CASCADA 🚀    ")
    print("==================================================")
    
    script_path = os.path.join(RUTA_BASE, "generar_analytics_firebase.py")
    python_exe = sys.executable.replace("pythonw.exe", "python.exe")
    
    print(f"🔍 Buscando script en: {script_path}\n")

    if not os.path.exists(script_path):
        print(f"❌ Error: No se encontró 'generar_analytics_firebase.py'")
        input("\nPresiona ENTER para volver al menú...")
        return

    try:
        if os.name == 'nt':
            subprocess.Popen([python_exe, script_path], creationflags=subprocess.CREATE_NEW_CONSOLE, cwd=RUTA_BASE)
        else:
            subprocess.Popen([python_exe, script_path], cwd=RUTA_BASE)
            
        print("✅ ¡Ventana de analítica lanzada con éxito!")
    except Exception as e:
        print(f"❌ Error al lanzar el script de analítica: {e}")
    
    input("\nPresiona ENTER para volver al menú...")

# =========================================================================
# SUBMENÚ 3: DEBUG Y PRUEBAS DE HARDWARE
# =========================================================================
def submenu_debug(ser):
    try:
        ser.write(b"DBG_START\n")
        ser.flush()
    except (serial.SerialException, OSError):
        return

    while True:
        limpiar_pantalla()
        print("==========================================================")
        print("    [3] MODO DEBUG / PRUEBAS DE HARDWARE (PIN 6 / LED 'L') ")
        print("==========================================================")
        print(" [!] LED 'L' de placa parpadea LENTO (2s Prende / 2s Apaga)")
        print("----------------------------------------------------------")
        print(" [1] DAR CORRIENTE CONTINUA A CHAPA  (Pin 6 en HIGH Fijo)")
        print(" [2] CORTAR CORRIENTE DE LA CHAPA    (Pin 6 en LOW / Paro)")
        print(" [3] Reanudar parpadeo 2s/2s del LED 'L'")
        print(" [4] Test de pulso de apertura (3 seg con cuenta regresiva)")
        print(" [0] VOLVER AL MENÚ PRINCIPAL        (Corta Pin 6 / LED Fijo)")
        print("==========================================================")
        
        tecla = esperar_tecla_instantanea()
        
        try:
            if tecla == '1':
                ser.write(b"DBG_CHAPA_ON\n")
                ser.flush()
                print("\n⚡ [CORRIENTE ACTIVADA] Mandando 5V CONTINUOS...")
                print("--> Presiona [2] para CORTAR la corriente.")
                time.sleep(1.2)
            elif tecla == '2':
                ser.write(b"DBG_CHAPA_OFF\n")
                ser.flush()
                print("\n⛔ [CORRIENTE CORTADA] Chapa desactivada.")
                time.sleep(1)
            elif tecla == '3':
                ser.write(b"DBG_START\n")
                ser.flush()
                print("\n🔄 Parpadeo lento (2s Prende / 2s Apaga) reanudado.")
                time.sleep(1)
            elif tecla == '4':
                ser.write(b"OK\n")
                ser.flush()
                print("\n")
                ejecutar_cuenta_regresiva_chapa(3)  # <--- AJUSTADO A 3 SEGUNDOS
                time.sleep(0.5)
            elif tecla == '0':
                ser.write(b"DBG_RESET\n")
                ser.flush()
                break
        except (serial.SerialException, OSError):
            print("\n❌ Error de comunicación serial en Debug. Reintentando...")
            time.sleep(1.5)
            break

# =========================================================================
# LÓGICA DE REGISTRO / EDICIÓN / CONSULTA / ELIMINACIÓN CON FILTRO
# =========================================================================
def agregar_o_editar_usuario(ser):
    filtro_actual = ""
    while True:
        limpiar_pantalla()
        print("==============================================")
        print("  REGISTRO / EDICIÓN DE USUARIO (NUBE / LOCAL)")
        print("==============================================")
        
        usuarios_dict, online = obtener_usuarios_hibrido()
        lista_usuarios = list(usuarios_dict.items())

        print("Usuarios registrados en la base de datos:\n")
        hay_coincidencias = False
        for idx, (uid, nombre) in enumerate(lista_usuarios, 1):
            if not filtro_actual or filtro_actual.lower() in nombre.lower():
                print(f"  [{idx}] Usuario: {nombre:<25} | UID: {uid}")
                hay_coincidencias = True
                
        if not hay_coincidencias:
            print(f"  ⚠️ (No se encontraron usuarios que coincidan con '{filtro_actual}')")
            
        print("\n----------------------------------------------")
        if filtro_actual:
            print(f" 🔍 Filtro de búsqueda activo: '{filtro_actual}'")
        print(" 👉 Para REGISTRAR / EDITAR: Acerca la TARJETA AL LECTOR o escribe su NÚMERO (1, 2...).")
        print(" 👉 Para BUSCAR / FILTRAR: Escribe un nombre.")
        print(" 👉 Para SALIR / LIMPIAR: Escribe '0' o presiona ENTER.")
        print("----------------------------------------------")

        entrada = capturar_entrada_hibrida(ser, "\nEscribe para BUSCAR o N°/Tarjeta para REGISTRAR/EDITAR: ").strip()

        if entrada == '0' or not entrada:
            if filtro_actual:
                filtro_actual = ""
                continue
            else:
                break

        uid_seleccionado = None
        nombre_actual = ""
        es_edicion = False

        if entrada.startswith("NFC:"):
            uid_seleccionado = entrada.split(":")[1].upper()
            if uid_seleccionado in usuarios_dict:
                nombre_actual = usuarios_dict[uid_seleccionado]
                es_edicion = True

        elif entrada.isdigit():
            num_opc = int(entrada)
            if 1 <= num_opc <= len(lista_usuarios):
                uid_seleccionado, nombre_actual = lista_usuarios[num_opc - 1]
                es_edicion = True
            else:
                print(f"\n❌ El número '{num_opc}' no existe en la lista.")
                time.sleep(1.5)
                continue

        elif entrada.upper() in usuarios_dict:
            uid_seleccionado = entrada.upper()
            nombre_actual = usuarios_dict[uid_seleccionado]
            es_edicion = True

        else:
            filtro_actual = entrada
            continue

        print("\n----------------------------------------------")
        if es_edicion:
            print(f" ⚠️ [MODO EDICIÓN DETECTADO]")
            print(f" • Usuario actual registrado: '{nombre_actual}'")
            print(f" • Código UID NFC          : {uid_seleccionado}")
        else:
            print(f" ✨ [NUEVO REGISTRO DETECTADO]")
            print(f" • Código UID NFC detectado: {uid_seleccionado}")
        print("----------------------------------------------")

        nombre_nuevo = input(f"\n1. Ingresa el {'NUEVO ' if es_edicion else ''}nombre para el usuario: ").strip()

        if not nombre_nuevo:
            print("❌ Operación cancelada. El nombre no puede estar vacío.")
            time.sleep(1.5)
            continue

        confirmacion = input(f"\n¿Guardar cambios? (s/n) + ENTER: ").strip().lower()
        if confirmacion == 's':
            usuarios_dict[uid_seleccionado] = nombre_nuevo
            guardar_usuarios_locales(usuarios_dict)
            if online:
                try:
                    requests.put(f"{URL_BASE}/usuarios/{uid_seleccionado}.json", json=nombre_nuevo, timeout=3)
                except Exception:
                    pass
            hablar(f"Usuario {nombre_nuevo} {'actualizado' if es_edicion else 'registrado'} con éxito.")
            filtro_actual = ""
            time.sleep(1.5)
        else:
            print("\n❌ Operación cancelada.")
            time.sleep(1.2)

def eliminar_usuario_con_confirmacion(ser):
    filtro_actual = ""
    while True:
        limpiar_pantalla()
        print("==============================================")
        print("       ELIMINAR USUARIO (NUBE / LOCAL)        ")
        print("==============================================")
        
        usuarios_dict, online = obtener_usuarios_hibrido()
        if not usuarios_dict:
            print("(No hay usuarios registrados en el sistema)")
            input("\nPresiona ENTER para volver...")
            break

        lista_usuarios = list(usuarios_dict.items())

        print("Lista de usuarios registrados:\n")
        hay_coincidencias = False
        for idx, (uid, nombre) in enumerate(lista_usuarios, 1):
            if not filtro_actual or filtro_actual.lower() in nombre.lower():
                print(f"  [{idx}] Usuario: {nombre:<25} | UID: {uid}")
                hay_coincidencias = True
                
        if not hay_coincidencias:
            print(f"  ⚠️ (No se encontraron usuarios que coincidan con '{filtro_actual}')")
            
        print("\n----------------------------------------------")
        if filtro_actual:
            print(f" 🔍 Filtro de búsqueda activo: '{filtro_actual}'")
        print(" 👉 Para SELECCIONAR: Escribe el NÚMERO (1, 2...) o pasa la TARJETA.")
        print(" 👉 Para BUSCAR/FILTRAR: Escribe un nombre.")
        print(" 👉 Para SALIR / LIMPIAR: Escribe '0' o presiona ENTER.")
        print("----------------------------------------------")

        entrada = capturar_entrada_hibrida(ser, "\nEscribe para BUSCAR o N°/Tarjeta para BORRAR: ").strip()

        if entrada == '0' or not entrada:
            if filtro_actual:
                filtro_actual = ""
                continue
            else:
                break

        uid_a_eliminar = None
        nombre_user = ""

        if entrada.startswith("NFC:"):
            uid_escaneado = entrada.split(":")[1].upper()
            if uid_escaneado in usuarios_dict:
                uid_a_eliminar = uid_escaneado
                nombre_user = usuarios_dict[uid_escaneado]
            else:
                print(f"\n⚠️ La tarjeta escaneada [UID: {uid_escaneado}] NO ESTÁ REGISTRADA.")
                time.sleep(1.5)
                continue

        elif entrada.isdigit():
            num_opc = int(entrada)
            if 1 <= num_opc <= len(lista_usuarios):
                uid_a_eliminar, nombre_user = lista_usuarios[num_opc - 1]
            else:
                print(f"\n❌ El número '{num_opc}' no existe en la lista.")
                time.sleep(1.5)
                continue

        elif entrada.upper() in usuarios_dict:
            uid_a_eliminar = entrada.upper()
            nombre_user = usuarios_dict[uid_a_eliminar]

        else:
            filtro_actual = entrada
            continue

        print("\n----------------------------------------------")
        print(" 🔍 TARJETA SELECCIONADA PARA ELIMINAR:")
        print(f" • Nombre del Usuario : {nombre_user}")
        print(f" • Código UID NFC     : {uid_a_eliminar}")
        print("----------------------------------------------")

        confirmar = input(f"¿Confirmas la eliminación permanente de '{nombre_user}'? (s/n) + ENTER: ").strip().lower()
        if confirmar == 's':
            del usuarios_dict[uid_a_eliminar]
            guardar_usuarios_locales(usuarios_dict)
            foto = os.path.join(RUTA_BASE, "rostros_usuarios", f"{uid_a_eliminar}.jpg")
            if os.path.exists(foto):
                try:
                    os.remove(foto)
                except OSError:
                    pass
            if online:
                try:
                    requests.delete(f"{URL_BASE}/usuarios/{uid_a_eliminar}.json", timeout=3)
                except Exception:
                    pass
            hablar(f"Usuario {nombre_user} eliminado del sistema.")
            filtro_actual = ""
            time.sleep(1.5)
        else:
            print("\n❌ Operación cancelada. Permanece en el menú...")
            time.sleep(1.2)

def consultar_tarjeta_lector(ser):
    while True:
        limpiar_pantalla()
        print("==========================================================")
        print("    MODO CONSULTA CONTINUA DE TARJETAS (AUDITORÍA NFC)   ")
        print("==========================================================")
        print(" 📡 Acerca CUALQUIER tarjeta o llavero al lector en cualquier momento.")
        print(" 🛑 Para SALIR de este menú y volver, presiona ENTER sin escribir nada.")
        print("==========================================================\n")
        
        print("-> Esperando tarjeta... (Presiona ENTER para salir)")
        
        inicio = time.time()
        uid = None
        salida_manual = False

        try:
            ser.reset_input_buffer()
            while time.time() - inicio < 6:
                if msvcrt.kbhit():
                    tecla = msvcrt.getch()
                    if tecla in [b'\r', b'\n']:
                        salida_manual = True
                        break
                
                if ser.in_waiting > 0:
                    linea = ser.readline().decode('utf-8', errors='ignore').strip()
                    if linea.startswith("CARD:"):
                        uid = linea.split(":")[1].upper()
                        break
                time.sleep(0.05)
        except (serial.SerialException, serial.SerialTimeoutException, OSError):
            pass

        if salida_manual:
            break

        if not uid:
            continue

        usuarios_dict, online = obtener_usuarios_hibrido()
        modo_txt = "ONLINE ☁️" if online else "OFFLINE 📁"
        
        print("----------------------------------------------------------")
        print(f" 🔍 RESULTADO DE LA AUDITORÍA ({modo_txt}):")
        print(f" • Código UID NFC : {uid}")
        
        if uid in usuarios_dict:
            nombre = usuarios_dict[uid]
            print(f" • Estado         : 🔓 REGISTRADO Y AUTORIZADO")
            print(f" • Nombre Asignado: {nombre}")
            print("----------------------------------------------------------")
            hablar(f"Tarjeta registrada a nombre de {nombre}.")
        else:
            print(f" • Estado         : ❌ TARJETA DENEGADA / NO REGISTRADA")
            print(f" • Nombre Asignado: (Sin asignación)")
            print("----------------------------------------------------------")
            hablar("Tarjeta no registrada en el sistema.")
            
        print("\n--> Presiona ENTER para salir o espera a la siguiente lectura...")
        
        t_espera = time.time()
        while time.time() - t_espera < 3.5:
            if msvcrt.kbhit():
                tecla = msvcrt.getch()
                if tecla in [b'\r', b'\n']:
                    salida_manual = True
                    break
            time.sleep(0.05)
            
        if salida_manual:
            break

# =========================================================================
# MENÚS Y SUBMENÚS DE NAVEGACIÓN
# =========================================================================
def submenu_registro_tarjetas(ser):
    while True:
        limpiar_pantalla()
        print("==============================================")
        print("  [1] SUBMENÚ: REGISTRO Y GESTIÓN DE TARJETAS ")
        print("==============================================")
        print(" [1] Agregar / Editar usuario (Buscador por nombre + Selección N°/Lector)")
        print(" [2] Eliminar usuario (Buscador por nombre + Selección N°/Lector)")
        print(" [3] Consultar tarjeta en tiempo real (Lector continuo 📡)")
        print(" [0] Volver al Menú Principal")
        print("==============================================")
        
        opc = esperar_tecla_instantanea()
        if opc == '1':
            agregar_o_editar_usuario(ser)
        elif opc == '2':
            eliminar_usuario_con_confirmacion(ser)
        elif opc == '3':
            consultar_tarjeta_lector(ser)
        elif opc == '0':
            break
        time.sleep(0.05)

def submenu_historial_datos(ser):
    while True:
        limpiar_pantalla()
        print("==============================================")
        print("  [2] SUBMENÚ: HISTORIAL Y ANALÍTICA DE DATOS ")
        print("==============================================")
        print(" [1] Ver lista de usuarios autorizados (Nube / Local)")
        print(" [2] Ver historial de accesos (Nube / Local)")
        print(" [3] GENERAR REPORTE GRÁFICO (Analytics en cascada 📊)")
        print(" [0] Volver al Menú Principal")
        print("==============================================")
        
        opc = esperar_tecla_instantanea()
        if opc == '1':
            limpiar_pantalla()
            print("==================================================")
            print("         USUARIOS AUTORIZADOS REGISTRADOS         ")
            print("==================================================")
            usuarios_dict, online = obtener_usuarios_hibrido()
            modo_txt = "ONLINE ☁️" if online else "OFFLINE 📁"
            print(f" Modo actual de consulta: [{modo_txt}]\n")
            if not usuarios_dict:
                print("(Sin usuarios registrados)")
            else:
                for idx, (uid, nombre) in enumerate(usuarios_dict.items(), 1):
                    print(f"  [{idx}] {nombre:<20} | UID: {uid}")
            print("==================================================")
            input("\nPresiona ENTER para volver al submenú...")
        elif opc == '2':
            limpiar_pantalla()
            print("--- ÚLTIMOS ACCESOS REGISTRADOS (NUBE / LOCAL) ---")
            cargado_nube = False
            try:
                resp = requests.get(f"{URL_BASE}/logs.json?orderBy=\"$key\"&limitToLast=15", timeout=2.5)
                if resp.status_code == 200 and resp.json():
                    logs_nube = resp.json()
                    for key, val in logs_nube.items():
                        print(f"[{val.get('fecha_hora')}] {val.get('estado')} | {val.get('usuario')} ({val.get('uid')}) - {val.get('detalle')}")
                    cargado_nube = True
            except Exception:
                pass

            if not cargado_nube:
                print("⚠️ Conexión a nube no disponible. Leyendo respaldo CSV local:\n")
                if os.path.exists(ARCHIVO_CSV_LOGS):
                    try:
                        with open(ARCHIVO_CSV_LOGS, mode='r', encoding='utf-8') as f:
                            reader = list(csv.DictReader(f))
                            ultimos = reader[-15:]
                            for r in ultimos:
                                print(f"[{r.get('fecha_hora')}] {r.get('estado')} | {r.get('usuario')} ({r.get('uid')}) - {r.get('detalle')}")
                    except Exception as e:
                        print(f"Error al leer registros locales: {e}")
                else:
                    print("(Sin registros en local)")
                    
            input("\nPresiona ENTER para volver al submenú...")
        elif opc == '3':
            lanzar_script_analytics_cascada()
        elif opc == '0':
            break
        time.sleep(0.05)

def menu_administrador(ser):
    while True:
        limpiar_pantalla()
        print("==============================================")
        print("     PANEL PRINCIPAL DE ADMINISTRACIÓN        ")
        print("==============================================")
        print(" [1] REGISTRO Y GESTIÓN DE TARJETAS 🎴")
        print(" [2] HISTORIAL Y ANALÍTICA DE DATOS 📊")
        print(" [3] DEBUG Y PRUEBAS DE HARDWARE ⚡")
        print(" [0] Volver al modo de monitoreo activo")
        print("==============================================")
        
        opc = esperar_tecla_instantanea()
        if opc == '1':
            submenu_registro_tarjetas(ser)
        elif opc == '2':
            submenu_historial_datos(ser)
        elif opc == '3':
            submenu_debug(ser)
        elif opc == '0':
            break
        time.sleep(0.05)

# =========================================================================
# BUCLE PRINCIPAL CON CONEXIÓN AUTODETECTABLE Y RECONEXIÓN DINÁMICA
# =========================================================================
def main():
    global porcentaje_actual_carga, PUERTO_COM_ACTUAL
    porcentaje_actual_carga = 0
    
    # Lanza el servidor web automáticamente al arrancar
    iniciar_servidor_web_automatico()

    animar_carga_hasta(20, "Buscando placas Arduino en puertos COM disponibles...", pasitos=8, demora=0.01)
    animar_carga_hasta(45, "Precargando historial de accesos (Firebase / Local)...", pasitos=10, demora=0.01)
    precargar_estadisticas_iniciales()
    animar_carga_hasta(75, "Descargando base de usuarios autorizados...", pasitos=10, demora=0.01)
    _actualizar_cache_usuarios()
    threading.Thread(target=_refrescar_cache_usuarios, daemon=True).start()
    threading.Thread(target=_vaciar_cola_motor, daemon=True).start()
    animar_carga_hasta(90, "Inicializando motor de voz sintetizada...", pasitos=6, demora=0.01)
    animar_carga_hasta(100, "¡Sistema cargado con éxito!", pasitos=5, demora=0.01)
    time.sleep(0.3)

    while True:
        ser = None
        while ser is None:
            puerto_dinamico = autodetectar_puerto_arduino()
            if puerto_dinamico:
                try:
                    ser = serial.Serial(puerto_dinamico, BAUD_RATE, timeout=0.1)
                    time.sleep(2)
                    ser.reset_input_buffer()
                    PUERTO_COM_ACTUAL = puerto_dinamico
                    imprimir_encabezado_escucha()
                except (serial.SerialException, OSError):
                    ser = None
            
            if ser is None:
                limpiar_pantalla()
                print("=========================================================")
                print(" CONTROL ACCESO NFC (GOOGLE FIREBASE + RESPALDO LOCAL 📁)")
                print("=========================================================")
                print(" ⚠️ [ADVERTENCIA] No se detectó ninguna placa Arduino en los puertos COM.")
                print(" 🔄 Reintentando escaneo automáticamente en 2 segundos...")
                print("=========================================================")
                time.sleep(2)

        ultimo_dato = time.time()
        con_latido = False  # pasa a True cuando el Arduino envía LIVE
        while True:
            try:
                if con_latido and time.time() - ultimo_dato > 20:
                    raise serial.SerialException("el lector no envía señal hace más de 20 s")
                if ser.in_waiting > 0:
                    ultimo_dato = time.time()
                    try:
                        linea = ser.readline().decode('utf-8', errors='ignore').strip()
                    except Exception:
                        linea = ""
                    
                    if linea == "LIVE":
                        con_latido = True
                        ultimo_dato = time.time()
                    elif linea.startswith("CARD:"):
                        uid = linea.split(":")[1].upper()
                        guardar_ultima_tarjeta(uid)  # para que el panel pueda tomar la tarjeta escaneada
                        if es_lectura_repetida(uid):
                            continue  # la misma tarjeta sigue apoyada en el lector: no abrir dos veces
                        usuarios_dict, online = usuarios_en_cache(uid)
                        
                        if len(uid) < 8 or not all(c in '0123456789ABCDEF' for c in uid):
                            try:
                                ser.write(b"ERR\n")
                                ser.flush()
                            except Exception:
                                pass
                            fecha = registrar_evento_local_y_nube(uid, "Desconocido", "DENEGADO", "MALA_LECTURA")
                            imprimir_encabezado_escucha()
                            print(f"[{fecha}] ⚠️ DENEGADO -> Error de lectura | UID: {uid}\n")
                            en_segundo_plano(hablar, "Acceso denegado. Error de lectura de tarjeta. Pase la tarjeta de nuevo.")

                        elif uid in usuarios_dict:
                            nombre = usuarios_dict[uid]
                            try:
                                ser.write(b"OK\n")
                                ser.flush()
                            except Exception:
                                pass
                            fecha = registrar_evento_local_y_nube(uid, nombre, "PERMITIDO", "OK")
                            imprimir_encabezado_escucha()
                            print(f"[{fecha}] 🔓 AUTORIZADO -> Usuario: {nombre} | UID: {uid}")
                            en_segundo_plano(hablar, f"Acceso autorizado. Bienvenido {nombre}.")
                            en_segundo_plano(ejecutar_cuenta_regresiva_chapa, 3)  # en paralelo: no frena la lectura

                        else:
                            try:
                                ser.write(b"ERR\n")
                                ser.flush()
                            except Exception:
                                pass
                            fecha = registrar_evento_local_y_nube(uid, "Desconocido", "DENEGADO", "NO_REGISTRADO")
                            imprimir_encabezado_escucha()
                            print(f"[{fecha}] ❌ DENEGADO -> Tarjeta NO registrada | UID: {uid}\n")
                            en_segundo_plano(hablar, "Acceso denegado. Tarjeta no registrada en el sistema.")

                        try:
                            ser.reset_input_buffer()
                            ser.reset_output_buffer()
                        except Exception:
                            pass

                vigilar_servidor_web()
                procesar_cola_debug(ser)

                tecla = capturar_tecla_instantanea()
                if tecla == TECLA_MENU_OCULTO:
                    menu_administrador(ser)
                    ultimo_dato = time.time()
                    imprimir_encabezado_escucha()

                time.sleep(0.02)

            except Exception as e:
                print(f"\n⚠️ [ALERTA] Error en el puerto serie ({type(e).__name__}): {e}")
                try:
                    if ser:
                        ser.close()
                except Exception:
                    pass
                ser = None
                time.sleep(1)
                break

if __name__ == "__main__":
    main()