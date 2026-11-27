"""Where Cliquet's log goes: Python's `logging`, never a print. Each command that runs
Cliquet sends its records to stderr with `log_to_stderr`."""

import logging
import sys


class _Stderr(logging.StreamHandler):
    """Writes each record to `sys.stderr` as it is when the record comes, which a test or a
    worker may have replaced."""

    def emit(self, record: logging.LogRecord) -> None:
        """Write the record to the current stderr."""
        self.stream = sys.stderr
        super().emit(record)


def log_to_stderr(names: tuple[str, ...], form: str = "%(message)s") -> None:
    """Send the records of the loggers `names`, from INFO up, to stderr as `form` says. Done
    once per logger, however often it is called."""
    for name in names:
        logger = logging.getLogger(name)
        if not any(isinstance(handler, _Stderr) for handler in logger.handlers):
            handler = _Stderr()
            handler.setFormatter(logging.Formatter(form))
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
