// Host contract tests for the ESP32 watch transport helpers.
// These do not emulate the Arduino runtime: they pin the byte framing, retry
// schedule and configuration rules the sketch relies on.
#include "../watch_transport.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace {

int gChecks = 0;
int gFailures = 0;
std::vector<std::string> gLines;

void collect(char* line) { gLines.push_back(std::string(line)); }

void check(bool ok, const char* expression, int line) {
  ++gChecks;
  if (!ok) {
    ++gFailures;
    std::printf("FAIL line %d: %s\n", line, expression);
  }
}

#define CHECK(expression) check((expression), #expression, __LINE__)

void checkLine(size_t index, const std::string& expected, int line) {
  check(index < gLines.size() && gLines[index] == expected, expected.c_str(), line);
}

void checkLineSize(size_t index, size_t expected, int line) {
  check(index < gLines.size() && gLines[index].size() == expected, "line size", line);
}

#define CHECK_LINE(index, expected) checkLine((index), (expected), __LINE__)
#define CHECK_LINE_SIZE(index, expected) checkLineSize((index), (expected), __LINE__)

void feed(watch::LineAssembler& assembler, const char* bytes) {
  assembler.feed(bytes, std::strlen(bytes), collect);
}

std::string repeated(size_t count, char byte) { return std::string(count, byte); }

void testLineAssembly() {
  watch::LineAssembler assembler;
  feed(assembler, "$W,1\n$W,2\r\n");
  CHECK(gLines.size() == 2);
  CHECK_LINE(0, "$W,1");
  CHECK_LINE(1, "$W,2");

  gLines.clear();
  feed(assembler, "$W,partial");
  CHECK(gLines.empty());
  CHECK(assembler.hasPartial());
  CHECK(assembler.pending() == 10);
  feed(assembler, "-rest\n");
  CHECK(gLines.size() == 1);
  CHECK_LINE(0, "$W,partial-rest");
  CHECK(!assembler.hasPartial());

  gLines.clear();
  feed(assembler, "\n\r\n\r");
  CHECK(gLines.empty());

  gLines.clear();
  feed(assembler, "$W,cr\r$W,lf\n$W,crlf\r\n");
  CHECK(gLines.size() == 3);
  CHECK_LINE(0, "$W,cr");
  CHECK_LINE(1, "$W,lf");
  CHECK_LINE(2, "$W,crlf");

  gLines.clear();
  feed(assembler, "$W,split\r");
  feed(assembler, "\n");
  CHECK(gLines.size() == 1);
  CHECK_LINE(0, "$W,split");

  CHECK(assembler.feed("$W,ignored\n", 11, nullptr) == 0);
}

void testLineLengthLimit() {
  watch::LineAssembler assembler;
  gLines.clear();
  const std::string exact = repeated(watch::kMaxLinePayload, 'A');
  CHECK(assembler.feed(exact.data(), exact.size(), collect) == 0);
  CHECK(assembler.hasPartial());
  CHECK(assembler.pending() == exact.size());
  CHECK(assembler.feed("\n", 1, collect) == 1);
  CHECK(gLines.size() == 1);
  CHECK_LINE_SIZE(0, watch::kMaxLinePayload);

  gLines.clear();
  assembler.reset();
  const std::string oversize = repeated(watch::kMaxLinePayload + 1, 'B');
  CHECK(assembler.feed(oversize.data(), oversize.size(), collect) == 0);
  CHECK(assembler.dropping());
  CHECK(!assembler.hasPartial());
  CHECK(assembler.feed("\n", 1, collect) == 0);
  CHECK(!assembler.dropping());
  CHECK(gLines.empty());
  feed(assembler, "$W,after\n");
  CHECK(gLines.size() == 1);
  CHECK_LINE(0, "$W,after");

  // A large WebSocket text payload may hold several lines; only complete,
  // in-limit lines reach the parser.
  gLines.clear();
  assembler.reset();
  const std::string batch = "$W,ok\n" + repeated(300, 'C') + "\n$W,ok2\n";
  CHECK(assembler.feed(batch.data(), batch.size(), collect) == 2);
  CHECK(gLines.size() == 2);
  CHECK_LINE(0, "$W,ok");
  CHECK_LINE(1, "$W,ok2");
}

void testBackoff() {
  const uint32_t expected[] = {1000, 2000, 4000, 8000, 16000, 30000, 30000};
  for (uint8_t attempt = 0; attempt < 7; ++attempt) {
    CHECK(watch::backoffDelayMs(attempt) == expected[attempt]);
  }
  CHECK(watch::backoffDelayMs(255) == watch::kBackoffMaxMs);
  CHECK(watch::backoffDelayMs(0, 250, 500) == 250);
  CHECK(watch::backoffDelayMs(1, 250, 500) == 500);
  CHECK(watch::backoffDelayMs(5, 250, 500) == 500);
}

void testReconnectSchedule() {
  watch::ReconnectSchedule schedule;
  schedule.reset(100);
  CHECK(schedule.attempts() == 0);
  CHECK(schedule.due(100));
  schedule.recordFailure(100);
  CHECK(schedule.attempts() == 1);
  CHECK(schedule.nextAttemptAt() == 1100);
  CHECK(!schedule.due(1099));
  CHECK(schedule.due(1100));
  schedule.recordFailure(1100);
  CHECK(schedule.nextAttemptAt() == 3100);
  schedule.recordSuccess();
  CHECK(schedule.attempts() == 0);
  CHECK(schedule.due(3100));

  watch::ReconnectSchedule deferred(1000, 5000);
  deferred.reset(0);
  deferred.defer(0, 250);
  CHECK(!deferred.due(249));
  CHECK(deferred.due(250));

  watch::ReconnectSchedule wrapping;
  wrapping.reset(0xFFFFFF00u);
  CHECK(wrapping.due(0xFFFFFF00u));
  wrapping.recordFailure(0xFFFFFF00u);
  CHECK(wrapping.nextAttemptAt() == 744);
  CHECK(!wrapping.due(743));
  CHECK(wrapping.due(744));

  watch::ReconnectSchedule deferredWrap;
  deferredWrap.reset(0xFFFFFFFFu - 100);
  deferredWrap.defer(0xFFFFFFFFu - 100, 200);
  CHECK(deferredWrap.nextAttemptAt() == 99);
  CHECK(!deferredWrap.due(98));
  CHECK(deferredWrap.due(99));
}

