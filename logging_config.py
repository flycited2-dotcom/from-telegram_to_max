import logging
from logging.handlers import RotatingFileHandler
from os import PathLike


def configure_logging(log_file: str | PathLike[str]) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ),
            logging.StreamHandler(),
        ],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
