#include "engine/json.hpp"

#include <cctype>
#include <cstdlib>
#include <string>
#include <utility>

namespace eva {
namespace {

class Parser {
public:
    explicit Parser(std::string_view text) : text_(text) {}

    Json parse_document() {
        skip_ws();
        Json value = parse_value();
        skip_ws();
        if (pos_ != text_.size()) fail("trailing characters after the JSON value");
        return value;
    }

private:
    [[noreturn]] void fail(const std::string& message) const {
        throw JsonError(message + " at offset " + std::to_string(pos_));
    }

    void skip_ws() {
        while (pos_ < text_.size()) {
            const char c = text_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                ++pos_;
                continue;
            }
            break;
        }
    }

    char peek() const { return pos_ < text_.size() ? text_[pos_] : '\0'; }

    bool consume(char c) {
        if (peek() == c) {
            ++pos_;
            return true;
        }
        return false;
    }

    void expect(char c) {
        if (!consume(c)) fail(std::string("expected '") + c + "'");
    }

    Json parse_value() {
        skip_ws();
        if (pos_ >= text_.size()) fail("unexpected end of input");
        const char c = text_[pos_];
        if (c == '{') return parse_object();
        if (c == '[') return parse_array();
        if (c == '"') return Json::make_string(parse_string());
        if (c == 't') {
            parse_literal("true");
            return Json::make_bool(true);
        }
        if (c == 'f') {
            parse_literal("false");
            return Json::make_bool(false);
        }
        if (c == 'n') {
            parse_literal("null");
            return Json();
        }
        if (c == '-' || (c >= '0' && c <= '9')) return parse_number();
        fail("unexpected character");
    }

    Json parse_object() {
        expect('{');
        Json object = Json::make_object();
        skip_ws();
        if (consume('}')) return object;
        while (true) {
            skip_ws();
            if (peek() != '"') fail("expected an object key");
            std::string key = parse_string();
            skip_ws();
            expect(':');
            object.set(std::move(key), parse_value());
            skip_ws();
            if (consume(',')) continue;
            expect('}');
            return object;
        }
    }

    Json parse_array() {
        expect('[');
        Json array = Json::make_array();
        skip_ws();
        if (consume(']')) return array;
        while (true) {
            array.push_back(parse_value());
            skip_ws();
            if (consume(',')) continue;
            expect(']');
            return array;
        }
    }

    std::string parse_string() {
        expect('"');
        std::string out;
        while (true) {
            if (pos_ >= text_.size()) fail("unterminated string");
            const char c = text_[pos_++];
            if (c == '"') return out;
            if (c != '\\') {
                if (static_cast<unsigned char>(c) < 0x20) fail("control character in string");
                out.push_back(c);
                continue;
            }
            if (pos_ >= text_.size()) fail("unterminated escape sequence");
            const char esc = text_[pos_++];
            switch (esc) {
                case '"': out.push_back('"'); break;
                case '\\': out.push_back('\\'); break;
                case '/': out.push_back('/'); break;
                case 'b': out.push_back('\b'); break;
                case 'f': out.push_back('\f'); break;
                case 'n': out.push_back('\n'); break;
                case 'r': out.push_back('\r'); break;
                case 't': out.push_back('\t'); break;
                case 'u': append_utf8(out, parse_hex4()); break;
                default: fail("unknown escape sequence");
            }
        }
    }

    unsigned parse_hex4() {
        unsigned value = 0;
        for (int i = 0; i < 4; ++i) {
            if (pos_ >= text_.size()) fail("truncated \\u escape");
            const char c = text_[pos_++];
            value <<= 4;
            if (c >= '0' && c <= '9') {
                value |= static_cast<unsigned>(c - '0');
            } else if (c >= 'a' && c <= 'f') {
                value |= static_cast<unsigned>(c - 'a' + 10);
            } else if (c >= 'A' && c <= 'F') {
                value |= static_cast<unsigned>(c - 'A' + 10);
            } else {
                fail("invalid hex digit in \\u escape");
            }
        }
        return value;
    }

