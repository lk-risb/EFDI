# C99 EFDI pod examples — zenoh-c 1.9.0.
#
#   make                       # build ./publish and ./subscribe (dynamic link)
#   make ZENOHC=/opt/zenoh-c   # zenoh-c installed to a non-default prefix
#   make static                # static link libzenohc.a (air-gapped / single-binary drop)
#   make clean
#
# Plain C99 + libzenohc. No build system beyond make + a C compiler. See README.md for how to
# obtain/build zenoh-c 1.9.0, including fully offline.

CC      ?= cc
STD     ?= -std=c99
WARN    ?= -Wall -Wextra
OPT     ?= -O2

# Where zenoh-c is installed (headers in $(ZENOHC)/include, lib in $(ZENOHC)/lib).
# Defaults to /usr/local (the `cmake --install` default). Override: make ZENOHC=/path.
ZENOHC  ?= /usr/local

CFLAGS  += $(STD) $(WARN) $(OPT) -I$(ZENOHC)/include
LDFLAGS += -L$(ZENOHC)/lib

# zenoh-c is a Rust cdylib/staticlib; the static archive pulls in the platform libs it needs.
LDLIBS_DYN    = -lzenohc
LDLIBS_STATIC = -l:libzenohc.a -lpthread -ldl -lm
# macOS static note: replace the -l:... form with the explicit archive path and add the
# frameworks Rust needs, e.g.:
#   $(ZENOHC)/lib/libzenohc.a -framework Security -framework CoreFoundation -lpthread

BINS = publish subscribe

.PHONY: all static clean
all: $(BINS)

publish: publish.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS) $(LDLIBS_DYN)

subscribe: subscribe.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS) $(LDLIBS_DYN)

# Static link — produces self-contained binaries you can carry to an air-gapped host that has
# libzenohc.a available at build time but no shared lib at runtime.
static: CFLAGS += -DEFDI_STATIC
static:
	$(CC) $(CFLAGS) -o publish   publish.c   $(LDFLAGS) $(LDLIBS_STATIC)
	$(CC) $(CFLAGS) -o subscribe subscribe.c $(LDFLAGS) $(LDLIBS_STATIC)

clean:
	rm -f $(BINS)
