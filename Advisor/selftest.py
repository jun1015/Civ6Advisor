"""离线自检：改完这里的代码，跑一遍确认没把东西碰坏。

全程不联网、不碰游戏，用的都是自己构造的数据：

  第一部分  LogTailer 解析 Lua.log
            含几个真实会发生的坑：半截行、游戏重启导致日志重写、
            空快照、块没闭合、快照里混进游戏自己的日志。

  第二部分  快照一路走到 Agent 的消息结构
            重点验证"新快照顶掉旧的"，别让对话历史越攒越长。

  第三部分  kb_list 列名单
            模型认不出俗称时的兜底（玩家说「奶妈」，名单一看就知道是「医疗兵」）。
            要列得全、不能混进没翻译的裸 ID、也不能长到被工具层截断。

  第四部分  回归测试（每个都对应一个真踩过的坑）
            ① 跨线程查库：浮层每问一个问题就新开一个线程
            ② kb_compare 不许给空表
            ③ 游戏内喊话里的 STATE_EMPTY

  第五部分  静态审计：扫 kb.py，不许读不存在的列
            （读错列不报错，只是那行内容永远不出现，最难发现）

  第六部分  启动脚本
            对应真实故障：双击 bat 后黑窗一闪，什么都没发生。
            查出来是两件事叠加 —— 商店版 Python 的"应用执行别名"
            经 start 启动会直接消失，而 pythonw 崩了又不会说话。

  第七部分  游戏内界面文件
            对应真实故障：mod 装了、日志里也有 READY，但游戏里"没有多余的按钮"。
            原因是这类 <AddUserInterfaces> 界面的父容器在 InGame.xml 里是
            <Container ID="AdditionalUserInterfaces"/> —— 空容器，没有 Size
            也没有 Anchor，相对它写 Anchor="R,T" 会落到屏幕外。

用法：python selftest.py
"""

import os
import re
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent        # noqa: E402
import game_bridge  # noqa: E402

_PASSED = []
_SKIPPED = []


def ok(name, cond, extra=""):
    if cond:
        _PASSED.append(name)
        print("   ok   " + name)
    else:
        print("   FAIL " + name + ("   <- " + extra if extra else ""))
        raise SystemExit(1)


def skip(name, why):
    """记一条"跳过"。

    存在的原因：有几项检查要读游戏自己的数据库（规则库那部分），而**克隆这个
    仓库的人不一定装了文明6**。以前的情况是直接抛 FileNotFoundError、一屏
    traceback —— 看着像代码坏了，其实只是没装游戏。

    跳过**只用于"环境缺东西"**，绝不用来掩盖失败：
    数量会单独统计并在结尾打出来，不会混进"全部通过"里。
    """
    _SKIPPED.append(f"{name}（{why}）")
    print(f"   跳过 {name}   <- {why}")


_GAME_OK = None


def game_data_available():
    """游戏缓存目录在不在。不在就跳过依赖它的检查。"""
    global _GAME_OK
    if _GAME_OK is None:
        try:
            import kb
            kb.find_cache_dir()
            _GAME_OK = True
        except Exception:
            _GAME_OK = False
    return _GAME_OK


STATE_BLOCK = (
    ">>>ADVISOR:STATE<<<\n"
    "回合=42 时代=中世纪 文明=日本 领袖=北条时宗\n"
    "金币=320 收支=+12 科技产出=45 文化产出=28\n"
    "科技=城堡(45%) 市政=封建制度(30%)\n"
    "交战=无\n"
    "城市数=2 总人口=13\n"
    "城市 京都 首都 人口8 生产:弓手(3回合) 成长:4回合\n"
    "城市 大阪 人口5 生产:粮仓(5回合)\n"
    "单位=剑客×3 投石车×2 共5\n"
    ">>>ADVISOR:STATEEND<<<\n"
)


def part1_tailer(log):
    print("[1] 解析 Lua.log")
    t = game_bridge.LogTailer(path=log, skip_existing=True)

    ok("启动时不重放历史信号", t.poll() == [] and t.last_state is None)

    _append(log, STATE_BLOCK + ">>>ADVISOR:TOGGLE<<<\n")
    sigs = t.poll()
    ok("只报出 TOGGLE", sigs == ["TOGGLE"], "实得 %r" % (sigs,))
    ok("快照已捕获", bool(t.last_state) and "回合=42" in t.last_state)
    ok("快照里不留标记", "<<<" not in t.last_state)
    ok("行数正确（8 行）", len(t.last_state.splitlines()) == 8,
       "实得 %d" % len(t.last_state.splitlines()))

    _append(log, ">>>ADVISOR:STATE<<<\n回合=43 时代=中世纪")
    ok("没有换行时不处理半截行", t.poll() == [])
    ok("旧快照没被污染", "回合=42" in t.last_state)
    _append(log, "\n>>>ADVISOR:STATEEND<<<\n")
    ok("补上换行后不误报信号", t.poll() == [])
    ok("新快照覆盖旧的", "回合=43" in t.last_state
       and "回合=42" not in t.last_state)

    _append(log, ">>>ADVISOR:STATE<<<\n>>>ADVISOR:STATEEND<<<\n"
                ">>>ADVISOR:TOGGLE<<<\n")
    ok("空快照不覆盖旧数据", t.poll() == ["TOGGLE"] and "回合=43" in t.last_state)

    _append(log, ">>>ADVISOR:STATE_ERROR<<< attempt to index a nil value\n"
                ">>>ADVISOR:TOGGLE<<<\n")
    sigs = t.poll()
    ok("能认出导出错误", "STATE_ERROR" in sigs, "实得 %r" % (sigs,))
    ok("TOGGLE 同时报出", "TOGGLE" in sigs)

    _write(log, "新一局开始\n>>>ADVISOR:STATE<<<\n回合=1 文明=罗马\n"
                ">>>ADVISOR:STATEEND<<<\n>>>ADVISOR:TOGGLE<<<\n")
    sigs = t.poll()
    ok("日志被重写后仍能读", sigs == ["TOGGLE"], "实得 %r" % (sigs,))
    ok("新快照生效", "罗马" in t.last_state)

    _append(log, ">>>ADVISOR:STATE<<<\n" + "填充行\n" * 260)
    ok("块没闭合时不会吃光内存", t.poll() == [] and t._buf == [])
    ok("旧快照仍然保留", "罗马" in t.last_state)

    _append(log, ">>>ADVISOR:STATE<<<\n回合=50\nPlotInfo: 噪声\n"
                "金币=100\n>>>ADVISOR:STATEEND<<<\n>>>ADVISOR:TOGGLE<<<\n")
    sigs = t.poll()
    ok("中途混入的日志被一起收下", "PlotInfo: 噪声" in t.last_state)
    ok("信号不受噪声影响", sigs == ["TOGGLE"])


