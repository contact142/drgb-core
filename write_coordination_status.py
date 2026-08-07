#!/usr/bin/env python3
"""Write the read-only DRGB coordination status from existing observer state."""

from pathlib import Path

from drgb.coordination import write_status


ROOT = Path.home() / ".drgb"


def main() -> dict:
    status = write_status(
        ROOT / "coordination",
        ROOT / "observe" / "last_cycle.json",
        ROOT / "observe_vps" / "last_cycle.json",
    )
    print(f"DRGB coordination status={status['status']} lanes={len(status['lanes'])} "
          f"blockers={len(status['blockers'])}")
    return status


if __name__ == "__main__":
    main()
