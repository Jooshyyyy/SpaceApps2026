#include "comms/engine_adapter.hpp"
#include "comms/websocket.hpp"
#include "engine/json.hpp"

#include <arpa/inet.h>
#include <algorithm>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <cmath>
#include <csignal>
#include <iostream>
#include <map>
#include <memory>
#include <poll.h>
#include <sstream>
#include <stdexcept>
#include <sys/socket.h>
#include <unistd.h>
#include <vector>

namespace {
using Clock = std::chrono::steady_clock;
volatile std::sig_atomic_t stopped = 0;
void stop_handler(int) { stopped = 1; }

struct Descriptor {
    int fd = -1;
    explicit Descriptor(int value = -1) : fd(value) {}
    ~Descriptor() { if (fd >= 0) ::close(fd); }
    Descriptor(const Descriptor&) = delete;
    Descriptor& operator=(const Descriptor&) = delete;
};
int integer(std::string_view value) {
    int result = 0;
    const auto parsed = std::from_chars(value.data(), value.data() + value.size(), result);
    if (parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size()) throw std::runtime_error("invalid integer argument");
    return result;
}
comms::Point point(std::string_view text) {
    auto comma = text.find(',');
    if (comma == std::string_view::npos) throw std::runtime_error("point must be ROW,COL");
    return {integer(text.substr(0, comma)), integer(text.substr(comma+1))};
}
int coordinate(const eva::Json& value, int limit) {
    const double n = value.number();
    if (!std::isfinite(n) || n < 0 || n >= limit || std::floor(n) != n) throw std::runtime_error("position/goal outside AOI or not an integer");
    return static_cast<int>(n);
}
std::string number(double n) {
    std::ostringstream out; out.precision(15); out << n; return out.str();
}

struct Client {
    Descriptor socket;
    std::string input, output;
    bool upgraded = false, dead = false, closing = false;
    Clock::time_point connected = Clock::now();
    int messages = 0;
    Clock::time_point window = Clock::now();
    explicit Client(int fd) : socket(fd) {}
    void queue(std::string bytes) {
        if (output.size() + bytes.size() > 16 * 1024 * 1024) { dead = true; return; }
        output += bytes;
    }
    void text(const std::string& message) { queue(comms::websocket_message(1, message)); }
};

class Bus {
public:
    Bus(std::string engine, std::string dir, int port, comms::Point start, comms::Point goal,
        int demo_step_ms)
        : adapter_(std::move(engine), std::move(dir)), mission_(start, goal),
          accounted_position_(start), demo_step_ms_(demo_step_ms) {
        info_ = adapter_.info();
        const auto info = eva::Json::parse(info_);
        const auto& aoi = info.at("aoi");
        rows_ = static_cast<int>(aoi.at("rows").number());
        cols_ = static_cast<int>(aoi.at("cols").number());
        dy_ = aoi.at("cell_size_m").at("dy").number();
        dx_ = aoi.at("cell_size_m").at("dx").number();
        validate_point(start); validate_point(goal);
        mission_.accept_simulation(adapter_.simulate(
            start, goal, mission_.o2_remaining_kg(), mission_.battery_remaining_wh()));
        // HELLO's datum must be real, even if the requested goal is a wall.
        const auto sim = eva::Json::parse(mission_.simulation_json());
        if (sim.at("outbound").at("route").at("path").size() == 0) {
            mission_.accept_simulation(adapter_.simulate(
                start, start, mission_.o2_remaining_kg(), mission_.battery_remaining_wh()));
            if (eva::Json::parse(mission_.simulation_json()).at("outbound").at("route").at("path").size() == 0) {
                throw std::runtime_error("start/refuge must be a traversable cell");
            }
            mission_.accept_simulation(adapter_.simulate(
                start, goal, mission_.o2_remaining_kg(), mission_.battery_remaining_wh()));
        }
        listener_.fd = ::socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
        if (listener_.fd < 0) throw std::runtime_error("socket failed");
        int yes = 1;
        ::setsockopt(listener_.fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
        sockaddr_in addr{};
        addr.sin_family = AF_INET; addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        addr.sin_port = htons(static_cast<std::uint16_t>(port));
        if (::bind(listener_.fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 ||
            ::listen(listener_.fd, 16) != 0) throw std::runtime_error("cannot bind/listen on loopback port");
        socklen_t len = sizeof(addr);
        if (::getsockname(listener_.fd, reinterpret_cast<sockaddr*>(&addr), &len) != 0) throw std::runtime_error("getsockname failed");
        port_ = ntohs(addr.sin_port);
        epoch_ = Clock::now(); next_tick_ = epoch_;
        next_demo_step_ = epoch_ + std::chrono::milliseconds(demo_step_ms_);
    }
    void run() {
        std::cout << "{\"type\":\"listening\",\"host\":\"127.0.0.1\",\"port\":" << port_ << "}" << std::endl;
        while (!stopped) {
            std::vector<pollfd> pollfds{{listener_.fd, POLLIN, 0}};
            for (const auto& c : clients_) pollfds.push_back({c->socket.fd,
                static_cast<short>((c->closing ? 0 : POLLIN) | (c->output.empty() ? 0 : POLLOUT)), 0});
            int timeout = static_cast<int>(std::chrono::duration_cast<std::chrono::milliseconds>(next_tick_ - Clock::now()).count());
            timeout = std::clamp(timeout, 0, job_ ? 5 : 100);
            if (::poll(pollfds.data(), pollfds.size(), timeout) < 0 && errno != EINTR) throw std::runtime_error("socket poll failed");
            const auto count = clients_.size();
            for (std::size_t i = 0; i < count; ++i) {
                auto& c = *clients_[i];
                const short events = pollfds[i+1].revents;
                if (events & (POLLERR | POLLHUP | POLLNVAL)) c.dead = true;
                if (!c.dead && events & POLLIN) read_client(c);
                if (!c.dead && events & POLLOUT) write_client(c);
                if (!c.upgraded && Clock::now() - c.connected > std::chrono::seconds(3)) c.dead = true;
                if (c.closing && c.output.empty()) c.dead = true;
            }
            clients_.erase(std::remove_if(clients_.begin(), clients_.end(), [](const auto& c) { return c->dead; }), clients_.end());
            if (pollfds[0].revents & POLLIN) accept_clients();
            complete_engine();
            const auto now = Clock::now();
            advance_demo(now);
            if (now >= next_tick_) {
                publish_telemetry();
                next_tick_ += std::chrono::milliseconds(100);
                if (next_tick_ <= now) next_tick_ = now + std::chrono::milliseconds(100);
            }
        }
    }
private:
    void validate_point(comms::Point p) const {
        if (p.row < 0 || p.col < 0 || p.row >= rows_ || p.col >= cols_) throw std::runtime_error("start/goal outside AOI");
    }
    std::string hello() const {
        return "{\"type\":\"hello\",\"protocol\":\"mars-eva-edge/1\",\"frame_bytes\":24,\"telemetry_hz\":10,"
               "\"telemetry_kind\":\"planned_projection\",\"altitude_origin_m\":" + number(mission_.altitude_origin_m()) +
               ",\"engine\":" + info_ + "}";
    }
    std::string route_update() const {
        return "{\"type\":\"route_update\",\"cause\":" + comms::json_quote(cause_) +
               ",\"revision\":" + std::to_string(revision_) +
               ",\"resources\":{\"o2_remaining_kg\":" + number(mission_.o2_remaining_kg()) +
               ",\"battery_remaining_wh\":" + number(mission_.battery_remaining_wh()) +
               ",\"cumulative_dose_msv\":" + number(mission_.cumulative_dose_msv()) +
               ",\"projected_o2_fraction_of_initial\":" + number(mission_.projected_o2_fraction()) +
               ",\"projected_battery_fraction_of_initial\":" + number(mission_.projected_battery_fraction()) +
               "},\"result\":" + mission_.simulation_json() + "}";
    }
    std::string error(const std::string& message) const {
        auto p = mission_.position();
        return "{\"type\":\"error\",\"error\":" + comms::json_quote(message) +
               ",\"revision\":" + std::to_string(revision_) + ",\"position\":{\"row\":" +
               std::to_string(p.row) + ",\"col\":" + std::to_string(p.col) + "}}";
    }
    void broadcast(const std::string& text) {
        for (auto& c : clients_) if (c->upgraded && !c->dead && !c->closing) c->text(text);
    }
    void accept_clients() {
        for (int i = 0; i < 16; ++i) {
            int fd = ::accept4(listener_.fd, nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC);
            if (fd < 0) return;
            if (clients_.size() >= 8) { ::close(fd); continue; }
            clients_.push_back(std::make_unique<Client>(fd));
        }
    }
    void write_client(Client& c) {
        const auto size = std::min<std::size_t>(c.output.size(), 65536);
        const ssize_t n = ::send(c.socket.fd, c.output.data(), size, MSG_NOSIGNAL);
        if (n > 0) c.output.erase(0, static_cast<std::size_t>(n));
        else if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) c.dead = true;
    }
    void read_client(Client& c) {
        char buffer[4096];
        const ssize_t n = ::recv(c.socket.fd, buffer, sizeof(buffer), 0);
        if (n == 0) { c.dead = true; return; }
        if (n < 0) {
            if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) c.dead = true;
            return;
        }
        c.input.append(buffer, static_cast<std::size_t>(n));
        if (c.input.size() > 16384) { c.dead = true; return; }
        try {
            if (!c.upgraded) {
                auto end = c.input.find("\r\n\r\n");
                if (end == std::string::npos) {
                    if (c.input.size() > 8192) c.dead = true;
                    return;
                }
                c.queue(comms::upgrade_response(c.input.substr(0, end + 4)));
                c.input.erase(0, end + 4); c.upgraded = true;
                c.text(hello()); c.text(route_update());
            }
            for (int i = 0; i < 32 && !c.closing; ++i) {
                auto message = comms::parse_websocket(c.input);
                if (!message) break;
                if (Clock::now() - c.window > std::chrono::seconds(1)) { c.messages = 0; c.window = Clock::now(); }
                if (++c.messages > 32) { c.dead = true; break; }
                if (message->opcode == 8) {
                    c.queue(comms::websocket_message(8, "")); c.closing = true;
                } else if (message->opcode == 9) c.queue(comms::websocket_message(10, message->payload));
                else if (message->opcode == 1) control(c, message->payload);
            }
        } catch (const std::exception&) { c.dead = true; }
    }
    void control(Client& client, const std::string& text) {
        try {
            comms::check_json_depth(text);
            const auto doc = eva::Json::parse(text);
            const std::string type(doc.at("type").string());
            // Validate every field and range before mutating mission state.
            // Accepted updates never cancel a running engine process: the
            // reactor coalesces them and calculates the newest state next.
            comms::Point p;
            bool active = false;
            if (type == "position" || type == "set_goal") {
                if (doc.size() != 3) throw std::runtime_error("expected type,row,col only");
                p = {coordinate(doc.at("row"), rows_), coordinate(doc.at("col"), cols_)};
            } else if (type == "storm" || type == "radiation_peak" || type == "comms_lost") {
                if (doc.size() != 2) throw std::runtime_error("expected type,active only");
                active = doc.at("active").boolean();
            } else throw std::runtime_error("unknown control type");
            if (type == "comms_lost") {
                mission_.set_comms_lost(active);
            } else {
                if (type == "position") mission_.set_position(p);
                else if (type == "set_goal") mission_.set_goal(p);
                else if (type == "storm") mission_.set_storm(active);
                else mission_.set_radiation_peak(active);
                mission_.recalculate(); // idempotent event still refreshes the plan
                cause_ = type + ((!active && (type == "storm" || type == "radiation_peak")) ? "_clear" : "");
                ++desired_generation_;
                start_next_job();
            }
            client.text("{\"type\":\"event_ack\",\"event\":" + comms::json_quote(type) + "}");
        } catch (const std::exception& e) { client.text(error(e.what())); }
    }
    enum class JobKind { Movement, Plan };

