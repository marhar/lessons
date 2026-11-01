// REST front end for VictoriaMetrics that shapes the data with DuckDB.
//
// C++ port of ../server.py: same endpoints, parameters, SQL and responses.
// Each request becomes one read_json() over VictoriaMetrics' /api/v1/export
// (the same query as ../../duckmetrics-demo/v2big.sql), so DuckDB does the
// unnesting, bucketing and latest-per-series work.
//
// Endpoints (all GET):
//   /api/v1/metrics   metric names            match[], start, end, format
//   /api/v1/samples   raw or bucketed samples match[], start, end, step, agg, format
//   /api/v1/latest    last sample per series  match[], start, end, format
//
// Build:
//   cmake -B build && cmake --build build
//
// Usage:
//   build/ath-server                            # :8000 -> http://localhost:8428
//   build/ath-server --port 9000 --vm http://vm:8428

#include <duckdb.h>
#include <httplib.h>

#include <charconv>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <optional>
#include <random>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <variant>
#include <vector>

namespace fs = std::filesystem;

static std::string VM_URL = "http://localhost:8428";
constexpr int64_t DEFAULT_RANGE_MS = 3'600'000;  // start defaults to end - 1h
constexpr int64_t MAX_POINTS = 11'000;           // per series, same cap as Prometheus query_range
// 9999-12-31T23:59:59.999Z; also keeps end - start and the bucket math far from int64 overflow
constexpr int64_t MAX_MS = 253'402'300'799'999;

using Pairs = std::vector<std::pair<std::string, std::string>>;

static const Pairs FORMATS = {
    {"json", "application/json"},
    {"csv", "text/csv"},
    {"parquet", "application/vnd.apache.parquet"},
    {"arrow", "application/vnd.apache.arrow.stream"},
};

// agg names are whitelisted here, never interpolated from the request
static const Pairs AGGS = {
    {"last", "arg_max(value, ts)"},
    {"first", "arg_min(value, ts)"},
    {"avg", "avg(value)"},
    {"min", "min(value)"},
    {"max", "max(value)"},
    {"sum", "sum(value)"},
    {"count", "count(value)::DOUBLE"},
};

static const std::string *lookup(const Pairs &pairs, const std::string &key) {
    for (auto &[k, v] : pairs)
        if (k == key) return &v;
    return nullptr;
}

static std::string keys(const Pairs &pairs) {
    std::string out;
    for (auto &[k, v] : pairs) out += (out.empty() ? "" : ", ") + k;
    return out;
}

struct BadRequest : std::runtime_error {
    using std::runtime_error::runtime_error;
};

// -- Python compatibility -----------------------------------------------------
// Error messages and parsing follow server.py, so the small pieces of Python
// it leans on (repr, float(), parse_qs, urlencode) are reproduced here.

// repr() of a str: single quotes unless the text has ' and no "
static std::string py_repr(const std::string &s) {
    char q = (s.find('\'') != std::string::npos && s.find('"') == std::string::npos) ? '"' : '\'';
    std::string out(1, q);
    char hex[8];
    for (size_t i = 0; i < s.size(); i++) {
        auto c = (unsigned char)s[i];
        if (c == q || c == '\\') out += {'\\', (char)c};
        else if (c == '\n') out += "\\n";
        else if (c == '\r') out += "\\r";
        else if (c == '\t') out += "\\t";
        else if (c < 0x20 || c == 0x7f) snprintf(hex, sizeof hex, "\\x%02x", c), out += hex;
        else if (c == 0xc2 && i + 1 < s.size() && (unsigned char)s[i + 1] < 0xa0 &&
                 (unsigned char)s[i + 1] >= 0x80)  // U+0080..U+009F
            snprintf(hex, sizeof hex, "\\x%02x", (unsigned char)s[++i]), out += hex;
        else out += (char)c;
    }
    return out + q;
}

// float(): surrounding whitespace, _ between digits, inf/nan; no hex
static std::optional<double> py_float(const std::string &raw) {
    auto b = raw.find_first_not_of(" \t\n\r\f\v");
    if (b == std::string::npos) return std::nullopt;
    auto s = raw.substr(b, raw.find_last_not_of(" \t\n\r\f\v") - b + 1);
    std::string t;
    for (size_t i = 0; i < s.size(); i++) {
        if (s[i] != '_') { t += s[i]; continue; }
        if (i == 0 || i + 1 == s.size() || !isdigit((unsigned char)s[i - 1]) || !isdigit((unsigned char)s[i + 1]))
            return std::nullopt;
    }
    if (t.find_first_of("xX(") != std::string::npos) return std::nullopt;
    char *end;
    double v = strtod(t.c_str(), &end);
    if (end == t.c_str() || end != t.c_str() + t.size()) return std::nullopt;
    return v;
}

