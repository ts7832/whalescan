#include "whalecore/orderbook.hpp"

#include <cmath>
#include <limits>
#include <stdexcept>

namespace whalecore {
namespace {

void check_size(double size) {
    if (!(size >= 0.0) || !std::isfinite(size)) throw std::invalid_argument("size must be finite and >= 0");
}

template <class Map>
void load(Map& book, std::span<const double> flat) {
    if (flat.size() % 2 != 0) throw std::invalid_argument("levels must be (price, size) pairs");
    book.clear();
    for (std::size_t i = 0; i < flat.size(); i += 2) {
        check_size(flat[i + 1]);
        if (flat[i + 1] > 0.0) book[OrderBook::to_ticks(flat[i])] = flat[i + 1];
    }
}

template <class Map>
void walk_levels(const Map& book, double notional, WalkResult& r) {
    double remaining = notional;
    for (const auto& [ticks, size] : book) {
        if (remaining <= 0.0) break;
        const double px = OrderBook::to_price(ticks);
        if (px <= 0.0) continue;
        const double cost = px * size;
        ++r.levels_consumed;
        r.worst_price = px;
        if (cost >= remaining) {
            r.filled_shares += remaining / px;
            r.filled_usdc += remaining;
            remaining = 0.0;
        } else {
            r.filled_shares += size;
            r.filled_usdc += cost;
            remaining -= cost;
        }
    }
    r.complete = remaining <= 1e-9 * notional;
}

template <class Map>
double top_size(const Map& book, int levels) {
    double s = 0.0;
    int k = 0;
    for (auto it = book.begin(); it != book.end() && k < levels; ++it, ++k) s += it->second;
    return s;
}

}  // namespace

std::int32_t OrderBook::to_ticks(double price) {
    if (!(price >= 0.0 && price <= 1.0)) throw std::invalid_argument("price must be within [0, 1]");
    const double scaled = price * kTicksPerUnit;
    const double rounded = std::nearbyint(scaled);
    // Every Polymarket tick size (0.1 .. 0.0001) is a multiple of 0.0001; anything else means a corrupt
    // message, and silently rounding it would merge distinct levels.
    if (std::abs(scaled - rounded) > 1e-6) throw std::invalid_argument("price is not on the 0.0001 tick grid");
    return static_cast<std::int32_t>(rounded);
}

double OrderBook::to_price(std::int32_t ticks) { return ticks / kTicksPerUnit; }

void OrderBook::apply_snapshot(std::span<const double> bids, std::span<const double> asks) {
    load(bids_, bids);
    load(asks_, asks);
}

void OrderBook::apply_delta(Side side, double price, double size) {
    check_size(size);
    const auto t = to_ticks(price);
    if (side == Side::Bid) {
        if (size == 0.0) bids_.erase(t); else bids_[t] = size;
    } else {
        if (size == 0.0) asks_.erase(t); else asks_[t] = size;
    }
}

void OrderBook::apply_deltas(std::span<const std::uint8_t> sides, std::span<const double> prices,
                             std::span<const double> sizes) {
    if (sides.size() != prices.size() || sides.size() != sizes.size())
        throw std::invalid_argument("sides, prices and sizes must have equal length");
    for (std::size_t i = 0; i < sides.size(); ++i) {  // validate everything first: all-or-nothing
        if (sides[i] > 1) throw std::invalid_argument("side must be 0 (bid) or 1 (ask)");
        check_size(sizes[i]);
        to_ticks(prices[i]);
    }
    for (std::size_t i = 0; i < sides.size(); ++i)
        apply_delta(sides[i] ? Side::Ask : Side::Bid, prices[i], sizes[i]);
}

void OrderBook::clear() {
    bids_.clear();
    asks_.clear();
}

std::optional<double> OrderBook::best_bid() const {
    if (bids_.empty()) return std::nullopt;
    return to_price(bids_.begin()->first);
}

std::optional<double> OrderBook::best_ask() const {
    if (asks_.empty()) return std::nullopt;
    return to_price(asks_.begin()->first);
}

std::optional<double> OrderBook::mid() const {
    const auto b = best_bid(), a = best_ask();
    if (!b || !a) return std::nullopt;
    return (*b + *a) / 2.0;
}

std::optional<double> OrderBook::spread() const {
    const auto b = best_bid(), a = best_ask();
    if (!b || !a) return std::nullopt;
    return *a - *b;
}

std::optional<double> OrderBook::microprice() const {
    const auto b = best_bid(), a = best_ask();
    if (!b || !a) return std::nullopt;
    const double bs = bids_.begin()->second, as = asks_.begin()->second;
    return (*b * as + *a * bs) / (bs + as);
}

double OrderBook::depth(Side side, int ticks_from_best) const {
    double usdc = 0.0;
    auto sum = [&](const auto& book) {
        if (book.empty()) return;
        const auto best = book.begin()->first;
        for (const auto& [t, sz] : book) {
            if (std::abs(t - best) > ticks_from_best) break;
            usdc += to_price(t) * sz;
        }
    };
    if (side == Side::Bid) sum(bids_); else sum(asks_);
    return usdc;
}

WalkResult OrderBook::walk(Side side, double notional_usdc) const {
    if (!(notional_usdc > 0.0) || !std::isfinite(notional_usdc))
        throw std::invalid_argument("notional_usdc must be positive");
    constexpr double nan = std::numeric_limits<double>::quiet_NaN();
    WalkResult r{nan, 0.0, 0.0, 0, nan, false};
    if (side == Side::Ask) walk_levels(asks_, notional_usdc, r); else walk_levels(bids_, notional_usdc, r);
    if (r.filled_shares > 0.0) r.vwap = r.filled_usdc / r.filled_shares;
    return r;
}

double OrderBook::imbalance(int levels) const {
    const double b = top_size(bids_, levels), a = top_size(asks_, levels);
    return (a + b) > 0.0 ? (b - a) / (b + a) : 0.0;
}

bool OrderBook::crossed() const {
    return !bids_.empty() && !asks_.empty() && bids_.begin()->first >= asks_.begin()->first;
}

std::size_t OrderBook::level_count(Side side) const {
    return side == Side::Bid ? bids_.size() : asks_.size();
}

}  // namespace whalecore
