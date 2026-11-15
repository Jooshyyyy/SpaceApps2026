// Minimal, dependency-free JSON reader.
//
// Layer 2 must read data/processed/aoi.json and honour its metadata instead of
// inferring grid shape from file sizes. Pulling a JSON library into a hackathon
// build is not worth it, so this is a compact recursive-descent parser covering
// the subset layer 1 emits (and the general JSON grammar, so a future layer 1
// change does not break the engine).
#pragma once

#include <map>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace eva {

class JsonError : public std::runtime_error {
public:
    explicit JsonError(const std::string& what) : std::runtime_error(what) {}
};

class Json {
public:
    enum class Type { Null, Bool, Number, String, Array, Object };

    Json() = default;

    static Json parse(std::string_view text);

    static Json make_bool(bool value);
    static Json make_number(double value);
    static Json make_string(std::string value);
    static Json make_array();
    static Json make_object();

    void push_back(Json value);            // array append
    void set(std::string key, Json value); // object assign

    Type type() const { return type_; }
    bool is_null() const { return type_ == Type::Null; }
    bool is_bool() const { return type_ == Type::Bool; }
    bool is_number() const { return type_ == Type::Number; }
    bool is_string() const { return type_ == Type::String; }
    bool is_array() const { return type_ == Type::Array; }
    bool is_object() const { return type_ == Type::Object; }

    bool has(std::string_view key) const;
    const Json& at(std::string_view key) const; // throws JsonError when absent
    const Json& at(std::size_t index) const;    // throws JsonError when out of range
    const Json& operator[](std::string_view key) const { return at(key); }
    const Json& operator[](std::size_t index) const { return at(index); }
    std::size_t size() const;

    bool boolean() const;                 // throws when not a bool
    double number() const;                // throws when not a number
    double number_or(double fallback) const;
    std::string_view string() const;      // throws when not a string
    const std::vector<Json>& items() const; // throws when not an array

private:
    Type type_ = Type::Null;
    bool bool_ = false;
    double number_ = 0.0;
    std::string string_;
    std::vector<Json> array_;
    std::map<std::string, Json> object_;
};

} // namespace eva