    // Encodes one BMP code point. Layer 1 never emits surrogate pairs, so this
    // deliberately does not combine them.
    static void append_utf8(std::string& out, unsigned code_point) {
        if (code_point < 0x80) {
            out.push_back(static_cast<char>(code_point));
            return;
        }
        if (code_point < 0x800) {
            out.push_back(static_cast<char>(0xC0 | (code_point >> 6)));
            out.push_back(static_cast<char>(0x80 | (code_point & 0x3F)));
            return;
        }
        out.push_back(static_cast<char>(0xE0 | (code_point >> 12)));
        out.push_back(static_cast<char>(0x80 | ((code_point >> 6) & 0x3F)));
        out.push_back(static_cast<char>(0x80 | (code_point & 0x3F)));
    }

    Json parse_number() {
        const std::size_t start = pos_;
        if (peek() == '-') ++pos_;
        while (pos_ < text_.size() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) ++pos_;
        if (peek() == '.') {
            ++pos_;
            while (pos_ < text_.size() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) ++pos_;
        }
        if (peek() == 'e' || peek() == 'E') {
            ++pos_;
            if (peek() == '+' || peek() == '-') ++pos_;
            while (pos_ < text_.size() && std::isdigit(static_cast<unsigned char>(text_[pos_]))) ++pos_;
        }
        const std::string token(text_.substr(start, pos_ - start));
        char* end = nullptr;
        const double value = std::strtod(token.c_str(), &end);
        if (end == token.c_str() || *end != '\0') fail("invalid number");
        return Json::make_number(value);
    }

    void parse_literal(std::string_view literal) {
        if (text_.compare(pos_, literal.size(), literal) != 0) fail("invalid literal");
        pos_ += literal.size();
    }

    std::string_view text_;
    std::size_t pos_ = 0;
};

} // namespace

Json Json::parse(std::string_view text) { return Parser(text).parse_document(); }

Json Json::make_bool(bool value) {
    Json json;
    json.type_ = Type::Bool;
    json.bool_ = value;
    return json;
}

Json Json::make_number(double value) {
    Json json;
    json.type_ = Type::Number;
    json.number_ = value;
    return json;
}

Json Json::make_string(std::string value) {
    Json json;
    json.type_ = Type::String;
    json.string_ = std::move(value);
    return json;
}

Json Json::make_array() {
    Json json;
    json.type_ = Type::Array;
    return json;
}

Json Json::make_object() {
    Json json;
    json.type_ = Type::Object;
    return json;
}

void Json::push_back(Json value) {
    if (type_ != Type::Array) throw JsonError("not an array");
    array_.push_back(std::move(value));
}

void Json::set(std::string key, Json value) {
    if (type_ != Type::Object) throw JsonError("not an object");
    object_[std::move(key)] = std::move(value);
}

bool Json::has(std::string_view key) const {
    if (type_ != Type::Object) return false;
    return object_.find(std::string(key)) != object_.end();
}

const Json& Json::at(std::string_view key) const {
    if (type_ != Type::Object) throw JsonError("not an object");
    const auto it = object_.find(std::string(key));
    if (it == object_.end()) throw JsonError("missing key '" + std::string(key) + "'");
    return it->second;
}

const Json& Json::at(std::size_t index) const {
    if (type_ != Type::Array) throw JsonError("not an array");
    if (index >= array_.size()) throw JsonError("array index out of range");
    return array_[index];
}

std::size_t Json::size() const {
    if (type_ == Type::Array) return array_.size();
    if (type_ == Type::Object) return object_.size();
    return 0;
}

bool Json::boolean() const {
    if (type_ != Type::Bool) throw JsonError("not a boolean");
    return bool_;
}

double Json::number() const {
    if (type_ != Type::Number) throw JsonError("not a number");
    return number_;
}

double Json::number_or(double fallback) const { return type_ == Type::Number ? number_ : fallback; }

std::string_view Json::string() const {
    if (type_ != Type::String) throw JsonError("not a string");
    return string_;
}

const std::vector<Json>& Json::items() const {
    if (type_ != Type::Array) throw JsonError("not an array");
    return array_;
}

} // namespace eva
