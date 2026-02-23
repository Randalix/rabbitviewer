"""Launch the RabbitViewer GUI (auto-starts daemon if needed)."""

import sys

__completions__ = ["--recursive", "--no-recursive", "--restart-daemon", "--cold-cache"]

def main():
    from main import main as _viewer_main
    sys.exit(_viewer_main())

if __name__ == "__main__":
    main()