// int(x) of a time or duration in ms: truncate toward zero; nullopt for nan, inf or past MAX_MS
static std::optional<int64_t> py_int(double x) {
    if (!(std::fabs(x) <= MAX_MS)) return std::nullopt;
    return (int64_t)x;
}

static std::string utf8_or_replace(const std::string &s);

// unquote_plus(): + is a space, %XX a byte, invalid UTF-8 becomes U+FFFD
static std::string unquote_plus(const std::string &s) {
    std::string out;
    for (size_t i = 0; i < s.size(); i++) {
        if (s[i] == '+') out += ' ';
        else if (s[i] == '%' && i + 2 < s.size() && isxdigit((unsigned char)s[i + 1]) &&
                 isxdigit((unsigned char)s[i + 2]))
            out += (char)std::stoi(s.substr(i + 1, 2), nullptr, 16), i += 2;
        else out += s[i];
    }
    return utf8_or_replace(out);
}

using Params = std::map<std::string, std::vector<std::string>>;

// parse_qs(): blank values are dropped, repeated keys keep their order
static Params parse_qs(const std::string &query) {
    Params p;
    size_t pos = 0;
    while (pos <= query.size()) {
        auto amp = query.find('&', pos);
        auto part = query.substr(pos, amp == std::string::npos ? std::string::npos : amp - pos);
        auto eq = part.find('=');
        if (eq != std::string::npos && eq + 1 < part.size())
            p[unquote_plus(part.substr(0, eq))].push_back(unquote_plus(part.substr(eq + 1)));
        if (amp == std::string::npos) break;
        pos = amp + 1;
    }
    return p;
}

// urlencode(): quote_plus each key and value
static std::string urlencode(const Pairs &q) {
    std::string out;
    char hex[4];
    for (auto &[k, v] : q) {
        if (!out.empty()) out += '&';
        for (auto *s : {&k, &v}) {
            for (unsigned char c : *s) {
                if (isalnum(c) || c == '_' || c == '.' || c == '-' || c == '~') out += (char)c;
                else if (c == ' ') out += '+';
                else snprintf(hex, sizeof hex, "%%%02X", c), out += hex;
            }
            if (s == &k) out += '=';
        }
    }
    return out;
}

// -- time parameters ----------------------------------------------------------
// Accepts what Prometheus/VictoriaMetrics accept: unix seconds, RFC3339,
// 'now', and relative durations ('-1h', 'now-30m'). Everything is resolved
// to epoch ms once per request, so start and end share the same 'now'.
// A leading '-' is always relative: -3600 means an hour ago, not 1969, as
// in VictoriaMetrics (Prometheus has no relative times).

static int64_t now_ms() {
    using namespace std::chrono;
    return duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count();
}

