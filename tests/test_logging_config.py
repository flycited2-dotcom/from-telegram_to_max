import importlib.util
import logging
import tempfile
import unittest
from pathlib import Path


class LoggingConfigurationTests(unittest.TestCase):
    def test_rotates_bridge_log_and_hides_http_client_urls(self):
        spec = importlib.util.find_spec("logging_config")
        self.assertIsNotNone(spec, "logging_config module is missing")
        if spec is None:
            return

        from logging_config import configure_logging

        root_logger = logging.getLogger()
        original_handlers = list(root_logger.handlers)
        original_level = root_logger.level
        httpx_logger = logging.getLogger("httpx")
        original_httpx_level = httpx_logger.level

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                log_file = Path(temp_dir) / "bridge.log"
                configure_logging(log_file)

                rotating_handlers = [
                    handler
                    for handler in root_logger.handlers
                    if hasattr(handler, "maxBytes")
                ]
                self.assertEqual(len(rotating_handlers), 1)
                self.assertEqual(rotating_handlers[0].maxBytes, 10 * 1024 * 1024)
                self.assertEqual(rotating_handlers[0].backupCount, 5)
                self.assertGreaterEqual(httpx_logger.level, logging.WARNING)
            finally:
                for handler in root_logger.handlers:
                    if handler not in original_handlers:
                        handler.close()
                root_logger.handlers = original_handlers
                root_logger.setLevel(original_level)
                httpx_logger.setLevel(original_httpx_level)


if __name__ == "__main__":
    unittest.main()
