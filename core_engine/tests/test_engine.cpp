// Tests for layer 2 (core engine).
//
// No third-party test framework: gtest is not installed and the build is a
// plain Makefile. A tiny CHECK harness keeps the suite dependency-free, matching
// the layer 1 Python suite that also runs on the standard library only.
#include "engine/astar.hpp"
#include "engine/grid.hpp"
#include "engine/json.hpp"
#include "engine/plss.hpp"

#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include <unistd.h>

namespace {

int g_checks = 0;
int g_failures = 0;

void check(bool condition, const char* expression, const char* file, int line) {
    ++g_checks;
    if (!condition) {
        ++g_failures;
        std::cerr << "FAIL " << file << ":" << line << "  " << expression << "\n";
    }
}

void check_near(double actual, double expected, double epsilon, const char* expression,
                const char* file, int line) {
    ++g_checks;
    if (!(std::fabs(actual - expected) <= epsilon)) {
        ++g_failures;
        std::cerr << "FAIL " << file << ":" << line << "  " << expression << "  got " << actual
                  << " expected " << expected << " (eps " << epsilon << ")\n";
    }
}

#define CHECK(expr) check((expr), #expr, __FILE__, __LINE__)
#define CHECK_NEAR(actual, expected, eps) \
    check_near((actual), (expected), (eps), #actual " ~= " #expected, __FILE__, __LINE__)

// --- fixtures ---------------------------------------------------------------

eva::Grid make_grid(int rows, int cols) {
    eva::Grid grid;
    grid.meta.format = "mars-eva-aoi/1";
    grid.meta.name = "test";
    grid.meta.rows = rows;
    grid.meta.cols = cols;
    grid.meta.dy_m = 1.0;
    grid.meta.dx_m = 1.0;
    grid.meta.max_slope_deg = 20.0;
    grid.meta.nodata = -3.402823e38f;
    grid.elevation.assign(static_cast<std::size_t>(rows) * cols, 0.0f);
    grid.slope.assign(static_cast<std::size_t>(rows) * cols, 0.0f);
    grid.cost.assign(static_cast<std::size_t>(rows) * cols, 1);
    return grid;
}

struct TempDir {
    std::filesystem::path path;
    TempDir() {
        path = std::filesystem::temp_directory_path() /
               ("eva_engine_test_" + std::to_string(::getpid()) + "_" +
                std::to_string(static_cast<unsigned long>(std::rand())));
        std::filesystem::create_directories(path);
    }
    ~TempDir() {
        std::error_code ec;
        std::filesystem::remove_all(path, ec);
    }
};

void write_bytes(const std::filesystem::path& file, const void* data, std::size_t bytes) {
    std::ofstream out(file, std::ios::binary);
    out.write(reinterpret_cast<const char*>(data), static_cast<std::streamsize>(bytes));
}

void write_aoi_json(const std::filesystem::path& dir, int rows, int cols, const std::string& format) {
    std::ofstream out(dir / "aoi.json");
    out << "{\n"
        << "  \"format\": \"" << format << "\",\n"
        << "  \"name\": \"unit-test\",\n"
        << "  \"source\": { \"nodata\": -3.402823e+38 },\n"
        << "  \"aoi\": {\n"
        << "    \"rows\": " << rows << ",\n"
        << "    \"cols\": " << cols << ",\n"
        << "    \"bbox_lonlat\": { \"west\": 1.0, \"south\": 2.0, \"east\": 3.0, \"north\": 4.0 },\n"
        << "    \"cell_size_m\": { \"dy\": 0.99, \"dx\": 0.97 }\n"
        << "  },\n"
        << "  \"policy\": { \"max_slope_deg\": 20.0 }\n"
        << "}\n";
}

// --- tests ------------------------------------------------------------------

void test_json_parses_nested_object() {
    const eva::Json json = eva::Json::parse(R"({
        "format": "mars-eva-aoi/1",
        "aoi": { "rows": 1000, "cols": 1000, "cell_size_m": { "dy": 0.99, "dx": 0.97 } },
        "flags": [1, 2, 3],
        "big": true
    })");
    CHECK(json.is_object());
    CHECK(json.at("format").string() == "mars-eva-aoi/1");
    CHECK(json.at("aoi").at("rows").number() == 1000.0);
    CHECK_NEAR(json.at("aoi").at("cell_size_m").at("dx").number(), 0.97, 1e-12);
    CHECK(json.at("flags").items().size() == 3);
    CHECK(json.at("big").boolean());
}

void test_json_rejects_malformed() {
    bool threw = false;
    try {
        eva::Json::parse("{ \"format\": ");
    } catch (const eva::JsonError&) {
        threw = true;
    }
    CHECK(threw);
}

void test_json_missing_key_throws() {
    const eva::Json json = eva::Json::parse("{\"a\": 1}");
    bool threw = false;
    try {
        json.at("b");
    } catch (const eva::JsonError&) {
        threw = true;
    }
    CHECK(threw);
}

void test_grid_loads_from_directory() {
    TempDir dir;
    const int rows = 4;
    const int cols = 5;
    write_aoi_json(dir.path, rows, cols, "mars-eva-aoi/1");

    std::vector<float> elevation(static_cast<std::size_t>(rows) * cols, 1.5f);
    std::vector<float> slope(static_cast<std::size_t>(rows) * cols, 2.5f);
    std::vector<std::uint8_t> cost(static_cast<std::size_t>(rows) * cols, 7);
    write_bytes(dir.path / "elevation.bin", elevation.data(), elevation.size() * sizeof(float));
    write_bytes(dir.path / "slope.bin", slope.data(), slope.size() * sizeof(float));
    write_bytes(dir.path / "cost.bin", cost.data(), cost.size());

    const eva::Grid grid = eva::load_grid(dir.path.string());
    CHECK(grid.rows() == rows);
    CHECK(grid.cols() == cols);
    CHECK_NEAR(grid.meta.dy_m, 0.99, 1e-12);
    CHECK_NEAR(grid.meta.dx_m, 0.97, 1e-12);
    CHECK_NEAR(grid.meta.max_slope_deg, 20.0, 1e-12);
    CHECK(grid.meta.nodata == -3.402823e38f);
    CHECK(grid.cells() == 20);
    CHECK_NEAR(grid.elevation[0], 1.5, 1e-6);
    CHECK(grid.cost[19] == 7);
    CHECK(!grid.has_radiation());
}

void test_grid_rejects_unknown_format() {
    TempDir dir;
    write_aoi_json(dir.path, 2, 2, "mars-eva-aoi/9");
    std::vector<float> elevation(4, 0.0f);
    std::vector<float> slope(4, 0.0f);
    std::vector<std::uint8_t> cost(4, 1);
    write_bytes(dir.path / "elevation.bin", elevation.data(), 4 * sizeof(float));
    write_bytes(dir.path / "slope.bin", slope.data(), 4 * sizeof(float));
    write_bytes(dir.path / "cost.bin", cost.data(), 4);

    bool threw = false;
    try {
        eva::load_grid(dir.path.string());
    } catch (const eva::GridError&) {
        threw = true;
    }
    CHECK(threw);
}

void test_grid_rejects_wrong_binary_size() {
    TempDir dir;
    write_aoi_json(dir.path, 2, 2, "mars-eva-aoi/1");
    std::vector<float> elevation(4, 0.0f);
    std::vector<float> slope(4, 0.0f);
    std::vector<std::uint8_t> cost(3, 1); // one byte short
    write_bytes(dir.path / "elevation.bin", elevation.data(), 4 * sizeof(float));
    write_bytes(dir.path / "slope.bin", slope.data(), 4 * sizeof(float));
    write_bytes(dir.path / "cost.bin", cost.data(), 3);

    bool threw = false;
    try {
        eva::load_grid(dir.path.string());
    } catch (const eva::GridError&) {
        threw = true;
    }
    CHECK(threw);
}

void test_astar_straight_line_on_flat() {
    const eva::Grid grid = make_grid(5, 5);
    const eva::Route route = eva::find_route(grid, 0, 0, 4, 4, eva::default_route_params());
    CHECK(route.found);
    CHECK(route.cells.size() == 5);
    CHECK_NEAR(route.length_m, 4.0 * std::sqrt(2.0), 1e-6);
    CHECK_NEAR(route.climb_m, 0.0, 1e-9);
}

void test_astar_refuses_a_wall_goal() {
    eva::Grid grid = make_grid(5, 5);
    grid.cost[grid.index(4, 4)] = 255;
    const eva::Route route = eva::find_route(grid, 0, 0, 4, 4, eva::default_route_params());
    CHECK(!route.found);
}

void test_astar_detours_around_a_wall() {
    eva::Grid grid = make_grid(5, 5);
    for (int row = 0; row < 4; ++row) {
        grid.cost[grid.index(row, 2)] = 255;
    }
    const eva::Route route = eva::find_route(grid, 0, 0, 4, 4, eva::default_route_params());
    CHECK(route.found);
    bool touches_wall = false;
    for (int cell : route.cells) {
        if (grid.cost[static_cast<std::size_t>(cell)] == 255) touches_wall = true;
    }
    CHECK(!touches_wall);
    CHECK(route.length_m > 4.0);
}

void test_astar_prefers_flat_over_steep() {
    eva::Grid grid = make_grid(3, 5);
    for (int col = 1; col <= 3; ++col) {
        grid.cost[grid.index(1, col)] = 254;
    }
    const eva::Route route = eva::find_route(grid, 1, 0, 1, 4, eva::default_route_params());
    CHECK(route.found);
    bool uses_steep = false;
    for (int cell : route.cells) {
        if (grid.cost[static_cast<std::size_t>(cell)] == 254) uses_steep = true;
    }
    CHECK(!uses_steep);
    // Two diagonals leave/rejoin the middle row and two steps cross its top.
    CHECK_NEAR(route.length_m, 2.0 + 2.0 * std::sqrt(2.0), 1e-6);
}

void test_astar_never_cuts_a_corner() {
    eva::Grid grid = make_grid(3, 3);
    grid.cost[grid.index(1, 0)] = 255;
    grid.cost[grid.index(0, 1)] = 255;

    eva::RouteParams strict = eva::default_route_params();
    strict.no_corner_cutting = true;
    const eva::Route blocked = eva::find_route(grid, 0, 0, 1, 1, strict);
    CHECK(!blocked.found);

    eva::RouteParams relaxed = eva::default_route_params();
    relaxed.no_corner_cutting = false;
    const eva::Route allowed = eva::find_route(grid, 0, 0, 1, 1, relaxed);
    CHECK(allowed.found);
}

void test_astar_treats_nodata_as_a_wall() {
    eva::Grid grid = make_grid(3, 3);
    grid.cost[grid.index(2, 2)] = 0;
    const eva::Route route = eva::find_route(grid, 0, 0, 2, 2, eva::default_route_params());
    CHECK(!route.found);
}

void test_astar_uses_radiation_only_when_supplied() {
    eva::Grid grid = make_grid(3, 5);
    grid.radiation.assign(grid.cells(), 0.0f);
    for (int col = 1; col <= 3; ++col) grid.radiation[grid.index(1, col)] = 10.0f;

    eva::RouteParams params = eva::default_route_params();
    params.radiation_weight = 2.0;
    const eva::Route route = eva::find_route(grid, 1, 0, 1, 4, params);
    CHECK(route.found);
    bool exposed = false;
    for (int cell : route.cells) {
        if (grid.radiation[static_cast<std::size_t>(cell)] > 0.0f) exposed = true;
    }
    CHECK(!exposed);
    CHECK_NEAR(route.length_m, 2.0 + 2.0 * std::sqrt(2.0), 1e-6);

    grid.radiation.clear();
    const eva::Route without_layer = eva::find_route(grid, 1, 0, 1, 4, params);
    CHECK(without_layer.found);
    CHECK_NEAR(without_layer.length_m, 4.0, 1e-6);
}

void test_astar_start_equals_goal() {
    const eva::Grid grid = make_grid(3, 3);
    const eva::Route route = eva::find_route(grid, 1, 1, 1, 1, eva::default_route_params());
    CHECK(route.found);
    CHECK(route.cells.size() == 1);
    CHECK_NEAR(route.length_m, 0.0, 1e-12);
}

void test_plss_flat_leg() {
    const eva::Grid grid = make_grid(1, 11);
    const eva::Route route = eva::find_route(grid, 0, 0, 0, 10, eva::default_route_params());
    CHECK(route.found);

    const eva::PlssConfig config;
    const eva::LegSim leg = eva::simulate_leg(grid, route, config);
    CHECK_NEAR(leg.distance_m, 10.0, 1e-6);
    CHECK_NEAR(leg.time_s, 10.0, 1e-6); // 1.0 m/s on flat ground
    CHECK_NEAR(leg.o2_kg, config.o2_rate_kg_per_h * 10.0 / 3600.0, 1e-12);
}

void test_plss_steep_leg_costs_more() {
    eva::Grid grid = make_grid(1, 11);
    for (float& value : grid.slope) value = 20.0f; // at the exclusion slope
    const eva::Route route = eva::find_route(grid, 0, 0, 0, 10, eva::default_route_params());
    CHECK(route.found);

    const eva::PlssConfig config;
    const eva::LegSim flat = eva::simulate_leg(make_grid(1, 11), route, config);
    const eva::LegSim steep = eva::simulate_leg(grid, route, config);
    CHECK_NEAR(steep.time_s, 10.0 / config.walk_speed_slope_mps, 1e-6);
    CHECK(steep.o2_kg > flat.o2_kg);
    CHECK(steep.batt_wh > flat.batt_wh);
}

void test_round_trip_is_feasible_with_full_tanks() {
    const eva::Grid grid = make_grid(1, 11);
    const eva::Route outbound = eva::find_route(grid, 0, 0, 0, 10, eva::default_route_params());
    const eva::Route inbound = eva::find_route(grid, 0, 10, 0, 0, eva::default_route_params());
    const eva::EvaSim sim = eva::simulate_round_trip(grid, outbound, inbound, eva::PlssConfig());
    CHECK(sim.return_feasible);
    CHECK_NEAR(sim.ttr_s, 10.0, 1e-6);
    CHECK((sim.flags & eva::ALERT_EVA_ABORT) == 0);
    CHECK((sim.flags & eva::ALERT_O2_LOW) == 0);
    CHECK(sim.o2_fraction > 0.9);
}

void test_round_trip_flags_an_impossible_return() {
    const eva::Grid grid = make_grid(1, 11);
    const eva::Route outbound = eva::find_route(grid, 0, 0, 0, 10, eva::default_route_params());
    const eva::Route inbound = eva::find_route(grid, 0, 10, 0, 0, eva::default_route_params());

    eva::PlssConfig config;
    config.o2_capacity_kg = 0.001; // not enough for the walk back
    const eva::EvaSim sim = eva::simulate_round_trip(grid, outbound, inbound, config);
    CHECK(!sim.return_feasible);
    CHECK((sim.flags & eva::ALERT_EVA_ABORT) != 0);
    CHECK((sim.flags & eva::ALERT_O2_LOW) != 0);
}

void test_round_trip_without_a_route_is_not_feasible() {
    eva::Grid grid = make_grid(3, 3);
    grid.cost[grid.index(2, 2)] = 255;
    const eva::Route outbound = eva::find_route(grid, 0, 0, 2, 2, eva::default_route_params());
    const eva::Route inbound = eva::find_route(grid, 2, 2, 0, 0, eva::default_route_params());
    const eva::EvaSim sim = eva::simulate_round_trip(grid, outbound, inbound, eva::PlssConfig());
    CHECK(!sim.return_feasible);
    CHECK((sim.flags & eva::ALERT_EVA_ABORT) != 0);
}

void test_round_trip_flags_battery_and_dose() {
    const eva::Grid grid = make_grid(1, 11);
    const eva::Route outbound = eva::find_route(grid, 0, 0, 0, 10, eva::default_route_params());
    const eva::Route inbound = eva::find_route(grid, 0, 10, 0, 0, eva::default_route_params());

    eva::PlssConfig config;
    config.battery_capacity_wh = 0.4;
    config.dose_rate_msv_per_h = 100.0;
    const eva::EvaSim sim = eva::simulate_round_trip(grid, outbound, inbound, config);
    CHECK(!sim.return_feasible);
    CHECK((sim.flags & eva::ALERT_BATT_LOW) != 0);
    CHECK((sim.flags & eva::ALERT_RAD_PEAK) != 0);
    CHECK((sim.flags & eva::ALERT_EVA_ABORT) != 0);
}

void test_alert_names() {
    CHECK(std::string(eva::alert_name(eva::ALERT_O2_LOW)) == "O2_LOW");
    CHECK(std::string(eva::alert_name(eva::ALERT_EVA_ABORT)) == "EVA_ABORT");
}

} // namespace

int main() {
    test_json_parses_nested_object();
    test_json_rejects_malformed();
    test_json_missing_key_throws();
    test_grid_loads_from_directory();
    test_grid_rejects_unknown_format();
    test_grid_rejects_wrong_binary_size();
    test_astar_straight_line_on_flat();
    test_astar_refuses_a_wall_goal();
    test_astar_detours_around_a_wall();
    test_astar_prefers_flat_over_steep();
    test_astar_never_cuts_a_corner();
    test_astar_treats_nodata_as_a_wall();
    test_astar_uses_radiation_only_when_supplied();
    test_astar_start_equals_goal();
    test_plss_flat_leg();
    test_plss_steep_leg_costs_more();
    test_round_trip_is_feasible_with_full_tanks();
    test_round_trip_flags_an_impossible_return();
    test_round_trip_without_a_route_is_not_feasible();
    test_round_trip_flags_battery_and_dose();
    test_alert_names();

    std::cout << g_checks - g_failures << "/" << g_checks << " checks passed\n";
    return g_failures == 0 ? 0 : 1;
}
