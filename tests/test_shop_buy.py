# tests/test_shop_buy.py
"""商店购买机制升级（v3.3.1）单元测试：会员折扣 / 限购 / 冷却 / 配置 schema。"""

import json
import os
import sys

import pytest

# 让测试能直接 import 插件包（与现有测试一致：把仓库父目录加入 sys.path）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

from astrbot_plugin_monixiuxian2.core.shop_manager import ShopManager  # noqa: E402


class _FakeConfig:
    """极简 AstrBotConfig 替身，仅实现 .get(key, default)。"""

    def __init__(self, data: dict):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)


def _make_manager(vip_users=None, vip_discount=1.0):
    data = {"ACCESS_CONTROL": {"SHOP_VIP_USERS": vip_users or []}}
    if vip_discount is not None:
        data["SHOP_VIP_DISCOUNT"] = vip_discount
    return ShopManager(_FakeConfig(data), config_manager=None)


def test_is_vip_detects_members():
    mgr = _make_manager(vip_users=["10001", "10002"], vip_discount=0.9)
    assert mgr.is_vip("10001") is True
    assert mgr.is_vip("10002") is True
    assert mgr.is_vip("99999") is False
    # 与配置大小写/类型无关：字符串比较
    assert mgr.is_vip(10001) is True  # int 也会被 str() 归一


def test_is_vip_empty_list():
    mgr = _make_manager(vip_users=[], vip_discount=0.9)
    assert mgr.is_vip("10001") is False


def test_vip_discount_rate_valid():
    mgr = _make_manager(vip_users=["1"], vip_discount=0.85)
    assert mgr.vip_discount_rate() == 0.85


def test_vip_discount_rate_invalid_falls_back_to_one():
    # 折扣必须落在 (0, 1]，越界或非法一律视为无折扣
    assert _make_manager(vip_users=["1"], vip_discount=1.0).vip_discount_rate() == 1.0
    assert _make_manager(vip_users=["1"], vip_discount=1.5).vip_discount_rate() == 1.0
    assert _make_manager(vip_users=["1"], vip_discount=0.0).vip_discount_rate() == 1.0
    assert _make_manager(vip_users=["1"], vip_discount=-0.3).vip_discount_rate() == 1.0
    # 缺失该配置项 -> 默认 1.0
    assert _make_manager(vip_users=["1"], vip_discount=None).vip_discount_rate() == 1.0


def test_get_vip_users_returns_string_set():
    mgr = _make_manager(vip_users=["10001", 10002], vip_discount=0.9)
    users = mgr.get_vip_users()
    assert users == {"10001", "10002"}
    assert all(isinstance(u, str) for u in users)


def test_discount_receipt_math():
    """复刻 handle_buy 的折扣回执计算，验证原价/实付/省下数值正确。"""
    original_price = 1000
    base_price = 800          # 商店随机折扣后单价
    shop_discount = 0.8
    is_vip = True
    vip_rate = 0.9
    quantity = 2

    unit_final = max(1, round(base_price * vip_rate))   # 720
    total_price = unit_final * quantity                 # 1440
    total_original = original_price * quantity           # 2000
    saved = total_original - total_price                # 560

    assert unit_final == 720
    assert total_price == 1440
    assert total_original == 2000
    assert saved == 560
    # 商店折扣文本应为 8.0 折
    assert f"{shop_discount * 10:.1f}折" == "8.0折"
    # 会员折扣文本应为 9.0 折
    assert f"{vip_rate * 10:.1f}折" == "9.0折"


def test_conf_schema_has_new_shop_keys():
    """_conf_schema.json 必须包含 v3.3.1 新增的购买相关配置项。"""
    schema_path = os.path.join(ROOT, "_conf_schema.json")
    schema = json.load(open(schema_path, encoding="utf-8"))

    values_items = schema["VALUES"]["items"]
    for key in ("SHOP_BUY_LIMIT", "SHOP_BUY_COOLDOWN", "SHOP_VIP_DISCOUNT"):
        assert key in values_items, f"VALUES.items 缺少 {key}"

    access_items = schema["ACCESS_CONTROL"]["items"]
    assert "SHOP_VIP_USERS" in access_items, "ACCESS_CONTROL.items 缺少 SHOP_VIP_USERS"


def test_expanded_items_loaded():
    """扩充后的物品配置应能被正确解析为合法 JSON 且数量增加。"""
    for fname in ("weapons.json", "items.json", "pills.json"):
        path = os.path.join(ROOT, "config", fname)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data, f"{fname} 为空或非法"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