static double unit_ms(const std::string &u) {
    static const std::map<std::string, double> UNITS_MS = {
        {"ms", 1}, {"s", 1000}, {"m", 60'000}, {"h", 3'600'000},
        {"d", 86'400'000}, {"w", 604'800'000}, {"y", 31'536'000'000}};
    return UNITS_MS.at(u);
}

// '90s', '1h30m' or bare seconds ('60') -> ms
static int64_t parse_duration(const std::string &s) {
    if (auto v = py_float(s))
        if (auto ms = py_int(*v * 1000)) return *ms;
    static const std::regex DURATION_RE(R"((\d+(?:\.\d+)?)(ms|s|m|h|d|w|y))");
    std::string joined;
    double total = 0;
    for (std::sregex_iterator it(s.begin(), s.end(), DURATION_RE), end; it != end; ++it) {
        joined += it->str(0);
        total += strtod(it->str(1).c_str(), nullptr) * unit_ms(it->str(2));  // inf if huge, like float()
    }
    auto ms = py_int(total);
    if (joined.empty() || joined != s || !ms)
        throw BadRequest("bad duration " + py_repr(s) + ": use e.g. 30s, 5m, 1h30m");
    return *ms;
}

// days since 1970-01-01 (Howard Hinnant's days_from_civil)
static int64_t days_from_civil(int64_t y, int64_t m, int64_t d) {
    y -= m <= 2;
    int64_t era = (y >= 0 ? y : y - 399) / 400;
    int64_t yoe = y - era * 400;
    int64_t doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    int64_t doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + doe - 719468;
}

// datetime.fromisoformat() for the common forms: YYYY-MM-DD or YYYYMMDD,
// then optionally any one separator and HH[:MM[:SS[.ffffff]]], then
// optionally Z or +HH[:MM]. No offset means UTC.
static std::optional<int64_t> parse_iso(const std::string &s) {
    static const std::regex ISO_RE(
        // \2 and \6 make the - and : separators all-or-none
        R"((\d{4})(-?)(\d{2})\2(\d{2})(?:.(\d{2})(?:(:?)(\d{2})(?:\6(\d{2})(?:[.,](\d+))?)?)?)?)"
        R"((Z|[+-]\d{2}(?::?\d{2})?)?)");
    std::smatch m;
    if (!std::regex_match(s, m, ISO_RE)) return std::nullopt;
    auto num = [&](int i) { return m[i].matched ? std::stoll(m[i].str()) : 0LL; };
    int64_t y = num(1), mo = num(3), d = num(4), h = num(5), mi = num(7), sec = num(8);
    static const int DAYS[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    bool leap = (y % 4 == 0 && y % 100 != 0) || y % 400 == 0;
    if (y < 1 || mo < 1 || mo > 12 || d < 1 || d > DAYS[mo - 1] + (mo == 2 && leap) || h > 23 || mi > 59 ||
        sec > 59)
        return std::nullopt;
    int64_t us = 0;
    if (m[9].matched) {
        auto frac = (m[9].str() + "00000").substr(0, 6);  // digits past 6 are dropped
        us = std::stoll(frac);
    }
    int64_t offset = 0;
    if (m[10].matched && m[10].str() != "Z") {
        auto tz = m[10].str();
        int64_t oh = std::stoll(tz.substr(1, 2));
        int64_t om = tz.size() > 3 ? std::stoll(tz.substr(tz.size() - 2)) : 0;
        if (oh > 23 || om > 59) return std::nullopt;
        offset = (tz[0] == '-' ? -1 : 1) * (oh * 3600 + om * 60);
    }
    int64_t total_us = ((days_from_civil(y, mo, d) * 86400 + h * 3600 + mi * 60 + sec) - offset) * 1'000'000 + us;
    // int(dt.timestamp() * 1000), with timestamp() as a float, like Python
    return py_int((double)total_us / 1e6 * 1000);
}

// unix seconds | RFC3339 | now | -<duration> | now-<duration> -> ms
static int64_t parse_time(const std::string &s, int64_t now) {
    if (s == "now") return now;
    if (s.starts_with("now-")) return now - parse_duration(s.substr(4));
    if (s.starts_with("-")) return now - parse_duration(s.substr(1));
    if (auto v = py_float(s))
        if (auto ms = py_int(*v * 1000)) return *ms;
    if (auto ms = parse_iso(s)) return *ms;
    throw BadRequest("bad time " + py_repr(s) + ": use unix seconds, RFC3339, now or -1h");
}

static bool has(const Params &p, const std::string &k) { return p.count(k) > 0; }
static const std::string &last(const Params &p, const std::string &k) { return p.at(k).back(); }

static std::pair<int64_t, int64_t> time_range(const Params &p) {
    int64_t now = now_ms();
    int64_t end = has(p, "end") ? parse_time(last(p, "end"), now) : now;
    int64_t start = has(p, "start") ? parse_time(last(p, "start"), now) : end - DEFAULT_RANGE_MS;
    if (start >= end) throw BadRequest("start must be before end");
    return {start, end};
}

static std::string seconds(int64_t ms) {
    char buf[64];
    snprintf(buf, sizeof buf, "%.3f", (double)ms / 1000);
    return buf;
}

// -- queries --------------------------------------------------------------------

static std::string export_url(const std::vector<std::string> &matches, int64_t start, int64_t end) {
    // urlencode escapes {, *, ~ etc.; DuckDB rejects a raw * in HTTP paths
    Pairs q;
    for (auto &m : matches) q.emplace_back("match[]", m);
    q.emplace_back("start", seconds(start));
    q.emplace_back("end", seconds(end));
    return VM_URL + "/api/v1/export?" + urlencode(q);
}

// One row per sample. Explicit columns: labels differ per metric, so a MAP
// beats letting read_json infer a struct of every label key it sees.
static const std::string SAMPLES = R"(
WITH series AS (
    SELECT
        metric['__name__']  AS name,
        map_from_entries(list_filter(map_entries(metric),
                                     lambda e: e.key <> '__name__')) AS labels,
        timestamps,
        "values"
    FROM read_json($url, format = 'newline_delimited', columns = {
        'metric': 'MAP(VARCHAR, VARCHAR)',
        'timestamps': 'BIGINT[]',
        'values': 'DOUBLE[]'})
),
samples AS (
    SELECT name, labels, unnest(timestamps) AS ts, unnest("values") AS value
    FROM series
)
)";

// buckets are aligned to start, stamped with the bucket's start time.
// ts >= $start keeps // (which truncates toward zero) from folding earlier
// samples into the first bucket. A NaN would poison avg/sum/min/max; ±Inf
// (VictoriaMetrics exports it as "Infinity") and null are kept.
static const std::string BUCKETED = R"(,
bucketed AS (
    SELECT name, labels, $start + (ts - $start) // $step * $step AS bucket,
           {agg} AS value
    FROM samples
    WHERE ts >= $start AND isnan(value) IS NOT TRUE
    GROUP BY name, labels, bucket
),
points AS (SELECT name, labels, bucket AS ts, value FROM bucketed)
)";

static const std::string RAW = R"(,
points AS (SELECT * FROM samples)
)";

// latest sample per series, as in v2big.sql
static const std::string LATEST = R"(,
points AS (
    SELECT * FROM samples
    QUALIFY row_number() OVER (PARTITION BY name, labels ORDER BY ts DESC) = 1
)
)";

