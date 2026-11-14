MawBuffGuard —— 任务列表（目标追踪器）卡进度修复
================================================

干什么
------
《魔兽世界》12.1.x 有个暴雪 bug：目标追踪器每次排版都会调用
ShouldShowMawBuffs()，而它不加保护地读取玩家光环
(C_UnitAuras.GetAuraDataByIndex("player", 1, "MAW"))。一旦这次调用发生在
被插件污染的上下文里（12.x 的 secret aura 机制），就会抛错：

    GetAuraDataByIndex(): Auras cannot be accessed when secret while tainted by '...'

抛错点在 ObjectiveTracker 的排版流程中间、没有 pcall 保护，于是整轮排版中断：
右侧任务列表的进度文字不再刷新，任务做完了也不变，必须 /reload 才恢复。
上游 issue: https://github.com/Stanzilla/WoWUIBugs/issues/882

本插件把 ShouldShowMawBuffs 换成一个带保护的安全版：
- 在噬渊塔里仍然返回 true（保持原逻辑）；
- 秘文期间（C_Secrets.ShouldAurasBeSecret() 为真）不读光环，直接当作"没有"；
- 外面再套 pcall，任何意外都退化成 false。

这个返回值只决定"要不要显示噬渊 buff 那一小块 UI"，当前版本内容里恒为 false，
所以吞掉错误没有副作用。暴雪修好之后本插件就是空转，可以直接删掉。


安装
----
把 MawBuffGuard 文件夹整个放到：

    World of Warcraft\_retail_\Interface\AddOns\MawBuffGuard\

注意层级：.toc 必须直接在这个文件夹里，别再套一层
（即 AddOns\MawBuffGuard\MawBuffGuard.toc 才是对的）。
装好后 /reload 或重启客户端。


验证
----
游戏内输入：

    /mawbuff

应显示"已接管 ShouldShowMawBuffs"，并给出当前 ShouldAurasBeSecret 的值。


卸载
----
删除文件夹即可，不写任何 SavedVariables，没有残留。


依赖 / 条件
-----------
- 仅正式服（Retail），需要 12.1.5 及以上（toc 里写的是 Interface: 120105,120107）。
- 不依赖任何库（没有 Ace3，没有第三方库文件）。
- 不涉及受保护动作、不做任何自动化，只替换一个纯 UI 判定函数。
- 以后客户端大版本升级、Interface 号变了，可能被判定为过期插件：
  要么改 toc 里的 Interface 号，要么在插件列表勾选"加载过期插件"。


已知限制
--------
- 只修这一条路径（#882）。如果是别的报错导致追踪器卡住，本插件无效。
- 如果同时装了别的替换 ShouldShowMawBuffs 的插件（例如 Aurora 的同类修法），
  两者会互相覆盖，但行为一致，谁最后生效都不影响结果。
