import sys

from .daemon import main

raise SystemExit(main(sys.stdin.buffer, sys.stdout.buffer, sys.argv[1:]))
