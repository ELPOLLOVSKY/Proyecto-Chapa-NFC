#include <SPI.h>
#include <MFRC522.h>
#include <avr/wdt.h>

#define SS_PIN 10
#define RST_PIN 9

#define PIN_SENAL_CHAPA 4        // Pin 4 para la chapa (Active Low)
#define PIN_LED_PLACA LED_BUILTIN

// Apertura de la chapa (pulsos anti-trabado)
#define PULSOS_CHAPA 6
#define MS_PULSO_ON 850
#define MS_PULSO_OFF 150
#define MS_ENTRE_APERTURAS 3000  // una orden OK repetida dentro de este tiempo se ignora

#define MS_LATIDO 5000           // "LIVE" cada 5 s: el PC sabe que el lector sigue vivo

MFRC522 rfid(SS_PIN, RST_PIN);

bool modoDebugActivo = false;
unsigned long ultimoTiempoDebug = 0;
bool estadoLedDebug = false;
unsigned long ultimoChequeoLector = 0;
unsigned long ultimoLatido = 0;
unsigned long ultimaApertura = 0;
bool hayApertura = false;

// Nunca espera al PC: si el buffer de salida está lleno, el mensaje se descarta
void enviarLinea(const String &s) {
  if (Serial.availableForWrite() >= (int)s.length() + 2) {
    Serial.println(s);
  }
}

void abrirChapa() {
  modoDebugActivo = false;
  digitalWrite(PIN_LED_PLACA, HIGH);

  for (int i = 0; i < PULSOS_CHAPA; i++) {
    wdt_reset();
    digitalWrite(PIN_SENAL_CHAPA, LOW);
    delay(MS_PULSO_ON);
    digitalWrite(PIN_SENAL_CHAPA, HIGH);
    delay(MS_PULSO_OFF);
  }

  digitalWrite(PIN_SENAL_CHAPA, HIGH);
  digitalWrite(PIN_LED_PLACA, HIGH);

  // Descarta las órdenes OK que llegaron durante la apertura:
  // si no, la chapa repetiría la secuencia una y otra vez
  while (Serial.available() > 0) {
    Serial.read();
  }

  // El pulso de la chapa puede dejar trabado al lector RFID: se reinicia
  rfid.PCD_Init();

  hayApertura = true;
  ultimaApertura = millis();
  enviarLinea("RESET_CONEXION");
}

void setup() {
  // Blindaje Active Low al encender
  digitalWrite(PIN_SENAL_CHAPA, HIGH);
  pinMode(PIN_SENAL_CHAPA, OUTPUT);

  pinMode(PIN_LED_PLACA, OUTPUT);
  digitalWrite(PIN_LED_PLACA, HIGH);

  Serial.begin(9600);
  SPI.begin();
  rfid.PCD_Init();

  // Si el microcontrolador se cuelga, se reinicia solo en 4 s
  wdt_enable(WDTO_4S);
}

void loop() {
  wdt_reset();

  if (millis() - ultimoLatido >= MS_LATIDO) {
    ultimoLatido = millis();
    enviarLinea("LIVE");
  }

  if (modoDebugActivo) {
    if (millis() - ultimoTiempoDebug >= 2000) {
      ultimoTiempoDebug = millis();
      estadoLedDebug = !estadoLedDebug;
      digitalWrite(PIN_LED_PLACA, estadoLedDebug ? HIGH : LOW);
    }
  }

  if (Serial.available() > 0) {
    String respuesta = Serial.readStringUntil('\n');
    respuesta.trim();

    if (respuesta == "OK") {
      // Ignora órdenes repetidas muy seguidas (tarjeta y botón web al mismo tiempo)
      if (!hayApertura || millis() - ultimaApertura >= MS_ENTRE_APERTURAS) {
        abrirChapa();
      }
    }
    else if (respuesta == "ERR") {
      modoDebugActivo = false;
      for (int i = 0; i < 6; i++) {
        digitalWrite(PIN_LED_PLACA, LOW);
        delay(80);
        digitalWrite(PIN_LED_PLACA, HIGH);
        delay(80);
      }
      digitalWrite(PIN_LED_PLACA, HIGH);
    }
    else if (respuesta == "DBG_START") {
      modoDebugActivo = true;
      ultimoTiempoDebug = millis();
      estadoLedDebug = true;
      digitalWrite(PIN_LED_PLACA, HIGH);
    }
    else if (respuesta == "DBG_CHAPA_ON") {
      modoDebugActivo = false;
      digitalWrite(PIN_SENAL_CHAPA, LOW);
      digitalWrite(PIN_LED_PLACA, HIGH);
    }
    else if (respuesta == "DBG_CHAPA_OFF" || respuesta == "DBG_RESET") {
      modoDebugActivo = false;
      digitalWrite(PIN_SENAL_CHAPA, HIGH);
      digitalWrite(PIN_LED_PLACA, HIGH);
    }
  }

  // Cada 5 s se revisa que el MFRC522 responda; si se cuelga, se reinicia solo
  if (millis() - ultimoChequeoLector >= 5000) {
    ultimoChequeoLector = millis();
    byte version = rfid.PCD_ReadRegister(MFRC522::VersionReg);
    if (version == 0x00 || version == 0xFF) {
      rfid.PCD_Init();
    }
  }

  if (rfid.PICC_IsNewCardPresent() && rfid.PICC_ReadCardSerial()) {
    String uidCadena = "";
    for (byte i = 0; i < rfid.uid.size; i++) {
      if (rfid.uid.uidByte[i] < 0x10) uidCadena += "0";
      uidCadena += String(rfid.uid.uidByte[i], HEX);
    }
    uidCadena.toUpperCase();

    enviarLinea("CARD:" + uidCadena);

    rfid.PICC_HaltA();
    rfid.PCD_StopCrypto1();

    if (!modoDebugActivo) {
      digitalWrite(PIN_LED_PLACA, HIGH);
    }

    delay(1000);
  }
}