    void start_next_job() {
        if (job_) return;
        job_ = std::make_unique<comms::EngineJob>();
        job_generation_ = desired_generation_;
        job_start_ = accounted_position_;
        if (accounted_position_ != mission_.position()) {
            job_kind_ = JobKind::Movement;
            job_goal_ = mission_.position();
        } else {
            job_kind_ = JobKind::Plan;
            job_start_ = mission_.position();
            job_goal_ = mission_.route_target();
        }
        adapter_.start_simulation(*job_, job_start_, job_goal_,
                                  mission_.o2_remaining_kg(), mission_.battery_remaining_wh());
    }

    void complete_engine() {
        if (!job_) return;
        try {
            if (auto result = job_->step()) {
                const auto kind = job_kind_;
                const auto generation = job_generation_;
                const auto goal = job_goal_;
                job_.reset();
                if (kind == JobKind::Movement) {
                    mission_.accept_movement(*result);
                    accounted_position_ = goal;
                    // A newer position may have arrived while this movement
                    // was evaluated. Only that newest position is retained.
                    start_next_job();
                } else if (generation == desired_generation_ &&
                           job_start_ == mission_.position() && goal == mission_.route_target()) {
                    mission_.accept_simulation(std::move(*result));
                    ++revision_;
                    broadcast(route_update());
                } else {
                    start_next_job(); // completed result is stale; do not admit it
                }
            }
        } catch (const std::exception& e) {
            job_.reset(); broadcast(error(e.what())); // mission remains stale
        }
    }
    void advance_demo(Clock::time_point now) {
        if (demo_step_ms_ <= 0 || job_ || now < next_demo_step_ || mission_.route_stale()) return;
        next_demo_step_ = now + std::chrono::milliseconds(demo_step_ms_);
        try {
            const auto result = eva::Json::parse(mission_.simulation_json());
            const auto& path = result.at("outbound").at("route").at("path").items();
            if (path.size() < 2) return;
            const auto& next = path[1];
            const comms::Point p{coordinate(next.at("row"), rows_), coordinate(next.at("col"), cols_)};
            mission_.set_position(p);
            mission_.recalculate();
            cause_ = "demo_position";
            ++desired_generation_;
            start_next_job();
        } catch (const std::exception& e) {
            broadcast(error(std::string("demo movement failed: ") + e.what()));
        }
    }
    void publish_telemetry() {
        const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - epoch_).count();
        auto frame = mission_.telemetry(seq_++, static_cast<std::uint32_t>(ms), dy_, dx_);
        const auto bytes = comms::encode_telemetry(frame);
        const auto wire = comms::websocket_message(2, std::string_view(reinterpret_cast<const char*>(bytes.data()), bytes.size()));
        for (auto& c : clients_) if (c->upgraded && !c->closing && !c->dead) c->queue(wire);
    }
    comms::EngineAdapter adapter_;
    comms::Mission mission_;
    std::unique_ptr<comms::EngineJob> job_;
    JobKind job_kind_ = JobKind::Plan;
    comms::Point accounted_position_, job_start_, job_goal_;
    Descriptor listener_;
    std::vector<std::unique_ptr<Client>> clients_;
    std::string info_, cause_ = "initial";
    std::uint64_t revision_ = 1;
    std::uint64_t desired_generation_ = 0, job_generation_ = 0;
    std::uint16_t seq_ = 0;
    int rows_ = 0, cols_ = 0, port_ = 0;
    double dy_ = 0, dx_ = 0;
    int demo_step_ms_ = 0;
    Clock::time_point epoch_, next_tick_;
    Clock::time_point next_demo_step_;
};

