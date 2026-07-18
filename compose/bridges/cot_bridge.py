#!/usr/bin/env python3
"""Zenoh-side CoT bridge entrypoint.

This wrapper keeps the file name for the stream-facing CoT path while delegating
the actual publish logic to the CoT output layer.
"""

from layers.cot_layer import main


if __name__ == "__main__":
    main()