def part2_agent():
    print("[2] 快照进入 Agent 的消息结构")

    cfg = {"api_key": "fake", "base_url": "http://127.0.0.1:1/v1",
           "model": "fake-model", "max_steps": 1}
    adv = agent.Advisor(cfg)
    ok("初始只有人格设定", len(adv.messages) == 1)

    raw = STATE_BLOCK.split(">>>ADVISOR:STATEEND")[0].split(">>>ADVISOR:STATE<<<\n", 1)[1].strip()
    adv._set_game_state(raw)
    ok("插在人格设定之后", len(adv.messages) == 2
       and adv.messages[0]["role"] == "system"
       and adv.messages[1]["role"] == "system")
    ok("带上了来源说明", "【玩家当前战局" in adv.messages[1]["content"])
    ok("原始数据没被改动", "回合=42" in adv.messages[1]["content"])

    adv.messages.append({"role": "user", "content": "我接下来该干嘛"})
    ok("对话历史追加正常", len(adv.messages) == 3)
    adv._set_game_state("回合=43 文明=日本")
    ok("新快照顶掉旧的，不累积", len(adv.messages) == 3
       and "回合=43" in adv.messages[1]["content"]
       and "回合=42" not in adv.messages[1]["content"])
    ok("玩家的话还在原位", adv.messages[2]["content"] == "我接下来该干嘛")

    adv._set_game_state(raw)
    ok("重复注入不叠加", len(adv.messages) == 3)

    adv._set_game_state(None)
    ok("空快照干净移除", len(adv.messages) == 2
       and adv.messages[1]["content"] == "我接下来该干嘛")

    adv.reset()
    ok("reset 后状态归零", len(adv.messages) == 1 and adv._state_at is None)


def part3_kb_list():
    print("\n[3] kb_list 列名单")
    if not game_data_available():
        skip("kb_list 列名单", "没找到文明6的缓存目录")
        return
    import kb
    import tools

    # ① 每个类型都要能列出内容
    empty = [k for k in kb.ENTITIES if "共 " not in kb.list_names(k)]
    ok("每个类型都能列出条目", not empty, "空的：" + "、".join(empty))

    # ② 关键场景：玩家说「奶妈」，名单里得找得到「医疗兵」——
    #    这正是当初决定加这个工具的理由。
    unit_list = kb.list_names("unit")
    ok("单位名单含「医疗兵」（俗称能对上）", "医疗兵" in unit_list)
    ok("单位名单确实是全量（>100 条）", unit_list.count("、") > 100)

    # ③ 没翻出中文的裸 ID 不能混进来（特性表最容易出这问题，原来 2985 字全是噪音）
    ok("单位名单无 LOC_ 裸 ID", "LOC_" not in unit_list)
    ok("特性名单也无 LOC_ 裸 ID", "LOC_" not in kb.list_names("trait"))

    # ④ 非法类型给友好提示，而不是抛异常
    ok("非法类型返回提示而非异常", "未知的类型" in kb.list_names("不存在的类型"))

    # ⑤ 经工具层能拿到，且不会被截断（截断会让模型看到半份名单）
    out = tools.execute("kb_list", {"kind": "unit"})
    ok("经工具层调用能拿到名单", "医疗兵" in out)
    ok("单份名单不会被截断", "已截断" not in out)
    ok("工具报错不抛异常（缺参数）", "错误" in tools.execute("kb_list", {}))

    # ⑥ 工具参数里的类型枚举必须和 ENTITIES 对齐，否则模型会传进不存在的类型
    spec = [s for s in tools.TOOL_SPECS if s[0] == "kb_list"]
    ok("kb_list 已注册进工具表", len(spec) == 1)
    ok("参数枚举与 ENTITIES 一致",
       set(spec[0][2]["kind"]["enum"]) == set(kb.ENTITIES.keys()))


def part4_regressions(log):
    print("\n[4] 回归测试")
    import kb
    import tools

    def game_checks():
        # 这一段要读游戏自己的数据库，所以整段是可跳过的。
        # ---- ① 跨线程查库 ---------------------------------------------------
        # 复刻浮层的真实用法：每问一个问题就新开一个 threading.Thread。
        # 曾经的坑：kb 是全局单例，SQLite 连接只认创建它的那个线程，
        # 于是"第一个问题正常、从第二个问题起所有查询全部报错"。
        results = {}

        def worker(tag, name):
            try:
                results[tag] = kb.unit(name).splitlines()[0]
            except Exception as e:
                results[tag] = "%s: %s" % (type(e).__name__, e)

        for i, name in enumerate(("剑客", "弓箭手"), start=1):
            t = threading.Thread(target=worker, args=(i, name))
            t.start()
            t.join()
            kb.close_thread_conn()      # 浮层每问完一次也是这么收尾的

        ok("第 1 个线程能查库", results.get(1, "").startswith("# 剑客"),
           "实得 %r" % results.get(1))
        ok("第 2 个线程（新线程）也能查库", results.get(2, "").startswith("# 弓箭手"),
           "实得 %r" % results.get(2))

        # ---- ② kb_compare 不许给空表 ---------------------------------------
        # 工具描述里把 12 个 kind 都列给了模型，就必须每个都能出内容。
        # 曾经的坑：只有 5 个 kind 配了字段表，另外 7 个排出一张空表格。
        #
        # 名字不写死，直接从名单里取前两个：游戏的官方中文名跟直觉差得远
        # （是「剑客」不是「剑士」，是「君主制」不是「君主国」），
        # 手抄一堆名字进测试只会变成维护负担。
        def two_names(kind):
            lines = kb.list_names(kind).splitlines()[1:]
            items = [x for line in lines for x in line.split("、")]
            items = [x for x in items if x and "……" not in x]
            return ",".join(items[:2])

        spec = [s for s in tools.TOOL_SPECS if s[0] == "kb_compare"][0]
        declared = list(spec[2]["kind"]["enum"])

        empty, skipped = [], []
        for kind in declared:
            names = two_names(kind)
            if "," not in names:
                skipped.append(kind)        # 库里只列得出一个名字，没法比
                continue
            try:
                out = kb.compare(names, kind)
            except Exception as e:
                empty.append("%s(%s)" % (kind, e))
                continue
            has_row = any(l.startswith("|") and "---" not in l for l in out.splitlines())
            has_section = "## " in out      # 描述模式（领袖/文明/信条走这条路）
            if not (has_row or has_section):
                empty.append("%s[%s]" % (kind, names))
        ok("对比工具对每种类型都能给出内容", not empty, "空的：" + "、".join(empty))
        ok("每个 kind 都取到了两个名字", not skipped, "取不到的：" + "、".join(skipped))

        # 名字解析失败要说出来，不能默默少一列
        out = kb.compare("剑客,奶妈", "unit")
        ok("解析不出的名字会明说", "奶妈" in out and "跳过" in out)

    if game_data_available():
        game_checks()
    else:
        skip("跨线程查库 / kb_compare 对比工具", "没找到文明6的缓存目录")

    # ---- ③ STATE_EMPTY --------------------------------------------------
    # 曾经的坑：Lua 发的是连字符版 STATE-EMPTY，而识别用的是 [A-Z_]+，
    # 那行会被静默丢掉，玩家点了按钮却什么提示都没有。
    _write(log, "旧内容\n")
    t2 = game_bridge.LogTailer(path=log, skip_existing=True)
    _append(log, ">>>ADVISOR:STATE_EMPTY<<<\n")
    ok("认得 STATE_EMPTY", "STATE_EMPTY" in t2.poll())

    _write(log, "旧内容\n")
    t3 = game_bridge.LogTailer(path=log, skip_existing=True)
    _append(log, ">>>ADVISOR:STATE-EMPTY<<<\n")
    ok("旧的连字符写法也认得（兼容没同步 mod 的机器）",
       "STATE_EMPTY" in t3.poll())


