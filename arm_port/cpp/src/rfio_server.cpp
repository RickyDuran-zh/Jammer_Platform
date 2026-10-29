// rfio daemon: owns the UHD streams and exposes them over localhost TCP.
//
// Protocol (see arm_port/docs/04_rfio_protocol.md):
//   control : one JSON object per line in, one JSON object per line out
//   rx      : {"cmd":"rx","nsamples":N,"timeout":T}
//             -> {"status":"ok","nsamples":N}\n + N*8 bytes of interleaved
//                little-endian float32 (complex64)
//   tx      : {"cmd":"tx","nsamples":N,"gain":G}\n + N*8 bytes
//             -> {"status":"ok","sent":N}\n
//
// Build: see CMakeLists.txt / arm_port/scripts/build_cpp.sh
// Run  : sudo jammer-rfio --port 5601 [--sim]
//
// The daemon never decides anything: the frame loop, the DSP, the interlock
// policy and every action come from the Python backend.  --sim serves synthetic
// IQ so the transport can be verified on a machine without a radio.

#include "rfio_json.hpp"
#include "rfio_radio.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <memory>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <random>
#include <stdexcept>
#include <string>
#include <vector>

// The protocol helpers live in rfio::json (cpp/include/rfio_json.hpp) so that
// cpp/tests can exercise the exact same parser.
namespace json = rfio::json;

namespace {

std::atomic<bool> g_stop{false};

void on_signal(int) {
    g_stop = true;
}

// ── socket helpers ───────────────────────────────────────────────────
class Conn {
  public:
    explicit Conn(int fd) : fd_(fd) {}

    bool read_line(std::string& out) {
        out.clear();
        char c = 0;
        while (true) {
            const ssize_t got = ::recv(fd_, &c, 1, 0);
            if (got == 0) return false;
            if (got < 0) {
                if (errno == EINTR) continue;
                return false;
            }
            if (c == '\n') return true;
            if (c != '\r') out.push_back(c);
            if (out.size() > (8u << 20)) throw std::runtime_error("line too long");
        }
    }

    bool read_exact(void* dst, std::size_t count) {
        char* p = static_cast<char*>(dst);
        std::size_t done = 0;
        while (done < count) {
            const ssize_t got = ::recv(fd_, p + done, count - done, 0);
            if (got == 0) return false;
            if (got < 0) {
                if (errno == EINTR) continue;
                return false;
            }
            done += static_cast<std::size_t>(got);
        }
        return true;
    }

    void send_all(const void* src, std::size_t count) {
        const char* p = static_cast<const char*>(src);
        std::size_t done = 0;
        while (done < count) {
            const ssize_t sent = ::send(fd_, p + done, count - done, MSG_NOSIGNAL);
            if (sent < 0) {
                if (errno == EINTR) continue;
                throw std::runtime_error("send failed");
            }
            done += static_cast<std::size_t>(sent);
        }
    }

    void send_line(const std::string& line) {
        send_all(line.data(), line.size());
        const char nl = '\n';
        send_all(&nl, 1);
    }

  private:
    int fd_;
};

std::string ok(const std::string& extra = "") {
    return "{\"status\":\"ok\"" + extra + "}";
}

std::string err(const std::string& message) {
    return "{\"status\":\"error\",\"error\":\"" + json::escape(message) + "\"}";
}

// ── simulated device ─────────────────────────────────────────────────
class SimRadio {
  public:
    explicit SimRadio(const rfio::RadioConfig& cfg) : cfg_(cfg), rng_(12345) {}

