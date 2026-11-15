// Mars EVA Nav — reloj de muñeca (simulación)
// ESP32 (NodeMCU ESP-32S) + OLED 1.3" I2C 128x64.
// Recibe por USB-serie (115200) el estado que calcula el prototipo web y lo dibuja.
//
// Conexión OLED -> ESP32:  GND->GND  VCC->3V3  SDA->P22  SCL->P21
// Botón de emergencia (SOS): pulsador entre GPIO25 y GND, resistencia de pull-up a 3V3.
//   Mantener 1,5 s -> envía "$B,SOS\n" por USB a la base de control.
//   Con el SOS activo, mantener otra vez 1,5 s -> "$B,CANCEL\n" (falsa alarma).
// LED RGB de estado: R->GPIO27  G->GPIO26  B->GPIO14, cada uno con resistencia de 220-330 ohm.
//   Pata común (la más larga): a GND si es cátodo común, a 3V3 si es ánodo común (ver LED_ANODO_COMUN).
//   Verde fijo = todo bien · amarillo = aviso (sin enlace) · rojo parpadeando = peligro
//   rojo/azul alternando = SOS · magenta = la base confirmó el SOS · azul respirando = esperando datos
//   Colores calibrados con cámara: el verde de este LED brilla mucho más que el rojo y el azul.
//
// Trama (una línea ASCII por actualización, ~10 Hz):
//   $W,<rumbo|-1>,<az>,<dist_m>,<o2%>,<bat%>,<ttr_s>,<dosis_uSv_x10>,<flags>,<texto>\n
//   rumbo: grados hacia el siguiente punto de la ruta (-1 = sin rumbo)
//   az:    rotación de la brújula de la tablet (0 = norte arriba)
//   flags: bit0 TORMENTA, bit1 RADIACION, bit2 O2 BAJO, bit3 BAT BAJA,
//          bit4 SIN ENLACE, bit5 EVACUACION, bit6 SOS ACTIVO, bit7 BASE CONFIRMÓ
//
// Librería necesaria: U8g2 (olikraus).

#include <Arduino.h>
#include <Wire.h>
#include <U8g2lib.h>

// Las pantallas de 1.3" suelen usar SH1106. Si la imagen sale corrida 2 píxeles
// o con una franja de basura a la derecha, comenta esta línea y usa la de SSD1306.
U8G2_SH1106_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);
// U8G2_SSD1306_128X64_NONAME_F_HW_I2C oled(U8G2_R0, U8X8_PIN_NONE);

constexpr uint8_t F_STORM = 1, F_RAD = 2, F_O2 = 4, F_BAT = 8, F_COMMS = 16, F_EVAC = 32, F_SOS = 64, F_BASEOK = 128;
constexpr uint8_t BTN_SOS = 25;
constexpr uint8_t PIN_SDA = 22, PIN_SCL = 21;
constexpr uint32_t SOS_HOLD_MS = 1500;  // evita activaciones accidentales
constexpr uint32_t DATA_TIMEOUT_MS = 2000;
constexpr uint8_t LED_R = 27, LED_G = 26, LED_B = 14;  // calibrado con la cámara
constexpr bool LED_ANODO_COMUN = false;  // tu LED es de cátodo común (pata común a GND)
// Ganancia por color (0-255) medida con la cámara: iguala el brillo de los tres canales.
constexpr uint8_t GAIN_R = 255, GAIN_G = 100, GAIN_B = 255;

struct State {
  float brg = -1, az = 0, dist = 0;
  int o2 = 100, bat = 100, ttr = 0, dose10 = 0;
  uint8_t flags = 0;
  char text[20] = "ESPERANDO";
  uint32_t lastRx = 0;
  bool everRx = false;
} st;

char line[96];
size_t lineLen = 0;

void parseLine(char* s) {
  if (strncmp(s, "$W,", 3) != 0) return;
  char* fields[10];
  int n = 0;
  for (char* tok = strtok(s + 3, ","); tok && n < 10; tok = strtok(nullptr, ",")) fields[n++] = tok;
  if (n < 9) return;  // trama incompleta: se descarta
  st.brg = atof(fields[0]);
  st.az = atof(fields[1]);
  st.dist = atof(fields[2]);
  st.o2 = constrain(atoi(fields[3]), 0, 100);
  st.bat = constrain(atoi(fields[4]), 0, 100);
  st.ttr = max(0, atoi(fields[5]));
  st.dose10 = max(0, atoi(fields[6]));
  st.flags = (uint8_t)atoi(fields[7]);
  strlcpy(st.text, n >= 9 ? fields[8] : "", sizeof(st.text));
  st.lastRx = millis();
  st.everRx = true;
}

void readSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineLen) { line[lineLen] = 0; parseLine(line); lineLen = 0; }
    } else if (lineLen < sizeof(line) - 1) {
      line[lineLen++] = c;
    } else {
      lineLen = 0;  // línea demasiado larga: se descarta
    }
  }
}

