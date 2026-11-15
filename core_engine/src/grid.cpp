#include "engine/grid.hpp"

#include "engine/json.hpp"

#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

namespace eva {
namespace {

bool host_is_little_endian() {
    const std::uint16_t probe = 1;
    return *reinterpret_cast<const std::uint8_t*>(&probe) == 1;
}

std::string read_text(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) throw GridError("cannot open " + path);
    std::string text((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    if (in.bad()) throw GridError("cannot read " + path);
    return text;
}

std::vector<char> read_binary(const std::string& path, std::size_t expected) {
    std::ifstream in(path, std::ios::binary);
    if (!in) throw GridError("cannot open " + path);
    in.seekg(0, std::ios::end);
    const std::streamoff size = in.tellg();
    if (size < 0) throw GridError("cannot size " + path);
    if (static_cast<std::size_t>(size) != expected) {
        throw GridError(path + ": expected " + std::to_string(expected) + " bytes, found " +
                        std::to_string(size));
    }
    in.seekg(0, std::ios::beg);
    std::vector<char> data(expected);
    if (expected > 0 && !in.read(data.data(), static_cast<std::streamsize>(expected))) {
        throw GridError("cannot read " + path);
    }
    return data;
}

template <typename T>
std::vector<T> decode(const std::vector<char>& bytes, const std::string& path) {
    if (bytes.size() % sizeof(T) != 0) {
        throw GridError(path + ": size is not a multiple of " + std::to_string(sizeof(T)));
    }
    std::vector<T> out(bytes.size() / sizeof(T));
    if (!out.empty()) std::memcpy(out.data(), bytes.data(), bytes.size());
    return out;
}

AoiMeta read_meta(const std::string& meta_path) {
    const Json document = Json::parse(read_text(meta_path));

    AoiMeta meta;
    meta.format = std::string(document.at("format").string());
    if (meta.format != "mars-eva-aoi/1") {
        throw GridError(meta_path + ": unsupported format '" + meta.format + "'");
    }
    if (document.has("name")) meta.name = std::string(document.at("name").string());

    const Json& aoi = document.at("aoi");
    meta.rows = static_cast<int>(aoi.at("rows").number());
    meta.cols = static_cast<int>(aoi.at("cols").number());
    if (meta.rows <= 0 || meta.cols <= 0) {
        throw GridError(meta_path + ": rows and cols must be positive");
    }

    const Json& cell = aoi.at("cell_size_m");
    meta.dy_m = cell.at("dy").number();
    meta.dx_m = cell.at("dx").number();
    if (!(meta.dy_m > 0.0) || !(meta.dx_m > 0.0)) {
        throw GridError(meta_path + ": cell size must be positive");
    }

    const Json& bbox = aoi.at("bbox_lonlat");
    meta.west = bbox.at("west").number();
    meta.south = bbox.at("south").number();
    meta.east = bbox.at("east").number();
    meta.north = bbox.at("north").number();

    meta.max_slope_deg = document.at("policy").at("max_slope_deg").number();
    if (document.has("source") && document.at("source").has("nodata")) {
        meta.nodata = static_cast<float>(document.at("source").at("nodata").number());
    }
    return meta;
}

} // namespace

bool Grid::traversable(int row, int col) const {
    if (!in_bounds(row, col)) return false;
    const std::size_t i = index(row, col);
    if (cost[i] == 0 || cost[i] == 255) return false;
    if (!std::isfinite(elevation[i])) return false;
    return true;
}

Grid load_grid(const std::string& dir) {
    if (!host_is_little_endian()) {
        throw GridError("layer 1 artifacts are little-endian; this host is big-endian");
    }

    const std::string meta_path = dir + "/aoi.json";
    AoiMeta meta;
    try {
        meta = read_meta(meta_path);
    } catch (const JsonError& error) {
        throw GridError(meta_path + ": " + error.what());
    }

    Grid grid;
    grid.meta = meta;

    const std::size_t cells =
        static_cast<std::size_t>(meta.rows) * static_cast<std::size_t>(meta.cols);
    const std::size_t float_bytes = cells * sizeof(float);

    grid.elevation =
        decode<float>(read_binary(dir + "/elevation.bin", float_bytes), "elevation.bin");
    grid.slope = decode<float>(read_binary(dir + "/slope.bin", float_bytes), "slope.bin");
    grid.cost = decode<std::uint8_t>(read_binary(dir + "/cost.bin", cells), "cost.bin");

    const std::string radiation_path = dir + "/radiation.bin";
    {
        std::ifstream probe(radiation_path, std::ios::binary);
        if (probe.good()) {
            probe.close();
            grid.radiation = decode<float>(read_binary(radiation_path, float_bytes), "radiation.bin");
        }
    }

    const std::string thermal_inertia_path = dir + "/thermal_inertia.bin";
    {
        std::ifstream probe(thermal_inertia_path, std::ios::binary);
        if (probe.good()) {
            probe.close();
            grid.thermal_inertia = decode<float>(read_binary(thermal_inertia_path, float_bytes), "thermal_inertia.bin");
        }
    }
    return grid;
}

} // namespace eva
