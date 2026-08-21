import logging
import re

from logutil import LOG_FORMAT, LOG_DATEFMT, redact_phone


def test_redact_phone_standard():
    assert redact_phone("13812345678") == "138****5678"


def test_redact_phone_with_country_code():
    assert redact_phone("+8613812345678") == "861****5678"


def test_redact_phone_too_short_untouched():
    assert redact_phone("123456") == "123456"


def test_redact_phone_non_digit_untouched():
    assert redact_phone("abc") == "abc"


def test_log_format_milliseconds_and_parts():
    fmt = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
    record = logging.LogRecord("__main__", logging.INFO, "tdl.py", 1, "机器人启动", None, None)
    out = fmt.format(record)
    parts = out.split(" - ")
    assert parts[1:] == ["__main__", "INFO", "机器人启动"]
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$", parts[0])
