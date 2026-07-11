//go:build ignore

// publish.go — send data to the goat fabric (modern Go).
//
//	export EFDI_ROUTER=tls/127.0.0.1:7447 EFDI_CERT=... EFDI_KEY=... EFDI_CA=... PARTNER_NAMESPACE=release/acme
//	go run publish.go            # one JSON sample
//	go run publish.go 50 0.2     # 50 samples at 200ms
//
// Publishes JSON under <namespace>/sensors/temp. Real payloads can be anything (bytes,
// protobuf, CBOR); JSON here for legibility.
//
// Built as a standalone program (//go:build ignore) so publish.go and subscribe.go can each
// be `go run` independently from the same directory without colliding main()s. See README.md.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"time"

	efdiconnect "example.com/efdi-examples-go/connect"

	"github.com/eclipse-zenoh/zenoh-go/zenoh"
)

func main() {
	zenoh.InitLoggerFromEnvOr("error")

	n := 1
	interval := 1.0
	if len(os.Args) > 1 {
		if v, err := strconv.Atoi(os.Args[1]); err == nil {
			n = v
		}
	}
	if len(os.Args) > 2 {
		if v, err := strconv.ParseFloat(os.Args[2], 64); err == nil {
			interval = v
		}
	}

	keyStr, err := efdiconnect.Key("sensors/temp")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	s, err := efdiconnect.Session()
	if err != nil {
		fmt.Fprintf(os.Stderr, "open session: %v\n", err)
		os.Exit(1)
	}
	defer s.Drop()

	ke, err := zenoh.NewKeyExpr(keyStr)
	if err != nil {
		fmt.Fprintf(os.Stderr, "bad key %q: %v\n", keyStr, err)
		os.Exit(1)
	}

	pub, err := s.DeclarePublisher(ke, nil)
	if err != nil {
		fmt.Fprintf(os.Stderr, "declare publisher: %v\n", err)
		os.Exit(1)
	}
	defer pub.Drop()

	for i := 0; i < n; i++ {
		payload, _ := json.Marshal(map[string]any{
			"ts":     float64(time.Now().UnixNano()) / 1e9,
			"seq":    i,
			"temp_c": 21.5 + float64(i)*0.1,
		})
		if err := pub.Put(zenoh.NewZBytes(payload), nil); err != nil {
			fmt.Fprintf(os.Stderr, "put: %v\n", err)
		} else {
			fmt.Printf("published -> %s: %s\n", keyStr, payload)
		}
		if i+1 < n {
			time.Sleep(time.Duration(interval * float64(time.Second)))
		}
	}
}
