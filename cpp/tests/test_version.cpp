#include <catch2/catch_test_macros.hpp>
#include <string>

#include "whalecore/version.hpp"

TEST_CASE("version string is semver") {
    REQUIRE(std::string(whalecore::version()) == "0.1.0");
}
