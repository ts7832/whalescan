#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <span>
#include <utility>
#include <vector>

namespace whalecore {

enum class Side : std::uint8_t { Bid = 0, Ask = 1 };

struct WalkResult {
    double vwap;           // NaN when nothing filled
    double filled_usdc;
    double filled_shares;
    int levels_consumed;
    double worst_price;    // NaN when nothing filled
    bool complete;         // the full notional was filled
};

// L2 order book for one outcome token. Prices are stored as integer ticks of 0.0001
// (Polymarket tick sizes 0.01 / 0.001 are exact multiples), avoiding float map keys.
class OrderBook {
public:
    static constexpr double kTicksPerUnit = 10000.0;

    // Flat interleaved levels [price0, size0, price1, size1, ...]; replaces the whole book.
    void apply_snapshot(std::span<const double> bids, std::span<const double> asks);
    // size == 0 removes the level.
    void apply_delta(Side side, double price, double size);
    // One WS message worth of deltas in order; sides[i] is 0 = bid, 1 = ask. Validates before mutating.
    void apply_deltas(std::span<const std::uint8_t> sides, std::span<const double> prices,
                      std::span<const double> sizes);
    void clear();

    std::optional<double> best_bid() const;
    std::optional<double> best_ask() const;
    std::optional<double> mid() const;
    std::optional<double> spread() const;
    std::optional<double> microprice() const;
    // USDC resting within `ticks_from_best` ticks of the best price on `side`.
    double depth(Side side, int ticks_from_best) const;
    // Side::Ask consumes asks (a buy); Side::Bid consumes bids (a sell).
    WalkResult walk(Side side, double notional_usdc) const;
    // (bid size - ask size) / total over the top `levels` levels; 0 when empty.
    double imbalance(int levels) const;
    bool crossed() const;
    // Best `n` (price, size) levels of one side, best first.
    std::vector<std::pair<double, double>> levels(Side side, std::size_t n) const;
    std::size_t level_count(Side side) const;

    static std::int32_t to_ticks(double price);
    static double to_price(std::int32_t ticks);

private:
    std::map<std::int32_t, double, std::greater<>> bids_;
    std::map<std::int32_t, double> asks_;
};

}  // namespace whalecore
