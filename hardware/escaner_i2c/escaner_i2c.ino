// Escáner I2C: prueba SDA=21/SCL=22 y también la combinación invertida.
#include <Arduino.h>
#include <Wire.h>

void scan(int sda, int scl) {
  Wire.end();
  Wire.begin(sda, scl);
  Wire.setClock(100000);
  Serial.printf("SDA=%d SCL=%d:", sda, scl);
  int found = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { Serial.printf(" 0x%02X", a); found++; }
  }
  Serial.println(found ? "" : " NADA");
}

void setup() { Serial.begin(115200); delay(500); }

void loop() {
  scan(21, 22);
  scan(22, 21);
  Serial.println("---");
  delay(1500);
}
