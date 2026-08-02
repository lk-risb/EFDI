#!/usr/bin/env python3
import argparse
from raw_socket_bridge import add_args, run_raw

parser = argparse.ArgumentParser(description="SAPIENT/FLEX 335 socket ingress -> Zenoh raw bytes")
add_args(parser, "sapient", 7001)
args = parser.parse_args()
run_raw("sapient/flex335", 7001, args, frame_mode="sapient32le" if args.tcp else "chunk")
