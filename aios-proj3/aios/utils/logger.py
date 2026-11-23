import logging


class _SafeStreamHandler(logging.StreamHandler):
    """StreamHandler decoupled from task execution: broken/closed log streams never raise."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dropped_logs = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.stream.write(msg + self.terminator)
            self.flush()
        except (OSError, BrokenPipeError, ValueError):
            self.dropped_logs += 1

    def handleError(self, record: logging.LogRecord) -> None:
        self.dropped_logs += 1


class SchedulerLogger:
    """Unified fault-tolerant logger for AIOS schedulers and kernel subsystems."""

    def __init__(self, name: str, log_mode: str = "console"):
        self.name = name
        self.log_mode = log_mode
        self.dropped_logs = 0
        self._logger = logging.getLogger(f"aios.{name}")
        if not self._logger.handlers:
            handler = _SafeStreamHandler()
            formatter = logging.Formatter("%(asctime)s - %(name)s - [%(levelname)s] %(message)s")
            handler.setFormatter(formatter)
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)
            self._logger.propagate = False

    def log(self, message: str, level: str = "info") -> None:
        if self.log_mode == "silent":
            return
        try:
            msg = str(message).strip()
            if level in ("error", "failed"):
                self._logger.error(msg)
            elif level in ("warning", "warn"):
                self._logger.warning(msg)
            else:
                self._logger.info(msg)
        except (OSError, BrokenPipeError, ValueError):
            self.dropped_logs += 1
