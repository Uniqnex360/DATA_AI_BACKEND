import sys
import logging
from logging.handlers import RotatingFileHandler
import os

def setup_internal_logging():
    log_dir = "/app/logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    log_file = os.path.join(log_dir, "dev_internal.log")

    # 1. Configure the Root Logger to catch all 'logging' calls from all files
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
        handlers=[
            RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5),
            logging.StreamHandler(sys.__stdout__) # Still show in docker logs
        ]
    )

    # 2. Redirect print() statements to the log file
    class StreamToLogger:
        def __init__(self, logger, level):
            self.logger = logger
            self.level = level
            self.linebuf = ''

        def write(self, buf):
            for line in buf.rstrip().splitlines():
                self.logger.log(self.level, line.rstrip())

        def flush(self):
            pass

    # This makes every 'print' statement act like 'logger.info'
    sys.stdout = StreamToLogger(logging.getLogger('STDOUT'), logging.INFO)
    # This makes every system error act like 'logger.error'
    sys.stderr = StreamToLogger(logging.getLogger('STDERR'), logging.ERROR)

    print("🚀 Internal Logging System Initialized - All prints/logs are being saved.")  