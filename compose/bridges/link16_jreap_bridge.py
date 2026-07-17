#!/usr/bin/env python3
import argparse
from raw_socket_bridge import add_args, run_raw

parser = argparse.ArgumentParser(description="Link-16/JREAP-C socket ingress -> Zenoh raw bytes")
add_args(parser, "link16", 3010)
args = parser.parse_args()
if args.tcp:
    parser.error("TCP is disabled until the gateway's JREAP-C stream framing ICD is available")
run_raw("link16", 3010, args)
