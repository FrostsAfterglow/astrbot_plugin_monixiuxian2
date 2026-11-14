# tests/test_breakthrough_config.py
"""v3.3.5 回归测试：配置数值被 WebUI 存成字符串后，突破指令不再崩溃。

原缺陷：``VALUES.BREAKTHROUGH_DEATH_PROBABILITY`` 元素可能是字符串，
``random.uniform(a, b)`` 内部执行 ``b - a`` 时抛
``TypeError: unsupported operand type(s) for -: 'str' and 'float'``。
"""
import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

from astrbot_plugin_monixiuxian2.utils.config_values import (  # noqa: E402
    as_float, as_int, as_number_range,
)
from astrbot_plugin_monixiuxian2.core.breakthrough_manager import BreakthroughManager  # noqa: E402
from astrbot_plugin_monixiuxian2.models import Player  # noqa: E402


@pytest.mark.parametrize("raw, expected", [
    ([0.01, 0.1], (0.01, 0.1)),
    (["0.01", "0.1"], (0.01, 0.1)),          # 用户报错的形态
    (["0.01", 0.1], (0.01, 0.1)),            # 混合形态
    ("0.01,0.1", (0.01, 0.1)),                # 整个列表被压成字符串
    ("[0.01, 0.1]", (0.01, 0.1)),             # 列表字面量
    ("0.2 ~ 0.4", (0.2, 0.4)),
    ("10%", (0.1, 0.1)),
    (["1%", "10%"], (0.01, 0.1)),
    (0.05, (0.05, 0.05)),                     # 单值
    ({"min": "0.02", "max": "0.2"}, (0.02, 0.2)),
    (None, (0.01, 0.1)),                      # 缺失回退默认
    ("abc", (0.01, 0.1)),                     # 非法回退默认
    ([], (0.01, 0.1)),
])
def test_as_number_range_handles_any_config_shape(raw, expected):
    got = as_number_range(raw, default=(0.01, 0.1), lo=0.0, hi=1.0)
    assert got[0] == pytest.approx(expected[0], abs=1e-6)
    assert got[1] == pytest.approx(expected[1], abs=1e-6)


def test_range_clamp_and_order():
    assert as_number_range([1.5, -0.3], default=(0.01, 0.1), lo=0.0, hi=1.0) == (0.0, 1.0)
    assert as_number_range([0.9, 0.1], default=(0.01, 0.1)) == (0.1, 0.9)
    assert as_number_range(["50", "500"], cast=int) == (50, 500)


def test_scalar_helpers():
    assert as_float("0.25") == 0.25
    assert as_float("25%") == 0.25
    assert as_float("bad", 1.0) == 1.0
    assert as_float(None, 1.0) == 1.0
    assert as_float(float("nan"), 1.0) == 1.0
    assert as_int("42") == 42
    assert as_int(None, 7) == 7


class _FakeDB:
    def __init__(self):
        self.updated = []

    async def update_player(self, player):
        self.updated.append(player)


class _FakeConfigManager:
    """只提供突破流程需要的境界配置；success_rate=0 强制进入失败分支。"""

    def __init__(self):
        self.level_data = [
            {"level_name": "炼气一层", "exp_needed": 0},
            {"level_name": "炼气二层", "exp_needed": 100, "success_rate": 0.0,
             "breakthrough_lifespan_gain": 0, "breakthrough_mental_power_gain": 0,
             "breakthrough_physical_damage_gain": 0, "breakthrough_magic_damage_gain": 0,
             "breakthrough_physical_defense_gain": 0, "breakthrough_magic_defense_gain": 0,
             "breakthrough_spiritual_qi_gain": 0, "breakthrough_blood_qi_gain": 0},
        ]
        self.body_level_data = self.level_data
        self.pills_data = {}

    def get_level_data(self, cultivation_type):
        return self.level_data


@pytest.mark.parametrize("raw", [
    ["0.01", "0.1"],
    "0.01,0.1",
    [0.01, 0.1],
    None,
    "abc",
])
def test_breakthrough_with_string_config_never_crashes(raw):
    """复现线上堆栈：字符串概率范围 + 字符串倍率，必须走完失败流程而不抛异常。"""
    cfg = {"VALUES": {"BREAKTHROUGH_DEATH_PROBABILITY": raw}}
    db = _FakeDB()
    mgr = BreakthroughManager(db, _FakeConfigManager(), cfg)
    player = Player(user_id="10001", experience=200, level_index=0,
                    cultivation_type="灵修")

    ok, msg, died = asyncio.run(mgr.execute_breakthrough(
        player, pill_name=None, temp_bonus=0.0, death_rate_multiplier="0"
    ))

    assert ok is False
    assert died is False
    assert "突破失败" in msg
    assert db.updated, "失败存活分支应正常落库"


def test_breakthrough_uses_configured_range():
    """配置生效：概率区间被钳制到 0~1，倍率为 0 时必定不死亡。"""
    cfg = {"VALUES": {"BREAKTHROUGH_DEATH_PROBABILITY": ["5", "9"]}}
    db = _FakeDB()
    mgr = BreakthroughManager(db, _FakeConfigManager(), cfg)
    player = Player(user_id="10002", experience=200, level_index=0,
                    cultivation_type="灵修")
    ok, msg, died = asyncio.run(mgr.execute_breakthrough(
        player, death_rate_multiplier=0.0
    ))
    assert ok is False and died is False
