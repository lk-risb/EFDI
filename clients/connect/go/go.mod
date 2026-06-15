// Module for the goat connect helper (package goatconnect). Mapped into the examples module
// via a replace directive in ../../examples/modern/go/go.mod. The module path is the one the
// examples import; the directory name (go/) differs from the package name (goatconnect).
module example.com/goat-moon-pod-examples-go/connect

go 1.22

require github.com/eclipse-zenoh/zenoh-go v1.9.0
