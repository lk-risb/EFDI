// publish.cpp — send data to the goat fabric (modern C++ / zenoh-cpp 1.9.0).
//
//   export GOAT_ROUTER=tls/127.0.0.1:7447 GOAT_CERT=... GOAT_KEY=... GOAT_CA=... \
//          GOAT_NAMESPACE=release/acme
//   ./publish              # one JSON sample
//   ./publish 50 0.2       # 50 samples, 200ms apart
//
// Publishes JSON under <namespace>/sensors/temp. Real payloads can be anything (bytes,
// protobuf, CBOR); JSON here for legibility — the fabric is payload-agnostic.

#include <chrono>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>

#include "goat_connect.hpp"  // from clients/connect/cpp (added to the include path by CMake)
#include "zenoh.hxx"

using namespace zenoh;

int main(int argc, char** argv) {
    try {
        const std::size_t n =
            argc > 1 ? static_cast<std::size_t>(std::strtoul(argv[1], nullptr, 10)) : 1;
        const double interval = argc > 2 ? std::strtod(argv[2], nullptr) : 1.0;

        const std::string key = goat::key("sensors/temp");

        auto session = goat::session();
        auto pub = session.declare_publisher(KeyExpr(key));

        for (std::size_t i = 0; i < n; ++i) {
            const double ts =
                std::chrono::duration<double>(
                    std::chrono::system_clock::now().time_since_epoch())
                    .count();

            std::ostringstream payload;
            payload << "{\"ts\":" << ts << ",\"seq\":" << i
                    << ",\"temp_c\":" << (21.5 + static_cast<double>(i) * 0.1) << "}";
            const std::string body = payload.str();

            pub.put(body);
            std::cout << "published -> " << key << ": " << body << "\n";

            if (i + 1 < n) {
                std::this_thread::sleep_for(
                    std::chrono::duration<double>(interval));
            }
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
