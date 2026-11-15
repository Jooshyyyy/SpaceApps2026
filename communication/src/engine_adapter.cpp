#include "comms/engine_adapter.hpp"
#include "engine/json.hpp"

#include <cerrno>
#include <csignal>
#include <cstring>
#include <fcntl.h>
#include <poll.h>
#include <stdexcept>
#include <sys/wait.h>
#include <unistd.h>
#include <utility>

namespace comms {
namespace {
std::string point(Point p) { return std::to_string(p.row) + "," + std::to_string(p.col); }
constexpr std::size_t kMaxOutput = 8 * 1024 * 1024;
}

EngineJob::~EngineJob() { cancel(); }
void EngineJob::cancel() {
    if (fd_ >= 0) { ::close(fd_); fd_ = -1; }
    if (child_ > 0) {
        ::kill(child_, SIGKILL);
        while (::waitpid(child_, nullptr, 0) < 0 && errno == EINTR) {}
        child_ = -1;
    }
}

void EngineJob::start(const std::vector<std::string>& args) {
    if (active() || args.empty()) throw std::runtime_error("invalid engine job start");
    std::vector<char*> argv;
    for (const auto& arg : args) argv.push_back(const_cast<char*>(arg.c_str()));
    argv.push_back(nullptr);
    int pipes[2];
    if (::pipe2(pipes, O_CLOEXEC) != 0) throw std::runtime_error("engine pipe failed");
    child_ = ::fork();
    if (child_ < 0) {
        ::close(pipes[0]); ::close(pipes[1]);
        throw std::runtime_error("engine fork failed");
    }
    if (child_ == 0) {
        ::close(pipes[0]);
        if (::dup2(pipes[1], STDOUT_FILENO) < 0 || ::dup2(pipes[1], STDERR_FILENO) < 0) _exit(126);
        ::close(pipes[1]);
        ::execvp(argv[0], argv.data());
        _exit(127);
    }
    ::close(pipes[1]);
    fd_ = pipes[0];
    if (::fcntl(fd_, F_SETFL, O_NONBLOCK) < 0) {
        cancel(); throw std::runtime_error("engine nonblocking pipe failed");
    }
    output_.clear(); eof_ = false;
    deadline_ = std::chrono::steady_clock::now() + std::chrono::seconds(5);
}

std::optional<std::string> EngineJob::step() {
    if (!active()) return std::nullopt;
    if (std::chrono::steady_clock::now() >= deadline_) {
        cancel(); throw std::runtime_error("engine timeout after 5 seconds");
    }
    // Bound work per reactor iteration, even if a faulty engine floods stdout.
    char buffer[8192];
    for (int reads = 0; !eof_ && reads < 32; ++reads) {
        const ssize_t n = ::read(fd_, buffer, sizeof(buffer));
        if (n > 0) {
            output_.append(buffer, static_cast<std::size_t>(n));
            if (output_.size() > kMaxOutput) {
                cancel(); throw std::runtime_error("engine output exceeds 8 MiB");
            }
        } else if (n == 0) {
            eof_ = true;
        } else if (errno == EAGAIN || errno == EWOULDBLOCK) {
            break;
        } else if (errno != EINTR) {
            cancel(); throw std::runtime_error("engine read failed");
        }
    }
    // Drain all pipe output before reaping, including multi-iteration results.
    if (!eof_) return std::nullopt;
    int status = 0;
    const pid_t waited = ::waitpid(child_, &status, WNOHANG);
    if (waited == 0 || (waited < 0 && errno == EINTR)) return std::nullopt;
    if (waited < 0) { cancel(); throw std::runtime_error("engine waitpid failed"); }
    child_ = -1;
    ::close(fd_); fd_ = -1;
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        throw std::runtime_error("engine execution failed: " + output_.substr(0, 1024));
    }
    return std::move(output_);
}

EngineAdapter::EngineAdapter(std::string engine_path, std::string data_dir)
    : engine_path_(std::move(engine_path)), data_dir_(std::move(data_dir)) {}

std::string EngineAdapter::run(const std::vector<std::string>& args) const {
    EngineJob job;
    job.start(args);
    while (true) {
        if (auto result = job.step()) return *result;
        ::poll(nullptr, 0, 5); // startup only; live requests use the socket reactor
    }
}

std::string EngineAdapter::info() const {
    auto output = run({engine_path_, "info", "--dir", data_dir_});
    const auto result = eva::Json::parse(output);
    if (!result.at("ok").boolean()) throw std::runtime_error("engine info returned ok=false");
    return output;
}

std::vector<std::string> EngineAdapter::simulation_args(Point start, Point goal) const {
    return {engine_path_, "sim", "--dir", data_dir_, "--start", point(start),
            "--goal", point(goal), "--path"};
}
std::string EngineAdapter::simulate(Point start, Point goal) const {
    return run(simulation_args(start, goal));
}
void EngineAdapter::start_simulation(EngineJob& job, Point start, Point goal) const {
    job.start(simulation_args(start, goal));
}
} // namespace comms
