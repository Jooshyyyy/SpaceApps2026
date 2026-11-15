#include "comms/frame.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace comms {
namespace {

void put_u16(std::uint8_t* output, std::uint16_t value) {
    output[0] = static_cast<std::uint8_t>(value & 0xFFu);
    output[1] = static_cast<std::uint8_t>(value >> 8);
}

void put_u32(std::uint8_t* output, std::uint32_t value) {
    output[0] = static_cast<std::uint8_t>(value & 0xFFu);
    output[1] = static_cast<std::uint8_t>((value >> 8) & 0xFFu);
    output[2] = static_cast<std::uint8_t>((value >> 16) & 0xFFu);
    output[3] = static_cast<std::uint8_t>(value >> 24);
}

std::uint16_t get_u16(const std::uint8_t* input) {
    return static_cast<std::uint16_t>(input[0]) |
           static_cast<std::uint16_t>(static_cast<std::uint16_t>(input[1]) << 8);
}

std::uint32_t get_u32(const std::uint8_t* input) {
    return static_cast<std::uint32_t>(input[0]) |
           (static_cast<std::uint32_t>(input[1]) << 8) |
           (static_cast<std::uint32_t>(input[2]) << 16) |
           (static_cast<std::uint32_t>(input[3]) << 24);
}

} // namespace

std::uint16_t crc16_ccitt_false(const std::uint8_t* data, std::size_t size) {
    std::uint16_t crc = 0xFFFFu;
    for (std::size_t i = 0; i < size; ++i) {
        crc ^= static_cast<std::uint16_t>(data[i]) << 8;
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc & 0x8000u) != 0
                      ? static_cast<std::uint16_t>((crc << 1) ^ 0x1021u)
                      : static_cast<std::uint16_t>(crc << 1);
        }
    }
    return crc;
}

std::array<std::uint8_t, kTelemetryBytes> encode_telemetry(const TelemetryFrame& frame) {
    if ((frame.flags & 0x80u) != 0 || frame.reserved != 0) {
        throw FrameError("reserved telemetry bits must be zero");
    }

    if ((frame.o2 > 200 && frame.o2 != 0xFF) ||
        (frame.batt > 200 && frame.batt != 0xFF)) {
        throw FrameError("oxygen and battery must be 0..200 or unknown");
    }
    std::array<std::uint8_t, kTelemetryBytes> out{};
    out[0] = kFrameMagic;
    out[1] = kFrameVersion;
    put_u16(out.data() + 2, frame.seq);
    put_u32(out.data() + 4, frame.t_ms);
    put_u16(out.data() + 8, frame.pos_x);
    put_u16(out.data() + 10, frame.pos_y);
    put_u16(out.data() + 12, static_cast<std::uint16_t>(frame.alt_dm));
    out[14] = frame.o2;
    out[15] = frame.batt;
    put_u16(out.data() + 16, frame.rad_cs);
    put_u16(out.data() + 18, frame.ttr_s);
    out[20] = frame.flags;
    out[21] = frame.reserved;
    put_u16(out.data() + 22, crc16_ccitt_false(out.data(), 22));
    return out;
}

TelemetryFrame decode_telemetry(const std::uint8_t* data, std::size_t size) {
    if (data == nullptr || size != kTelemetryBytes) {
        throw FrameError("telemetry frame must be exactly 24 bytes");
    }
    if (data[0] != kFrameMagic) throw FrameError("invalid telemetry magic");
    if (data[1] != kFrameVersion) throw FrameError("unsupported telemetry version");
    const std::uint16_t expected = crc16_ccitt_false(data, 22);
    const std::uint16_t actual = get_u16(data + 22);
    if (actual != expected) throw FrameError("telemetry CRC mismatch");
    if (data[21] != 0 || (data[20] & 0x80u) != 0) {
        throw FrameError("reserved telemetry bits must be zero");
    }

    if ((data[14] > 200 && data[14] != 0xFF) ||
        (data[15] > 200 && data[15] != 0xFF)) {
        throw FrameError("oxygen and battery must be 0..200 or unknown");
    }
    TelemetryFrame frame;
    frame.seq = get_u16(data + 2);
    frame.t_ms = get_u32(data + 4);
    frame.pos_x = get_u16(data + 8);
    frame.pos_y = get_u16(data + 10);
    frame.alt_dm = static_cast<std::int16_t>(get_u16(data + 12));
    frame.o2 = data[14];
    frame.batt = data[15];
    frame.rad_cs = get_u16(data + 16);
    frame.ttr_s = get_u16(data + 18);
    frame.flags = data[20];
    frame.reserved = data[21];
    return frame;
}

std::uint8_t percent_to_u8(double fraction) {
    if (!std::isfinite(fraction)) return 0xFF;
    const double clamped = std::clamp(fraction, 0.0, 1.0);
    return static_cast<std::uint8_t>(std::lround(clamped * 200.0));
}

std::uint16_t units_to_u16(double value) {
    if (!std::isfinite(value)) return 0xFFFF;
    const double clamped = std::clamp(value, 0.0, 65534.0);
    return static_cast<std::uint16_t>(std::lround(clamped));
}

std::int16_t altitude_to_i16(double relative_m) {
    if (!std::isfinite(relative_m)) throw FrameError("relative altitude must be finite");
    const double dm = std::clamp(relative_m * 10.0, -32768.0, 32767.0);
    return static_cast<std::int16_t>(std::lround(dm));
}

} // namespace comms
