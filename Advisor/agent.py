"""文明6 顾问 —— agent 主循环。

用法：
    python agent.py                进入对话模式
    python agent.py "圣地怎么加成"  只问一句就退出

它是怎么工作的：
    你的问题 -> 大模型 -> （想查资料就调工具）-> 我们把结果喂回去
             -> 大模型再看 -> 还缺就再查 -> 直到能回答

这个"想查就查、查完再看"的循环，就是 agent 和普通聊天机器人的区别。
"""

from __future__ import annotations

import json
import os
import sys

# Windows 控制台默认是 GBK，直接 print 中文可能乱码或报错，这里强制成 UTF-8
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import llm
import paths
import tools

# 打包成 exe 之后 __file__ 会指向临时解压目录，所以统一走 paths.app_dir()
HERE = paths.app_dir()
CONFIG_PATH = os.path.join(HERE, "config.json")
# 密钥单独一个文件，**不进 config.json**。
# config.json 是可以提交/分享的"普通设置"；密钥是唯一绝不能外流的东西，
# 单独放一个文件，.gitignore 里排除掉它就干净了。
SECRET_PATH = os.path.join(HERE, "secret.json")
# 也可以不改文件、直接用环境变量喂密钥（CI 或临时换 key 时方便）
SECRET_ENV = "ADVISOR_API_KEY"
PATCH_PATH = os.path.join(HERE, "text_patch.json")


def refresh_text_patch():
    """启动时刷新本地化补丁（约 1 秒）。

    补丁装的是资料片/DLC 的中文名（游戏生成的数据库里缺这部分）。
    游戏一更新或装了新 DLC，旧补丁就缺新文本，所以每次启动重扫一遍。
    内容没变时结果**完全一致**（实测 md5 相同），重复跑没有任何副作用。

    任何失败都静默降级：找不到游戏目录也好、脚本坏了也好，
    一律继续用现有补丁——绝不能让这个环节拖垮启动。
    """
    had_patch = os.path.exists(PATCH_PATH)
    try:
        import build_text_patch
        ok, _ = build_text_patch.rebuild(silent=True)
    except Exception:
        ok = False

    # 只有"本来就没补丁、又没生成出来"才值得打扰用户：这种情况资料片的
    # 中文名会缺失，模型看到裸英文 ID 会跳过整条（之前实测漏答过圣地两条加成）。
    if not ok and not had_patch:
        print("提示：没能从游戏目录生成本地化补丁，资料片的部分中文名称会缺失。")
        print("      可手动运行 python build_text_patch.py 查看原因。\n")

CONFIG_TEMPLATE = {
    # "custom" = 通用：不预设任何一家服务商，接口地址和模型名由你自己填。
    # 想省事可以改成 llm.py 里列出的那几家之一（deepseek / qwen / zhipu / openai），
    # 地址和模型名会自动带出来。**这个值不影响任何功能**，只是个快捷方式。
    "provider": "custom",
    # 注意：这里**故意没有 api_key**。
    # 密钥存在同目录的 secret.json 里（见 SECRET_PATH），不写进 config.json ——
    # 这样 config.json 可以放心提交到 Git 或发给别人。
    "base_url": "",
    "model": "",
    "temperature": 0.3,
    "timeout": 120,
    "max_steps": 8,
    "max_turns": 10,
    "proxy": None,
}

SYSTEM_PROMPT = """你是「文明6 顾问」，一个坐在玩家旁边看他打游戏的老手。

# 最重要的一条规矩

你手上有一批【工具】，查询的是游戏文件里读出来的规则库——里面的数据是绝对权威的。
但你自己脑子里记的文明6知识不可靠：可能过时、可能混进了别代游戏、也可能记错了数值。

所以：**凡是涉及具体数值、具体效果、具体条件的问题，一律先调用工具查，再回答。**
哪怕你觉得自己很确定，也要查。查一下的成本极低，答错的代价很高。

# 怎么挑工具

- 知道确切名字 → 直接用对应的详情工具（kb_unit / kb_district / kb_tech …）
- 不确定游戏里管它叫什么 → 先用 kb_search 搜出准确名称
- 搜不到、或者玩家说的明显是俗称黑话（"奶妈""大金""投石"这种）→ 用 kb_list
  把那一类的**全部名字**看一遍，照着对一下就认出来了。
  **别反复猜同义词硬试**——猜中是运气，看一遍名单是确定的
- 问的是"有哪些 XX"这类列举（"远程单位有哪些""哪些建筑给住房"）→ 也用 kb_list。
  这种问题搜关键词是搜不出来的，因为答案是一份名单而不是一个条目
- 问的是机制、概念、规则（"宜居度""忠诚度""为什么城市叛变"）→ 用 kb_lore
- 懒得判断类型 → 直接用 kb_any，它会自动识别
- "A 和 B 哪个好""这俩有什么区别" → 用 kb_compare 一次查完

一次查不到就换个关键词再查，允许连着查好几次。

# 查不到的时候

如果规则库里确实没有这条内容，就老实告诉玩家"规则库里没有这条记录"，
并说明你的推测来自哪里、可信度如何。**绝对不要编造数值和效果。**

# 回答的风格

- 玩家是新手，说人话，别堆术语；必须用术语时顺手解释一下
- 先给结论，再给依据，别让他读到第三段才知道答案
- 数字必须来自工具结果，不许自己凑
- 规则里出现「每 2 个 +1」这种写法时，它的意思就是**每个只加 0.5**，
  别四舍五入说成"每个 +1"（学院、工业区、圣地拿区域/树林相邻加成的都是这种）
- 别把工具返回的原文照抄一遍，那是给你的材料，不是给他的答案
- 简洁。能三句说完就别写五句
"""