static const std::string TABLE = R"(
SELECT name, labels, make_timestamp_ms(ts) AS ts, value
FROM points
ORDER BY name, labels, ts
)";

// json for /samples: one object per series, timestamps and values as arrays
static const std::string SERIES = R"(
SELECT name, labels, list(ts ORDER BY ts) AS timestamps,
       list(value ORDER BY ts) AS values
FROM points
GROUP BY name, labels
ORDER BY name, labels
)";

// explicit type: with no names, read_json would infer JSON[]
static const std::string METRICS =
    "SELECT unnest(data) AS name FROM read_json($url, columns = {'data': 'VARCHAR[]'})";

// -- DuckDB C API -----------------------------------------------------------------

struct DuckError : std::runtime_error {
    duckdb_error_type type;
    DuckError(const std::string &msg, duckdb_error_type t) : std::runtime_error(msg), type(t) {}
};

struct Connection {
    duckdb_connection con = nullptr;
    explicit Connection(duckdb_database db) {
        if (duckdb_connect(db, &con) == DuckDBError) throw DuckError("cannot connect", DUCKDB_ERROR_CONNECTION);
    }
    ~Connection() { duckdb_disconnect(&con); }
};

struct Result {
    duckdb_result res{};
    ~Result() { duckdb_destroy_result(&res); }
};

using Args = std::vector<std::pair<std::string, std::variant<std::string, int64_t>>>;

// prepare, bind $named parameters, run; a failure throws DuckError
static std::unique_ptr<Result> run(duckdb_connection con, const std::string &sql, const Args &args) {
    duckdb_prepared_statement stmt;
    if (duckdb_prepare(con, sql.c_str(), &stmt) == DuckDBError) {
        std::string msg = duckdb_prepare_error(stmt);
        duckdb_destroy_prepare(&stmt);
        throw DuckError(msg, DUCKDB_ERROR_INVALID);
    }
    for (auto &[name, value] : args) {
        idx_t i;
        duckdb_bind_parameter_index(stmt, &i, name.c_str());
        if (auto *s = std::get_if<std::string>(&value)) duckdb_bind_varchar(stmt, i, s->c_str());
        else duckdb_bind_int64(stmt, i, std::get<int64_t>(value));
    }
    auto r = std::make_unique<Result>();
    auto state = duckdb_execute_prepared(stmt, &r->res);
    duckdb_destroy_prepare(&stmt);
    if (state == DuckDBError) throw DuckError(duckdb_result_error(&r->res), duckdb_result_error_type(&r->res));
    return r;
}

// call f(chunk, row) for every row
template <class F>
static void each_row(Result &r, F f) {
    while (auto chunk = duckdb_fetch_chunk(r.res)) {
        for (idx_t i = 0, n = duckdb_data_chunk_get_size(chunk); i < n; i++) f(chunk, i);
        duckdb_destroy_data_chunk(&chunk);
    }
}

static bool is_null(duckdb_vector v, idx_t row) {
    auto *validity = duckdb_vector_get_validity(v);
    return validity && !duckdb_validity_row_is_valid(validity, row);
}

template <class T>
static T at(duckdb_vector v, idx_t row) {
    return ((T *)duckdb_vector_get_data(v))[row];
}

static std::string str_at(duckdb_vector v, idx_t row) {
    auto *s = (duckdb_string_t *)duckdb_vector_get_data(v) + row;
    return {duckdb_string_t_data(s), duckdb_string_t_length(*s)};
}

// -- JSON ---------------------------------------------------------------------------
// Written like Python's json.dumps defaults: ", " and ": " separators,
// non-ASCII as \uXXXX, floats as repr() ("154.0", "1e-05").

