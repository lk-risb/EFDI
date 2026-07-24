#!/usr/bin/env python3
import argparse
from raw_socket_bridge import add_args, run_raw

parser = argparse.ArgumentParser(
    description="STANAG 5516 (Link-16 J-series over JREAP-C) socket ingress -> Zenoh raw bytes")
add_args(parser, "stanag_5516", 3010)
args = parser.parse_args()
if args.tcp:
    parser.error("TCP is disabled until the gateway's JREAP-C stream framing ICD is available")
run_raw("stanag_5516", 3010, args)