def format_game_state(raw):
    """把游戏导出的战局快照包成给模型看的一段说明。

    快照本身已经是"键=值"的紧凑文本（由 AdvisorLauncher.lua 拼好），
    这里只加来源说明，不改动内容 —— 少一道解析就少一个出错的地方。
    """
    return (
        "【玩家当前战局 · 游戏内实时导出】\n"
        "以下数据是玩家点开你的那一刻，从他的游戏里导出的真实局面。\n"
        "回答「我该怎么办」「接下来造什么」这类问题时，务必结合它，"
        "不要凭空推测玩家现在处于什么处境。\n"
        "如果它和玩家的说法对不上（数据可能滞后一轮），以玩家说的为准，"
        "并顺口提一句你看到的和他描述的不太一样，请他确认。\n"
        "注意：数据里没写的，就是这次没导出来，别当成「没有」。\n"
        "\n"
        + raw
    )


# ------------------------------------------------------------------ 密钥
#
# 密钥单独一个文件，不跟 config.json 混在一起。
# 理由：config.json 是"可以给出去"的普通设置（模型名、接口地址），密钥不是。
# 混在一起的话，任何一次"把配置发我看看"都会把密钥带出去；提交到 Git 更是一泄
# 到底 —— 而且 Git 历史里的东西删不干净，光在最新版本里删掉没用。
# 分开之后，.gitignore 里排除 secret.json 一个文件就完事了。


