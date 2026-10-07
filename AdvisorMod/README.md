# Civ6AdvisorBridge —— 游戏里的那个按钮

这个 mod 只做两件事：

1. 在顶栏加一个按钮（外加 `F8` 热键）
2. 点它的时候，把**当前战局**导成一段文本，`print` 进游戏的 `Lua.log`

**它不含任何 AI 逻辑。** 真正的问答在隔壁 `../Advisor/`（Python 那边）——
那个程序盯着 `Lua.log`，看到信号就把浮窗弹出来。

> 所以：只装这个 mod 得不到 AI 助手；只跑 Python 那边则没有游戏内按钮
> （但按 `Ctrl+Alt+A` 一样能叫出浮窗）。两边都装才最顺手。

## 装法

**① 把 `Civ6AdvisorBridge` 整个文件夹**复制到：

```
%USERPROFILE%\Documents\My Games\Sid Meier's Civilization VI\Mods\
```

放好后应该是这样：

```
Mods\
└── Civ6AdvisorBridge\
    ├── Civ6AdvisorBridge.modinfo
    ├── Text\AdvisorText.xml
    └── UI\Additions\
        ├── AdvisorLauncher.xml
        └── AdvisorLauncher.lua
```

**② 启动游戏 → 额外内容 → 启用 `Civ6 AI Advisor Bridge`。**

**③ 显示模式改成「无边框窗口」**（选项 → 视频 → 显示模式）。

> 必须做。独占全屏会挡住顾问浮窗，这是 Windows 的机制，绕不过去。

**④ 进地图。** 右上角、文明百科按钮**左边**会多出一个按钮（图标和百科那个
「？」一模一样 —— 是故意复用游戏自带贴图的，所以看着不像"外来户"）。

### 改完之后怎么生效

改了这个文件夹里的东西，双击 `../Advisor/sync_mod.bat` 同步（它把这里的内容覆盖到
上面那个 Mods 目录）。游戏**只读 Mods 目录**，不读这个项目目录。

然后让游戏重新加载 mod：

- **重启游戏**（最稳）
- 或者**回主菜单再读一次档** —— 战局界面会重建、mod 的 Lua 跟着重新加载，
  几十秒的事，比重启整个游戏快

## 怎么确认它在工作

看 `Lua.log`：

```
%LOCALAPPDATA%\Firaxis Games\Sid Meier's Civilization VI\Logs\Lua.log
```

搜「**按钮就位**」，正常会看到：

```
AdvisorLauncher: 按钮就位（初始化）：顶栏还没就绪，先用退路 → 按屏幕 …算了绝对坐标
AdvisorLauncher: 按钮就位（加载完成）：已挂进顶栏按钮栈（锚点=L,T）
AdvisorLauncher: 顾问按钮诊断：屏幕=2560x1600 上下文根=2560x1600（正常） 锚点=L,T 按钮偏移=0,0
AdvisorLauncher: >>>ADVISOR:READY<<<
```

| 看到什么 | 意思 |
|---|---|
| 只有第 1 行、没有第 2 行 | 按钮落在"退路"位置（能用，但位置不一定好看） |
| 第 2 行写着「已挂进顶栏按钮栈」 | 位置由游戏布局算，最好 |
| `>>>ADVISOR:READY<<<` | 脚本跑起来了 |
| 一行都没有 | **mod 没被加载** —— 检查 `.modinfo` 在不在、游戏里启用了没有（也看 `Modding.log`） |

点按钮之后，日志里还会出现 `>>>ADVISOR:STATE<<<` … `>>>ADVISOR:STATEEND<<<`。

## 它怎么把数据送出去

游戏内的 Lua 沙箱里**没有 io / socket / os.execute** —— 连不了网、也开不了进程。
唯一的出口是 `print()`，它写进游戏的 `Lua.log`。所以整条链路是：

```
游戏（这个 mod） --print--> Lua.log --监听--> Python（Advisor） --HTTP--> 大模型
```

发出去的是几种带标记的文本行：

| 标记 | 什么时候 | 意思 |
|---|---|---|
| `>>>ADVISOR:READY<<<` | 脚本加载完 | "我装好了" |
| `>>>ADVISOR:TOGGLE<<<` | 点按钮 / 按 F8 | "用户想提问" |
| `>>>ADVISOR:STATE<<<` … `>>>ADVISOR:STATEEND<<<` | 紧跟 TOGGLE | 中间夹着当前战局 |
| `>>>ADVISOR:STATE_EMPTY<<<` | 还没进战局 | 没数据可导 |
| `>>>ADVISOR:STATE_ERROR<<<` | 导出中途出错 | 后面跟原因 |

**战局是"点按钮那一刻现查"的，不是每回合推送。** 日志有长度上限，每回合塞一份
既会把它撑爆、也会拖慢游戏。

导出内容一行一类：回合 / 时代 / 文明 / 领袖、金币与收支、科技文化信仰产出、
正在研究的科技与市政及百分比、交战对象、各城市（人口 / 在建 / 剩余回合）、
军队构成，以及每座城市周围的**已探明**地形与资源。

> ⚠️ 标记里只能出现 `A-Z` 和下划线 —— Python 那边是按
> `>>>ADVISOR:([A-Z_]+)<<<` 匹配的。写成 `STATE-EMPTY` 这种带连字符的会被
> **静默丢弃**（日志里明明有，就是匹配不上）。旧版遗留的 `STATE-EMPTY` 写法
> 被额外兼容了一句，但别指望别的写法也有这待遇。

## 四个文件各是什么

| 文件 | 作用 |
|---|---|
| `Civ6AdvisorBridge.modinfo` | 游戏的"说明书"：加载哪些文件、在什么阶段加载。`<Context>InGame</Context>` = **只在进战局时才加载** |
| `UI/Additions/AdvisorLauncher.xml` | 按钮长什么样：29×29，贴图复用游戏自带的 `TopBar_CivilopediaButton` |
| `UI/Additions/AdvisorLauncher.lua` | 全部逻辑：把按钮挂进顶栏、点按钮时导出战局、`F8` 热键 |
| `Text/AdvisorText.xml` | 按钮的鼠标悬停提示（「AI 顾问 [F8]」） |

## 改它的注意事项

- **别改 XML 里的 `Anchor` / `Offset`** —— 那两行只是 Lua 完全没跑起来时的兜底，
  正常挂进顶栏时会被 Lua 覆盖掉。想调位置看上面「怎么确认它在工作」那张表。
- 改完必须 `sync_mod.bat` + 让游戏重新加载（见上），**不热生效**。
- 写 XML 前先跑一次静态校验，它能在不启动游戏的情况下抓出非法列名 / 缺失文件：
  ```
  python ../_tools/validate_mod.py .
  ```
  （它会提示界面文件"跳过表校验" —— 这是对的，`<Context>` 不是数据表。）
- 界面的坑都在 `../Advisor/README.md` 的排错表里；底层原理（为什么位置要运行时算、
  为什么必须能重试）在 `civ6-modding` 技能文档里。
