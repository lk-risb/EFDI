module example.com/efdi-examples-go

go 1.22

// The official Go binding for Zenoh 1.x. Wraps zenoh-c via cgo, so zenoh-c must be installed
// and discoverable at build/link time (see README.md). Pinned to the fleet's Zenoh 1.9.0.
require github.com/eclipse-zenoh/zenoh-go v1.9.0

// The goat-specific connect helper lives in this repo at clients/connect/go (package
// efdiconnect). We map its import path to that directory so the examples build in-tree
// without publishing the helper as a separate module.
require example.com/efdi-examples-go/connect v0.0.0

replace example.com/efdi-examples-go/connect => ../../../connect/go
