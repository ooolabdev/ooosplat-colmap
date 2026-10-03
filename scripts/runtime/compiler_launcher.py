# SPDX-License-Identifier: BSD-3-Clause
"""Normalize quoted forced-includes before delegating to locked ccache."""

import subprocess
import sys


def normalize_argument(argument):
    for prefix, replacement in (
        ('/FI"', "/FI"),
        ('--pre-include="', "--pre-include="),
    ):
        if argument.startswith(prefix) and argument.endswith('"'):
            return replacement + argument[len(prefix) : -1]
    return argument


def main():
    if len(sys.argv) < 3:
        raise SystemExit(
            "usage: compiler_launcher.py CCACHE COMPILER [ARGS...]"
        )
    raise SystemExit(
        subprocess.call([normalize_argument(arg) for arg in sys.argv[1:]])
    )


if __name__ == "__main__":
    main()
