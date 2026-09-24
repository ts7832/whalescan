#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_floating_point.hpp>

#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <vector>

#include "whalecore/orderbook.hpp"

using Catch::Matchers::WithinAbs;
using whalecore::OrderBook;
using whalecore::Side;

static OrderBook make_book() {
    OrderBook b;
    // Polymarket sends bids ascending; order must not matter.
    std::vector<double> bids{0.39, 10, 0.399, 50, 0.40, 100};
    std::vector<double> asks{0.50, 100, 0.52, 200, 0.55, 1000};
    b.apply_snapshot(bids, asks);
    return b;
}

TEST_CASE("snapshot sets best levels") {
    const auto b = make_book();
    REQUIRE_THAT(*b.best_bid(), WithinAbs(0.40, 1e-12));
    REQUIRE_THAT(*b.best_ask(), WithinAbs(0.50, 1e-12));
    REQUIRE_THAT(*b.mid(), WithinAbs(0.45, 1e-12));
    REQUIRE_THAT(*b.spread(), WithinAbs(0.10, 1e-12));
    REQUIRE(b.level_count(Side::Bid) == 3);
}

TEST_CASE("deltas update and erase levels") {
    auto b = make_book();
    b.apply_delta(Side::Bid, 0.40, 0.0);
    REQUIRE_THAT(*b.best_bid(), WithinAbs(0.399, 1e-12));
    b.apply_delta(Side::Ask, 0.49, 25.0);
    REQUIRE_THAT(*b.best_ask(), WithinAbs(0.49, 1e-12));
    b.apply_delta(Side::Ask, 0.49, 5.0);
    REQUIRE(b.level_count(Side::Ask) == 4);
}

TEST_CASE("snapshot replaces previous state") {
    auto b = make_book();
    std::vector<double> bids{0.10, 1}, asks{0.90, 1};
    b.apply_snapshot(bids, asks);
    REQUIRE(b.level_count(Side::Bid) == 1);
    REQUIRE_THAT(*b.best_ask(), WithinAbs(0.90, 1e-12));
}

TEST_CASE("microprice leans toward the thinner side") {
    OrderBook b;
    std::vector<double> bids{0.40, 100}, asks{0.42, 300};
    b.apply_snapshot(bids, asks);
    REQUIRE_THAT(*b.microprice(), WithinAbs(0.405, 1e-12));  // (0.40*300 + 0.42*100) / 400
}

TEST_CASE("walking the asks across levels") {
    const auto b = make_book();
    const auto w = b.walk(Side::Ask, 100.0);  // $50 at 0.50, then $50 at 0.52
    const double shares = 100.0 + 50.0 / 0.52;
    REQUIRE(w.complete);
    REQUIRE(w.levels_consumed == 2);
    REQUIRE_THAT(w.filled_usdc, WithinAbs(100.0, 1e-9));
    REQUIRE_THAT(w.filled_shares, WithinAbs(shares, 1e-9));
    REQUIRE_THAT(w.vwap, WithinAbs(100.0 / shares, 1e-12));
    REQUIRE_THAT(w.worst_price, WithinAbs(0.52, 1e-12));
}

TEST_CASE("walking the bids sells into the highest bids first") {
    const auto b = make_book();
    const auto w = b.walk(Side::Bid, 20.0);
    REQUIRE(w.levels_consumed == 1);
    REQUIRE_THAT(w.vwap, WithinAbs(0.40, 1e-12));
}

TEST_CASE("walk beyond available liquidity is incomplete") {
    OrderBook b;
    std::vector<double> bids{}, asks{0.5, 10};
    b.apply_snapshot(bids, asks);
    const auto w = b.walk(Side::Ask, 100.0);
    REQUIRE_FALSE(w.complete);
    REQUIRE_THAT(w.filled_usdc, WithinAbs(5.0, 1e-12));
}

TEST_CASE("walk on empty book") {
    OrderBook b;
    const auto w = b.walk(Side::Ask, 100.0);
    REQUIRE_FALSE(w.complete);
    REQUIRE(std::isnan(w.vwap));
    REQUIRE(w.levels_consumed == 0);
    REQUIRE_FALSE(b.best_ask().has_value());
    REQUIRE_FALSE(b.microprice().has_value());
}

TEST_CASE("depth sums USDC within N ticks of best") {
    const auto b = make_book();
    // tick = 0.0001; 0.40 and 0.399 are within 10 ticks, 0.39 is not
    REQUIRE_THAT(b.depth(Side::Bid, 10), WithinAbs(0.40 * 100 + 0.399 * 50, 1e-9));
}

TEST_CASE("imbalance over top levels") {
    OrderBook b;
    std::vector<double> bids{0.40, 300}, asks{0.42, 100};
    b.apply_snapshot(bids, asks);
    REQUIRE_THAT(b.imbalance(5), WithinAbs(0.5, 1e-12));
}

TEST_CASE("crossed book is detected") {
    auto b = make_book();
    REQUIRE_FALSE(b.crossed());
    b.apply_delta(Side::Bid, 0.60, 1.0);
    REQUIRE(b.crossed());
}

TEST_CASE("invalid input throws") {
    OrderBook b;
    std::vector<double> odd{0.5}, ok{};
    REQUIRE_THROWS_AS(b.apply_snapshot(odd, ok), std::invalid_argument);
    REQUIRE_THROWS_AS(b.apply_delta(Side::Bid, 1.5, 1.0), std::invalid_argument);
    REQUIRE_THROWS_AS(b.apply_delta(Side::Bid, 0.5, -1.0), std::invalid_argument);
    REQUIRE_THROWS_AS(b.walk(Side::Ask, 0.0), std::invalid_argument);
}

TEST_CASE("apply_deltas equals applying each delta in order") {
    auto a = make_book();
    auto b = make_book();
    std::vector<std::uint8_t> sides{0, 1, 1, 0};
    std::vector<double> prices{0.40, 0.50, 0.49, 0.41};
    std::vector<double> sizes{0.0, 0.0, 30.0, 5.0};
    a.apply_deltas(sides, prices, sizes);
    for (std::size_t i = 0; i < sides.size(); ++i)
        b.apply_delta(sides[i] ? Side::Ask : Side::Bid, prices[i], sizes[i]);
    REQUIRE(*a.best_bid() == *b.best_bid());
    REQUIRE(*a.best_ask() == *b.best_ask());
    REQUIRE(a.level_count(Side::Bid) == b.level_count(Side::Bid));
    REQUIRE_THAT(*a.best_bid(), WithinAbs(0.41, 1e-12));
    REQUIRE_THAT(*a.best_ask(), WithinAbs(0.49, 1e-12));
}

TEST_CASE("apply_deltas rejects mismatched lengths") {
    auto b = make_book();
    std::vector<std::uint8_t> sides{0};
    std::vector<double> prices{0.4, 0.5}, sizes{1.0};
    REQUIRE_THROWS_AS(b.apply_deltas(sides, prices, sizes), std::invalid_argument);
}

TEST_CASE("prices off the 0.0001 grid are rejected, grid prices are exact") {
    OrderBook b;
    REQUIRE_THROWS_AS(b.apply_delta(Side::Bid, 0.40005, 1.0), std::invalid_argument);
    b.apply_delta(Side::Bid, 0.0001, 1.0);
    b.apply_delta(Side::Ask, 0.9999, 1.0);
    REQUIRE_THAT(*b.best_bid(), WithinAbs(0.0001, 1e-15));
    REQUIRE_THAT(*b.best_ask(), WithinAbs(0.9999, 1e-15));
}
