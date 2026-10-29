// UHD implementation of the rfio radio wrapper.

#include "rfio_radio.hpp"

#include <uhd/stream.hpp>
#include <uhd/types/stream_cmd.hpp>
#include <uhd/types/tune_request.hpp>
#include <uhd/usrp/multi_usrp.hpp>
#include <uhd/version.hpp>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <vector>

namespace rfio {

struct Radio::Impl {
    uhd::usrp::multi_usrp::sptr usrp;
    uhd::rx_streamer::sptr rx;
    uhd::tx_streamer::sptr tx;
    uhd::rx_metadata_t rx_md;
    uhd::tx_metadata_t tx_md;
};

namespace {

void check(const uhd::rx_metadata_t& md) {
    switch (md.error_code) {
        case uhd::rx_metadata_t::ERROR_CODE_NONE:
            return;
        case uhd::rx_metadata_t::ERROR_CODE_TIMEOUT:
            throw std::runtime_error("recv timeout (no samples from device)");
        case uhd::rx_metadata_t::ERROR_CODE_OVERFLOW:
            throw std::runtime_error("recv overflow (host could not keep up)");
        case uhd::rx_metadata_t::ERROR_CODE_LATE_COMMAND:
            throw std::runtime_error("recv late command");
        case uhd::rx_metadata_t::ERROR_CODE_BROKEN_CHAIN:
            throw std::runtime_error("recv broken chain");
        case uhd::rx_metadata_t::ERROR_CODE_ALIGNMENT:
            throw std::runtime_error("recv alignment error");
        case uhd::rx_metadata_t::ERROR_CODE_BAD_PACKET:
            throw std::runtime_error("recv bad packet");
        default:
            throw std::runtime_error("recv unknown error");
    }
}

std::string esc(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 2);
    for (char c : s) {
        if (c == '"' || c == '\\') {
            out.push_back('\\');
        }
        out.push_back(c);
    }
    return out;
}

}  // namespace

Radio::~Radio() {
    close();
    delete impl_;
    impl_ = nullptr;
}

void Radio::open(const RadioConfig& cfg) {
    close();
    cfg_ = cfg;
    if (!impl_) {
        impl_ = new Impl();
    }

    try {
        impl_->usrp = uhd::usrp::multi_usrp::make(cfg.device_args);
        impl_->usrp->set_master_clock_rate(cfg.master_clock_rate, 0);
        if (!cfg.clock_source.empty()) {
            impl_->usrp->set_clock_source(cfg.clock_source, 0);
        }

        // RX chain
        impl_->usrp->set_rx_rate(cfg.sample_rate, 0);
        impl_->usrp->set_rx_freq(uhd::tune_request_t(cfg.center_freq), 0);
        impl_->usrp->set_rx_gain(cfg.rx_gain, "", 0);
        if (!cfg.rx_antenna.empty()) {
            impl_->usrp->set_rx_antenna(cfg.rx_antenna, 0);
        }

        // TX chain
        impl_->usrp->set_tx_rate(cfg.sample_rate, 0);
        impl_->usrp->set_tx_freq(uhd::tune_request_t(cfg.center_freq), 0);
        impl_->usrp->set_tx_gain(cfg.tx_gain, "", 0);
        if (!cfg.tx_antenna.empty()) {
            impl_->usrp->set_tx_antenna(cfg.tx_antenna, 0);
        }

        uhd::stream_args_t args("fc32", "sc16");
        args.channels.push_back(0);
        impl_->rx = impl_->usrp->get_rx_stream(args);
        impl_->tx = impl_->usrp->get_tx_stream(args);

        applied_rx_gain_ = impl_->usrp->get_rx_gain("", 0);
        applied_tx_gain_ = impl_->usrp->get_tx_gain("", 0);

        std::this_thread::sleep_for(
            std::chrono::duration<double>(std::max(0.0, cfg.settle_sec)));

        if (cfg.rx_mode == "continuous") {
            start_rx();
            // Discard whatever the device queued during settling.
            std::vector<std::complex<float>> scratch(cfg.num_samples);
            try {
                impl_->rx->recv(&scratch.front(), cfg.num_samples, impl_->rx_md, 0.05, true);
            } catch (...) {
                // Drain is best effort.
            }
        }
    } catch (const std::exception& e) {
        close();
        throw std::runtime_error(std::string("USRP open/config failed: ") + e.what());
    }

    open_ = true;
}

void Radio::close() {
    if (impl_) {
        try {
            stop_rx();
        } catch (...) {
        }
        impl_->rx.reset();
        impl_->tx.reset();
        impl_->usrp.reset();
    }
    open_ = false;
    rx_running_ = false;
}

void Radio::start_rx() {
    if (!impl_ || !impl_->rx || rx_running_) {
        return;
    }
    uhd::stream_cmd_t cmd(uhd::stream_cmd_t::STREAM_MODE_START_CONTINUOUS);
    cmd.stream_now = true;
    impl_->rx->issue_stream_cmd(cmd);
    rx_running_ = true;
}

