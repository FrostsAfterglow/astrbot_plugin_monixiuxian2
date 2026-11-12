# boss_raid.py
"""全服 Boss 讨伐 —— 纯逻辑层（无 IO，可独立测试）。

设计来源：融合「互动」插件 ``games_world.py`` 的多人讨伐（BossFight）机制，
把「一个人单挑」升级为「全服共享一条血池、按累计伤害贡献分配奖池」的协作玩法。
移植自互动插件的核心机制：

* :class:`RaidState` 对应 ``BossFight`` —— 共享血池、``damages`` 累计伤害、
  ``attacks`` 出手数、最后一击 ``killed_by``、进度条 ``render()``；
* :func:`raid_share` 对应 ``boss_share`` —— 按伤害占比分配奖池、
  整除余数补给贡献第一，保证 ``Σ分配 == pool`` 不凭空造钱；
* :func:`raid_damage` 借鉴 ``battle_damage`` 的随机浮动与「克制打法」，
  但伤害基数改为修仙体系里的**玩家战力线性值**（见下方说明）。

为什么要改伤害公式：互动插件的 Boss 血池是几百~几千，所以用对数刻度把
战力压成几十点伤害；修仙插件的 Boss 血量是数万~数十万，若照搬对数伤害会
导致「永远打不死」。因此这里保留协作机制、把伤害基数换成与血池同量级的
玩家战力（攻击力）线性值，让多人协作在合理次数内可分出胜负。
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


# ---- 数值常量（集中放置，便于调节 / 测试） ----
RAID_SWING = 0.15          # 单次伤害随机浮动 ±15%（互动插件 BATTLE_SWING=0.15）
RAID_MIN_HIT = 1           # 保底伤害，避免新号完全打不动
RAID_COUNTER_UP = 1.15     # 克制打法加成
RAID_BAR_WIDTH = 12        # 进度条宽度


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def raid_damage(power: int, style: str = "", weakness: str = "",
                rng: "random.Random | None" = None) -> Tuple[int, float]:
    """计算一次「挑战全服boss」出手造成的伤害。

    Args:
        power: 玩家战力（取修仙角色的攻击力；<=0 时退化为保底伤害）。
        style: 本次打法（如「强攻」），命中 Boss 弱点时获得克制加成。
        weakness: 当前 Boss 的弱点打法名。
        rng: 随机源，便于测试注入。

    Returns:
        ``(伤害, 本次实际倍率)``。伤害恒 >= RAID_MIN_HIT。
    """
    rng = rng or random
    base = max(0, _safe_int(power, 0))
    swing = 1.0 + rng.uniform(-RAID_SWING, RAID_SWING)
    counter = RAID_COUNTER_UP if (style and weakness and style == weakness) else 1.0
    multiplier = swing * counter
    if base <= 0:
        return RAID_MIN_HIT, multiplier
    return max(RAID_MIN_HIT, int(base * multiplier)), multiplier


def raid_share(damages: Dict[str, int], pool: int) -> Dict[str, int]:
    """按累计伤害占比把奖池分给参与者（移植互动插件 ``boss_share``）。

    刻意用整除 + 余数补给第一名，保证 ``Σ分配 == pool`` 恒成立，
    不会因为四舍五入凭空多出或少掉灵石。总伤害为 0 或奖池为 0 时返回空字典。
    """
    total = sum(max(0, _safe_int(v, 0)) for v in damages.values())
    total_pool = max(0, _safe_int(pool, 0))
    if total <= 0 or total_pool <= 0 or not damages:
        return {}
    result: Dict[str, int] = {}
    for uid, value in damages.items():
        dmg = max(0, _safe_int(value, 0))
        if dmg > 0:
            result[uid] = total_pool * dmg // total
    remainder = total_pool - sum(result.values())
    if remainder and result:
        top = max(damages, key=lambda k: (max(0, _safe_int(damages[k], 0)), k))
        result[top] = result.get(top, 0) + remainder
    return result


def progress_bar(done: int, total: int, width: int = RAID_BAR_WIDTH) -> str:
    """文本进度条，形如 ``██████░░░░░``。"""
    if total <= 0:
        return "█" * width
    filled = min(width, max(0, int(width * max(0, done) / total)))
    return "█" * filled + "░" * (width - filled)


@dataclass
class RaidState:
    """一场全服 Boss 讨伐的状态（对应互动插件 ``BossFight``）。

    以 JSON 形式持久化在 ``system_config`` 的 ``boss_raid_state`` 键里，
    插件重启后不丢失；每次刷新新 Boss 时由 ``init_raid`` 重建。
    """

    boss_id: int
    boss_name: str
    boss_level: str
    hp: int
    hp_max: int
    reward_pool: int
    damages: Dict[str, int] = field(default_factory=dict)
    attacks: Dict[str, int] = field(default_factory=dict)
    names: Dict[str, str] = field(default_factory=dict)
    weakness: str = ""
    killed_by: str = ""
    finished: bool = False
    started_at: int = 0

    @classmethod
    def from_boss(cls, boss, hp_mult: float = 1.0, reward_mult: float = 3.0) -> "RaidState":
        """从一只世界 Boss 生成讨伐局。

        Args:
            boss: :class:`~models_extended.Boss` 实例。
            hp_mult: 血池倍率（全服讨伐通常 >1，让多人协作更有意义）。
            reward_mult: 奖池倍率（多人分，比单人奖励更高）。
        """
        hp_max = max(1, int(boss.max_hp * hp_mult))
        return cls(
            boss_id=int(boss.boss_id),
            boss_name=str(boss.boss_name),
            boss_level=str(boss.boss_level),
            hp=hp_max,
            hp_max=hp_max,
            reward_pool=max(0, int(boss.stone_reward * reward_mult)),
            started_at=int(time.time()),
        )

    def to_dict(self) -> dict:
        return {
            "boss_id": self.boss_id, "boss_name": self.boss_name,
            "boss_level": self.boss_level, "hp": self.hp, "hp_max": self.hp_max,
            "reward_pool": self.reward_pool, "damages": self.damages,
            "attacks": self.attacks, "names": self.names,
            "weakness": self.weakness, "killed_by": self.killed_by,
            "finished": self.finished, "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RaidState":
        return cls(
            boss_id=_safe_int(d.get("boss_id"), 0),
            boss_name=str(d.get("boss_name", "")),
            boss_level=str(d.get("boss_level", "")),
            hp=_safe_int(d.get("hp"), 0),
            hp_max=_safe_int(d.get("hp_max"), 1),
            reward_pool=_safe_int(d.get("reward_pool"), 0),
            damages={str(k): _safe_int(v) for k, v in (d.get("damages") or {}).items()},
            attacks={str(k): _safe_int(v) for k, v in (d.get("attacks") or {}).items()},
            names={str(k): str(v) for k, v in (d.get("names") or {}).items()},
            weakness=str(d.get("weakness", "")),
            killed_by=str(d.get("killed_by", "")),
            finished=bool(d.get("finished", False)),
            started_at=_safe_int(d.get("started_at"), 0),
        )

    @property
    def progress(self) -> int:
        """已削血百分比（0~100）。"""
        if self.hp_max <= 0:
            return 100
        return min(100, max(0, (self.hp_max - self.hp) * 100 // self.hp_max))

    @property
    def total_damage(self) -> int:
        return sum(max(0, v) for v in self.damages.values())

    def attendance(self) -> int:
        """有效参战人数（伤害 > 0）。"""
        return sum(1 for v in self.damages.values() if v > 0)

    def attack(self, uid: str, name: str, damage: int) -> Tuple[int, bool]:
        """对讨伐局出手一次。

        记录**实际打出**的伤害（Boss 只剩一点血时请求 300 点只能记 20 点，
        避免贡献占比虚高），返回 ``(实际伤害, 本次是否完成最后一击)``。
        """
        if self.finished:
            return 0, False
        want = max(0, _safe_int(damage, 0))
        if want <= 0:
            return 0, False
        dealt = min(want, self.hp)
        self.damages[str(uid)] = self.damages.get(str(uid), 0) + dealt
        self.attacks[str(uid)] = self.attacks.get(str(uid), 0) + 1
        if name:
            self.names[str(uid)] = name
        self.hp -= dealt
        if self.hp <= 0:
            self.hp = 0
            self.finished = True
            self.killed_by = str(uid)
            return dealt, True
        return dealt, False

    def render(self) -> str:
        """渲染血条进度，形如 ``🐲 血魔·金丹境 62% [██████░░░░] 22800/60000``。"""
        tail = f"（弱点：{self.weakness}）" if self.weakness else ""
        return (
            f"🐲 {self.boss_name} {self.progress}% "
            f"[{progress_bar(self.hp_max - self.hp, self.hp_max)}] "
            f"{self.hp}/{self.hp_max}{tail}"
        )

    def damage_board(self, top: int = 8) -> List[Tuple[str, int, int]]:
        """伤害榜：``[(uid, 累计伤害, 出手数), ...]``，按伤害降序。"""
        rows = [(uid, dmg, self.attacks.get(uid, 0))
                for uid, dmg in self.damages.items() if dmg > 0]
        rows.sort(key=lambda r: (-r[1], r[0]))
        return rows[:top]

    def settle(self) -> Tuple[Dict[str, int], str]:
        """结算奖池：``({uid: 灵石}, 最后一击者)``；未击杀返回空字典。"""
        if not self.finished:
            return {}, ""
        return raid_share(self.damages, self.reward_pool), self.killed_by
