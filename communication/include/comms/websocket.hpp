#pragma once
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>

namespace comms {
class WebSocketError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};
std::string upgrade_response(std::string_view request);
std::string websocket_message(std::uint8_t opcode, std::string_view payload);
struct WebSocketMessage { std::uint8_t opcode; std::string payload; };
// Consumes one masked client frame, retaining incomplete TCP input. This
// protocol deliberately supports complete messages only (no fragmentation).
std::optional<WebSocketMessage> parse_websocket(std::string& buffer);
bool valid_utf8(std::string_view text);
std::string json_quote(std::string_view text);
void check_json_depth(std::string_view text);
} // namespace comms
