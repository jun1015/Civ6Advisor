"""文明6 顾问 —— 极简大模型客户端（OpenAI 兼容协议）。

只用 Python 标准库，不需要 pip install 任何东西。
关键能力是 tools 参数（function calling）：让模型能"要求"我们去查资料，
这是 agent 和普通聊天机器人的分界线。

**不绑定任何一家服务商**：只要对方实现了 OpenAI 兼容协议
（POST {base_url}/chat/completions，请求头带 Bearer token），就能直接用。
你只需要填两样东西：接口地址、模型名。

本文件里那几个服务商预设**纯粹是省得你去查地址的快捷填充**，
默认那项是"通用（自己填）" —— 不预设任何一家，也不会偷偷替你选。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request


class LLMError(Exception):
    """调用大模型失败。里面的文字是直接给人看的，不含技术黑话。"""


# ---------------------------------------------------------------- 服务商预设
#
# 默认 "custom" = 通用：地址和模型都留空，由用户自己填。
#
# 下面是几家常见服务的地址，**只是快捷填充**，不代表偏好 ——
# 换个没列在这里的服务（自建、公司内网、其他云厂商）照样能用，
# 选 "custom" 把地址和模型名填进去就行。
DEFAULT_PROVIDER = "custom"

PROVIDERS = {
    "custom": {
        "label": "通用（自己填）",
        "base_url": "",
        "model": "",
        "key_url": "",
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_url": "https://platform.deepseek.com/api_keys",
    },
    "qwen": {
        "label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "key_url": "https://bailian.console.aliyun.com/",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_url": "https://platform.openai.com/api-keys",
    },
}


def preset(name):
    """取服务商预设。不认识的名字一律按"通用"处理 —— 绝不偷偷替你选一家。"""
    return PROVIDERS.get((name or "").strip().lower(), PROVIDERS[DEFAULT_PROVIDER])


class LLMClient:
    """极简客户端。只实现一件事：把消息发过去，把回复拿回来。"""

    def __init__(self, api_key, base_url, model, timeout=120,
                 proxy=None, temperature=0.3):
        if not api_key:
            raise LLMError(
                "还没有填 API 密钥。\n"
                "  图形界面：点浮层标题栏的「设置」，填「API 密钥」那一栏。\n"
                "  命令行：在同一目录建 secret.json，内容是 "
                '{"api_key": "你的密钥"}'
            )
        self.api_key = api_key.strip()
        self.base_url = self._normalize_url(base_url)
        self.model = (model or "").strip()
        if not self.model:
            raise LLMError(
                "还没有填模型名。\n"
                "点浮层标题栏的「设置」，填「模型名」—— 用哪家就填哪家的模型，"
                "例如 deepseek-chat / qwen-plus / glm-4-flash / gpt-4o-mini。"
            )
        self.timeout = timeout
        self.proxy = proxy
        self.temperature = temperature
        self._opener = self._build_opener()

    # ------------------------------------------------------------ 内部工具

    @staticmethod
    def _normalize_url(url):
        """把用户手填的地址整理成能直接请求的完整地址。

        容错是**故意**的 —— 这串东西是手打的，很容易少写一段：
            api.example.com             → https://api.example.com/chat/completions
            https://api.example.com     → https://api.example.com/chat/completions
            https://api.deepseek.com/v1 → https://api.deepseek.com/v1/chat/completions
            已经以 /chat/completions 结尾 → 原样不动
        """
        url = (url or "").strip().rstrip("/")
        if not url:
            raise LLMError(
                "还没有填接口地址。\n"
                "点浮层标题栏的「设置」，填「API 地址」—— "
                "就是你所用的那家服务商的接口地址，通义/OpenAI 这类通常以 /v1 结尾。"
            )
        if "://" not in url:                 # 只写了域名，帮忙补上协议
            url = "https://" + url
        if not url.endswith("/chat/completions"):
            url = url + "/chat/completions"
        return url

    def _build_opener(self):
        """处理代理：None=跟随系统环境变量，''=直连，其他=指定代理。"""
        if self.proxy is None:
            return urllib.request.build_opener()
        if self.proxy == "":
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        handler = urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
        return urllib.request.build_opener(handler)

    def _post(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise LLMError(self._explain_http_error(e)) from e
        except urllib.error.URLError as e:
            raise LLMError(self._explain_url_error(e)) from e
        except socket.timeout as e:
            raise LLMError(
                f"请求超时（{self.timeout} 秒）。可能是网络慢，"
                "或者模型在思考一个特别长的问题，可以再试一次。"
            ) from e

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMError(
                "服务器返回的不是合法 JSON，通常说明网络中间有别的东西"
                f"（比如公司代理、校园网认证页）插了一脚。原始返回开头：\n{raw[:300]}"
            ) from e

    @staticmethod
    def _explain_http_error(e):
        """把 HTTP 错误码翻译成人话。"""
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass

        table = {
            400: "请求格式不对。如果刚刚改过 config.json，检查有没有多余的逗号或引号。",
            401: "API key 不对或已失效。去后台重新复制一个，注意别把前后的空格也复制进来。",
            402: "账户余额不足，需要充值。",
            403: "这个 key 没有权限调用当前模型，可能选错了模型名。",
            404: "接口地址不对，检查 config.json 里的 base_url。",
            429: "请求太频繁被限流了，等几秒再试。",
            500: "对方服务器出错了，不是你的问题，过一会儿再试。",
            502: "对方服务器网关错误（也可能是你本机的网络代理拦截了请求）。",
            503: "对方服务器暂时不可用，稍后再试。",
        }
        hint = table.get(e.code, f"HTTP {e.code}")
        msg = f"调用失败：{hint}"
        if detail:
            msg += f"\n\n服务器原话：{detail}"
        return msg

    @staticmethod
    def _explain_url_error(e):
        reason = str(getattr(e, "reason", e))
        return (
            "连不上服务器。请检查：\n"
            "  1) 电脑能正常上网吗\n"
            "  2) 如果挂着网络代理，试试在 config.json 里把 proxy 设成 \"\" 直连\n"
            f"\n底层错误：{reason}"
        )

    # ------------------------------------------------------------ 对外接口

    def chat(self, messages, tools=None):
        """发一轮对话过去，返回模型的回复（一个 dict）。

        返回值里可能有两种东西：
          - content：一段文字，那就是最终回答
          - tool_calls：一串"我想查点东西"的请求，需要我们去执行
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools

        data = self._post(payload)

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"服务器没返回任何回复内容。原始返回：{json.dumps(data, ensure_ascii=False)[:400]}")

        msg = choices[0]["message"]
        # usage 挂在 message 外面，但上层要拿它算 token 用量。
        # 挂到这个 dict 上带出去 —— 省得上层为了统计再发一次请求。
        # 前缀下划线表示"这不是 API 字段"：agent 回填历史时会重新构造
        # assistant 消息（只取 content / tool_calls），所以它不会被回传。
        usage = data.get("usage")
        if usage:
            msg["_usage"] = usage
        return msg
