#include <SPI.h>
#include <MFRC522.h>

#define SS_PIN 10
#define RST_PIN 9

#define PIN_SENAL_CHAPA 4        // Pin 4 para la chapa (Active Low)[cite: 1]
#define PIN_LED_PLACA LED_BUILTIN 

MFRC522 rfid(SS_PIN, RST_PIN);

bool modoDebugActivo = false;
unsigned long ultimoTiempoDebug = 0;
bool estadoLedDebug = false;

void setup() {
  // Blindaje Active Low al encender
  digitalWrite(PIN_SENAL_CHAPA, HIGH); 
  pinMode(PIN_SENAL_CHAPA, OUTPUT);
  
  pinMode(PIN_LED_PLACA, OUTPUT);
  digitalWrite(PIN_LED_PLACA, HIGH);

  Serial.begin(9600);
  SPI.begin();
  rfid.PCD_Init();
}

void loop() {
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
      modoDebugActivo = false;
      digitalWrite(PIN_LED_PLACA, HIGH);

      // Temporizador de apertura (Pulsos anti-trabado)
      for (int i = 0; i < 6; i++) {
        digitalWrite(PIN_SENAL_CHAPA, LOW);   
        delay(850);                           
        digitalWrite(PIN_SENAL_CHAPA, HIGH);  
        delay(150);                           
      }

      digitalWrite(PIN_SENAL_CHAPA, HIGH);    
      digitalWrite(PIN_LED_PLACA, HIGH);    
      
      // AVISO A PYTHON DE QUE TERMINÓ LA ACCIÓN PARA REINICIAR CONEXIÓN
      Serial.println("RESET_CONEXION");
      Serial.flush(); // Limpieza de búfer serial para evitar bloqueos
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

  if (rfid.PICC_IsNewCardPresent() && rfid.PICC_ReadCardSerial()) {
    String uidCadena = "";
    for (byte i = 0; i < rfid.uid.size; i++) {
      if (rfid.uid.uidByte[i] < 0x10) uidCadena += "0";
      uidCadena += String(rfid.uid.uidByte[i], HEX);
    }
    uidCadena.toUpperCase();

    Serial.println("CARD:" + uidCadena);
    Serial.flush(); // Asegura el envío limpio del UID para que no se pegue el puerto

    rfid.PICC_HaltA();
    rfid.PCD_StopCrypto1();

    if (!modoDebugActivo) {
      digitalWrite(PIN_LED_PLACA, HIGH);
    }
    
    delay(1000);
  }
}