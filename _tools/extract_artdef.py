"""
从《文明6》原版 ArtDefs/Units.artdef 中抽出某个单位的模型定义，
改名成你自己的单位，生成 mod 用的 ArtDefs/Units.artdef。

用途：新单位不写这一步在游戏里是「隐身」的（逻辑存在、没有身体）。
复用原版模型就不用碰 3D 建模。

用法：
    python extract_artdef.py <源单位ArtDef名> <目标单位ArtDef名> <输出文件>

例：
    python extract_artdef.py SWORDSMAN MY_CHAMPION 我的MOD/ArtDefs/Units.artdef

源单位 ArtDef 名 = 单位 Type 去掉 'UNIT_' 前缀，例如：
    UNIT_SWORDSMAN      -> SWORDSMAN
    UNIT_WARRIOR        -> WARRIOR
    UNIT_TANK           -> TANK
"""
import sys
import os
import xml.etree.ElementTree as ET

# 游戏安装目录**不写死**，复用 Advisor 那边的定位逻辑（同 validate_mod.py 的说明）。
sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Advisor")))
import build_text_patch as _btp          # noqa: E402  只为拿 find_game_dir

CIV6 = _btp.find_game_dir() or ""
SRC = os.path.join(CIV6, "Base", "ArtDefs", "Units.artdef")

TAG_SET = "AssetObjects..ArtDefSet"


def text_of(parent, tag):
    el = parent.find(tag)
    return el.get("text") if el is not None else None


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        return 1
    if not CIV6:
        print("没找到文明6的安装目录 —— 本工具要从游戏原版 artdef 里抽模型定义。")
        print("装了但不在常见位置的话，先设一下环境变量再跑：")
        print("  set CIV6_DIR=D:\\你的路径\\Sid Meier's Civilization VI")
        return 1
    src_name, dst_name, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

    # artdef 里的实体名统一是 UNIT_ 前缀 + 大写
    def norm(n):
        n = n.strip()
        if not n.upper().startswith("UNIT_"):
            n = "UNIT_" + n
        return n.upper()

    src_name = norm(src_name)
    dst_name = norm(dst_name)

    tree = ET.parse(SRC)
    root = tree.getroot()

    version = None
    collections = None
    for child in root:
        if child.tag == "m_Version":
            version = child
        elif child.tag == "m_RootCollections":
            collections = child
    if collections is None:
        raise SystemExit("在源文件里找不到 m_RootCollections")

    units_coll = None
    for coll in collections:
        if text_of(coll, "m_CollectionName") == "Units":
            units_coll = coll
            break
    if units_coll is None:
        raise SystemExit("找不到名为 Units 的集合")

    target = None
    for el in units_coll:
        if el.tag == "Element" and text_of(el, "m_Name") == src_name:
            target = el
            break
    if target is None:
        raise SystemExit(f"找不到单位定义 {src_name}，检查拼写")

    # 只改最外层名字，内部对 XrefName 的引用保持原样 -> 复用原模型
    name_el = target.find("m_Name")
    name_el.set("text", dst_name)

    # 组装一个最小可合并的 artdef：版本号抄原版，模板名必须是 Units
    new_root = ET.Element(TAG_SET)
    if version is not None:
        new_root.append(version)

    tmpl = ET.SubElement(new_root, "m_TemplateName")
    tmpl.set("text", "Units")

    new_rc = ET.SubElement(new_root, "m_RootCollections")
    new_coll = ET.SubElement(new_rc, "Element")
    ET.SubElement(new_coll, "m_CollectionName").set("text", "Units")
    ET.SubElement(new_coll, "m_ReplaceMergedCollectionElements").set("text", "false")
    new_coll.append(target)

    ET.indent(new_root, space="\t")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    out_tree = ET.ElementTree(new_root)
    out_tree.write(out_path, encoding="UTF-8", xml_declaration=True)

    size = os.path.getsize(out_path)
    print(f"已生成 {out_path}")
    print(f"  源模型: {src_name} -> 目标单位: {dst_name}  ({size} 字节)")


if __name__ == "__main__":
    sys.exit(main())
