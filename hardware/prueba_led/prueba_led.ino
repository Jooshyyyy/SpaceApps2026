// Prueba del LED RGB: enciende un pin a la vez y muestra en la OLED cuál es.
#include <Wire.h>
#include <U8g2lib.h>
U8G2_SH1106_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);
const uint8_t pins[] = {14, 27, 26};
void setup() {
  Wire.begin(22, 21);
  oled.begin(); oled.setContrast(255);
  for (uint8_t p : pins) { pinMode(p, OUTPUT); digitalWrite(p, LOW); }
}
void loop() {
  for (uint8_t i = 0; i < 3; i++) {
    for (uint8_t p : pins) digitalWrite(p, LOW);
    digitalWrite(pins[i], HIGH);
    oled.clearBuffer();
    oled.setFont(u8g2_font_6x10_tr); oled.drawStr(8, 14, "PRUEBA LED RGB");
    oled.drawStr(8, 30, "Encendido:");
    char b[12]; snprintf(b, sizeof(b), "GPIO %d", pins[i]);
    oled.setFont(u8g2_font_helvB14_tr); oled.drawStr(8, 54, b);
    oled.sendBuffer();
    delay(3000);
  }
}
