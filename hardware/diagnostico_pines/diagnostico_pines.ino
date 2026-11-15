// Diagnóstico: prueba todos los pares SDA/SCL buscando la OLED (0x3C/0x3D) y lista pines con conexión externa.
#include <Wire.h>
const int io[] = {0,2,12,4,5,13,14,15,16,17,18,19,21,22,23,25,26,27,32,33};  // pines que pueden ser salida
const int N = sizeof(io)/sizeof(io[0]);
void setup() {
  Serial.begin(115200); delay(400);
  Serial.println("== busqueda OLED ==");
  int found = 0;
  for (int i = 0; i < N; i++) for (int j = 0; j < N; j++) {
    if (i == j) continue;
    Wire.end(); if (!Wire.begin(io[i], io[j], 100000)) continue; delay(2);
    for (uint8_t a = 0x3C; a <= 0x3D; a++) {
      Wire.beginTransmission(a);
      if (Wire.endTransmission() == 0) { Serial.printf("OLED SDA=%d SCL=%d addr=0x%02X\n", io[i], io[j], a); found++; }
    }
  }
  Wire.end();
  Serial.printf("pares encontrados: %d\n", found);
  String ext = "EXT:";
  for (int i = 0; i < N; i++) { int p = io[i];
    pinMode(p, INPUT_PULLUP); delayMicroseconds(300); int up = digitalRead(p);
    pinMode(p, INPUT_PULLDOWN); delayMicroseconds(300); int dn = digitalRead(p);
    if (up == dn) ext += " " + String(p) + "=" + up; }
  Serial.println(ext);
  Serial.println("== fin ==");
}
void loop() {}
