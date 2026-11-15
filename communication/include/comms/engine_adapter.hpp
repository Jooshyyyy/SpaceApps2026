#pragma once

#include "comms/mission.hpp"
#include <chrono>
#include <optional>
#include <string>
#include <vector>
#include <sys/types.h>

namespace comms {

// One bounded, nonblocking child. No shell, no threads. Cancellation always
// reaps the child; sockets and pipes have close-on-exec set by their owners.
class EngineJob {
public:
    EngineJob() = default;
    ~EngineJob();
    EngineJob(const EngineJob&) = delete;
    EngineJob& operator=(const EngineJob&) = delete;
    void start(const std::vector<std::string>& args);
    std::optional<std::string> step();
    bool active() const { return child_ > 0; }
private:
    void cancel();
    pid_t child_ = -1;
    int fd_ = -1;
    bool eof_ = false;
    std::string output_;
    std::chrono::steady_clock::time_point deadline_;
};

class EngineAdapter {
public:
    EngineAdapter(std::string engine_path, std::string data_dir);
    std::string info() const;
    std::string simulate(Point start, Point goal, double o2_kg = 1.20,
                         double battery_wh = 250.0) const;
    void start_simulation(EngineJob& job, Point start, Point goal, double o2_kg,
                          double battery_wh) const;
private:
    std::string run(const std::vector<std::string>& args) const;
    std::vector<std::string> simulation_args(Point start, Point goal, double o2_kg,
                                              double battery_wh) const;
    std::string engine_path_;
    std::string data_dir_;
};
} // namespace comms
