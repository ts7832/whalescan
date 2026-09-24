cmake_minimum_required(VERSION 3.26)
project(whalecore LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(WHALECORE_BUILD_PYTHON "Build the Python extension module" ON)
option(WHALECORE_BUILD_TESTS "Build the Catch2 test executable" OFF)

find_package(Threads REQUIRED)

add_library(whalecore_lib STATIC
  cpp/src/version.cpp
  cpp/src/montecarlo.cpp
)
target_include_directories(whalecore_lib PUBLIC cpp/include)
target_link_libraries(whalecore_lib PUBLIC Threads::Threads)
set_target_properties(whalecore_lib PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_compile_options(whalecore_lib PRIVATE -O3 -Wall -Wextra -Wpedantic)

if(WHALECORE_BUILD_PYTHON)
  find_package(Python 3.12 REQUIRED COMPONENTS Interpreter Development.Module)
  find_package(nanobind CONFIG REQUIRED)
  nanobind_add_module(whalecore NB_STATIC cpp/src/bindings.cpp)
  target_link_libraries(whalecore PRIVATE whalecore_lib)
  install(TARGETS whalecore LIBRARY DESTINATION .)
endif()

if(WHALECORE_BUILD_TESTS)
  include(FetchContent)
  FetchContent_Declare(Catch2
    GIT_REPOSITORY https://github.com/catchorg/Catch2.git
    GIT_TAG v3.7.1)
  FetchContent_MakeAvailable(Catch2)
  add_executable(whalecore_tests
    cpp/tests/test_version.cpp
    cpp/tests/test_montecarlo.cpp
  )
  target_link_libraries(whalecore_tests PRIVATE whalecore_lib Catch2::Catch2WithMain)
  include(CTest)
  list(APPEND CMAKE_MODULE_PATH ${catch2_SOURCE_DIR}/extras)
  include(Catch)
  catch_discover_tests(whalecore_tests)
endif()
