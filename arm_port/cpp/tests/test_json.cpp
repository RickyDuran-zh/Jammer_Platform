// Host-runnable tests for the rfio JSON reader/writer.
//
//   bash arm_port/scripts/test_cpp.sh
//
// No UHD, no sockets, no test framework: the daemon's wire format is the part
// most likely to break silently, so it is verified on any machine with a C++17
// compiler (including the board itself).

#include "rfio_json.hpp"

#include <cmath>
#include <cstdio>
#include <string>

namespace {

int g_checks = 0;
int g_failures = 0;

void check(bool ok, const std::string& what, const std::string& detail = "") {
    ++g_checks;
    if (!ok) {
        ++g_failures;
        std::printf("[FAIL] %s%s%s\n", what.c_str(), detail.empty() ? "" : " - ",
                    detail.c_str());
    }
}

void check_near(double got, double want, const std::string& what) {
    check(std::fabs(got - want) < 1e-12, what,
          "got " + std::to_string(got) + " want " + std::to_string(want));
}

void check_eq(const std::string& got, const std::string& want, const std::string& what) {
    check(got == want, what, "got '" + got + "' want '" + want + "'");
}

bool throws(const std::string& text) {
    try {
        rfio::json::Parser(text).parse();
    } catch (const std::exception&) {
        return true;
    }
    return false;
}

}  // namespace

int main() {
    using rfio::json::Msg;
    using rfio::json::Parser;

    // ── open request: mixed strings, numbers, bools ──────────────────
    {
        const Msg m = Parser(
            "{\"cmd\":\"open\",\"device_args\":\"type=x300,addr=192.168.10.2\","
            "\"sample_rate\":20000000.0,\"num_samples\":4096,\"rx_gain\":15,"
            "\"tdm\":true,\"settle_sec\":0.5}")
                          .parse();
        check_eq(m.s("cmd", ""), "open", "open.cmd");
        check_eq(m.s("device_args", ""), "type=x300,addr=192.168.10.2", "open.device_args");
        check_near(m.n("sample_rate", 0), 20000000.0, "open.sample_rate");
        check_near(m.n("num_samples", 0), 4096.0, "open.num_samples");
        check_near(m.n("rx_gain", 0), 15.0, "open.rx_gain (integer literal)");
        check_near(m.n("settle_sec", 0), 0.5, "open.settle_sec");
        check(m.b("tdm", false), "open.tdm=true");
        check(m.b("tdm", false), "open.tdm still true on re-read");
    }

    // ── defaults when the key is absent ─────────────────────────────
    {
        const Msg m = Parser("{\"cmd\":\"status\"}").parse();
        check_eq(m.s("cmd", ""), "status", "status.cmd");
        check_near(m.n("port", 5601), 5601, "default number");
        check_eq(m.s("host", "127.0.0.1"), "127.0.0.1", "default string");
        check(m.b("verbose", true), "default bool");
        check(!m.has("port"), "has() reports absent key");
    }

    // ── rx / tx requests exactly as the Python client sends them ────
    {
        const Msg rx = Parser("{\"cmd\":\"rx\",\"nsamples\":4096,\"timeout\":1.0}").parse();
        check_eq(rx.s("cmd", ""), "rx", "rx.cmd");
        check_near(rx.n("nsamples", 0), 4096.0, "rx.nsamples");
        check_near(rx.n("timeout", 0), 1.0, "rx.timeout");

        const Msg tx = Parser(
            "{\"cmd\":\"tx\",\"nsamples\":4096,\"gain\":5.0,"
            "\"start_of_burst\":true,\"end_of_burst\":true}")
                          .parse();
        check_eq(tx.s("cmd", ""), "tx", "tx.cmd");
        check_near(tx.n("gain", 0), 5.0, "tx.gain");
        check(tx.b("start_of_burst", false), "tx.start_of_burst");
        check(tx.b("end_of_burst", false), "tx.end_of_burst");
    }

    // ── number formats ──────────────────────────────────────────────
    {
        const Msg m = Parser(
            "{\"a\":-12,\"b\":1e9,\"c\":-2.5e-3,\"d\":3.0E6,\"e\":0}")
                          .parse();
        check_near(m.n("a", 0), -12.0, "negative integer");
        check_near(m.n("b", 0), 1e9, "positive exponent");
        check_near(m.n("c", 0), -2.5e-3, "negative exponent");
        check_near(m.n("d", 0), 3.0e6, "upper-case exponent");
        check_near(m.n("e", 0), 0.0, "zero");
    }

    // ── escaped strings ─────────────────────────────────────────────
    {
        const Msg m = Parser("{\"msg\":\"recv \\\"timeout\\\"\\nnext\\ttab\"}").parse();
        check_eq(m.s("msg", ""), "recv \"timeout\"\nnext\ttab", "decoded escapes");

        const std::string encoded = rfio::json::escape("recv \"timeout\"\nnext");
        check_eq(encoded, "recv \\\"timeout\\\"\\nnext", "escape()");
        const Msg round = Parser("{\"e\":\"" + encoded + "\"}").parse();
        check_eq(round.s("e", ""), "recv \"timeout\"\nnext", "escape/parse round-trip");
    }

    // ── nested values are skipped, later keys still parsed ──────────
    {
        const Msg m = Parser(
            "{\"channels\":[0,1,2],\"meta\":{\"a\":\"b\",\"c\":[1,2]},\"cmd\":\"status\"}")
                          .parse();
        check_eq(m.s("cmd", ""), "status", "key after nested containers");
        check(!m.has("channels"), "nested array not materialised");
    }

    // ── whitespace tolerance ────────────────────────────────────────
    {
        const Msg m = Parser("{\n  \"cmd\" : \"rx\" ,\n  \"nsamples\": 512\n}\n").parse();
        check_eq(m.s("cmd", ""), "rx", "pretty-printed request");
        check_near(m.n("nsamples", 0), 512, "pretty-printed number");
    }

    // ── malformed input must throw, never guess ────────────────────
    check(throws("{\"cmd\" \"rx\"}"), "missing colon throws");
    check(throws("{\"cmd\":\"rx\""), "unterminated object throws");
    check(throws("{\"cmd\":\"rx}"), "unterminated string throws");
    check(throws("{\"cmd\":}"), "missing value throws");
    check(throws("{\"cmd\":rx}"), "bare word value throws");
    check(throws("{\"cmd\":\"rx\",}"), "trailing comma throws");
    check(throws(""), "empty input throws");
    check(!throws("{}"), "empty object is valid");

    std::printf("%d checks, %d failure(s)\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}
