# Civ6Mod —— 文明6 modding 工作区

主体是 **[文明6 顾问](Advisor/README.md)**：一个浮在游戏画面上的 AI 问答助手，
回答之前会先去查游戏规则库里的权威数据，而不是凭训练记忆瞎编。

```
你 > 圣地相邻加成怎么算

  · 我查一下圣地的规则。
  [1] 查 kb_district (name=圣地)

圣地最划算的是贴着山脉（每格 +1）和自然奇观（每格 +2）；
相邻区域和树林是每 2 个才 +1，也就是每个只加 0.5，别算错。
```

> **声明：代码、注释、文档、图标全部由 AI 生成。** 人类作者负责提出需求、试玩、
> 判断对错、决定取舍。能实测的结论都留了证据（`python selftest.py`，170 项，
> 不联网不碰游戏），没验证过的地方文档里会写明"未验证"。
>
> 非官方项目，与 Firaxis Games / 2K 无关；仓库里不含任何游戏数据、贴图或文本内容。

## 目录

| 目录 | 是什么 |
|---|---|
| **`Advisor/`** | **顾问本体**：浮层 + 大脑（Python）。**从它的 [README](Advisor/README.md) 看起** |
| `AdvisorMod/` | 游戏内的那个按钮（一个 mod）。不含 AI 逻辑，只负责往外喊一声 —— 见它的 [README](AdvisorMod/README.md) |
| `_tools/` | 写 mod 时用的小工具（校验 `.modinfo`、从 `.artdef` 抽数据） |

仓库里不放构建产物 `文明6顾问.exe` 和 `text_patch.json`（后者要从你自己装的游戏里
生成），原因和生成方法见 [Advisor/README.md](Advisor/README.md)。

## 快速开始

```bash
cd Advisor
python selftest.py     # 自检 170 项：不联网、不碰游戏
```

想跑起来（两种方式都需要先装 Python）：双击 `Advisor/launch_overlay.bat` 直接跑，
或双击 `Advisor/build_exe.bat` 打包出一个不依赖 Python 的 exe。
配 API 密钥、装游戏内按钮、改显示模式的完整步骤见
[Advisor/README.md](Advisor/README.md)。
