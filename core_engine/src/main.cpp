#include "engine/astar.hpp"
#include "engine/grid.hpp"
#include "engine/plss.hpp"

#include <charconv>
#include <cmath>
#include <cstddef>
#include <iomanip>
#include <iostream>
#include <locale>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>

namespace {

class CliError : public std::runtime_error {
public:
    explicit CliError(const std::string& what) : std::runtime_error(what) {}
};

std::string quote(std::string_view value) {
    std::ostringstream out;
    out << '"';
    for (const unsigned char c : value) {
        switch (c) {
            case '"': out << "\\\""; break;
            case '\\': out << "\\\\"; break;
            case '\b': out << "\\b"; break;
            case '\f': out << "\\f"; break;
            case '\n': out << "\\n"; break;
            case '\r': out << "\\r"; break;
            case '\t': out << "\\t"; break;
            default:
                if (c < 0x20) {
                    out << "\\u00" << std::hex << std::setw(2) << std::setfill('0')
                        << static_cast<unsigned>(c) << std::dec;
                } else {
                    out << static_cast<char>(c);
                }
        }
    }
    out << '"';
    return out.str();
}

std::string number(double value) {
    if (!std::isfinite(value)) throw CliError("cannot serialize a non-finite result");
    std::ostringstream out;
    out.imbue(std::locale::classic());
    out << std::setprecision(15) << value;
    return out.str();
}

const char* boolean(bool value) { return value ? "true" : "false"; }

void usage(std::ostream& out) {
    out << "Mars EVA core engine — layer 2\n\n"
        << "Usage:\n"
        << "  eva_engine info --dir DIR\n"
        << "  eva_engine route --dir DIR --start ROW,COL --goal ROW,COL [options]\n"
        << "  eva_engine sim --dir DIR --start ROW,COL --goal ROW,COL [options]\n\n"
        << "Route options:\n"
        << "  --slope-weight N          Terrain penalty scale (default 4)\n"
        << "  --climb-weight N          Cost per metre climbed (default 5)\n"
        << "  --radiation-weight N      Exposure weight; needs radiation.bin (default 1)\n"
        << "  --soil-weight N           Soil drag penalty; needs thermal_inertia.bin (default 1)\n"
        << "  --orthogonal              Disable diagonal movement\n"
        << "  --allow-corner-cutting    Permit diagonal movement between blocked cells\n\n"
        << "Simulation options:\n"
        << "  --o2-capacity KG          Initial oxygen (default 1.20)\n"
        << "  --battery-capacity WH     Initial battery energy (default 250)\n"
        << "  --path                    Include both route paths in the JSON output\n\n"
        << "All successful commands print JSON to stdout. Input/contract errors print\n"
        << "a JSON error to stderr and exit 2. An unreachable route is a valid result\n"
        << "with found=false and exit 0.\n";
}

class Options {
public:
    Options(int argc, char** argv) {
        if (argc < 2) throw CliError("missing command; use --help");
        command = argv[1];
        if (command == "--help" || command == "-h") {
            help = true;
            return;
        }
        for (int i = 2; i < argc; ++i) {
            const std::string token = argv[i];
            if (token == "--help" || token == "-h") {
                help = true;
                continue;
            }
            if (token.rfind("--", 0) != 0) {
                throw CliError("unexpected positional argument '" + token + "'");
            }
            if (token == "--orthogonal" || token == "--allow-corner-cutting" ||
                token == "--path") {
                if (!switches.insert(token).second) {
                    throw CliError("duplicate option '" + token + "'");
                }
                continue;
            }
            if (i + 1 >= argc) throw CliError("missing value for option '" + token + "'");
            if (!values.emplace(token, argv[++i]).second) {
                throw CliError("duplicate option '" + token + "'");
            }
        }
    }