// bytes.decode('utf-8', 'replace'): only well-formed sequences (no overlongs,
// surrogates or code points past U+10FFFF), and one U+FFFD per maximal bad prefix
static std::string utf8_or_replace(const std::string &s) {
    std::string out;
    for (size_t i = 0; i < s.size();) {
        auto c = (unsigned char)s[i];
        if (c < 0x80) { out += (char)c, i++; continue; }
        size_t n = 0;
        unsigned char lo = 0x80, hi = 0xbf;  // allowed range of the second byte
        if (c >= 0xc2 && c <= 0xdf) n = 2;
        else if (c >= 0xe0 && c <= 0xef) n = 3, lo = c == 0xe0 ? 0xa0 : 0x80, hi = c == 0xed ? 0x9f : 0xbf;
        else if (c >= 0xf0 && c <= 0xf4) n = 4, lo = c == 0xf0 ? 0x90 : 0x80, hi = c == 0xf4 ? 0x8f : 0xbf;
        size_t j = 1;
        while (n && j < n && i + j < s.size()) {
            auto b = (unsigned char)s[i + j];
            if (b < (j == 1 ? lo : 0x80) || b > (j == 1 ? hi : 0xbf)) break;
            j++;
        }
        if (n && j == n) out.append(s, i, n);
        else out += "\xEF\xBF\xBD";
        i += j;
    }
    return out;
}

static void json_str(std::string &out, const std::string &raw) {
    auto s = utf8_or_replace(raw);
    char hex[8];
    auto u = [&](unsigned cp) { snprintf(hex, sizeof hex, "\\u%04x", cp), out += hex; };
    out += '"';
    for (size_t i = 0; i < s.size(); i++) {
        auto c = (unsigned char)s[i];
        if (c == '"') out += "\\\"";
        else if (c == '\\') out += "\\\\";
        else if (c == '\n') out += "\\n";
        else if (c == '\r') out += "\\r";
        else if (c == '\t') out += "\\t";
        else if (c == '\b') out += "\\b";
        else if (c == '\f') out += "\\f";
        else if (c < 0x20) u(c);
        else if (c < 0x80) out += (char)c;
        else {
            size_t n = (c >> 5) == 6 ? 2 : (c >> 4) == 14 ? 3 : 4;
            unsigned cp = c & (0x7f >> n);
            for (size_t j = 1; j < n; j++) cp = (cp << 6) | ((unsigned char)s[i + j] & 0x3f);
            i += n - 1;
            if (cp < 0x10000) u(cp);
            else cp -= 0x10000, u(0xd800 | (cp >> 10)), u(0xdc00 | (cp & 0x3ff));
        }
    }
    out += '"';
}

static void json_int(std::string &out, int64_t v) { out += std::to_string(v); }

// repr(float): shortest round-trip digits, exponent form outside 1e-4..1e16;
// NaN/Inf become null (JSON has none; VictoriaMetrics stores staleness markers as NaN)
static void json_float(std::string &out, double v) {
    if (!std::isfinite(v)) { out += "null"; return; }
    char buf[64];
    auto end = std::to_chars(buf, buf + sizeof buf, v, std::chars_format::scientific).ptr;
    std::string sci(buf, end);  // -d.ddde+XX
    bool neg = sci[0] == '-';
    auto e = sci.find('e');
    std::string digits;
    for (size_t i = neg; i < e; i++)
        if (sci[i] != '.') digits += sci[i];
    int exp = std::stoi(sci.substr(e + 1));
    int decpt = exp + 1, n = (int)digits.size();
    if (neg) out += '-';
    if (decpt <= -4 || decpt > 16) {
        out += digits[0];
        if (n > 1) out += "." + digits.substr(1);
        snprintf(buf, sizeof buf, "e%c%02d", exp < 0 ? '-' : '+', std::abs(exp));
        out += buf;
    } else if (decpt <= 0) {
        out += "0." + std::string(-decpt, '0') + digits;
    } else if (decpt < n) {
        out += digits.substr(0, decpt) + "." + digits.substr(decpt);
    } else {
        out += digits + std::string(decpt - n, '0') + ".0";
    }
}

static void json_nullable_str(std::string &out, duckdb_vector v, idx_t row) {
    if (is_null(v, row)) out += "null";
    else json_str(out, str_at(v, row));
}

static void json_nullable_float(std::string &out, duckdb_vector v, idx_t row) {
    if (is_null(v, row)) out += "null";
    else json_float(out, at<double>(v, row));
}

// MAP(VARCHAR, VARCHAR) -> {"k": "v", ...}
static void json_map(std::string &out, duckdb_vector v, idx_t row) {
    if (is_null(v, row)) { out += "null"; return; }
    auto entry = at<duckdb_list_entry>(v, row);
    auto entries = duckdb_list_vector_get_child(v);
    auto keys = duckdb_struct_vector_get_child(entries, 0);
    auto values = duckdb_struct_vector_get_child(entries, 1);
    out += '{';
    for (idx_t i = entry.offset; i < entry.offset + entry.length; i++) {
        if (i > entry.offset) out += ", ";
        json_str(out, str_at(keys, i));
        out += ": ";
        json_nullable_str(out, values, i);
    }
    out += '}';
}

