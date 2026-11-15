// Narrow host probe for the arduinoWebSockets 2.6.1 API used by the sketch.
//
// ESP32 selects NETWORK_ESP32, which defines SSL_AXTLS in WebSockets.h, so the
// exact 2.6.1 declaration is:
//   void beginSSL(const char*, uint16_t, const char* url = "/",
//                 const char* fingerprint = "", const char* protocol = "arduino");
//   void beginSslWithCA(const char*, uint16_t, const char* url = "/",
//                       const char* CA_cert = nullptr,
//                       const char* protocol = "arduino");
// Passing the root PEM to beginSSL therefore compiles, but stores it as a
// fingerprint and leaves _CA_cert null. This probe models that source behavior
// and instantiates the same watch::beginTlsWithCa template as the sketch.
// It does not emulate TLS, WiFi or the real library.
#include "../watch_transport.h"

#include <cstdint>
#include <cstdio>
#include <cstring>

namespace {

class WebSocketsClient261 {
 public:
  // Exact ESP32/SSL_AXTLS signatures from 2.6.1 WebSocketsClient.h.
  void beginSSL(const char* host, uint16_t port, const char* url = "/",
                const char* fingerprint = "", const char* protocol = "arduino") {
    (void)host;
    (void)port;
    (void)url;
    (void)protocol;
    fingerprint_ = fingerprint;
    ca_ = nullptr;
  }

  void beginSslWithCA(const char* host, uint16_t port, const char* url = "/",
                      const char* CA_cert = nullptr, const char* protocol = "arduino") {
    (void)host;
    (void)port;
    (void)url;
    (void)protocol;
    fingerprint_ = "";
    ca_ = CA_cert;
  }

  const char* fingerprint() const { return fingerprint_; }
  const char* ca() const { return ca_; }

 private:
  const char* fingerprint_ = "";
  const char* ca_ = nullptr;
};

}  // namespace

int main() {
  const char* pem = "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n";

  WebSocketsClient261 wrong;
  wrong.beginSSL("reloj-eva.onrender.com", 443, "/reloj/ws", pem);
  if (wrong.ca() != nullptr || std::strcmp(wrong.fingerprint(), pem) != 0) {
    std::puts("FAIL: 2.6.1 beginSSL no longer treats the PEM as a fingerprint");
    return 1;
  }

  WebSocketsClient261 right;
  watch::beginTlsWithCa(right, "reloj-eva.onrender.com", 443, "/reloj/ws", pem);
  if (right.ca() == nullptr || std::strcmp(right.ca(), pem) != 0) {
    std::puts("FAIL: watch::beginTlsWithCa did not configure the CA");
    return 2;
  }
  if (right.fingerprint()[0] != '\0') {
    std::puts("FAIL: beginSslWithCA left a fingerprint configured");
    return 3;
  }

  std::puts("2.6.1 ESP32 API probe OK: beginSSL=PEM-as-fingerprint, beginSslWithCA=CA");
  return 0;
}
