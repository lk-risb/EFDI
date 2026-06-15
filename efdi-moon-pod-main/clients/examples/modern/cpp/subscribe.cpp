// subscribe.cpp — receive data from the goat fabric (modern C++ / zenoh-cpp 1.9.0).
//
//   ./subscribe                              # your own namespace (<ns>/**), follow forever
//   ./subscribe 'release/goat/**'     # inbound data goat sends you
//   ./subscribe '<keyexpr>' 5                # exit after 5 samples
//
// Default key-expr is <namespace>/** (everything under your prefix). Use ** for any depth,
// * for a single segment.

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <string>
#include <thread>

#include "goat_connect.hpp"  // from clients/connect/cpp (added to the include path by CMake)
#include "zenoh.hxx"

using namespace zenoh;

int main(int argc, char** argv) {
    try {
        const std::string keyexpr =
            argc > 1 ? std::string(argv[1]) : (goat::namespace_prefix() + "/**");
        const std::size_t limit =
            argc > 2 ? static_cast<std::size_t>(std::strtoul(argv[2], nullptr, 10)) : 0;  // 0 = forever

        auto session = goat::session();

        std::atomic<std::size_t> seen{0};
        std::atomic<bool> done{false};

        // The data handler runs on a zenoh worker thread — guard shared state (atomics here).
        auto on_sample = [&](const Sample& sample) {
            const std::string payload = sample.get_payload().as_string();
            const std::string_view key = sample.get_keyexpr().as_string_view();
            std::cout << key << "  " << payload << "\n";
            const std::size_t count = seen.fetch_add(1) + 1;
            if (limit != 0 && count >= limit) done.store(true);
        };

        auto sub = session.declare_subscriber(KeyExpr(keyexpr), on_sample, closures::none);
        std::cout << "subscribed: " << keyexpr << " (Ctrl-C to stop)\n";

        while (!done.load()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        return 0;
    } catch (const ZException& e) {
        std::cerr << "zenoh error: " << e.what() << "\n";
        return 1;
    } catch (const std::exception& e) {
        std::cerr << "error: " << e.what() << "\n";
        return 1;
    }
}
