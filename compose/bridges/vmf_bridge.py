#!/usr/bin/env python3
import argparse
from raw_socket_bridge import add_args, run_raw

parser = argparse.ArgumentParser(description="VMF socket ingress -> Zenoh raw bytes")
add_args(parser, "vmf", 2000)
args = parser.parse_args()
run_raw("vmf", 2000, args, frame_mode="vmf16be" if args.tcp else "chunk")
