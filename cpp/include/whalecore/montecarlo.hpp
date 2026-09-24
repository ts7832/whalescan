#pragma once

#include <cstdint>
#include <span>
#include <vector>

namespace whalecore {

// Result of testing one betting record against the "no skill" null hypothesis,
// under which a position bought at price p wins with probability exactly p.
struct SkillResult {
    double edge;       // stake-weighted mean of (outcome - price)
    double p_value;    // (1 + #{simulated edge >= observed}) / (1 + n_sims)
    double null_mean;  // mean simulated edge (should be ~0)
    double null_sd;    // sd of simulated edge
    double n_eff;      // effective sample size (sum w)^2 / sum w^2
};

// prices in (0,1), outcomes 0/1, weights > 0, equal non-zero length; n_sims >= 1.
SkillResult skill_mc(std::span<const double> prices, std::span<const std::uint8_t> outcomes,
                     std::span<const double> weights, std::uint32_t n_sims, std::uint64_t seed);

// Many records in CSR layout: record i owns rows [offsets[i], offsets[i+1]).
// Each record i uses seed splitmix64(seed ^ i), so results are independent of n_threads.
// n_threads == 0 means hardware concurrency. Empty records yield {NaN, 1, 0, 0, 0}.
std::vector<SkillResult> skill_mc_batch(std::span<const double> prices,
                                        std::span<const std::uint8_t> outcomes,
                                        std::span<const double> weights,
                                        std::span<const std::int64_t> offsets, std::uint32_t n_sims,
                                        std::uint64_t seed, unsigned n_threads);

}  // namespace whalecore
