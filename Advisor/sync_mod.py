"""把游戏内 mod 从项目目录同步到游戏的 Mods 目录。

为什么需要这一步：
    游戏只认 Documents\\My Games\\...\\Mods\\ 下面的文件，
    根本不读我们这个项目目录。所以每次改完 mod，都得同步一次，
    再重启游戏（或者在"额外内容"里关掉再打开）才会生效。

为什么用 Python 写、只留一个两行的 bat：
    bat 文件里写中文会踩编码的坑（cmd 默认不是 UTF-8，注释会乱码，
    严重时连命令都被带歪）。把逻辑放在 Python 里最省事，
    bat 就只剩两行纯 ASCII 的启动代码。
"""

import hashlib
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "AdvisorMod", "Civ6AdvisorBridge"))
DST = os.path.join(
    os.path.expanduser("~"),
    "Documents", "My Games", "Sid Meier's Civilization VI", "Mods",
    "Civ6AdvisorBridge",
)

MOD_INFO = "Civ6AdvisorBridge.modinfo"


def digest(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def main():
    if not os.path.isfile(os.path.join(SRC, MOD_INFO)):
        print("[错误] 没找到 mod 源文件，期望在：")
        print("       " + SRC)
        return 1

    print("源目录：" + SRC)
    print("目标  ：" + DST)
    print()

    copied, same = [], []
    for root, _dirs, files in os.walk(SRC):
        rel = os.path.relpath(root, SRC)
        target_dir = DST if rel == "." else os.path.join(DST, rel)
        os.makedirs(target_dir, exist_ok=True)

        for name in files:
            s = os.path.join(root, name)
            d = os.path.join(target_dir, name)
            label = name if rel == "." else os.path.join(rel, name).replace("\\", "/")

            if os.path.isfile(d) and digest(s) == digest(d):
                same.append(label)
                continue

            shutil.copy2(s, d)
            copied.append(label)

    for label in copied:
        print("  已更新   " + label)
    for label in same:
        print("  没变化   " + label)

    print()
    if copied:
        print("同步完成，共更新 %d 个文件。" % len(copied))
        print("接下来：重启游戏（或在「额外内容」里把这个 mod 关掉再打开）。")
    else:
        print("全部已是最新，不需要改动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
