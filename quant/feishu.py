"""飞书应用 Bot 推送(日报摘要/告警)。

接入流程:
  1. 在飞书开放平台 https://open.feishu.cn 创建企业自建应用
  2. 应用能力勾选「机器人」
  3. 权限管理勾选 im:message:send_as_bot (以应用身份发送消息)
  4. 应用发布上线,在目标群聊中通过群设置→群机器人→添加机器人→选择此应用
  5. 在 config.json -> feishu 段填入 app_id / app_secret / chat_id (群聊 chat_id
     可从群设置→群信息中复制,或在机器人添加完成后从回调 URL 获取)
  6. enabled 设为 true

config.json -> feishu:
  {
    "feishu": {
      "enabled": true,
      "app_id": "cli_xxxx",
      "app_secret": "xxxx",
      "chat_id": "oc_xxxx"
    }
  }

CLI:
  python feishu.py "测试消息"            # 发送文本
  python feishu.py --card "日报卡片json"  # 发送卡片
  python feishu.py --test                # 发送测试卡片
"""

import argparse
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

log = logging.getLogger("quant")

CONFIG_PATH = Path(__file__).parent / "config.json"

FEISHU_HOST = "https://open.feishu.cn"
TOKEN_URL = FEISHU_HOST + "/open-apis/auth/v3/tenant_access_token/internal"
SEND_MSG_URL = FEISHU_HOST + "/open-apis/im/v1/messages?receive_id_type=chat_id"

DEFAULT_TIMEOUT = 10


def _load_feishu_config():
    """读 config.json -> feishu,缺失返回空字典。"""
    if not CONFIG_PATH.exists():
        return {}
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get("feishu") or {}
    except Exception:
        return {}


class FeishuBot:
    """飞书应用 Bot,自动缓存 tenant_access_token。"""

    def __init__(self, app_id=None, app_secret=None, chat_id=None, timeout=DEFAULT_TIMEOUT):
        cfg = _load_feishu_config()
        self.app_id = app_id or cfg.get("app_id", "")
        self.app_secret = app_secret or cfg.get("app_secret", "")
        self.chat_id = chat_id or cfg.get("chat_id", "")
        self.enabled = bool(cfg.get("enabled", False)) if not app_id else True
        self.timeout = timeout
        # token 缓存: (token, expire_at)
        self._token = None
        self._token_expire_at = 0.0

    # -------- token --------

    def _get_token(self):
        """获取 tenant_access_token,缓存 100 分钟(飞书默认 2h)。"""
        if self._token and time.time() < self._token_expire_at - 300:
            return self._token
        if not self.app_id or not self.app_secret:
            raise ValueError("feishu app_id/app_secret 未配置")
        body = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode("utf-8")
        resp = _post_json(TOKEN_URL, body, timeout=self.timeout)
        if resp.get("code") != 0:
            raise RuntimeError(f"获取 token 失败: {resp.get('msg')}")
        self._token = resp["tenant_access_token"]
        self._token_expire_at = time.time() + int(resp.get("expire", 7200))
        log.info("feishu token 已刷新,有效 %ds", resp.get("expire", 7200))
        return self._token

    # -------- 发送 --------

    def send_text(self, text, chat_id=None):
        """发送文本消息,返回响应 dict 或 None(失败/未启用)。"""
        return self._send("text", json.dumps({"text": text}), chat_id)

    def send_card(self, card, chat_id=None):
        """发送 interactive 卡片,card 是 dict 或 JSON 字符串。"""
        if isinstance(card, dict):
            card = json.dumps(card, ensure_ascii=False)
        return self._send("interactive", card, chat_id)

    def _send(self, msg_type, content_str, chat_id):
        if not self.enabled:
            log.info("feishu 未启用,跳过推送")
            return None
        target = chat_id or self.chat_id
        if not target:
            log.warning("feishu chat_id 未配置,跳过推送")
            return None
        try:
            token = self._get_token()
            body = json.dumps(
                {"receive_id": target, "msg_type": msg_type, "content": content_str},
                ensure_ascii=False,
            ).encode("utf-8")
            resp = _post_json(SEND_MSG_URL, body, bearer=token, timeout=self.timeout)
            if resp.get("code") != 0:
                log.error("feishu 推送失败: %s", resp.get("msg"))
                return resp
            log.info("feishu 推送成功: msg_type=%s", msg_type)
            return resp
        except Exception as e:
            log.error("feishu 推送异常: %s", e)
            return None


