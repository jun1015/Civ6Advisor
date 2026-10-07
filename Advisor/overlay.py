"""游戏内顾问的浮层窗口。

它浮在游戏画面上，努力看起来像游戏的一部分：
  - 字体用游戏中文版自己的 NotoSansCJKsc
  - 配色用从游戏 UI 定义里提取出来的真实色值
  - 无边框、半透明、置顶、可拖动

平时它是隐藏的。游戏里点了顾问按钮（或按 F8），
游戏内的 mod 会往 Lua.log 写一个信号，这里读到就把它浮出来。

为什么不做成"游戏内绘制的面板"：
    游戏内的 Lua 沙箱是全封闭的，只能往外喊、听不到外面。
    所以窗口只能由外面画 —— 但"浮在游戏画面上"和"由游戏渲染"，
    对眼睛来说是一回事。
"""

import ctypes
import os
import re
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import simpledialog

import agent
import game_bridge
import kb
import llm
import paths
import theme

# 缩放前的基准尺寸
BASE_W = 430
BASE_H = 520

POLL_MS = 350          # 多久看一眼 Lua.log

HERE = paths.app_dir()      # 打包后 __file__ 指向临时目录，见 paths.py
# pythonw 没有控制台：启动成功还是失败，只能靠这两个文件告诉外面。
# bat 启动后会来看 overlay_started.txt 在不在，由此判断到底起没起来。
STARTED_FILE = os.path.join(HERE, "overlay_started.txt")
ERROR_FILE = os.path.join(HERE, "overlay_error.txt")


# ------------------------------------------------------------------ 工具

def enable_dpi_awareness():
    """让窗口在高 DPI 屏幕上不发虚。返回缩放倍数（1.0 表示 100%）。"""
    if not sys.platform.startswith("win"):
        return 1.0
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            return 1.0
    try:
        dc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)   # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, dc)
        return max(1.0, dpi / 96.0)
    except Exception:
        return 1.0


_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def tables_to_list(md):
    """把 Markdown 表格换成"第一列：其余列"的清单。

    为什么不保留原样：tkinter 里中英混排根本对不齐，硬排出来是歪的。
    换成一列一列的清单，反而一眼能读。
    """
    lines = md.splitlines()
    out, i = [], 0
    while i < len(lines):
        if (lines[i].strip().startswith("|")
                and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1])):
            i += 2                       # 跳过表头和分隔行
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c for c in _cells(lines[i]) if c]
                if cells:
                    out.append(cells[0] if len(cells) == 1
                               else f"{cells[0]}：{' · '.join(cells[1:])}")
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


# ------------------------------------------------------------------ 主窗口