def part5_dead_fields():
    print("\n[5] 静态审计：不许读不存在的列")
    # 这类 bug 不报错、不崩，只是那一行 _line 永远拿到 None，
    # 输出里静悄悄地少一行内容，谁也不会发现。
    # 曾经一次查出 9 处：resource() 里的"战略/奢侈/加成/产出类型/开采改良"
    # 五行读的都是 Resources 表里不存在的列，等于从来没输出过。
    if not game_data_available():
        skip("静态审计（对照 schema 查死列）", "没找到文明6的缓存目录")
        return
    import re
    import kb

    src = open(os.path.join(HERE, "kb.py"), encoding="utf-8").read()
    lines = src.splitlines()

    bounds = []
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)def (\w+)\s*\(", ln)
        if m:
            bounds.append((len(m.group(1)), m.group(2), i))
    bounds.append((-1, "<eof>", len(lines)))

    bad = []
    for idx in range(len(bounds) - 1):
        _ind, name, start = bounds[idx]
        _ind2, _n2, end = bounds[idx + 1]
        body = "\n".join(lines[start:end])

        tables = set(re.findall(r'_row\(\s*"(\w+)"', body))
        tables |= set(re.findall(r'SELECT \* FROM "(\w+)"', body))
        getters = set(re.findall(r'r\.get\(\s*"(\w+)"\s*\)', body))
        # 一个方法只碰一张表时才能确定 r 是哪张表；碰多张的跳过，别误报
        if len(tables) != 1 or not getters:
            continue
        table = tables.pop()
        cols = set(kb.kb().cols(table))
        for g in sorted(getters - cols):
            bad.append("%s: %s.%s" % (name, table, g))

    ok("没有读不存在的列（死字段）", not bad, "、".join(bad))

    # 同一个游标上边遍历边再查，会把外层结果集冲掉：循环跑到一半就结束。
    # 表现极其隐蔽（_trait_lines 就是这么让罗斯福的特性整段消失的）。
    # 规矩：要么 fetchall() 之后再遍历，要么用另一个游标。
    risky = []
    for idx in range(len(bounds) - 1):
        _ind, name, start = bounds[idx]
        _ind2, _n2, end = bounds[idx + 1]
        body = lines[start:end]
        for i, ln in enumerate(body):
            if not re.search(r"for\b.*\bin\b.*self\.(gc|lc)\.execute\(", ln):
                continue
            # 往后面找这个循环体里还有没有别的 execute
            indent = len(ln) - len(ln.lstrip())
            for nxt in body[i + 1:]:
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                    break
                if "self.gc.execute" in nxt or "self.lc.execute" in nxt:
                    risky.append("%s（第 %d 行）" % (name, start + i + 1))
                    break
    ok("没有「边遍历游标边查询」的写法", not risky, "、".join(risky))


def part6_launcher():
    """启动脚本本身也要守住 —— 这里的坑全是"一点报错都没有"的那种。

    真实故障：双击 bat，黑窗一闪，什么都没发生。查出来是两件事叠加：
      · 商店版 Python 在 WindowsApps 下是个"应用执行别名"（0 字节跳转
        文件），经 start 启动时进程会直接消失；
      · pythonw 没有控制台，崩了也不说话，外面完全看不出发生了什么。
    下面几条必须一直成立，否则那个故障会悄悄回来。
    """
    print("\n[6] 启动脚本")
    with open(os.path.join(HERE, "launch_overlay.bat"),
              "r", encoding="utf-8", errors="replace") as f:
        bat = f.read()

    # 注释行里也提到了 start，所以只看真正会执行的命令行
    live = [ln for ln in bat.splitlines()
            if ln.strip() and not ln.strip().lower().startswith("rem")]

    started = [ln for ln in live if re.match(r"\s*start\b", ln, re.I)]
    ok("启动脚本没用 start 调 pythonw（别名经 start 启动会消失）",
       not started, "可疑行：" + " | ".join(started))

    # 绝对路径有两种写法：直接 %~dp0，或先 set 成 %DIR% 再用
    calls = [ln for ln in live if "overlay.py" in ln]
    bad = [ln for ln in calls
           if "%~dp0" not in ln and "%DIR%" not in ln]
    ok("启动脚本用绝对路径调 overlay.py（别名不保证继承工作目录）",
       bool(calls) and not bad,
       ("没写全路径：" + " | ".join(bad)) if bad else "一行调用都没找到")

    with open(os.path.join(HERE, "overlay.py"), encoding="utf-8") as f:
        ov = f.read()

    ok("浮层崩了会把堆栈落盘（pythonw 下看不到任何输出）",
       "ERROR_FILE" in ov and "traceback.format_exc()" in ov)
    ok("浮层起来后会留标记，供 bat 判断成败",
       "STARTED_FILE" in ov and "_stamp_started" in ov)
    ok("失败原因交给 Python 弹窗显示（bat 的 echo 中文会乱码）",
       "--show-error" in ov and "show_error_box" in ov)
    ok("浮层启动后不再自动隐藏（否则看着就像闪退）",
       "AUTOHIDE_MS" not in ov)


def part7_mod_ui():
    """游戏内界面：按钮到底摆在哪。

    这里每条都对应一个"不报错但功能没了"的坑 —— 游戏不会告诉你
    按钮画到了屏幕外，也不会告诉你贴图名拼错了。
    """
    mod = os.path.normpath(os.path.join(HERE, "..", "AdvisorMod",
                                        "Civ6AdvisorBridge"))
    with open(os.path.join(mod, "UI", "Additions", "AdvisorLauncher.xml"),
              encoding="utf-8") as f:
        xml = f.read()
    with open(os.path.join(mod, "UI", "Additions", "AdvisorLauncher.lua"),
              encoding="utf-8") as f:
        lua = f.read()
    with open(os.path.join(mod, "Civ6AdvisorBridge.modinfo"),
              encoding="utf-8") as f:
        info = f.read()

    # 只审真实代码：两个文件的注释里都**故意**写了反面例子做说明，
    # 不剥掉就会误报。
    xml_code = re.sub(r"<!--.*?-->", "", xml, flags=re.S)
    lua_code = re.sub(r"--\[\[.*?\]\]", "", lua, flags=re.S)
    lua_code = re.sub(r"--[^\n]*", "", lua_code)

    # 父容器是空容器，相对它算角落锚点会落到屏幕外。
    ok("界面 XML 没用角落锚点（父容器是空容器，会落到屏幕外）",
       not re.search(r'Anchor="[RL],[TB]"', xml_code))
    ok("界面 XML 的兜底锚点是居中（两条路都失败时也看得见）",
       'Anchor="C,C"' in xml_code)

    ok("界面脚本用 LookUpControl 把按钮挂进顶栏按钮栈",
       "/InGame/TopPanel/RightContents" in lua_code
       and "LookUpControl" in lua_code)
    ok("跨上下文改父级包了容错（ChangeParent 可能直接抛异常）",
       re.search(r"Safe\(function\(\)\s*button:ChangeParent", lua_code)
       is not None)
    ok("界面脚本有按屏幕尺寸算坐标的退路",
       "GetScreenSizeVal" in lua_code and "SetOffsetX" in lua_code)

    # 贴图名写错不会报错，只会得到一个透明按钮。
    ok("按钮贴图用的是原版真实存在的贴图名",
       'Texture="TopBar_CivilopediaButton"' in xml_code)

    ok("modinfo 把界面 xml 和 lua 都声明全了（漏了就不发货）",
       info.count("UI/Additions/AdvisorLauncher.xml") >= 2
       and info.count("UI/Additions/AdvisorLauncher.lua") >= 2)

    # ---- 实测补的：按钮"日志说挂上了，屏幕上却没有" ----
    # 这几种原因全都**不报错**，游戏也不会给任何提示，只能靠静态检查兜住。

    # ① Safe() 只转交 pcall 的第一个返回值 → 多返回值的 API 静默失真。
    #    真实故障：UIManager:GetScreenSizeVal() 明明返回 (宽, 高)，
    #    高度被 pcall 截断丢掉 → 每次都打印"屏幕尺寸取不到"。
    ok("Safe() 转交 pcall 的两个返回值（多返回值 API 才不会被截断）",
       re.search(r"local\s+ok\s*,\s*a\s*,\s*b\s*=\s*pcall\(", lua_code)
       is not None
       and re.search(r"return\s+a\s*,\s*b", lua_code) is not None)

    # ② 跨上下文挂载有时机问题：Initialize 那一刻顶栏常常还没建好；
    #    更坏的是拿到一棵马上被重建的旧树 —— ChangeParent 不报错，
    #    按钮却随旧树一起销毁。所以必须能重试，不能"试一次就算"。
    ok("挂按钮有时序重试（加载完成事件或逐帧重试），不是只试一次",
       "LoadScreenClose" in lua_code or "SetUpdate" in lua_code)
    ok("重试在真正挂上后才停（成功即停）",
       "attached" in lua_code and "if attached then return end" in lua_code)

    # 🚨 这里踩过一个特别隐蔽的坑，务必盯住：
    #    PlaceButton 里把"退路"也当成成功返回 → 调用方 placed = true → 重试全停
    #    → LoadScreenClose 那次补挂永远不会发生 → 按钮永远卡在退路上。
    #    而退路位置在顶栏那一条里，被顶栏自己的背景图盖着。
    #    表现：Lua.log 写着"按钮就位"，屏幕上什么都没有 —— 不报错，最难查。
    ok("退路不算成功（算成功重试就停摆，按钮会永远卡在退路上）",
       re.search(r"if why ~= nil then return true, why end", lua_code) is not None
       and re.search(r"if why ~= nil then return false, why end", lua_code) is not None)
    ok("等待挂载时机的注册不被退路挡住（不能写在 if 里）",
       re.search(r"\n    Safe\(function\(\)\s*\n\s*Events\.LoadScreenClose\.Add",
                 lua_code) is not None
       and "if not placed then" not in lua_code)
    ok("退路位置让开顶栏那一条（落在顶栏里会被背景图盖住）",
       "FALLBACK_BELOW_BAR" in lua_code)

    # 2026-10-03 实测复查：退路会把按钮的锚点/偏移改成屏幕绝对值（如 1198,-748）。
    # 挂进顶栏按钮栈后若不还原，栈算出的基准位置会被这个巨大偏移顶到屏幕外 ——
    # 表现和坑 6 一样："日志写着已挂进顶栏按钮栈，屏幕上什么都没有"。
    # 对照证据：初始化时就挂上的那次（退路没跑过）偏移是 0,0，看得见。
    ok("挂进栈时把退路改过的偏移归零（否则带着退路坐标挂进去 → 屏幕外）",
       "button:SetOffsetX(HOME_OFFSET_X" in lua_code)
    # 2026-10-07 实测：锚点用了自己的 C,C（XML 里的兜底值），而栈里原版按钮
    # 都不写 Anchor、用的是默认值 —— 于是错开半个身位（29 的一半 ≈ 14），
    # 表现为"按钮和顶栏里的字有一点点重合"。
    # 正解：挂载时照抄原版按钮的锚点，而不是还原 XML 里的值。
    ok("挂进栈时锚点照抄原版按钮（自己的 C,C 会错开半个身位 → 和旁边的字重合）",
       "StackChildAnchor" in lua_code
       and "/InGame/TopPanel/RightContents/CivpediaButton" in lua_code
       and "button:SetAnchor(usedAnchor)" in lua_code)
    ok("锚点读不到时有 L,T 兜底（原版不写 Anchor 时的默认值就是它）",
       '"L,T"' in lua_code)
    ok("原始锚点/偏移在任何摆放动作之前就记下来（摆完再记 = 记到了退路的值）",
       lua_code.index("HOME_ANCHOR == nil") < lua_code.index('Attempt("初始化")'))
    ok("挂进栈后让栈重算布局（栈的布局早跑完了，后加的孩子不会自动归位）",
       "stack:CalculateSize()" in lua_code)
    ok("挂载日志会报出实际用的锚点（下次错位能直接看日志）",
       "锚点=" in lua_code and "usedAnchor" in lua_code)
    ok("诊断日志报出锚点和偏移，偏移过大时直接给判词",
       "锚点=" in lua_code and "offVerdict" in lua_code)

    # ③ 顶层 Context 的 Size 写在 XML 里无效，必须在 Lua 里显式撑开，
    #    否则 Anchor="C,C" 参照的那个"中心"根本不是屏幕中心。
    ok("退路里把上下文根控件撑到屏幕尺寸（顶层 Context 的 XML Size 无效）",
       "ContextPtr:SetSizeX" in lua_code and "ContextPtr:SetSizeY" in lua_code)

    # ④ 控件在"还没参与过布局"的状态下可能带着隐藏标记，
    #    移完不显式打开就是"存在但看不见"。
    #    注意：中间会插进"还原锚点/偏移 + 让栈重算布局"几步，
    #    所以只要求**同一段里**调用过，不要求紧挨着 ChangeParent。
    cp = lua_code.index("button:ChangeParent(stack)")
    ok("改完父级后显式 SetHide(false)（否则存在但看不见）",
       "button:SetHide(false)" in lua_code[cp:cp + 800])

    # ⑤ 日志必须能区分走了哪条路，否则下次又要靠猜。
    ok("日志能区分走了哪条路（挂栈 / 绝对坐标 / 兜底）",
       "已挂进顶栏按钮栈" in lua_code
       and "算了绝对坐标" in lua_code
       and "WHY_FAILED" in lua_code)
    ok("会打印屏幕与上下文根的尺寸（下次出问题不用靠猜）",
       "顾问按钮诊断" in lua_code and "ContextPtr:GetSizeX" in lua_code)

    # ---- 地图导出：让顾问看得见"脚下这片地" ----
    # 这里最容易出的错是"把没探索的黑幕当成草原" —— 不报错，
    # 但模型会拿着不存在的地形理直气壮地给建议。
    ok("地图导出按 IsRevealed 过滤（黑幕不算地形）",
       "vis:IsRevealed(" in lua_code)
    ok("地块的每个 API 调用都包了 Safe",
       "Safe(function() return plot:GetTerrainType()" in lua_code
       and "Safe(function() return plot:IsWater()" in lua_code)
    ok("资源带方位和距离（不给网格坐标）",
       "DirName(" in lua_code and '"北"' in lua_code)
    ok("资源条数有上限（免得把快照撑爆）",
       "MAX_RESOURCES" in lua_code)
    ok("地图信息接进了快照", "BuildMapInfo(pid, player)" in lua_code)
    ok("地图导出遍历所有城市（多城时后面建的城也看得见）",
       "for _, c in ipairs(cities)" in lua_code)
    ok("地图输出有硬上限（城多了不会把快照撑爆）",
       "MAX_CITIES_MAP" in lua_code
       and re.search(r"MAP_RADIUS\s*=\s*2\b", lua_code) is not None)


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _append(path, text):
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(text)


def part8_overlay_ui():
    """浮层界面：用户能不能看出该在哪儿打字、到哪儿改配置。

    这里每条都对应一次真实翻车：
      ① 输入条被 expand=True 的对话区挤出窗口，只剩 1 像素 —— 根本看不到输入框；
      ② 提示文字塞进 Entry 里，中文输入法下清不掉，用户只能手动删；
      ③ 浮层是 pythonw 起的（没有控制台），API 地址/模型/密钥无处可改。
    """
    with open(os.path.join(HERE, "overlay.py"), encoding="utf-8") as f:
        src = f.read()
    # 只审真实代码。注意别用"删掉 # 到行尾"那种粗暴剥法 ——
    # overlay.py 里满是 "#0E1A26" 这样的颜色字面量，会被误伤。
    code = re.sub(r"(?m)^\s*#.*$", "", src)
    code = re.sub(r"[ \t]#[ \t][^\n]*", "", code)

    m = re.search(r'PLACEHOLDER\s*=\s*"([^"]+)"', code)
    ok("输入框有一行给用户看的提示文字",
       m is not None and len(m.group(1)) >= 4,
       m.group(1) if m else "没找到 PLACEHOLDER")

    ok("提示是独立 Label，不塞进输入框（塞进去用户得手动删）",
       "self.ph_label" in code and "place(in_=self.entry" in code)
    ok("提示靠 StringVar 的 trace 同步（中文输入法下按键事件的 char 是空串）",
       "trace_add(" in code and "_ph_sync" in code
       and "isprintable()" not in code)
    ok("提交读的是 entry_var，不再有'提示态'要判断",
       "self.entry_var.get()" in code and "_ph_active" not in code)
    ok("点窗口任意处会把光标送进输入框",
       "def _ph_focus" in code)
    ok("输入区上方有强调线（失焦暗色 / 聚焦点亮）",
       "self.accent_line" in code and "height=px(2)" in code)
    ok("输入框有焦点反馈：聚焦时整条输入区变亮",
       "def _paint_input_area" in code and "INPUT_BG_FOCUS" in code)
    ok("焦点反馈挂在 FocusIn / FocusOut 上",
       '"<FocusIn>"' in code and '"<FocusOut>"' in code)

    with open(os.path.join(HERE, "theme.py"), encoding="utf-8") as f:
        th = f.read()
    ok("theme 里定义了输入框的聚焦底色", "INPUT_BG_FOCUS" in th)

    # ---- 设置面板：浮层版是 pythonw 起的，没有控制台，
    #      ensure_api_key() 那套终端问答弹不出来 —— 配置必须有地方改 ----
    ok("标题栏有设置入口",
       "def open_settings" in code and "open_settings()" in code)
    ok("设置面板含 API 地址 / 模型名 / 密钥 三项",
       code.count("add_field(") >= 4
       and '"API 地址"' in code and '"模型名"' in code and '"API 密钥"' in code)
    ok("设置会写回 config.json（不是只改内存）",
       "agent.save_settings(" in code)
    ok("保存后重建 advisor，新配置立刻生效",
       "self.advisor = agent.Advisor(self.config)" in code)

    with open(os.path.join(HERE, "agent.py"), encoding="utf-8") as f:
        ag = f.read()
    ok("agent.save_settings 只写动过的字段（None 不覆盖预设兜底值）",
       "def save_settings" in ag and "if val is not None" in ag)

    # ---- 实测补的：输入条整个没渲染出来 ----
    # Tk 的 pack 是**按调用顺序**从容器里切空间的。对话区带 expand=True
    # 又排在前面时，会把窗口吃光，后面才 pack 的输入条只剩 1 像素 ——
    # 窗口里根本看不到输入框，而且不报任何错、日志里也没有任何痕迹。
    # 实测数据：body 请求高 918px，foot 被挤成 height=1 且 winfo_ismapped()=False。

    m_foot = re.search(r'foot\.pack\(side="bottom"', code)
    m_body = re.search(r'body\.pack\(side="top",\s*fill="both",\s*expand=True\)', code)
    ok("输入条先于对话区 pack（否则被 expand 的对话区挤成 1 像素）",
       m_foot is not None and m_body is not None
       and m_foot.start() < m_body.start())
    ok("输入条用 side=bottom 占住底部", m_foot is not None)
    ok("对话区 Text 设了 width/height=1（默认 24 行会抢光窗口）",
       re.search(r"width=1,\s*height=1", code) is not None)

    # ---- 实测补的：每次提问都把上一轮问答抹掉 ----
    # _submit 里调了 _clear()、_render 里 delete("1.0","end")，两处都在清屏，
    # 聊天窗于是变成"只显示最新一条"。用户反馈的原话是"上一次的问答看不到了"。
    body_render = code.split("def _render")[1].split("\n    def ")[0]
    # 还要把函数自己的文档字符串剥掉 —— 它为了说明"反面例子"故意写了那个调用，
    # 不剥就会误判（和注释一个道理）。
    body_render = re.sub(r'"""(?:.|\n)*?"""', "", body_render)
    ok("回答是追加到末尾，不清屏（清了就看不到上一轮）",
       "delete(" not in body_render and 'see("end")' in body_render)
    body_submit = code.split("def _submit")[1].split("\n    def ")[0]
    ok("提问时不再清空对话区", "_clear()" not in body_submit)
    ok("留了清屏出口 Ctrl+L（历史是累积的）",
       '"<Control-l>"' in code and "def _reset_conversation" in code)
    ok("Ctrl+L 会一起清掉模型记忆（只清界面会造成割裂）",
       "self.advisor.reset()" in code)


def part9_context():
    """上下文管理：历史滑动窗口 + token 计量。

    来自一次讨论：历史只增不减的话，算下来约二三十轮就会把 64K 上下文撑爆，
    而用户对"花了多少 token"完全没有概念。
    """
    with open(os.path.join(HERE, "agent.py"), encoding="utf-8") as f:
        src = f.read()
    code = re.sub(r'"""(?:.|\n)*?"""', "", src)      # 剥文档字符串，别看反面例子

    ok("历史超长会滑掉最旧的轮次（否则约二三十轮撑爆）",
       "def _trim" in code and "max_turns" in code)
    ok("按『轮』切，不按『条』（tool 消息和调用请求拆开会让 API 报错）",
       'm.get("role") == "user"' in code)
    ok("丢掉旧轮时留一句提示（模型得知道自己不知道）",
       "已省略" in code and "self._dropped" in code)
    ok("开头两条（人格设定 / 战局快照）永不被滑掉",
       "head_len" in code and "self._state_at is not None" in code)
    ok("token 用量取自响应并累计",
       "prompt_tokens" in code and "usage_total" in code)
    ok("记了缓存命中（这部分计费便宜一个数量级）",
       "prompt_cache_hit_tokens" in code)
    ok("轮数上限可在 config 里调", '"max_turns"' in code)
    ok("窗口是在追加新问题**之前**裁的（别把刚问的裁掉）",
       code.index("self._trim()") < code.index('self.messages.append({"role": "user"'))

    with open(os.path.join(HERE, "llm.py"), encoding="utf-8") as f:
        llm = f.read()
    ok("chat() 把 usage 带出来（省得为统计再发一次请求）",
       'msg["_usage"]' in llm)

    with open(os.path.join(HERE, "overlay.py"), encoding="utf-8") as f:
        ov = f.read()
    ok("浮层会报 token 用量", "def _show_usage" in ov and "会话累计" in ov)
    ok("上下文快满时提醒（别等 API 甩报错）",
       "last_prompt_tokens" in ov)


def part10_secret():
    """密钥与 config.json 分家。

    对应真实需求：项目要放到 Git 上，而 config.json 原来存着真实密钥 ——
    一推上去等于公开。现在密钥单独存 secret.json，被 .gitignore 排除。

    这里盯三件事：① config.json 的模板里不能再出现 api_key；
    ② 保存路径不能把密钥写回 config.json；③ 老版本搬家必须是
    "先写成功、再删"（反过来中途失败就把密钥弄丢了）。
    """
    import agent

    # ---- 静态检查：源码里不该再有"把密钥写进 config.json"的写法 ----
    with open("agent.py", encoding="utf-8") as f:
        code = f.read()

    ok("config.json 的模板里不含 api_key（新装的机器从一开始就没有密钥）",
       '"api_key": ""' not in code.split("CONFIG_TEMPLATE")[1].split("}")[0])

    ok("密钥路径是独立的 secret.json",
       'SECRET_PATH = os.path.join(HERE, "secret.json")' in code)

    # 过去是 raw["api_key"] = key 写进 config；现在只允许出现在 secret 的写法里
    ok("没有把密钥写回 config.json 的分支",
       'raw["api_key"] = ' not in code)

    # 搬家顺序：先 save_secret（可能抛），校验通过后才 pop
    seg = code.split("def _migrate_key_out_of_config")[1].split("def load_config")[0]
    ok("老密钥搬家是「先写成功、再删」（反过来中途失败就丢密钥）",
       seg.index("save_secret(legacy)") < seg.index("cfg.pop"))
    ok("搬家后有回读校验（读完对不上就不删原文件）",
       "!=" in seg and "load_secret()" in seg)

    ok("环境变量可覆盖密钥（临时换 key / 跑自动化用）",
       'SECRET_ENV = "ADVISOR_API_KEY"' in code and "os.environ.get(SECRET_ENV)" in code)

    ok("secret.json 读坏了不抛异常（只当没配过）",
       "except (OSError, json.JSONDecodeError):" in code)

    # ---- 动态检查：真跑一遍读写 ----
    import json
    import tempfile
    d = tempfile.mkdtemp(prefix="advisor_secret_")
    cfg_p = os.path.join(d, "config.json")
    sec_p = os.path.join(d, "secret.json")
    old_cfg, old_sec = agent.CONFIG_PATH, agent.SECRET_PATH
    try:
        agent.CONFIG_PATH, agent.SECRET_PATH = cfg_p, sec_p
        with open(cfg_p, "w", encoding="utf-8") as f:
            json.dump({"provider": "deepseek", "api_key": "sk-legacy-key-123"}, f)
        with open(sec_p, "w", encoding="utf-8") as f:
            json.dump({"api_key": "sk-moved-key-456"}, f)   # 预先占位，防止被覆盖

        agent._migrate_key_out_of_config(json.load(open(cfg_p, encoding="utf-8")))
        after = json.load(open(cfg_p, encoding="utf-8"))
        ok("搬家后 config.json 里不再有 api_key",
           "api_key" not in after)
        ok("已经有 secret.json 时不会被旧值覆盖（先到先得）",
           agent.load_secret().get("api_key") == "sk-moved-key-456")

        agent.save_settings(base_url="http://x/v1", model="m", api_key="sk-new-789")
        a2 = json.load(open(cfg_p, encoding="utf-8"))
        ok("save_settings 把接口/模型写 config.json，密钥写 secret.json",
           a2.get("base_url") == "http://x/v1" and a2.get("model") == "m"
           and "api_key" not in a2
           and agent.load_secret().get("api_key") == "sk-new-789")

        # 只在真的变了才落盘：内容一样时不动文件（看修改时间）
        import time
        t0 = os.path.getmtime(cfg_p)
        time.sleep(0.01)
        agent.save_settings(base_url="http://x/v1", model="m")
        ok("设置没变化时不重写 config.json",
           os.path.getmtime(cfg_p) == t0)

        agent.save_settings(api_key="")
        ok("密钥填空 = 清除（不是保留旧的）",
           not (agent.load_secret().get("api_key") or "").strip())

        # 模板里没有 api_key 时，程序读到的密钥只可能来自 secret.json / 环境变量
        os.environ["ADVISOR_API_KEY"] = "sk-from-env"
        try:
            ok("环境变量优先级高于 secret.json",
               agent.resolve_api_key() == "sk-from-env")
        finally:
            os.environ.pop("ADVISOR_API_KEY", None)
    finally:
        agent.CONFIG_PATH, agent.SECRET_PATH = old_cfg, old_sec


def part11_provider():
    """不绑定任何一家服务商。

    对应真实诉求：「现在似乎默认是 deepseek 的 api，能不能改成通用的」。
    现在默认 provider = "custom" —— 地址和模型名留空、由用户填；
    llm.py 里那几家只是"省得你去查地址"的快捷填充，不代表偏好。

    这里还盯一个容易被忽略的连带问题：默认变成"空的"之后，
    Advisor.__init__ 里那句 LLMClient(...) 会对空地址抛异常 ——
    而 pythonw 下浮层会整个起不来（连控制台都没有）。所以客户端必须
    **用到时才建**。
    """
    import agent as ag
    import llm

    ok("默认服务商是「通用」，不是某一家", llm.DEFAULT_PROVIDER == "custom")
    ok("「通用」预设里不含任何厂商地址/模型（否则等于偷偷选了）",
       not llm.PROVIDERS["custom"]["base_url"]
       and not llm.PROVIDERS["custom"]["model"])
    ok("不认识的服务商名按「通用」处理，绝不偷偷挑一家",
       llm.preset("no-such-vendor") is llm.PROVIDERS["custom"]
       and llm.preset(None) is llm.PROVIDERS["custom"])
    with open("agent.py", encoding="utf-8") as f:
        code = f.read()
    ok("config 模板里的 provider 是 custom", '"provider": "custom"' in code)

    # 手填地址的容错 —— 这串东西是人手打的，少写一段很常见
    n = llm.LLMClient._normalize_url
    ok("地址只写域名时自动补 https:// 和 /chat/completions",
       n("api.example.com") == "https://api.example.com/chat/completions")
    ok("地址带 /v1 时补在它后面（不重复加路径）",
       n("https://x.com/v1") == "https://x.com/v1/chat/completions")
    ok("已经是完整接口路径时原样不动",
       n("https://x.com/v1/chat/completions") == "https://x.com/v1/chat/completions")
    try:
        n("")
        ok("空地址给出明确提示", False)
    except llm.LLMError as e:
        ok("空地址的提示指向「设置」面板（别让用户去翻源码）", "设置" in str(e))

    a = ag.Advisor({"provider": "custom", "base_url": "", "model": "",
                    "api_key": "sk-fake-fake-fake", "max_steps": 8})
    ok("没填地址时 Advisor 仍能构造（否则浮层整个起不来）", a is not None)
    ok("网络客户端是用到时才建（不在 __init__ 里）", a.client is None)
    try:
        a.ask("测试", verbose=False)
        ok("没填地址时提问给出提示而不是崩掉", False)
    except llm.LLMError as e:
        ok("没填地址时提问给出提示而不是崩掉", "设置" in str(e))

    # usage 里的"命中缓存"各家字段名不同，都要认
    ok("命中缓存的字段名兼容两种（DeepSeek 的 / OpenAI 的）",
       "prompt_cache_hit_tokens" in code and "prompt_tokens_details" in code)

    with open("overlay.py", encoding="utf-8") as f:
        ui = f.read()
    ok("设置面板有「常用服务商」下拉（选一家自动填地址和模型）",
       "OptionMenu" in ui and "on_pick" in ui and "llm.preset" in ui)
    ok("启动时若没填地址/模型，自动打开设置面板（别让用户先撞一次报错）",
       "open_settings" in ui and "第一次用，先把这两项填上" in ui)


def part12_repo():
    """仓库卫生：什么不该进版本库。

    对应真实诉求：「我们是不是不应该直接在仓库里面放 .exe 文件，而是应该让
    拉仓库的人自己编译」。答：对，而且已经这么做了 —— 这里把规则钉住，
    免得哪天手滑把 exe 或密钥提交上去（二进制进了 git 历史就删不掉了）。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    gi_path = os.path.join(here, "..", ".gitignore")
    ok("仓库根目录有 .gitignore", os.path.exists(gi_path))
    gi = ""
    if os.path.exists(gi_path):
        with open(gi_path, encoding="utf-8") as f:
            gi = f.read()

    # 一个都不能漏 —— 这几条都是"提交了就很难挽回"的东西
    ok("构建产物 exe 被忽略（二进制进历史后删不干净）", "*.exe" in gi)
    ok("密钥文件被忽略", "secret.json" in gi)
    ok("本地配置被忽略", "config.json" in gi)
    ok("从游戏生成的补丁被忽略（含 Firaxis 文本，不代为分发）",
       "text_patch.json" in gi)
    ok("别人的参考项目被忽略（不转手分发别人的代码）", "_ref/" in gi)

    # 许可证（2026-10-07 定为 MIT）。没有许可证的公开仓库 = 默认保留所有权利，
    # 按字面意思别人连 fork 改一下都不行 —— 和"发出来给人看"的初衷相反。
    lic = os.path.join(here, "..", "LICENSE")
    ok("仓库根有 LICENSE（没写就是默认保留所有权利，别人无权使用）",
       os.path.exists(lic))
    if os.path.exists(lic):
        with open(lic, encoding="utf-8") as f:
            lic_text = f.read()
        ok("LICENSE 是 MIT 全文（授权句 + 免责声明都在）",
           "MIT License" in lic_text
           and "Permission is hereby granted, free of charge" in lic_text
           and "WITHOUT WARRANTY OF ANY KIND" in lic_text)

    # build_exe.bat 必须纯 ASCII：cmd 按控制台代码页解析 bat 内容，
    # 中文参数会变成乱码 —— 打出来的 exe 名字就是乱的。所以中文全在 .py 里。
    bat = os.path.join(here, "build_exe.bat")
    ok("有 build_exe.bat（克隆的人双击它就能编译出 exe）", os.path.exists(bat))
    if os.path.exists(bat):
        raw = open(bat, "rb").read()
        ok("build_exe.bat 是纯 ASCII（中文参数在这个文件里会变乱码）",
           all(b < 128 for b in raw))
        ok("build_exe.bat 用 CRLF（Windows 批处理的规矩）", b"\r\n" in raw)

    with open("build_exe.py", encoding="utf-8") as f:
        be = f.read()
    ok("打包前检查 tkinter（少了它，打出的 exe 一运行就退出）",
       "import tkinter" in be)
    ok("打包前检查 PyInstaller，并直接给出该敲的安装命令",
       "import PyInstaller" in be and "pip install" in be)
    ok("图标用绝对路径（相对路径按 spec 目录解析，实测会报找不到）",
       '"--icon", icon' in be and "os.path.join(here, ICON)" in be)
    ok("打完自动清中间产物（_pyi_build / *.spec）",
       "rmtree" in be and "*.spec" in be)
    ok("留了 --dist，便于在不碰现有 exe 的前提下验证打包",
       '"--dist"' in be)

    # ---- 文档：给外人看的，不是写给自己看的 ----
    repo_readme = os.path.join(here, "..", "README.md")
    ok("仓库根有 README（外人进来第一眼看到的）", os.path.exists(repo_readme))
    for label, p in (("Advisor/README.md", "README.md"),
                     ("仓库根 README", repo_readme)):
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            text = f.read()
        # 用户明确要求：说清楚这东西是 AI 写的
        ok(f"{label} 里有「由 AI 生成」的声明", "AI 生成" in text)
        # 公开仓库里不该出现某台机器的绝对路径
        ok(f"{label} 没写死本机路径（要用 %USERPROFILE%）", "C:\\Users\\" not in text)

    # mod 目录也要有自己的说明 —— 单独翻到那个目录的人（想改 mod 的）
    # 不该只看到四个文件就没了。用户报过这个缺口。
    mod_readme = os.path.join(here, "..", "AdvisorMod", "README.md")
    ok("mod 目录有自己的 README", os.path.exists(mod_readme))
    if os.path.exists(mod_readme):
        with open(mod_readme, encoding="utf-8") as f:
            mr = f.read()
        ok("mod 说明写明它不含 AI 逻辑（只负责按钮 + 导出战局）",
           "不含任何 AI 逻辑" in mr)
        ok("mod 说明写了怎么装（复制进 Mods + 在「额外内容」里启用）",
           "Mods" in mr and "额外内容" in mr)
        ok("mod 说明写了信号标记与匹配规则（外面按 [A-Z_]+ 匹配）",
           "ADVISOR:TOGGLE" in mr and "[A-Z_]+" in mr)
        ok("mod 说明写了改完要 sync_mod、且不热生效",
           "sync_mod" in mr and "不热生效" in mr)


def main():
    log = os.path.join(tempfile.mkdtemp(prefix="advisor_selftest_"), "Lua.log")
    _write(log, "旧日志行\n>>>ADVISOR:TOGGLE<<<\n>>>ADVISOR:READY<<<\n")

    part1_tailer(log)
    part2_agent()
    part3_kb_list()
    part4_regressions(log)
    part5_dead_fields()
    part6_launcher()
    part7_mod_ui()
    part8_overlay_ui()
    part9_context()
    part10_secret()
    part11_provider()
    part12_repo()

    print("\n全部通过：%d 项" % len(_PASSED))
    if _SKIPPED:
        # 单独统计，不混进"通过"里 —— 否则"全部通过"就会变成一句假话
        print("另有 %d 项跳过（环境缺东西，不是代码问题）：" % len(_SKIPPED))
        for s in _SKIPPED:
            print("  - " + s)
        print("  （想跑全量：装好文明6 并至少启动过一次，再运行本脚本）")


if __name__ == "__main__":
    main()
