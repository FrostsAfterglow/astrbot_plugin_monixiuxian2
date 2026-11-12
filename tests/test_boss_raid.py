"""boss_raid.py 全服讨伐纯逻辑确定性测试（融合互动插件多人讨伐机制）。

不依赖数据库/平台，验证伤害计算、奖池分配、进度条、最后一击、序列化。
"""
import sys
import random
import json

sys.path.insert(0, "/workspace")

from astrbot_plugin_monixiuxian2 import boss_raid as R


passed = 0
def check(cond, msg):
    global passed
    assert cond, "❌ " + msg
    passed += 1


class FakeBoss:
    boss_id, boss_name, boss_level = 7, "血魔·金丹境", "金丹"
    max_hp, stone_reward = 1000, 500


# 1) raid_share 守恒 + 按占比 + 余数补给 + 零贡献/零奖池
s = R.raid_share({"a": 60, "b": 30, "c": 10}, 1000)
check(sum(s.values()) == 1000, "奖池守恒")
check(s["a"] > s["b"] > s["c"], "按伤害占比分配")
s2 = R.raid_share({"a": 1, "b": 1, "c": 1}, 10)
check(sum(s2.values()) == 10, "等贡献余数也守恒（分配给末位）")
check(R.raid_share({}, 100) == {}, "空贡献返回空")
check(R.raid_share({"a": 5}, 0) == {}, "零奖池返回空")
s3 = R.raid_share({"a": 10, "b": 0, "c": 5}, 300)
check(sum(s3.values()) == 300 and s3.get("b", 0) == 0, "零贡献者不参与分配")

# 2) raid_damage 线性 + 浮动 + 克制 + 保底
rng = random.Random(42)
d, _ = R.raid_damage(1000, rng=rng)
check(850 <= d <= 1150, "伤害在 ±15% 浮动区间")
_, m2 = R.raid_damage(1000, style="强攻", weakness="强攻", rng=random.Random(1))
_, mc = R.raid_damage(1000, rng=random.Random(1))
check(abs(m2 / mc - R.RAID_COUNTER_UP) < 1e-9, "克制倍率生效")
check(R.raid_damage(0, rng=rng)[0] == R.RAID_MIN_HIT, "零战力保底最小伤害")
check(R.raid_damage(-5, rng=rng)[0] == R.RAID_MIN_HIT, "负战力保底")

# 3) progress_bar
check(len(R.progress_bar(50, 100)) == R.RAID_BAR_WIDTH, "进度条长度固定")
check(R.progress_bar(100, 100).count("█") == R.RAID_BAR_WIDTH, "满血进度全满")

# 4) RaidState 攻防 / 最后一击 / 结算 / 序列化
st = R.RaidState.from_boss(FakeBoss(), hp_mult=1.0, reward_mult=3.0)
check(st.hp == 1000 and st.reward_pool == 1500, "from_boss 血池/奖池正确")
d, killed = st.attack("u1", "道友甲", 300)
check(d == 300 and not killed and st.hp == 700, "第一次出手扣血")
d, killed = st.attack("u2", "道友乙", 300)
check(st.damages == {"u1": 300, "u2": 300}, "累计伤害")
d, killed = st.attack("u1", "道友甲", 9999)  # 超出只记实际 400
check(killed and d == 400 and st.hp == 0 and st.killed_by == "u1", "最后一击只记实际伤害")
check(st.damages["u1"] == 700 and st.damages["u2"] == 300, "伤害仅记实际值")
check(st.progress == 100, "进度 100%")
shares, killer = st.settle()
check(killer == "u1" and sum(shares.values()) == 1500 and shares["u1"] > shares["u2"],
      "结算按贡献分配、最后一击者最多")
check(st.attack("u3", "丙", 100) == (0, False), "结束后拒绝再出手")
st2 = R.RaidState.from_dict(json.loads(json.dumps(st.to_dict())))
check(st2.hp == 0 and st2.damages == st.damages and st2.finished and st2.killed_by == "u1",
      "序列化往返一致")
check(len(st.damage_board()) == 2 and st.damage_board()[0][0] == "u1", "伤害榜按伤害降序")
check("血魔" in st2.render() and "100%" in st2.render(), "render 含名称与进度")

# 5) 无敌讨伐：招式命中为 0 时不应把别人伤害清零
st3 = R.RaidState.from_boss(FakeBoss(), hp_mult=1.0, reward_mult=1.0)
st3.attack("a", "A", 200)
st3.attack("b", "B", 0)  # 无效出手：直接返回，不写入 damages
check(st3.damages == {"a": 200} and st3.hp == 800, "0 伤害不出错、不污染他人贡献")


if __name__ == "__main__":
    print(f"boss_raid 纯逻辑测试通过：{passed} 项 ✅")
