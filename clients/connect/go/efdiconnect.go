// Package efdiconnect — open an mTLS Zenoh session to an EFDI pod from env vars.
//
// The ONLY EFDI-specific code you need. Everything else is plain Zenoh (the official
// eclipse-zenoh/zenoh-go 1.x binding, which wraps zenoh-c via cgo).
//
//	import "<your-module>/connect/go" // package efdiconnect
//	s, _ := efdiconnect.Session()
//	defer s.Drop()
//	ke, _ := zenoh.NewKeyExpr(efdiconnect.Key("sensors/temp"))
//	pub, _ := s.DeclarePublisher(ke, nil)
//	pub.Put(zenoh.NewZBytesFromString("21.5"), nil)
//
// Env vars (see ../../README.md):
//
//	EFDI_ROUTER       tls/127.0.0.1:7447     pod Zenoh endpoint
//	EFDI_CERT         path to your mTLS client cert (PEM)
//	EFDI_KEY          path to your mTLS private key (PEM)
//	EFDI_CA           path to the CA root that signs the router (PEM)
//	PARTNER_NAMESPACE    release/<you>          your owned prefix (publish under this)
//	EFDI_VERIFY_NAME  "true"/"false"         TLS hostname verification (default false for a
//	                                         local pod reached at 127.0.0.1; "true" for a
//	                                         DNS-named remote router)
//
// Build/link: this package transitively cgo-links zenoh-c. zenoh-c must be installed and
// discoverable (CGO_CFLAGS / CGO_LDFLAGS / LD_LIBRARY_PATH). See ../../examples/modern/go/README.md.
package efdiconnect

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/eclipse-zenoh/zenoh-go/zenoh"
)

// env returns the value of name or an actionable error if unset/empty.
func env(name string) (string, error) {
	v := os.Getenv(name)
	if v == "" {
		return "", fmt.Errorf(
			"%s is not set. Source the pod env (see clients/README.md), e.g.\n  export %s=...",
			name, name)
	}
	return v, nil
}

// tlsBlock is the json5 object inserted at transport/link/tls. It MUST be inserted as ONE
// whole object — see Config() for why.
type tlsBlock struct {
	RootCACertificate   string `json:"root_ca_certificate"`
	ConnectCertificate  string `json:"connect_certificate"`
	ConnectPrivateKey   string `json:"connect_private_key"`
	EnableMTLS          bool   `json:"enable_mtls"`
	VerifyNameOnConnect bool   `json:"verify_name_on_connect"`
}

// Config builds the Zenoh client config from the EFDI_* env vars.
//
// CRITICAL GOTCHA: the mTLS TLS settings are inserted as ONE json5 object at
// "transport/link/tls" with enable_mtls=true. Inserting the sub-keys individually
// (transport/link/tls/connect_certificate, etc.) silently does NOT enable the client-cert
// send path on Zenoh 1.x — the session opens but the router rejects you or you connect
// read-only. So we marshal the whole block and InsertJson5 it in a single call.
func Config() (zenoh.Config, error) {
	router, err := env("EFDI_ROUTER")
	if err != nil {
		return zenoh.Config{}, err
	}
	ca, err := env("EFDI_CA")
	if err != nil {
		return zenoh.Config{}, err
	}
	cert, err := env("EFDI_CERT")
	if err != nil {
		return zenoh.Config{}, err
	}
	key, err := env("EFDI_KEY")
	if err != nil {
		return zenoh.Config{}, err
	}
	verifyName := strings.ToLower(os.Getenv("EFDI_VERIFY_NAME")) == "true"

	conf := zenoh.NewConfigDefault()

	if err := conf.InsertJson5(zenoh.ConfigModeKey, `"client"`); err != nil {
		return zenoh.Config{}, fmt.Errorf("insert mode: %w", err)
	}

	endpoints, err := json.Marshal([]string{router})
	if err != nil {
		return zenoh.Config{}, fmt.Errorf("marshal endpoints: %w", err)
	}
	if err := conf.InsertJson5(zenoh.ConfigConnectKey, string(endpoints)); err != nil {
		return zenoh.Config{}, fmt.Errorf("insert connect endpoints: %w", err)
	}

	tls, err := json.Marshal(tlsBlock{
		RootCACertificate:   ca,
		ConnectCertificate:  cert,
		ConnectPrivateKey:   key,
		EnableMTLS:          true,
		VerifyNameOnConnect: verifyName,
	})
	if err != nil {
		return zenoh.Config{}, fmt.Errorf("marshal tls block: %w", err)
	}
	if err := conf.InsertJson5("transport/link/tls", string(tls)); err != nil {
		return zenoh.Config{}, fmt.Errorf("insert tls block: %w", err)
	}

	return conf, nil
}

// Session opens and returns a Zenoh session. Caller must Drop() it.
func Session() (zenoh.Session, error) {
	conf, err := Config()
	if err != nil {
		return zenoh.Session{}, err
	}
	return zenoh.Open(conf, nil)
}

// Namespace returns your owned prefix, e.g. "release/acme" (trailing slash stripped).
func Namespace() (string, error) {
	ns, err := env("PARTNER_NAMESPACE")
	if err != nil {
		return "", err
	}
	return strings.TrimRight(ns, "/"), nil
}

// Key builds a fully-qualified key under your namespace: Key("sensors/temp") ->
// "release/acme/sensors/temp". Pass an absolute key (e.g. "release/<partner>/**") only if
// you have rights to it.
func Key(suffix string) (string, error) {
	ns, err := Namespace()
	if err != nil {
		return "", err
	}
	return ns + "/" + strings.TrimLeft(suffix, "/"), nil
}
