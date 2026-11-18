"""包入口：支持 `python -m talaria`。"""

import sys

from talaria.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
