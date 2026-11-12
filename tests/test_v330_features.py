"""v3.3.0 新功能集成测试（真实 AstrBot 环境）：

1. Boss 刷新在主群发出「某某boss刷新」提醒
2. /挑战单人boss 单人副本（不抢世界 Boss 血池）
3. /挑战全服boss 多人讨伐融合并按贡献分奖池
4. 探索固定 15 分钟 + 到期自动结算并主动回发提醒
"""
import asyncio
import sys
import time

sys.path.insert(0, "/workspace")

from astrbot.api.star import Context
from astrbot.api import AstrBotConfig
import astrbot_plugin_monixiuxian2.main as m
from astrbot_plugin_monixiuxian2.boss_raid import RaidState


class _FakePlatform:
    def __init__(self, name):
        self._name = name
    def meta(self):
        class M:
            name = self._name
        return M()


class FakeContext:
    """可捕获 send_message 的平台管理器（用于验证主动推送）。"""
    def __init__(self, platform_name="aiocqhttp"):
        self._platforms = [_FakePlatform(platform_name)]
        self.sent = []  # [(umo, text)]
    def get_insts(self):
        return self._platforms
    async def send_message(self, umo, chain):
        text = ""
        for comp in getattr(chain, "chain", []) or []:
            text += getattr(comp, "text", "")
        self.sent.append((umo, text))
        return True


class Event:
    def __init__(self, text="", uid="10001", name="测试道友", gid="88888"):
        self.text, self.uid, self.name, self.gid = text, uid, name, gid
    def get_sender_id(self): return self.uid
    def get_sender_name(self): return self.name
    def get_message_str(self): return self.text
    def get_message_outline(self): return self.text
    def get_group_id(self): return self.gid
    def is_admin(self): return True
    def is_private_chat(self): return False
    def unified_msg_origin(self): return f"aiocqhttp:GroupMessage:{self.gid}"
    def plain_result(self, t): return ("TEXT", t)
    def chain_result(self, c): return ("CHAIN", c)
    def image_result(self, p): return ("IMAGE", p)
    def get_message_obj(self): return None
    async def send(self, x): pass


async def drive(fn, *a):
    out = []
    async for r in fn(*a):
        out.append(r)
    return out


def cfg():
    c = AstrBotConfig()
    c.update({
        "ACCESS_CONTROL": {"WHITELIST_GROUPS": [], "SHOP_MANAGERS": [], "BOSS_ADMINS": []},
        "VALUES": {"INITIAL_GOLD": 100, "BASE_EXP_PER_MINUTE": 100,
                   "CHECK_IN_GOLD_MIN": 50, "CHECK_IN_GOLD_MAX": 500},
        "FILES": {"DATABASE_FILE": "v330_test.db"},
        "ENABLE_IMAGE_CARD": True,
        "BOSS_BROADCAST": {
            "MAIN_GROUP_ID": "1070840810",
            "MAIN_GROUP_PLATFORM": "aiocqhttp",
            "SPAWN_NOTIFY_ENABLED": True,
        },
        "RIFT_CHECK_INTERVAL": 30,
    })
    return c


PASSED = 0
def check(cond, msg):
    global PASSED
    assert cond, "❌ " + msg
    PASSED += 1
    print("  ✅", msg)


async def make_player(p, uid, name, gid="88888"):
    """用真实指令建角色（保证数据完整）。"""
    await drive(p.handle_start_xiuxian, Event("我要修仙 灵修", uid=uid, name=name, gid=gid), "灵修")
    # 给点修为，让战力足够参与讨伐
    pl = await p.db.get_player_by_id(uid)
    if pl:
        pl.experience = 5_000_000
        await p.db.update_player(pl)


