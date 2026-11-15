#include "comms/mission.hpp"

#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

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

std::string simulation(double elevation, std::uint8_t flags = 0, bool found = true) {
    const std::string path = found
        ? "[{\"row\":0,\"col\":0,\"elevation_m\":" + std::to_string(elevation) + "}]"
        : "[]";
    return std::string("{\"ok\":true,\"found\":") + (found ? "true" : "false") +
           ",\"outbound\":{\"route\":{\"path\":" + path + "}}" +
           ",\"ttr_s\":12.4,\"o2_fraction\":0.5,\"batt_fraction\":0.25," +
           "\"dose_total_msv\":0.12,\"flags\":" + std::to_string(flags) + "}";
}

struct Fixture {
    std::vector<std::pair<comms::Point, comms::Point>> calls;
    bool fail = false;

    comms::Mission make() {
        return comms::Mission({10, 20}, {80, 90}, [this](comms::Point start, comms::Point goal) {
            calls.emplace_back(start, goal);
            if (fail) throw std::runtime_error("engine unavailable");
            return simulation(-100.0 + (start.row == 20 ? 0.5 : 0.0));
        });
    }
};

void test_initial_route_and_telemetry() {
    Fixture fixture;
    auto mission = fixture.make();
    mission.recalculate();
    CHECK(fixture.calls.size() == 1);
    CHECK(fixture.calls[0].first.row == 10);
    CHECK(fixture.calls[0].second.row == 80);
    CHECK(!mission.route_stale());

    const auto frame = mission.telemetry(7, 1000, 1.0, 2.0);
    CHECK(frame.seq == 7);
    CHECK(frame.t_ms == 1000);
    CHECK(frame.pos_x == 40); // col 20 at 2 m/cell
    CHECK(frame.pos_y == 10);
    CHECK(frame.alt_dm == 0);
    CHECK(frame.o2 == 100);
    CHECK(frame.batt == 50);
    CHECK(frame.rad_cs == 12);
    CHECK(frame.ttr_s == 12);
    CHECK(frame.flags == 0);
}

void test_position_recalculates_and_updates_relative_altitude() {
    Fixture fixture;
    auto mission = fixture.make();
    mission.recalculate();
    mission.set_position({20, 30});
    CHECK(fixture.calls.back().first.row == 20);
    CHECK(fixture.calls.back().second.col == 90);
    CHECK(mission.position().row == 20);
    CHECK(mission.telemetry(0, 0, 1.0, 1.0).alt_dm == 5);
}

void test_emergencies_route_to_refuge_and_restore_goal() {
    Fixture fixture;
    auto mission = fixture.make();
    mission.recalculate();
    mission.set_storm(true);
    CHECK(fixture.calls.back().first.row == 10);
    CHECK(fixture.calls.back().second.row == 10);
    CHECK(fixture.calls.back().second.col == 20);
    CHECK((mission.telemetry(0, 0, 1.0, 1.0).flags & (1u << 0)) != 0);
    CHECK((mission.telemetry(0, 0, 1.0, 1.0).flags & (1u << 5)) == 0);

    mission.set_radiation_peak(true);
    mission.set_storm(false);
    CHECK(fixture.calls.back().second.row == 10); // radiation emergency remains
    mission.set_radiation_peak(false);
    CHECK(fixture.calls.back().second.row == 80); // selected goal restored
    CHECK((mission.telemetry(0, 0, 1.0, 1.0).flags & 0x03) == 0);
}

void test_comms_event_does_not_recalculate() {
    Fixture fixture;
    auto mission = fixture.make();
    mission.recalculate();
    mission.set_comms_lost(true);
    CHECK(fixture.calls.size() == 1);
    CHECK((mission.telemetry(0, 0, 1.0, 1.0).flags & (1u << 4)) != 0);
}

void test_failed_recalculation_stays_stale_and_preserves_last_sim() {
    Fixture fixture;
    auto mission = fixture.make();
    mission.recalculate();
    const std::string previous = mission.simulation_json();
    fixture.fail = true;
    bool threw = false;
    try {
        mission.set_goal({70, 60});
    } catch (const std::runtime_error&) {
        threw = true;
    }
    CHECK(threw);
    CHECK(mission.route_stale());
    CHECK(mission.simulation_json() == previous);
    CHECK((mission.telemetry(0, 0, 1.0, 1.0).flags & (1u << 5)) != 0);
}

void test_engine_alerts_are_preserved() {
    comms::Mission mission({0, 0}, {1, 1}, [](comms::Point, comms::Point) {
        return simulation(-50.0, 0x4C, false);
    });
    mission.recalculate();
    CHECK((mission.telemetry(0, 0, 1.0, 1.0).flags & 0x4C) == 0x4C);
}

} // namespace

int main() {
    test_initial_route_and_telemetry();
    test_position_recalculates_and_updates_relative_altitude();
    test_emergencies_route_to_refuge_and_restore_goal();
    test_comms_event_does_not_recalculate();
    test_failed_recalculation_stays_stale_and_preserves_last_sim();
    test_engine_alerts_are_preserved();
    std::cout << checks - failures << '/' << checks << " checks passed\n";
    return failures == 0 ? 0 : 1;
}
