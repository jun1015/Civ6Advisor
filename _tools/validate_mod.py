"""
文明6 Mod 静态校验器（不需要启动游戏）

做的事：
  1. 从游戏自带的 schema 文件里读出每张表的合法列名
  2. 扫描你的 mod 里的 xml，检查每个 <Row> 的属性和表名是否合法
  3. 检查所有 xml 是否格式良好、modinfo 里列出的文件是否真实存在

为什么需要它：
  Civ6 遇到一个非法列名时，会「从那一行开始整个文件都不再加载」。
  这是新手最常见的坑，而且游戏里只表现为「什么都没发生」。

用法：
    python validate_mod.py ../AdvisorMod/Civ6AdvisorBridge   # 校验仓库里那个 mod
    python validate_mod.py <你的mod文件夹>
"""
import os
import re
import sys
import glob
import xml.etree.ElementTree as ET

# 游戏安装目录**不写死**，复用 Advisor 那边已经写好的定位逻辑：
#   先看 CIV6_DIR 环境变量 → 再搜常见安装位置 → 最后问 Steam 的库清单。
# 两条原因：① 别人的游戏不一定装在 D:\Steam；
#           ② 把作者的目录结构写进公开仓库，等于顺带泄露本机信息。
sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Advisor")))
import build_text_patch as _btp          # noqa: E402  只为拿 find_game_dir

CIV6 = _btp.find_game_dir() or ""

SCHEMA_FILES = {
    "gameplay": os.path.join(CIV6, "Base", "Assets", "Gameplay", "Data", "Schema", "01_GameplaySchema.sql"),
    "config": os.path.join(CIV6, "Base", "Assets", "Configuration", "Data", "Schema", "AdditionalTables.sql"),
    "icons": os.path.join(CIV6, "Base", "Assets", "Database", "IconManager.sql"),
    "localization": os.path.join(CIV6, "Base", "Assets", "Text", "LocalizationDatabase_Schema.sql"),
    "modding": os.path.join(CIV6, "Base", "Assets", "Database", "Modding.sql"),
    "xp1": os.path.join(CIV6, "DLC", "Expansion1", "Data", "Expansion1_Schema.sql"),
    "xp2": os.path.join(CIV6, "DLC", "Expansion2", "Data", "Expansion2_Schema.sql"),
}

# 允许的操作元素（不是表名）
CSS_OPERATORS = {"Row", "Update", "Replace", "Delete", "Where", "Set"}

# 只出现在这些表里的「行」，按表归属校验
SCHEMA_LINE = re.compile(
    r"CREATE\s+TABLE\s+['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?\s*\((.*?)\)\s*(?:;|$)",
    re.IGNORECASE | re.DOTALL,
)
COLUMN_LINE = re.compile(r"['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s+(TEXT|INTEGER|BOOLEAN|INT|REAL|BLOB|NUMERIC)", re.IGNORECASE)


def load_schema():
    tables = {}
    for label, path in SCHEMA_FILES.items():
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            sql = fh.read()
        for m in SCHEMA_LINE.finditer(sql):
            name, body = m.group(1), m.group(2)
            cols = {c.group(1) for c in COLUMN_LINE.finditer(body)}
            cols.discard("PRIMARY")
            cols.discard("FOREIGN")
            if cols:
                tables.setdefault(name, set()).update(cols)
    return tables


