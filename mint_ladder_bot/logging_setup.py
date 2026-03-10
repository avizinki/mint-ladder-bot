import logging
from typing import Optional


def setup_logging(verbosity: int = 0, log_file: Optional[str] = None) -> None:
    """
    Configure root logger for the mint_ladder_bot package.

    verbosity:
      0 -> INFO
      1 -> DEBUG
      -1 -> WARNING
    """

    level = logging.INFO
    if verbosity >= 1:
        level = logging.DEBUG
    elif verbosity <= -1:
        level = logging.WARNING

    logger = logging.getLogger()
    logger.setLevel(level)

    # Clear existing handlers to avoid duplicate logs in some environments.
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    class FlushingStreamHandler(logging.StreamHandler):
        def emit(self, record):
            super().emit(record)
            self.flush()

    stream_handler = FlushingStreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if log_file:
        class FlushingFileHandler(logging.FileHandler):
            def emit(self, record):
                super().emit(record)
                self.flush()
        file_handler = FlushingFileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # Quiet HTTP request URLs (httpx / httpcore) unless DEBUG.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)