# -------- HTTP --------


def _post_json(url, body, bearer=None, timeout=DEFAULT_TIMEOUT):
    """POST JSON 并返回解析后的 dict。"""
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        log.warning("feishu HTTP %d: %s", e.code, raw[:200])
    return json.loads(raw)


# -------- 卡片构造 --------


def build_daily_card(stats, cands, ai_summary, now=None):
    """构造每日日报卡片。

    Args:
        stats: dict, market_stats 返回
        cands: list, analyze_candidates 返回
        ai_summary: str, AI 综合分析全文
        now: datetime
    Returns:
        dict 卡片结构(旧版 schema)
    """
    if now is None:
        now = datetime.now()
    date_str = now.strftime("%Y-%m-%d %H:%M")

    # 市场情绪判断
    up = stats.get("up", 0)
    dn = stats.get("down", 0)
    if up > dn * 1.5:
        mood, tmpl = "偏多(普涨)", "green"
    elif dn > up * 1.5:
        mood, tmpl = "偏空(普跌)", "red"
    else:
        mood, tmpl = "震荡", "blue"

    # 候选行
    cand_lines = []
    for c in cands[:5]:
        hits = "、".join(c.get("hits", [])[:2]) if c.get("hits") else "-"
        cand_lines.append(
            f"**{c['rank']}. {c['code']} {c['name']}** | 玉姐 {c['score']}分 | "
            f"信号 {c['verdict']} | 命中: {hits}"
        )
    cand_block = "\n".join(cand_lines) if cand_lines else "（无候选）"

    # AI 摘要(取前 800 字,超长截断)
    ai_text = (ai_summary or "").strip()
    if len(ai_text) > 800:
        ai_text = ai_text[:800] + "..."

    return {
        "config": {"wide_screen": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📈 A股开盘日报 {date_str}"},
            "template": tmpl,
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**市场情绪: {mood}**\n"
                        f"总数 {stats.get('total',0)} | 涨 {up} / 跌 {dn} / 平 {stats.get('flat',0)}\n"
                        f"涨停 {stats.get('limit_up',0)} | 跌停 {stats.get('limit_down',0)} | "
                        f"成交额 {stats.get('total_amount_yi',0):.0f} 亿"
                    ),
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**🎯 玉姐精选 Top {len(cands)}**"},
            },
            {"tag": "div", "text": {"tag": "lark_md", "content": cand_block}},
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**🤖 AI 综合分析**"},
            },
            {"tag": "div", "text": {"tag": "lark_md", "content": ai_text or "(AI 调用失败)"},
             },
        ],
    }


def build_test_card():
    return {
        "config": {"wide_screen": True},
        "header": {
            "title": {"tag": "plain_text", "content": "飞书推送测试"},
            "template": "turquoise",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "✅ 飞书 Bot 配置正常,日报将自动推送至此群聊。"}},
        ],
    }


# -------- 主流程接入 --------


def send_daily_to_feishu(stats, cands, ai_summary, now=None):
    """供 daily_scan 调用,推送日报卡片。失败仅打日志,不抛异常。"""
    try:
        bot = FeishuBot()
        if not bot.enabled:
            print("feishu: 未启用,跳过推送")
            return False
        card = build_daily_card(stats, cands, ai_summary, now=now)
        resp = bot.send_card(card)
        return bool(resp and resp.get("code") == 0)
    except Exception as e:
        print(f"feishu: 推送失败 {e}")
        return False


# -------- CLI --------


def main():
    ap = argparse.ArgumentParser(description="飞书推送 CLI")
    ap.add_argument("text", nargs="?", help="要发送的文本")
    ap.add_argument("--card", help="卡片 JSON 字符串(或文件路径)")
    ap.add_argument("--test", action="store_true", help="发送测试卡片")
    args = ap.parse_args()

    if args.test:
        FeishuBot().send_card(build_test_card())
        return
    if args.card:
        p = Path(args.card)
        card = p.read_text(encoding="utf-8") if p.exists() else args.card
        FeishuBot().send_card(card)
        return
    if args.text:
        FeishuBot().send_text(args.text)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