// BIGINT[] or DOUBLE[] -> [...]
template <class T>
static void json_list(std::string &out, duckdb_vector v, idx_t row) {
    auto entry = at<duckdb_list_entry>(v, row);
    auto child = duckdb_list_vector_get_child(v);
    out += '[';
    for (idx_t i = entry.offset; i < entry.offset + entry.length; i++) {
        if (i > entry.offset) out += ", ";
        if constexpr (std::is_same_v<T, double>) json_nullable_float(out, child, i);
        else if (is_null(child, i)) out += "null";
        else json_int(out, at<T>(child, i));
    }
    out += ']';
}

// -- requests ---------------------------------------------------------------------

struct Reply {
    int code = 200;
    std::string ctype, body;
};

struct Request {
    Params params;
    std::string fmt;
    duckdb_connection con;
    std::string vm;  // the VictoriaMetrics URL being read, for error messages
    Reply reply;
};

static void send_json(Request &rq, const std::string &body) { rq.reply = {200, *lookup(FORMATS, "json"), body}; }

static void send_error(Reply &reply, int code, const std::string &msg) {
    std::string body = "{\"status\": \"error\", \"error\": ";
    json_str(body, msg);
    reply = {code, *lookup(FORMATS, "json"), body + "}"};
}

static std::string sql_quote(const std::string &s) {
    std::string out = "'";
    for (char c : s) out += c == '\'' ? std::string("''") : std::string(1, c);
    return out + "'";
}

// Long format (name, labels, ts, value) as csv, parquet or arrow
static void table(Request &rq, const std::string &sql, const Args &args, bool labels) {
    auto dir = fs::temp_directory_path() / ("ath-" + std::to_string(std::random_device{}()));
    fs::create_directories(dir);
    struct Cleanup {
        fs::path p;
        ~Cleanup() { std::error_code ec; fs::remove_all(p, ec); }
    } cleanup{dir};
    auto path = (dir / "out").string();

    std::string select = sql, options;
    if (rq.fmt == "csv") {
        // csv has no MAP type: labels go out as a JSON object string
        if (labels) select = "SELECT * REPLACE (to_json(labels)::VARCHAR AS labels) FROM (" + sql + ")";
        options = "FORMAT csv, HEADER true, TIMESTAMPFORMAT '%Y-%m-%dT%H:%M:%S.%gZ'";
    } else if (rq.fmt == "parquet") {
        options = "FORMAT parquet";
    } else {
        options = "FORMAT arrows";  // Arrow IPC stream, from the nanoarrow extension
    }
    run(rq.con, "COPY (" + select + ") TO " + sql_quote(path) + " (" + options + ")", args);
    std::ifstream f(path, std::ios::binary);
    std::stringstream ss;
    ss << f.rdbuf();
    rq.reply = {200, *lookup(FORMATS, rq.fmt), ss.str()};
}

static const std::vector<std::string> &matches(const Params &p) {
    if (!has(p, "match[]")) throw BadRequest("need at least one match[] series selector");
    return p.at("match[]");
}

static void metrics(Request &rq) {
    Pairs q;
    if (has(rq.params, "match[]"))
        for (auto &m : rq.params.at("match[]")) q.emplace_back("match[]", m);
    // VictoriaMetrics picks its own default range when none is given
    // end is parsed first and both share one 'now', as in time_range
    int64_t now = now_ms();
    std::map<std::string, int64_t> times;
    for (auto k : {"end", "start"})
        if (has(rq.params, k)) times[k] = parse_time(last(rq.params, k), now);
    if (times.size() == 2 && times["start"] >= times["end"]) throw BadRequest("start must be before end");
    for (auto k : {"start", "end"})
        if (times.count(k)) q.emplace_back(k, seconds(times[k]));
    rq.vm = VM_URL + "/api/v1/label/__name__/values?" + urlencode(q);
    Args args = {{"url", rq.vm}};
    if (rq.fmt != "json") return table(rq, METRICS, args, false);
    auto r = run(rq.con, METRICS, args);
    std::string out = "{\"status\": \"success\", \"data\": [";
    bool first = true;
    each_row(*r, [&](duckdb_data_chunk c, idx_t i) {
        if (!first) out += ", ";
        first = false;
        json_nullable_str(out, duckdb_data_chunk_get_vector(c, 0), i);
    });
    send_json(rq, out + "]}");
}

