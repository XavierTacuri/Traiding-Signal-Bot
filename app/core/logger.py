import logging

LOGGER_NAME = "forex_signal_bot"
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"
HANDLER_MARKER = "_forex_signal_console_handler"


def setup_logger() -> logging.Logger:
    """Configure the application logger once without duplicating handlers."""
    application_logger = logging.getLogger(LOGGER_NAME)
    application_logger.setLevel(logging.INFO)
    application_logger.propagate = False
    owned_handlers = [
        handler for handler in application_logger.handlers
        if getattr(handler, HANDLER_MARKER, False)
    ]
    if not owned_handlers:
        handler = logging.StreamHandler()
        setattr(handler, HANDLER_MARKER, True)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
        application_logger.addHandler(handler)
    return application_logger


logger = setup_logger()
