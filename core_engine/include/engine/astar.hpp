// Multicriteria A* over the layer 1 cost grid.
//
// The three criteria are horizontal distance, terrain penalty (slope, already
// encoded in cost.bin) and radiation exposure when layer 1 supplies a radiation
// grid. Uphill metres cost extra so the search prefers contouring over climbing.
#pragma once

#include "engine/grid.hpp"

#include <cstddef>
#include <vector>

namespace eva {

struct RouteParams {
    double slope_weight = 4.0;      // terrain penalty scale at cost 254
    double climb_weight = 5.0;      // cost per metre climbed
    double radiation_weight = 0.0;  // multiplier per unit of radiation exposure
    bool allow_diagonal = true;
    bool no_corner_cutting = true;  // never slip diagonally between two walls
};

RouteParams default_route_params();

struct Route {
    bool found = false;
    std::vector<int> cells;   // row-major cell indices, start -> goal
    double cost = 0.0;        // accumulated g
    double length_m = 0.0;    // horizontal length
    double climb_m = 0.0;     // total ascent
    double descent_m = 0.0;   // total descent
    std::size_t expanded = 0;
    double elapsed_ms = 0.0;
};

// Returns found == false when the goal is unreachable, out of bounds, or sits on
// a wall. A start cell equal to the goal cell yields a one-cell route.
Route find_route(const Grid& grid, int start_row, int start_col, int goal_row, int goal_col,
                 const RouteParams& params);

} // namespace eva