static void samples(Request &rq) {
    auto &p = rq.params;
    auto [start, end] = time_range(p);
    rq.vm = export_url(matches(p), start, end);
    Args args = {{"url", rq.vm}};
    std::optional<int64_t> step;
    std::string sql;
    if (has(p, "step")) {
        step = parse_duration(last(p, "step"));
        if (*step <= 0) throw BadRequest("step must be positive");
        // Prometheus' own check: buckets count both ends, so up to MAX_POINTS + 1
        if ((end - start) / *step > MAX_POINTS)
            throw BadRequest("more than " + std::to_string(MAX_POINTS) +
                             " points per series: raise step or shorten the range");
        auto agg = has(p, "agg") ? last(p, "agg") : "last";
        auto *expr = lookup(AGGS, agg);
        if (!expr) throw BadRequest("agg must be one of " + keys(AGGS));
        auto bucketed = BUCKETED;
        bucketed.replace(bucketed.find("{agg}"), 5, *expr);
        sql = SAMPLES + bucketed;
        args.emplace_back("start", start);
        args.emplace_back("step", *step);
    } else if (has(p, "agg")) {
        throw BadRequest("agg needs step");
    } else {
        sql = SAMPLES + RAW;
    }

    if (rq.fmt != "json") return table(rq, sql + TABLE, args, true);
    auto r = run(rq.con, sql + SERIES, args);
    std::string out = "{\"status\": \"success\", \"start\": ";
    json_int(out, start);
    out += ", \"end\": ";
    json_int(out, end);
    out += ", \"step\": ";
    step ? json_int(out, *step) : void(out += "null");
    out += ", \"data\": [";
    bool first = true;
    each_row(*r, [&](duckdb_data_chunk c, idx_t i) {
        out += first ? "{\"name\": " : ", {\"name\": ";
        first = false;
        json_nullable_str(out, duckdb_data_chunk_get_vector(c, 0), i);
        out += ", \"labels\": ";
        json_map(out, duckdb_data_chunk_get_vector(c, 1), i);
        out += ", \"timestamps\": ";
        json_list<int64_t>(out, duckdb_data_chunk_get_vector(c, 2), i);
        out += ", \"values\": ";
        json_list<double>(out, duckdb_data_chunk_get_vector(c, 3), i);
        out += '}';
    });
    send_json(rq, out + "]}");
}

static void latest(Request &rq) {
    auto [start, end] = time_range(rq.params);
    rq.vm = export_url(matches(rq.params), start, end);
    Args args = {{"url", rq.vm}};
    auto sql = SAMPLES + LATEST;
    if (rq.fmt != "json") return table(rq, sql + TABLE, args, true);
    auto r = run(rq.con, sql + "SELECT * FROM points ORDER BY name, labels", args);
    std::string out = "{\"status\": \"success\", \"start\": ";
    json_int(out, start);
    out += ", \"end\": ";
    json_int(out, end);
    out += ", \"data\": [";
    bool first = true;
    each_row(*r, [&](duckdb_data_chunk c, idx_t i) {
        out += first ? "{\"name\": " : ", {\"name\": ";
        first = false;
        json_nullable_str(out, duckdb_data_chunk_get_vector(c, 0), i);
        out += ", \"labels\": ";
        json_map(out, duckdb_data_chunk_get_vector(c, 1), i);
        out += ", \"timestamp\": ";
        json_int(out, at<int64_t>(duckdb_data_chunk_get_vector(c, 2), i));
        out += ", \"value\": ";
        json_nullable_float(out, duckdb_data_chunk_get_vector(c, 3), i);
        out += '}';
    });
    send_json(rq, out + "]}");
}

// The C API reports an HTTP failure's status in its message but not the
// response body, so ask VictoriaMetrics again for its own error text.
static std::string vm_error_body(const std::string &url) {
    auto split = url.find('/', url.find("://") + 3);
    httplib::Client cli(url.substr(0, split));
    auto res = cli.Get(url.substr(split));
    if (!res) return "";
    auto &b = res->body;
    auto first = b.find_first_not_of(" \t\n\r\f\v");
    return first == std::string::npos ? "" : b.substr(first, b.find_last_not_of(" \t\n\r\f\v") - first + 1);
}

