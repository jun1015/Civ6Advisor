"""程序"自己家"的目录在哪。

单独放一个模块，是因为好几个文件都要用，而且**打包成 exe 之后规则不一样**：

    PyInstaller 打成单文件 exe 后，代码是解压到一个临时目录里跑的，
    所以 __file__ 指向的是那个临时目录（每次启动还不一样，退出就删）。
    config.json / text_patch.json / 启动留痕要放在用户看得见的地方，
    也就是 exe 自己所在的目录 —— 那个得用 sys.executable。

这个模块不 import 本项目的任何东西，所以谁都可以安全地用。
"""

import os
import sys


def app_dir():
    """返回程序所在目录：没打包 = 源码目录；打包了 = exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))
