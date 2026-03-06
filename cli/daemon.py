"""Start the RabbitViewer daemon in the foreground."""

import sys


def main():
    # Translate legacy --restart to --restart-daemon.
    if "--restart" in sys.argv:
        sys.argv.remove("--restart")
        sys.argv.append("--restart-daemon")

    if "--daemon" not in sys.argv:
        sys.argv.append("--daemon")
    from main import main as _main
    _main()


if __name__ == "__main__":
    main()