void drawCompass(int cx, int cy, int r) {
  oled.drawCircle(cx, cy, r);
  for (int a = 0; a < 360; a += 30) {
    const float t = (a - st.az) * DEG_TO_RAD;
    const int r1 = (a % 90 == 0) ? r - 5 : r - 3;
    oled.drawLine(cx + r1 * sinf(t), cy - r1 * cosf(t), cx + r * sinf(t), cy - r * cosf(t));
  }
  const float tn = -st.az * DEG_TO_RAD;
  oled.setFont(u8g2_font_5x7_tr);
  oled.drawStr(cx + (r - 11) * sinf(tn) - 2, cy - (r - 11) * cosf(tn) + 4, "N");
  const int rr = r - 6;  // radio útil del radar dentro de las marcas

  // Escala automática: el objetivo siempre queda dentro del radar.
  static const int ranges[] = {25, 50, 100, 200, 500, 1000};
  int range = ranges[5];
  for (int R : ranges) if (st.dist <= R) { range = R; break; }
  char lbl[8];
  if (range >= 1000) strlcpy(lbl, "1km", sizeof(lbl)); else snprintf(lbl, sizeof(lbl), "%dm", range);
  oled.setFont(u8g2_font_4x6_tr);
  oled.drawStr(0, 63, lbl);

  // Objetivo: punto que parpadea más rápido cuanto más cerca está.
  if (st.brg >= 0) {
    const float t = (st.brg - st.az) * DEG_TO_RAD;
    const float k = min(st.dist / range, 1.0f);
    const int tx = cx + rr * k * sinf(t), ty = cy - rr * k * cosf(t);
    const uint32_t period = 150 + (uint32_t)(650 * min(st.dist, 200.0f) / 200.0f);
    oled.drawCircle(tx, ty, 3);
    if ((millis() % period) < period * 6 / 10) oled.drawDisc(tx, ty, 2);
  }
  // Tú: triángulo en el centro apuntando hacia donde miras.
  oled.drawTriangle(cx, cy - 3, cx - 2, cy + 2, cx + 2, cy + 2);
}

// Barra horizontal con etiqueta a la izquierda y el % dentro (texto en XOR).
void drawBar(int y, const char* label, int pct) {
  constexpr int X = 79, W = 48, H = 6;  // marco x79..126
  oled.setFont(u8g2_font_4x6_tr);
  oled.drawStr(66, y + H - 1, label);
  oled.drawFrame(X, y, W, H);
  oled.drawBox(X + 1, y + 1, (W - 2) * pct / 100, H - 2);
  char buf[6];
  snprintf(buf, sizeof(buf), "%d%%", pct);
  oled.setFontMode(1);
  oled.setDrawColor(2);
  oled.drawStr(X + (W - oled.getStrWidth(buf)) / 2, y + H - 1, buf);
  oled.setDrawColor(1);
  oled.setFontMode(0);
}

void drawInfo() {
  char buf[24];
  oled.setFont(u8g2_font_6x10_tr);
  oled.drawStr(66, 8, st.text);

  oled.setFont(u8g2_font_helvB14_tr);
  snprintf(buf, sizeof(buf), "%d", (int)roundf(st.dist));
  oled.drawStr(66, 25, buf);
  const int numW = oled.getStrWidth(buf);
  oled.setFont(u8g2_font_6x10_tr);
  oled.drawStr(66 + numW + 2, 25, "m");

  oled.setFont(u8g2_font_5x8_tf);
  if (st.brg >= 0) snprintf(buf, sizeof(buf), "RUMBO %d\xb0", (int)roundf(st.brg));
  else strlcpy(buf, "RUMBO --", sizeof(buf));
  oled.drawStr(66, 33, buf);
  snprintf(buf, sizeof(buf), "TTR %d:%02d", st.ttr / 60, st.ttr % 60);
  oled.drawStr(66, 41, buf);
  snprintf(buf, sizeof(buf), "%d.%d uSv%s", st.dose10 / 10, st.dose10 % 10, (st.flags & F_COMMS) ? " SE" : "");
  oled.drawStr(66, 49, buf);

  drawBar(51, "O2", st.o2);
  drawBar(58, "BAT", st.bat);
}

// Botón SOS: devuelve el avance 0..1 mientras se mantiene apretado.
float sosButton(uint32_t now) {
  static uint32_t downAt = 0;
  static bool fired = false;
  if (digitalRead(BTN_SOS) == HIGH) {  // suelto
    downAt = 0;
    return 0;
  }
  if (downAt == 0) { downAt = now; fired = false; }
  if (fired) return 0;
  if (now - downAt >= SOS_HOLD_MS) {
    // Con un SOS activo, mantener de nuevo lo cancela (falsa alarma).
    Serial.println((st.flags & F_SOS) ? "$B,CANCEL" : "$B,SOS");  // a la base (servidor.py)
    fired = true;
    return 0;
  }
  return (float)(now - downAt) / SOS_HOLD_MS;
}

