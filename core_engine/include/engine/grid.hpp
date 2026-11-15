// Layer 1 artifacts as the engine sees them.
//
// Contract: docs/ingest-format.md. The engine reads aoi.json first, validates
// the format tag and the grid shape, and refuses anything it does not
// understand instead of guessing.
#pragma once

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

namespace eva {

class GridError : public std::runtime_error {
public:
    explicit GridError(const std::string& what) : std::runtime_error(what) {}
};

struct AoiMeta {
    std::string format;   // "mars-eva-aoi/1"
    std::string name;
    int rows = 0;
    int cols = 0;
    double dy_m = 0.0;    // metric cell height at the AOI's middle row
    double dx_m = 0.0;    // metric cell width at the AOI's middle row
    double west = 0.0;
    double south = 0.0;
    double east = 0.0;
    double north = 0.0;
    double max_slope_deg = 0.0;
    double radiation_baseline_msv_per_h = 0.03;
    bool environment_available = false;
    double ambient_temperature_c = 0.0;
    double ambient_pressure_pa = 0.0;
    double mean_thermal_inertia = 0.0;
    std::string terrain_traction;
    float nodata = 0.0f;
};

struct Grid {
    AoiMeta meta;
    std::vector<float> elevation;   // metres, row-major
    std::vector<float> slope;       // degrees, row-major, NaN = unknown
    std::vector<uint8_t> cost;      // 0 no data, 1..254 traversable, 255 wall
    std::vector<float> radiation;   // optional, empty when layer 1 omits it
    std::vector<float> thermal_inertia; // optional, SI units J/(m^2 K s^0.5)

    int rows() const { return meta.rows; }
    int cols() const { return meta.cols; }
    std::size_t cells() const {
        return static_cast<std::size_t>(meta.rows) * static_cast<std::size_t>(meta.cols);
    }
    std::size_t index(int row, int col) const {
        return static_cast<std::size_t>(row) * static_cast<std::size_t>(meta.cols) +
               static_cast<std::size_t>(col);
    }
    bool in_bounds(int row, int col) const {
        return row >= 0 && col >= 0 && row < meta.rows && col < meta.cols;
    }
    bool has_radiation() const { return !radiation.empty(); }
    bool has_thermal_inertia() const { return !thermal_inertia.empty(); }

    // A cell is traversable when it carries a usable cost and a finite
    // elevation. Cost 0 means "no data" and 255 means "impassable"; neither is
    // walkable, and neither may be treated as cheap ground.
    bool traversable(int row, int col) const;
};

// Loads <dir>/{elevation,slope,cost}.bin plus <dir>/aoi.json and <dir>/radiation.bin
// when present. Throws GridError on any contract violation.
Grid load_grid(const std::string& dir);

} // namespace eva
