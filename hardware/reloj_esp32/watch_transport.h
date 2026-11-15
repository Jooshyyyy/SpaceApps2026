// Transport helpers shared by the ESP32 sketch and its host tests.
//
// They stay free of Arduino calls so the framing, retry and configuration
// rules can be checked with a normal host compiler. The sketch owns the
// hardware-facing pieces (Serial, WiFiClientSecure, U8g2, WebSocketsClient).
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace watch {

// servidor.py rejects lines longer than MAX_LINE = 200, so the watch accepts
// exactly that many payload bytes per line and discards anything longer.
constexpr size_t kMaxLinePayload = 200;
constexpr uint32_t kBackoffInitialMs = 1000;
constexpr uint32_t kBackoffMaxMs = 30000;
// 2025-01-01T00:00:00Z. A certificate cannot validate if the clock says 1970.
constexpr int64_t kTlsMinEpochSeconds = 1735689600;

using LineHandler = void (*)(char* line);

// Reassembles newline-terminated ASCII lines from arbitrary chunks. Serial and
// WebSocket text frames feed separate instances so a partial USB line can
// never be joined with a WebSocket line.
class LineAssembler {
 public:
  LineAssembler() { reset(); }

  void reset() {
    len_ = 0;
    dropping_ = false;
    afterCr_ = false;
  }

  // Emits each complete line through handler and returns how many it emitted.
  // '\r', '\n' and '\r\n' all terminate a line; empty lines are ignored. An
  // over-long line is dropped whole (including its terminator) and the next
  // line starts clean after the next terminator.
  size_t feed(const char* data, size_t length, LineHandler handler) {
    if (data == nullptr || handler == nullptr) return 0;
    size_t emitted = 0;
    for (size_t i = 0; i < length; ++i) {
      const char c = data[i];
      if (afterCr_) {
        afterCr_ = false;
        if (c == '\n') continue;  // the LF of a CRLF pair never starts a line
      }
      if (c == '\r') {
        emitted += finishLine(handler);
        afterCr_ = true;
        continue;
      }
      if (c == '\n') {
        emitted += finishLine(handler);
        continue;
      }
      if (dropping_) continue;
      if (len_ < kMaxLinePayload) {
        buffer_[len_++] = c;
      } else {
        dropping_ = true;
        len_ = 0;
      }
    }
    return emitted;
  }

  bool hasPartial() const { return len_ > 0; }
  bool dropping() const { return dropping_; }
  size_t pending() const { return len_; }

 private:
  size_t finishLine(LineHandler handler) {
    if (dropping_) {
      dropping_ = false;
      len_ = 0;
      return 0;
    }
    if (len_ == 0) return 0;
    buffer_[len_] = '\0';
    handler(buffer_);
    len_ = 0;
    return 1;
  }

  char buffer_[kMaxLinePayload + 1] = {};
  size_t len_ = 0;
  bool dropping_ = false;
  bool afterCr_ = false;
};

// 1 s, 2 s, 4 s, 8 s, 16 s, then a 30 s cap. No attempt ever exceeds maxMs.
inline uint32_t backoffDelayMs(uint8_t attempt, uint32_t initialMs = kBackoffInitialMs,
                               uint32_t maxMs = kBackoffMaxMs) {
  if (initialMs == 0) initialMs = 1;
  if (maxMs < initialMs) maxMs = initialMs;
  uint32_t delay = initialMs;
  for (uint8_t i = 0; i < attempt && delay < maxMs; ++i) {
    delay = (delay > maxMs / 2) ? maxMs : delay * 2;
  }
  return delay;
}

// The WSS handshake must wait for NTP: WiFiClientSecure validates the Render
// certificate dates against the system clock.
inline bool clockReadyForTls(int64_t epochSeconds) {
  return epochSeconds >= kTlsMinEpochSeconds;
}

// Non-blocking reconnect bookkeeping. millis() wraps every ~49 days, so "due"
// compares signed differences instead of wall-clock ordering.
class ReconnectSchedule {
 public:
  ReconnectSchedule(uint32_t initialMs = kBackoffInitialMs, uint32_t maxMs = kBackoffMaxMs)
      : initialMs_(initialMs), maxMs_(maxMs), nextAtMs_(0), attempts_(0) {}

  void reset(uint32_t nowMs) {
    attempts_ = 0;
    nextAtMs_ = nowMs;
  }

  bool due(uint32_t nowMs) const {
    return static_cast<int32_t>(nowMs - nextAtMs_) >= 0;
  }

  void defer(uint32_t nowMs, uint32_t delayMs) { nextAtMs_ = nowMs + delayMs; }

  void recordFailure(uint32_t nowMs) {
    const uint32_t delay = backoffDelayMs(attempts_, initialMs_, maxMs_);
    if (attempts_ < 255) ++attempts_;
    nextAtMs_ = nowMs + delay;
  }

  void recordSuccess() {
    attempts_ = 0;
    nextAtMs_ = 0;
  }

  uint8_t attempts() const { return attempts_; }
  uint32_t nextAttemptAt() const { return nextAtMs_; }

 private:
  uint32_t initialMs_;
  uint32_t maxMs_;
  uint32_t nextAtMs_;
  uint8_t attempts_;
};

namespace detail {
inline bool tokenValid(const char* token) {
  if (token == nullptr) return false;
  const size_t tokenLen = strlen(token);
  if (tokenLen == 0 || tokenLen > 256) return false;
  for (size_t i = 0; i < tokenLen; ++i) {
    const unsigned char c = static_cast<unsigned char>(token[i]);
    if (c <= 0x20 || c >= 0x7f) return false;  // no CR/LF header injection
  }
  return true;
}
}  // namespace detail

// WSS configuration is only attempted when the secret header and the pinned
// root are both present. The host must be a bare DNS name/us address because
// WebSocketsClient::beginSslWithCA expects host + path separately.
inline bool configUsable(const char* host, const char* token, const char* caPem) {
  if (host == nullptr || token == nullptr || caPem == nullptr) return false;

  const size_t hostLen = strlen(host);
  if (hostLen == 0 || hostLen > 253) return false;
  for (size_t i = 0; i < hostLen; ++i) {
    const unsigned char c = static_cast<unsigned char>(host[i]);
    if (c <= 0x20 || c >= 0x7f) return false;
    if (c == '/' || c == '\\' || c == '?' || c == '#' || c == '@') return false;
  }
  if (strstr(host, "://") != nullptr) return false;

  if (!detail::tokenValid(token)) return false;

  const size_t pemLen = strlen(caPem);
  if (pemLen < 64 || pemLen > 8192) return false;
  return strstr(caPem, "-----BEGIN CERTIFICATE-----") != nullptr &&
         strstr(caPem, "-----END CERTIFICATE-----") != nullptr;
}

// Extra header for WebSocketsClient::setExtraHeaders(). arduinoWebSockets
// 2.6.1 appends one CRLF to extraHeaders in sendHeader(), so this value must
// NOT end with CRLF: an empty line would terminate the HTTP header block early.
constexpr char kAuthorizationPrefix[] = "Authorization: Bearer ";

inline size_t buildAuthorizationHeader(char* out, size_t capacity, const char* token) {
  if (out == nullptr || capacity == 0 || !detail::tokenValid(token)) return 0;
  constexpr size_t prefixLen = sizeof(kAuthorizationPrefix) - 1;
  const size_t tokenLen = strlen(token);
  const size_t total = prefixLen + tokenLen;
  if (total + 1 > capacity) return 0;
  memcpy(out, kAuthorizationPrefix, prefixLen);
  memcpy(out + prefixLen, token, tokenLen);
  out[total] = '\0';
  return total;
}

// arduinoWebSockets 2.6.1 entry point for a PEM trust anchor. On ESP32 the
// library defines SSL_AXTLS, so beginSSL's fourth argument is a SHA-1
// fingerprint: passing a PEM there compiles but never configures the CA. Keep
// the call here so the host API probe exercises the same path as the sketch.
template <typename Client>
inline void beginTlsWithCa(Client& client, const char* host, uint16_t port,
                           const char* path, const char* caPem) {
  client.beginSslWithCA(host, port, path, caPem);
}

// Device -> server button line: "$B,<button>\n". The button is limited to the
// same alphanumeric shape servidor.py accepts and the newline is appended once.
inline size_t buildOutboundFrame(char* out, size_t capacity, const char* line) {
  if (out == nullptr || line == nullptr) return 0;
  if (strncmp(line, "$B,", 3) != 0) return 0;
  const char* button = line + 3;
  const size_t buttonLen = strlen(button);
  if (buttonLen == 0 || buttonLen > 20) return 0;
  for (size_t i = 0; i < buttonLen; ++i) {
    const char c = button[i];
    const bool allowed = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                         (c >= '0' && c <= '9') || c == '_' || c == '-';
    if (!allowed) return 0;
  }
  const size_t total = 3 + buttonLen + 1;  // header + newline
  if (total + 1 > capacity) return 0;      // keep room for the terminator
  memcpy(out, line, 3 + buttonLen);
  out[3 + buttonLen] = '\n';
  out[total] = '\0';
  return total;
}

}  // namespace watch
