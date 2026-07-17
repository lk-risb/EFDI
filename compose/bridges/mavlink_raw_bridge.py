#!/usr/bin/env python3
import argparse
from raw_socket_bridge import add_args, run_raw

parser = argparse.ArgumentParser(description="MAVLink socket ingress -> Zenoh raw bytes")
add_args(parser, "mavlink", 14550)
run_raw("mavlink", 14550, parser.parse_args())
