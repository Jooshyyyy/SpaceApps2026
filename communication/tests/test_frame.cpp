#include "comms/frame.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>
#include <string>

namespace {

int checks = 0;
int failures = 0;

void check(bool condition, const char* expression, int line) {
    ++checks;
    if (!condition) {
        ++failures;
        std::cerr << "FAIL line " << line << ": " << expression << '\n';
    }
}

#define CHECK(expr) check((expr), #expr, __LINE__)

template <typename Function>
void throws_frame_error(Function function) {
    bool threw = false;
    try {
        function();
    } catch (const comms::FrameError&) {
        threw = true;
    }
    CHECK(threw);
}

void test_crc_known_answer() {
    const std::string input = "123456789";
    CHECK(comms::crc16_ccitt_false(reinterpret_cast<const std::uint8_t*>(input.data()), input.size()) ==
          0x29B1);
}

void test_golden_frame() {
    comms::TelemetryFrame frame;
    frame.seq = 0x1234;
    frame.t_ms = 0x01020304;
    frame.pos_x = 500;
    frame.pos_y = 1000;
    frame.alt_dm = -123;
    frame.o2 = 200;
    frame.batt = 150;
    frame.rad_cs = 1234;
    frame.ttr_s = 600;
    frame.flags = 0x4C;

    const std::array<std::uint8_t, 24> expected = {
        0x4D, 0x01, 0x34, 0x12, 0x04, 0x03, 0x02, 0x01,
        0xF4, 0x01, 0xE8, 0x03, 0x85, 0xFF, 0xC8, 0x96,
        0xD2, 0x04, 0x58, 0x02, 0x4C, 0x00, 0xFF, 0xC4};
    const auto encoded = comms::encode_telemetry(frame);
    CHECK(encoded == expected);
    CHECK(encoded.size() == comms::kTelemetryBytes);

    const auto decoded = comms::decode_telemetry(encoded.data(), encoded.size());
    CHECK(decoded.seq == frame.seq);
    CHECK(decoded.t_ms == frame.t_ms);
    CHECK(decoded.pos_x == frame.pos_x);
    CHECK(decoded.pos_y == frame.pos_y);
    CHECK(decoded.alt_dm == frame.alt_dm);
    CHECK(decoded.o2 == frame.o2);
    CHECK(decoded.batt == frame.batt);
    CHECK(decoded.rad_cs == frame.rad_cs);
    CHECK(decoded.ttr_s == frame.ttr_s);
    CHECK(decoded.flags == frame.flags);
    CHECK(decoded.reserved == 0);
}

void test_decoder_rejects_invalid_frames() {
    comms::TelemetryFrame frame;
    auto encoded = comms::encode_telemetry(frame);
    throws_frame_error([&] { comms::decode_telemetry(encoded.data(), 23); });

    auto invalid = encoded;
    invalid[0] = 'X';
    throws_frame_error([&] { comms::decode_telemetry(invalid.data(), invalid.size()); });

    invalid = encoded;
    invalid[1] = 2;
    throws_frame_error([&] { comms::decode_telemetry(invalid.data(), invalid.size()); });

    invalid = encoded;
    invalid[20] ^= 0x01;
    throws_frame_error([&] { comms::decode_telemetry(invalid.data(), invalid.size()); });

    invalid = encoded;
    invalid[21] = 1;
    invalid[22] = 0;
    invalid[23] = 0;
    throws_frame_error([&] { comms::decode_telemetry(invalid.data(), invalid.size()); });

    frame.o2 = 201;
    throws_frame_error([&] { comms::encode_telemetry(frame); });
    frame.o2 = 0xFF;
    frame.batt = 254;
    throws_frame_error([&] { comms::encode_telemetry(frame); });

    invalid = encoded;
    invalid[14] = 201;
    const auto invalid_percent_crc = comms::crc16_ccitt_false(invalid.data(), 22);
    invalid[22] = static_cast<std::uint8_t>(invalid_percent_crc);
    invalid[23] = static_cast<std::uint8_t>(invalid_percent_crc >> 8);
    throws_frame_error([&] { comms::decode_telemetry(invalid.data(), invalid.size()); });

    invalid = encoded;
    invalid[20] = 0x80;
    const std::uint16_t crc = comms::crc16_ccitt_false(invalid.data(), 22);
    invalid[22] = static_cast<std::uint8_t>(crc & 0xFF);
    invalid[23] = static_cast<std::uint8_t>(crc >> 8);
    throws_frame_error([&] { comms::decode_telemetry(invalid.data(), invalid.size()); });
}

void test_quantization_clamps_and_uses_sentinels() {
    const double nan = std::numeric_limits<double>::quiet_NaN();
    CHECK(comms::percent_to_u8(0.0) == 0);
    CHECK(comms::percent_to_u8(0.5) == 100);
    CHECK(comms::percent_to_u8(1.0) == 200);
    CHECK(comms::percent_to_u8(-0.1) == 0);
    CHECK(comms::percent_to_u8(1.1) == 200);
    CHECK(comms::percent_to_u8(nan) == 0xFF);

    CHECK(comms::units_to_u16(-1.0) == 0);
    CHECK(comms::units_to_u16(1.4) == 1);
    CHECK(comms::units_to_u16(1.5) == 2);
    CHECK(comms::units_to_u16(70000.0) == 65534);
    CHECK(comms::units_to_u16(nan) == 0xFFFF);
}

void test_altitude_is_relative_and_saturates() {
    const double nan = std::numeric_limits<double>::quiet_NaN();
    CHECK(comms::altitude_to_i16(-12.34) == -123);
    CHECK(comms::altitude_to_i16(4000.0) == 32767);
    CHECK(comms::altitude_to_i16(-4000.0) == -32768);
    throws_frame_error([&] { comms::altitude_to_i16(nan); });
}

void test_sequence_and_timestamp_full_widths() {
    comms::TelemetryFrame frame;
    frame.seq = 65535;
    frame.t_ms = 0xFFFFFFFFu;
    const auto encoded = comms::encode_telemetry(frame);
    const auto decoded = comms::decode_telemetry(encoded.data(), encoded.size());
    CHECK(decoded.seq == 65535);
    CHECK(decoded.t_ms == 0xFFFFFFFFu);
}

} // namespace

int main() {
    test_crc_known_answer();
    test_golden_frame();
    test_decoder_rejects_invalid_frames();
    test_quantization_clamps_and_uses_sentinels();
    test_altitude_is_relative_and_saturates();
    test_sequence_and_timestamp_full_widths();
    std::cout << checks - failures << '/' << checks << " checks passed\n";
    return failures == 0 ? 0 : 1;
}
