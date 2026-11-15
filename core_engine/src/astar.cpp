#include "engine/astar.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <limits>
#include <queue>
#include <vector>

namespace eva {
namespace {

constexpr double kInfinity = std::numeric_limits<double>::infinity();

struct OpenNode {
    double f = 0.0;
    int cell = -1;
};

struct OpenGreater {
    bool operator()(const OpenNode& a, const OpenNode& b) const { return a.f > b.f; }
};

double elapsed_ms(std::chrono::steady_clock::time_point began) {
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - began).count();
}

// Admissible because every traversable move costs at least its horizontal
// distance: the terrain factor is >= 1 and the climb term is never negative.
double heuristic(const Grid& grid, int row, int col, int goal_row, int goal_col) {
    const double dr = static_cast<double>(goal_row - row) * grid.meta.dy_m;
    const double dc = static_cast<double>(goal_col - col) * grid.meta.dx_m;
    return std::sqrt(dr * dr + dc * dc);
}

// Returns a negative value when the move is not allowed.
double step_cost(const Grid& grid, int row, int col, int next_row, int next_col,
                 const RouteParams& params) {
    const std::size_t next = grid.index(next_row, next_col);
    const std::uint8_t cost = grid.cost[next];
    if (cost == 0 || cost == 255) return -1.0;

    const float here_z = grid.elevation[grid.index(row, col)];
    const float next_z = grid.elevation[next];
    if (!std::isfinite(here_z) || !std::isfinite(next_z)) return -1.0;

    const double dr = static_cast<double>(next_row - row) * grid.meta.dy_m;
    const double dc = static_cast<double>(next_col - col) * grid.meta.dx_m;
    const double distance = std::sqrt(dr * dr + dc * dc);

    const double terrain = 1.0 + params.slope_weight * (static_cast<double>(cost) - 1.0) / 254.0;
    double radiation = 0.0;
    if (grid.has_radiation() && params.radiation_weight > 0.0) {
        const float exposure = grid.radiation[next];
        if (std::isfinite(exposure) && exposure > 0.0f) {
            radiation = params.radiation_weight * static_cast<double>(exposure);
        }
    }
    double soil_penalty = 0.0;
    if (grid.has_thermal_inertia() && params.soil_weight > 0.0) {
        const float ti = grid.thermal_inertia[next];
        if (std::isfinite(ti) && ti > 0.0f) {
            // Duricrust/bedrock (TI >= 300 SI) provides firm footing (0 penalty).
            // Loose dust (TI < 300 SI) incurs slip and sinkage penalty.
            if (ti < 300.0f) {
                soil_penalty = params.soil_weight * static_cast<double>((300.0f - ti) / 200.0f);
            }
        }
    }
    const double dz = static_cast<double>(next_z) - static_cast<double>(here_z);
    const double climb = dz > 0.0 ? dz * params.climb_weight : 0.0;
    return distance * (terrain + radiation + soil_penalty) + climb;
}

} // namespace

RouteParams default_route_params() { return RouteParams{}; }

