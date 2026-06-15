//go:build ignore

// subscribe.go — receive data from the goat fabric (modern Go).
//
//	go run subscribe.go                            # your own namespace, follow forever
//	go run subscribe.go 'release/goat/**'   # inbound data goat sends you
//	go run subscribe.go '<keyexpr>' 5              # exit after 5 samples
//
// Default key-expr is '<namespace>/**' (everything under your prefix). Use ** for any depth,
// * for a single segment.
//
// Built as a standalone program (//go:build ignore) so it can `go run` independently from
// publish.go in the same directory. See README.md.
package main

import (
	"fmt"
	"os"
	"os/signal"
	"strconv"
	"sync/atomic"
	"time"
	"unicode/utf8"

	goatconnect "example.com/goat-moon-pod-examples-go/connect"

	"github.com/eclipse-zenoh/zenoh-go/zenoh"
)

func main() {
	zenoh.InitLoggerFromEnvOr("error")

	ns, err := goatconnect.Namespace()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	keyexpr := ns + "/**"
	if len(os.Args) > 1 {
		keyexpr = os.Args[1]
	}
	limit := 0 // 0 = follow forever
	if len(os.Args) > 2 {
		if v, err := strconv.Atoi(os.Args[2]); err == nil {
			limit = v
		}
	}

	s, err := goatconnect.Session()
	if err != nil {
		fmt.Fprintf(os.Stderr, "open session: %v\n", err)
		os.Exit(1)
	}
	defer s.Drop()

	ke, err := zenoh.NewKeyExpr(keyexpr)
	if err != nil {
		fmt.Fprintf(os.Stderr, "bad key %q: %v\n", keyexpr, err)
		os.Exit(1)
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt)

	var seen int64
	handler := zenoh.Closure[zenoh.Sample]{Call: func(sample zenoh.Sample) {
		raw := sample.Payload().Bytes()
		var shown string
		if utf8.Valid(raw) {
			shown = string(raw)
		} else {
			n := len(raw)
			if n > 32 {
				n = 32
			}
			shown = fmt.Sprintf("<%d bytes> %x", len(raw), raw[:n])
		}
		fmt.Printf("%s  %s  %s\n", time.Now().Format("15:04:05"), sample.KeyExpr().String(), shown)
		if c := atomic.AddInt64(&seen, 1); limit != 0 && c >= int64(limit) {
			// signal main loop to exit
			select {
			case stop <- os.Interrupt:
			default:
			}
		}
	}}

	sub, err := s.DeclareSubscriber(ke, handler, nil)
	if err != nil {
		fmt.Fprintf(os.Stderr, "declare subscriber: %v\n", err)
		os.Exit(1)
	}
	defer sub.Drop()

	fmt.Printf("subscribed: %s (Ctrl-C to stop)\n", keyexpr)
	<-stop
}
