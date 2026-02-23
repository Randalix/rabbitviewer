"""Launch the RabbitViewer GUI (auto-starts daemon if needed)."""

import sys

def main():
    from main import main as _viewer_main
    sys.exit(_viewer_main())

if __name__ == "__main__":
    main()
