"""结构化日志配置与手机号脱敏工具。

独立成模块：导入它不会触发 tdl.py 的环境变量校验 / exit，方便单元测试。
"""
import logging

LOG_FORMAT = "%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level=None):
    """配置根 logger，输出格式：
    2026-08-21 12:14:53.918 - __main__ - INFO - 消息
    """
    level_name = (level or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format=LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        force=True,
    )
    # telebot 内部 INFO 太吵，压到 WARNING
    logging.getLogger("telebot").setLevel(logging.WARNING)


def redact_phone(phone):
    """手机号脱敏：13812345678 -> 138****5678。长度不足或非数字原样返回。"""
    digits = str(phone).lstrip("+").replace(" ", "")
    if len(digits) >= 7 and digits.isdigit():
        return digits[:3] + "****" + digits[-4:]
    return str(phone)
