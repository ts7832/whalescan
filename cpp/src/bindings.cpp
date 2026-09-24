#include <nanobind/nanobind.h>

#include "whalecore/version.hpp"

namespace nb = nanobind;

NB_MODULE(whalecore, m) {
    m.doc() = "WHALESCAN C++ core: Monte Carlo skill engine and order book";
    m.def("version", &whalecore::version, "Library version string.");
}
