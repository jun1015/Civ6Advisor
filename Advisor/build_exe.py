"""把浮层打包成单文件 exe（文明6顾问.exe）。

为什么单独一个 py、而不是把命令写在 bat 里：
    exe 的名字是中文（文明6顾问.exe），而 **cmd 是按控制台代码页解析 bat 内容的**，
    中文参数很容易变成乱码 —— 结果就是打出一个名字是乱码的 exe（本机实测：
    Git Bash 里调 cmd 那次连 bat 都没跑起来，代码页这条路不值得赌）。
    Python 处理 Unicode 是天生的，所以正式的名字放在这里。
    这也和项目里 sync_mod.bat → sync_mod.py 的路子一致：bat 只负责双击。

用法：
    python build_exe.py                打包到本目录（会覆盖现有的 exe）
    python build_exe.py --dist D:\\tmp   打到别的目录（验证打包流程时用，
                                        不会碰到正在用的那个 exe）

产物：<dist>/文明6顾问.exe，约 12.7 MB，双击即可运行，目标机器不需要装 Python。
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys

import paths

NAME = "文明6顾问"
ENTRY = "overlay.py"
ICON = "advisor_icon.ico"
PYI_ARGS = ["--onefile", "--noconsole", "--clean", "--noconfirm"]


def check_env():
    """打包前先把两个"打了也白打"的情况拦下来，返回问题列表。"""
    problems = []

    try:
        import tkinter  # noqa: F401
    except Exception:
        problems.append(
            "这个 Python 没带 tkinter（界面库）——打出来的 exe 一运行就退出。\n"
            "  换一个带 tkinter 的 Python（微软商店版自带；精简版/嵌入式版通常没有）。"
        )

    try:
        import PyInstaller  # noqa: F401
    except Exception:
        problems.append(
            "还没装 PyInstaller（打包工具）。先执行：\n"
            "  python -m pip install --user pyinstaller\n"
            "  国内网络慢就加个源：\n"
            "  python -m pip install --user pyinstaller "
            "-i https://mirrors.cloud.tencent.com/pypi/simple/"
        )

    return problems


def build(dist):
    here = paths.app_dir()
    entry = os.path.join(here, ENTRY)
    icon = os.path.join(here, ICON)
    work = os.path.join(here, "_pyi_build")

    for path in (entry, icon):
        if not os.path.exists(path):
            raise SystemExit(f"找不到文件：{path}")

    cmd = [
        sys.executable, "-m", "PyInstaller", *PYI_ARGS,
        "--name", NAME,
        # ⚠️ 图标必须给**绝对路径**。相对路径是按 spec 文件所在目录解析的，
        #    实测会报 "Icon file not found"（specpath 一挪就找不到）。
        "--icon", icon,
        "--distpath", dist,
        "--workpath", work,
        "--specpath", work,
        entry,
    ]

    print("打包中，大约需要一到两分钟……\n")
    rc = subprocess.run(cmd, cwd=here).returncode

    # 中间产物一律清掉，只留 exe（_pyi_build / *.spec 也在 .gitignore 里）
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(os.path.join(here, "build"), ignore_errors=True)
    for spec in glob.glob(os.path.join(here, "*.spec")):
        try:
            os.remove(spec)
        except OSError:
            pass

    if rc != 0:
        print("\n打包失败 —— 上面应该有 PyInstaller 的报错，把那几行发我。")
        return 1

    exe = os.path.join(dist, NAME + ".exe")
    if not os.path.exists(exe):
        print(f"\n命令跑完了，但没找到产物：{exe}")
        return 1

    size = os.path.getsize(exe) / 1048576
    print(f"\n完成：{exe}（{size:.1f} MB）")
    print("双击即可运行，目标机器不用装 Python。")
    print("提醒：第一次启动要等 4 秒左右（单文件 exe 得先把自己解压到临时目录）。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="把浮层打包成单文件 exe")
    ap.add_argument("--dist", default=None,
                    help="产物目录，默认放在项目目录（会覆盖现有的 exe）")
    args = ap.parse_args()

    problems = check_env()
    if problems:
        print("先解决下面这些问题再打包：\n")
        for i, p in enumerate(problems, 1):
            print(f"{i}) {p}")
        return 1

    dist = os.path.abspath(args.dist or paths.app_dir())
    os.makedirs(dist, exist_ok=True)
    return build(dist)


if __name__ == "__main__":
    sys.exit(main())
