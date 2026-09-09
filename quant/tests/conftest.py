"""共用 pytest fixtures。"""

import pytest

from bot_context import Ctx


@pytest.fixture
def ctx():
    """默认 CLI 会话上下文(handler 显式传参用)。"""
    return Ctx()
