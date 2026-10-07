--[[
    文明6 AI 顾问 —— 游戏内入口 + 战局快照
    =====================================================================

    这个脚本干两件事：
      1. 玩家点顾问按钮时，把当前战局打成几行文本，扔进 Lua.log
      2. 紧接着发一个 TOGGLE 信号，让外面的顾问窗浮出来

    为什么必须"另开一个进程"：
      游戏内的 Lua 沙箱是全封闭的 —— 没有网络、不能开窗口、不能读写文件。
      它唯一能碰到外面世界的动作，就是 print 一行字到 Lua.log。
      所以真正干活的逻辑（查规则库、调大模型）全在游戏外面。

    为什么战局数据是"点按钮时才导"：
      每回合导出会让日志持续膨胀（一局几百回合就是好几 MB），
      还要每回合遍历所有城市和单位。既然玩家是"想问才点"，
      点的时候现查一份最新的就够了 —— 而且永远不过期。

    容错原则：
      文明6 的 Lua API 很脆。同一个方法在不同版本、不同上下文里
      可能突然不存在、或者直接报错。所以每个取值都单独包 pcall：
      拿不到的字段就空着，绝不让一个字段拖垮整份快照。

    信号格式不要改，外面按它做匹配（game_bridge.py 里有对应正则）。
]]

-- 与外部程序约定的标记
local SIGNAL_TOGGLE = ">>>ADVISOR:TOGGLE<<<"
local SIGNAL_READY  = ">>>ADVISOR:READY<<<"
local TAG_BEGIN     = ">>>ADVISOR:STATE<<<"
local TAG_END       = ">>>ADVISOR:STATEEND<<<"
-- 还没进入战局（在主菜单、读档中）时喊这个。
-- 注意标记里只能有 A-Z 和下划线：外面是按 >>>ADVISOR:([A-Z_]+)<<< 匹配的，
-- 写成 STATE-EMPTY 里的连字符会让整行**被静默丢掉**。
local SIG_EMPTY     = ">>>ADVISOR:STATE_EMPTY<<<"

-- F8 的键值。提到顶层取一次：
--   ① 万一这个上下文里没有 Keys 这张表，也不会在按键回调里反复报错；
--   ② 真的取不到就是 nil，后面那句比较会恒为 false —— F8 自然失效，
--      但绝不抛异常（外面还有 Ctrl+Alt+A 兜底）。
local VK_ADVISOR = Keys and Keys.VK_F8 or nil

-- 快照的篇幅控制
local MAX_CITIES     = 8      -- 城市最多详列几个，多的折叠
local MAX_UNIT_KINDS = 8      -- 单位最多列几种，多的折叠
local MAX_BYTES      = 1800   -- 整份快照的字节上限（print 有长度限制）

-- YieldTypes 这个全局表不是每个上下文都有，给个兜底的索引值
local YIELD_PRODUCTION = (YieldTypes and YieldTypes.YIELD_PRODUCTION) or 1

-- ==========================================================================
-- 取值小工具
-- ==========================================================================

-- 执行一段取值代码，失败返回 nil。文明6 里到处都要这么干。
--
-- ⚠️ 必须把**两个**返回值都转交出去。游戏里多返回值的 API 很常见，
--    最典型的就是 UIManager:GetScreenSizeVal() → (宽, 高)。
--    写成 `local ok, v = pcall(fn); return v` 只会带回第一个值，
--    高度永远是 nil —— 而且它**不报错**，只是静默走进错误分支。
--    （这个 bug 真发生过：屏幕尺寸明明取得到，却一直打印"取不到"。）
local function Safe(fn)
    local ok, a, b = pcall(fn)
    if not ok then return nil end
    return a, b
end

-- 本地化：按顺序试每个候选串，返回第一个能翻出名字的。
-- 传 "UNIT_BUILDER" 这种类型串，或 LOC_ 开头的键，都能处理。
local function Loc(...)
    for i = 1, select("#", ...) do
        local s = select(i, ...)
        if type(s) == "string" and s ~= "" then
            local v = Safe(function() return Locale.Lookup(s) end)
            if type(v) == "string" and v ~= "" then return v end
        end
    end
    return nil
end

-- 取名：能给中文就给中文，给不了就退回原始类型串
local function Name(raw)
    if raw == nil then return nil end
    return Loc(raw) or raw
end

-- 数字格式化：整数不带小数点
local function Num(v)
    if type(v) ~= "number" then return nil end
    if v == math.floor(v) then return tostring(math.floor(v)) end
    return string.format("%.1f", v)
end

-- 带正负号的数字，用于每回合收支
local function Signed(v)
    if type(v) ~= "number" then return nil end
    local n = math.floor(v + 0.5)
    if n > 0 then return "+" .. n end
    return tostring(n)
end

