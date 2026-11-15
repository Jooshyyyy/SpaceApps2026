#include "engine/plss.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>

namespace eva {
namespace {

double clamp01(double value) { return std::clamp(value, 0.0, 1.0); }

double slope_fraction(const Grid& grid, int cell) {
    if (grid.meta.max_slope_deg <= 0.0) return 0.0;
    const float slope = grid.slope[static_cast<std::size_t>(cell)];
    if (!std::isfinite(slope)) return 0.0;
    return clamp01(static_cast<double>(slope) / grid.meta.max_slope_deg);
}

double radiation_factor(const Grid& grid, int cell) {
    if (!grid.has_radiation()) return 1.0;
    const float exposure = grid.radiation[static_cast<std::size_t>(cell)];
    if (!std::isfinite(exposure) || exposure <= 0.0f) return 1.0;
    return 1.0 + static_cast<double>(exposure);
}

} // namespace

LegSim simulate_leg(const Grid& grid, const Route& route, const PlssConfig& config) {
    LegSim leg;
    if (!route.found || route.cells.size() < 2) return leg;

    const int cols = grid.cols();
    for (std::size_t i = 1; i < route.cells.size(); ++i) {
        const int a = route.cells[i - 1];
        const int b = route.cells[i];
        const int ra = a / cols;
        const int ca = a % cols;
        const int rb = b / cols;
        const int cb = b % cols;
        const double dr = static_cast<double>(rb - ra) * grid.meta.dy_m;
        const double dc = static_cast<double>(cb - ca) * grid.meta.dx_m;
        const double distance = std::sqrt(dr * dr + dc * dc);

        const double fraction = slope_fraction(grid, b);
        double soil_drag = 0.0;
        if (grid.has_thermal_inertia()) {
            const float ti = grid.thermal_inertia[static_cast<std::size_t>(b)];
            if (std::isfinite(ti) && ti < 300.0f && ti > 0.0f) {
                soil_drag = static_cast<double>((300.0f - ti) / 300.0f);
            }
        }
        const double soil_speed_mult = 1.0 / (1.0 + 0.20 * soil_drag);
        const double speed = (config.walk_speed_mps -
                             (config.walk_speed_mps - config.walk_speed_slope_mps) * fraction) * soil_speed_mult;
        if (speed <= 0.0) continue; // a zero speed would divide by zero
        const double dt = distance / speed;

        leg.time_s += dt;
        leg.o2_kg += (config.o2_rate_kg_per_h + config.o2_rate_slope_kg_per_h * fraction) *
                     (1.0 + 0.15 * soil_drag) * dt / 3600.0;
        leg.batt_wh +=
            (config.battery_draw_w + config.battery_draw_slope_w * fraction) *
            (1.0 + 0.15 * soil_drag) * dt / 3600.0;
        leg.dose_msv += config.dose_rate_msv_per_h * radiation_factor(grid, b) * dt / 3600.0;
        leg.distance_m += distance;

        const double dz = static_cast<double>(grid.elevation[static_cast<std::size_t>(b)]) -
                          static_cast<double>(grid.elevation[static_cast<std::size_t>(a)]);
        if (dz > 0.0) leg.climb_m += dz;
    }
    return leg;
}

EvaSim simulate_round_trip(const Grid& grid, const Route& outbound, const Route& inbound,
                           const PlssConfig& config) {
    EvaSim sim;
    sim.outbound = simulate_leg(grid, outbound, config);
    sim.inbound = simulate_leg(grid, inbound, config);
    sim.ttr_s = sim.inbound.time_s;

    sim.o2_remaining_kg = config.o2_capacity_kg - sim.outbound.o2_kg;
    sim.batt_remaining_wh = config.battery_capacity_wh - sim.outbound.batt_wh;
    sim.dose_total_msv = sim.outbound.dose_msv + sim.inbound.dose_msv;
    sim.o2_fraction = config.o2_capacity_kg > 0.0 ? sim.o2_remaining_kg / config.o2_capacity_kg : 0.0;
    sim.batt_fraction =
        config.battery_capacity_wh > 0.0 ? sim.batt_remaining_wh / config.battery_capacity_wh : 0.0;
    const bool routes_found = outbound.found && inbound.found;
    sim.return_feasible = routes_found && sim.o2_remaining_kg >= sim.inbound.o2_kg &&
                          sim.batt_remaining_wh >= sim.inbound.batt_wh;

    if (sim.o2_fraction < config.o2_low_fraction) {
        sim.flags |= ALERT_O2_LOW;
        sim.reasons.emplace_back("oxygen below the low threshold");
    }
    if (sim.batt_fraction < config.batt_low_fraction) {
        sim.flags |= ALERT_BATT_LOW;
        sim.reasons.emplace_back("battery below the low threshold");
    }
    if (sim.dose_total_msv > config.dose_peak_msv) {
        sim.flags |= ALERT_RAD_PEAK;
        sim.reasons.emplace_back("cumulative dose above the peak threshold");
    }
    if (!routes_found) {
        sim.flags |= ALERT_EVA_ABORT;
        sim.reasons.emplace_back("outbound or return route is unavailable");
    } else if (!sim.return_feasible) {
        sim.flags |= ALERT_EVA_ABORT;
        sim.reasons.emplace_back("return leg exceeds the remaining consumables");
    }
    return sim;
}

const char* alert_name(std::uint8_t flag) {
    switch (flag) {
        case ALERT_STORM: return "STORM";
        case ALERT_RAD_PEAK: return "RAD_PEAK";
        case ALERT_O2_LOW: return "O2_LOW";
        case ALERT_BATT_LOW: return "BATT_LOW";
        case ALERT_COMMS_LOST: return "COMMS_LOST";
        case ALERT_ROUTE_STALE: return "ROUTE_STALE";
        case ALERT_EVA_ABORT: return "EVA_ABORT";
        default: return "UNKNOWN";
    }
}

} // namespace eva
