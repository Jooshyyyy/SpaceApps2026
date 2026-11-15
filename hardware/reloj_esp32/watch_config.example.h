// Copia este archivo a watch_config.h y completa los valores reales.
// watch_config.h está en .gitignore: el token es un secreto y no se versiona.
#pragma once

// Host público de Render, sin "https://" ni ruta (p. ej. mi-reloj.onrender.com).
#define WATCH_HOST "PON_AQUI_TU_SERVICIO.onrender.com"

// Render expone HTTP y WSS en el mismo puerto público.
#define WATCH_PORT 443
#define WATCH_PATH "/reloj/ws"

// Mismo valor de WATCH_TOKEN configurado en el servidor. Va en la cabecera
// Authorization: Bearer, nunca en la URL.
#define WATCH_TOKEN "PON_AQUI_EL_TOKEN"

// Certificado RAÍZ que valida la cadena TLS de tu servicio Render.
// El sketch lo pasa a beginSslWithCA; no uses beginSSL (su 4.º argumento es
// una huella SHA-1 en ESP32) ni setInsecure. Pega el PEM completo de la raíz,
// con BEGIN/END, como un literal C. Instrucciones en README.md.
// Sin este valor el reloj no intenta WSS y sigue funcionando por USB.
#define WATCH_TRUSTED_ROOT_PEM ""

// Plantilla del literal (bórrala al pegar el PEM real):
// #define WATCH_TRUSTED_ROOT_PEM \
//   "-----BEGIN CERTIFICATE-----\n" \
//   "<líneas base64 de la raíz>\n" \
//   "-----END CERTIFICATE-----\n"

// NTP necesario para que WiFiClientSecure valide la vigencia del certificado.
#define WATCH_NTP_SERVER_1 "pool.ntp.org"
#define WATCH_NTP_SERVER_2 "time.google.com"
