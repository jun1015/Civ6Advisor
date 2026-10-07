"""工具清单：告诉大模型"你能查什么"，以及怎么真的去查。

这里是 agent 的"手"。kb.py 是规则库本身，本文件把它的能力翻译成
大模型看得懂的说明书（JSON Schema），并负责真正执行调用。

写工具描述的原则：说清"什么时候该用它"，而不是"它是什么"。
模型是照着这句话决定要不要伸手的。
"""

from __future__ import annotations

import kb

# 单次工具返回的字符上限。超了就截断——防止一次查询把上下文塞爆，
# 白白烧钱还挤掉对话历史。
MAX_RESULT_CHARS = 6000

_NAME = {"type": "string",
         "description": "名称。中文（如「剑客」「圣地」）或游戏内部 ID"
                        "（如 UNIT_SWORDSMAN）都可以，会自动解析。"}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 30,
          "description": "最多返回几条，不填就用默认值（十几条）。够用就行，别贪多。"}

# (工具名, 用途说明, 参数表, 必填参数, 实际执行的函数)
TOOL_SPECS = [
    (
        "kb_search",
        "在规则库里模糊搜索关键词，返回【条目清单】（只有名称和类别，没有详情）。"
        "当你不确定玩家说的东西在游戏里叫什么名字、或者想先看看有哪些同类东西时，用这个。"
        "拿到确切名称之后，再用下面那些详情工具去查。",
        {"keyword": {"type": "string", "description": "要搜的关键词"}, "limit": _LIMIT},
        ["keyword"],
        lambda a: kb.search(a["keyword"], limit=int(a.get("limit", 15))),
    ),
    (
        "kb_list",
        "列出某一类型下【全部条目的名字】，只给名单、不给详情。两种时候用它："
        "① 玩家用的是俗称、你猜不出游戏里管它叫什么——比如玩家说「奶妈」，"
        "把单位名单看一遍你就能认出是「医疗兵」；"
        "② 问题是「有哪些XX」这类列举（如「远程单位有哪些」），"
        "这种换关键词是搜不出来的，因为答案是一份名单而不是一个条目。"
        "看完名单后，再用对应的详情工具去查具体条目。",
        {"kind": {
            "type": "string",
            "enum": list(kb.ENTITIES.keys()),
            "description": "要列哪一类。unit=单位、tech=科技、civic=市政、"
                           "building=建筑、district=区域、improvement=改良设施、"
                           "resource=资源、project=项目、policy=政策卡、"
                           "government=政体、civilization=文明、leader=领袖、"
                           "trait=特性、belief=信条、religion=宗教、feature=地貌、"
                           "terrain=地形、great_person=伟人",
        }},
        ["kind"],
        lambda a: kb.list_names(a["kind"]),
    ),
    (
        "kb_lore",
        "按描述文字搜索机制性内容。当玩家问的是【机制、概念、规则为什么这样】"
        "而不是某个具体东西时用这个。例如「宜居度」「忠诚度」「时代分数」"
        "「旅游业绩怎么来的」「为什么我的城市叛变了」。"
        "返回的是描述里提到该关键词的条目及它们的说明文字。",
        {"keyword": {"type": "string", "description": "机制关键词"},
         "limit": _LIMIT},
        ["keyword"],
        lambda a: kb.lore(a["keyword"], limit=int(a.get("limit", 12))),
    ),
    (
        "kb_unit",
        "查一个单位的完整档案：造价、战斗力、移动力、前置科技、升级链、单位能力、AI 行为。",
        {"name": _NAME},
        ["name"],
        lambda a: kb.unit(a["name"]),
    ),
    (
        "kb_tech",
        "查一项科技：研究成本、前置科技、尤里卡（加速）条件、解锁了哪些东西。",
        {"name": _NAME},
        ["name"],
        lambda a: kb.tech(a["name"]),
    ),
    (
        "kb_civic",
        "查一项市政：文化成本、前置市政、鼓舞（加速）条件、解锁了哪些东西。",
        {"name": _NAME},
        ["name"],
        lambda a: kb.civic(a["name"]),
    ),
    (
        "kb_building",
        "查一个建筑：造价、前置条件、必须建在哪个区域、住房/宜居度、产出加成。",
        {"name": _NAME},
        ["name"],
        lambda a: kb.building(a["name"]),
    ),
    (
        "kb_district",
        "查一个区域（如圣地、学院、工业区、商业中心）：造价、前置科技/市政、"
        "选址要求、以及【相邻加成怎么算】。",
        {"name": _NAME},
        ["name"],
        lambda a: kb.district(a["name"]),
    ),
    (
        "kb_improvement",
        "查一个改良设施（如农场、矿场、种植园、牧场）：产出、能建在什么地形上、"
        "需要什么科技。",
        {"name": _NAME},
        ["name"],
        lambda a: kb.improvement(a["name"]),
    ),
    (
        "kb_resource",
        "查一种资源（如铁、马、石油、香料）：它是战略/奢侈/加成哪一类、"
        "给地块加什么产出、用什么改良设施开采、能出现在哪些地形植被上、"
        "以及需要什么科技才能开采。",
        {"name": _NAME},
        ["name"],
        lambda a: kb.resource(a["name"]),
    ),
    (
        "kb_policy",
        "查一张政策卡的实际生效效果（不是卡面文字，是规则库里真实的数值）。",
        {"name": _NAME},
        ["name"],
        lambda a: kb.policy(a["name"]),
    ),
    (
        "kb_government",
        "查一个政体（如酋长国、君主制、民主制）：有哪几个政策槽、槽位类型、"
        "以及固有加成。当玩家问「该换哪个政体」时，配合政策卡一起查。",
        {"name": _NAME},
        ["name"],
        lambda a: kb.government(a["name"]),
    ),
    (
        "kb_civilization",
        "查一个文明（如中国、罗马、希腊）：文明特有能力、特色单位/建筑/区域/改良设施、"
        "以及它有哪些可选领袖。",
        {"name": _NAME},
        ["name"],
        lambda a: kb.civilization(a["name"]),
    ),
    (
        "kb_leader",
        "查一个领袖（如秦始皇、凯撒）：领袖特性具体效果、以及领袖议程"
        "（AI 扮演这个领袖时喜欢/讨厌什么）。",
        {"name": _NAME},
        ["name"],
        lambda a: kb.leader(a["name"]),
    ),
    (
        "kb_any",
        "万能查询：不确定要查的东西属于哪一类时用这个，会自动识别类型并给出详情。"
        "当你只是想快速了解某个东西、不想纠结用哪个工具时，直接用它最省事。",
        {"name": _NAME},
        ["name"],
        lambda a: kb.any(a["name"]),
    ),
    (
        "kb_compare",
        "把多个【同类型】的东西并排列出来做对比。玩家问「A 和 B 哪个好」"
        "「这几个有什么区别」时用这个，比分别查几次再自己拼更好。",
        {"names": {"type": "string",
                   "description": "要对比的名称，用英文逗号分隔，例如「剑客,弓箭手,枪兵」"},
         "kind": {"type": "string",
                  "description": "这些东西属于哪一类。按上面的清单选一个。",
                  "enum": ["unit", "building", "district", "improvement", "resource",
                           "policy", "tech", "civic", "government", "leader",
                           "civilization", "belief"]}},
        ["names", "kind"],
        lambda a: kb.compare(a["names"], a["kind"]),
    ),
]


