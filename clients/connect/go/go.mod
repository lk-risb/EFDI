// Module for the goat connect helper (package efdiconnect). Mapped into the examples module
// via a replace directive in ../../examples/modern/go/go.mod. The module path is the one the
// examples import; the directory name (go/) differs from the package name (efdiconnect).
module example.com/efdi-examples-go/connect

go 1.22

require github.com/eclipse-zenoh/zenoh-go v1.9.0
