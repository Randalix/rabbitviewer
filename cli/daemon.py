"""Start the RabbitViewer daemon in the foreground."""

import logging
import sys


def main():
    if "--restart" in sys.argv:
        sys.argv.remove("--restart")
        logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
        from cli.stop import stop_daemon
        if not stop_daemon():
            logging.error("Could not stop existing daemon; aborting restart.")
            sys.exit(1)
        logging.info("Restarting daemon...")

    # Inject --daemon so main.main() enters daemon mode.
    if "--daemon" not in sys.argv:
        sys.argv.append("--daemon")
    from main import main as _main
    _main()


if __name__ == "__main__":
    main()