def build_tools():
    """生成传给大模型的工具说明书（OpenAI 兼容格式）。"""
    out = []
    for name, desc, props, required, _fn in TOOL_SPECS:
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                },
            },
        })
    return out


_DISPATCH = {name: fn for name, _d, _p, _r, fn in TOOL_SPECS}


def _clip(text):
    """限制单次返回的长度，并在截断处说清原因，好让模型自己调整策略。"""
    text = "" if text is None else str(text)
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return (text[:MAX_RESULT_CHARS]
            + f"\n\n（结果太长，已截断，只显示前 {MAX_RESULT_CHARS} 字。"
              "可以换个更精确的关键词，或者把 limit 调小。）")


def execute(name, args):
    """执行一次工具调用，永远返回字符串（出错也返回文字，不抛异常）。

    返回文字而不是抛异常，是为了让模型看到失败原因后能自己换个查法，
    而不是整个程序崩掉。
    """
    fn = _DISPATCH.get(name)
    if fn is None:
        return f"错误：没有名为 {name} 的工具。"
    if not isinstance(args, dict):
        args = {}
    try:
        return _clip(fn(args))
    except KeyError as e:
        missing = str(e).strip("'")
        return f"错误：缺少必填参数 {missing}。"
    except Exception as e:
        return (f"查询出错：{type(e).__name__}: {e}\n"
                "（可能是这个名字库里没有。可以先用 kb_search 搜一下确认准确名称。）")


def tool_names():
    return [name for name, _d, _p, _r, _f in TOOL_SPECS]
