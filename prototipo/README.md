# Prototipo Mars EVA Nav — tablet, reloj y base de control

Interfaz del astronauta (tablet), reloj de muñeca (ESP32 + OLED) y panel de la base de control,
conectados al motor de rutas (`core_engine/eva_engine`, capa 2) y al bus de eventos
(`communication/eva_bus`, capa 3) de este repositorio.

## Cómo correrlo
1. Compilar el backend y generar los datos de la zona (ver el README principal):
   `data/processed/*.bin` se regeneran con los scripts de `scripts/`.
2. `cd prototipo && python3 servidor.py` (solo biblioteca estándar de Python).
3. Abrir:
   - astronauta (tablet): `http://127.0.0.1:8791`
   - base de control: `http://127.0.0.1:8791/base`
4. Tablet Android por USB, sin Wi-Fi: `adb reverse tcp:8791 tcp:8791`.

`servidor.py` arranca `eva_bus`, se conecta como cliente WebSocket, sirve la página, comparte el
estado de la EVA entre pantallas y hace de puente USB con el reloj (`/dev/ttyUSB*`, 115200).
Solo una pantalla de astronauta controla el reloj: la última que abrió la página.

`generar_mision.py` regenera `mission.json`: paradas en posiciones reales de Curiosity 2025
(`msl_waypoints.json`, NASA) con rutas de vista previa calculadas por `eva_engine`.

## Funciones
- Elegir parada tocando el mapa o un punto de Curiosity, también durante la EVA
  (el bus recalcula desde la posición actual).
- Botón SOS (mantener 1,5 s; mantener otra vez para cancelar) en la tablet y en el reloj.
- Base de control en otra pantalla: estado en vivo, órdenes (regreso, alertas), confirmación del SOS y bitácora.
- Sonidos de advertencia distintos por alerta, que se repiten mientras dura el peligro.
- Cajón «Simulación» para la demo: velocidad, tormenta, radiación, corte de enlace, SOS.

## Reloj (carpeta `hardware/`)
`hardware/reloj_esp32` — NodeMCU ESP-32S, core ESP32 3.x, librería U8g2.
Compilar: `arduino-cli compile --fqbn esp32:esp32:esp32:UploadSpeed=115200 hardware/reloj_esp32`

| Pieza | Pin |
|---|---|
| OLED 1,3" SH1106 (I2C 0x3C) | SDA = GPIO22, SCL = GPIO21, VCC = 3V3 |
| Botón SOS (a GND, pull-up a 3V3) | GPIO25 |
| LED RGB cátodo común (220–330 Ω por color) | R = GPIO27, G = GPIO26, B = GPIO14 |

LED: verde = todo bien · amarillo = sin enlace · rojo parpadeando = peligro ·
rojo/azul = SOS · magenta = la base confirmó · azul respirando = esperando datos.
Las ganancias de color se calibraron con cámara para este LED (el verde brilla mucho más).

Al encender muestra el logo ARES con el anillo girando alrededor del planeta (`logo_ares.h`;
la página reproduce la misma animación en la mini pantalla simulada).

**Por WiFi (el USB solo da energía):**
1. Sin WiFi guardado, el reloj abre la red `MarsEVA-Reloj-XXXX`. Conéctate con el celular,
   abre http://192.168.4.1, elige tu red y escribe la contraseña (queda solo en el ESP32).
2. El reloj debe estar en la misma red que la PC del servidor. Se anuncia cada 2 s por UDP 4210
   (`$H,reloj`) y el servidor le manda las tramas a esa IP; no hay que configurar direcciones.
3. Si la PC tiene firewall, permite UDP 4210 desde la red local (por ejemplo, con ufw:
   `sudo ufw allow from <tu-red>/24 to any port 4210 proto udp`).
4. Para borrar el WiFi guardado: mantén el botón SOS mientras enciendes el reloj.

Herramientas para revisar el cableado:
- `diagnostico_pines` busca la OLED en todos los pares SDA/SCL y lista los pines con conexión externa.
- `calibrar_led` controla cada canal del LED desde la PC por serie.
- `prueba_led` y `escaner_i2c`: pruebas rápidas.

Trama tablet → reloj: `$W,<rumbo|-1>,<az>,<dist_m>,<o2%>,<bat%>,<ttr_s>,<dosis_uSv_x10>,<flags>,<texto>\n`.
Reloj → tablet: `$B,SOS` / `$B,CANCEL`. Por WiFi van en UDP 4210 (el reloj repite cada botón 3 veces; el servidor descarta los duplicados).
