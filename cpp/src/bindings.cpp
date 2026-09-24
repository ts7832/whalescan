#include <nanobind/nanobind.h>
#include <nanobind/ndarray.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/pair.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>

#include <cstdint>
#include <span>
#include <string>

#include "whalecore/montecarlo.hpp"
#include "whalecore/orderbook.hpp"
#include "whalecore/version.hpp"

namespace nb = nanobind;
using namespace nb::literals;

template <class T>
using Vec = nb::ndarray<const T, nb::ndim<1>, nb::c_contig, nb::device::cpu>;

using Levels = nb::ndarray<const double, nb::shape<-1, 2>, nb::c_contig, nb::device::cpu>;

static std::span<const double> flat(const Levels& a) { return {a.data(), a.shape(0) * 2}; }

template <class T>
std::span<const T> as_span(const Vec<T>& a) {
    return {a.data(), a.shape(0)};
}

NB_MODULE(whalecore, m) {
    m.doc() = "WHALESCAN C++ core: Monte Carlo skill engine and order book";
    m.def("version", &whalecore::version, "Library version string.");

    nb::class_<whalecore::SkillResult>(m, "SkillResult")
        .def_ro("edge", &whalecore::SkillResult::edge)
        .def_ro("p_value", &whalecore::SkillResult::p_value)
        .def_ro("null_mean", &whalecore::SkillResult::null_mean)
        .def_ro("null_sd", &whalecore::SkillResult::null_sd)
        .def_ro("n_eff", &whalecore::SkillResult::n_eff)
        .def("__repr__", [](const whalecore::SkillResult& r) {
            return "SkillResult(edge=" + std::to_string(r.edge) + ", p_value=" + std::to_string(r.p_value) +
                   ", n_eff=" + std::to_string(r.n_eff) + ")";
        });

    m.def(
        "skill_mc",
        [](Vec<double> prices, Vec<std::uint8_t> outcomes, Vec<double> weights, std::uint32_t n_sims,
           std::uint64_t seed) {
            const auto p = as_span(prices);
            const auto y = as_span(outcomes);
            const auto w = as_span(weights);
            nb::gil_scoped_release release;  // pure C++ from here; let other Python threads run
            return whalecore::skill_mc(p, y, w, n_sims, seed);
        },
        "prices"_a, "outcomes"_a, "weights"_a, "n_sims"_a = 100000, "seed"_a = 42,
        "Monte Carlo test of one betting record against the no-skill null.");

    m.def(
        "skill_mc_batch",
        [](Vec<double> prices, Vec<std::uint8_t> outcomes, Vec<double> weights, Vec<std::int64_t> offsets,
           std::uint32_t n_sims, std::uint64_t seed, unsigned n_threads) {
            const auto p = as_span(prices);
            const auto y = as_span(outcomes);
            const auto w = as_span(weights);
            const auto o = as_span(offsets);
            nb::gil_scoped_release release;
            return whalecore::skill_mc_batch(p, y, w, o, n_sims, seed, n_threads);
        },
        "prices"_a, "outcomes"_a, "weights"_a, "offsets"_a, "n_sims"_a = 100000, "seed"_a = 42,
        "n_threads"_a = 0, "Multithreaded skill_mc over CSR-packed records.");

    using whalecore::OrderBook;
    using whalecore::Side;
    using whalecore::WalkResult;

    nb::enum_<Side>(m, "Side").value("BID", Side::Bid).value("ASK", Side::Ask);

    nb::class_<WalkResult>(m, "WalkResult")
        .def_ro("vwap", &WalkResult::vwap)
        .def_ro("filled_usdc", &WalkResult::filled_usdc)
        .def_ro("filled_shares", &WalkResult::filled_shares)
        .def_ro("levels_consumed", &WalkResult::levels_consumed)
        .def_ro("worst_price", &WalkResult::worst_price)
        .def_ro("complete", &WalkResult::complete);

    nb::class_<OrderBook>(m, "OrderBook")
        .def(nb::init<>())
        .def("apply_snapshot",
             [](OrderBook& b, Levels bids, Levels asks) { b.apply_snapshot(flat(bids), flat(asks)); },
             "bids"_a, "asks"_a, "Replace the book with (price, size) rows.")
        .def("apply_delta", &OrderBook::apply_delta, "side"_a, "price"_a, "size"_a)
        .def("apply_deltas",
             [](OrderBook& b, Vec<std::uint8_t> sides, Vec<double> prices, Vec<double> sizes) {
                 b.apply_deltas(as_span(sides), as_span(prices), as_span(sizes));
             },
             "sides"_a, "prices"_a, "sizes"_a, "Apply a batch of deltas (side 0 = bid, 1 = ask) in order.")
        .def("clear", &OrderBook::clear)
        .def("best_bid", &OrderBook::best_bid)
        .def("best_ask", &OrderBook::best_ask)
        .def("mid", &OrderBook::mid)
        .def("spread", &OrderBook::spread)
        .def("microprice", &OrderBook::microprice)
        .def("depth", &OrderBook::depth, "side"_a, "ticks_from_best"_a)
        .def("walk", &OrderBook::walk, "side"_a, "notional_usdc"_a)
        .def("imbalance", &OrderBook::imbalance, "levels"_a = 5)
        .def("crossed", &OrderBook::crossed)
        .def("levels", &OrderBook::levels, "side"_a, "n"_a = 10)
        .def("level_count", &OrderBook::level_count, "side"_a);
}
