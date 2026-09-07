"""向后兼容入口: analyze.py <codes...> 等价于 cli.py analyze <codes...>。"""

import sys

from cli import main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "analyze", *sys.argv[1:]]
    main()
