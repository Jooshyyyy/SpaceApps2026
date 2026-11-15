// Telemetry frame v1. Explicit little-endian serialization; never map this
// structure directly onto the wire because C++ padding and host endianness are
// not part of the protocol.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <stdexcept>

namespace comms {

constexpr std::size_t kTelemetryBytes = 24;
constexpr std::uint8_t kFrameMagic = 0x4D; // 'M'
constexpr std::uint8_t kFrameVersion = 1;

class FrameError : public std::runtime_error {
public:
    explicit FrameError(const std::string& what) : std::runtime_error(what) {}
};

struct TelemetryFrame {
    std::uint16_t seq = 0;
    std::uint32_t t_ms = 0;
    std::uint16_t pos_x = 0xFFFF;
    std::uint16_t pos_y = 0xFFFF;
    std::int16_t alt_dm = 0;       // relative to HELLO altitude_origin_m
    std::uint8_t o2 = 0xFF;        // 0..200, units of 0.5%; 0xFF unknown
    std::uint8_t batt = 0xFF;      // 0..200, units of 0.5%; 0xFF unknown
    std::uint16_t rad_cs = 0xFFFF; // units of 0.01 mSv; 0xFFFF unknown
    std::uint16_t ttr_s = 0xFFFF;  // seconds; 0xFFFF unknown
    std::uint8_t flags = 0;
    std::uint8_t reserved = 0;
};

std::uint16_t crc16_ccitt_false(const std::uint8_t* data, std::size_t size);
std::array<std::uint8_t, kTelemetryBytes> encode_telemetry(const TelemetryFrame& frame);
TelemetryFrame decode_telemetry(const std::uint8_t* data, std::size_t size);

// Wire quantization helpers. Finite values clamp instead of wrapping. Valid
// u16 quantities stop at 65534 so 0xFFFF remains the unknown sentinel.
std::uint8_t percent_to_u8(double fraction);
std::uint16_t units_to_u16(double value);
std::int16_t altitude_to_i16(double relative_m);

} // namespace comms
