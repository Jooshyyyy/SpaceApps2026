#include "comms/websocket.hpp"
#include <iostream>
#include <string>

namespace {
int checks = 0, failures = 0;
#define CHECK(x) do { ++checks; if (!(x)) { ++failures; std::cerr << "FAIL line " << __LINE__ << ": " << #x << '\n'; } } while (false)
template <typename F> void rejects(F f) {
    bool threw = false;
    try { f(); } catch (const comms::WebSocketError&) { threw = true; }
    CHECK(threw);
}
std::string request() {
    return "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: keep-alive, Upgrade\r\n"
           "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n";
}
std::string masked(std::string payload) {
    std::string out = "\x81";
    if (payload.size() < 126) out += static_cast<char>(0x80 | payload.size());
    else { out += '\xFE'; out += static_cast<char>(payload.size() >> 8); out += static_cast<char>(payload.size()); }
    out += "abcd";
    for (std::size_t i = 0; i < payload.size(); ++i) out += static_cast<char>(payload[i] ^ "abcd"[i % 4]);
    return out;
}
}
int main() {
    CHECK(comms::upgrade_response(request()).find("s3pPLMBiTxaQ9kYGzzhZRbK+xOo=") != std::string::npos);
    auto origin = request(); origin.insert(origin.size() - 2, "Origin: https://untrusted.example\r\n");
    rejects([&] { comms::upgrade_response(origin); });
    rejects([] { comms::upgrade_response("GET / HTTP/1.1\r\n\r\n"); });
    auto version = request(); version.replace(version.find("Version: 13"), 11, "Version: 12");
    rejects([&] { comms::upgrade_response(version); });
    auto duplicate = request(); duplicate.insert(duplicate.size()-2, "Host: duplicate\r\n");
    rejects([&] { comms::upgrade_response(duplicate); });
    auto bytes = masked("hello"), partial = bytes.substr(0, 5);
    CHECK(!comms::parse_websocket(partial));
    partial += bytes.substr(5);
    auto message = comms::parse_websocket(partial);
    CHECK(message && message->opcode == 1 && message->payload == "hello");
    CHECK(partial.empty());
    bytes = masked(std::string(126, 'x'));
    CHECK(comms::parse_websocket(bytes)->payload.size() == 126);
    bytes = std::string("\x81\x00", 2);
    rejects([&] { comms::parse_websocket(bytes); });
    bytes = masked("hello"); bytes[0] = 1; // fragmentation deliberately unsupported
    rejects([&] { comms::parse_websocket(bytes); });
    bytes = masked(std::string(4097, 'x'));
    rejects([&] { comms::parse_websocket(bytes); });
    bytes = masked(std::string(126, 'x')); bytes[0] = '\x89'; // oversized ping
    rejects([&] { comms::parse_websocket(bytes); });
    CHECK(comms::valid_utf8("Mars \xF0\x9F\x9A\x80"));
    CHECK(!comms::valid_utf8("\xC0\x80"));
    CHECK(!comms::valid_utf8("\xED\xA0\x80"));
    bytes = masked("\xFF");
    rejects([&] { comms::parse_websocket(bytes); });
    rejects([] { comms::check_json_depth(std::string(17, '[')); });
    CHECK(comms::json_quote("\n\"\\") == "\"\\u000a\\\"\\\\\"");
    std::cout << checks-failures << '/' << checks << " checks passed\n";
    return failures ? 1 : 0;
}
