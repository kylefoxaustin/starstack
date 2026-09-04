#!/usr/bin/env python3
"""
Entry point for the frozen starstack.exe.

    starstack.exe                -> the button (window)
    starstack.exe <folder>       -> the button, stacking that folder straight away
    starstack.exe --cli ...      -> plain starstack command line, no window

The window runs stacks by re-launching this same exe with --cli, so the CLI
stays the one source of truth and the exe needs no Python on the machine.
"""
import multiprocessing
import sys


def main():
    multiprocessing.freeze_support()          # required for worker processes in a frozen exe
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        import starstack
        sys.exit(starstack.main(sys.argv[2:]))
    import button
    button.main()


if __name__ == "__main__":
    main()