    void receive(std::complex<float>* out, std::size_t n) {
        const double amp = 0.02;
        for (std::size_t k = 0; k < n; ++k) {
            const double re = normal_(rng_) * amp;
            const double im = normal_(rng_) * amp;
            // one occupied channel so the Python sensing sees structure
            const double ph = 2.0 * 3.14159265358979 * 4.0e5 * (double)k / cfg_.sample_rate;
            out[k] = std::complex<float>(float(re + 0.3 * std::cos(ph)),
                                         float(im + 0.3 * std::sin(ph)));
        }
        ++rx_count_;
    }
    std::size_t transmit(const std::complex<float>*, std::size_t n, double gain) {
        ++tx_count_;
        last_gain_ = gain;
        return n;
    }
    std::string info_json() const {
        char buf[512];
        std::snprintf(buf, sizeof(buf),
                      "{\"rx_rate\":%.0f,\"tx_rate\":%.0f,\"rx_freq\":%.0f,"
                      "\"rx_gain\":%.1f,\"tx_gain\":%.1f,\"rx_antenna\":\"SIM\","
                      "\"tx_antenna\":\"SIM\",\"rx_overruns\":0,\"tx_underruns\":0,"
                      "\"mboard\":\"simulated\",\"daemon\":\"rfio-cpp-sim\","
                      "\"rx_count\":%llu,\"tx_count\":%llu}",
                      cfg_.sample_rate, cfg_.sample_rate, cfg_.center_freq, cfg_.rx_gain,
                      last_gain_, static_cast<unsigned long long>(rx_count_),
                      static_cast<unsigned long long>(tx_count_));
        return buf;
    }

  private:
    rfio::RadioConfig cfg_;
    std::mt19937 rng_;
    std::normal_distribution<double> normal_{0.0, 1.0};
    unsigned long long rx_count_ = 0;
    unsigned long long tx_count_ = 0;
    double last_gain_ = 0.0;
};

// ── session ──────────────────────────────────────────────────────────
class Session {
  public:
    Session(int fd, bool sim) : conn_(fd), sim_(sim) {
        if (sim_) sim_radio_.reset(new SimRadio(rfio::RadioConfig()));
    }

    void serve() {
        std::string line;
        while (!g_stop && conn_.read_line(line)) {
            if (line.empty()) continue;
            json::Msg msg;
            try {
                msg = json::Parser(line).parse();
            } catch (const std::exception& e) {
                conn_.send_line(err(std::string("bad request: ") + e.what()));
                continue;
            }
            const std::string cmd = msg.s("cmd", "");
            try {
                if (!dispatch(cmd, msg)) return;
            } catch (const std::exception& e) {
                conn_.send_line(err(e.what()));
            }
        }
    }

  private:
    bool dispatch(const std::string& cmd, const json::Msg& msg) {
        if (cmd == "open") {
            rfio::RadioConfig cfg;
            cfg.device_args = msg.s("device_args", cfg.device_args);
            cfg.master_clock_rate = msg.n("master_clock_rate", cfg.master_clock_rate);
            cfg.center_freq = msg.n("center_freq", cfg.center_freq);
            cfg.sample_rate = msg.n("sample_rate", cfg.sample_rate);
            cfg.num_samples = static_cast<std::size_t>(msg.n("num_samples", 4096));
            cfg.rx_gain = msg.n("rx_gain", cfg.rx_gain);
            cfg.tx_gain = msg.n("tx_gain", cfg.tx_gain);
            cfg.rx_antenna = msg.s("rx_antenna", cfg.rx_antenna);
            cfg.tx_antenna = msg.s("tx_antenna", cfg.tx_antenna);
            cfg.clock_source = msg.s("clock_source", cfg.clock_source);
            cfg.tdm = msg.b("tdm", cfg.tdm);
            cfg.rx_mode = msg.s("rx_mode", cfg.rx_mode);
            cfg.settle_sec = msg.n("settle_sec", cfg.settle_sec);
            cfg.recv_timeout_sec = msg.n("recv_timeout", cfg.recv_timeout_sec);
            if (sim_) {
                sim_radio_.reset(new SimRadio(cfg));
                buffer_.assign(cfg.num_samples, std::complex<float>(0, 0));
                conn_.send_line(ok(",\"info\":" + sim_radio_->info_json()));
            } else {
                radio_.open(cfg);
                buffer_.assign(cfg.num_samples, std::complex<float>(0, 0));
                conn_.send_line(ok(",\"info\":" + radio_.info_json()));
            }
            return true;
        }

        if (cmd == "rx") {
            const std::size_t n = static_cast<std::size_t>(msg.n("nsamples", 4096));
            if (buffer_.size() < n) buffer_.assign(n, std::complex<float>(0, 0));
            const double timeout = msg.n("timeout", 1.0);
            if (sim_) {
                sim_radio_->receive(buffer_.data(), n);
            } else {
                radio_.receive(buffer_.data(), n, timeout);
            }
            char head[96];
            std::snprintf(head, sizeof(head), "{\"status\":\"ok\",\"nsamples\":%zu}", n);
            conn_.send_line(head);
            conn_.send_all(buffer_.data(), n * sizeof(std::complex<float>));
            return true;
        }

        if (cmd == "tx") {
            const std::size_t n = static_cast<std::size_t>(msg.n("nsamples", 0));
            if (n == 0 || n > (1u << 22)) {
                conn_.send_line(err("invalid nsamples"));
                return true;
            }
            buffer_.assign(n, std::complex<float>(0, 0));
            if (!conn_.read_exact(buffer_.data(), n * sizeof(std::complex<float>))) {
                return false;
            }
            const double gain = msg.n("gain", 0.0);
            std::size_t sent = 0;
            if (sim_) {
                sent = sim_radio_->transmit(buffer_.data(), n, gain);
            } else {
                sent = radio_.transmit(buffer_.data(), n, gain);
            }
            char head[96];
            std::snprintf(head, sizeof(head), "{\"status\":\"ok\",\"sent\":%zu}", sent);
            conn_.send_line(head);
            return true;
        }

        if (cmd == "rebuild_rx") {
            if (!sim_) radio_.rebuild_rx();
            conn_.send_line(ok());
            return true;
        }

        if (cmd == "rebuild") {
            if (!sim_) radio_.rebuild();
            conn_.send_line(ok());
            return true;
        }

        if (cmd == "status") {
            conn_.send_line(ok(",\"info\":" + (sim_ ? sim_radio_->info_json()
                                                       : radio_.info_json())));
            return true;
        }

        if (cmd == "close") {
            if (!sim_) radio_.close();
            conn_.send_line(ok());
            return true;
        }

        if (cmd == "shutdown") {
            conn_.send_line(ok());
            g_stop = true;
            return false;
        }

        conn_.send_line(err("unknown cmd '" + cmd + "'"));
        return true;
    }

