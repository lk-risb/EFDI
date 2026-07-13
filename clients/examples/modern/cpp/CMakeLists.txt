# Modern C++ EFDI pod examples — zenoh-cpp 1.9.0 (over zenoh-c).
#
#   cmake -B build -DCMAKE_BUILD_TYPE=Release
#   cmake --build build
#   ./build/publish ; ./build/subscribe
#
# Requires zenoh-cpp 1.9.0 installed (which itself needs zenoh-c 1.9.0). See README.md for how
# to get both, including offline. If you installed to a non-default prefix, pass it:
#   cmake -B build -DCMAKE_PREFIX_PATH=/path/to/zenoh-install

cmake_minimum_required(VERSION 3.16)
project(efdi_modern_cpp CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# zenoh-cpp ships a CMake package config. The C++ binding over zenoh-c exposes the
# zenohcxx::zenohc imported target (header-only C++ wrappers + the linked libzenohc).
find_package(zenohcxx REQUIRED)

# Path to the header-only connect helper: clients/connect/cpp/efdi_connect.hpp
set(EFDI_CONNECT_CPP_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../../connect/cpp"
    CACHE PATH "Directory containing efdi_connect.hpp")

foreach(example publish subscribe)
  add_executable(${example} ${example}.cpp)
  target_include_directories(${example} PRIVATE "${EFDI_CONNECT_CPP_DIR}")
  target_link_libraries(${example} PRIVATE zenohcxx::zenohc)
endforeach()
