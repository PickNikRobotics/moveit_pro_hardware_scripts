#!/usr/bin/env python3

import sys

sys.path.insert(0, "/usr/lib/moveit-pro-scripts")

from cd_objective_lib import run_objective

if __name__ == "__main__":
    run_objective("Move Boxes Looping")
