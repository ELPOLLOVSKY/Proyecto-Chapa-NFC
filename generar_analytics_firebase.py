import os
import sys
import requests
import pandas as pd
import matplotlib.pyplot as plt

URL_BASE = 'https://registros-acceso-nfc-default-rtdb.firebaseio.com'

def obtener_ruta_base():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))

RUTA_BASE = obtener_ruta_base()
ARCHIVO_CSV_LOGS = os.path.join(RUTA_BASE, "registros_locales.csv")

def generar_reporte_analitico():
    print("\n☁️ Intentando descargar registros de Firebase vía REST...")
    df = None

    try:
        response = requests.get(f"{URL_BASE}/logs.json", timeout=4)
        if response.status_code == 200 and response.json():
            datos = response.json()
            df = pd.DataFrame(datos.values())
            print("✅ Datos cargados correctamente desde la Nube (Firebase).")
    except Exception as e:
        print(f"⚠️ Error al conectar con Firebase: {e}")

    if df is None or df.empty:
        if os.path.exists(ARCHIVO_CSV_LOGS):
            print(f"📁 Cargando registros desde respaldo local ({ARCHIVO_CSV_LOGS})...")
            try:
                df = pd.read_csv(ARCHIVO_CSV_LOGS)
            except Exception as e:
                print(f"❌ Error al leer archivo CSV local: {e}")
                input("\nPresiona ENTER para salir...")
                return
        else:
            print("❌ No hay registros en la nube ni en el archivo local.")
            input("\nPresiona ENTER para salir...")
            return

    print("==================================================")
    print("      PANEL DE ANALÍTICA DE ACCESOS (FIREBASE)    ")
    print("==================================================")
    print(f"📊 Total de eventos registrados: {len(df)}")
    
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))

    df['estado'].value_counts().plot(
        kind='pie', 
        ax=axes[0], 
        autopct='%1.1f%%', 
        colors=['#4CAF50', '#F44336'],
        startangle=90
    )
    axes[0].set_title('Porcentaje de Accesos (Permitidos vs Denegados)')
    axes[0].set_ylabel('')

    if 'usuario' in df.columns:
        df['usuario'].value_counts().head(5).plot(
            kind='bar', 
            ax=axes[1], 
            color='#2196F3'
        )
        axes[1].set_title('Top 5 Usuarios con Mayor Actividad')
        axes[1].set_xlabel('Usuario')
        axes[1].set_ylabel('Cantidad de Intentos')
        axes[1].tick_params(axis='x', rotation=30)

    plt.tight_layout()
    print("\n📊 Abriendo ventana de gráficos... Cierra la ventana del gráfico para regresar.")
    plt.show()

if __name__ == "__main__":
    generar_reporte_analitico()