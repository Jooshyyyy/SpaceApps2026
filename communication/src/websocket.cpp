#include "comms/websocket.hpp"
#include <array>
#include <bit>
#include <cctype>
#include <iomanip>
#include <map>
#include <sstream>

namespace comms {
namespace {
// SHA-1 is used solely for RFC 6455's non-security handshake, never for auth.
std::array<std::uint8_t, 20> sha1(std::string input) {
    const std::uint64_t bits = static_cast<std::uint64_t>(input.size()) * 8;
    input.push_back(static_cast<char>(0x80));
    while (input.size() % 64 != 56) input.push_back(0);
    for (int i = 7; i >= 0; --i) input.push_back(static_cast<char>(bits >> (i * 8)));
    std::array<std::uint32_t, 5> h{0x67452301u, 0xEFCDAB89u, 0x98BADCFEu, 0x10325476u, 0xC3D2E1F0u};
    for (std::size_t off = 0; off < input.size(); off += 64) {
        std::uint32_t w[80]{};
        for (int i = 0; i < 16; ++i) {
            for (int j = 0; j < 4; ++j) w[i] = (w[i] << 8) | static_cast<unsigned char>(input[off + i * 4 + j]);
        }
        for (int i = 16; i < 80; ++i) w[i] = std::rotl(w[i-3] ^ w[i-8] ^ w[i-14] ^ w[i-16], 1);
        auto [a, b, c, d, e] = h;
        for (int i = 0; i < 80; ++i) {
            std::uint32_t f, k;
            if (i < 20) { f = (b & c) | (~b & d); k = 0x5A827999u; }
            else if (i < 40) { f = b ^ c ^ d; k = 0x6ED9EBA1u; }
            else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8F1BBCDCu; }
            else { f = b ^ c ^ d; k = 0xCA62C1D6u; }
            auto temp = std::rotl(a, 5) + f + e + k + w[i];
            e = d; d = c; c = std::rotl(b, 30); b = a; a = temp;
        }
        h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e;
    }
    std::array<std::uint8_t, 20> out{};
    for (int i = 0; i < 20; ++i) out[i] = static_cast<std::uint8_t>(h[i / 4] >> (24 - (i % 4) * 8));
    return out;
}
std::string base64(const std::array<std::uint8_t, 20>& input) {
    constexpr auto alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    for (std::size_t i = 0; i < input.size(); i += 3) {
        std::uint32_t n = static_cast<std::uint32_t>(input[i]) << 16;
        if (i + 1 < input.size()) n |= static_cast<std::uint32_t>(input[i+1]) << 8;
        if (i + 2 < input.size()) n |= input[i+2];
        out += alphabet[(n >> 18) & 63]; out += alphabet[(n >> 12) & 63];
        out += i+1 < input.size() ? alphabet[(n >> 6) & 63] : '=';
        out += i+2 < input.size() ? alphabet[n & 63] : '=';
    }
    return out;
}
std::string lower(std::string text) {
    for (auto& c : text) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return text;
}
std::string trim(std::string text) {
    auto first = text.find_first_not_of(" \t\r");
    if (first == std::string::npos) return {};
    return text.substr(first, text.find_last_not_of(" \t\r") - first + 1);
}
bool token(std::string value, std::string_view wanted) {
    std::istringstream in(lower(std::move(value)));
    std::string item;
    while (std::getline(in, item, ',')) if (trim(item) == wanted) return true;
    return false;
}
}

