import logging
import signal
from typing import Any


logger = logging.getLogger(__name__)


def start_trading(args: dict[str, Any]) -> int:
    """
    Main entry point for trading mode
    """
    # Import here to avoid loading worker module when it's not used
    from freqtrade.emergency_authority import EmergencyTakeover
    from freqtrade.worker import Worker

    def term_handler(signum, frame):
        # Raise KeyboardInterrupt - so we can handle it in the same way as Ctrl-C
        raise KeyboardInterrupt()

    # Create and run worker
    worker = None
    try:
        signal.signal(signal.SIGTERM, term_handler)
        worker = Worker(args)
        worker.run()
    except EmergencyTakeover as err:
        logger.critical("Emergency takeover active (%s); halting core trader cleanly.", err)
        return 0
    finally:
        if worker:
            logger.info("worker found ... calling exit")
            worker.exit()
    return 0
