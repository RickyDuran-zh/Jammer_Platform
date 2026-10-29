// UHD wrapper used by the rfio daemon.
//
// Mirrors arm_port/python/jammer_arm/radio/uhd_device.py one-to-one so both
// transports behave identically: same configuration order, same TDM RX pause,
// same metadata error classification, same "rebuild RX, then rebuild chain"
// recovery ladder.

#pragma once

#include <complex>
#include <cstddef>
#include <string>

namespace rfio {

struct RadioConfig {
    std::string device_args = "type=x300,addr=192.168.10.2";
    double master_clock_rate = 200e6;
    double center_freq = 3.0e9;
    double sample_rate = 20e6;
    std::size_t num_samples = 4096;
    double rx_gain = 15.0;
    double tx_gain = 0.0;
    double tx_gain_min = 0.0;
    double tx_gain_max = 10.0;
    std::string rx_antenna = "TX/RX";
    std::string tx_antenna = "TX/RX";
    std::string clock_source = "internal";
    bool tdm = true;
    // "burst" requests exactly num_samples per frame (STREAM_MODE_NUM_SAMPS_AND_DONE)
    // so a ~12 fps loop cannot overflow a 20 MS/s stream.  "continuous" is for
    // experiments that drain faster than the sample rate.
    std::string rx_mode = "burst";
    double settle_sec = 0.5;
    double recv_timeout_sec = 1.0;
};

class Radio {
  public:
    Radio() = default;
    ~Radio();
    Radio(const Radio&) = delete;
    Radio& operator=(const Radio&) = delete;

    // Throws std::runtime_error on failure.
    void open(const RadioConfig& cfg);
    void close();
    void rebuild_rx();
    void rebuild();

    // Number of samples written into out; throws on timeout/overflow.
    std::size_t receive(std::complex<float>* out, std::size_t n, double timeout_sec);
    // Number of samples accepted by the device.
    std::size_t transmit(const std::complex<float>* in, std::size_t n, double gain_db);

    bool is_open() const { return open_; }
    const RadioConfig& config() const { return cfg_; }
    // Flat JSON object with the applied values plus counters.
    std::string info_json() const;

  private:
    void start_rx();
    void stop_rx();
    void apply_tx_gain(double gain);

    RadioConfig cfg_;
    bool open_ = false;
    bool rx_running_ = false;

    // Opaque UHD handles are kept out of the header to keep this file readable.
    struct Impl;
    Impl* impl_ = nullptr;

    unsigned long long rx_overruns_ = 0;
    unsigned long long tx_underruns_ = 0;
    double applied_rx_gain_ = 0.0;
    double applied_tx_gain_ = 0.0;
};

}  // namespace rfio
