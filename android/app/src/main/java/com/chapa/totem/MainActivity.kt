package com.chapa.totem

import android.app.Activity
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.Gravity
import android.view.WindowManager
import android.widget.TextView
import com.hoho.android.usbserial.driver.UsbSerialPort
import com.hoho.android.usbserial.driver.UsbSerialProber
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.net.Inet4Address
import java.net.NetworkInterface
import java.util.Collections
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

private const val TAG = "TotemChapa"
// Misma base que usa el PC (app.py y sincronizacion_firebase.py)
private const val FIREBASE = "https://registros-acceso-nfc-default-rtdb.firebaseio.com"
private const val ACCION_PERMISO = "com.chapa.totem.USB_PERMISO"
private const val BAUDIOS = 9600
// Una orden de apertura más vieja que esto ya no se ejecuta
private const val VENTANA_COMANDO_MS = 8000L
private val TIPO_JSON = "application/json; charset=utf-8".toMediaType()

class MainActivity : Activity() {

    private val http = OkHttpClient.Builder()
        .connectTimeout(3, TimeUnit.SECONDS)
        .readTimeout(4, TimeUnit.SECONDS)
        .writeTimeout(4, TimeUnit.SECONDS)
        .build()

    private val hilos = Executors.newScheduledThreadPool(4)
    private val activo = AtomicBoolean(true)
    private lateinit var usb: UsbManager
    private lateinit var pantalla: TextView
    private lateinit var idEquipo: String

    @Volatile private var puerto: UsbSerialPort? = null
    @Volatile private var latenciaMs: Long? = null
    private val bloqueoEscritura = Any()
    private var ultimoUid = ""
    private var ultimoUidMs = 0L

