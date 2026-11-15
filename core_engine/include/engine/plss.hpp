// PLSS life-support simulator.
//
// PLSS is the Portable Life Support System: the EVA suit's oxygen, power and
// radiation bookkeeping. The engine simulates a planned route against it and
// produces the survival numbers the HUD renders, plus the alert flags. The
// engine is the single source of truth for alerts; clients only render them.
#pragma once

#include "engine/astar.hpp"
#include "engine/grid.hpp"

#include <cstdint>
#include <string>
#include <vector>

namespace eva {

// Alert bits are frozen by the frame contract (docs/telemetry-frame.md):
//   bit0 STORM, bit1 RAD_PEAK, bit2 O2_LOW, bit3 BATT_LOW,
//   bit4 COMMS_LOST, bit5 ROUTE_STALE, bit6 EVA_ABORT, bit7 reserved.
enum AlertFlag : std::uint8_t {
    ALERT_STORM = 1u << 0,
    ALERT_RAD_PEAK = 1u << 1,
    ALERT_O2_LOW = 1u << 2,
    ALERT_BATT_LOW = 1u << 3,
    ALERT_COMMS_LOST = 1u << 4,
    ALERT_ROUTE_STALE = 1u << 5,
    ALERT_EVA_ABORT = 1u << 6,
};

// Plausible EVA suit figures, documented as assumptions rather than presented as
// measured hardware data. They are one struct so a judge or a teammate can tune
// them without touching the search.
struct PlssConfig {
    double o2_capacity_kg = 1.20;
    double o2_rate_kg_per_h = 0.55;        // flat-ground baseline
    double o2_rate_slope_kg_per_h = 0.35;  // extra at the exclusion slope
    double battery_capacity_wh = 250.0;
    double battery_draw_w = 130.0;
    double battery_draw_slope_w = 70.0;
    double dose_rate_msv_per_h = 0.03;     // Mars surface baseline
    double walk_speed_mps = 1.00;          // flat ground
    double walk_speed_slope_mps = 0.45;    // at the exclusion slope
    double o2_low_fraction = 0.25;
    double batt_low_fraction = 0.20;
    double dose_peak_msv = 0.30;
};

struct LegSim {
    double time_s = 0.0;
    double o2_kg = 0.0;
    double batt_wh = 0.0;
    double dose_msv = 0.0;
    double distance_m = 0.0;
    double climb_m = 0.0;
};

struct EvaSim {
    LegSim outbound;
    LegSim inbound;
    double ttr_s = 0.0;               // time to walk back to the refuge
    double o2_remaining_kg = 0.0;
    double batt_remaining_wh = 0.0;
    double dose_total_msv = 0.0;
    double o2_fraction = 0.0;
    double batt_fraction = 0.0;
    bool return_feasible = false;
    std::uint8_t flags = 0;
    std::vector<std::string> reasons;
};

// Simulates one leg. An unfound route yields an empty leg.
LegSim simulate_leg(const Grid& grid, const Route& route, const PlssConfig& config);

// Outbound to the goal, then the return leg to the refuge. Return feasibility
// compares the inbound consumption with what the outbound leg left in the tanks.
EvaSim simulate_round_trip(const Grid& grid, const Route& outbound, const Route& inbound,
                           const PlssConfig& config);

// Human-readable name for one alert bit, for CLI output and logs.
const char* alert_name(std::uint8_t flag);

} // namespace eva
