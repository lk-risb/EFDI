#!/usr/bin/env python3
import argparse
from raw_socket_bridge import add_args, run_raw

parser = argparse.ArgumentParser(description="STANAG 4586 socket ingress -> Zenoh raw bytes")
add_args(parser, "stanag_4586", 4586)
args = parser.parse_args()
run_raw("stanag_4586", 4586, args, frame_mode="stanag16le" if args.tcp else "chunk")