def check_file(path, tables, problems, warnings):
    rel = os.path.relpath(path)
    if rel.endswith(".modinfo") or rel.endswith(".dep"):
        try:
            ET.parse(path)
        except ET.ParseError as exc:
            problems.append(f"{rel}: XML 格式错误 -> {exc}")
        return
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        problems.append(f"{rel}: XML 格式错误 -> {exc}")
        return
    root = tree.getroot()

    if root.tag == "AssetObjects..ArtDefSet":
        return  # 美术定义文件，不适用表校验

    # 游戏数据 / 图标 / 文本文件的根元素统一是 GameInfo 或 GameData，
    # 子元素才是表名。**其他根元素一律跳过表校验** —— 最典型的是界面文件
    # （根元素是 <Context>，走 <AddUserInterfaces> 那条路，里面是控件树而不是
    # 数据表）。早先的版本会把 <Context> 当成"未知的表"报错，纯属误报。
    if root.tag not in ("GameInfo", "GameData"):
        warnings.append(
            f"{rel}: 根元素是 <{root.tag}>，不是数据文件，跳过表校验"
        )
        return

    table_els = list(root)

    known = 0
    for table_el in table_els:
        table_name = table_el.tag
        if table_name not in tables:
            problems.append(f"{rel}: 未知的表 <{table_name}>")
            continue
        known += 1
        valid = tables[table_name]
        rows = [r for r in table_el if r.tag in ("Row", "Replace", "Update", "Delete")]
        if not rows:
            warnings.append(f"{rel}: 表 <{table_name}> 是空的，没有任何数据行")
        for row in rows:
            if row.tag == "Update":
                for sub in row:
                    if sub.tag in ("Where", "Set"):
                        for k in sub.attrib:
                            if k not in valid:
                                problems.append(f"{rel}: {table_name} 没有列 '{k}'（在 <{sub.tag}> 里）")
                    else:
                        for k in sub.attrib:
                            if k not in valid:
                                problems.append(f"{rel}: {table_name} 没有列 '{k}'（在 <{row.tag}> 里）")
                continue
            if row.tag == "Delete":
                continue
            for attr in row.attrib:
                if attr not in valid:
                    problems.append(
                        f"{rel}: 表 {table_name} 没有列 '{attr}'  <- 这一行会导致整个文件从这里起不加载"
                    )
            for child in row:
                if child.tag not in valid:
                    problems.append(f"{rel}: 表 {table_name} 没有列 <{child.tag}>  <- 应写成属性而非子元素")


def check_modinfo(mod_dir, problems, warnings):
    infos = glob.glob(os.path.join(mod_dir, "*.modinfo"))
    if not infos:
        problems.append("找不到 .modinfo 文件，游戏不会识别这个 mod")
        return
    for path in infos:
        try:
            tree = ET.parse(path)
        except ET.ParseError as exc:
            problems.append(f"{os.path.basename(path)}: XML 格式错误 -> {exc}")
            continue
        root = tree.getroot()
        if root.tag != "Mod":
            problems.append(f"{os.path.basename(path)}: 根元素必须是 <Mod>")
        if not root.get("id"):
            problems.append(f"{os.path.basename(path)}: <Mod> 缺少 id（GUID）")
        for el in root.iter("File"):
            rel = (el.text or "").strip()
            if rel and not os.path.exists(os.path.join(mod_dir, rel)):
                problems.append(f"{os.path.basename(path)}: 列出的文件不存在 -> {rel}")
        listed = {(el.text or "").strip() for el in root.iter("File")}
        for f in glob.glob(os.path.join(mod_dir, "**", "*"), recursive=True):
            if os.path.isfile(f):
                rel = os.path.relpath(f, mod_dir).replace("\\", "/")
                if rel.endswith(".modinfo"):
                    continue
                if rel not in listed:
                    warnings.append(f"{rel} 没有登记在 .modinfo 的 <Files> 里")


def normalize_dir(path):
    """把命令行给的目录规范化。

    踩过两次的坑：用 Git Bash 那套 `/c/Users/...` 写法时，Windows 的 glob
    解析不了，结果报「扫描 0 个文件 / 找不到 .modinfo」—— 看着像 mod 的问题，
    其实是路径写法的问题。这里顺手转成 `C:/Users/...`。
    """
    p = os.path.expanduser(path or ".")
    m = re.match(r"^/([A-Za-z])/(.*)$", p)      # /c/Users/... -> C:/Users/...
    if m:
        p = f"{m.group(1).upper()}:/{m.group(2)}"
    return os.path.abspath(p)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    if not CIV6:
        print("没找到文明6的安装目录 —— 校验要读游戏自带的 schema，找不到就没法做。")
        print("装了但不在常见位置的话，先设一下环境变量再跑：")
        print("  set CIV6_DIR=D:\\你的路径\\Sid Meier's Civilization VI")
        return 1
    mod_dir = normalize_dir(sys.argv[1])
    tables = load_schema()
    print(f"已从游戏 schema 读取 {len(tables)} 张表定义\n")

    problems, warnings = [], []
    xml_files = []
    for pat in ("**/*.xml", "**/*.dep", "**/*.modinfo"):
        xml_files += glob.glob(os.path.join(mod_dir, pat), recursive=True)
    for path in sorted(set(xml_files)):
        check_file(path, tables, problems, warnings)
    check_modinfo(mod_dir, problems, warnings)

    print(f"扫描 {len(set(xml_files))} 个文件")
    if warnings:
        print(f"\n提示（{len(warnings)}）:")
        for w in warnings:
            print("  - " + w)
    if problems:
        print(f"\n错误（{len(problems)}）:")
        for p in problems:
            print("  x " + p)
        return 1
    print("\n通过：没有发现非法列名或缺失文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