void Radio::stop_rx() {
    if (!impl_ || !impl_->rx || !rx_running_) {
        return;
    }
    uhd::stream_cmd_t cmd(uhd::stream_cmd_t::STREAM_MODE_STOP_CONTINUOUS);
    impl_->rx->issue_stream_cmd(cmd);
    rx_running_ = false;
}

void Radio::rebuild_rx() {
    if (!impl_ || !impl_->usrp) {
        open(cfg_);
        return;
    }
    stop_rx();
    impl_->rx.reset();
    uhd::stream_args_t args("fc32", "sc16");
    args.channels.push_back(0);
    impl_->rx = impl_->usrp->get_rx_stream(args);
    start_rx();
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
}

void Radio::rebuild() {
    open(cfg_);
}

std::size_t Radio::receive(std::complex<float>* out, std::size_t n, double timeout_sec) {
    if (!open_ || !impl_ || !impl_->rx) {
        throw std::runtime_error("RX stream is not open");
    }
    const bool burst = cfg_.rx_mode != "continuous";
    if (burst) {
        uhd::stream_cmd_t cmd(uhd::stream_cmd_t::STREAM_MODE_NUM_SAMPS_AND_DONE);
        cmd.stream_now = true;
        cmd.num_samps = n;
        impl_->rx->issue_stream_cmd(cmd);
    } else if (!rx_running_) {
        start_rx();
    }

    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::duration<double>(timeout_sec + 1.0);
    std::size_t filled = 0;
    while (filled < n) {
        std::size_t got = 0;
        try {
            got = impl_->rx->recv(out + filled, n - filled, impl_->rx_md, timeout_sec);
            check(impl_->rx_md);
        } catch (const std::exception&) {
            if (impl_->rx_md.error_code == uhd::rx_metadata_t::ERROR_CODE_OVERFLOW) {
                ++rx_overruns_;
            }
            throw;
        }
        filled += got;
        if (burst) {
            break;  // the burst arrives as one block
        }
        if (got == 0 && std::chrono::steady_clock::now() > deadline) {
            break;
        }
    }
    if (filled < n) {
        std::ostringstream oss;
        oss << "short recv: " << filled << "/" << n;
        throw std::runtime_error(oss.str());
    }
    return filled;
}

void Radio::apply_tx_gain(double gain) {
    const double clamped = std::min(std::max(gain, cfg_.tx_gain_min), cfg_.tx_gain_max);
    impl_->usrp->set_tx_gain(clamped, "", 0);
    applied_tx_gain_ = clamped;
}

std::size_t Radio::transmit(const std::complex<float>* in, std::size_t n, double gain_db) {
    if (!open_ || !impl_ || !impl_->tx) {
        throw std::runtime_error("TX stream is not open");
    }
    apply_tx_gain(gain_db);

    const bool tdm = cfg_.tdm && cfg_.rx_mode == "continuous";
    if (tdm) {
        stop_rx();
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }

    impl_->tx_md.start_of_burst = true;
    impl_->tx_md.end_of_burst = true;
    impl_->tx_md.has_time_spec = false;

    std::size_t sent_total = 0;
    try {
        while (sent_total < n) {
            const std::size_t sent =
                impl_->tx->send(in + sent_total, n - sent_total, impl_->tx_md,
                                std::max(0.2, cfg_.recv_timeout_sec));
            if (sent == 0) {
                ++tx_underruns_;
                throw std::runtime_error("TX send returned 0 samples (underrun)");
            }
            sent_total += sent;
            impl_->tx_md.start_of_burst = false;
        }
    } catch (...) {
        if (tdm) {
            try {
                start_rx();
            } catch (...) {
                rx_running_ = false;
            }
        }
        throw;
    }

    if (tdm) {
        start_rx();
    }
    return sent_total;
}

std::string Radio::info_json() const {
    std::ostringstream oss;
    oss.setf(std::ios::fixed);
    oss.precision(3);
    std::string mboard = "unknown";
    double rx_rate = cfg_.sample_rate;
    double tx_rate = cfg_.sample_rate;
    double rx_freq = cfg_.center_freq;
    if (impl_ && impl_->usrp) {
        try {
            mboard = impl_->usrp->get_mboard_name(0);
            rx_rate = impl_->usrp->get_rx_rate(0);
            tx_rate = impl_->usrp->get_tx_rate(0);
            rx_freq = impl_->usrp->get_rx_freq(0);
        } catch (...) {
        }
    }
    oss << "{\"rx_rate\":" << rx_rate
        << ",\"tx_rate\":" << tx_rate
        << ",\"rx_freq\":" << rx_freq
        << ",\"rx_gain\":" << applied_rx_gain_
        << ",\"tx_gain\":" << applied_tx_gain_
        << ",\"rx_antenna\":\"" << esc(cfg_.rx_antenna) << "\""
        << ",\"tx_antenna\":\"" << esc(cfg_.tx_antenna) << "\""
        << ",\"rx_overruns\":" << rx_overruns_
        << ",\"tx_underruns\":" << tx_underruns_
        << ",\"mboard\":\"" << esc(mboard) << "\""
        << ",\"uhd_version\":\"" << esc(uhd::get_version_string()) << "\""
        << ",\"daemon\":\"rfio-cpp\"}";
    return oss.str();
}

}  // namespace rfio