static Reply handle(duckdb_database db, const std::string &target) {
    auto path = target.substr(0, target.find_first_of("?#"));
    auto q = target.find('?');
    auto query = q == std::string::npos ? "" : target.substr(q + 1, target.find('#', q) - q - 1);
    static const std::map<std::string, void (*)(Request &)> routes = {
        {"/api/v1/metrics", metrics},
        {"/api/v1/samples", samples},
        {"/api/v1/latest", latest},
    };
    Reply reply;
    auto route = routes.find(path);
    if (route == routes.end()) {
        send_error(reply, 404, "no route " + path);
        return reply;
    }
    Request rq;
    rq.params = parse_qs(query);
    try {
        rq.fmt = has(rq.params, "format") ? last(rq.params, "format") : "json";
        if (!lookup(FORMATS, rq.fmt)) throw BadRequest("format must be one of " + keys(FORMATS));
        Connection con(db);  // one connection per request, like a cursor
        rq.con = con.con;
        route->second(rq);
        return rq.reply;
    } catch (const BadRequest &e) {
        send_error(reply, 400, e.what());
    } catch (const DuckError &e) {
        if (e.type == DUCKDB_ERROR_HTTP) {
            // pass VictoriaMetrics' own message through (bad selector, etc.)
            std::smatch m;
            std::string msg = e.what();
            static const std::regex STATUS_RE(R"(\(HTTP (\d{3}))");
            int status = std::regex_search(msg, m, STATUS_RE) ? std::stoi(m[1]) : 0;
            // the query was rejected; 404 (wrong --vm) and 429 are VM-side problems
            bool rejected = status >= 400 && status < 500 && status != 404 && status != 429;
            auto body = vm_error_body(rq.vm);
            send_error(reply, rejected ? 400 : 502, "victoriametrics: " + (body.empty() ? msg : body));
        } else {
            send_error(reply, 500, e.what());
        }
    } catch (const std::exception &e) {
        send_error(reply, 500, e.what());
    }
    return reply;
}

// 127.0.0.1 - - [30/Oct/2026 23:11:58] "GET /api/v1/metrics HTTP/1.1" 200 -
static void log_request(const httplib::Request &req, const httplib::Response &res) {
    char when[32];
    auto t = std::time(nullptr);
    std::tm tm;
    std::strftime(when, sizeof when, "%d/%b/%Y %H:%M:%S", localtime_r(&t, &tm));
    fprintf(stderr, "%s - - [%s] \"%s %s %s\" %d -\n", req.remote_addr.c_str(), when, req.method.c_str(),
            req.target.c_str(), req.version.c_str(), res.status);
}

int main(int argc, char **argv) {
    std::string host = "127.0.0.1";
    int port = 8000;
    std::string usage = "usage: ath-server [-h] [--host HOST] [--port PORT] [--vm VM]\n";
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i], value;
        if (a == "-h" || a == "--help") {
            std::cout << usage << "\nREST front end for VictoriaMetrics, via DuckDB\n\noptions:\n"
                      << "  -h, --help   show this help message and exit\n  --host HOST\n  --port PORT\n"
                      << "  --vm VM      VictoriaMetrics base URL\n";
            return 0;
        }
        auto eq = a.find('=');
        if (eq != std::string::npos) value = a.substr(eq + 1), a = a.substr(0, eq);
        else if (i + 1 < argc) value = argv[++i];
        else { std::cerr << usage << "ath-server: error: argument " << a << ": expected one argument\n"; return 2; }
        if (a == "--host") host = value;
        else if (a == "--port") port = std::atoi(value.c_str());
        else if (a == "--vm") VM_URL = value;
        else { std::cerr << usage << "ath-server: error: unrecognized arguments: " << a << "\n"; return 2; }
    }
    while (VM_URL.ends_with('/')) VM_URL.pop_back();

    // VictoriaMetrics answers are live: never serve a cached HTTP body.
    // One GET per read, no HEAD first, so VM's error bodies come through.
    // No retries: DuckDB backs off for up to a minute on 503/429; fail fast instead.
    duckdb_config config;
    duckdb_create_config(&config);
    for (auto [name, value] : {std::pair{"enable_external_file_cache", "false"},
                               {"force_download", "true"},
                               {"http_retries", "0"}}) {
        if (duckdb_set_config(config, name, value) == DuckDBError) {
            std::cerr << "cannot set duckdb option " << name << "\n";
            return 1;
        }
    }
    duckdb_database db;
    char *err = nullptr;
    if (duckdb_open_ext(nullptr, &db, config, &err) == DuckDBError) {
        std::cerr << "cannot open duckdb: " << (err ? err : "") << "\n";
        return 1;
    }
    duckdb_destroy_config(&config);

    // format=arrow is written by the nanoarrow extension (server.py uses pyarrow)
    {
        Connection con(db);
        duckdb_result r;
        if (duckdb_query(con.con, "INSTALL nanoarrow FROM community; LOAD nanoarrow", &r) == DuckDBError)
            std::cerr << "warning: format=arrow unavailable: " << duckdb_result_error(&r) << "\n";
        duckdb_destroy_result(&r);
    }

    httplib::Server srv;
    srv.Get(".*", [&](const httplib::Request &req, httplib::Response &res) {
        auto reply = handle(db, req.target);
        res.status = reply.code;
        res.set_content(reply.body, reply.ctype);
    });
    srv.set_logger(log_request);
    std::cout << "listening on http://" << host << ":" << port << " -> " << VM_URL << std::endl;
    if (!srv.listen(host, port)) {
        std::cerr << "cannot listen on " << host << ":" << port << "\n";
        return 1;
    }
    duckdb_close(&db);
}