    private val receptor = object : BroadcastReceiver() {
        @Suppress("DEPRECATION")
        override fun onReceive(ctx: Context, intent: Intent) {
            when (intent.action) {
                ACCION_PERMISO -> {
                    val dispositivo: UsbDevice? = intent.getParcelableExtra(UsbManager.EXTRA_DEVICE)
                    val concedido = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)
                    if (concedido && dispositivo != null) abrirPuerto(dispositivo)
                    else mostrarEstado("Permiso USB denegado. Desconecta y vuelve a conectar el Arduino.")
                }
                UsbManager.ACTION_USB_DEVICE_ATTACHED -> buscarArduino()
                UsbManager.ACTION_USB_DEVICE_DETACHED -> {
                    cerrarPuerto()
                    mostrarEstado("Arduino desconectado")
                }
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Es un tótem: la pantalla no debe apagarse
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        usb = getSystemService(Context.USB_SERVICE) as UsbManager
        idEquipo = idDelEquipo()

        pantalla = TextView(this).apply {
            textSize = 20f
            gravity = Gravity.CENTER
            setPadding(40, 40, 40, 40)
        }
        setContentView(pantalla)

        val filtro = IntentFilter().apply {
            addAction(ACCION_PERMISO)
            addAction(UsbManager.ACTION_USB_DEVICE_ATTACHED)
            addAction(UsbManager.ACTION_USB_DEVICE_DETACHED)
        }
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(receptor, filtro, Context.RECEIVER_NOT_EXPORTED)
        else registerReceiver(receptor, filtro)

        hilos.scheduleWithFixedDelay({ latir() }, 0, 5, TimeUnit.SECONDS)
        hilos.scheduleWithFixedDelay({ revisarComandos() }, 500, 1000, TimeUnit.MILLISECONDS)
        hilos.execute { buscarArduino() }
    }

    override fun onDestroy() {
        activo.set(false)
        try { unregisterReceiver(receptor) } catch (_: Exception) {}
        cerrarPuerto()
        hilos.shutdownNow()
        super.onDestroy()
    }

    // ------------------------------------------------------------------
    // USB: encontrar, pedir permiso, abrir y leer el Arduino
    // ------------------------------------------------------------------
    private fun buscarArduino() {
        val driver = UsbSerialProber.getDefaultProber().findAllDrivers(usb).firstOrNull()
        if (driver == null) {
            mostrarEstado("Conecta el Arduino con el cable OTG")
            return
        }
        val dispositivo = driver.device
        if (usb.hasPermission(dispositivo)) {
            abrirPuerto(dispositivo)
        } else {
            // FLAG_MUTABLE: Android necesita completar el intent con el resultado del permiso
            val pi = PendingIntent.getBroadcast(
                this, 0, Intent(ACCION_PERMISO).setPackage(packageName),
                PendingIntent.FLAG_MUTABLE
            )
            usb.requestPermission(dispositivo, pi)
            mostrarEstado("Acepta el permiso USB para conectar el Arduino")
        }
    }

    private fun abrirPuerto(dispositivo: UsbDevice) {
        val driver = UsbSerialProber.getDefaultProber().findAllDrivers(usb)
            .firstOrNull { it.device.deviceId == dispositivo.deviceId }
        if (driver == null) {
            mostrarEstado("Dispositivo USB no compatible")
            return
        }
        val conexion = usb.openDevice(dispositivo)
        if (conexion == null) {
            mostrarEstado("No se pudo abrir el USB")
            return
        }
        val p = driver.ports[0]
        try {
            p.open(conexion)
            p.setParameters(BAUDIOS, 8, UsbSerialPort.STOPBITS_1, UsbSerialPort.PARITY_NONE)
        } catch (e: Exception) {
            mostrarEstado("Error abriendo el Arduino: ${e.message}")
            return
        }
        puerto = p
        mostrarEstado("Arduino conectado. Pasa una tarjeta.")
        hilos.execute { leerArduino(p) }
    }

    private fun leerArduino(p: UsbSerialPort) {
        val buffer = ByteArray(256)
        val linea = StringBuilder()
        while (activo.get() && puerto === p) {
            val n = try {
                p.read(buffer, 200)
            } catch (e: Exception) {
                cerrarPuerto()
                mostrarEstado("Arduino desconectado (${e.message})")
                return
            }
            for (i in 0 until n) {
                val c = buffer[i].toInt().toChar()
                when (c) {
                    '\n' -> { procesarLinea(linea.toString().trim()); linea.clear() }
                    '\r' -> {}
                    else -> linea.append(c)
                }
            }
        }
    }

    private fun cerrarPuerto() {
        val p = puerto
        puerto = null
        try { p?.close() } catch (_: Exception) {}
    }

    // ------------------------------------------------------------------
    // Tarjetas: el Arduino envía "CARD:<UID>"
    // ------------------------------------------------------------------
    private fun procesarLinea(l: String) {
        if (!l.startsWith("CARD:")) return
        val uid = l.removePrefix("CARD:").trim().uppercase()
        if (uid.isEmpty()) return
        val ahora = System.currentTimeMillis()
        // Mismo UID en menos de 2 s es la tarjeta todavía apoyada: no se vuelve a enviar
        if (uid == ultimoUid && ahora - ultimoUidMs < 2000) return
        ultimoUid = uid
        ultimoUidMs = ahora

        val evento = JSONObject()
            .put("uid", uid)
            .put("dispositivo", idEquipo)
            .put("ts", JSONObject().put(".sv", "timestamp"))   // hora del servidor de Firebase
        peticion("POST", "/eventos.json", evento.toString())
        mostrarEstado("Tarjeta $uid: verificando...")
    }

    // ------------------------------------------------------------------
    // Órdenes del PC (abrir la chapa)
    // ------------------------------------------------------------------
    private fun revisarComandos() {
        val r = peticion("GET", "/comandos/$idEquipo.json") ?: return
        if (r == "null" || r.isBlank()) return
        val comandos = JSONObject(r)
        val ahora = System.currentTimeMillis()
        val claves = comandos.keys()
        while (claves.hasNext()) {
            val clave = claves.next()
            val c = comandos.optJSONObject(clave) ?: continue
            // Se borra antes de ejecutar: así no se repite si la red falla a mitad
            peticion("DELETE", "/comandos/$idEquipo/$clave.json")
            if (ahora - c.optLong("ts", 0L) > VENTANA_COMANDO_MS) {
                Log.i(TAG, "Orden vieja descartada")
                continue
            }
            if (c.optString("cmd") == "OK") abrirChapa(c.optString("nombre", ""))
        }
    }

    private fun abrirChapa(nombre: String) {
        val p = puerto
        if (p == null) {
            mostrarEstado("Orden recibida, pero el Arduino no está conectado")
            return
        }
        try {
            synchronized(bloqueoEscritura) { p.write("OK\n".toByteArray(), 1000) }
            mostrarEstado("🔓 Acceso autorizado: $nombre")
        } catch (e: Exception) {
            mostrarEstado("Error enviando al Arduino: ${e.message}")
        }
    }

    // ------------------------------------------------------------------
    // Latido: el PC ve este equipo en línea, su IP y su latencia
    // ------------------------------------------------------------------
    private fun latir() {
        val cuerpo = JSONObject()
            .put("nombre", "Totem ${android.os.Build.MODEL}")
            .put("tipo", "android")
            .put("ip", ipLocal())
            .put("latencia_ms", latenciaMs ?: JSONObject.NULL)
            .put("usb_conectado", puerto != null)
            .put("ts", System.currentTimeMillis() / 1000.0)
        peticion("PUT", "/dispositivos/$idEquipo.json", cuerpo.toString())
    }

    // ------------------------------------------------------------------
    // Utilidades
    // ------------------------------------------------------------------
    /** Devuelve el cuerpo de la respuesta, o null si falló la petición. Mide la latencia. */
    private fun peticion(metodo: String, ruta: String, cuerpo: String? = null): String? {
        val builder = Request.Builder().url(FIREBASE + ruta)
        val body = cuerpo?.toRequestBody(TIPO_JSON)
        when (metodo) {
            "GET" -> builder.get()
            "PUT" -> builder.put(body ?: return null)
            "POST" -> builder.post(body ?: return null)
            "DELETE" -> builder.delete()
        }
        val inicio = System.currentTimeMillis()
        return try {
            http.newCall(builder.build()).execute().use { r ->
                if (!r.isSuccessful) return null
                latenciaMs = System.currentTimeMillis() - inicio
                r.body?.string()
            }
        } catch (e: Exception) {
            Log.w(TAG, "Sin conexión con Firebase: ${e.message}")
            null
        }
    }

    private fun mostrarEstado(msg: String) {
        Log.i(TAG, msg)
        runOnUiThread {
            pantalla.text = "TOTEM CHAPA\n\n$msg\n\nEquipo: $idEquipo\nIP: ${ipLocal()}"
        }
    }

    private fun idDelEquipo(): String {
        val prefs = getSharedPreferences("totem", MODE_PRIVATE)
        return prefs.getString("id", null) ?: ("totem_" + UUID.randomUUID().toString().take(8)).also {
            prefs.edit().putString("id", it).apply()
        }
    }

    private fun ipLocal(): String = try {
        Collections.list(NetworkInterface.getNetworkInterfaces())
            .flatMap { Collections.list(it.inetAddresses) }
            .firstOrNull { it is Inet4Address && !it.isLoopbackAddress }
            ?.hostAddress ?: "sin red"
    } catch (e: Exception) {
        "sin red"
    }
}
