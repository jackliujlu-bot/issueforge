"""Feishu (Lark) ingestion layer."""

from app.feishu.message_parser import FeishuTaskRequest, parse_feishu_message
from app.feishu.webhook_server import build_app

__all__ = ["FeishuTaskRequest", "build_app", "parse_feishu_message"]