std::string upgrade_response(std::string_view request) {
    if (request.size() > 8192) throw WebSocketError("HTTP headers too large");
    std::istringstream in{std::string(request)};
    std::string line;
    std::getline(in, line);
    if (line != "GET / HTTP/1.1\r") throw WebSocketError("expected GET / HTTP/1.1");
    std::map<std::string, std::string> headers;
    while (std::getline(in, line) && line != "\r") {
        const auto colon = line.find(':');
        if (colon == std::string::npos) throw WebSocketError("invalid HTTP header");
        auto key = lower(trim(line.substr(0, colon)));
        if (!headers.emplace(key, trim(line.substr(colon + 1))).second) throw WebSocketError("duplicate HTTP header");
    }
    const auto key = headers["sec-websocket-key"];
    bool key_valid = key.size() == 24 && key.substr(22) == "==";
    for (std::size_t i = 0; key_valid && i < 22; ++i) {
        const auto c = static_cast<unsigned char>(key[i]);
        key_valid = std::isalnum(c) || c == '+' || c == '/';
    }
    if (!key_valid || headers["host"].empty() || headers["sec-websocket-version"] != "13" ||
        !token(headers["connection"], "upgrade") || lower(headers["upgrade"]) != "websocket") {
        throw WebSocketError("invalid WebSocket upgrade");
    }
    // No browser origins: avoid cross-site WebSocket control of a local bus.
    // Native Dart/Python clients omit Origin.
    if (headers.contains("origin")) throw WebSocketError("browser origins are not supported");
    return "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " +
           base64(sha1(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")) + "\r\n\r\n";
}

std::string websocket_message(std::uint8_t opcode, std::string_view payload) {
    std::string out(1, static_cast<char>(0x80u | opcode));
    const auto size = static_cast<std::uint64_t>(payload.size());
    if (size < 126) out.push_back(static_cast<char>(size));
    else if (size <= 65535) {
        out.push_back(126); out.push_back(static_cast<char>(size >> 8)); out.push_back(static_cast<char>(size));
    } else {
        out.push_back(127);
        for (int i = 7; i >= 0; --i) out.push_back(static_cast<char>(size >> (8 * i)));
    }
    out.append(payload);
    return out;
}

std::optional<WebSocketMessage> parse_websocket(std::string& buffer) {
    if (buffer.size() < 2) return std::nullopt;
    const auto byte = [&](std::size_t i) { return static_cast<std::uint8_t>(buffer[i]); };
    const auto first = byte(0), second = byte(1);
    const auto opcode = static_cast<std::uint8_t>(first & 15);
    if ((first & 0xF0) != 0x80 || !(second & 0x80) ||
        (opcode != 1 && opcode != 8 && opcode != 9 && opcode != 10)) {
        throw WebSocketError("only masked, complete text/control messages are supported");
    }
    std::size_t offset = 2;
    std::uint64_t size = second & 127;
    if (size == 126) {
        if (buffer.size() < 4) return std::nullopt;
        size = static_cast<std::uint64_t>(byte(2)) * 256 + byte(3); offset = 4;
        if (size < 126) throw WebSocketError("noncanonical WebSocket length");
    } else if (size == 127) {
        throw WebSocketError("client message exceeds 4096 bytes");
    }
    if (size > 4096 || (opcode >= 8 && size > 125) || (opcode == 8 && size == 1)) {
        throw WebSocketError("invalid WebSocket message length");
    }
    if (buffer.size() < offset + 4 + size) return std::nullopt;
    std::string payload;
    payload.reserve(static_cast<std::size_t>(size));
    for (std::size_t i = 0; i < size; ++i) payload.push_back(static_cast<char>(byte(offset + 4 + i) ^ byte(offset + i % 4)));
    if (opcode == 1 && !valid_utf8(payload)) throw WebSocketError("invalid UTF-8");
    buffer.erase(0, offset + 4 + static_cast<std::size_t>(size));
    return WebSocketMessage{opcode, std::move(payload)};
}

bool valid_utf8(std::string_view text) {
    for (std::size_t i = 0; i < text.size();) {
        const auto c = static_cast<unsigned char>(text[i++]);
        if (c < 0x80) continue;
        int count;
        std::uint32_t value, minimum;
        if (c >= 0xC2 && c <= 0xDF) { count = 1; value = c & 31; minimum = 0x80; }
        else if (c >= 0xE0 && c <= 0xEF) { count = 2; value = c & 15; minimum = 0x800; }
        else if (c >= 0xF0 && c <= 0xF4) { count = 3; value = c & 7; minimum = 0x10000; }
        else return false;
        while (count--) {
            if (i >= text.size()) return false;
            const auto next = static_cast<unsigned char>(text[i++]);
            if ((next & 0xC0) != 0x80) return false;
            value = (value << 6) | (next & 63);
        }
        if (value < minimum || value > 0x10FFFF || (value >= 0xD800 && value <= 0xDFFF)) return false;
    }
    return true;
}
std::string json_quote(std::string_view text) {
    std::ostringstream out; out << '"';
    for (unsigned char c : text) {
        if (c == '"' || c == '\\') out << '\\' << c;
        else if (c < 32) out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << static_cast<unsigned>(c);
        else out << c;
    }
    out << '"'; return out.str();
}
void check_json_depth(std::string_view text) {
    int depth = 0; bool quoted = false, escaped = false;
    for (const char c : text) {
        if (escaped) { escaped = false; continue; }
        if (quoted && c == '\\') { escaped = true; continue; }
        if (c == '"') { quoted = !quoted; continue; }
        if (quoted) continue;
        if (c == '[' || c == '{') { if (++depth > 16) throw WebSocketError("JSON nesting exceeds 16"); }
        if (c == ']' || c == '}') --depth;
    }
}
} // namespace comms