def load_secret():
    """读 secret.json。文件不存在或内容坏了都返回空 dict，绝不抛异常。

    这个函数在启动路径上，它坏掉不该导致程序起不来 —— 大不了当作"没配密钥"，
    后面会提示用户重新填一次。
    """
    if not os.path.exists(SECRET_PATH):
        return {}
    try:
        with open(SECRET_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_secret(key):
    """把密钥写进 secret.json（保留文件里的其他字段）。"""
    data = load_secret()
    data["api_key"] = key
    with open(SECRET_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def resolve_api_key():
    """密钥的读取顺序：环境变量 → secret.json。"""
    env = (os.environ.get(SECRET_ENV) or "").strip()
    if env:
        return env
    return (load_secret().get("api_key") or "").strip()


def _write_config(raw):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


def _migrate_key_out_of_config(cfg):
    """老版本把密钥存在 config.json 里。第一次读到它就搬进 secret.json。

    ⚠️ 顺序必须是**先写成功、再删**。反过来的话，中途一失败密钥就丢了。
    写完还要回读校验：读不到同样的值就当没搬过，原样留着最安全。
    """
    legacy = (cfg.get("api_key") or "").strip()
    if not legacy:
        return

    if not (load_secret().get("api_key") or "").strip():
        try:
            save_secret(legacy)
        except OSError:
            return                      # 写不进去就原样留着，至少别弄丢
        if (load_secret().get("api_key") or "").strip() != legacy:
            return                      # 回读校验没过，同样不动

    cfg.pop("api_key", None)
    try:
        _write_config(cfg)
    except OSError:
        return
    print("密钥已移到单独的文件：\n"
          f"  {SECRET_PATH}\n"
          "（config.json 里不再保存密钥；这个文件已在 .gitignore 里排除）\n")


def load_config():
    """读配置。文件不存在就生成一份，然后照常往下走。

    注意：**不在这里要密钥**。模板里本来就没有密钥（见 CONFIG_TEMPLATE 的说明），
    缺密钥由 ensure_api_key() 在终端问、或浮层的设置面板问。
    所以新克隆一份代码下来，第一次运行就能直接用 —— 不用"先跑一次再跑一次"。
    """
    if not os.path.exists(CONFIG_PATH):
        _write_config(CONFIG_TEMPLATE)
        print(f"已生成默认配置：\n  {CONFIG_PATH}\n"
              "（里面不含密钥 —— 密钥存在同目录的 secret.json，只会问你要一次）\n")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError as e:
            raise SystemExit(f"config.json 格式有误（第 {e.lineno} 行）：{e.msg}") from e

    # 老版本的密钥先搬家（搬完 cfg 里就没有 api_key 了）
    _migrate_key_out_of_config(cfg)

    merged = dict(CONFIG_TEMPLATE)
    merged.update(cfg)
    merged["api_key"] = resolve_api_key()

    provider = (merged.get("provider") or llm.DEFAULT_PROVIDER).strip().lower()
    p = llm.preset(provider)
    # 预设只是"快捷填充"：只有它真的有值时才拿来兜底。
    # "通用"预设的地址/模型是空的 —— 那就保持空，让发起请求时给出明确的提示，
    # 而不是偷偷替用户选一家（这正是"不绑死某家"的意思）。
    if not merged.get("base_url"):
        merged["base_url"] = p["base_url"]
    if not merged.get("model"):
        merged["model"] = p["model"]
    merged["key_url"] = p["key_url"]
    return merged


def save_api_key(key):
    """把密钥存进 secret.json（**不再**写 config.json）。"""
    save_secret(key.strip())


def save_settings(base_url=None, model=None, api_key=None, provider=None):
    """把浮层设置面板改的几项写回去，两个文件分开存：

        provider / base_url / model / temperature → config.json（不含密钥）
        api_key                                   → secret.json（.gitignore 排除）

    传 None 表示"这一项没动" —— 不覆盖。
    另外**只在真的变了才落盘**：以前每次点保存都重写 config.json，
    没必要，也让"文件被我改过了"这件事失去信号价值。

    base_url / model 平时是空的，靠 load_config() 从 PROVIDERS 预设兜底。
    把兜底值当作用户设置写死进文件，以后想换 provider 就换不动了。
    """
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    dirty = False
    for key, val in (("provider", provider), ("base_url", base_url),
                     ("model", model)):
        if val is not None and raw.get(key) != val:
            raw[key] = val
            dirty = True
    if "api_key" in raw:            # 顺手清掉老版本遗留的密钥
        raw.pop("api_key")
        dirty = True
    if dirty:
        _write_config(raw)

    if api_key is not None:
        key = api_key.strip()
        if key:
            save_secret(key)
        else:                       # 填了空 = 想清掉
            data = load_secret()
            if data.pop("api_key", None) is not None:
                with open(SECRET_PATH, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

    return raw


def ensure_api_key(config):
    """没配 key 时，直接在终端里问用户要，拿到就存起来。

    这样用户永远不用自己去编辑配置文件。填一次，以后永久生效。
    （浮层版没有终端，走的是设置面板那条路，见 overlay.py。）
    """
    if (config.get("api_key") or "").strip():
        return config

    p = llm.preset(config.get("provider"))
    print("=" * 60)
    print("第一次使用，需要先配一个 API 密钥。")
    print()
    if p["key_url"]:
        print(f"  1) 打开 {p['key_url']}")
    else:
        print("  1) 打开你所用接口服务商的后台")
    print("  2) 找到「API Keys」（或叫「密钥管理」），点创建")
    print("  3) 把那串密钥完整复制下来")
    print()
    print("  （只做这一次，以后运行不会再问）")
    print("=" * 60)
    print()

    try:
        key = input("请把 key 粘贴到这里，然后回车 > ").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\n已取消。")

    if not key:
        raise SystemExit("没有输入 key，已退出。想改用免费的本地模型，可以告诉我。")

    if len(key) < 10:
        raise SystemExit(f"「{key}」看起来不像一个 key（太短了）。请重新运行再试一次。")

    save_api_key(key)
    config["api_key"] = key
    print(f"\n已保存到 {os.path.basename(SECRET_PATH)}，以后不用再填了。\n")
    return config


class Advisor:
    def __init__(self, config):
        self.config = config
        self.max_steps = int(config.get("max_steps") or 8)
        # 网络客户端**不在这里建** —— 见 _ensure_client() 的说明。
        self.client = None
        self.tool_specs = tools.build_tools()
        self.max_turns = max(1, int(config.get("max_turns") or 10))
        self._state_at = None        # 战局快照在 messages 里的位置
        self.reset()

    def _ensure_client(self):
        """第一次真要发请求时才建网络客户端。

        ⚠️ 故意**不放在 __init__** 里。接口地址和模型名都是用户填的，填错、
        或者还没填（程序默认不预设任何一家，所以"还没填"是常态）时，
        __init__ 一抛异常，浮层就整个起不来了 —— 而 pythonw 下连控制台都没有，
        用户只会看到"窗口没出现"。
        挪到第一次提问时再建：窗口照常显示，报错就出现在用户正看着的地方。
        """
        if self.client is None:
            self.client = llm.LLMClient(
                api_key=self.config.get("api_key"),
                base_url=self.config.get("base_url"),
                model=self.config.get("model"),
                timeout=int(self.config.get("timeout") or 120),
                proxy=self.config.get("proxy"),
                temperature=float(self.config.get("temperature") or 0.3),
            )
        return self.client

    def reset(self):
        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._state_at = None
        self._dropped = 0            # 被滑出窗口、已经丢掉的历史轮数
        self.last_turn = {}          # 本轮问答的 token 用量
        # 会话累计（跨多轮问答）
        self.usage_total = {"prompt": 0, "completion": 0, "cached": 0, "calls": 0}
        # 最近**一次请求**的 prompt token —— 用来判断上下文是不是快满了
        self.last_prompt_tokens = 0

    def _set_game_state(self, raw):
        """把战局快照挂到人格设定之后，永远只保留一份。

        为什么放在 system 而不是当 user 消息追加：
          它属于"运行环境"，不是"玩家说过的话"。混进对话历史的话，
          连着问几轮就会攒下好几份不同时间的局面，模型容易看糊涂。

        为什么要替换而不是累积：
          局面是会变的。旧的那份留着只有坏处。
        """
        if self._state_at is not None:
            del self.messages[self._state_at]
            self._state_at = None
        if raw:
            self.messages.insert(1, {
                "role": "system",
                "content": format_game_state(raw),
            })
            self._state_at = 1

    def _trim(self):
        """历史太长就滑掉最旧的若干轮。

        开头两条不能动：人格设定（第 0 条），以及战局快照（第 1 条，如果有）。

        为什么按"轮"切而不是按"条"：tool 消息必须和它的调用请求
        （那条带 tool_calls 的 assistant 消息）待在一起 —— 拆开的话
        API 会直接报错"找到 tool 消息却找不到对应的父消息"。
        所以以 role == "user" 作为每一轮的起点，整段保留或整段丢。

        丢掉的不是静默消失：在历史开头留一句提示，让模型**知道自己不知道**。
        这样被追问细节时它会说"那段我没看到"，而不是编一个。
        （这比让模型自己压缩安全 —— 压缩会改写数值，错了会一路污染下去。）
        """
        head_len = 1 + (1 if self._state_at is not None else 0)
        head = self.messages[:head_len]
        body = self.messages[head_len:]

        starts = [i for i, m in enumerate(body) if m.get("role") == "user"]
        if len(starts) <= self.max_turns:
            return

        keep_from = starts[len(starts) - self.max_turns]
        self._dropped += len(starts) - self.max_turns
        self.messages = head + [{
            "role": "system",
            "content": (f"（更早的 {self._dropped} 轮问答已省略。"
                        "如果玩家追问那些内容里的细节，说明你没看到，"
                        "请让他重说一遍，不要凭印象猜。）"),
        }] + body[keep_from:]

    def _tally_usage(self, reply):
        """把这次请求的 token 用量记进累计。

        一次 ask() 可能发好几次请求（每轮工具调用都要再问一次），
        所以在循环里逐次累加，而不是等问答结束再算一次。
        """
        u = reply.get("_usage") or {}
        if not u:
            return
        p = int(u.get("prompt_tokens") or 0)
        c = int(u.get("completion_tokens") or 0)
        self.usage_total["prompt"] += p
        self.usage_total["completion"] += c
        self.usage_total["calls"] += 1
        # "命中缓存"的那部分 —— 计费便宜一个数量级。
        # 我们每轮都要发那 7000 字符固定开销，看这个数才知道真实花销。
        # 字段名各家不一样：DeepSeek 报 prompt_cache_hit_tokens，
        # OpenAI 系放在 prompt_tokens_details.cached_tokens 里。两种都认，
        # 都没有就当 0（并不影响别的功能，只是那行少一个数字）。
        hit = u.get("prompt_cache_hit_tokens")
        if hit is None:
            details = u.get("prompt_tokens_details")
            if isinstance(details, dict):
                hit = details.get("cached_tokens")
        if isinstance(hit, (int, float)):
            self.usage_total["cached"] += int(hit)
        self.last_prompt_tokens = p

        t = self.last_turn
        t["prompt"] = t.get("prompt", 0) + p
        t["completion"] = t.get("completion", 0) + c
        t["calls"] = t.get("calls", 0) + 1

    # ------------------------------------------------------------ 主循环

    def ask(self, question, verbose=True, on_event=None, game_state=None):
        """问一个问题，返回最终回答（字符串）。

        on_event(kind, text) 是可选回调，用来把"它正在查什么"实时抛给界面：
          kind="thinking" 模型说它打算去查什么
          kind="tool"     正在执行哪个工具
        命令行不传它就照旧打印；浮层传它，把进度画进窗口。

        game_state 是游戏导出的战局快照原文（可能是 None）。
        每次都用整份替换掉上一份，不会越攒越多。
        """
        self._set_game_state(game_state)
        self._trim()                 # 历史太长就先滑掉最旧的那几轮
        self.last_turn = {}          # 本轮用量的账本，从头计

        def notify(kind, text):
            if on_event is not None:
                on_event(kind, text)
            elif verbose:
                print(text)

        self.messages.append({"role": "user", "content": question})

        for step in range(1, self.max_steps + 1):
            reply = self._ensure_client().chat(self.messages, self.tool_specs)
            self._tally_usage(reply)

            content = reply.get("content") or ""
            tool_calls = reply.get("tool_calls") or []

            # 把模型的这一轮回复记进历史。注意只保留这三个字段，
            # 多余的字段（比如推理模型的 reasoning_content）回传会报错。
            assistant_msg = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            self.messages.append(assistant_msg)

            if not tool_calls:
                return content or "（模型没有返回内容）"

            # 模型要求查资料：逐个执行，把结果喂回去
            if content:
                notify("thinking", f"  · {content.strip()}")

            for call in tool_calls:
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                raw_args = fn.get("arguments") or "{}"

                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    result = (f"错误：参数不是合法 JSON（{raw_args}）。"
                              "请重新给出正确的参数。")
                    args = None
                else:
                    result = tools.execute(name, args)

                shown = self._brief(args if args is not None else raw_args)
                notify("tool", f"  [{step}] 查 {name} {shown}")

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": result,
                })

        return (f"（我已经连着查了 {self.max_steps} 轮还没得出结论，先停一下。）\n"
                "这个问题可能太大了，试着问得更具体一点？比如把「我该怎么发展」"
                "换成「圣地相邻加成怎么算」。")

    @staticmethod
    def _brief(args):
        """把参数压成一行短提示，方便在终端里看 agent 在干什么。"""
        try:
            if isinstance(args, str):
                return args[:60]
            parts = [f"{k}={v}" for k, v in args.items()]
            return "(" + ", ".join(parts)[:60] + ")"
        except Exception:
            return ""


# ---------------------------------------------------------------- 命令行

BANNER = """文明6 顾问 —— 基于游戏规则库的问答助手

直接打字提问即可，例如：
  圣地相邻加成怎么算
  剑客和枪兵哪个强
  宜居度低了会怎样

命令： /reset 清空对话   /tools 看能查什么   /exit 退出
"""


def main():
    try:
        config = load_config()
        config = ensure_api_key(config)
    except SystemExit as e:
        print(e)
        return 1

    refresh_text_patch()

    try:
        advisor = Advisor(config)
    except llm.LLMError as e:
        print(f"启动失败：{e}")
        return 1

    # 有命令行参数：问一句就退出
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:]).strip()
        if not question:
            print("用法：python agent.py \"你的问题\"")
            return 1
        try:
            print(advisor.ask(question))
        except llm.LLMError as e:
            print(f"出错：{e}")
            return 1
        return 0

    # 否则进入对话模式
    print(BANNER)

    while True:
        try:
            question = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0

        if not question:
            continue
        if question in ("/exit", "/quit", "exit", "quit"):
            print("再见。")
            return 0
        if question == "/reset":
            advisor.reset()
            print("（对话历史已清空）")
            continue
        if question == "/tools":
            for t in tools.TOOL_SPECS:
                print(f"  {t[0]:<16} {t[1].splitlines()[0][:50]}...")
            continue

        try:
            answer = advisor.ask(question)
        except llm.LLMError as e:
            print(f"\n出错：{e}\n")
            continue

        print(f"\n{answer}\n")


if __name__ == "__main__":
    sys.exit(main())
