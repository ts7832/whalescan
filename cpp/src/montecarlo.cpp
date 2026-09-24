#include "whalecore/montecarlo.hpp"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <limits>
#include <random>
#include <stdexcept>
#include <thread>

namespace whalecore {
namespace {

std::uint64_t splitmix64(std::uint64_t x) {
    x += 0x9E3779B97F4A7C15ULL;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
    return x ^ (x >> 31);
}

// Top 53 bits of a 64-bit draw -> uniform double in [0, 1).
inline double to_unit(std::uint64_t r) { return static_cast<double>(r >> 11) * 0x1.0p-53; }

void validate(std::span<const double> p, std::span<const std::uint8_t> y, std::span<const double> w,
              std::uint32_t n_sims) {
    if (p.size() != y.size() || p.size() != w.size())
        throw std::invalid_argument("prices, outcomes and weights must have equal length");
    if (n_sims == 0) throw std::invalid_argument("n_sims must be >= 1");
    for (std::size_t i = 0; i < p.size(); ++i) {
        if (!(p[i] > 0.0 && p[i] < 1.0)) throw std::invalid_argument("prices must be in (0, 1)");
        if (y[i] > 1) throw std::invalid_argument("outcomes must be 0 or 1");
        if (!(w[i] > 0.0) || !std::isfinite(w[i]))
            throw std::invalid_argument("weights must be positive and finite");
    }
}

// Assumes validated, non-empty input.
SkillResult run(std::span<const double> p, std::span<const std::uint8_t> y,
                std::span<const double> w, std::uint32_t n_sims, std::uint64_t seed) {
    const std::size_t n = p.size();
    double sw = 0.0, sw2 = 0.0;
    for (const double wi : w) { sw += wi; sw2 += wi * wi; }

    std::vector<double> wn(n);
    double observed = 0.0;
    for (std::size_t i = 0; i < n; ++i) {
        wn[i] = w[i] / sw;
        observed += wn[i] * (static_cast<double>(y[i]) - p[i]);
    }

    std::mt19937_64 rng(seed);
    constexpr double tol = 1e-12;  // ties count as "at least as good"
    std::uint64_t at_least = 0;
    double sum = 0.0, sumsq = 0.0;
    for (std::uint32_t k = 0; k < n_sims; ++k) {
        double s = 0.0;
        for (std::size_t i = 0; i < n; ++i) {
            const double yi = to_unit(rng()) < p[i] ? 1.0 : 0.0;
            s += wn[i] * (yi - p[i]);
        }
        at_least += s >= observed - tol ? 1 : 0;
        sum += s;
        sumsq += s * s;
    }
    const double mean = sum / n_sims;
    const double var = std::max(0.0, sumsq / n_sims - mean * mean);
    return {observed, (1.0 + static_cast<double>(at_least)) / (1.0 + n_sims), mean, std::sqrt(var),
            sw * sw / sw2};
}

}  // namespace

SkillResult skill_mc(std::span<const double> prices, std::span<const std::uint8_t> outcomes,
                     std::span<const double> weights, std::uint32_t n_sims, std::uint64_t seed) {
    validate(prices, outcomes, weights, n_sims);
    if (prices.empty()) throw std::invalid_argument("need at least one position");
    return run(prices, outcomes, weights, n_sims, seed);
}

std::vector<SkillResult> skill_mc_batch(std::span<const double> prices,
                                        std::span<const std::uint8_t> outcomes,
                                        std::span<const double> weights,
                                        std::span<const std::int64_t> offsets, std::uint32_t n_sims,
                                        std::uint64_t seed, unsigned n_threads) {
    validate(prices, outcomes, weights, n_sims);
    if (offsets.empty() || offsets.front() != 0 ||
        offsets.back() != static_cast<std::int64_t>(prices.size()))
        throw std::invalid_argument("offsets must start at 0 and end at len(prices)");
    for (std::size_t i = 1; i < offsets.size(); ++i)
        if (offsets[i] < offsets[i - 1]) throw std::invalid_argument("offsets must be non-decreasing");

    const std::size_t m = offsets.size() - 1;
    std::vector<SkillResult> out(m);
    unsigned threads = n_threads ? n_threads : std::max(1u, std::thread::hardware_concurrency());
    threads = static_cast<unsigned>(std::min<std::size_t>(threads, std::max<std::size_t>(m, 1)));

    std::atomic<std::size_t> next{0};
    auto worker = [&] {
        for (std::size_t i = next.fetch_add(1); i < m; i = next.fetch_add(1)) {
            const auto lo = static_cast<std::size_t>(offsets[i]);
            const auto hi = static_cast<std::size_t>(offsets[i + 1]);
            if (lo == hi) {
                out[i] = {std::numeric_limits<double>::quiet_NaN(), 1.0, 0.0, 0.0, 0.0};
                continue;
            }
            out[i] = run(prices.subspan(lo, hi - lo), outcomes.subspan(lo, hi - lo),
                         weights.subspan(lo, hi - lo), n_sims, splitmix64(seed ^ i));
        }
    };
    std::vector<std::thread> pool;
    for (unsigned k = 1; k < threads; ++k) pool.emplace_back(worker);
    worker();
    for (auto& t : pool) t.join();
    return out;
}

}  // namespace whalecore