-- 把某个索引对应的 GameInfo 项转成本地化名字
local function EraName(idx)
    if type(idx) ~= "number" or idx < 0 then return nil end
    local info = Safe(function() return GameInfo.Eras[idx] end)
    if info == nil then return nil end
    return Loc(info.Name, info.EraType)
end

local function TechName(idx)
    if type(idx) ~= "number" or idx < 0 then return nil end
    local info = Safe(function() return GameInfo.Technologies[idx] end)
    if info == nil then return nil end
    return Loc(info.Name, info.TechnologyType)
end

local function CivicName(idx)
    if type(idx) ~= "number" or idx < 0 then return nil end
    local info = Safe(function() return GameInfo.Civics[idx] end)
    if info == nil then return nil end
    return Loc(info.Name, info.CivicType)
end

local function UnitTypeName(idx)
    if type(idx) ~= "number" or idx < 0 then return nil end
    local info = Safe(function() return GameInfo.Units[idx] end)
    if info == nil then return nil end
    return Loc(info.Name, info.UnitType)
end

-- 地块上的三样东西：地形 / 地貌 / 资源。套路和上面几个完全一样，
-- 所以抽成一个按表名取的通用版本。
local function PlotName(tableName, typeField, idx)
    if type(idx) ~= "number" or idx < 0 then return nil end
    local info = Safe(function() return GameInfo[tableName][idx] end)
    if info == nil then return nil end
    return Loc(info.Name, info[typeField])
end

-- 方位说成人话。顾问被明确要求不要用网格坐标（见 agent.py 的系统提示词），
-- 所以这里直接给"北2""东南3"这种说法。
-- 注意 y 轴：Civ6 里 y 越小越靠北。
local function DirName(dx, dy)
    if dx == 0 and dy == 0 then return "脚下" end
    local ew = (dx > 0 and "东") or (dx < 0 and "西") or ""
    local ns = (dy < 0 and "北") or (dy > 0 and "南") or ""
    if ew == "" then return ns end
    if ns == "" then return ew end
    local ax, ay = math.abs(dx), math.abs(dy)
    if ax > ay * 2 then return ew end      -- 明显偏东西，就说正方向
    if ay > ax * 2 then return ns end
    return ew .. ns                        -- 中文说"东北"，不说"北东"
end

-- 城市正在造什么 + 还要几回合
local function Production(city)
    local queue = Safe(function() return city:GetBuildQueue() end)
    if queue == nil then return nil, nil end

    local item = Safe(function() return queue:CurrentlyBuilding() end)
    if type(item) ~= "string" or item == "" then return nil, nil end

    local name = Name(item)

    -- 剩余回合：先问游戏
    local turns = Safe(function() return queue:GetTurnsLeft() end)
    if type(turns) ~= "number" or turns < 0 then
        turns = nil
        -- 问不到就自己算：(总成本 - 已完成) / 每回合产出
        local cost  = Safe(function() return queue:GetProductionCost() end)
        local done  = Safe(function() return queue:GetProductionProgress() end)
        local yield = Safe(function() return city:GetYield(YIELD_PRODUCTION) end)
        if type(cost) == "number" and cost > 0
           and type(yield) == "number" and yield > 0 then
            turns = math.ceil((cost - (type(done) == "number" and done or 0)) / yield)
        end
    end
    return name, turns
end

-- ==========================================================================
-- 快照构造
-- ==========================================================================

-- 返回 head / cities / tail 三段，方便超长时单独裁剪中间那段
-- 地图信息：让顾问知道"脚下这片地长什么样"。
--
-- 每座城市各扫一圈 —— 只扫一个中心点的话，多城之后后面建的城完全看不见。
-- 半径取 2（5×5）而不是 3：城一多，49 格/城会让输出迅速膨胀，
-- 而 5×5 已经够回答"这城周围有什么"。
-- 计算量不是问题（每格几次 pcall，毫秒级）；真正要盯的是**输出体积** ——
-- 整份快照只有 1800 字节预算。
local MAP_RADIUS     = 2     -- 每个中心点看周围几格
local MAX_CITIES_MAP = 8     -- 最多扫几座城，超了明说省略
local MAX_RESOURCES  = 4     -- 每座城最多列几处资源

