#!/usr/bin/env bash
# Build and run the C++ unit tests without Python.
set -euo pipefail
cd "$(dirname "$0")/.."
cmake -S . -B build/cpp -DWHALECORE_BUILD_TESTS=ON -DWHALECORE_BUILD_PYTHON=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build build/cpp -j
ctest --test-dir build/cpp --output-on-failure
