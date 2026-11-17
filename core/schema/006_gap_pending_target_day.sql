-- `gap_pending.missing_through` 语义修正（v0.2.66）。
--
-- 旧版本写的是「缺口**之前**已有的最后一根」，而清理逻辑检查的是
-- 「这一天有没有 bar」——于是补上新缺口后恒为真，把代表**旧洞**的 marker
-- 一起删掉，旧洞失去默认续跑入口（后续 EOD 只看最新末根，再也不会重建它）。
--
-- 新语义是「缺口的目标日」：这一天**没拿到**。判别很干净 ——
-- 目标日按定义没有 bar，旧格式的 last_date 按定义有 bar。
-- 所以「missing_through 当天有 bar」的行就是旧格式，把它推进到下一个交易日。
--
-- 生产实测该表为 0 行（滚动刷新尚在首轮覆盖），这条迁移对全新库是空操作；
-- 它保的是「老库升级」这条路径。
UPDATE gap_pending
   SET missing_through = (
        SELECT MIN(date) FROM daily_bars
         WHERE code = 'sh000300' AND date > gap_pending.missing_through
   )
 WHERE missing_through IS NOT NULL
   AND EXISTS (
        SELECT 1 FROM daily_bars
         WHERE code = gap_pending.code AND date = gap_pending.missing_through
   )
   AND EXISTS (
        SELECT 1 FROM daily_bars
         WHERE code = 'sh000300' AND date > gap_pending.missing_through
   );