void testClockGate() {
  CHECK(!watch::clockReadyForTls(0));
  CHECK(!watch::clockReadyForTls(1700000000));
  CHECK(!watch::clockReadyForTls(watch::kTlsMinEpochSeconds - 1));
  CHECK(watch::clockReadyForTls(watch::kTlsMinEpochSeconds));
  CHECK(watch::clockReadyForTls(1767225600));
}

void testConfigValidation() {
  const char* token = "token-de-prueba-1234567890";
  const char* pem =
      "-----BEGIN CERTIFICATE-----\nNO_ES_UN_CERTIFICADO_REAL\n-----END CERTIFICATE-----\n";
  CHECK(watch::configUsable("reloj-eva.onrender.com", token, pem));
  CHECK(!watch::configUsable(nullptr, token, pem));
  CHECK(!watch::configUsable("", token, pem));
  CHECK(!watch::configUsable("https://reloj-eva.onrender.com", token, pem));
  CHECK(!watch::configUsable("reloj-eva.onrender.com/reloj/ws", token, pem));
  CHECK(!watch::configUsable("reloj eva", token, pem));
  CHECK(!watch::configUsable(repeated(254, 'h').c_str(), token, pem));
  CHECK(!watch::configUsable("reloj-eva.onrender.com", nullptr, pem));
  CHECK(!watch::configUsable("reloj-eva.onrender.com", "", pem));
  CHECK(!watch::configUsable("reloj-eva.onrender.com", "token con espacio", pem));
  CHECK(!watch::configUsable("reloj-eva.onrender.com", "token\ninyectado", pem));
  CHECK(!watch::configUsable("reloj-eva.onrender.com", repeated(257, 't').c_str(), pem));
  CHECK(!watch::configUsable("reloj-eva.onrender.com", token, nullptr));
  CHECK(!watch::configUsable("reloj-eva.onrender.com", token, ""));
  CHECK(!watch::configUsable("reloj-eva.onrender.com", token, "-----BEGIN CERTIFICATE-----"));
  CHECK(!watch::configUsable("reloj-eva.onrender.com", token, "no pem here"));
}

void testAuthorizationHeader() {
  char header[64] = {0};
  CHECK(watch::buildAuthorizationHeader(header, sizeof(header), "token-abc") == 31);
  CHECK(std::strcmp(header, "Authorization: Bearer token-abc") == 0);
  CHECK(std::strstr(header, "\r\n") == nullptr);
  CHECK(watch::buildAuthorizationHeader(header, sizeof(header), "bad token") == 0);
  CHECK(watch::buildAuthorizationHeader(header, sizeof(header), "bad\ntoken") == 0);
  CHECK(watch::buildAuthorizationHeader(header, sizeof(header), "") == 0);
  CHECK(watch::buildAuthorizationHeader(header, sizeof(header), nullptr) == 0);
  char exact[32];
  CHECK(watch::buildAuthorizationHeader(exact, sizeof(exact), "token-abc") == 31);
  char small[31];
  CHECK(watch::buildAuthorizationHeader(small, sizeof(small), "token-abc") == 0);
  CHECK(watch::buildAuthorizationHeader(nullptr, sizeof(header), "token-abc") == 0);
}

void testOutboundFrame() {
  char frame[32] = {0};
  CHECK(watch::buildOutboundFrame(frame, sizeof(frame), "$B,SOS") == 7);
  CHECK(std::strcmp(frame, "$B,SOS\n") == 0);
  CHECK(watch::buildOutboundFrame(frame, sizeof(frame), "$B,CANCEL") == 10);
  CHECK(std::strcmp(frame, "$B,CANCEL\n") == 0);
  const std::string twenty = "$B," + repeated(20, 'A');
  CHECK(watch::buildOutboundFrame(frame, sizeof(frame), twenty.c_str()) == 24);
  CHECK(std::strcmp(frame, (twenty + "\n").c_str()) == 0);

  CHECK(watch::buildOutboundFrame(frame, sizeof(frame), "$W,1") == 0);
  CHECK(watch::buildOutboundFrame(frame, sizeof(frame), "$B,") == 0);
  CHECK(watch::buildOutboundFrame(frame, sizeof(frame), "$B,SOS\n") == 0);
  CHECK(watch::buildOutboundFrame(frame, sizeof(frame), "$B,SOS SOS") == 0);
  CHECK(watch::buildOutboundFrame(frame, sizeof(frame),
                                  ("$B," + repeated(21, 'A')).c_str()) == 0);
  CHECK(watch::buildOutboundFrame(frame, sizeof(frame), nullptr) == 0);
  char small[7] = {0};
  CHECK(watch::buildOutboundFrame(small, sizeof(small), "$B,SOS") == 0);
  CHECK(small[0] == '\0');
}

}  // namespace

int main() {
  testLineAssembly();
  testLineLengthLimit();
  testBackoff();
  testReconnectSchedule();
  testClockGate();
  testConfigValidation();
  testAuthorizationHeader();
  testOutboundFrame();
  std::printf("%d checks, %d failures\n", gChecks, gFailures);
  return gFailures == 0 ? 0 : 1;
}