class Overlay:

    RICH = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`")

    def __init__(self, config):
        self.config = config
        self.scale = enable_dpi_awareness()
        self.font_loaded = theme.load_game_fonts()
        self.fonts = theme.ui_fonts(self.font_loaded, self.scale)

        self.bridge = game_bridge.LogTailer()
        self.advisor = agent.Advisor(config)

        self._visible = False
        self._busy = False
        self._drag_from = (0, 0)
        self._drag_win = (0, 0)
        self._hotkey_down = False
        # 设置窗口（懒创建）。None = 还没开过。
        self._settings_win = None
        # 上一次"汇报过"的战局序号。-1 = 还没汇报过任何一份；
        # 0 专门表示"点了按钮但没读到战局"。见 _announce_state。
        self._last_seq = -1

        self._build()

    # -------------------------------------------------- 界面搭建

    def _px(self, n):
        return int(round(n * self.scale))

    def _build(self):
        px = self._px
        self.W, self.H = px(BASE_W), px(BASE_H)

        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)              # 去掉标题栏，更像游戏的一部分
        root.attributes("-topmost", True)        # 永远盖在游戏上面
        root.attributes("-alpha", 0.97)          # 微微透一点，不糊住画面
        root.configure(bg=theme.BORDER)
        self.root = root

        outer = tk.Frame(root, bg=theme.BORDER)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=theme.PANEL)
        inner.pack(fill="both", expand=True, padx=px(1), pady=px(1))

        # ---- 标题栏（也是拖动把手）----
        bar = tk.Frame(inner, bg=theme.PANEL_TOP, height=px(30))
        bar.pack(fill="x")
        bar.pack_propagate(False)

        dot = tk.Canvas(bar, width=px(9), height=px(9), bg=theme.PANEL_TOP,
                        highlightthickness=0)
        dot.create_oval(0, 0, px(8), px(8), fill=theme.ACCENT, outline="")
        dot.pack(side="left", padx=(px(11), px(7)))

        title = tk.Label(bar, text="顾问", bg=theme.PANEL_TOP, fg=theme.TEXT_DIM,
                         font=self.fonts["small"])
        title.pack(side="left")

        close = tk.Label(bar, text="Esc 收起", bg=theme.PANEL_TOP, fg=theme.TEXT_DIM,
                         font=self.fonts["small"], cursor="hand2")
        close.pack(side="right", padx=(px(8), px(11)))
        close.bind("<Button-1>", lambda _e: self.hide())

        gear = tk.Label(bar, text="设置", bg=theme.PANEL_TOP, fg=theme.TEXT_DIM,
                        font=self.fonts["small"], cursor="hand2")
        gear.pack(side="right")
        gear.bind("<Button-1>", lambda _e: self.open_settings())

        for w in (bar, dot, title):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        # ---- 输入区 ----
        # ⚠️ 必须先 pack 底部的输入条，再 pack 带 expand=True 的对话区。
        #    Tk 的 pack 是**按调用顺序**从容器里切空间的 —— 先切的先得。
        #    如果对话区排在前面，它会一口气吃掉整块剩余空间，后面才 pack 的
        #    输入条只剩 1 像素：不报错、不留日志，表现就是"窗口里根本没有输入框"。
        #    （实测踩过：body 请求高 918px，输入的 foot 被挤到 height=1 且未映射。）
        self.foot = foot = tk.Frame(inner, bg=theme.INPUT_BG, height=px(38))
        foot.pack(side="bottom", fill="x")
        foot.pack_propagate(False)

        # 输入区上沿的强调线：失焦时是暗色（融进背景），拿到焦点就点亮成青绿。
        # 这是"现在能打字"最直观的信号（实测反馈：看不出选中了没有）。
        self.accent_line = tk.Frame(inner, bg=theme.BORDER, height=px(2))
        self.accent_line.pack(side="bottom", fill="x")

        self.prompt = tk.Label(foot, text=">", bg=theme.INPUT_BG, fg=theme.ACCENT,
                               font=self.fonts["input"])
        self.prompt.pack(side="left", padx=(px(12), px(5)))

        # 输入框里永远只有用户自己打的字 —— 那行提示是**另一个 Label** 叠在上面。
        # 早先是把提示 insert 进 Entry，中文输入法下清不掉，用户得手动删。
        self.entry_var = tk.StringVar()
        self.entry = tk.Entry(foot, textvariable=self.entry_var,
                              bg=theme.INPUT_BG, fg=theme.TEXT,
                              font=self.fonts["input"], relief="flat",
                              borderwidth=0, highlightthickness=0,
                              insertbackground=theme.TEXT,
                              insertwidth=px(2))          # 光标粗一点，好找
        self.entry.pack(side="left", fill="both", expand=True,
                        padx=(0, px(11)), pady=px(7))
        self.entry.bind("<Return>", self._submit)
        # 焦点一进一出，整条输入区的观感跟着变 —— 让人看得出"现在能打字"
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)

        self.ph_label = tk.Label(foot, text=self.PLACEHOLDER, bg=theme.INPUT_BG,
                                 fg=theme.TEXT_DIM, font=self.fonts["input"])
        self.ph_label.place(in_=self.entry, x=0, rely=0.5, anchor="w")
        self.ph_label.bind("<Button-1>", lambda _e: self.entry.focus_set())
        # 用 StringVar 的 trace 同步提示显隐，**不用**按键事件：
        # 中文输入法下 <KeyPress> 的 event.char 常常是空串，按字符判断会失效。
        self.entry_var.trace_add("write", self._ph_sync)

        # 点窗口里任何地方都把光标送进输入框 —— 省得用户去瞄最下面那行。
        # 绑在 toplevel 上就够了：Tk 的事件绑定链里，toplevel 是所有子控件的下一站。
        root.bind("<Button-1>", self._ph_focus, add="+")

        # ---- 对话区（最后 pack，吃掉剩下的空间）----
        body = tk.Frame(inner, bg=theme.BG)
        body.pack(side="top", fill="both", expand=True)

        self.answer = tk.Text(
            body, bg=theme.BG, fg=theme.TEXT, font=self.fonts["body"],
            wrap="word", relief="flat", borderwidth=0, highlightthickness=0,
            insertbackground=theme.TEXT, padx=px(12), pady=px(10),
            spacing1=px(2), spacing3=px(3), cursor="arrow",
            # width/height 都设 1：Text 默认按 24 行报请求高度（实测 918px），
            # 会让它自己抢光窗口。设 1 表示"我不挑，给我多少用多少"。
            width=1, height=1,
        )
        self.answer.pack(side="left", fill="both", expand=True)

        sb = tk.Scrollbar(body, command=self.answer.yview, width=px(9),
                          troughcolor=theme.BG, bg=theme.PANEL_TOP,
                          relief="flat", borderwidth=0, highlightthickness=0)
        sb.pack(side="right", fill="y")
        self.answer.configure(yscrollcommand=sb.set, state="disabled")
        self.answer.bind("<MouseWheel>",
                         lambda e: self.answer.yview_scroll(int(-e.delta / 120), "units"))

        self.answer.tag_configure("h", foreground=theme.ACCENT,
                                  font=self.fonts["title"],
                                  spacing1=px(10), spacing3=px(4))
        self.answer.tag_configure("bold", font=self.fonts["tag"])
        self.answer.tag_configure("dim", foreground=theme.TEXT_DIM,
                                  font=self.fonts["small"])
        self.answer.tag_configure("tool", foreground=theme.ACCENT,
                                  font=self.fonts["small"])
        self.answer.tag_configure("user", foreground=theme.TEXT_DIM,
                                  font=self.fonts["body"], spacing1=px(10))

        root.bind("<Escape>", lambda _e: self.hide())
        # Ctrl+L 清屏**并**清掉模型记忆 —— 只清界面会造成"窗口空白、
        # 模型却记得你看不见的上文"的割裂（见 _reset_conversation）
        root.bind("<Control-l>", lambda _e: self._reset_conversation())

        self._place_default()

    def _place_default(self):
        """默认落在屏幕右下偏上 —— 那里通常只有小地图，最不挡视野。"""
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = sw - self.W - self._px(58)
        y = sh - self.H - self._px(130)
        self.root.geometry(f"{self.W}x{self.H}+{max(0, x)}+{max(0, y)}")

    # -------------------------------------------------- 显示 / 隐藏

    def show(self):
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self._visible = True
        self._grab_keyboard()

    def hide(self):
        self.root.withdraw()
        self._visible = False

    def toggle(self):
        self.hide() if self._visible else self.show()

    def _grab_keyboard(self):
        """把键盘焦点抢过来。

        必须做这一步：overrideredirect 的无边框窗口，Windows 不会
        自动把焦点给它，不抢的话输入框打不了字。
        """
        try:
            self.root.lift()
            self.root.focus_force()
            self.entry.focus_set()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if hwnd:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    # -------------------------------------------------- 输入框焦点反馈

    def _paint_input_area(self, focused):
        """把整条输入区画成"有焦点 / 没焦点"两种样子。

        不去给 Entry 单加边框：这是个无边框窗口，在游戏画面上突然多一个
        矩形边框很突兀。改成整条输入区的底色 + 上沿强调线一起变 ——
        实测反馈是"看不出选中了没有"，这样一眼就能看出来。
        """
        bg = theme.INPUT_BG_FOCUS if focused else theme.INPUT_BG
        line = theme.ACCENT if focused else theme.BORDER
        for w in (self.foot, self.prompt, self.ph_label):
            try:
                w.configure(bg=bg)
            except tk.TclError:
                pass            # 窗口正在销毁时控件可能已经没了
        try:
            self.accent_line.configure(bg=line)
        except tk.TclError:
            pass

    def _on_focus_in(self, _event=None):
        self._paint_input_area(True)

    def _on_focus_out(self, _event=None):
        self._paint_input_area(False)

    # -------------------------------------------------- 输入框提示

    PLACEHOLDER = "在这里打字，回车发送"

    def _ph_sync(self, *_):
        """输入框空着就显示提示，有内容就藏起来。

        提示是独立的 Label（place 在输入框上），**不塞进 Entry**。
        早先的写法是往 Entry 里 insert 提示文字，再靠 <KeyPress> 的
        event.char 判断"是否开始打字"然后删掉 —— 中文输入法下 event.char
        常常是空串，提示永远清不掉，用户只能手动删。
        改成 trace + 独立 Label 之后，Entry 的内容永远只有用户自己打的字。
        """
        if self.entry_var.get():
            self.ph_label.place_forget()
        else:
            self.ph_label.place(in_=self.entry, x=0, rely=0.5, anchor="w")

    def _ph_focus(self, _event=None):
        """点窗口任意处都聚焦输入框。"""
        try:
            self.entry.focus_set()
        except Exception:
            pass

    # -------------------------------------------------- 设置窗口

    def open_settings(self):
        """改 API 地址 / 模型名 / 密钥。

        为什么非要有这个面板：浮层版是 pythonw 启动的（没有控制台），
        agent.ensure_api_key() 那套"在终端里问你要 key"根本弹不出来 ——
        key 没配、想换模型、想换成别家的兼容接口，用户就没地方改了。
        """
        win = self._settings_win
        if win is not None and win.winfo_exists():
            win.lift()
            win.focus_force()
            return

        px = self._px
        win = tk.Toplevel(self.root)
        win.title("顾问设置")
        win.configure(bg=theme.PANEL)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        self._settings_win = win

        body = tk.Frame(win, bg=theme.PANEL, padx=px(16), pady=px(14))
        body.pack(fill="both", expand=True)

        def add_field(label, value):
            tk.Label(body, text=label, bg=theme.PANEL, fg=theme.TEXT_DIM,
                     font=self.fonts["small"], anchor="w").pack(fill="x",
                                                                pady=(px(9), px(3)))
            e = tk.Entry(body, bg=theme.INPUT_BG, fg=theme.TEXT,
                         font=self.fonts["input"], relief="flat", width=44,
                         insertbackground=theme.TEXT,
                         highlightthickness=px(1), highlightbackground=theme.BORDER,
                         highlightcolor=theme.ACCENT)
            e.insert(0, value or "")
            e.pack(fill="x", ipady=px(5))
            return e

        # ---- 常用服务商：选一下就把地址和模型名填好（只是省事，不选也行）----
        providers = ["custom"] + [k for k in llm.PROVIDERS if k != "custom"]
        labels = [llm.PROVIDERS[k]["label"] for k in providers]
        cur = (self.config.get("provider") or llm.DEFAULT_PROVIDER).strip().lower()
        prov_var = tk.StringVar(
            value=llm.PROVIDERS.get(cur, llm.PROVIDERS[providers[0]])["label"])
        hint_var = tk.StringVar()

        def set_hint(name):
            """提示行跟着选中的服务商走（密钥去哪儿创建）。"""
            p = llm.preset(name)
            if name == "custom":
                hint_var.set("密钥在你所用服务商的后台创建；接口地址和模型名照它的文档填。")
            elif p["key_url"]:
                hint_var.set("密钥在 " + p["key_url"] + " 创建")
            else:
                hint_var.set("")

        def on_pick(label):
            """选了预设就自动填上地址和模型名 —— 用户还可以接着手改。

            这里引用的 e_url / e_model 是**后面才创建**的。能这么写是因为
            闭包在"被调用时"才去查找这两个名字，而那时它们已经建好了；
            加上 OptionMenu 的回调只在构造时捕获一次（不能事后 configure），
            所以顺序只能是这样。
            """
            idx = labels.index(label) if label in labels else 0
            name = providers[idx]
            p = llm.preset(name)
            if p["base_url"]:
                e_url.delete(0, "end")
                e_url.insert(0, p["base_url"])
            if p["model"]:
                e_model.delete(0, "end")
                e_model.insert(0, p["model"])
            set_hint(name)

        tk.Label(body, text="常用服务商（只是快捷填充，用别家就选「通用」）",
                 bg=theme.PANEL, fg=theme.TEXT_DIM,
                 font=self.fonts["small"], anchor="w").pack(fill="x",
                                                            pady=(px(9), px(3)))
        om = tk.OptionMenu(body, prov_var, *labels, command=on_pick)
        om.configure(bg=theme.INPUT_BG, fg=theme.TEXT, relief="flat",
                     font=self.fonts["small"], highlightthickness=0,
                     activebackground=theme.BORDER, activeforeground=theme.TEXT,
                     borderwidth=0, anchor="w", padx=px(6))
        om["menu"].configure(bg=theme.INPUT_BG, fg=theme.TEXT,
                             font=self.fonts["small"], borderwidth=0)
        om.pack(fill="x")

        e_url = add_field("API 地址", self.config.get("base_url"))
        e_model = add_field("模型名", self.config.get("model"))
        e_key = add_field("API 密钥", self.config.get("api_key"))

        tk.Label(body, textvariable=hint_var, bg=theme.PANEL, fg=theme.TEXT_FAINT,
                 font=self.fonts["small"], anchor="w", justify="left",
                 wraplength=px(360)).pack(fill="x", pady=(px(8), 0))
        set_hint(cur)
        # 说清楚密钥存哪儿 —— 免得用户以为它和别的设置一起躺在 config.json 里，
        # 顺手把 config.json 分享出去。
        tk.Label(body, text="（密钥单独存在 secret.json，不会写进 config.json）",
                 bg=theme.PANEL, fg=theme.TEXT_FAINT,
                 font=self.fonts["small"], anchor="w").pack(fill="x", pady=(px(2), 0))

        def do_save():
            url = e_url.get().strip()
            model = e_model.get().strip()
            key = e_key.get().strip()
            idx = labels.index(prov_var.get()) if prov_var.get() in labels else 0
            provider = providers[idx]
            try:
                agent.save_settings(base_url=url, model=model, api_key=key,
                                    provider=provider)
            except Exception as exc:
                self._append(f"（设置没能存下来：{exc}）\n", "dim")
                return
            self.config["base_url"] = url
            self.config["model"] = model
            self.config["api_key"] = key
            self.config["provider"] = provider
            # 这家的"去哪创建密钥"也要跟着换，否则下次打开设置还显示上一家的地址
            self.config["key_url"] = llm.preset(provider)["key_url"]
            try:
                # 重建一次，新配置立刻生效，不用重启浮层
                self.advisor = agent.Advisor(self.config)
                self._append("（设置已保存%s）\n" % (f"，现在用 {model}" if model else ""),
                             "dim")
            except Exception as exc:
                self._append(f"（存下来了，但这套配置起不来：{exc}）\n", "dim")
            win.destroy()

        btns = tk.Frame(body, bg=theme.PANEL)
        btns.pack(fill="x", pady=(px(16), 0))
        tk.Button(btns, text="取消", command=win.destroy, font=self.fonts["small"],
                  bg=theme.PANEL_TOP, fg=theme.TEXT, relief="flat",
                  activebackground=theme.BORDER, activeforeground=theme.TEXT,
                  borderwidth=0, padx=px(14), pady=px(4)).pack(side="right")
        tk.Button(btns, text="保存", command=do_save, font=self.fonts["small"],
                  bg=theme.ACCENT, fg=theme.TEXT, relief="flat",
                  activebackground=theme.ACCENT, activeforeground=theme.TEXT,
                  borderwidth=0, padx=px(14), pady=px(4)).pack(side="right",
                                                                 padx=(0, px(8)))

        win.bind("<Escape>", lambda _e: win.destroy())
        win.update_idletasks()
        # 摆在浮层左边；放不下就摆到右边
        x = self.root.winfo_x() - win.winfo_width() - px(12)
        if x < 0:
            x = self.root.winfo_x() + self.root.winfo_width() + px(12)
        win.geometry(f"+{max(0, x)}+{self.root.winfo_y()}")

    # -------------------------------------------------- 拖动

    def _drag_start(self, event):
        self._drag_from = (event.x_root, event.y_root)
        self._drag_win = (self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, event):
        dx = event.x_root - self._drag_from[0]
        dy = event.y_root - self._drag_from[1]
        x = self._drag_win[0] + dx
        y = self._drag_win[1] + dy
        self.root.geometry(f"+{x}+{y}")

    # -------------------------------------------------- 输出区

    def _clear(self):
        """只清空对话区（纯界面操作）。"""
        self.answer.configure(state="normal")
        self.answer.delete("1.0", "end")
        self.answer.configure(state="disabled")

    def _reset_conversation(self):
        """Ctrl+L：界面和模型记忆**一起**清空。

        只清界面会留下一个很别扭的状态 —— 窗口空白，但模型还记得上文，
        下一次提问它会接着一个用户看不见的上下文回答。
        （命令行版的 /reset 也是这个语义：清空对话历史，重新开始。）
        """
        self._clear()
        try:
            self.advisor.reset()
        except Exception:
            pass
        self._last_seq = -1        # 让"已读取当前战局"下次重新提示一次

    def _append(self, text, tag=None):
        self.answer.configure(state="normal")
        if tag:
            self.answer.insert("end", text, (tag,))
        else:
            self.answer.insert("end", text)
        self.answer.configure(state="disabled")
        self.answer.see("end")

    def _insert_rich(self, text):
        """处理 **加粗** 和 `行内代码`。"""
        pos = 0
        for m in self.RICH.finditer(text):
            if m.start() > pos:
                self.answer.insert("end", text[pos:m.start()])
            self.answer.insert("end", m.group(1) if m.group(1) is not None
                               else m.group(2), ("bold",))
            pos = m.end()
        if pos < len(text):
            self.answer.insert("end", text[pos:])

    def _render(self, md):
        """把回答渲染到对话区末尾。

        注意这里是**追加**，不是清屏重画 —— 早先这个函数第一句是
        answer.delete("1.0", "end")，结果每次提问都把上一轮的问答抹掉，
        整个窗口变成"只显示最新一条"。实测反馈就是这个。
        """
        px = self._px
        text = tables_to_list(md)
        self.answer.configure(state="normal")
        for raw in text.splitlines():
            line = raw.rstrip()
            if line.startswith("#"):
                self.answer.insert("end", line.lstrip("#").strip() + "\n", ("h",))
            elif not line:
                self.answer.insert("end", "\n")
            else:
                if line.startswith("- "):
                    line = "· " + line[2:]
                elif line.startswith("* "):
                    line = "· " + line[2:]
                self._insert_rich(line + "\n")
        self.answer.configure(state="disabled")
        self.answer.see("end")          # 滚到底，让最新的回答露出来

    # -------------------------------------------------- 提问

    def _submit(self, _event=None):
        question = self.entry_var.get().strip()
        if not question:
            return "break"
        if self._busy:
            self._append("\n（还在回答上一个问题，稍等一下）\n", "dim")
            return "break"

        self.entry_var.set("")      # 清空之后 trace 会把提示重新显示出来

        # 上面还有内容就隔一条线 —— 否则两轮问答会连成一片分不清
        if self.answer.get("1.0", "end").strip():
            self._append("\n" + "─" * 30 + "\n\n", "dim")

        self._append(question + "\n", "user")
        self._busy = True
        threading.Thread(target=self._worker, args=(question,), daemon=True).start()
        return "break"

    def _worker(self, question):
        """在后台线程里跑，免得界面卡住。

        注意这个线程是一次性的：每问一个问题就新开一个。所以退出前要把
        本线程持有的数据库连接还掉（kb 的连接是按线程建的，见 kb.Civ6KB）。
        不还也不会立刻出错——但一个晚上问几十次就会攒下几十套连接。
        """
        try:
            reply = self.advisor.ask(
                question, verbose=False, on_event=self._on_event,
                game_state=self.bridge.last_state,
            )
        except Exception as exc:
            reply = f"出错了：{exc}"
        finally:
            try:
                kb.close_thread_conn()
            except Exception:
                pass
        self.root.after(0, self._on_answer, reply)

    def _on_event(self, _kind, text):
        # 注意：这个回调在后台线程里，碰界面必须切回主线程
        self.root.after(0, self._append, text.strip() + "\n", "tool")

    def _on_answer(self, reply):
        self._busy = False
        self._render(reply)
        self._show_usage()

    def _show_usage(self):
        """报一下这次花了多少 token。

        不报的话，用户对成本完全没有概念。特别值得看的是"命中缓存"那部分 ——
        我们每轮都要发那 7000 字符固定开销（人格设定 + 16 个工具的说明书），
        命中缓存时计费便宜一个数量级，看这个数才知道真实花销。
        """
        adv = self.advisor
        turn = getattr(adv, "last_turn", None) or {}
        total = getattr(adv, "usage_total", None) or {}
        if not turn.get("calls"):
            return

        used = turn.get("prompt", 0) + turn.get("completion", 0)
        all_used = total.get("prompt", 0) + total.get("completion", 0)
        line = f"（本次 {used} token · 会话累计 {all_used} token"
        if total.get("cached"):
            line += f" · 其中 {total['cached']} 命中缓存"
        self._append(line + "）\n", "dim")

        # 上下文快满了就提醒一句，别等 API 甩个报错回来
        if getattr(adv, "last_prompt_tokens", 0) >= 48000:
            self._append("（上下文快满了，建议按 Ctrl+L 重新开始一遍）\n", "dim")

    # -------------------------------------------------- 游戏信号

    def _poll(self):
        try:
            for sig in self.bridge.poll():
                if sig == game_bridge.SIGNAL_TOGGLE:
                    # 游戏里的按钮是"叫它出来"，不是开关。
                    # 之前用的是 toggle：窗口已经开着时再点一下会把它收起来，
                    # 而玩家的预期明明是"再问一次"。收起交给 Esc 就行。
                    self.show()
                    self._announce_state()
                elif sig == game_bridge.SIGNAL_READY:
                    self._append("\n（游戏里的顾问按钮已就绪）\n", "dim")
                elif sig == game_bridge.SIGNAL_STATE_ERROR:
                    # 游戏内导出炸了。不明说的话，玩家只会看到窗口开了、
                    # 但顾问对"你现在什么局面"一无所知，还以为是自己没说清。
                    self._append("（游戏里导出战局时出错了，这次没有局面数据）\n", "dim")
                elif sig == game_bridge.SIGNAL_STATE_EMPTY:
                    self._append("（还没进入地图，读不到战局）\n", "dim")
        except Exception:
            pass
        self.root.after(POLL_MS, self._poll)

    def _announce_state(self):
        """点按钮时顺口汇报一句：这回战局读到没有。

        纯提示，不参与逻辑 —— agent 拿的始终是 bridge.last_state，
        这里就算写错了，问答本身照样正常。
        """
        b = self.bridge
        # 用序号判断"有没有新数据"，而不是每次都念一遍：
        # 同一份数据连着点几次按钮不该刷屏。
        seq = b.state_seq if b.last_state else 0
        if seq == self._last_seq:
            return
        self._last_seq = seq
        if seq == 0:
            self._append("（这回没读到战局，你直接说局面就行）\n", "dim")
        else:
            self._append("（已读取当前战局）\n", "dim")

    HOTKEY = "Ctrl+Alt+A"

    def _poll_hotkey(self):
        """全局热键兜底：Ctrl+Alt+A。

        存在的意义：万一 mod 没装成功、或者游戏内按钮出问题，
        用户至少还有一个办法把窗口叫出来，不至于卡死在没法用。

        用轮询 GetAsyncKeyState 而不是 RegisterHotKey：
        后者要把消息钩子接进 tkinter 的事件循环，麻烦且容易和游戏打架。
        轮询每 120ms 一次，开销可以忽略。
        """
        if sys.platform.startswith("win"):
            try:
                u = ctypes.windll.user32
                down = bool(
                    (u.GetAsyncKeyState(0x11) & 0x8000)      # Ctrl
                    and (u.GetAsyncKeyState(0x12) & 0x8000)  # Alt
                    and (u.GetAsyncKeyState(0x41) & 0x8000)  # A
                )
                # 只在"从没按到按下"这一瞬间触发，按住不放不会反复开关
                if down and not self._hotkey_down:
                    self.toggle()
                self._hotkey_down = down
            except Exception:
                pass
        self.root.after(120, self._poll_hotkey)

    # -------------------------------------------------- 启动

    def _startup_notice(self):
        if not self.bridge.exists():
            self._append("还没找到游戏日志文件：\n", "dim")
            self._append(self.bridge.path + "\n\n", "dim")
            self._append("先启动一次文明6，这个文件才会出现。\n", "dim")
        else:
            self._append("已就绪\n", "h")
            self._append("在游戏里点右上角的顾问按钮，这个窗口就会浮出来。\n\n", "dim")
            self._append(f"游戏里没反应的话，按 {self.HOTKEY} 也能叫我。\n\n", "dim")
            self._append("要提问：点最下面那一行输入框，打完按回车。\n"
                         f"（就是写着「{self.PLACEHOLDER}」的那一行）\n\n", "dim")
            self._append("换模型 / 改密钥：点标题栏的「设置」。\n\n", "dim")
            self._append("按 Esc 收起这个窗口（程序会在后台继续待命）。\n"
                         "问答会一直留着、可以往上翻；想从头开始按 Ctrl+L。\n", "dim")

        # 程序不预设任何一家服务商了，所以"没填过接口/模型"是第一次使用时的常态 ——
        # 直接把设置面板打开，别让用户先撞一次报错才知道要填什么。
        if not (self.config.get("base_url") and self.config.get("model")):
            self._append("\n第一次用，先把这两项填上：\n"
                         "  ① API 地址 —— 你用哪家服务商，填它文档里的接口地址\n"
                         "  ② 模型名   —— 例如 deepseek-chat / qwen-plus / gpt-4o-mini\n"
                         "  （不想查地址就在「常用服务商」里选一家，会自动填好）\n", "dim")
            self.root.after(400, self.open_settings)
        elif not (self.config.get("api_key") or "").strip():
            self._append("\n还差一个 API 密钥：点标题栏的「设置」填上。\n", "dim")

        self.root.deiconify()
        self._visible = True
        # 一浮出来就把光标送进输入框 —— 省得用户还要先点一下才知道能打字。
        # 顺带也让输入条亮成"有焦点"的样子（见 _paint_input_area）。
        self._grab_keyboard()

    def run(self):
        self._poll()
        self._poll_hotkey()
        self.root.after(150, self._startup_notice)
        self.root.mainloop()


# ------------------------------------------------------------------ 入口

def ensure_api_key(config):
    """没配 key 就当场问一次。返回 True 表示可以继续。"""
    if (config.get("api_key") or "").strip():
        return True
    root = tk.Tk()
    root.withdraw()
    key = simpledialog.askstring(
        "需要 API 密钥",
        "这是第一次运行，需要一个 API 密钥。\n\n"
        "把你在接口服务商后台创建的密钥粘贴到下面：",
        parent=root,
    )
    root.destroy()
    if not key or not key.strip():
        return False
    agent.save_api_key(key.strip())
    config["api_key"] = key.strip()
    return True


def _write(path, text):
    """写状态文件。写不进去也不能反过来影响启动。"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def _stamp_started():
    """留一个"我起来了"的记号，给 bat 看。"""
    _write(STARTED_FILE,
           "顾问已启动\n"
           f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
           f"进程号：{os.getpid()}\n"
           f"工作目录：{os.getcwd()}\n"
           f"解释器：{sys.executable}\n")


def main():
    # pythonw 启动时没有控制台，sys.stdout 是 None，任何 print 都会炸
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    try:
        config = agent.load_config()
        if not ensure_api_key(config):
            return 1

        # 每次启动顺手刷新本地化补丁（约 0.6 秒，静默）
        agent.refresh_text_patch()

        ov = Overlay(config)
        # 能走到这里说明窗口真的建起来了。留个记号再进消息循环。
        _stamp_started()
        ov.run()
        return 0
    except BaseException:
        # pythonw 下异常不会有任何输出，进程就那么没了 —— 用户端看到的
        # 就是"双击 bat，黑窗一闪，什么都没发生"。所以堆栈一定要落盘。
        _write(ERROR_FILE,
               "顾问启动失败\n"
               f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
               + traceback.format_exc())
        raise


def show_error_box():
    """启动失败时弹一个 Windows 消息框，把原因摆到用户面前。

    为什么不用 bat 的 echo 来显示：bat 的编码和系统代码页纠缠不清，
    中文几乎必然变乱码。MessageBoxW 是 Unicode 的，怎么写都不会错。
    """
    if os.path.exists(ERROR_FILE):
        with open(ERROR_FILE, encoding="utf-8") as f:
            body = f.read().strip()
    else:
        body = ("什么线索都没留下 —— 说明连 Python 都没跑起来，\n"
                "多半是最后一个兜底的解释器也找不到。")
    try:
        ctypes.windll.user32.MessageBoxW(
            0, "顾问启动失败：\n\n" + body[-2500:],
            "文明6 顾问", 0x10)          # 0x10 = 红色错误图标
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--show-error":
        show_error_box()
        sys.exit(1)
    sys.exit(main())