async def main():
    print("=== 1) Boss 刷新 → 主群「某某boss刷新」提醒 ===")
    fc = FakeContext()
    p = m.XiuXianPlugin(Context(*([None] * 11)), cfg())
    p.context = fc
    await p.initialize()
    await make_player(p, "10001", "道友甲")
    fc.sent.clear()
    ok, msg, boss = await p.boss_mgr.spawn_boss(base_exp=100000)
    check(ok and boss, "Boss 生成成功")
    await p._broadcast_boss_spawn(boss)
    check(any("刷新了" in t for _, t in fc.sent), "主群收到「刷新了」提醒")
    check(any("1070840810" in u for u, _ in fc.sent), "提醒发往主群 1070840810")
    check(any("挑战单人boss" in t and "挑战全服boss" in t for _, t in fc.sent),
          "提醒含单人/全服两条挑战入口")

    print("=== 2) /挑战单人boss 单人副本（不抢世界 Boss 血池）===")
    boss_before = (await p.boss_mgr.get_boss_info())[2]
    hp_before = boss_before.hp
    out = await drive(p.handle_boss_fight_solo, Event("挑战单人boss", uid="10001"))
    check(out and out[0][0] == "TEXT", "返回文本结果")
    boss_after = (await p.boss_mgr.get_boss_info())[2]
    check(boss_after.hp == hp_before, "世界 Boss 血量未被单人副本改动（镜像独立）")

    print("=== 3) /挑战全服boss 多人讨伐 + 按贡献分奖池 ===")
    fc.sent.clear()
    await make_player(p, "10002", "道友乙")
    await make_player(p, "10003", "道友丙")
    # 直接把讨伐局血量压低，确保几刀能击杀（验证「最后一击 + 贡献分配」逻辑）
    raid = await p.boss_mgr._load_raid()
    raid.hp = 5000
    await p.boss_mgr._save_raid(raid)
    res = []
    for uid, name in (("10001", "道友甲"), ("10002", "道友乙"), ("10003", "道友丙")):
        ok2, msg2, r2 = await p.boss_handlers.handle_boss_fight_all(uid, name)
        res.append((uid, ok2, r2))
        if r2 and r2.get("killed"):
            break
    killed = [r for r in res if r[2] and r[2].get("killed")]
    check(len(killed) >= 1, "全服讨伐被击杀（最后一击生效）")
    if killed:
        await p._broadcast_raid_defeat(killed[0][2])
        check(any("伤害榜" in t for _, t in fc.sent), "击杀广播含伤害榜")
        # 贡献多者分得更多灵石
        shares = killed[0][2].get("shares", {})
        check(shares and sum(shares.values()) > 0, "奖池已按贡献分配（灵石下发）")
        # 世界 Boss 本体被标记击败
        cur = (await p.boss_mgr.get_boss_info())[2]
        check(cur is None or cur.status == 0, "世界 Boss 本体被标记为已击败")

    print("=== 4) 探索固定 15 分钟 + 到期自动结算并提醒 ===")
    fc.sent.clear()
    ev = Event("探索秘境 1", uid="10001", gid="88888")
    out = await drive(p.handle_rift_explore, ev, 1)
    check(any("15 分钟" in t for _, t in out), "进入秘境提示固定 15 分钟")
    cd = await p.db.ext.get_user_cd("10001")
    check(cd is not None and cd.type == 3, "已进入探索状态（EXPLORING）")
    # 回拨到期时间（绕过真等 15 分钟）
    await p.db.ext.set_user_busy("10001", 3, int(time.time()) - 10,
                                 {"rift_id": 1, "rift_level": 1,
                                  "umo": ev.unified_msg_origin(), "duration": 900})
    done = await p._process_due_explorations(int(time.time()))
    check(done >= 1, "到期探索被自动结算")
    check(any("探索完成提醒" in t for _, t in fc.sent), "向来源会话主动回发完成提醒")
    check(any("88888" in u for u, _ in fc.sent), "提醒精确发回进入秘境的群会话")

    await p.terminate()
    print(f"\n全部通过：{PASSED} 项 ✅")


if __name__ == "__main__":
    asyncio.run(main())
