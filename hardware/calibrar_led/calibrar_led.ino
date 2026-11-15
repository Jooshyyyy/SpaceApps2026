// Calibración del LED RGB por PWM: "$P,r,g,b\n" (0-255) en R=GPIO27, G=GPIO26, B=GPIO14 (cátodo común).
const uint8_t R = 27, G = 26, B = 14;
char line[32]; size_t n = 0;
void setup() { Serial.begin(115200); for (uint8_t p : {R, G, B}) { ledcAttach(p, 5000, 8); ledcWrite(p, 0); } }
void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') { line[n] = 0; n = 0;
      int r, g, b; if (sscanf(line, "$P,%d,%d,%d", &r, &g, &b) == 3) { ledcWrite(R, r); ledcWrite(G, g); ledcWrite(B, b); Serial.printf("OK %d %d %d\n", r, g, b); }
    } else if (n < sizeof(line) - 1) line[n++] = c;
  }
}
