#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_floating_point.hpp>

#include <cmath>
#include <cstdint>
#include <random>
#include <stdexcept>
#include <vector>

#include "whalecore/montecarlo.hpp"

using Catch::Matchers::WithinAbs;
using whalecore::skill_mc;
using whalecore::skill_mc_batch;

TEST_CASE("edge is the stake-weighted mean of outcome minus price") {
    std::vector<double> p{0.2, 0.4};
    std::vector<std::uint8_t> y{1, 1};
    std::vector<double> w{1.0, 3.0};
    const auto r = skill_mc(p, y, w, 1000, 1);
    REQUIRE_THAT(r.edge, WithinAbs(0.65, 1e-12));      // (0.8*1 + 0.6*3) / 4
    REQUIRE_THAT(r.n_eff, WithinAbs(1.6, 1e-12));      // 4^2 / 10
}

TEST_CASE("a wallet that lost every coin flip is not significant") {
    std::vector<double> p(50, 0.5), w(50, 1.0);
    std::vector<std::uint8_t> y(50, 0);
    REQUIRE(skill_mc(p, y, w, 5000, 3).p_value > 0.99);
}

TEST_CASE("an impossible record gets the minimum p-value") {
    std::vector<double> p(200, 0.3), w(200, 1.0);
    std::vector<std::uint8_t> y(200, 1);
    REQUIRE_THAT(skill_mc(p, y, w, 10000, 5).p_value, WithinAbs(1.0 / 10001.0, 1e-15));
}

TEST_CASE("null distribution is centred on zero") {
    std::mt19937_64 gen(9);
    std::uniform_real_distribution<double> up(0.1, 0.9), uw(1.0, 50.0);
    std::vector<double> p(80), w(80);
    std::vector<std::uint8_t> y(80, 1);
    for (int i = 0; i < 80; ++i) { p[i] = up(gen); w[i] = uw(gen); }
    const auto r = skill_mc(p, y, w, 20000, 13);
    REQUIRE(std::abs(r.null_mean) < 4.0 * r.null_sd / std::sqrt(20000.0));
}

TEST_CASE("p-values of no-skill wallets are calibrated") {
    constexpr int wallets = 2000, per = 50;
    std::mt19937_64 gen(7);
    std::uniform_real_distribution<double> up(0.1, 0.9), uw(1.0, 100.0), u(0.0, 1.0);
    std::vector<double> p, w;
    std::vector<std::uint8_t> y;
    std::vector<std::int64_t> offsets{0};
    for (int k = 0; k < wallets; ++k) {
        for (int i = 0; i < per; ++i) {
            const double pi = up(gen);
            p.push_back(pi);
            w.push_back(uw(gen));
            y.push_back(u(gen) < pi ? 1 : 0);
        }
        offsets.push_back(offsets.back() + per);
    }
    const auto res = skill_mc_batch(p, y, w, offsets, 2000, 11, 0);
    int below = 0;
    for (const auto& r : res) below += r.p_value < 0.1 ? 1 : 0;
    const double frac = below / static_cast<double>(wallets);
    REQUIRE(frac > 0.08);
    REQUIRE(frac < 0.12);
}

TEST_CASE("batch results do not depend on thread count") {
    std::mt19937_64 gen(21);
    std::uniform_real_distribution<double> up(0.1, 0.9), u(0.0, 1.0);
    std::vector<double> p, w;
    std::vector<std::uint8_t> y;
    std::vector<std::int64_t> offsets{0};
    for (int k = 0; k < 50; ++k) {
        for (int i = 0; i < 30; ++i) { const double pi = up(gen); p.push_back(pi); w.push_back(1.0 + i); y.push_back(u(gen) < pi); }
        offsets.push_back(offsets.back() + 30);
    }
    const auto a = skill_mc_batch(p, y, w, offsets, 3000, 99, 1);
    const auto b = skill_mc_batch(p, y, w, offsets, 3000, 99, 4);
    REQUIRE(a.size() == 50);
    for (std::size_t i = 0; i < a.size(); ++i) {
        REQUIRE(a[i].edge == b[i].edge);
        REQUIRE(a[i].p_value == b[i].p_value);
    }
}

TEST_CASE("empty wallet slot yields NaN edge and p = 1") {
    std::vector<double> p{0.5}, w{1.0};
    std::vector<std::uint8_t> y{1};
    std::vector<std::int64_t> offsets{0, 0, 1};
    const auto res = skill_mc_batch(p, y, w, offsets, 100, 1, 2);
    REQUIRE(std::isnan(res[0].edge));
    REQUIRE(res[0].p_value == 1.0);
    REQUIRE(res[1].edge == 0.5);
}

TEST_CASE("invalid inputs throw") {
    std::vector<double> p{0.5}, w{1.0};
    std::vector<std::uint8_t> y{1};
    std::vector<double> bad_p{1.0}, bad_w{0.0}, two{0.5, 0.5};
    std::vector<std::uint8_t> bad_y{2};
    REQUIRE_THROWS_AS(skill_mc(two, y, w, 10, 1), std::invalid_argument);
    REQUIRE_THROWS_AS(skill_mc(bad_p, y, w, 10, 1), std::invalid_argument);
    REQUIRE_THROWS_AS(skill_mc(p, bad_y, w, 10, 1), std::invalid_argument);
    REQUIRE_THROWS_AS(skill_mc(p, y, bad_w, 10, 1), std::invalid_argument);
    REQUIRE_THROWS_AS(skill_mc(p, y, w, 0, 1), std::invalid_argument);
    std::vector<double> none;
    std::vector<std::uint8_t> none_y;
    REQUIRE_THROWS_AS(skill_mc(none, none_y, none, 10, 1), std::invalid_argument);
    std::vector<std::int64_t> bad_offsets{0, 2};
    REQUIRE_THROWS_AS(skill_mc_batch(p, y, w, bad_offsets, 10, 1, 1), std::invalid_argument);
}
