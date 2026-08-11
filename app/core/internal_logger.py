
from __future__ import annotations

import os
import sys
import logging
import threading
from logging.handlers import RotatingFileHandler

_INIT_LOCK = threading.Lock()
_INITIALIZED = False


def setup_internal_logging(
    log_dir: str = "/app/logs",
    log_filename: str = "app.log",
    level: int = logging.INFO,
    max_bytes: int = 50 * 1024 * 1024,
    backup_count: int = 10,
) -> None:
    
    global _INITIALIZED

    with _INIT_LOCK:
        if _INITIALIZED:
            return
        _INITIALIZED = True

        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, log_filename)

        
        real_stdout = sys.__stdout__
        real_stderr = sys.__stderr__

        
        root = logging.getLogger()
        root.setLevel(level)
        root.handlers.clear()

        fmt = logging.Formatter(
            fmt='[%(asctime)s] [%(levelname)s] [%(process)d] [%(name)s] %(message)s'
        )

        file_handler = RotatingFileHandler(
            log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        file_handler.setLevel(level)

        console_handler = logging.StreamHandler(real_stdout)
        console_handler.setFormatter(fmt)
        console_handler.setLevel(level)

        root.addHandler(file_handler)
        root.addHandler(console_handler)

        
        logging.raiseExceptions = False

        
        guard = threading.local()

        class _StreamToFileAndFallback:
            def __init__(self, stream_name: str, lvl: int, fallback):
                self.stream_name = stream_name
                self.lvl = lvl
                self.fallback = fallback

            def write(self, buf: str) -> None:
                if not buf:
                    return

                
                if getattr(guard, "busy", False):
                    try:
                        self.fallback.write(buf)
                        self.fallback.flush()
                    except Exception:
                        pass
                    return

                
                lines = buf.splitlines()
                if not lines:
                    return

                guard.busy = True
                try:
                    logger_name = self.stream_name

                    for line in lines:
                        line = line.rstrip()
                        if not line:
                            continue

                        
                        try:
                            record = logging.LogRecord(
                                name=logger_name,
                                level=self.lvl,
                                pathname="",
                                lineno=0,
                                msg=line,
                                args=(),
                                exc_info=None,
                            )
                            file_handler.emit(record)
                        except Exception:
                            
                            try:
                                self.fallback.write(line + "\n")
                                self.fallback.flush()
                            except Exception:
                                pass
                finally:
                    guard.busy = False

            def flush(self) -> None:
                try:
                    self.fallback.flush()
                except Exception:
                    pass

            def isatty(self) -> bool:
                return False

        
        sys.stdout = _StreamToFileAndFallback("STDOUT", logging.INFO, real_stdout)
        sys.stderr = _StreamToFileAndFallback("STDERR", logging.ERROR, real_stderr)

        
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

        root.getLogger(__name__).info("Internal logging initialized. File=%s", log_path)