void usage() {
    std::cout << "Mars EVA Edge Bus — local binary WebSocket at 10 Hz\n"
              << "Usage: eva_bus --dir DIR --engine PATH [--port N] [--start ROW,COL] [--goal ROW,COL] [--demo-step-ms N]\n"
              << "Defaults: port 8765, start 500,500, goal 600,650. Port 0 selects an available port.\n"
              << "Binds 127.0.0.1 only. Android: adb reverse tcp:8765 tcp:8765\n"
              << "Connect: ws://127.0.0.1:8765/ (native clients; browser Origin rejected).\n"
              << "24-byte binary telemetry; JSON hello, controls, event_ack, route_update, error.\n"
              << "Controls: position/set_goal {row,col}; storm/radiation_peak/comms_lost {active}.\n"
              << "--demo-step-ms moves one route cell per interval; 0 (default) disables automatic movement.\n"
              << "PLSS telemetry is a planned projection, not measured live suit telemetry.\n"
              << "Engine timeout 5s. Up to 8 clients; 4096-byte unfragmented client messages.\n"
              << "SIGINT/SIGTERM shut down cleanly. Argument/startup errors exit 2 with JSON stderr.\n";
}
}

int main(int argc, char** argv) {
    try {
        std::signal(SIGINT, stop_handler); std::signal(SIGTERM, stop_handler);
        std::map<std::string, std::string> options;
        for (int i = 1; i < argc; ++i) {
            std::string key = argv[i];
            if (key == "--help") { usage(); return 0; }
            if (key != "--dir" && key != "--engine" && key != "--port" && key != "--start" &&
                key != "--goal" && key != "--demo-step-ms") throw std::runtime_error("unknown option: " + key);
            if (++i == argc) throw std::runtime_error("missing option value");
            if (!options.emplace(key, argv[i]).second) throw std::runtime_error("duplicate option: " + key);
        }
        if (!options.contains("--dir") || !options.contains("--engine")) throw std::runtime_error("--dir and --engine are required; use --help");
        const int port = options.contains("--port") ? integer(options.at("--port")) : 8765;
        if (port < 0 || port > 65535) throw std::runtime_error("port must be 0..65535");
        const auto start = point(options.contains("--start") ? options.at("--start") : "500,500");
        const auto goal = point(options.contains("--goal") ? options.at("--goal") : "600,650");
        const int demo_step_ms = options.contains("--demo-step-ms") ? integer(options.at("--demo-step-ms")) : 0;
        if (demo_step_ms < 0) throw std::runtime_error("demo-step-ms must be non-negative");
        Bus bus(options.at("--engine"), options.at("--dir"), port, start, goal, demo_step_ms);
        bus.run();
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "{\"ok\":false,\"error\":" << comms::json_quote(e.what()) << "}\n";
        return 2;
    }
}
