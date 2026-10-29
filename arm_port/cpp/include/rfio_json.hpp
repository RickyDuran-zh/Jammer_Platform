// Minimal flat-object JSON reader/writer used by the rfio daemon.
//
// The rfio protocol only carries flat objects with string / number / bool / null
// members (see docs/04_rfio_protocol.md), so a purpose-built parser keeps the
// daemon free of a JSON dependency on the board.  Living in a header lets
// cpp/tests/test_json.cpp exercise exactly the code the daemon runs - duplicated
// parsers rot silently, this one cannot.
//
// Deliberately NOT a general JSON implementation:
//   * nested arrays/objects are skipped, not materialised;
//   * numbers are always doubles;
//   * duplicate keys keep the last value.

#pragma once

#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <stdexcept>
#include <string>

namespace rfio {
namespace json {

struct Msg {
    std::map<std::string, double> num;
    std::map<std::string, std::string> str;
    std::map<std::string, bool> flag;

    double n(const std::string& key, double def) const {
        auto it = num.find(key);
        return it == num.end() ? def : it->second;
    }
    std::string s(const std::string& key, const std::string& def) const {
        auto it = str.find(key);
        return it == str.end() ? def : it->second;
    }
    bool b(const std::string& key, bool def) const {
        auto it = flag.find(key);
        return it == flag.end() ? def : it->second;
    }
    bool has(const std::string& key) const {
        return num.count(key) || str.count(key) || flag.count(key);
    }
};

class Parser {
  public:
    explicit Parser(const std::string& text) : t_(text) {}

    Msg parse() {
        Msg msg;
        ws();
        expect('{');
        ws();
        if (peek() == '}') {
            ++i_;
            ws();
            return msg;
        }
        while (true) {
            ws();
            const std::string key = parse_string();
            ws();
            expect(':');
            ws();
            parse_value(msg, key);
            ws();
            const char c = peek();
            if (c == '\0') {
                throw std::runtime_error("malformed JSON: unexpected end of input");
            }
            ++i_;
            if (c == '}') break;
            if (c != ',') throw std::runtime_error("malformed JSON: expected , or }");
        }
        return msg;
    }

  private:
    const std::string& t_;
    std::size_t i_ = 0;

    char peek() const { return i_ < t_.size() ? t_[i_] : '\0'; }

    void ws() {
        while (i_ < t_.size() && (t_[i_] == ' ' || t_[i_] == '\n' || t_[i_] == '\r' ||
                                  t_[i_] == '\t')) {
            ++i_;
        }
    }

    void expect(char c) {
        if (peek() != c) {
            throw std::runtime_error(std::string("malformed JSON: expected '") + c + "'");
        }
        ++i_;
    }

    std::string parse_string() {
        expect('"');
        std::string out;
        while (i_ < t_.size()) {
            const char c = t_[i_++];
            if (c == '"') return out;
            if (c == '\\' && i_ < t_.size()) {
                const char e = t_[i_++];
                switch (e) {
                    case 'n': out.push_back('\n'); break;
                    case 't': out.push_back('\t'); break;
                    case 'r': out.push_back('\r'); break;
                    case 'b': out.push_back('\b'); break;
                    case 'f': out.push_back('\f'); break;
                    case 'u':  // \uXXXX: this protocol only uses ASCII keys/values
                        for (int k = 0; k < 4 && i_ < t_.size(); ++k) {
                            ++i_;
                        }
                        out.push_back('?');
                        break;
                    default: out.push_back(e); break;
                }
            } else {
                out.push_back(c);
            }
        }
        throw std::runtime_error("malformed JSON: unterminated string");
    }

    void parse_value(Msg& msg, const std::string& key) {
        const char c = peek();
        if (c == '"') {
            msg.str[key] = parse_string();
            return;
        }
        if (c == 't' || c == 'f') {
            const bool value = (c == 't');
            if (t_.compare(i_, value ? 4 : 5, value ? "true" : "false") != 0) {
                throw std::runtime_error("malformed JSON: bad literal");
            }
            i_ += value ? 4 : 5;
            msg.flag[key] = value;
            return;
        }
        if (c == 'n') {
            i_ += 4;
            return;
        }
        if (c == '[' || c == '{') {
            skip_container();
            return;
        }
        std::size_t start = i_;
        while (i_ < t_.size() && std::strchr("+-0123456789.eE", t_[i_]) != nullptr) {
            ++i_;
        }
        if (start == i_) {
            throw std::runtime_error("malformed JSON: unexpected value");
        }
        msg.num[key] = std::strtod(t_.substr(start, i_ - start).c_str(), nullptr);
    }

    void skip_container() {
        int depth = 0;
        while (i_ < t_.size()) {
            const char c = t_[i_++];
            if (c == '"') {
                while (i_ < t_.size() && t_[i_] != '"') {
                    if (t_[i_] == '\\') ++i_;
                    ++i_;
                }
                ++i_;
                continue;
            }
            if (c == '[' || c == '{') ++depth;
            if (c == ']' || c == '}') {
                if (--depth == 0) return;
            }
        }
        throw std::runtime_error("malformed JSON: unterminated container");
    }
};

// Escape a string for inclusion in a JSON object value.
inline std::string escape(const std::string& in) {
    std::string out;
    out.reserve(in.size() + 8);
    for (char c : in) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x",
                                  static_cast<unsigned>(static_cast<unsigned char>(c)));
                    out += buf;
                } else {
                    out.push_back(c);
                }
        }
    }
    return out;
}

}  // namespace json
}  // namespace rfio
