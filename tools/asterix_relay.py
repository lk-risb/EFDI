#!/usr/bin/env python3
"""asterix_relay.py — Forward ASTERIX UDP from radar to moon-pod.

Run this on the PC connected to the Giraffe radar (must be on NetBird mesh).

It listens for ASTERIX UDP packets from the radar on LOCAL_PORT,
and forwards each packet unchanged to the moon-pod at DEST_IP:DEST_PORT.

Usage:
    python3 asterix_relay.py

    # Or override defaults:
    python3 asterix_relay.py --local-port 30002 --dest <MOON_POD_NETBIRD_IP>:30048

Requirements:
    Python 3.6+  (no extra packages needed)
"""

import argparse
import socket

LOCAL_PORT = 30002          # port the Giraffe sends ASTERIX to on this PC
DEST_IP    = ""             # moon-pod NetBird IP — set via --dest IP:PORT
DEST_PORT  = 30048          # asterix_cat48 listening port on moon-pod


def main():
    ap = argparse.ArgumentParser(description="ASTERIX UDP relay → moon-pod")
    ap.add_argument("--local-port", type=int, default=LOCAL_PORT,
                    help="Local UDP port the radar sends to (default: {})".format(LOCAL_PORT))
    ap.add_argument("--dest", required=not DEST_IP,
                    default="{}:{}".format(DEST_IP, DEST_PORT) if DEST_IP else None,
                    help="moon-pod address IP:PORT (NetBird mesh IP of the pod)")
    args = ap.parse_args()

    dest_ip, dest_port = args.dest.rsplit(":", 1)
    dest = (dest_ip, int(dest_port))

    recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    recv_sock.bind(("0.0.0.0", args.local_port))

    send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    print("ASTERIX relay started")
    print("  Listening on  : 0.0.0.0:{}".format(args.local_port))
    print("  Forwarding to : {}:{}".format(dest_ip, dest_port))
    print("  Ctrl-C to stop")

    packets = 0
    try:
        while True:
            data, addr = recv_sock.recvfrom(65535)
            send_sock.sendto(data, dest)
            packets += 1
            if packets % 100 == 0:
                print("Relayed {} packets (last from {})".format(packets, addr[0]))
    except KeyboardInterrupt:
        print("\nStopped. {} packets relayed.".format(packets))
    finally:
        recv_sock.close()
        send_sock.close()


if __name__ == "__main__":
    main()
