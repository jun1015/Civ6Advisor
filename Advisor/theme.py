"""游戏外观 —— 字体和配色全部取自文明6自己的资源。

为什么值得费这个事：
    浮在游戏画面上的窗口，最怕看起来像"另一个软件弹出来了"。
    把字体和配色换成游戏自己的，视觉上就融进去了，不容易出戏。

字体是"临时加载"的（Windows 的 FR_PRIVATE 标志）：
    不写进系统字体目录、不污染用户环境、不需要管理员权限，程序一退就没了。
    用的就是游戏中文本体自带的 NotoSansCJKsc —— 跟游戏中文版是同一个字体文件。
"""

import ctypes
import os
import sys

# ---------------------------------------------------------------- 配色
# 这些色值不是"照着感觉调的"，是从游戏自己的 UI 定义文件里统计出来的真实值：
#   #2E465F  在游戏 UI 定义里出现 85 次，是主面板的标志色
#   #204665  次一级的面板
#   #CEDAE1  正文文字（浅灰蓝）
#   #8FA8BC  次要文字
#   #14908C  游戏用来做高亮的青绿
BG         = "#16222F"
PANEL      = "#204665"
PANEL_TOP  = "#2E465F"
BORDER     = "#2E465F"
TEXT       = "#CEDAE1"
TEXT_DIM   = "#8FA8BC"
TEXT_FAINT = "#6F8AA0"
ACCENT     = "#14908C"
INPUT_BG   = "#0E1A26"
# 输入框拿到焦点时的底色。比 INPUT_BG 亮一档 —— 目的是让
# "现在键盘在打字框里"这件事看得见（实测反馈：看不出选中了没有）。
INPUT_BG_FOCUS = "#1B3048"

# ---------------------------------------------------------------- 字体
# 这三个文件就是游戏中文版 UI 用的字（见 Civ6_FontStyles_zh_Hans_CN.xml）
FONT_FILES = (
    "Base/Assets/UI/Fonts/NotoSans/NotoSansCJKsc-Regular.otf",
    "Base/Assets/UI/Fonts/NotoSans/NotoSansCJKsc-Medium.otf",
    "Base/Assets/UI/Fonts/NotoSans/NotoSansCJKsc-Bold.otf",
)

FAMILY_REGULAR = "Noto Sans CJK SC Regular"
FAMILY_MEDIUM = "Noto Sans CJK SC Medium"
FAMILY_BOLD = "Noto Sans CJK SC Bold"

# 字体加载失败时的退路（Windows 中文环境基本都有）
FALLBACK = "Microsoft YaHei"

FR_PRIVATE = 0x10


def game_dir():
    """游戏安装目录；找不到返回 None。

    复用补丁脚本里的查找逻辑，避免"两处各写一份、改了一处忘了另一处"。
    """
    try:
        import build_text_patch
        return build_text_patch.find_game_dir()
    except Exception:
        return None


def load_game_fonts():
    """把游戏自带的中文字体临时注册进本进程。

    返回 True 表示界面可以用游戏字体；False 表示退回系统字体。
    """
    if not sys.platform.startswith("win"):
        return False
    root = game_dir()
    if not root:
        return False

    gdi = ctypes.windll.gdi32
    loaded = 0
    for rel in FONT_FILES:
        path = os.path.join(root, rel.replace("/", os.sep))
        if os.path.isfile(path):
            try:
                loaded += gdi.AddFontResourceExW(path, FR_PRIVATE, 0)
            except Exception:
                pass
    return loaded > 0


def ui_fonts(game_font_loaded, scale=1.0):
    """给界面用的一组字体。键是用途，值是 tkinter 字体元组。

    字号写成负数 = 按"像素"算。这样在高 DPI 屏幕上大小完全由我们控制，
    不会因为 Tk 自己按 point 换算而忽大忽小。
    """
    if game_font_loaded:
        body, small, bold = FAMILY_MEDIUM, FAMILY_REGULAR, FAMILY_BOLD
    else:
        body = small = bold = FALLBACK

    def px(n):
        return -max(9, int(round(n * scale)))

    return {
        "title": (bold, px(15)),
        "body": (body, px(13)),
        "small": (small, px(11)),
        "input": (body, px(13)),
        "tag": (bold, px(12)),
        "mono": ("Consolas", px(12)),
    }