uint8_t ledNow[3];  // último color pedido (para el diagnóstico)
void ledRGB(uint8_t r, uint8_t g, uint8_t b) {
  ledNow[0] = r; ledNow[1] = g; ledNow[2] = b;
  auto out = [](uint8_t pin, uint8_t v) {
    ledcWrite(pin, LED_ANODO_COMUN ? 255 - v : v);
  };
  out(LED_R, (uint16_t)r * GAIN_R / 255); out(LED_G, (uint16_t)g * GAIN_G / 255); out(LED_B, (uint16_t)b * GAIN_B / 255);
}

// Color del LED según el estado que llega de la tablet.
void updateLed(uint32_t now, bool dataOk, float hold) {
  if (hold > 0) { const uint8_t v = 40 + 215 * hold; ledRGB(v, v, v); return; }  // manteniendo el botón: blanco que crece
  if (!dataOk) {  // sin datos: azul respirando
    const float k = 0.15f + 0.85f * (0.5f - 0.5f * cosf(now * 0.0025f));
    ledRGB(0, 0, 255 * k);
    return;
  }
  if (st.flags & F_SOS) {
    if (st.flags & F_BASEOK) ledRGB(90, 0, 255);              // la base confirmó: magenta fijo (poco rojo: el azul es débil)
    else if ((now / 150) % 2) ledRGB(255, 0, 0); else ledRGB(0, 0, 255);  // SOS: rojo/azul
    return;
  }
  if (st.flags & (F_STORM | F_RAD | F_O2 | F_BAT | F_EVAC)) {  // peligro: rojo parpadeando
    if ((now / 250) % 2) ledRGB(255, 0, 0); else ledRGB(0, 0, 0);
    return;
  }
  if (st.flags & F_COMMS) { ledRGB(255, 190, 0); return; }     // aviso: amarillo (rojo 255 + verde ~75 reales)
  ledRGB(0, 255, 0);                                           // todo bien: verde
}

void setup() {
  Serial.begin(115200);
  Wire.begin(PIN_SDA, PIN_SCL);
  oled.begin();
  oled.setContrast(255);
  pinMode(BTN_SOS, INPUT_PULLUP);
  for (uint8_t p : {LED_R, LED_G, LED_B}) {
    const bool ok = ledcAttach(p, 5000, 8);
    Serial.printf("$D,ledc pin %u %s\n", p, ok ? "OK" : "FALLO");
  }
  ledRGB(0, 0, 0);
}

void loop() {
  readSerial();
  static uint32_t lastDraw = 0;
  const uint32_t now = millis();
  if (now - lastDraw < 50) return;  // ~20 fps
  lastDraw = now;

  const bool dataOk = st.everRx && now - st.lastRx <= DATA_TIMEOUT_MS;
  const float hold = sosButton(now);
  updateLed(now, dataOk, hold);
  static uint32_t lastDbg = 0;  // diagnóstico 1 vez/s: "$D,flags,datos,boton,R,G,B" (el servidor lo ignora)
  if (now - lastDbg > 1000) {
    lastDbg = now;
    Serial.printf("$D,%u,%d,%d,%u,%u,%u,%s,duty R%lu G%lu B%lu\n", st.flags, dataOk, digitalRead(BTN_SOS), ledNow[0], ledNow[1], ledNow[2], st.text,
                  ledcRead(LED_R), ledcRead(LED_G), ledcRead(LED_B));
  }
  oled.clearBuffer();
  if (!dataOk) {
    oled.setFont(u8g2_font_6x10_tr);
    oled.drawStr(14, 24, "MARS EVA NAV");
    oled.drawStr(8, 42, st.everRx ? "SIN DATOS TABLET" : "ESPERANDO USB...");
  } else {
    drawCompass(31, 32, 30);
    drawInfo();
    // Alerta crítica: parpadeo invirtiendo toda la pantalla.
    const bool critical = st.flags & (F_STORM | F_RAD | F_O2 | F_BAT | F_EVAC | F_SOS);
    if (critical && (now / 400) % 2) {
      oled.setDrawColor(2);
      oled.drawBox(0, 0, 128, 64);
      oled.setDrawColor(1);
    }
  }
  if (hold > 0) {  // manteniendo el botón: barra de progreso del SOS
    oled.setDrawColor(0);
    oled.drawBox(4, 18, 120, 28);
    oled.setDrawColor(1);
    oled.drawFrame(4, 18, 120, 28);
    oled.setFont(u8g2_font_6x10_tr);
    oled.drawStr(10, 30, (st.flags & F_SOS) ? "MANTENER:CANCELAR" : "MANTENER: SOS");
    oled.drawFrame(10, 34, 108, 8);
    oled.drawBox(12, 36, (int)(104 * hold), 4);
  }
  oled.sendBuffer();
}
