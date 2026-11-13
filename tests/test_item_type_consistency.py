# tests/test_item_type_consistency.py
"""v3.3.4 回归测试：修复「道具与实际使用不符」。

根因：items.json 使用中文类型（丹药/材料/法器/功法），但商店库存生成
（ShopManager._get_all_shop_items）漏做类型映射，导致这些道具在商店里
看得见、查得到，购买时却被 _grant_purchased_item 判为「未知的物品类型」
而发放失败。本测试锁定该链路，防止回归。
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

from astrbot_plugin_monixiuxian2.core.shop_manager import ShopManager  # noqa: E402
from astrbot_plugin_monixiuxian2.handlers.shop_handler import ShopHandler  # noqa: E402

# _grant_purchased_item 能识别并发放的全部英文类型
GRANTABLE_TYPES = {
    'weapon', 'armor', 'main_technique', 'technique', 'accessory', 'material',
    'pill', 'exp_pill', 'utility_pill', 'legacy_pill',
}


class _FakeConfig:
    def __init__(self, data=None):
        self._data = data or {}

    def get(self, key, default=None):
        return self._data.get(key, default)


class _FakeConfigManager:
    """按 name -> config 提供各道具源，足够驱动 _get_all_shop_items。"""

    def __init__(self):
        def load(fname):
            with open(os.path.join(ROOT, "config", fname), encoding="utf-8") as f:
                data = json.load(f)
            seq = data.values() if isinstance(data, dict) else data
            return {v["name"]: v for v in seq if isinstance(v, dict)}

        self.weapons_data = load("weapons.json")
        self.items_data = load("items.json")
        self.pills_data = load("pills.json")
        self.exp_pills_data = load("exp_pills.json")
        self.utility_pills_data = load("utility_pills.json")

    # _get_all_shop_items 只用到上面的 *_data；其余属性留空防御性兜底
    level_data = []
    body_level_data = []


def test_all_generated_shop_items_have_grantable_type():
    """商店库存生成的所有物品，type 必须能被发放逻辑识别（不得残留中文类型）。"""
    mgr = ShopManager(_FakeConfig(), _FakeConfigManager())
    items = mgr._get_all_shop_items()
    assert items, "商店未生成任何物品"
    bad = [(i["name"], i["type"]) for i in items if i["type"] not in GRANTABLE_TYPES]
    assert not bad, f"以下物品购买后会发放失败（未知类型）：{bad}"


def test_items_json_chinese_types_are_fully_mapped():
    """items.json 里的中文类型道具，生成库存后一个都不能漏映射。"""
    mgr = ShopManager(_FakeConfig(), _FakeConfigManager())
    items = mgr._get_all_shop_items()
    from_items = {v["name"] for v in mgr.config_manager.items_data.values()
                  if v.get("shop_weight", 0) > 0 and v.get("price", 0) > 0}
    generated = {i["name"] for i in items}
    missing = from_items - generated
    assert not missing, f"items.json 中可出售道具未进入商店：{sorted(missing)[:10]}"


def test_normalize_item_type_covers_legacy_chinese_types():
    """旧库存（数据库里已是中文类型）经兜底归一化后也能被发放逻辑识别。"""
    cases = {
        '丹药': 'legacy_pill', '材料': 'material', '功法': 'technique',
        '心法': 'main_technique', '武器': 'weapon', '防具': 'armor',
        '饰品': 'accessory',
    }
    for cn, en in cases.items():
        assert ShopHandler._normalize_item_type({'type': cn}) == en
    # 法器按 subtype 细分
    for sub, en in [('武器', 'weapon'), ('防具', 'armor'), ('饰品', 'accessory')]:
        assert ShopHandler._normalize_item_type(
            {'type': '法器', 'data': {'subtype': sub}}) == en
    assert ShopHandler._normalize_item_type({'type': '法器', 'data': {}}) == 'weapon'
    # 已是英文类型时保持不变
    for en in ('weapon', 'legacy_pill', 'pill'):
        assert ShopHandler._normalize_item_type({'type': en}) == en


def test_new_promoted_items_are_purchasable():
    """帮助里宣传的 4 个 items.json 新商品，购买链路类型合法（本次用户反馈重点）。"""
    mgr = ShopManager(_FakeConfig(), _FakeConfigManager())
    items = {i["name"]: i for i in mgr._get_all_shop_items()}
    for name in ('太上感应篇', '混元一气幡', '万年温玉', '九转还魂丹'):
        assert name in items, f"{name} 未出现在商店中"
        assert items[name]["type"] in GRANTABLE_TYPES, \
            f"{name} 类型 {items[name]['type']} 会导致购买失败"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