    void validate(const std::set<std::string>& allowed) const {
        for (const auto& [key, value] : values) {
            (void)value;
            if (!allowed.contains(key)) throw CliError("unknown option '" + key + "'");
        }
        for (const std::string& key : switches) {
            if (!allowed.contains(key)) throw CliError("unknown option '" + key + "'");
        }
    }

    std::string required(std::string_view key) const {
        const auto it = values.find(std::string(key));
        if (it == values.end()) throw CliError("missing required option '" + std::string(key) + "'");
        return it->second;
    }

    double number_or(std::string_view key, double fallback, bool positive = false) const {
        const auto it = values.find(std::string(key));
        if (it == values.end()) return fallback;
        const std::string& text = it->second;
        std::size_t consumed = 0;
        double value = 0.0;
        try {
            value = std::stod(text, &consumed);
        } catch (const std::exception&) {
            throw CliError("invalid numeric value for '" + std::string(key) + "'");
        }
        if (consumed != text.size() || !std::isfinite(value) || value < 0.0 ||
            (positive && value == 0.0)) {
            throw CliError("invalid numeric value for '" + std::string(key) + "'");
        }
        return value;
    }

    bool enabled(std::string_view key) const { return switches.contains(std::string(key)); }

    std::string command;
    bool help = false;

private:
    std::map<std::string, std::string> values;
    std::set<std::string> switches;
};

struct Cell {
    int row = 0;
    int col = 0;
};

int parse_int(const std::string& text, std::string_view option) {
    int value = 0;
    const char* begin = text.data();
    const char* end = begin + text.size();
    const auto result = std::from_chars(begin, end, value);
    if (result.ec != std::errc{} || result.ptr != end) {
        throw CliError("invalid integer for '" + std::string(option) + "'");
    }
    return value;
}

Cell parse_cell(const std::string& text, std::string_view option) {
    const std::size_t comma = text.find(',');
    if (comma == std::string::npos || text.find(',', comma + 1) != std::string::npos) {
        throw CliError("invalid " + std::string(option) + " '" + text + "': expected ROW,COL");
    }
    try {
        return {parse_int(text.substr(0, comma), option), parse_int(text.substr(comma + 1), option)};
    } catch (const CliError&) {
        throw CliError("invalid " + std::string(option) + " '" + text + "': expected ROW,COL");
    }
}

std::string data_directory(const Options& options) { return options.required("--dir"); }

eva::RouteParams route_params(const Options& options) {
    eva::RouteParams params = eva::default_route_params();
    params.slope_weight = options.number_or("--slope-weight", params.slope_weight);
    params.climb_weight = options.number_or("--climb-weight", params.climb_weight);
    params.radiation_weight = options.number_or("--radiation-weight", params.radiation_weight);
    params.soil_weight = options.number_or("--soil-weight", params.soil_weight);
    params.allow_diagonal = !options.enabled("--orthogonal");
    params.no_corner_cutting = !options.enabled("--allow-corner-cutting");
    return params;
}

void print_leg(std::ostream& out, const eva::LegSim& leg) {
    out << "{\"time_s\":" << number(leg.time_s)
        << ",\"o2_kg\":" << number(leg.o2_kg)
        << ",\"batt_wh\":" << number(leg.batt_wh)
        << ",\"dose_msv\":" << number(leg.dose_msv)
        << ",\"distance_m\":" << number(leg.distance_m)
        << ",\"climb_m\":" << number(leg.climb_m) << '}';
}

void print_route(std::ostream& out, const eva::Grid& grid, const eva::Route& route, bool with_path) {
    out << "\"found\":" << boolean(route.found)
        << ",\"cost\":" << number(route.cost)
        << ",\"length_m\":" << number(route.length_m)
        << ",\"climb_m\":" << number(route.climb_m)
        << ",\"descent_m\":" << number(route.descent_m)
        << ",\"total_radiation\":" << number(route.total_radiation)
        << ",\"mean_thermal_inertia\":" << number(route.mean_thermal_inertia)
        << ",\"expanded\":" << route.expanded
        << ",\"elapsed_ms\":" << number(route.elapsed_ms);
    if (!with_path) return;

    out << ",\"path\":[";
    for (std::size_t i = 0; i < route.cells.size(); ++i) {
        const int cell = route.cells[i];
        const int row = cell / grid.cols();
        const int col = cell % grid.cols();
        if (i != 0) out << ',';
        out << "{\"row\":" << row << ",\"col\":" << col
            << ",\"elevation_m\":"
            << number(static_cast<double>(grid.elevation[static_cast<std::size_t>(cell)]));
        if (grid.has_radiation()) {
            out << ",\"radiation\":"
                << number(static_cast<double>(grid.radiation[static_cast<std::size_t>(cell)]));
        }
        if (grid.has_thermal_inertia()) {
            out << ",\"thermal_inertia\":"
                << number(static_cast<double>(grid.thermal_inertia[static_cast<std::size_t>(cell)]));
        }
        out << '}';
    }
    out << ']';
}

void print_start_goal(std::ostream& out, const Cell& start, const Cell& goal) {
    out << "\"start\":{\"row\":" << start.row << ",\"col\":" << start.col << '}'
        << ",\"goal\":{\"row\":" << goal.row << ",\"col\":" << goal.col << '}';
}

int command_info(const Options& options) {
    options.validate({"--dir"});
    const eva::Grid grid = eva::load_grid(data_directory(options));
    std::size_t traversable = 0;
    for (int row = 0; row < grid.rows(); ++row) {
        for (int col = 0; col < grid.cols(); ++col) {
            if (grid.traversable(row, col)) ++traversable;
        }
    }

    const eva::AoiMeta& meta = grid.meta;
    std::cout << "{\"ok\":true,\"aoi\":{"
              << "\"format\":" << quote(meta.format)
              << ",\"name\":" << quote(meta.name)
              << ",\"rows\":" << meta.rows << ",\"cols\":" << meta.cols
              << ",\"cell_size_m\":{\"dy\":" << number(meta.dy_m)
              << ",\"dx\":" << number(meta.dx_m) << '}'
              << ",\"bbox_lonlat\":{\"west\":" << number(meta.west)
              << ",\"south\":" << number(meta.south)
              << ",\"east\":" << number(meta.east)
              << ",\"north\":" << number(meta.north) << "}}"
              << ",\"traversable_cells\":" << traversable
              << ",\"non_traversable_cells\":" << grid.cells() - traversable
              << ",\"radiation_available\":" << boolean(grid.has_radiation())
              << ",\"radiation_baseline_msv_per_h\":" << number(meta.radiation_baseline_msv_per_h)
              << ",\"thermal_inertia_available\":" << boolean(grid.has_thermal_inertia())
              << ",\"environment_available\":" << boolean(meta.environment_available);
    if (meta.environment_available) {
        std::cout << ",\"environment\":{\"ambient_temperature_c\":" << number(meta.ambient_temperature_c)
                  << ",\"ambient_pressure_pa\":" << number(meta.ambient_pressure_pa)
                  << ",\"mean_thermal_inertia\":" << number(meta.mean_thermal_inertia)
                  << ",\"terrain_traction\":" << quote(meta.terrain_traction) << '}';
    }
    std::cout << "}\n";
    return 0;
}

const std::set<std::string>& route_options() {
    static const std::set<std::string> allowed = {
        "--dir", "--start", "--goal", "--slope-weight", "--climb-weight",
        "--radiation-weight", "--soil-weight", "--orthogonal", "--allow-corner-cutting"};
    return allowed;
}

int command_route(const Options& options) {
    options.validate(route_options());
    const Cell start = parse_cell(options.required("--start"), "start");
    const Cell goal = parse_cell(options.required("--goal"), "goal");
    const eva::Grid grid = eva::load_grid(data_directory(options));
    const eva::Route route = eva::find_route(grid, start.row, start.col, goal.row, goal.col,
                                             route_params(options));

    std::cout << "{\"ok\":true,";
    print_start_goal(std::cout, start, goal);
    std::cout << ',';
    print_route(std::cout, grid, route, true);
    std::cout << ",\"radiation_available\":" << boolean(grid.has_radiation())
              << ",\"thermal_inertia_available\":" << boolean(grid.has_thermal_inertia()) << "}\n";
    return 0;
}

int command_sim(const Options& options) {
    std::set<std::string> allowed = route_options();
    allowed.insert({"--o2-capacity", "--battery-capacity", "--path"});
    options.validate(allowed);

    const Cell start = parse_cell(options.required("--start"), "start");
    const Cell goal = parse_cell(options.required("--goal"), "goal");
    const eva::Grid grid = eva::load_grid(data_directory(options));
    const eva::RouteParams params = route_params(options);
    const eva::Route outbound = eva::find_route(grid, start.row, start.col, goal.row, goal.col, params);
    const eva::Route inbound = eva::find_route(grid, goal.row, goal.col, start.row, start.col, params);

    eva::PlssConfig config;
    config.dose_rate_msv_per_h = grid.meta.radiation_baseline_msv_per_h;
    config.o2_capacity_kg = options.number_or("--o2-capacity", config.o2_capacity_kg, true);
    config.battery_capacity_wh =
        options.number_or("--battery-capacity", config.battery_capacity_wh, true);
    const eva::EvaSim sim = eva::simulate_round_trip(grid, outbound, inbound, config);
    const bool found = outbound.found && inbound.found;
    const bool with_path = options.enabled("--path");

    std::cout << "{\"ok\":true,";
    print_start_goal(std::cout, start, goal);
    std::cout << ",\"found\":" << boolean(found) << ",\"outbound\":{\"route\":{";
    print_route(std::cout, grid, outbound, with_path);
    std::cout << "},\"sim\":";
    print_leg(std::cout, sim.outbound);
    std::cout << "},\"inbound\":{\"route\":{";
    print_route(std::cout, grid, inbound, with_path);
    std::cout << "},\"sim\":";
    print_leg(std::cout, sim.inbound);
    std::cout << "},\"ttr_s\":" << number(sim.ttr_s)
              << ",\"o2_remaining_kg\":" << number(sim.o2_remaining_kg)
              << ",\"batt_remaining_wh\":" << number(sim.batt_remaining_wh)
              << ",\"dose_total_msv\":" << number(sim.dose_total_msv)
              << ",\"o2_fraction\":" << number(sim.o2_fraction)
              << ",\"batt_fraction\":" << number(sim.batt_fraction)
              << ",\"return_feasible\":" << boolean(sim.return_feasible)
              << ",\"flags\":" << static_cast<unsigned>(sim.flags) << ",\"alerts\":[";
    bool first = true;
    for (unsigned bit = 0; bit < 7; ++bit) {
        const auto flag = static_cast<std::uint8_t>(1u << bit);
        if ((sim.flags & flag) == 0) continue;
        if (!first) std::cout << ',';
        first = false;
        std::cout << quote(eva::alert_name(flag));
    }
    std::cout << "],\"reasons\":[";
    for (std::size_t i = 0; i < sim.reasons.size(); ++i) {
        if (i != 0) std::cout << ',';
        std::cout << quote(sim.reasons[i]);
    }
    std::cout << "],\"radiation_available\":" << boolean(grid.has_radiation())
              << ",\"thermal_inertia_available\":" << boolean(grid.has_thermal_inertia()) << "}\n";
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    try {
        const Options options(argc, argv);
        if (options.help) {
            usage(std::cout);
            return 0;
        }
        if (options.command == "info") return command_info(options);
        if (options.command == "route") return command_route(options);
        if (options.command == "sim") return command_sim(options);
        throw CliError("unknown command '" + options.command + "'; use --help");
    } catch (const std::exception& error) {
        std::cerr << "{\"ok\":false,\"error\":" << quote(error.what()) << "}\n";
        return 2;
    }
}