-- 扫一个中心点。返回一行摘要（没探明就返回 nil）
local function ScanAround(vis, cx, cy, label)
    local tally, order, res = {}, {}, {}
    local known = 0

    local function bump(key)
        if key == nil or key == "" then return end
        if tally[key] == nil then
            tally[key] = 0
            order[#order + 1] = key
        end
        tally[key] = tally[key] + 1
    end

    for dx = -MAP_RADIUS, MAP_RADIUS do
        for dy = -MAP_RADIUS, MAP_RADIUS do
            local x, y = cx + dx, cy + dy

            -- 没探明的格连地形都看不到，必须跳过 —— 否则会把黑幕当成草原
            if Safe(function() return vis:IsRevealed(x, y) end) then
                local plot = Safe(function() return Map.GetPlot(x, y) end)
                if plot ~= nil then
                    known = known + 1

                    if Safe(function() return plot:IsWater() end) then
                        bump("水域")
                    else
                        bump(PlotName("Terrains", "TerrainType",
                                      Safe(function() return plot:GetTerrainType() end)))
                        if Safe(function() return plot:IsMountain() end) then
                            bump("山脉")
                        elseif Safe(function() return plot:IsHills() end) then
                            bump("丘陵")
                        end
                        bump(PlotName("Features", "FeatureType",
                                      Safe(function() return plot:GetFeatureType() end)))
                    end

                    local ridx = Safe(function() return plot:GetResourceType() end)
                    if type(ridx) == "number" and ridx >= 0 then
                        local rname = PlotName("Resources", "ResourceType", ridx)
                        if rname ~= nil then
                            local dist = math.max(math.abs(dx), math.abs(dy))
                            res[#res + 1] = {
                                name  = rname,
                                dist  = dist,
                                where = (dist == 0) and "脚下"
                                        or (DirName(dx, dy) .. dist),
                            }
                        end
                    end
                end
            end
        end
    end

    if known == 0 then return nil end      -- 一格都没探明，等于没信息

    -- 地形：多的排前面，读起来更像人话
    table.sort(order, function(a, b)
        if tally[a] ~= tally[b] then return tally[a] > tally[b] end
        return a < b
    end)
    local parts = {}
    for _, k in ipairs(order) do
        parts[#parts + 1] = k .. "×" .. tally[k]
    end
    local line = label .. "周围=" .. table.concat(parts, " ")

    -- 资源：近的排前面
    table.sort(res, function(a, b)
        if a.dist ~= b.dist then return a.dist < b.dist end
        return a.name < b.name
    end)
    local rs = {}
    for i = 1, math.min(#res, MAX_RESOURCES) do
        rs[#rs + 1] = res[i].name .. "(" .. res[i].where .. ")"
    end
    if #rs > 0 then
        line = line .. " 资源=" .. table.concat(rs, "、")
        if #res > MAX_RESOURCES then line = line .. "等" .. #res .. "处" end
    end

    return line
end

local function BuildMapInfo(pid, player)
    local rows = {}
    if player == nil then return rows end

    local vis = Safe(function() return PlayersVisibility[pid] end)
    if vis == nil then return rows end

    -- 中心点：每座城市各一个；还没建城就用第一个单位
    local centers = {}
    local cities = Safe(function()
        local t = {}
        for _, c in player:GetCities():Members() do t[#t + 1] = c end
        return t
    end)

    if cities ~= nil and #cities > 0 then
        for _, c in ipairs(cities) do
            centers[#centers + 1] = {
                x     = Safe(function() return c:GetX() end),
                y     = Safe(function() return c:GetY() end),
                label = Safe(function() return c:GetName() end),
            }
        end
    else
        local unit = Safe(function()
            for _, u in player:GetUnits():Members() do return u end
        end)
        if unit ~= nil then
            centers[1] = {
                x     = Safe(function() return unit:GetX() end),
                y     = Safe(function() return unit:GetY() end),
                label = UnitTypeName(Safe(function() return unit:GetType() end)),
            }
        end
    end

    if #centers == 0 then return rows end

    local shown = math.min(#centers, MAX_CITIES_MAP)
    for i = 1, shown do
        local c = centers[i]
        if type(c.x) == "number" and type(c.y) == "number" then
            local line = ScanAround(vis, c.x, c.y,
                                    (c.label ~= nil and c.label ~= "")
                                    and c.label or "据点")
            if line ~= nil then table.insert(rows, line) end
        end
    end
    if #centers > shown then
        table.insert(rows, "（另 " .. (#centers - shown) .. " 座城市未扫）")
    end

    return rows
end

local function BuildSnapshot()
    local pid = Safe(function() return Game.GetLocalPlayer() end)
    if type(pid) ~= "number" or pid < 0 then return nil end   -- 还没进入战局
    local player = Players[pid]
    if player == nil then return nil end

    local head, cities, tail = {}, {}, {}

    ---------------------------------------------------------------- 我是谁
    local who = {}
    local turn = Safe(function() return Game.GetCurrentGameTurn() end)
    if type(turn) == "number" then table.insert(who, "回合=" .. turn) end

    local era = EraName(Safe(function() return Game.GetEras():GetCurrentEra() end))
    if era then table.insert(who, "时代=" .. era) end

    local cfg = PlayerConfigurations[pid]
    if cfg then
        local civ = Name(Safe(function() return cfg:GetCivilizationTypeName() end))
        if civ then table.insert(who, "文明=" .. civ) end
        local leader = Name(Safe(function() return cfg:GetLeaderTypeName() end))
        if leader then table.insert(who, "领袖=" .. leader) end
    end
    if #who > 0 then table.insert(head, table.concat(who, " ")) end

    ------------------------------------------------------------------ 脚下这片地
    -- 顾问看不到地图，就答不了"这个位置该不该建城" —— 实测反馈的缺口
    for _, line in ipairs(BuildMapInfo(pid, player)) do
        table.insert(head, line)
    end

    ------------------------------------------------------------ 钱和产出
    local eco = {}
    local treasury = Safe(function() return player:GetTreasury() end)
    if treasury then
        local gold = Num(Safe(function() return treasury:GetGoldBalance() end))
        if gold then table.insert(eco, "金币=" .. gold) end
        local y = Safe(function() return treasury:GetGoldYield() end)
        local m = Safe(function() return treasury:GetTotalMaintenance() end)
        if type(y) == "number" then
            table.insert(eco, "收支=" .. Signed(y - (type(m) == "number" and m or 0)))
        end
    end

    local techs = Safe(function() return player:GetTechs() end)
    if techs then
        local sci = Num(Safe(function() return techs:GetScienceYield() end))
        if sci then table.insert(eco, "科技产出=" .. sci) end
    end

    local culture = Safe(function() return player:GetCulture() end)
    if culture then
        local cul = Num(Safe(function() return culture:GetCultureYield() end))
        if cul then table.insert(eco, "文化产出=" .. cul) end
    end

    local religion = Safe(function() return player:GetReligion() end)
    if religion then
        local faith = Num(Safe(function() return religion:GetFaithYield() end))
        if faith then table.insert(eco, "信仰产出=" .. faith) end
        local fb = Num(Safe(function() return religion:GetFaithBalance() end))
        if fb then table.insert(eco, "信仰=" .. fb) end
    end
    if #eco > 0 then table.insert(head, table.concat(eco, " ")) end

    ------------------------------------------------------------ 研究方向
    local study = {}
    if techs then
        local idx = Safe(function() return techs:GetResearchingTech() end)
        local tn = TechName(idx)
        if tn then
            local pct = nil
            local cost = Safe(function() return techs:GetResearchCost(idx) end)
            local prog = Safe(function() return techs:GetResearchProgress(idx) end)
            if type(cost) == "number" and cost > 0 and type(prog) == "number" then
                pct = math.floor(prog / cost * 100)
            end
            table.insert(study, "科技=" .. tn .. (pct and ("(" .. pct .. "%)") or ""))
        else
            table.insert(study, "科技=未选择")
        end
    end
    if culture then
        local idx = Safe(function() return culture:GetProgressingCivic() end)
        local cn = CivicName(idx)
        if cn then
            local pct = nil
            local cost = Safe(function() return culture:GetCultureCost(idx) end)
            local prog = Safe(function() return culture:GetCulturalProgress(idx) end)
            if type(cost) == "number" and cost > 0 and type(prog) == "number" then
                pct = math.floor(prog / cost * 100)
            end
            table.insert(study, "市政=" .. cn .. (pct and ("(" .. pct .. "%)") or ""))
        else
            table.insert(study, "市政=未选择")
        end
    end
    if #study > 0 then table.insert(head, table.concat(study, " ")) end

    ------------------------------------------------------------ 跟谁在打
    local wars = {}
    local diplo = Safe(function() return player:GetDiplomacy() end)
    if diplo then
        for other = 0, 63 do
            if other ~= pid then
                if Safe(function() return diplo:IsAtWarWith(other) end) then
                    local ocfg = PlayerConfigurations[other]
                    local oname = nil
                    if ocfg then
                        oname = Name(Safe(function() return ocfg:GetCivilizationTypeName() end))
                    end
                    table.insert(wars, oname or ("玩家" .. other))
                end
            end
        end
    end
    if #wars > 0 then
        table.insert(head, "交战中=" .. table.concat(wars, "、"))
    else
        table.insert(head, "交战=无")
    end

    ------------------------------------------------------------ 城市
    local rows = {}
    local cityColl = Safe(function() return player:GetCities() end)
    if cityColl then
        pcall(function()
            for _, city in cityColl:Members() do
                local row = {}
                row.name = Name(Safe(function() return city:GetName() end)) or "?"
                row.pop  = Safe(function() return city:GetPopulation() end)
                row.cap  = Safe(function() return city:IsCapital() end)
                row.prod, row.turns = Production(city)
                row.grow = Safe(function()
                    local g = city:GetGrowth()
                    if g == nil then return nil end
                    return g:GetTurnsUntilGrowth()
                end)
                table.insert(rows, row)
            end
        end)
    end

    -- 人口多的排前面（超篇幅时先保这些）
    table.sort(rows, function(a, b) return (a.pop or 0) > (b.pop or 0) end)

    local totalPop = 0
    for _, r in ipairs(rows) do totalPop = totalPop + (r.pop or 0) end
    table.insert(head, "城市数=" .. #rows .. " 总人口=" .. totalPop)

    for i = 1, math.min(#rows, MAX_CITIES) do
        local r = rows[i]
        local parts = { "城市", r.name }
        if r.cap then table.insert(parts, "首都") end
        if r.pop then table.insert(parts, "人口" .. r.pop) end
        if r.prod then
            local p = "生产:" .. r.prod
            if r.turns then p = p .. "(" .. r.turns .. "回合)" end
            table.insert(parts, p)
        else
            table.insert(parts, "生产:空")
        end
        if type(r.grow) == "number" and r.grow >= 0 then
            table.insert(parts, "成长:" .. r.grow .. "回合")
        end
        table.insert(cities, table.concat(parts, " "))
    end

    ------------------------------------------------------------ 军队
    local unitCount = {}
    local unitColl = Safe(function() return player:GetUnits() end)
    if unitColl then
        pcall(function()
            for _, unit in unitColl:Members() do
                local t = Safe(function() return unit:GetType() end)
                if type(t) == "number" then
                    unitCount[t] = (unitCount[t] or 0) + 1
                end
            end
        end)
    end

    local kinds, total = {}, 0
    for t, n in pairs(unitCount) do
        total = total + n
        table.insert(kinds, { name = UnitTypeName(t) or ("类型" .. t), n = n })
    end
    table.sort(kinds, function(a, b) return a.n > b.n end)

    if total > 0 then
        local segs = {}
        for i = 1, math.min(#kinds, MAX_UNIT_KINDS) do
            table.insert(segs, kinds[i].name .. "×" .. kinds[i].n)
        end
        if #kinds > MAX_UNIT_KINDS then
            table.insert(segs, "等" .. #kinds .. "种")
        end
        table.insert(tail, "单位=" .. table.concat(segs, " ") .. " 共" .. total)
    else
        table.insert(tail, "单位=无")
    end

    return head, cities, tail
end

-- 把快照打出去。超长时只砍城市明细（基本信息永远保留）
local function EmitSnapshot(head, cities, tail)
    local function Bytes(t)
        local n = 0
        for _, l in ipairs(t) do n = n + #l end
        return n
    end

    local total = Bytes(head) + Bytes(cities) + Bytes(tail)
    local shown = #cities
    while total > MAX_BYTES and shown > 0 do
        total = total - #cities[shown]
        shown = shown - 1
    end

    print(TAG_BEGIN)
    for _, l in ipairs(head) do print(l) end
    for i = 1, shown do print(cities[i]) end
    if shown < #cities then
        print("（另 " .. (#cities - shown) .. " 座城市明细因篇幅省略）")
    end
    for _, l in ipairs(tail) do print(l) end
    print(TAG_END)
end

-- ==========================================================================
-- 对外入口
-- ==========================================================================

local function DumpGameState()
    local ok, err = pcall(function()
        local head, cities, tail = BuildSnapshot()
        if head then
            EmitSnapshot(head, cities, tail)
        else
            print(SIG_EMPTY)   -- 还没进入战局
        end
    end)
    if not ok then
        -- 出问题时也要喊一声，否则外面只能干等。
        -- 注意标记后面才跟消息：标记本身必须是干净的，外面才好认。
        print(">>>ADVISOR:STATE_ERROR<<< " .. tostring(err))
    end
end

local function Signal(text)
    print(text)
end

-- 点按钮：先导快照，再喊开门。
-- 顺序不能反 —— 外面收到 TOGGLE 就弹窗，那时数据必须已经在了。
local function OnButtonClicked()
    DumpGameState()
    Signal(SIGNAL_TOGGLE)
end

-- ==========================================================================
-- 把按钮摆到顶栏右侧
-- ==========================================================================

-- 让开最右边两个原版按钮（菜单 + 文明百科）。
-- 两个按钮各 29 宽、栈内间距 3，所以是 3+29+3+29 = 64，再多留 3 的缝。
local TOPBAR_KEEP_OUT = 67
-- 按钮中心的纵向位置：顶栏高 29，中心就在 14.5
local TOPBAR_CENTER_Y = 14.5
-- 按钮边长，与 XML 里的 Size 保持一致
local BUTTON_SIZE = 29
-- 重试节奏：每 30 帧（约半秒）一次。SetUpdate 的回调不带帧号，只能自己数。
--
-- 故意**不设"放弃"上限**：一次控件查找的开销可忽略，而顶栏什么时候才就绪
-- 并不确定（读档 / 新开局 / 中途换界面各不相同）。挂上之后每帧只剩一个
-- 布尔判断，等于零成本。以前设了 60 秒上限，配合"退路也算成功"的写法，
-- 结果是永远卡在退路上（见下方 PlaceButton 的说明）。
local RETRY_EVERY_FRAMES = 30
-- 退路位置：让开顶栏那一条（y 约 0~29 被顶栏自己的背景图盖住，
-- 落在那里等于白放）。往下挪这么多个像素，确保在空白地图区域里看得见。
local FALLBACK_BELOW_BAR = 38

-- 摆位置时"没成功"的统一说法。拿它来做判断，避免字符串散落各处。
local WHY_FAILED = "两条路都失败，按钮落在兜底位置"

-- 界面 XML 里声明的锚点与偏移（退路会**改掉**它们）。
-- 值不写死，在 Initialize 里从控件本身读出来 —— 这样改了 XML 也不会对不上。
--
-- ⚠️ HOME_ANCHOR 只用来打日志，**不用来还原**：XML 里那个 C,C 是"两条路都
--    失败"时的兜底锚点，并不适合待在按钮栈里（见 StackChildAnchor 的说明）。
local HOME_ANCHOR, HOME_OFFSET_X, HOME_OFFSET_Y = nil, nil, nil

-- 顶栏按钮栈里原版按钮用的锚点。
--
-- 为什么不能直接用自己的 C,C：
--   XML 里 MenuButton / CivpediaButton **都不写 Anchor**，用的是默认值。
--   栈是按"这一套默认锚点"来排子的；我们的按钮带着 C,C 进去，就会和旁边的
--   原版控件错开**半个身位**（29 像素的一半 ≈ 14）—— 实测表现就是
--   **按钮和顶栏里的文字/图标"有一点点重合"**。
--
-- 不用去猜默认值到底是什么：直接在运行时读原版按钮的。读不到才退回 L,T
--（原版控件不写 Anchor 时的默认值就是 L,T）。
local function StackChildAnchor()
    local a = Safe(function()
        local sib = ContextPtr:LookUpControl("/InGame/TopPanel/RightContents/CivpediaButton")
        if sib == nil then return nil end
        return sib:GetAnchor()
    end)
    if type(a) == "string" and a ~= "" then return a end
    return "L,T"
end

-- ① 首选：挂进顶栏自己的按钮栈。成功返回说明串，失败返回 nil。
--
--    ⚠️ 时机比方法重要，这是社区踩了多年的坑：
--    LookUpControl 问的是**别的上下文**里的控件。我们 Initialize 的那一刻，
--    顶栏常常还没建好 —— 拿到 nil 是常态。更坏的情况是"拿到了一棵
--    马上要被重建的旧控件树"：ChangeParent 不报错，按钮却随着旧树一起
--    被销毁。表现就是"日志写着成功，屏幕上什么都没有"。
--    所以调用方**必须能重试**（见 Initialize），单次结果不算数。
--
--    时机正确的做法见 YnAMP：在 Events.LoadScreenClose 里做 ChangeParent。
local function AttachToTopBar(button)
    local stack = Safe(function()
        return ContextPtr:LookUpControl("/InGame/TopPanel/RightContents")
    end)
    if stack == nil then return nil end

    local usedAnchor = nil
    local ok = Safe(function()
        button:ChangeParent(stack)

        -- ⚠️ 挂进来之后，按钮的锚点和偏移都得归到"栈认的那一套"。
        --
        -- 2026-10-03 实测其①：退路留下的偏移（1198,-748）不清掉，
        -- 按钮会带着它挂进栈 → 位置算到屏幕外，日志却写着"已挂进顶栏按钮栈"。
        -- （栈只算子控件的**基准位置**，控件自己的偏移是**叠加**上去的。）
        --
        -- 2026-10-07 实测其②：锚点也不能用自己的 C,C —— 会跟旁边原版控件
        -- 错开半个身位，看起来就是"按钮和顶栏里的字有一点点重合"。
        -- 所以这里照抄原版按钮的锚点，而不是还原 XML 里的值。
        usedAnchor = StackChildAnchor()
        button:SetAnchor(usedAnchor)
        button:SetOffsetX(HOME_OFFSET_X or 0)
        button:SetOffsetY(HOME_OFFSET_Y or 0)

        -- 栈的布局早就跑完了，后加进来的孩子不会自动归位，得让它重算一次。
        -- （原版也是这么干的：:CalculateSize() 在原版 Lua 里有 517 处调用。）
        stack:CalculateSize()

        -- 移过去后显式打开。控件在"还没参与过布局"的状态下可能带着隐藏
        -- 标记，不打开就是"存在但看不见"——社区范例里 ChangeParent 后面
        -- 都跟着这么一句。
        button:SetHide(false)
        return true
    end)
    if ok then
        -- 把实际用的锚点写进日志：万一还有错位，这行能直接告诉我们读到了什么。
        return "已挂进顶栏按钮栈（锚点=" .. tostring(usedAnchor) .. "）"
    end
    return nil
end

-- ② 退路：自己按屏幕尺寸算绝对坐标。成功返回说明串，失败返回 nil。
--
--    前提：父容器（本 Context 的根控件）尺寸得是屏幕大小，否则
--    Anchor="C,C" 参照的那个"中心"根本不是屏幕中心，坐标全错。
--    而**顶层 Context 的 Size 写在 XML 里是无效的** —— 既吃不到 XML 的
--    Size，也不会自动撑开（UI Plugins Framework 的实证结论）。
--    所以这里必须用 Lua 把根控件显式撑到屏幕尺寸。
local function PlaceByScreenCoords(button)
    local w, h = Safe(function() return UIManager:GetScreenSizeVal() end)
    if type(w) ~= "number" or type(h) ~= "number" or w <= 0 or h <= 0 then
        return nil
    end

    Safe(function()
        ContextPtr:SetSizeX(w)
        ContextPtr:SetSizeY(h)
        return true
    end)

    -- C,C = 控件中心对齐父容器中心。父容器已撑满屏幕 → 等价于屏幕绝对坐标。
    local centerX = w - TOPBAR_KEEP_OUT - BUTTON_SIZE / 2
    local centerY = TOPBAR_CENTER_Y + FALLBACK_BELOW_BAR
    local ok = Safe(function()
        button:SetAnchor("C,C")
        button:SetOffsetX(math.floor(centerX - w / 2))
        button:SetOffsetY(math.floor(centerY - h / 2))
        button:SetHide(false)
        return true
    end)
    if ok then return "按屏幕 " .. w .. "x" .. h .. " 算了绝对坐标" end
    return nil
end

-- 摆位置。
-- 返回值：ok   —— true 表示**真的挂进顶栏按钮栈了**（这是唯一的好结果）
--         why  —— 一句说明，会被写进 Lua.log 方便排查
--
-- ⚠️ 这里有个踩过的坑，务必守住：**"用了退路"不等于"摆好了"。**
--    退路只是先把按钮放到一个看得见的位置顶着用；顶栏一旦就绪，还是要挂进去——
--    因为顶栏里的位置由游戏自己算，换分辨率、改界面缩放都不会错位。
--    曾经把退路也当成成功返回，于是调用方 `placed = true`、重试全部停摆，
--    按钮就永远卡在退路上。而退路的位置在顶栏那一条里，被顶栏背景盖着 ——
--    表现正是"Lua.log 写着按钮就位，屏幕上却什么都没有"。
local function PlaceButton(button)
    local why = AttachToTopBar(button)
    if why ~= nil then return true, why end

    why = PlaceByScreenCoords(button)
    if why ~= nil then return false, why end

    -- 两条路都不行：至少把控件显式打开，它会落在 XML 写的兜底锚点上。
    Safe(function() button:SetHide(false); return true end)
    return false, WHY_FAILED
end

-- 把关键尺寸打进日志，专门给"按钮建了却看不见"这类问题用。
-- 有这行就不必再靠猜：屏幕多大、本上下文的根控件多大、按钮落在哪。
local function PrintDiagnostics(button)
    local sw, sh = Safe(function() return UIManager:GetScreenSizeVal() end)
    local cw, ch = Safe(function()
        return ContextPtr:GetSizeX(), ContextPtr:GetSizeY()
    end)
    local bx = Safe(function() return button:GetOffsetX() end)
    local by = Safe(function() return button:GetOffsetY() end)
    local anc = Safe(function() return button:GetAnchor() end)

    -- 偏移量是**叠加**在父级算出的基准位置之上的。挂进顶栏后还留着几百像素的
    -- 偏移，说明退路那套坐标没被清掉 —— 位置会算到屏幕外。
    -- （2026-10-03 实测就是这个：日志写着"已挂进顶栏按钮栈"，屏幕上看不见。）
    local offVerdict = ""
    if type(bx) == "number" and type(by) == "number"
       and (math.abs(bx) > 200 or math.abs(by) > 200) then
        offVerdict = "（偏移过大 = 退路坐标没清掉，会落到屏幕外）"
    end

    -- 上下文根 = 0x0 是"锚点全算到屏幕外"的头号嫌疑，所以单独给一句判词。
    local rootVerdict = "正常"
    if type(cw) ~= "number" or type(ch) ~= "number" then
        rootVerdict = "取不到"
    elseif cw <= 0 or ch <= 0 then
        rootVerdict = "是 0x0（锚点会算到屏幕外，这是坏消息）"
    end

    print("顾问按钮诊断：屏幕=" .. tostring(sw) .. "x" .. tostring(sh)
        .. " 上下文根=" .. tostring(cw) .. "x" .. tostring(ch) .. "（" .. rootVerdict .. "）"
        .. " 锚点=" .. tostring(anc)
        .. " 按钮偏移=" .. tostring(bx) .. "," .. tostring(by) .. offVerdict)
end

function Initialize()
    local button = Controls.AdvisorToggleButton
    if button == nil then
        -- 界面根本没建出来。这行没有 >>> 标记，外面不会误当成信号。
        print("界面没建出来：Controls.AdvisorToggleButton 不存在")
        return
    end

    -- 先把 XML 里声明的锚点/偏移记下来 —— **必须在任何摆放动作之前**，
    -- 否则退路一改，这里读到的就是退路的值了，等于没记。
    if HOME_ANCHOR == nil then
        HOME_ANCHOR   = Safe(function() return button:GetAnchor() end)
        HOME_OFFSET_X = Safe(function() return button:GetOffsetX() end)
        HOME_OFFSET_Y = Safe(function() return button:GetOffsetY() end)
        print("按钮原始锚点：" .. tostring(HOME_ANCHOR)
            .. " 偏移=" .. tostring(HOME_OFFSET_X) .. "," .. tostring(HOME_OFFSET_Y))
    end

    button:RegisterCallback(Mouse.eLClick, OnButtonClicked)
    -- 有些情况下控件初始带着隐藏标记，先显式打开再说。
    Safe(function() button:SetHide(false); return true end)

    -- 摆按钮。**必须能重试**，而且"退路"不算完成。
    --
    -- 实测的时序：读档进游戏时，我们这份界面跑在加载屏**之前**
    -- （Lua.log 里 AdvisorLauncher 那几行远早于 LoadScreen: OnLoadGameViewStateDone），
    -- 那一刻顶栏还没建好，LookUpControl 拿到 nil 是常态。
    -- 真正就绪要等加载屏关掉。所以只试一次就收工 = 按钮永远留在退路上。
    local attached = false        -- 真的挂进顶栏了吗（唯一的好结果）
    local fallbackShown = false   -- 退路位置是否已经摆过一次

    local function Attempt(tag)
        if attached then return end

        local ok, why = PlaceButton(button)
        if ok then
            attached = true
            print("按钮就位（" .. tag .. "）：" .. why)
            PrintDiagnostics(button)
            return
        end

        -- 退路只摆一次、只报一次；之后每轮只重试"挂进顶栏"（一次控件查找）。
        -- 这样日志里最多两行：一行退路、一行真正挂上。
        if not fallbackShown then
            fallbackShown = true
            print("按钮就位（" .. tag .. "）：顶栏还没就绪，先用退路 → " .. why)
            PrintDiagnostics(button)
        end
    end

    Attempt("初始化")

    -- ① 加载屏关闭时界面才真正就绪 —— 社区推荐的挂载时机
    --    （YnAMP 就是在 Events.LoadScreenClose 里做 ChangeParent 的）。
    --    注意：这个注册**不能**放在 if 里 —— 退路了也要继续等这个时机。
    Safe(function()
        Events.LoadScreenClose.Add(function() Attempt("加载完成") end)
        return true
    end)

    -- ② 再兜一层：每 30 帧试一次，直到真挂上为止（不设放弃时限）。
    --    挂上之后每帧只剩一个布尔判断，开销等于零。
    local frames = 0
    Safe(function()
        ContextPtr:SetUpdate(function()
            if attached then return end
            frames = frames + 1
            if frames % RETRY_EVERY_FRAMES == 0 then
                Attempt("第 " .. tostring(frames) .. " 帧")
            end
        end)
        return true
    end)

    -- 窗口缩放 / 改分辨率后重新摆一次。
    -- 挂在顶栏栈上的情况下这一步是多余的，但重复执行无害。
    Safe(function()
        Events.SystemUpdateUI.Add(function(evtType)
            if evtType == SystemUpdateUI.ScreenResize then
                PlaceButton(button)
            end
        end)
        return true
    end)

    -- 键盘兜底：按 F8 等同于点按钮（F1~F9 里只有 F8 没被游戏占用，见
    -- Base/Assets/Configuration/Data/InputConfiguration.xml）。
    -- VK_ADVISOR 取不到时这个判断恒为 false，不会报错。
    ContextPtr:SetInputHandler(
        function(input)
            if VK_ADVISOR ~= nil
               and input:GetMessageType() == KeyEvents.KeyUp
               and input:GetKey() == VK_ADVISOR then
                OnButtonClicked()
                return true
            end
            return false
        end,
        true
    )

    -- 告诉外面"我装好并加载了"，只用于排查问题
    Signal(SIGNAL_READY)
end

Initialize()
