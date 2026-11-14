# tests/test_inventory_card.py
"""v3.3.5 回归测试：「我的信息」第二张背包合成图（全部已有物品）。"""
import asyncio
import io
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")

try:
    from PIL import Image  # noqa: E402
except ImportError:  # pragma: no cover
    Image = None

from astrbot_plugin_monixiuxian2.handlers.player_handler import PlayerHandler  # noqa: E402
from astrbot_plugin_monixiuxian2.models import Player  # noqa: E402
from astrbot_plugin_monixiuxian2.utils.image_generator import get_image_generator  # noqa: E402


class _FakeDB:
    pass


class _FakeConfigManager:
    def __init__(self):
        self.items_data = {"精铁": {"rank": "凡品"}, "一品气血丹": {"rank": "凡品"}}
        self.weapons_data = {"青铜剑": {"rank": "灵品"}}
        self.pills_data = {}
        self.exp_pills_data = {"炼气丹": {"rank": "凡品"}}
        self.utility_pills_data = {}
        self.storage_rings_data = {
            "基础储物戒": {"rank": "凡品", "capacity": 20, "description": "基础容器"}
        }


def _handler():
    """绕过 __init__（其构造依赖框架对象），只装配背包汇总需要的属性。"""
    h = PlayerHandler.__new__(PlayerHandler)
    h.db = _FakeDB()
    h.config_manager = _FakeConfigManager()
    h.config = {}
    return h


def _player():
    return Player(
        user_id="10001", user_name="测试道友",
        pills_inventory=json.dumps({"炼气丹": 5, "一品气血丹": 2}, ensure_ascii=False),
        storage_ring_items=json.dumps({"精铁": 10, "青铜剑": 1}, ensure_ascii=False),
        storage_ring="基础储物戒",
        weapon="青铜剑", armor="", main_technique="",
        techniques=json.dumps(["太上感应篇"], ensure_ascii=False),
    )


def _rows(payload):
    return [it for gp in payload["groups"] for it in gp["items"]]


def test_collect_inventory_covers_all_items():
    payload = _handler()._collect_inventory(_player())
    names = {it["name"] for it in _rows(payload)}

    # 丹药背包 + 储物戒 + 已装备 + 已习功法 + 随身储物戒，一个都不能漏
    assert {"炼气丹", "一品气血丹", "精铁", "青铜剑",
            "太上感应篇", "基础储物戒"} <= names
    assert len(payload["groups"]) == 3
    assert payload["total_kinds"] == len(names)
    assert payload["total_count"] == 21
    assert payload["ring_used"] == 2
    assert payload["ring_capacity"] == 20
    assert payload["dao_hao"] == "测试道友"

    # 品级 / 槽位标签正常带入
    tagged = {it["name"]: it["tag"] for it in _rows(payload)}
    assert "凡品" in tagged["炼气丹"]
    assert "灵品" in tagged["青铜剑"] or "法器" in tagged["青铜剑"]


def test_collect_inventory_handles_empty_bag_and_legacy_dict_count():
    p = Player(
        user_id="10002", user_name="空空道友",
        pills_inventory="{}",
        storage_ring_items=json.dumps({"玄铁": {"count": 3, "bound": False}}, ensure_ascii=False),
        storage_ring="基础储物戒",
        weapon="", armor="", main_technique="", techniques="[]",
    )
    payload = _handler()._collect_inventory(p)
    counts = {it["name"]: it["count"] for it in _rows(payload)}
    assert counts["玄铁"] == 3, "历史 {count: n} 格式需兼容"
    assert payload["ring_used"] == 1
    assert "基础储物戒" in counts, "随身储物戒本身也属于已有物品"


def _png_size(buf):
    data = buf.getvalue()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    with Image.open(io.BytesIO(data)) as im:
        return im.size


@pytest.mark.skipif(Image is None, reason="缺少 Pillow")
def test_inventory_card_renders_valid_png():
    gen = get_image_generator()
    if not gen.is_available():
        pytest.skip("缺少中文字体")
    payload = _handler()._collect_inventory(_player())
    buf = asyncio.run(gen.generate_inventory_card("10001", payload))
    assert buf is not None
    w, h = _png_size(buf)
    assert w == 1080 and h >= 900


@pytest.mark.skipif(Image is None, reason="缺少 Pillow")
def test_inventory_card_empty_bag_still_renders():
    gen = get_image_generator()
    if not gen.is_available():
        pytest.skip("缺少中文字体")
    payload = {
        "dao_hao": "空空道友", "ring_name": "基础储物戒",
        "ring_used": 0, "ring_capacity": 20, "total_kinds": 0, "total_count": 0,
        "groups": [
            {"title": "丹药背包", "subtitle": "PILL BAG · 0 种", "color": "jade", "items": []},
            {"title": "储物戒藏品", "subtitle": "STORAGE RING · 0 种", "color": "gold", "items": []},
        ],
    }
    buf = asyncio.run(gen.generate_inventory_card("10002", payload))
    assert buf is not None
    assert _png_size(buf)[1] >= 900


@pytest.mark.skipif(Image is None, reason="缺少 Pillow")
def test_inventory_card_grows_with_item_count():
    """物品再多也不裁切：画布高度随条目数增长。"""
    gen = get_image_generator()
    if not gen.is_available():
        pytest.skip("缺少中文字体")

    def payload(n):
        return {
            "dao_hao": "多宝道友", "ring_name": "基础储物戒",
            "ring_used": n, "ring_capacity": 99, "total_kinds": n, "total_count": n,
            "groups": [{
                "title": "储物戒藏品", "subtitle": f"STORAGE RING · {n} 种",
                "color": "gold",
                "items": [{"name": f"物品{i}", "tag": "凡品", "count": 1} for i in range(n)],
            }],
        }

    small = asyncio.run(gen.generate_inventory_card("1", payload(2)))
    big = asyncio.run(gen.generate_inventory_card("1", payload(60)))
    assert small is not None and big is not None
    assert _png_size(big)[1] > _png_size(small)[1] + 500


def test_player_info_emits_two_images():
    """核心链路：开启图片卡片时，一次「我的信息」连发档案卡 + 背包卡。"""
    from pathlib import Path
    from astrbot_plugin_monixiuxian2.config_manager import ConfigManager

    class _FakeExt:
        async def get_active_loan(self, uid):
            return None

        async def get_user_cd(self, uid):
            return None

    class _FakeDB:
        def __init__(self, p):
            self.player = p
            self.ext = _FakeExt()

        async def get_player_by_id(self, uid):
            return self.player

        async def update_player(self, p):
            return None

    class _FakeEvent:
        def get_sender_id(self):
            return "10001"

        def get_sender_name(self):
            return "测试道友"

        def get_message_str(self):
            return "我的信息"

        def plain_result(self, text):
            return ("plain", text)

        def chain_result(self, chain):
            return ("chain", len(chain))

        def image_result(self, path):
            return ("image", path)

    cm = ConfigManager(Path(ROOT))
    handler = PlayerHandler(_FakeDB(_player()), {"ENABLE_IMAGE_CARD": True}, cm)

    async def collect():
        return [r async for r in handler.handle_player_info(_FakeEvent())]

    results = asyncio.run(collect())
    assert len(results) == 2, f"应发出 2 张图，实际 {len(results)}: {results}"
    assert all(r[0] in ("chain", "image") for r in results), "不应回退到纯文本"