Route find_route(const Grid& grid, int start_row, int start_col, int goal_row, int goal_col,
                 const RouteParams& params) {
    const auto began = std::chrono::steady_clock::now();
    Route route;

    if (!grid.in_bounds(start_row, start_col) || !grid.in_bounds(goal_row, goal_col)) return route;
    if (!grid.traversable(start_row, start_col) || !grid.traversable(goal_row, goal_col)) return route;

    const int cols = grid.cols();
    const int start = static_cast<int>(grid.index(start_row, start_col));
    const int goal = static_cast<int>(grid.index(goal_row, goal_col));
    if (start == goal) {
        route.found = true;
        route.cells.push_back(start);
        route.elapsed_ms = elapsed_ms(began);
        return route;
    }

    const std::size_t cells = grid.cells();
    std::vector<double> g(cells, kInfinity);
    std::vector<int> parent(cells, -1);
    std::vector<char> closed(cells, 0);
    std::priority_queue<OpenNode, std::vector<OpenNode>, OpenGreater> open;

    g[static_cast<std::size_t>(start)] = 0.0;
    open.push({heuristic(grid, start_row, start_col, goal_row, goal_col), start});

    static const int kDr[8] = {-1, -1, -1, 0, 0, 1, 1, 1};
    static const int kDc[8] = {-1, 0, 1, -1, 1, -1, 0, 1};

    while (!open.empty()) {
        const OpenNode node = open.top();
        open.pop();
        const std::size_t here = static_cast<std::size_t>(node.cell);
        if (closed[here]) continue;
        closed[here] = 1;
        ++route.expanded;
        if (node.cell == goal) break;

        const int row = node.cell / cols;
        const int col = node.cell % cols;
        for (int k = 0; k < 8; ++k) {
            const int next_row = row + kDr[k];
            const int next_col = col + kDc[k];
            if (!grid.in_bounds(next_row, next_col)) continue;
            const bool diagonal = kDr[k] != 0 && kDc[k] != 0;
            if (diagonal && !params.allow_diagonal) continue;
            if (diagonal && params.no_corner_cutting) {
                if (!grid.traversable(row, next_col) || !grid.traversable(next_row, col)) continue;
            }
            const double step = step_cost(grid, row, col, next_row, next_col, params);
            if (step < 0.0) continue;
            const std::size_t next = grid.index(next_row, next_col);
            if (closed[next]) continue;
            const double candidate = g[here] + step;
            if (candidate >= g[next]) continue;
            g[next] = candidate;
            parent[next] = node.cell;
            open.push({candidate + heuristic(grid, next_row, next_col, goal_row, goal_col),
                       static_cast<int>(next)});
        }
    }

    if (!closed[static_cast<std::size_t>(goal)]) {
        route.elapsed_ms = elapsed_ms(began);
        return route;
    }

    route.found = true;
    route.cost = g[static_cast<std::size_t>(goal)];
    for (int cell = goal; cell != -1; cell = parent[static_cast<std::size_t>(cell)]) {
        route.cells.push_back(cell);
    }
    std::reverse(route.cells.begin(), route.cells.end());

    for (std::size_t i = 1; i < route.cells.size(); ++i) {
        const int a = route.cells[i - 1];
        const int b = route.cells[i];
        const int ra = a / cols;
        const int ca = a % cols;
        const int rb = b / cols;
        const int cb = b % cols;
        const double dr = static_cast<double>(rb - ra) * grid.meta.dy_m;
        const double dc = static_cast<double>(cb - ca) * grid.meta.dx_m;
        route.length_m += std::sqrt(dr * dr + dc * dc);
        const double dz =
            static_cast<double>(grid.elevation[static_cast<std::size_t>(b)]) -
            static_cast<double>(grid.elevation[static_cast<std::size_t>(a)]);
        if (dz > 0.0) {
            route.climb_m += dz;
        } else {
            route.descent_m -= dz;
        }
    }

    double rad_sum = 0.0;
    double ti_sum = 0.0;
    int ti_count = 0;
    for (int cell : route.cells) {
        if (grid.has_radiation()) {
            const float r = grid.radiation[static_cast<std::size_t>(cell)];
            if (std::isfinite(r)) rad_sum += static_cast<double>(r);
        }
        if (grid.has_thermal_inertia()) {
            const float t = grid.thermal_inertia[static_cast<std::size_t>(cell)];
            if (std::isfinite(t)) {
                ti_sum += static_cast<double>(t);
                ++ti_count;
            }
        }
    }
    route.total_radiation = rad_sum;
    route.mean_thermal_inertia = ti_count > 0 ? (ti_sum / ti_count) : 0.0;
    route.elapsed_ms = elapsed_ms(began);
    return route;
}

} // namespace eva