    Conn conn_;
    bool sim_;
    rfio::Radio radio_;
    std::unique_ptr<SimRadio> sim_radio_;
    std::vector<std::complex<float>> buffer_;
};

void usage() {
    std::printf("usage: jammer-rfio [--port 5601] [--host 127.0.0.1] [--sim] [--once]\n");
}

}  // namespace

int main(int argc, char** argv) {
    int port = 5601;
    std::string host = "127.0.0.1";
    bool sim = false;
    bool once = false;

    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--port" && i + 1 < argc) {
            port = std::atoi(argv[++i]);
        } else if (arg == "--host" && i + 1 < argc) {
            host = argv[++i];
        } else if (arg == "--sim") {
            sim = true;
        } else if (arg == "--once") {
            once = true;
        } else if (arg == "-h" || arg == "--help") {
            usage();
            return 0;
        } else {
            std::fprintf(stderr, "unknown argument: %s\n", arg.c_str());
            usage();
            return 2;
        }
    }

    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    std::signal(SIGPIPE, SIG_IGN);

    const int server = ::socket(AF_INET, SOCK_STREAM, 0);
    if (server < 0) {
        std::perror("socket");
        return 1;
    }
    int one = 1;
    ::setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port));
    if (::inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
        std::fprintf(stderr, "invalid host: %s\n", host.c_str());
        return 2;
    }
    if (::bind(server, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
        std::perror("bind");
        return 1;
    }
    if (::listen(server, 1) < 0) {
        std::perror("listen");
        return 1;
    }
    std::printf("[rfio] listening on %s:%d%s\n", host.c_str(), port,
                sim ? " (simulated device)" : "");
    std::fflush(stdout);

    while (!g_stop) {
        const int fd = ::accept(server, nullptr, nullptr);
        if (fd < 0) {
            if (errno == EINTR) continue;
            break;
        }
        ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        try {
            Session session(fd, sim);
            session.serve();
        } catch (const std::exception& e) {
            std::fprintf(stderr, "[rfio] session error: %s\n", e.what());
        }
        ::close(fd);
        if (once) break;
    }

    ::close(server);
    std::printf("[rfio] stopped\n");
    return 0;
}
