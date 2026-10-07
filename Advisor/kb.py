#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""civ6_kb.py —— 文明6 权威规则库检索层（供 AI 工具调用）

数据源是游戏自己编译出来的缓存，只读、离线、不需要装 mod：

    %LOCALAPPDATA%\\Firaxis Games\\Sid Meier's Civilization VI\\Cache\\
        DebugGameplay.sqlite       428 张表   规则总账
        DebugLocalization.sqlite   18.2 万条  12 种语言的文本

为什么不把 SQL 交给调用方（尤其不要让模型自己写）：
  1. 428 张表，一条"单位"信息散在 4~6 张表里，要 JOIN 才能拼完整；
  2. 列名不按套路——`Boosts` 表用 `TechnologyType` 而不是 `Technology`，
     `UnitPromotions` 的主键叫 `UnitPromotionType` 而不是 `PromotionType`；
  3. 模型猜错的后果是"静默给错答案"，比报错更危险。
所以这里把 JOIN 全部做完，对外只暴露**语义函数**：
调用方只负责"选函数 + 填中文名"，例如 unit("剑客")。

输出是中文 Markdown，不是裸 JSON —— 给模型看省 token，给人看直接可读。
优先使用库里自带的人类可读描述（`LOC_*_DESCRIPTION`），数值参数作补充；
原作者写好的中文，比任何二次翻译都准。
"""

import os
import re
import sys
import json
import sqlite3
import argparse
import threading
import paths

# ---------------------------------------------------------------- 数据源定位

CACHE_SUBDIR = os.path.join("Firaxis Games", "Sid Meier's Civilization VI", "Cache")


def find_cache_dir():
    """定位游戏的缓存目录（优先 LOCALAPPDATA）。"""
    cands = []
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cands.append(os.path.join(la, CACHE_SUBDIR))
    up = os.environ.get("USERPROFILE")
    if up:
        cands.append(os.path.join(up, "AppData", "Local", CACHE_SUBDIR))
    home = os.path.expanduser("~")
    cands.append(os.path.join(home, "AppData", "Local", CACHE_SUBDIR))
    for c in cands:
        if os.path.isfile(os.path.join(c, "DebugGameplay.sqlite")):
            return c
    raise FileNotFoundError(
        "没找到文明6的缓存目录。请确认游戏已安装、且至少启动过一次。\n"
        "期望位置：" + "\n  ".join(cands)
    )


# ------------------------------------------------------- 实体类型登记（类型 -> 表）

ENTITIES = {
    "unit":         ("Units",                  "UnitType",                  "单位"),
    "tech":         ("Technologies",           "TechnologyType",            "科技"),
    "civic":        ("Civics",                 "CivicType",                 "市政"),
    "building":     ("Buildings",              "BuildingType",              "建筑"),
    "district":     ("Districts",              "DistrictType",              "区域"),
    "improvement":  ("Improvements",           "ImprovementType",           "改良设施"),
    "resource":     ("Resources",              "ResourceType",              "资源"),
    "project":      ("Projects",               "ProjectType",               "项目"),
    "policy":       ("Policies",               "PolicyType",                "政策卡"),
    "government":   ("Governments",            "GovernmentType",            "政体"),
    "civilization": ("Civilizations",          "CivilizationType",          "文明"),
    "leader":       ("Leaders",                "LeaderType",                "领袖"),
    "trait":        ("Traits",                 "TraitType",                 "特性"),
    "belief":       ("Beliefs",                "BeliefType",                "信条"),
    "religion":     ("Religions",              "ReligionType",              "宗教"),
    "feature":      ("Features",               "FeatureType",               "地貌"),
    "terrain":      ("Terrains",               "TerrainType",               "地形"),
    "great_person": ("GreatPersonIndividuals", "GreatPersonIndividualType", "伟人"),
}

# 从本地化 tag 的前缀猜实体类型（用于全库搜索）
TAG_PREFIX_TO_KIND = {
    "UNIT_": "unit", "TECH_": "tech", "CIVIC_": "civic", "BUILDING_": "building",
    "DISTRICT_": "district", "IMPROVEMENT_": "improvement", "RESOURCE_": "resource",
    "PROJECT_": "project", "POLICY_": "policy", "GOVERNMENT_": "government",
    "CIVILIZATION_": "civilization", "LEADER_": "leader", "TRAIT_": "trait",
    "BELIEF_": "belief", "RELIGION_": "religion", "FEATURE_": "feature",
    "TERRAIN_": "terrain", "GREAT_PERSON_": "great_person",
}

# 库里有中文、但 tag 不遵循 `LOC_<ID>_NAME` 约定的枚举值。
# 这些是从实际查询里逐个核对出来的，不是猜的。
ENUM_ZH = {
    "DOMAIN_LAND": "陆地", "DOMAIN_SEA": "海洋", "DOMAIN_AIR": "空中",
    "YIELD_FOOD": "食物", "YIELD_PRODUCTION": "生产力", "YIELD_GOLD": "金币",
    "YIELD_SCIENCE": "科技值", "YIELD_CULTURE": "文化值", "YIELD_FAITH": "信仰值",
    "YIELD_TOURISM": "旅游业绩", "YIELD_DIPLOMATIC_FAVOR": "外交支持",
    "YIELD_HOUSING": "住房", "YIELD_AMENITIES": "宜居度",
    "UNITAI_COMBAT": "战斗", "UNITAI_EXPLORE": "探索",
    "UNITTYPE_LAND_COMBAT": "陆地战斗", "UNITTYPE_MELEE": "近战",
    "UNITTYPE_RANGED": "远程", "UNITTYPE_SIEGE": "攻城", "UNITTYPE_NAVAL": "海军",
    "PROMOTION_CLASS_MELEE": "近战", "PROMOTION_CLASS_RANGED": "远程",
    "PROMOTION_CLASS_ANTI_CAVALRY": "反骑兵", "PROMOTION_CLASS_LIGHT_CAVALRY": "轻骑兵",
    "PROMOTION_CLASS_HEAVY_CAVALRY": "重骑兵", "PROMOTION_CLASS_SIEGE": "攻城",
    "PROMOTION_CLASS_NAVAL_MELEE": "海军近战", "PROMOTION_CLASS_NAVAL_RANGED": "海军远程",
    "PROMOTION_CLASS_NAVAL_RAIDER": "海军袭击者", "PROMOTION_CLASS_NAVAL_CARRIER": "航空母舰",
    "FORMATION_CLASS_LAND_COMBAT": "陆地战斗", "FORMATION_CLASS_NAVAL": "海军",
    "FORMATION_CLASS_AIR": "空中", "FORMATION_CLASS_SUPPORT": "支援",
    "FORMATION_CLASS_CIVILIAN": "平民",
}

# compare() 输出的表头用中文（列名 -> 中文标签）
FIELD_ZH = {
    "Combat": "近战攻击", "RangedCombat": "远程攻击", "Range": "射程",
    "Cost": "造价", "Maintenance": "维护费", "PrereqTech": "前置科技",
    "PrereqCivic": "前置市政", "StrategicResource": "战略资源",
    "PromotionClass": "晋升类别", "Domain": "活动域", "CombatClass": "战斗类别",
    "Housing": "住房", "Entertainment": "娱乐设施", "Amenities": "宜居度",
    "PrereqDistrict": "所在区域", "EraType": "时代", "CitizenSlots": "公民槽位",
    "TilesRequired": "所需地块", "ObsoleteEra": "过时时代",
    "IsWonder": "是否奇观",
    "ResourceClassType": "资源类别", "GovernmentSlotType": "卡槽类型",
    "InfluencePointsPerTurn": "每回合影响力", "Appeal": "魅力影响",
    "OnePerCity": "每城限一", "Frequency": "出现频度", "Tier": "等级",
}

# compare() 对每类实体取哪些列。
# 没有列进来的类型（领袖/文明/信条）差别全在文字里，硬排表格只会得到一张空表，
# 那些类型改走"逐条列描述"（见 compare 的实现）。
COMPARE_FIELDS = {
    "unit": ["Combat", "RangedCombat", "Range", "Cost", "Maintenance",
             "PrereqTech", "StrategicResource", "PromotionClass", "Domain"],
    "building": ["Cost", "Maintenance", "PrereqTech", "PrereqDistrict",
                 "Housing", "Entertainment"],
    "district": ["Cost", "PrereqTech", "Housing", "Entertainment", "CitizenSlots"],
    "tech": ["Cost", "EraType"],
    "civic": ["Cost", "EraType"],
    "improvement": ["PrereqTech", "PrereqCivic", "Housing", "Appeal",
                    "OnePerCity", "TilesRequired"],
    "resource": ["ResourceClassType", "Happiness", "PrereqTech", "Frequency"],
    "policy": ["GovernmentSlotType", "PrereqCivic", "PrereqTech"],
    "government": ["PrereqCivic", "Tier", "InfluencePointsPerTurn"],
}




_TEXT_PATCH = None


def _text_patch():
    """懒加载 text_patch.json —— 补游戏库缺失的 DLC 文本。

    由同目录的 build_text_patch.py 生成。游戏生成的 DebugLocalization.sqlite
    对 DLC/资料片文本覆盖不全（实测缺 1.7 万条），于是「市政广场」「棉花堡」
    这类实体只能显示成裸 ID，模型看不懂就会跳过。补丁从游戏原始 XML 里捞。
    文件不存在就当空表，静默降级。
    """
    global _TEXT_PATCH
    if _TEXT_PATCH is None:
        path = os.path.join(paths.app_dir(), "text_patch.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                _TEXT_PATCH = json.load(f)
        except (OSError, json.JSONDecodeError):
            _TEXT_PATCH = {}
    return _TEXT_PATCH


class Civ6KB:
    """只读的规则库访问对象。

    连接是**按线程**建的，这一点很关键：
      浮层（overlay.py）每问一个问题就新开一个线程，而 sqlite3 的连接
      默认不允许跨线程使用——在别的线程里执行会抛
      `ProgrammingError: SQLite objects created in a thread can only be used
      in that same thread`。
      这个对象是全局单例，于是"第一个问题正常、从第二个问题起所有查询全废"。
      按线程各持一套连接就绕开了：线程用完自己那套，随线程一起释放。

    连接一律 mode=ro（只读），绝不写游戏数据库。
    """

    def __init__(self, cache_dir=None):
        self.cache_dir = cache_dir or find_cache_dir()
        self._cols_cache = {}
        self._patch_cache = {}
        self._local = threading.local()

    def _conn(self):
        """取当前线程的连接，没有就建一套。"""
        loc = self._local
        if getattr(loc, "gc", None) is None:
            gp = os.path.join(self.cache_dir, "DebugGameplay.sqlite")
            lo = os.path.join(self.cache_dir, "DebugLocalization.sqlite")
            loc.g = sqlite3.connect(f"file:{gp}?mode=ro", uri=True)
            loc.l = sqlite3.connect(f"file:{lo}?mode=ro", uri=True)
            loc.gc = loc.g.cursor()
            loc.lc = loc.l.cursor()
        return loc

    @property
    def g(self):
        return self._conn().g

    @property
    def l(self):
        return self._conn().l

    @property
    def gc(self):
        return self._conn().gc

    @property
    def lc(self):
        return self._conn().lc

    def close(self):
        """关掉**当前线程**的连接。

        外层用完记得调（见 kb.close_thread_conn）：线程对象被回收时才释放
        会拖上一阵子，长跑的浮层里积攒起来是白白占着文件句柄。
        """
        loc = self._local
        for name in ("g", "l"):
            con = getattr(loc, name, None)
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
                setattr(loc, name, None)
        loc.gc = loc.lc = None

    # ------------------------------------------------------------ 底层工具

    def cols(self, table):
        if table not in self._cols_cache:
            self._cols_cache[table] = [
                r[1] for r in self.gc.execute(f'PRAGMA table_info("{table}")')
            ]
        return self._cols_cache[table]

    def pk(self, table, guess=None):
        """拿主键列名。猜错就退回到"第一个以 Type 结尾的列"。"""
        cs = self.cols(table)
        if guess and guess in cs:
            return guess
        for c in cs:
            if c.lower().endswith("type"):
                return c
        return cs[0]

    def name_col(self, table):
        """拿存本地化 tag 的列名（通常叫 Name）。"""
        cs = self.cols(table)
        if "Name" in cs:
            return "Name"
        for c in cs:
            if c.lower().endswith("name"):
                return c
        return None

    def has(self, table, col):
        return col in self.cols(table)

    def zh(self, tag):
        """本地化 tag -> 简体中文。取不到返回 None。"""
        if not tag:
            return None
        row = self.lc.execute(
            "SELECT Text FROM LocalizedText WHERE Tag=? AND Language='zh_Hans_CN'",
            (tag,),
        ).fetchone()
        if row:
            return row[0]
        # 库里没有就问补丁（DLC 文本常缺失，见 _text_patch 的说明）
        return _text_patch().get(tag)

    def tr(self, tag):
        """本地化 tag -> 文本。中文缺失时退回英文（个别 DLC 的文本没进库）。"""
        if not tag:
            return None
        zh = self.zh(tag)
        if zh:
            return zh
        row = self.lc.execute(
            "SELECT Text FROM LocalizedText WHERE Tag=? AND Language='en_US'",
            (tag,),
        ).fetchone()
        return row[0] if row else None

    def _patch_rows(self, suffix):
        """补丁里以 suffix 结尾的条目 [(Tag, Text), ...]。按后缀缓存。

        搜索类方法（resolve / closest / search / lore）原本只查 LocalizedText 表，
        于是补丁里的 DLC 条目——比如「市政广场」——在这些入口里都搜不到，
        只有反过来（已知 tag 求中文）时用得上。这里把它们一并纳入。
        """
        if suffix not in self._patch_cache:
            self._patch_cache[suffix] = [
                (t, x) for t, x in _text_patch().items()
                if t.startswith("LOC_") and t.endswith(suffix)
            ]
        return self._patch_cache[suffix]

    def zh_or_id(self, tag_or_id):
        """把 tag 或裸 ID 都翻成中文；翻不出就原样返回。

        三级回退，每一级都是踩坑后加的：
          ① `UNIT_SWORDSMAN` 这种裸 ID，库里对应的 tag 是 `LOC_UNIT_SWORDSMAN_NAME`，
             得补前后缀再查一次（最初版本漏了这步，满屏英文 ID）；
          ② `DOMAIN_LAND` 这类枚举值不走上面的约定，查 `ENUM_ZH` 对照表；
          ③ 中文缺失就退回英文；中英都缺则保留原 ID（例：BUILDING_JEBEL_BARKAL，
             那个 DLC 的文本压根没进本地化库）。
        """
        if tag_or_id is None or tag_or_id == "":
            return None
        if not isinstance(tag_or_id, str):
            return tag_or_id
        if tag_or_id.startswith("LOC_"):
            return self.tr(tag_or_id) or tag_or_id
        return (self.tr(f"LOC_{tag_or_id}_NAME")
                or ENUM_ZH.get(tag_or_id)
                or tag_or_id)

    def zh_of_entity(self, kind, entity_id):
        """实体 ID -> 中文名（走该表的 Name 列）。"""
        table, pk_guess, _ = ENTITIES[kind]
        nc = self.name_col(table)
        if not nc:
            return entity_id
        pk = self.pk(table, pk_guess)
        row = self.gc.execute(
            f'SELECT "{nc}" FROM "{table}" WHERE "{pk}"=?', (entity_id,)
        ).fetchone()
        if row and row[0]:
            return self.zh(row[0]) or entity_id
        return entity_id

    # -------------------------------------------------------------- 名称解析

    def resolve(self, name, kind=None):
        """把用户输入（中文名 / 英文名 / 内部 ID）解析成实体。

        返回 [(kind, entity_id, 中文名), ...]；kind 为 None 时搜全部类型。
        """
        kinds = [kind] if kind else list(ENTITIES.keys())
        out, seen = [], set()

        # ① 直接当作内部 ID（UNIT_SWORDSMAN 这种）
        for k in kinds:
            table, pk_guess, _ = ENTITIES[k]
            pk = self.pk(table, pk_guess)
            hit = self.gc.execute(
                f'SELECT "{pk}" FROM "{table}" WHERE "{pk}"=?', (name,)
            ).fetchone()
            if hit:
                key = (k, hit[0])
                if key not in seen:
                    seen.add(key)
                    out.append((k, hit[0], self.zh_of_entity(k, hit[0])))

        # ② 中文（或英文）名精确匹配 -> 得到 tag
        cand_tags = [r[0] for r in self.lc.execute(
            "SELECT DISTINCT Tag FROM LocalizedText "
            "WHERE Language='zh_Hans_CN' AND Text=? AND Tag LIKE 'LOC_%_NAME'",
            (name,),
        )]
        cand_tags += [t for t, x in self._patch_rows("_NAME") if x == name]
        for k in kinds:
            table, pk_guess, _ = ENTITIES[k]
            nc = self.name_col(table)
            if not nc:
                continue
            pk = self.pk(table, pk_guess)
            for t in cand_tags:
                hit = self.gc.execute(
                    f'SELECT "{pk}" FROM "{table}" WHERE "{nc}"=?', (t,)
                ).fetchone()
                if hit:
                    key = (k, hit[0])
                    if key not in seen:
                        seen.add(key)
                        out.append((k, hit[0], self.zh_of_entity(k, hit[0])))

        # ③ 模糊匹配（中文名包含关键字）
        if not out:
            like_tags = [r[0] for r in self.lc.execute(
                "SELECT DISTINCT Tag FROM LocalizedText "
                "WHERE Language='zh_Hans_CN' AND Tag LIKE 'LOC_%_NAME' AND Text LIKE ? "
                "LIMIT 400",
                (f"%{name}%",),
            )]
            like_tags += [t for t, x in self._patch_rows("_NAME") if name in x]
            for t in like_tags:
                k = self._kind_from_tag(t)
                if k is None or k not in kinds:
                    continue
                table, pk_guess, _ = ENTITIES[k]
                nc = self.name_col(table)
                if not nc:
                    continue
                pk = self.pk(table, pk_guess)
                hit = self.gc.execute(
                    f'SELECT "{pk}" FROM "{table}" WHERE "{nc}"=?', (t,)
                ).fetchone()
                if hit:
                    key = (k, hit[0])
                    if key not in seen:
                        seen.add(key)
                        out.append((k, hit[0], self.zh_of_entity(k, hit[0])))
        return out

    @staticmethod
    def _kind_from_tag(tag):
        """LOC_UNIT_SWORDSMAN_NAME -> 'unit'"""
        if not tag.startswith("LOC_") or not tag.endswith("_NAME"):
            return None
        core = tag[4:-5]
        for prefix, kind in TAG_PREFIX_TO_KIND.items():
            if core.startswith(prefix):
                return kind
        return None

    def _exists(self, kind, name_tag):
        """确认这个 Name tag 真的挂在实体表里（过滤掉同名但非实体的 tag）。"""
        table, pk_guess, _ = ENTITIES[kind]
        nc = self.name_col(table)
        if not nc:
            return False
        return self.gc.execute(
            f'SELECT 1 FROM "{table}" WHERE "{nc}"=? LIMIT 1', (name_tag,)
        ).fetchone() is not None

    def closest(self, name, kind=None, limit=5):
        """字符级模糊匹配。

        存在的原因：同一个东西常有多个叫法——库里叫「炼铁术」，
        用户会问「铁器加工」，子串匹配（LIKE %铁器%）找不到。
        这里按"命中字符数"打分，让「炼铁术」因为含「铁」而被捞出来。
        """
        chars = [c for c in name if c.strip()]
        if not chars:
            return []
        # 最多拿 10 个字去 OR。这不是精度取舍，是自保：
        # 每个字是一条 LIKE，全表扫一遍；模型要是把整句话当成名字塞进来
        # （"我接下来该造什么好呢"这种），几十条 LIKE 叠在一起会卡住好几秒。
        # 而且命中 8 个字以上时，候选池早就没有区分度了，多要也没用。
        chars = chars[:10]
        likes = " OR ".join(["Text LIKE ?"] * len(chars))
        rows = self.lc.execute(
            f"SELECT DISTINCT Tag, Text FROM LocalizedText "
            f"WHERE Language='zh_Hans_CN' AND Tag LIKE 'LOC_%_NAME' AND ({likes}) "
            f"LIMIT 2000",
            [f"%{c}%" for c in chars],
        ).fetchall()
        # 补丁条目必须按同样的规则过滤。早期这里是把 text_patch.json 全量倒进来，
        # 于是 0 命中的条目也能占候选名额——用「铁器」查科技时会冒出「医学」「发明」
        # 这种完全不搭边的（它们只是名字短，排在了前面）。
        # ⚠ 本模块定义了公开 API `any()`，内置 any() 已被覆盖，这里只能用 sum()。
        rows = list(rows) + [r for r in self._patch_rows("_NAME")
                            if sum(1 for c in chars if c in r[1])]
        scored = []
        for tag, text in rows:
            k = self._kind_from_tag(tag)
            if k is None or (kind and k != kind):
                continue
            score = sum(1 for c in chars if c in text)
            if score == 0:
                continue
            scored.append((-score, len(text), k, text))
        scored.sort()
        out, seen = [], set()
        for _, _, k, text in scored:
            if text in seen:
                continue
            seen.add(text)
            out.append((k, text))
            if len(out) >= limit:
                break
        return out

    def _scan_names(self, keyword, kind=None, cap=400):
        """子串匹配：中文名里直接包含这个词。"""
        rows = self.lc.execute(
            "SELECT DISTINCT Tag, Text FROM LocalizedText "
            "WHERE Language='zh_Hans_CN' AND Tag LIKE 'LOC_%_NAME' AND Text LIKE ? LIMIT ?",
            (f"%{keyword}%", cap),
        ).fetchall()
        rows = list(rows) + [r for r in self._patch_rows("_NAME") if keyword in r[1]]
        out = []
        for tag, text in rows:
            k = self._kind_from_tag(tag)
            if k is None or (kind and k != kind):
                continue
            if self._exists(k, tag):
                out.append((k, text))
        return out

    def _need_one(self, kind, name):
        """解析出唯一实体；找不到就抛错，并在错误里附上最接近的候选。

        附候选是为了让调用方（尤其模型）能自我纠正，而不是卡死。
        """
        hits = self.resolve(name, kind=kind)
        if not hits:
            alts = self.closest(name, kind=kind, limit=4)
            hint = "；".join(f"「{t}」" for _k, t in alts)
            raise LookupError(
                f"在{ENTITIES[kind][2]}里找不到「{name}」。"
                + (f"库里最接近的是：{hint}。" if hint else "")
                + f" 也可以先 search('{name}') 看看库里都有什么。"
            )
        return hits[0][1]

    # ---------------------------------------------------------------- 详情字段

    def _row(self, table, entity_id, pk_guess=None):
        pk = self.pk(table, pk_guess)
        cs = self.cols(table)
        row = self.gc.execute(f'SELECT * FROM "{table}" WHERE "{pk}"=?', (entity_id,)).fetchone()
        return dict(zip(cs, row)) if row else None

    def _line(self, label, val, unit="", allow_zero=False):
        """输出一行 '**标签**：值'。空值跳过；数值 0 默认也跳过（避免噪声）。"""
        if val is None or val == "":
            return None
        if isinstance(val, (int, float)) and val == 0 and not allow_zero:
            return None
        txt = self.zh_or_id(val) if isinstance(val, str) else val
        return f"- **{label}**：{txt}{unit}"

    def _join(self, items):
        return "\n".join(x for x in items if x)

    def _related(self, table, col, entity_id):
        """从关联表里取一列，筛条件固定是 ResourceType=当前资源。

        表名和列名都是现查现用的：这类关联表在不同版本里叫法会变，
        表不存在就返回空列表，绝不抛异常。
        """
        if not (self.has(table, col) and self.has(table, "ResourceType")):
            return []
        rows = self.gc.execute(
            f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "ResourceType"=?',
            (entity_id,)).fetchall()
        return [self.zh_or_id(x[0]) for x in rows if x[0]]

    def _era_of(self, tech_id, civic_id):
        """从前置科技/市政反推时代。

        `Units` 表**没有** EraType 这一列（原来这里读 r.get("EraType")，
        永远取不到值，所以"时代"那一行从来没出现过）。游戏里单位的时代
        本来就是跟着它的前置科技/市政走的，照这个反推就行。
        """
        for table, col, v in (("Technologies", "TechnologyType", tech_id),
                              ("Civics", "CivicType", civic_id)):
            if not v or not self.has(table, "EraType"):
                continue
            row = self.gc.execute(
                f'SELECT EraType FROM "{table}" WHERE "{col}"=?', (v,)).fetchone()
            if row and row[0]:
                return self.zh_or_id(row[0])
        return None

    def _desc(self, kind, entity_id):
        """先取该表的 Description 列，再退回 LOC_<ID>_DESCRIPTION。输出已清理富文本。"""
        table, pk_guess, _ = ENTITIES[kind]
        t = None
        if self.has(table, "Description"):
            t = self.tr_variant(self._row(table, entity_id, pk_guess).get("Description"))
        if not t:
            t = self.tr_variant(f"LOC_{entity_id}_DESCRIPTION")
        return self.clean_text(t)

    def _is_internal_trait(self, trait_id):
        """过滤 InternalOnly 的特性。

        这类特性是给 AI 和系统用的（例：TRAIT_LEADER_CULTURAL_MAJOR_CIV、
        TRAIT_LEADER_LOW_RELIGIOUS_PREFERENCE），对玩家是纯噪声。
        """
        if not self.has("Traits", "InternalOnly"):
            return False
        r = self.gc.execute(
            "SELECT InternalOnly FROM Traits WHERE TraitType=?", (trait_id,)).fetchone()
        return bool(r and r[0])

    # ------------------------------------------------------------ 公开检索接口

    def search(self, keyword, limit=15, kind=None):
        """全库按关键词搜索实体。逐级放宽：精确 → 子串 → 字符级。"""
        found = {}
        for k, _eid, zh in self.resolve(keyword, kind=kind):
            found.setdefault((k, zh), None)
        if len(found) < limit:
            for k, zh in self._scan_names(keyword, kind):
                found.setdefault((k, zh), None)
        if len(found) < limit:
            for k, zh in self.closest(keyword, kind=kind, limit=limit * 2):
                found.setdefault((k, zh), None)
        if not found:
            return f"库里没有与「{keyword}」相关的条目。"
        items = sorted(found, key=lambda x: (x[1] != keyword, len(x[1])))
        shown = items[:limit]
        lines = [f"含「{keyword}」的条目（{len(shown)} / {len(items)} 条）：", ""]
        for k, zh in shown:
            lines.append(f"- [{ENTITIES[k][2]}] {zh}")
        if len(items) > limit:
            lines.append(f"- ……另有 {len(items) - limit} 条未列出")
        return "\n".join(lines)

    def lore(self, keyword, limit=12):
        """按概念搜索：在全库描述文本里找提到这个词的条目。

        和 `search` 的分工很重要：
          `search` 找的是"名字里含这个词的实体"——用来**找东西**；
          `lore`   找的是"描述里提到这个词的条目"——用来**问机制**。
        例：问「宜居度」，search 一无所获（没有哪个单位叫这名字），
        lore 却能捞出所有提到宜居度的建筑、信条和政策卡。

        只扫 `*_DESCRIPTION`，正好滤掉海量 UI 提示字符串。
        """
        rows = self.lc.execute(
            "SELECT DISTINCT Tag, Text FROM LocalizedText "
            "WHERE Language='zh_Hans_CN' AND Tag LIKE '%_DESCRIPTION' AND Text LIKE ? "
            "LIMIT ?",
            (f"%{keyword}%", max(limit * 8, 60)),
        ).fetchall()
        rows = list(rows) + [r for r in self._patch_rows("_DESCRIPTION")
                             if keyword in r[1]]
        out, seen = [], set()
        for tag, text in rows:
            t = self.clean_text(text)
            if not t or t in seen:
                continue
            base = tag[4:-12]          # 去掉 LOC_ 前缀与 _DESCRIPTION 后缀
            kind = None
            for prefix, k in TAG_PREFIX_TO_KIND.items():
                if base.startswith(prefix):
                    kind = k
                    break
            if kind:
                name, label = self.zh_of_entity(kind, base), ENTITIES[kind][2]
            else:
                name, label = self.zh(f"LOC_{base}_NAME") or base, "文本"
            seen.add(t)
            out.append((label, name, t))
            if len(out) >= limit:
                break
        if not out:
            return (f"没有条目在描述里提到「{keyword}」。\n"
                    f"如果它是某个单位/建筑/科技的名字，改用 search('{keyword}')。")
        lines = [f"描述中提到「{keyword}」的条目：", ""]
        for label, name, t in out:
            snippet = t if len(t) <= 110 else t[:110] + "…"
            lines.append(f"- **[{label}] {name}**：{snippet}")
        return "\n".join(lines)

    # ---- 单位 ----

    def unit(self, name):
        """单位完整档案：数值、前置、升级链、AI 行为、反查谁能升级成它。"""
        uid = self._need_one("unit", name)
        r = self._row("Units", uid, "UnitType")
        zh = self.zh_of_entity("unit", uid)
        out = [f"# {zh}（{uid}）", ""]
        d = self._desc("unit", uid)
        if d:
            out += [f"> {d}", ""]

        stats = [
            self._line("近战攻击", r.get("Combat")),
            self._line("远程攻击", r.get("RangedCombat")),
            self._line("轰炸攻击", r.get("Bombard")),
            self._line("对空攻击", r.get("AntiAirCombat")),
            self._line("射程", r.get("Range")),
            self._line("移动力", r.get("BaseMoves")),
            self._line("视野", r.get("BaseSightRange"), " 格"),
            self._line("造价", r.get("Cost"), " 生产力"),
            self._line("维护费", r.get("Maintenance"), " 金币/回合", allow_zero=True),
            self._line("时代", self._era_of(r.get("PrereqTech"), r.get("PrereqCivic"))),
            self._line("前置科技", r.get("PrereqTech")),
            self._line("前置市政", r.get("PrereqCivic")),
            self._line("战略资源", r.get("StrategicResource")),
            self._line("晋升类别", r.get("PromotionClass")),
            self._line("活动域", r.get("Domain")),
            self._line("编队类别", r.get("FormationClass")),
        ]
        body = self._join(stats)
        if body:
            out += ["## 数值与前置", body, ""]

        if r.get("ZoneOfControl"):
            out.append("- 拥有**控制区（ZOC）**：敌军进入相邻格后无法继续移动")
        if r.get("CanRetreatWhenCaptured") == 0 and r.get("Combat"):
            out.append("- 被击败时**不会撤退**")
        if len(out) and out[-1] != "":
            out.append("")

        # 升级链：顺着追到最远，再列出其他能升级成它的单位
        chain = self.gc.execute(
            "SELECT Unit, UpgradeUnit FROM UnitUpgrades WHERE Unit=? OR UpgradeUnit=?",
            (uid, uid),
        ).fetchall()
        if chain:
            out.append("## 升级链")
            path, cur, seen = [uid], uid, {uid}
            while True:
                nxt = [b for a, b in chain if a == cur and b not in seen]
                if not nxt:
                    break
                cur = nxt[0]
                seen.add(cur)
                path.append(cur)
            out.append(" → ".join(self.zh_of_entity("unit", x) for x in path))
            for a, b in chain:
                if b == uid and a != uid:
                    out.append(f"- 另有「{self.zh_of_entity('unit', a)}」也可升级为它")
            out.append("")

        # AI 行为标签
        if self.has("UnitAiInfos", "UnitType"):
            ais = [x[0] for x in self.gc.execute(
                "SELECT AiType FROM UnitAiInfos WHERE UnitType=?", (uid,))]
            if ais:
                out.append("## AI 行为标签")
                out.append("、".join(ais))
                out.append("")

        # 单位能力
        for tbl, col in (("UnitAbilities", "UnitAbilityType"), ("UnitAbilities", "UnitType")):
            if self.has(tbl, "UnitType"):
                try:
                    ab = self.gc.execute(
                        f'SELECT * FROM "{tbl}" WHERE UnitType=?', (uid,)).fetchall()
                    if ab:
                        cols = self.cols(tbl)
                        out.append("## 单位能力")
                        for row in ab:
                            dd = dict(zip(cols, row))
                            nm = dd.get("UnitAbilityType") or dd.get("AbilityType")
                            out.append(f"- {self.zh_or_id(nm)}（{nm}）")
                        out.append("")
                except Exception:
                    pass
                break

        # 特色归属（Trait 列）
        if self.has("Units", "TraitType") and r.get("TraitType"):
            out.append(f"- 所属特性：{self.zh_or_id(r['TraitType'])}")
        return "\n".join(out).rstrip()

    # ---- 科技 / 市政 ----

    def _techlike(self, kind, name):
        table, pk_guess, label = ENTITIES[kind]
        eid = self._need_one(kind, name)
        r = self._row(table, eid, pk_guess)
        zh = self.zh_of_entity(kind, eid)
        out = [f"# {zh}（{eid}）", ""]
        d = self._desc(kind, eid)
        if d:
            out += [f"> {d}", ""]

        stats = [
            self._line("时代", r.get("EraType")),
            self._line("研究成本", r.get("Cost"), " 科技值"),
            self._line("是否可重复研究", "是" if r.get("Repeatable") else None),
        ]
        body = self._join(stats)
        if body:
            out += ["## 基本", body, ""]

        # 前置
        pt = "TechnologyPrereqs" if kind == "tech" else "CivicPrereqs"
        pcol = "Technology" if kind == "tech" else "Civic"
        pre_col = "PrereqTech" if kind == "tech" else "PrereqCivic"
        if self.has(pt, pcol):
            pres = [x[0] for x in self.gc.execute(
                f'SELECT "{pre_col}" FROM "{pt}" WHERE "{pcol}"=?', (eid,))]
            if pres:
                out.append("## 前置" + label)
                for p in pres:
                    out.append(f"- {self.zh_of_entity(kind, p)}")
                out.append("")

        # 尤里卡 / 鼓舞
        out += self._boost(kind, eid)

        # 解锁什么
        unlocks = []
        for k in ("unit", "building", "district", "improvement", "project"):
            t2, pk2, lbl2 = ENTITIES[k]
            if not self.has(t2, pre_col):
                continue
            ncol = self.name_col(t2)
            q = (f'SELECT "{self.pk(t2, pk2)}" FROM "{t2}" WHERE "{pre_col}"=?')
            for (x,) in self.gc.execute(q, (eid,)):
                unlocks.append(f"- [{lbl2}] {self.zh_of_entity(k, x)}")
        if unlocks:
            out.append("## 解锁内容")
            out += unlocks
            out.append("")

        # 成本反查：什么科技以此为前置
        if self.has(pt, pcol):
            nxt = [x[0] for x in self.gc.execute(
                f'SELECT "{pcol}" FROM "{pt}" WHERE "{pre_col}"=?', (eid,))]
            if nxt:
                out.append("## 后续" + label)
                out.append("、".join(self.zh_of_entity(kind, x) for x in nxt))
                out.append("")
        return "\n".join(out).rstrip()

    def _boost(self, kind, eid):
        """尤里卡（科技）/ 鼓舞（市政）的触发条件。

        取文本的顺序不能反：`TriggerDescription` 才是**触发条件**
        （"建造1座铁矿。"），`TriggerLongDescription` 是风味文案
        （"稳定的铁资源能让您铸造更好的武器…"）。
        原先优先取长描述，于是"尤里卡条件"下面写的全是背景故事，
        对玩家毫无用处还容易误导。
        """
        if not self.has("Boosts", "BoostID"):
            return []
        col = "TechnologyType" if kind == "tech" else "CivicType"
        rows = self.gc.execute(f'SELECT * FROM Boosts WHERE "{col}"=?', (eid,)).fetchall()
        if not rows:
            return []
        cs = self.cols("Boosts")
        out = ["## 尤里卡（加速条件）" if kind == "tech" else "## 鼓舞（加速条件）"]
        for row in rows:
            d = dict(zip(cs, row))
            txt = self.clean_text(
                self.zh(d.get("TriggerDescription"))
                or self.zh(d.get("TriggerLongDescription"))
                or self.zh(d.get("TriggerId")))
            if txt:
                out.append(f"- {txt}")
            else:
                bits = []
                for key, label in (("Unit1Type", "单位"), ("BuildingType", "建筑"),
                                   ("DistrictType", "区域"), ("ImprovementType", "改良"),
                                   ("ResourceType", "资源"), ("BoostingTechType", "科技"),
                                   ("BoostingCivicType", "市政")):
                    v = d.get(key)
                    if v:
                        bits.append(f"{label} {self.zh_or_id(v)}")
                n = d.get("NumItems")
                if n:
                    bits.append(f"×{n}")
                out.append("- " + ("；".join(bits) if bits else "（条件见游戏内提示）"))
        out.append("")
        return out

    def tech(self, name):
        """科技详情：成本、时代、前置、尤里卡加速条件、解锁内容。"""
        return self._techlike("tech", name)

    def civic(self, name):
        """市政详情：成本、时代、前置、鼓舞加速条件、解锁内容。"""
        return self._techlike("civic", name)

    # ---- 建筑 / 区域 / 改良 ----

    @staticmethod
    def clean_text(txt, num=None):
        """清掉游戏富文本标记，露出干净的句子。

        库里的描述是**界面模板**，直接拿来会得到这种东西：
            '+{1_num} [ICON_Science] 科技值来自相邻山脉{1_Num : plural 1?单元格; other?单元格;}。'
        处理顺序有讲究：
          ① 删图标标记 `[ICON_XXX]`；
          ② 复数占位符取 `other?` 后的实词（`…; other?单元格;}` -> `单元格`），
             这一步不能省——否则"来自相邻"后面会缺名词，句子残废；
          ③ `{1_num}` 换成真实数值；
          ④ 剩下的占位符一律丢弃。
        """
        if not txt:
            return None
        t = re.sub(r"\[ICON_[^\]]*\]", "", txt)
        t = re.sub(r"\[NEWLINE\]", " ", t)
        t = re.sub(r"\{[^{}]*?other\?([^;}]+)[^}]*\}", r"\1", t)
        if num is not None:
            t = re.sub(r"\{1_[nN]um\}", str(num), t)
        t = re.sub(r"\{[^{}]*\}", "", t)
        # 模板写成 "+{1_num}"，数值为负时会拼出 "+-1"（如朝鲜书院的 -1 科技），去掉多余加号
        t = t.replace("+-", "-")
        t = re.sub(r"\s+", " ", t).strip()
        return t or None

    def tr_variant(self, tag):
        """先精确查；查不到就去掉资料片后缀再查。

        库里的文本常有两个版本：带 `_EXPANSION2` 的资料片版和通用版。
        有些条目只发布了其中一个——例如秦始皇的能力，
        `LOC_TRAIT_LEADER_FIRST_EMPEROR_EXPANSION2_DESCRIPTION` 查不到，
        但去掉 `_EXPANSION2` 后的 `..._DESCRIPTION` 是有的。
        """
        if not tag:
            return None
        t = self.tr(tag)
        if t:
            return t
        for suf in ("_EXPANSION2", "_EXPANSION1", "_XP2", "_XP1"):
            if suf in tag:
                t = self.tr(tag.replace(suf, ""))
                if t:
                    return t
        return None

    @staticmethod
    def _signed(v):
        """1 -> '+1'，-1 -> '-1'（直接拼 f"+{v}" 会得到 '+-1'）。"""
        f = float(v)
        s = str(int(f)) if f == int(f) else f"{f:g}"
        return s if s.startswith("-") else "+" + s

    @staticmethod
    def _resclass_zh(cls):
        """资源类 ID -> 中文（战略资源 / 奢侈品资源 / 加成资源）。"""
        if not cls or cls == "NO_RESOURCECLASS":
            return None
        table = {"RESOURCECLASS_STRATEGIC": "战略资源",
                 "RESOURCECLASS_LUXURY": "奢侈品资源",
                 "RESOURCECLASS_BONUS": "加成资源"}
        return table.get(cls)

    def _adjacency(self, eid):
        """区域的相邻加成。

        链路：`District_Adjacencies`(DistrictType, YieldChangeId)
              → `Adjacency_YieldChanges`。
        后者自带 `Description` 本地化列，就是官方中文，比自己拼条件可靠；
        但它是界面模板，必须过 `clean_text()`，而且要**去重**——
        学院靠山那条其实有 5 行（对应 5 种山的地形类型），文案完全一样。

        特别当心 `TilesRequired`：它是"要凑够几格才给这 N 点加成"。
        =1 时是每格 N 点；=2 时是每格只有 N/2（凑两格才给 N）。
        官方描述文案里**没有**这个信息（只写 `+{1_num}`），漏掉就会把数值说大一倍——
        圣地的"相邻区域+1"其实是"每 2 个区域+1"，学院/商业中心/工业区同理。
        """
        if not (self.has("District_Adjacencies", "DistrictType")
                and self.has("Adjacency_YieldChanges", "ID")):
            return []
        # 资源类列不一定存在，缺了就补 NULL 占位，保持取列数一致
        has_rc = self.has("Adjacency_YieldChanges", "AdjacentResourceClass")
        rows = self.gc.execute(
            "SELECT a.Description, a.YieldType, a.YieldChange, a.TilesRequired,"
            "       a.AdjacentTerrain, a.AdjacentFeature, a.AdjacentDistrict,"
            "       a.AdjacentImprovement, a.AdjacentResource, a.AdjacentRiver,"
            "       a.AdjacentWonder, a.AdjacentNaturalWonder, a.AdjacentSeaResource,"
            "       a.OtherDistrictAdjacent" + (", a.AdjacentResourceClass" if has_rc else ", NULL")
            + "  FROM District_Adjacencies d"
            "  JOIN Adjacency_YieldChanges a ON a.ID = d.YieldChangeId"
            " WHERE d.DistrictType=?",
            (eid,),
        ).fetchall()
        if not rows:
            return []
        lines, seen = [], set()
        for (desc, ytype, ychg, tiles, terr, feat, dist, imp, res,
             river, wonder, nwonder, seares, other, resclass) in rows:
            yield_zh = self.zh_or_id(ytype)
            n_req = tiles or 1
            # TilesRequired>1 时必须显式写出"每 N 格才 +X"，否则模型会读成每格 +X
            every = (f"每 {n_req} 个 {self._signed(ychg)}，即每个 {self._signed(ychg / n_req)}"
                     if n_req > 1 else "")
            txt = self.clean_text(self.zh(desc) if desc else None, ychg)
            if txt:
                # 描述里已带数值（如"+1 科技值来自相邻山脉"）就别再叠前缀，否则读起来是
                # "科技值 +1：+1 科技值来自相邻山脉。"
                item = f"- {txt}" if str(ychg) in txt else f"- {yield_zh} {self._signed(ychg)}：{txt}"
                if every:
                    item += f"（{every}）"
                key = ("d", txt, ychg)
            else:
                conds = [self.zh_or_id(v) for v in (terr, feat, dist, imp, res) if v]
                for v, label in ((river, "河流"), (wonder, "奇观"),
                                 (nwonder, "自然奇观"), (seares, "海洋资源"),
                                 (other, "相邻区域")):
                    if v:
                        conds.append(label)
                rc = self._resclass_zh(resclass)
                if rc:
                    conds.append(rc)
                if conds:
                    cond = "、".join(conds)
                    item = f"- {yield_zh} {self._signed(ychg)}：每 {n_req} 个{cond}"
                    if every:
                        item += f"（即每个 {self._signed(ychg / n_req)}）"
                else:
                    # 一个相邻条件都没有 → 是固定加成，不是相邻加成
                    # （如朝鲜书院的 +4 科技，跟周围地形无关）
                    cond = ""
                    item = f"- {yield_zh} {self._signed(ychg)}（固定加成，与相邻地块无关）"
                key = ("r", cond, yield_zh, ychg)
            if key in seen:
                continue
            seen.add(key)
            lines.append(item)
        if not lines:
            return []
        return ["## 相邻加成", *lines, ""]

    def _place(self, kind, name):
        table, pk_guess, label = ENTITIES[kind]
        eid = self._need_one(kind, name)
        r = self._row(table, eid, pk_guess)
        zh = self.zh_of_entity(kind, eid)
        out = [f"# {zh}（{eid}）", ""]
        d = self._desc(kind, eid)
        if d:
            out += [f"> {d}", ""]

        stats = [
            self._line("造价", r.get("Cost"), " 生产力"),
            self._line("维护费", r.get("Maintenance"), " 金币/回合"),
            self._line("时代", r.get("EraType")),
            self._line("前置科技", r.get("PrereqTech")),
            self._line("前置市政", r.get("PrereqCivic")),
            self._line("所在区域", r.get("PrereqDistrict")),
            self._line("住房", r.get("Housing")),
            self._line("娱乐设施", r.get("Entertainment")),
            self._line("宜居度", r.get("Amenities")),
            self._line("公民槽位", r.get("CitizenSlots")),
            self._line("是否奇观", "是" if r.get("IsWonder") else None),
            self._line("每城限建", r.get("OnePerCity") and "1" or None),
        ]
        body = self._join(stats)
        if body:
            out += ["## 数值与前置", body, ""]

        # 相邻要求
        adj = []
        for col, label2 in (("RequiresRiver", "需临河"), ("RequiresAdjacentRiver", "需相邻河流"),
                            ("AdjacentToMountain", "需相邻山脉"), ("AdjacentToLand", "需相邻陆地"),
                            ("Coast", "需沿海"), ("MustBeLake", "需湖泊"),
                            ("RequiresPlacement", "需手动选址")):
            if r.get(col) in (1, "1"):
                adj.append(f"- {label2}")
        if adj:
            out += ["## 位置要求", *adj, ""]

        # 产出加成
        for tbl, key in (("Building_YieldChanges", "BuildingType"),
                         ("District_YieldChanges", "DistrictType"),
                         ("Improvement_YieldChanges", "ImprovementType")):
            if self.has(tbl, key):
                rows = self.gc.execute(f'SELECT * FROM "{tbl}" WHERE "{key}"=?', (eid,)).fetchall()
                if rows:
                    cs = self.cols(tbl)
                    out.append("## 固定产出")
                    for row in rows:
                        dd = dict(zip(cs, row))
                        y = self.zh_or_id(dd.get("YieldType"))
                        out.append(f"- {y} +{dd.get('YieldChange')}")
                    out.append("")
                break

        # 相邻加成（只有区域有这个概念）
        if kind == "district":
            out += self._adjacency(eid)

        if self.has(table, "TraitType") and r.get("TraitType"):
            out.append(f"- 特色归属：{self.zh_or_id(r['TraitType'])}")
        return "\n".join(out).rstrip()

    def building(self, name):
        """建筑详情：造价、前置、所在区域、住房/宜居、产出与相邻加成。"""
        return self._place("building", name)

    def district(self, name):
        """区域详情：造价、前置、位置要求、住房/宜居、相邻加成。"""
        return self._place("district", name)

    def improvement(self, name):
        """改良设施详情。"""
        return self._place("improvement", name)

    def resource(self, name):
        """资源详情：类别（战略/奢侈/加成）、产出、开采方式、能出现在什么地形。"""
        eid = self._need_one("resource", name)
        r = self._row("Resources", eid, "ResourceType")
        zh = self.zh_of_entity("resource", eid)
        out = [f"# {zh}（{eid}）", ""]
        d = self._desc("resource", eid)
        if d:
            out += [f"> {d}", ""]

        # 这几项原来读的是 Resources 表里**并不存在**的列
        # （Strategic / Luxury / Bonus / YieldType / ImprovementType），
        # 于是那五行永远是 None，输出里从来没出现过。真实数据在别的表：
        # 类别在 ResourceClassType，"产出多少""怎么开采"都是关联表。
        yields = []
        for ytype, ychg in self.gc.execute(
                "SELECT YieldType, YieldChange FROM Resource_YieldChanges "
                "WHERE ResourceType=?", (eid,)):
            if ychg:
                yields.append(f"{self.zh_or_id(ytype)} {self._signed(ychg)}")
        ways = self._related("Improvement_ValidResources", "ImprovementType", eid)
        terrains = self._related("Resource_ValidTerrains", "TerrainType", eid)
        feats = self._related("Resource_ValidFeatures", "FeatureType", eid)
        stats = self._join([
            self._line("资源类别", r.get("ResourceClassType")),
            self._line("地块产出", "、".join(yields) or None),
            self._line("开采方式", "、".join(ways) or None),
            self._line("所在地形", "、".join(terrains) or None),
            self._line("所在植被", "、".join(feats) or None),
            self._line("前置科技", r.get("PrereqTech")),
            self._line("宜居度", r.get("Happiness")),
        ])
        if stats:
            out += ["## 基本", stats, ""]
        return "\n".join(out).rstrip()

    # ---- 政策卡 / 政体 ----

    def _modifier_texts(self, mod_ids):
        """把 ModifierId 列表翻译成人话（优先取库里带 LOC 的描述参数）。"""
        out, seen = [], set()
        for mid in mod_ids:
            if mid in seen:
                continue
            seen.add(mid)
            args = self.gc.execute(
                "SELECT Name, Value FROM ModifierArguments WHERE ModifierId=?", (mid,)
            ).fetchall()
            described = False
            for name, val in args:
                if isinstance(val, str) and val.startswith("LOC_"):
                    t = self.zh(val)
                    if t:
                        out.append(f"- {t}")
                        described = True
                        break
            if described:
                continue
            nums = {n: v for n, v in args}
            mtype = self.gc.execute(
                "SELECT ModifierType FROM Modifiers WHERE ModifierId=?", (mid,)
            ).fetchone()
            label = mid
            if mtype:
                label = f"{mid}（{mtype[0].replace('MODIFIER_', '').lower()}）"
            detail = "，".join(f"{k}={v}" for k, v in list(nums.items())[:5])
            out.append(f"- {label}" + (f"：{detail}" if detail else ""))
        return out

    def policy(self, name):
        """政策卡效果：生效的修正条目（含中文描述）。"""
        eid = self._need_one("policy", name)
        r = self._row("Policies", eid, "PolicyType")
        zh = self.zh_of_entity("policy", eid)
        out = [f"# {zh}（{eid}）", ""]
        d = self._desc("policy", eid)
        if d:
            out += [f"> {d}", ""]
        meta = self._join([
            # Policies 表没有 SlotType/EraType 这两列（原来读它们，
            # 那两行永远不出现）。槽位就叫 GovernmentSlotType。
            self._line("槽位类型", r.get("GovernmentSlotType")),
            self._line("前置市政", r.get("PrereqCivic")),
            self._line("前置科技", r.get("PrereqTech")),
        ])
        if meta:
            out += ["## 基本", meta, ""]
        mods = [x[0] for x in self.gc.execute(
            "SELECT ModifierId FROM PolicyModifiers WHERE PolicyType=?", (eid,))]
        if mods:
            out.append("## 生效修正")
            out += self._modifier_texts(mods)
            out.append("")
        return "\n".join(out).rstrip()

    def government(self, name):
        """政体详情：槽位配置、固有加成。"""
        eid = self._need_one("government", name)
        r = self._row("Governments", eid, "GovernmentType")
        zh = self.zh_of_entity("government", eid)
        out = [f"# {zh}（{eid}）", ""]
        d = self._desc("government", eid)
        if d:
            out += [f"> {d}", ""]
        slots = self.gc.execute(
            "SELECT GovernmentSlotType, NumSlots FROM Government_SlotCounts WHERE GovernmentType=?",
            (eid,),
        ).fetchall()
        if slots:
            out.append("## 槽位")
            for st, n in slots:
                out.append(f"- {self.zh_or_id(st)} × {n}")
            out.append("")
        mods = [x[0] for x in self.gc.execute(
            "SELECT ModifierId FROM GovernmentModifiers WHERE GovernmentType=?", (eid,))]
        if mods:
            out.append("## 固有加成")
            out += self._modifier_texts(mods)
            out.append("")
        return "\n".join(out).rstrip()

    # ---- 文明 / 领袖 ----

    def civilization(self, name):
        """文明详情：特有能力、特色单位/建筑/区域/改良、可选领袖。"""
        eid = self._need_one("civilization", name)
        zh = self.zh_of_entity("civilization", eid)
        out = [f"# {zh}（{eid}）", ""]

        # 特性
        traits = [x[0] for x in self.gc.execute(
            "SELECT TraitType FROM CivilizationTraits WHERE CivilizationType=?", (eid,))]
        if traits:
            out.append("## 文明特性")
            for t in traits:
                if self._is_internal_trait(t):
                    continue
                tname = self._trait_label(t)
                if not tname:
                    continue
                tdesc = self._desc("trait", t) or ""
                if tdesc:
                    out.append(f"- **{tname}**：{tdesc}")
                else:
                    out.append(f"- **{tname}**")
            out.append("")

        # 领袖
        leaders = [x[0] for x in self.gc.execute(
            "SELECT LeaderType FROM CivilizationLeaders WHERE CivilizationType=?", (eid,))]
        if leaders:
            out.append("## 可用领袖")
            for l in leaders:
                out.append(f"- {self.zh_of_entity('leader', l)}")
            out.append("")

        # 特色内容：靠 TraitType 反查
        if traits:
            marks = ",".join("?" * len(traits))
            for kind, col in (("unit", "TraitType"), ("building", "TraitType"),
                              ("district", "TraitType"), ("improvement", "TraitType")):
                table, pk_guess, label = ENTITIES[kind]
                if not self.has(table, col):
                    continue
                pk = self.pk(table, pk_guess)
                rows = self.gc.execute(
                    f'SELECT "{pk}" FROM "{table}" WHERE "{col}" IN ({marks})', traits
                ).fetchall()
                if rows:
                    out.append(f"## 特色{label}")
                    for (x,) in rows:
                        out.append(f"- {self.zh_of_entity(kind, x)}（{x}）")
                    out.append("")
        return "\n".join(out).rstrip()

    def leader(self, name):
        """领袖详情：领袖特性、议程。"""
        eid = self._need_one("leader", name)
        zh = self.zh_of_entity("leader", eid)
        out = [f"# {zh}（{eid}）", ""]
        traits = [x[0] for x in self.gc.execute(
            "SELECT TraitType FROM LeaderTraits WHERE LeaderType=?", (eid,))]
        if traits:
            out.append("## 领袖特性")
            for t in traits:
                if self._is_internal_trait(t):
                    continue
                tname = self._trait_label(t)
                if not tname:
                    continue
                tdesc = self._desc("trait", t) or ""
                out.append(f"- **{tname}**：{tdesc}" if tdesc else f"- **{tname}**")
            out.append("")
        # 议程：关联表是 `HistoricalAgendas`，不是名字看起来更对的那个 `LeaderAgendas`
        rows = []
        for tbl, key in (("HistoricalAgendas", "LeaderType"),
                         ("AgendaPreferredLeaders", "LeaderType")):
            if self.has(tbl, key):
                rows = self.gc.execute(
                    f'SELECT AgendaType FROM "{tbl}" WHERE "{key}"=?', (eid,)).fetchall()
                if rows:
                    break
        if rows:
            out.append("## 议程")
            for (aid,) in rows:
                a = self._row("Agendas", aid, "AgendaType") or {}
                an = self.zh(a.get("Name")) or self.zh(f"LOC_{aid}_NAME") or aid
                ad = self.clean_text(self.zh(a.get("Description"))
                                     or self.zh(f"LOC_{aid}_DESCRIPTION"))
                out.append(f"- **{an}**：{ad}" if ad else f"- **{an}**")
            out.append("")
        return "\n".join(out).rstrip()

    # ---- 通用分发 ----

    def any(self, name):
        """不确定类型时用：先搜，再对唯一命中的实体给予详情。"""
        hits = self.resolve(name)
        if not hits:
            return f"没有找到「{name}」。"
        if len(hits) > 1:
            return self.search(name)

        kind, eid, zh = hits[0]
        try:
            return getattr(self, kind)(eid)
        except Exception:
            return self.search(name)

    # ---- 对比 ----

    # 领袖/文明的特性挂在不同的关联表里
    _TRAIT_LINK = {"leader": ("LeaderTraits", "LeaderType"),
                   "civilization": ("CivilizationTraits", "CivilizationType")}

    def _trait_label(self, trait_id):
        """特性的显示名。翻不出中文就返回 None —— 调用方应当跳过它。

        有些 TraitType 压根没有中文名（例：TRAIT_LEADER_UNIT_AMERICAN_ROUGH_RIDER，
        它其实是"这个特色单位属于哪个领袖"的归属标记，不是给玩家看的特性）。
        这类直接写裸英文 ID 给玩家看是纯噪音。
        """
        name = self.zh_of_entity("trait", trait_id)
        if not name or name == trait_id:
            return None
        return name

    def _trait_lines(self, kind, eid):
        """领袖/文明的特性清单（对比时用）。

        ⚠ 必须先把查询结果 fetchall 出来再遍历，不能直接
        `for x in self.gc.execute(...)`：循环体里还要查别的表
        （_is_internal_trait / zh_of_entity / _desc 都会用 self.gc），
        而**同一个游标上再执行一次查询会把外层的结果集冲掉**——
        表现是循环跑到一半就结束，前面被 continue 掉的条目直接把整段吃空。
        本模块里所有遍历游标的地方都按这个写法来，别偷这一行。
        """
        link = self._TRAIT_LINK.get(kind)
        if not link or not self.has(*link):
            return []
        tbl, col = link
        rows = self.gc.execute(
            f'SELECT TraitType FROM "{tbl}" WHERE "{col}"=?', (eid,)).fetchall()
        out = []
        for (t,) in rows:
            if self._is_internal_trait(t):
                continue
            name = self._trait_label(t)
            if not name:
                continue
            d = self._desc("trait", t)
            out.append(f"- **{name}**：{d}" if d else f"- **{name}**")
        return out

    def _cell(self, v):
        """对比表里的一个格子。

        None/空字符串统一显示成「—」而不是留白——留白的话整行看不出是缺项。
        布尔值要单独判：`str(True)` 会输出英文的 "True"。
        """
        if v is None or v == "":
            return "—"
        if isinstance(v, bool):
            return "是" if v else "否"
        if isinstance(v, str):
            return str(self.zh_or_id(v))
        return str(v)

    def compare(self, names, kind):
        """并排对比同类型的多个实体。"""
        if kind not in ENTITIES:
            return ("未知的类型「%s」。可用：" % kind) + "、".join(ENTITIES.keys())

        items, missing = [], []
        for n in names:
            try:
                eid = self._need_one(kind, n)
            except LookupError:
                missing.append(n)
                continue
            table, pk_guess, _label = ENTITIES[kind]
            items.append((self.zh_of_entity(kind, eid), eid,
                          self._row(table, eid, pk_guess)))

        if not items:
            return ("没有可对比的条目。"
                    + (f"（找不到：{'、'.join(missing)}）" if missing else ""))

        out = [f"# {ENTITIES[kind][2]}对比", ""]
        # 名字没解析出来时要说一声。原来只是默默少一列，
        # 模型会以为自己要的东西已经在表里了。
        if missing:
            out += [f"（找不到这几项，已跳过：{'、'.join(missing)}）", ""]

        fields = COMPARE_FIELDS.get(kind) or []
        if fields:
            out.append("| 项目 | " + " | ".join(x[0] for x in items) + " |")
            out.append("|---" * (len(items) + 1) + "|")
            for f in fields:
                cells = [self._cell(r.get(f)) for _zh, _eid, r in items]
                # 所有条目这一项都缺、或者都是 0 → 整行删掉，别占地方。
                # 0 在这里等于"没有这项能力"（近战单位的射程、农场的魅力影响）。
                if all(c == "—" or c == "0" for c in cells):
                    continue
                out.append(f"| {FIELD_ZH.get(f, f)} | " + " | ".join(cells) + " |")
            return "\n".join(out)

        # 没有数值可比的类型（领袖 / 文明 / 信条等）：差别全在文字里。
        for zh, eid, _r in items:
            out.append(f"## {zh}")
            d = self._desc(kind, eid)
            lines = self._trait_lines(kind, eid)
            if d:
                out.append(d)
            elif not lines:
                out.append("（库里没有文字描述，可以改用 kb_leader / kb_civilization 查它的特性）")
            out.extend(lines)
            out.append("")
        return "\n".join(out).rstrip()

    # ---- 列名单（给模型"看菜单"）----

    def list_names(self, kind, limit=400, per_line=6):
        """列出某类型下【全部条目的名字】，只给名字、不给详情。

        存在的原因：玩家会说俗称（「奶妈」其实是医疗兵）。字符匹配救不了
        （一个共同字都没有），语义向量模型也救不了（实测过：它把「奶妈」理解成
        哺乳，给出的候选是「乳化可乐」「哺育世界」）。但**把整份名单摊给模型看**
        就能解决——模型懂「奶妈 = 治疗单位」，它只是不知道游戏里管这叫「医疗兵」。

        各类型条数都不多（区域 30、科技 77、建筑 128、单位 133、伟人 178），
        一份名单几百 token，比来回猜词便宜。

        也用于回答「有哪些远程单位」这类**列举型**问题——那种问题换什么关键词
        都搜不出来，因为答案是一份名单而不是一个条目。
        """
        if kind not in ENTITIES:
            return ("未知的类型「%s」。可用：" % kind) + "、".join(ENTITIES.keys())
        table, pk_guess, label = ENTITIES[kind]
        nc = self.name_col(table)
        if not nc:
            return f"{label}没有名字列，无法列举。"
        rows = self.gc.execute(f'SELECT DISTINCT "{nc}" FROM "{table}"').fetchall()
        names = []
        for (tag,) in rows:
            if not tag:
                continue
            # 特性表里绝大多数是内部机制标记（如 Minel_HalfProduction），
            # 列出来只会淹没有效信息。
            if kind == "trait" and self._is_internal_trait(tag):
                continue
            zh = self.zh_or_id(tag)
            # 没翻出中文的（zh_or_id 会原样返回 LOC_xxx）列出来只是噪音——
            # 模型看到裸英文 ID 也没法跟玩家的说法对上，还白占预算。
            if zh and not str(zh).startswith("LOC_"):
                names.append(zh)
        names = sorted(set(names))
        if not names:
            return f"{label}：库里没有条目。"
        total = len(names)
        shown = names[:limit]
        out = [f"{label}（共 {total} 个，这里列出名字，要看详情请用对应的工具）：", ""]
        for i in range(0, len(shown), per_line):
            out.append("、".join(shown[i:i + per_line]))
        if total > limit:
            out.append(f"……另有 {total - limit} 个未列出")
        return "\n".join(out)


# ------------------------------------------------------------------ 单例与接口

_KB = None


def kb():
    global _KB
    if _KB is None:
        _KB = Civ6KB()
    return _KB


def list_names(kind, limit=400):
    """列出某类型下的全部名字。模型认不出俗称时，让它"看菜单"。"""
    return kb().list_names(kind, limit=limit)


def close_thread_conn():
    """释放当前线程持有的数据库连接。

    浮层每问完一个问题调一次：那个工作线程马上就结束了，连接没必要多留。
    没建过连接的线程调它是空操作，随便调。
    """
    if _KB is not None:
        _KB.close()


def search(keyword, limit=15):
    """全库搜索关键词，返回条目清单。用于不知道确切名字时。"""
    return kb().search(keyword, limit=limit)


def lore(keyword, limit=12):
    """按概念搜索描述文本。

    用于问机制而不是找具体东西，例如「宜居度」「忠诚度」「时代分数」。
    """
    return kb().lore(keyword, limit=limit)


def unit(name):
    """查一个单位的完整档案：数值、前置科技、升级链、AI 行为、单位能力。"""
    return kb().unit(name)


def tech(name):
    """查一个科技：研究成本、前置科技、尤里卡加速条件、解锁内容。"""
    return kb().tech(name)


def civic(name):
    """查一个市政：研究成本、前置市政、鼓舞加速条件、解锁内容。"""
    return kb().civic(name)


def building(name):
    """查一个建筑：造价、前置、所在区域、住房/宜居、产出加成。"""
    return kb().building(name)


def district(name):
    """查一个区域：造价、前置、位置要求、相邻加成。"""
    return kb().district(name)


def improvement(name):
    """查一个改良设施。"""
    return kb().improvement(name)


def resource(name):
    """查一种资源：战略/奢侈/加成类别、产出、开采方式。"""
    return kb().resource(name)


def policy(name):
    """查一张政策卡的实际生效效果。"""
    return kb().policy(name)


def government(name):
    """查一个政体：槽位配置与固有加成。"""
    return kb().government(name)


def civilization(name):
    """查一个文明：特有能力、特色单位/建筑/区域/改良、可选领袖。"""
    return kb().civilization(name)


def leader(name):
    """查一个领袖：领袖特性与议程。"""
    return kb().leader(name)


def any(name):
    """不确定类型时用：自动识别并给出详情。"""
    return kb().any(name)


def compare(names, kind):
    """并排对比同类型的多个实体，names 用逗号分隔。"""
    return kb().compare([x.strip() for x in names.split(",") if x.strip()], kind)


# ------------------------------------------------------------------ 命令行入口

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="文明6 权威规则库检索（离线、只读、取自游戏自身缓存）")
    ap.add_argument("action",
                    choices=["search", "lore", "unit", "tech", "civic", "building",
                             "district", "improvement", "resource", "policy",
                             "government", "civilization", "leader", "any",
                             "compare", "tables", "cache"],
                    help="要执行的动作")
    ap.add_argument("arg", nargs="?", help="名称或关键词（compare 用逗号分隔）")
    ap.add_argument("--kind", default="unit", help="compare 时的类型，默认 unit")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（供程序调用）")
    args = ap.parse_args(argv)

    if args.action == "cache":
        print(find_cache_dir())
        return 0

    if args.action == "tables":
        total = kb().gc.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        print(f"规则库共 {total} 张表")
        return 0

    if not args.arg:
        ap.error(f"动作 {args.action} 需要一个名称参数")

    fn = globals()[args.action]
    if args.action == "compare":
        result = fn(args.arg, args.kind)
    elif args.action == "search":
        result = fn(args.arg)
    else:
        result = fn(args.arg)

    if args.json:
        print(json.dumps({"ok": True, "action": args.action,
                          "arg": args.arg, "result": result},
                         ensure_ascii=False, indent=2))
    else:
        print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
