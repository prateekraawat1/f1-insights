import logging
import json
import asyncio
import time
from datetime import datetime, timezone
from backend.cache import cache
from backend.session import session_monitor

class JsonFormatter(logging.Formatter):
    """
    Format logs as JSON for structured observability.
    """
    def format(self, record):
        log_record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_record)

def setup_structured_logging():
    """Override root logger to output JSON."""
    root_logger = logging.getLogger()
    
    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)

    # We can also silence some noisy loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


async def data_quality_loop():
    """
    Background loop to verify data freshness.
    If the session is LIVE but the current_lap hasn't changed in >5 mins,
    it emits a data staleness warning.
    """
    last_lap = None
    last_lap_time = time.time()
    
    while True:
        await asyncio.sleep(60)
        
        if not session_monitor.is_any_live_session():
            continue
            
        current_lap = cache.get_race_meta("current_lap")
        
        if current_lap is None:
            logging.warning("⚠️ Data Quality: No current_lap in cache during LIVE session.")
            continue
            
        if current_lap != last_lap:
            last_lap = current_lap
            last_lap_time = time.time()
        else:
            time_since_change = time.time() - last_lap_time
            if time_since_change > 300: # 5 minutes
                logging.warning(f"⚠️ Data Quality: Telemetry may be stale. Lap {current_lap} unchanged for {int(time_since_change)}s.")
