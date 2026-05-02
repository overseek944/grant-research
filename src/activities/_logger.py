"""
Logger helper that works both inside Temporal workers and in direct demo mode.
"""
import logging

_log = logging.getLogger("grant-agent")
logging.basicConfig(level=logging.INFO, format="  %(message)s")


class _SafeLogger:
    def info(self, msg: str):    _log.info(msg)
    def warning(self, msg: str): _log.warning("⚠  " + msg)
    def error(self, msg: str):   _log.error("✗  " + msg)


safe_logger = _SafeLogger()
