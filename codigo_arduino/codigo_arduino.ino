#include <SPI.h>
#include <MFRC522.h>

#define SS_PIN 10
#define RST_PIN 9

#define PIN_SENAL_CHAPA 6       // Pin 6 manda la señal al conversor/chapa
#define PIN_LED_PLACA LED_BUILTIN  // Pin 13 (LED 'L' del Arduino)

MFRC522 rfid(SS_PIN, RST_PIN);

bool modoDebugActivo = false;
unsigned long ultimoTiempoDebug = 0;
bool estadoLedDebug = false;

void setup() {
  Serial.begin(9600);
  SPI.begin();
  rfid.PCD_Init();

  pinMode(PIN_SENAL_CHAPA, OUTPUT);
  pinMode(PIN_LED_PLACA, OUTPUT);

  digitalWrite(PIN_SENAL_CHAPA, LOW);
  digitalWrite(PIN_LED_PLACA, HIGH); // LED 'L' encendido fijo en reposo
}

void loop() {
  // 1. Parpadeo de 2s / 2s en Modo Debug
  if (modoDebugActivo) {
    if (millis() - ultimoTiempoDebug >= 2000) {
      ultimoTiempoDebug = millis();
      estadoLedDebug = !estadoLedDebug;
      digitalWrite(PIN_LED_PLACA, estadoLedDebug ? HIGH : LOW);
    }
  }

  // 2. Comandos Serial desde Python
  if (Serial.available() > 0) {
    String respuesta = Serial.readStringUntil('\n');
    respuesta.trim();

    // ACCESO AUTORIZADO (OK): PULSOS ANTI-TRABADO DURANTE 6 SEGUNDOS
    if (respuesta == "OK") {
      modoDebugActivo = false;
      digitalWrite(PIN_LED_PLACA, HIGH);

      // Ejecuta 6 ciclos de 1 segundo (Golpe fuerte + Micro-corte de liberación)
      for (int i = 0; i < 6; i++) {
        digitalWrite(PIN_SENAL_CHAPA, HIGH); // Da corriente de golpe
        delay(850);                          // Mantiene la fuerza 0.85 seg
        digitalWrite(PIN_SENAL_CHAPA, LOW);  // Corta un instante para liberar tensión
        delay(150);                          // 0.15 seg de corte
      }

      digitalWrite(PIN_SENAL_CHAPA, LOW);
      digitalWrite(PIN_LED_PLACA, HIGH);    // Mantiene LED fijo encendido
    } 
    // ACCESO DENEGADO (ERR)
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
    
    // COMANDOS DE DEBUG
    else if (respuesta == "DBG_START") {
      modoDebugActivo = true;
      ultimoTiempoDebug = millis();
      estadoLedDebug = true;
      digitalWrite(PIN_LED_PLACA, HIGH);
    }
    else if (respuesta == "DBG_CHAPA_ON") {
      modoDebugActivo = false;
      digitalWrite(PIN_SENAL_CHAPA, HIGH);
      digitalWrite(PIN_LED_PLACA, HIGH);
    }
    else if (respuesta == "DBG_CHAPA_OFF") {
      modoDebugActivo = false;
      digitalWrite(PIN_SENAL_CHAPA, LOW);
      digitalWrite(PIN_LED_PLACA, HIGH);
    }
    else if (respuesta == "DBG_RESET") {
      modoDebugActivo = false;
      digitalWrite(PIN_SENAL_CHAPA, LOW);
      digitalWrite(PIN_LED_PLACA, HIGH);
    }
  }

  // 3. Lectura RFID
  if (rfid.PICC_IsNewCardPresent() && rfid.PICC_ReadCardSerial()) {
    String uidCadena = "";
    for (byte i = 0; i < rfid.uid.size; i++) {
      if (rfid.uid.uidByte[i] < 0x10) uidCadena += "0";
      uidCadena += String(rfid.uid.uidByte[i], HEX);
    }
    uidCadena.toUpperCase();

    Serial.println("CARD:" + uidCadena);

    rfid.PICC_HaltA();
    rfid.PCD_StopCrypto1();

    if (!modoDebugActivo) {
      digitalWrite(PIN_LED_PLACA, HIGH);
    }
    
    delay(1000);
  }
}