"""从游戏安装目录的翻译 XML 里，把本地化库缺的文本捞出来。

背景
----
游戏运行时生成的 `DebugLocalization.sqlite` 并不完整：部分资料片/DLC 的文本
（例如「市政广场」「棉花堡」）没被编译进去，于是规则库只能吐出裸的英文 ID。
模型看到 `DISTRICT_GOVERNMENT` 不知道那是什么，就会把它跳过——
实测已经导致「圣地相邻加成」漏答两条。

原始文本其实躺在游戏目录的 `*_Translations_*.xml` 里（<Replace Tag=... Language=...>）。
这个脚本把它们抽出来，和数据库比对，只留下库中缺失的部分，
写成 `text_patch.json` 供 kb.py 兜底。

用法
----
· agent.py 每次启动时会**自动**调用 `rebuild(silent=True)` 刷新一遍（实测约 0.9 秒）。
  游戏更新或装了新 DLC 后，补丁会自动跟着变新，不用手动做什么。
· 想单独看过程或排查问题：`python build_text_patch.py`
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET

import kb
import paths

HERE = paths.app_dir()          # 打包后 __file__ 不可用，见 paths.py
OUT_PATH = os.path.join(HERE, "text_patch.json")

# 只保留这几类 tag——它们才是"实体名称/说明/引言"，是问答真正用得到的。
# 不设这个白名单的话，会把上千条界面文案（按钮、提示、菜单）也一起卷进来。
KEEP_SUFFIX = ("_NAME", "_DESCRIPTION", "_QUOTE")

GAME_CANDIDATES = [
    r"D:\Steam\steamapps\common\Sid Meier's Civilization VI",
    r"E:\Steam\steamapps\common\Sid Meier's Civilization VI",
    r"F:\Steam\steamapps\common\Sid Meier's Civilization VI",
    r"C:\Program Files (x86)\Steam\steamapps\common\Sid Meier's Civilization VI",
    r"C:\Program Files\Steam\steamapps\common\Sid Meier's Civilization VI",
]
STEAM_ROOTS = [r"D:\Steam", r"E:\Steam", r"C:\Program Files (x86)\Steam",
               r"C:\Program Files\Steam"]


def find_game_dir():
    """定位游戏安装目录。找不到返回 None。"""
    env = os.environ.get("CIV6_DIR")
    if env and os.path.isdir(os.path.join(env, "Base", "Assets")):
        return env

    def ok(p):
        return p and os.path.isdir(os.path.join(p, "Base", "Assets"))

    for p in GAME_CANDIDATES:
        if ok(p):
            return p

    # 交给 Steam 自己的库清单：一台机器可能有好几个 Steam 库目录
    for root in STEAM_ROOTS:
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        if not os.path.exists(vdf):
            continue
        try:
            raw = open(vdf, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for m in re.finditer(r'"path"\s+"([^"]+)"', raw):
            lib = m.group(1).replace("\\\\", "\\")
            p = os.path.join(lib, "steamapps", "common", "Sid Meier's Civilization VI")
            if ok(p):
                return p
    return None


def search_roots(root):
    """只在这几个地方找文本——游戏目录里还躺着几十 GB 美术资源，全盘遍历太慢。"""
    bases = [os.path.join(root, "Base", "Assets", "Text"),
             os.path.join(root, "Base", "Assets", "UI")]
    dlc = os.path.join(root, "DLC")
    if os.path.isdir(dlc):
        for name in os.listdir(dlc):
            for sub in ("Text", "Config"):
                p = os.path.join(dlc, name, sub)
                if os.path.isdir(p):
                    bases.append(p)
    return bases


def iter_text_files(root):
    """找出所有可能含中文文本的 XML。

    两种存放格式都要覆盖：
      1. 多语言合一：文件名带 Translations，语言写在 Language 属性里
      2. 目录即语言：路径里含 zh_Hans_CN
    """
    for base in search_roots(root):
        for dirpath, _dirs, files in os.walk(base):
            in_zh_dir = "zh_Hans_CN" in dirpath
            for fn in files:
                if not fn.lower().endswith(".xml"):
                    continue
                if "translation" in fn.lower() or in_zh_dir:
                    yield os.path.join(dirpath, fn)


def collect_chinese(root):
    """扫出所有 tag -> 简体中文。返回 (映射, 文件数, 解析失败数)。"""
    found = {}
    n_files = n_bad = 0
    for path in iter_text_files(root):
        n_files += 1
        try:
            tree = ET.parse(path)
        except (ET.ParseError, OSError):
            n_bad += 1
            continue
        # 路径里带 zh_Hans_CN 的文件，语言由目录决定，元素上没有 Language 属性
        default_lang = "zh_Hans_CN" if "zh_Hans_CN" in path else None
        for el in tree.iter():
            tag = el.get("Tag")
            if not tag:
                continue
            if (el.get("Language") or default_lang) != "zh_Hans_CN":
                continue
            text_el = el.find("Text")
            if text_el is None or not text_el.text:
                continue
            # 多语言变体用 | 分隔（单数|复数|……），中文取第一段
            txt = text_el.text.split("|")[0].strip()
            if txt:
                found[tag] = txt
    return found, n_files, n_bad


def existing_tags():
    """本地化库里已有的简体中文 tag。"""
    lo = os.path.join(kb.find_cache_dir(), "DebugLocalization.sqlite")
    con = sqlite3.connect(f"file:{lo}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT Tag FROM LocalizedText WHERE Language='zh_Hans_CN'").fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}


def rebuild(silent=False):
    """重扫游戏 XML，重写 text_patch.json。

    返回 `(ok, patch)`；失败时 `ok=False`，`patch` 是原因说明文字。

    `silent=True` 全程不打印（agent 启动时用这条路）。
    写文件走「临时文件 + 原子替换」：万一中途失败（磁盘满、被占用），
    原来的补丁原封不动，不会留下半个文件把下次查询带崩。
    """
    def say(*a):
        if not silent:
            print(*a)

    game = find_game_dir()
    if not game:
        return False, "没找到文明6的安装目录"

    say(f"游戏目录：{game}")
    found, n_files, n_bad = collect_chinese(game)
    say(f"扫描文件 {n_files} 个（解析失败 {n_bad} 个），提取到 {len(found)} 条简体中文")

    have = existing_tags()
    say(f"本地化库已有 {len(have)} 条简体中文")

    missing_all = {k: v for k, v in found.items() if k not in have}
    patch = {k: v for k, v in missing_all.items() if k.endswith(KEEP_SUFFIX)}

    say(f"其中库中缺失：{len(missing_all)} 条")
    say(f"过滤到名称/说明/引言类：{len(patch)} 条 → 写入 text_patch.json")

    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(patch, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, OUT_PATH)

    say(f"完成，{OUT_PATH}（{os.path.getsize(OUT_PATH) / 1024:.0f} KB）")
    return True, patch


def main():
    ok, res = rebuild(silent=False)
    if not ok:
        print(f"{res}。")
        print("可以手动指定后再跑，例如：")
        print(r"  set CIV6_DIR=D:\Steam\steamapps\common\Sid Meier's Civilization VI")
        print(r"  python build_text_patch.py")
        return 1

    print("\n抽样看看补上了什么：")
    for k in sorted(res)[:12]:
        print(f"  {k:50} {res[k][:28]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
