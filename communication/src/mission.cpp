#include "comms/mission.hpp"

#include "engine/json.hpp"

#include <cmath>
#include <algorithm>
#include <cstddef>
#include <optional>
#include <stdexcept>
#include <utility>

namespace comms {

Mission::Mission(Point refuge, Point selected_goal, Simulator simulator,
                 double o2_capacity_kg, double battery_capacity_wh)
    : refuge_(refuge), selected_goal_(selected_goal), position_(refuge), simulator_(std::move(simulator)),
      initial_o2_kg_(o2_capacity_kg), initial_battery_wh_(battery_capacity_wh),
      o2_remaining_kg_(o2_capacity_kg), battery_remaining_wh_(battery_capacity_wh) {
    if (!std::isfinite(initial_o2_kg_) || initial_o2_kg_ <= 0.0 ||
        !std::isfinite(initial_battery_wh_) || initial_battery_wh_ <= 0.0) {
        throw std::invalid_argument("PLSS capacities must be finite and positive");
    }
    // No injected runner selects asynchronous reactor mode.
}

Point Mission::route_target() const {
    return (storm_ || radiation_peak_) ? refuge_ : selected_goal_;
}

void Mission::recalculate() {
    route_stale_ = true;
    if (!simulator_) return; // server completes through accept_simulation()
    accept_simulation(simulator_(position_, route_target()));
}

void Mission::accept_simulation(std::string candidate) {
    route_stale_ = true;
    try {
        const eva::Json result = eva::Json::parse(candidate);
        if (!result.at("ok").boolean()) throw std::runtime_error("engine returned ok=false");

        // Commit the candidate only after the complete document has parsed and
        // all fields needed by telemetry have been read successfully.
        const double projected_o2 = result.at("o2_remaining_kg").number();
        const double projected_batt = result.at("batt_remaining_wh").number();
        const double dose = result.at("dose_total_msv").number();
        const double ttr = result.at("ttr_s").number();
        const double raw_flags = result.at("flags").number();
        if (!std::isfinite(raw_flags) || raw_flags < 0 || raw_flags > 127 ||
            std::floor(raw_flags) != raw_flags) throw std::runtime_error("invalid engine flags");
        const auto flags = static_cast<std::uint8_t>(raw_flags);
        if (!std::isfinite(projected_o2) || !std::isfinite(projected_batt) || !std::isfinite(dose) ||
            !std::isfinite(ttr)) throw std::runtime_error("invalid engine telemetry");
        const auto& path = result.at("outbound").at("route").at("path").items();
        std::optional<double> elevation;
        if (!path.empty()) {
            elevation = path.front().at("elevation_m").number();
            if (!std::isfinite(*elevation)) throw std::runtime_error("invalid engine elevation");
        }

        simulation_json_ = std::move(candidate);
        // The engine receives the resources currently remaining. Percentages
        // on the wire remain relative to the capacities at EVA start.
        o2_fraction_ = std::clamp(projected_o2 / initial_o2_kg_, 0.0, 1.0);
        batt_fraction_ = std::clamp(projected_batt / initial_battery_wh_, 0.0, 1.0);
        dose_msv_ = cumulative_dose_msv_ + std::max(0.0, dose);
        ttr_s_ = ttr;
        engine_flags_ = static_cast<std::uint8_t>(flags & 0x7Fu);
        // Layer 2 evaluates thresholds against the capacity it receives
        // (the amount currently remaining). Layer 3 owns the EVA baseline,
        // so it restores low-resource alerts relative to the initial tanks.
        if (o2_fraction_ < 0.25) engine_flags_ |= 1u << 2;
        if (batt_fraction_ < 0.20) engine_flags_ |= 1u << 3;
        if (projected_o2 <= 0.0 || projected_batt <= 0.0) engine_flags_ |= 1u << 6;
        has_simulation_ = true;
        if (elevation.has_value()) {
            if (!altitude_datum_set_) {
                altitude_origin_m_ = *elevation;
                altitude_datum_set_ = true;
            }
            current_elevation_m_ = *elevation;
        }
        route_stale_ = false;
    } catch (...) {
        route_stale_ = true;
        throw;
    }
}

void Mission::accept_movement(const std::string& candidate) {
    const eva::Json result = eva::Json::parse(candidate);
    if (!result.at("ok").boolean() || !result.at("found").boolean()) {
        throw std::runtime_error("movement route is unavailable");
    }
    const auto& leg = result.at("outbound").at("sim");
    const double o2_used = leg.at("o2_kg").number();
    const double battery_used = leg.at("batt_wh").number();
    const double dose = leg.at("dose_msv").number();
    if (!std::isfinite(o2_used) || !std::isfinite(battery_used) || !std::isfinite(dose) ||
        o2_used < 0.0 || battery_used < 0.0 || dose < 0.0) {
        throw std::runtime_error("invalid movement consumption");
    }
    o2_remaining_kg_ = std::max(0.0, o2_remaining_kg_ - o2_used);
    battery_remaining_wh_ = std::max(0.0, battery_remaining_wh_ - battery_used);
    cumulative_dose_msv_ += dose;
}

void Mission::set_position(Point point) {
    position_ = point;
    recalculate();
}

void Mission::set_goal(Point point) {
    selected_goal_ = point;
    recalculate();
}

void Mission::set_storm(bool active) {
    if (storm_ == active) return;
    storm_ = active;
    recalculate();
}

void Mission::set_radiation_peak(bool active) {
    if (radiation_peak_ == active) return;
    radiation_peak_ = active;
    recalculate();
}

void Mission::set_comms_lost(bool active) { comms_lost_ = active; }

TelemetryFrame Mission::telemetry(std::uint16_t seq, std::uint32_t t_ms, double dy_m,
                                  double dx_m) const {
    TelemetryFrame frame;
    frame.seq = seq;
    frame.t_ms = t_ms;
    frame.pos_x = units_to_u16(static_cast<double>(position_.col) * dx_m);
    frame.pos_y = units_to_u16(static_cast<double>(position_.row) * dy_m);
    frame.alt_dm = altitude_to_i16(current_elevation_m_ - altitude_origin_m_);
    if (has_simulation_) {
        frame.o2 = percent_to_u8(o2_fraction_);
        frame.batt = percent_to_u8(batt_fraction_);
        frame.rad_cs = units_to_u16(dose_msv_ * 100.0);
        frame.ttr_s = units_to_u16(ttr_s_);
    }
    frame.flags = engine_flags_;
    if (storm_) frame.flags |= 1u << 0;
    if (radiation_peak_) frame.flags |= 1u << 1;
    if (comms_lost_) frame.flags |= 1u << 4;
    if (route_stale_) frame.flags |= 1u << 5;
    return frame;
}

} // namespace comms
