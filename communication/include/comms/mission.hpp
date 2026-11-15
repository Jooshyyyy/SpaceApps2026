#pragma once

#include "comms/frame.hpp"

#include <functional>
#include <string>

namespace comms {

struct Point {
    int row = 0;
    int col = 0;
    bool operator==(const Point&) const = default;
};

// Owns mission/event state. The injected runner is the production layer 2
// adapter in the server and a deterministic fake in unit tests.
class Mission {
public:
    using Simulator = std::function<std::string(Point start, Point goal)>;

    Mission(Point refuge, Point selected_goal, Simulator simulator = {});
    void accept_simulation(std::string candidate);
    Point route_target() const;

    // Marks ROUTE_STALE before invoking the engine. On failure the last good
    // simulation remains available and ROUTE_STALE stays set.
    void recalculate();
    void set_position(Point point);
    void set_goal(Point point);
    void set_storm(bool active);
    void set_radiation_peak(bool active);
    void set_comms_lost(bool active); // event-only: no route recalculation

    Point position() const { return position_; }
    Point refuge() const { return refuge_; }
    Point selected_goal() const { return selected_goal_; }
    bool route_stale() const { return route_stale_; }
    const std::string& simulation_json() const { return simulation_json_; }
    double altitude_origin_m() const { return altitude_origin_m_; }

    TelemetryFrame telemetry(std::uint16_t seq, std::uint32_t t_ms, double dy_m,
                             double dx_m) const;

private:
    Point refuge_;
    Point selected_goal_;
    Point position_;
    Simulator simulator_;
    bool storm_ = false;
    bool radiation_peak_ = false;
    bool comms_lost_ = false;
    bool route_stale_ = false;
    bool has_simulation_ = false;
    std::string simulation_json_;
    double o2_fraction_ = 0.0;
    double batt_fraction_ = 0.0;
    double dose_msv_ = 0.0;
    double ttr_s_ = 0.0;
    std::uint8_t engine_flags_ = 0;
    double altitude_origin_m_ = 0.0;
    double current_elevation_m_ = 0.0;
    bool altitude_datum_set_ = false;
};

} // namespace comms
