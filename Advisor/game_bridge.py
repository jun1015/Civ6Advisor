"""Lua.log 信号监听 —— 游戏里唯一的"对外喊话"通道。

背景：游戏内的 Lua 沙箱是全封闭的（没有网络、没有文件读写、没有进程通信）。
游戏里唯一能碰到外面世界的动作，就是 print 一行字到 Lua.log。
所以游戏内的 mod 只能喊，不能听。

这个模块负责盯着那个文件的新增内容，把两类东西挖出来：

  1. 信号（一行搞定）
        >>>ADVISOR:TOGGLE<<<     玩家点了顾问按钮
        >>>ADVISOR:READY<<<      mod 加载完成，只用于排查
  2. 战局快照（多行，被两个标记夹住）
        >>>ADVISOR:STATE<<<
        回合=42 时代=中世纪 文明=日本
        ...
        >>>ADVISOR:STATEEND<<<

   快照是玩家点按钮的瞬间由游戏导出的，所以它总是新鲜的。
   poll() 每次返回新增的信号名，并把最近一份快照存进 self.last_state。

两个必须处理的现实问题：
  1. 启动时不能把历史信号当成新信号 —— 所以从文件末尾开始读
  2. 游戏重启会重写整个日志，文件会变小 —— 检测到就把读取位置归零
"""

import os
import re

# 与游戏内 mod 的 AdvisorLauncher.lua 严格对应，改一边就要改另一边
SIGNAL_RE = re.compile(r">>>ADVISOR:([A-Z_]+)<<<")
STATE_BEGIN = ">>>ADVISOR:STATE<<<"
STATE_END = ">>>ADVISOR:STATEEND<<<"

# 早期版本的 Lua 用的是连字符（STATE-EMPTY），而上面的正则只认 [A-Z_]，
# 于是那行会被**静默丢掉**。读的时候统一换算成下划线，两种写法都认。
EMPTY_MARK = ">>>ADVISOR:STATE_EMPTY<<<"
EMPTY_MARK_LEGACY = ">>>ADVISOR:STATE-EMPTY<<<"

SIGNAL_TOGGLE = "TOGGLE"   # 玩家点了顾问按钮
SIGNAL_READY = "READY"     # mod 加载完成（只用于诊断提示）
SIGNAL_STATE_ERROR = "STATE_ERROR"   # 游戏内拼快照时抛异常了
SIGNAL_STATE_EMPTY = "STATE_EMPTY"   # 还没进入战局，没东西可导

# 快照块异常时的兜底：最多攒这么多行就放弃，防止内存无限涨
_MAX_STATE_LINES = 200


def find_log_path():
    """定位 Lua.log。

    注意它在 AppData\\Local，不在 Documents\\My Games，
    也不在游戏安装目录 —— 这是三个不同的地方，容易搞混。
    """
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "Firaxis Games",
                        "Sid Meier's Civilization VI", "Logs", "Lua.log")


class LogTailer:
    """增量读取 Lua.log，只关心新增内容里的信号和快照。

    用法：
        t = LogTailer()
        ...
        for name in t.poll():
            if name == SIGNAL_TOGGLE:
                state = t.last_state      # 此刻已经是最新的一份
    """

    def __init__(self, path=None, skip_existing=True):
        self.path = path or find_log_path()
        self.offset = 0
        self.last_state = None          # 最近一份战局快照的原始文本
        self.state_seq = 0              # 快照序号，每次收到完整一份就 +1
        self._collecting = False        # 正在收集快照块？
        self._buf = []
        if skip_existing:
            # 从当前位置开始，别把上次游戏留下的信号当成新的
            try:
                self.offset = os.path.getsize(self.path)
            except OSError:
                self.offset = 0

    def exists(self):
        return os.path.isfile(self.path)

    def poll(self):
        """返回本次新增内容里出现的信号名列表（可能为空）。"""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []

        if size < self.offset:
            # 文件变小了 = 游戏重启重写了日志，从头来过
            self.offset = 0
            self._reset_block()

        if size == self.offset:
            return []

        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return []

        if not data:
            return []

        # 只处理"写完的整行"：末尾若没有换行符，说明这行还在写，
        # 留到下一轮再读。否则会把半截信号丢掉。
        cut = data.rfind(b"\n")
        if cut < 0:
            return []
        usable = data[:cut + 1]
        self.offset += len(usable)

        text = usable.decode("utf-8", errors="replace")
        text = text.replace(EMPTY_MARK_LEGACY, EMPTY_MARK)
        return self._consume(text)

    # ------------------------------------------------------------------ 内部

    def _reset_block(self):
        self._collecting = False
        self._buf = []

    def _consume(self, text):
        """逐行扫描，分离出信号和快照内容。

        快照块的状态跨调用保留 —— 万一日志是分两次写进来的，
        下一轮还能接着收，不会丢掉半截数据。
        """
        signals = []

        for line in text.splitlines():
            if STATE_BEGIN in line:
                self._collecting = True
                self._buf = []
                continue

            if STATE_END in line:
                self._collecting = False
                body = "\n".join(self._buf).strip()
                if body:
                    self.last_state = body
                    self.state_seq += 1
                self._buf = []
                continue

            if self._collecting:
                stripped = line.strip()
                if stripped:
                    self._buf.append(stripped)
                    if len(self._buf) > _MAX_STATE_LINES:
                        # 只进不出说明块没闭合，别把内存吃光
                        self._reset_block()
                continue

            for m in SIGNAL_RE.finditer(line):
                signals.append(m.group(1))

        return signals
