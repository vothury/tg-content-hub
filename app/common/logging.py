import logging
import sys

from app.config import settings


def setup_logging(service: str) -> logging.Logger:
    logging.basicConfig(
        level=settings.log_level.upper(),
        stream=sys.stdout,
        format=f"%(asctime)s %(levelname)-7s [{service}] %(name)s: %(message)s",
        force=True,
    )
    return logging.getLogger(service)