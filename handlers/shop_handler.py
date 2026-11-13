# handlers/shop_handler.py

import time
import re
from astrbot.api.event import AstrMessageEvent
from astrbot.api import AstrBotConfig, logger
from ..data import DataBase
from ..core import ShopManager, EquipmentManager, PillManager, StorageRingManager
from ..models import Player
from ..config_manager import ConfigManager
from .utils import player_required

__all__ = ["ShopHandler"]

class ShopHandler:
    """商店处理器"""
    
    ITEM_ACQUIRE_HINTS = {
        'pill': "丹阁刷新、秘境稀有掉落",
        'exp_pill': "丹阁、炼丹系统、历练/秘境奖励",
        'utility_pill': "丹阁稀有、秘境/Boss 掉落",
        'legacy_pill': "百宝阁限量，购买后立即生效",
        'weapon': "器阁、Boss 掉落",
        'armor': "器阁、Boss 掉落",
        'accessory': "器阁、Boss 掉落",
        'main_technique': "百宝阁稀有刷新",
        'technique': "百宝阁、Boss 掉落",
        'material': "历练、秘境、悬赏、灵田收获与百宝阁限量",
    }

    # items.json 历史中文类型 -> 发放逻辑识别的英文类型。
    # 与 ShopManager._map_legacy_item_type 保持一致，用于兼容升级前已入库的旧库存。
    _CN_TYPE_MAP = {
        '武器': 'weapon', '防具': 'armor', '饰品': 'accessory',
        '心法': 'main_technique', '功法': 'technique',
        '材料': 'material', '丹药': 'legacy_pill',
    }

    @classmethod
    def _normalize_item_type(cls, item: dict) -> str:
        """把商店物品的中文类型归一化为英文类型（兼容旧库存数据）。"""
        t = item.get('type', '')
        if t == '法器':
            sub = (item.get('data') or {}).get('subtype', '') or item.get('subtype', '')
            return {'武器': 'weapon', '防具': 'armor', '饰品': 'accessory'}.get(sub, 'weapon')
        return cls._CN_TYPE_MAP.get(t, t)

    def __init__(self, db: DataBase, config: AstrBotConfig, config_manager: ConfigManager):
        self.db = db
        self.config = config
        self.config_manager = config_manager
        self.shop_manager = ShopManager(config, config_manager)
        self.storage_ring_manager = StorageRingManager(db, config_manager)
        self.equipment_manager = EquipmentManager(db, config_manager, self.storage_ring_manager)
        self.pill_manager = PillManager(db, config_manager)
        access_control = self.config.get("ACCESS_CONTROL", {})
        self.shop_manager_ids = {
            str(user_id)
            for user_id in access_control.get("SHOP_MANAGERS", [])
        }
        # 购买冷却记录（内存表）：user_id -> 上次购买时间戳（防刷，重启后清空）
        self._buy_cooldowns: dict = {}

    async def _ensure_pavilion_refreshed(self, pavilion_id: str, item_getter, count: int) -> None:
        """确保阁楼已刷新"""
        last_refresh_time, current_items = await self.db.get_shop_data(pavilion_id)
        if current_items:
            updated = self.shop_manager.ensure_items_have_stock(current_items)
            if updated:
                await self.db.update_shop_data(pavilion_id, last_refresh_time, current_items)
        refresh_hours = self.config.get("PAVILION_REFRESH_HOURS", 6)
        if not current_items or self.shop_manager.should_refresh_shop(last_refresh_time, refresh_hours):
            new_items = self.shop_manager.generate_pavilion_items(item_getter, count)
            await self.db.update_shop_data(pavilion_id, int(time.time()), new_items)

    async def handle_pill_pavilion(self, event: AstrMessageEvent, args: str = ""):
        """处理丹阁命令 - 展示丹药列表 / 支持「丹阁 购买 <物品> [数量]」"""
        args = (args or "").strip()
        if args:
            if args.startswith("购买"):
                async for r in self._buy_from_pavilion(event, "pill_pavilion", "丹阁"):
                    yield r
                return
            yield event.plain_result("用法：丹阁 购买 <物品名> [数量]，或单独发送「丹阁」查看丹药列表。")
            return
        count = self.config.get("PAVILION_PILL_COUNT", 10)
        await self._ensure_pavilion_refreshed("pill_pavilion", self.shop_manager.get_pills_for_display, count)
        last_refresh, items = await self.db.get_shop_data("pill_pavilion")
        if not items:
            yield event.plain_result("丹阁暂无丹药出售。")
            return
        refresh_hours = self.config.get("PAVILION_REFRESH_HOURS", 6)
        display = self.shop_manager.format_pavilion_display("丹阁", items, refresh_hours, last_refresh)
        yield event.plain_result(display)

    async def handle_weapon_pavilion(self, event: AstrMessageEvent, args: str = ""):
        """处理器阁命令 - 展示武器列表 / 支持「器阁 购买 <物品> [数量]」"""
        args = (args or "").strip()
        if args:
            if args.startswith("购买"):
                async for r in self._buy_from_pavilion(event, "weapon_pavilion", "器阁"):
                    yield r
                return
            yield event.plain_result("用法：器阁 购买 <物品名> [数量]，或单独发送「器阁」查看武器列表。")
            return
        count = self.config.get("PAVILION_WEAPON_COUNT", 10)
        await self._ensure_pavilion_refreshed("weapon_pavilion", self.shop_manager.get_weapons_for_display, count)
        last_refresh, items = await self.db.get_shop_data("weapon_pavilion")
        if not items:
            yield event.plain_result("器阁暂无武器出售。")
            return
        refresh_hours = self.config.get("PAVILION_REFRESH_HOURS", 6)
        display = self.shop_manager.format_pavilion_display("器阁", items, refresh_hours, last_refresh)
        yield event.plain_result(display)

    async def handle_treasure_pavilion(self, event: AstrMessageEvent, args: str = ""):
        """处理百宝阁命令 - 展示所有物品 / 支持「百宝阁 购买 <物品> [数量]」"""
        args = (args or "").strip()
        if args:
            if args.startswith("购买"):
                async for r in self._buy_from_pavilion(event, "treasure_pavilion", "百宝阁"):
                    yield r
                return
            yield event.plain_result("用法：百宝阁 购买 <物品名> [数量]，或单独发送「百宝阁」查看物品列表。")
            return
        count = self.config.get("PAVILION_TREASURE_COUNT", 15)
        await self._ensure_pavilion_refreshed("treasure_pavilion", self.shop_manager.get_all_items_for_display, count)
        last_refresh, items = await self.db.get_shop_data("treasure_pavilion")
        if not items:
            yield event.plain_result("百宝阁暂无物品出售。")
            return
        refresh_hours = self.config.get("PAVILION_REFRESH_HOURS", 6)
        display = self.shop_manager.format_pavilion_display("百宝阁", items, refresh_hours, last_refresh)
        yield event.plain_result(display)

    async def _find_item_in_pavilions(self, item_name: str, pavilion_id: str = None):
        """在所有阁楼（或指定 ``pavilion_id`` 阁楼）中查找物品。"""
        if pavilion_id:
            pavilions = [pavilion_id]
        else:
            pavilions = ["pill_pavilion", "weapon_pavilion", "treasure_pavilion"]
        for pid in pavilions:
            _, items = await self.db.get_shop_data(pid)
            if items:
                for item in items:
                    if item['name'] == item_name and item.get('stock', 0) > 0:
                        return pid, item
        return None, None

    @staticmethod
    def _parse_buy_target(item_name: str, event, use_raw_fallback: bool = True) -> tuple:
        """解析购买指令中的物品名与数量。

        兼容全角空格/数字、``x10`` 写法；当指令参数仅含名称且
        ``use_raw_fallback`` 为真时，再从原始消息兜底解析一次数量。
        返回 ``(名称, 数量)``。

        ``use_raw_fallback`` 在「限定阁购买」(如 ``/丹阁 购买 炼气丹 1``)
        场景必须置为 ``False``，否则原始消息 ``/丹阁 购买 ...`` 会被整体
        当成物品名误解析。
        """

        def parse(text: str):
            text = text.strip().replace("　", " ")
            text = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
            text = re.sub(r"\s+", " ", text)
            m = re.match(r"^(.*?)(?:\s+(\d+)|[xX＊*]\s*(\d+))$", text)
            if m:
                part = m.group(1).strip()
                qty = int(m.group(2) or m.group(3))
                return part, max(1, qty)
            return text.strip(), 1

        name, qty = parse(item_name or "")
        # 指令参数仅含名称时，从原始消息兜底解析数量
        if qty == 1 and use_raw_fallback:
            try:
                raw = event.get_message_str().strip()
                if raw.startswith("购买"):
                    raw = raw[len("购买"):].strip()
                name, qty = parse(raw)
            except Exception:
                pass
        return name, qty

    async def _grant_purchased_item(self, player: Player, target_item: dict, quantity: int) -> tuple:
        """按物品类型把已购物品发放到玩家（储物戒/丹药背包/即时生效）。

        返回 ``(是否成功, 提示文本)``；失败提示用于在事务内回滚。
        """
        # items.json 历史上使用中文类型，升级前已写入数据库的商店库存仍是中文。
        # 这里做一次兜底归一化，保证老存档玩家也能正常购买这些道具。
        item_type = self._normalize_item_type(target_item)
        name = target_item['name']

        # 储物类：武器/防具/心法/功法/饰品/材料
        if item_type in ('weapon', 'armor', 'main_technique', 'technique', 'accessory', 'material'):
            label = {
                'weapon': '武器', 'armor': '防具', 'main_technique': '心法',
                'technique': '功法', 'accessory': '饰品', 'material': '材料',
            }.get(item_type, '物品')
            success, msg = await self.storage_ring_manager.store_item(
                player, name, quantity, external_transaction=True)
            if success:
                return True, f"成功购买{label}【{name}】x{quantity}，已存入储物戒。"
            return True, f"成功购买【{name}】x{quantity}。\n⚠️ 存入储物戒失败：{msg}"

        if item_type in ('pill', 'exp_pill', 'utility_pill'):
            await self.pill_manager.add_pill_to_inventory(player, name, count=quantity)
            return True, f"成功购买【{name}】x{quantity}，已添加到背包。"

        if item_type == 'legacy_pill':
            success, message = await self._apply_legacy_pill_effects(player, target_item, quantity)
            if not success:
                return False, message
            return True, message

        return False, f"未知的物品类型：{item_type}"

    @player_required
    async def handle_buy(self, player: Player, event: AstrMessageEvent, item_name: str = ""):
        """处理全局购买物品命令（/购买 <物品> [数量]）"""
        item_name, quantity = self._parse_buy_target(item_name, event)
        if not item_name:
            yield event.plain_result("请指定要购买的物品名称，例如：购买 青铜剑")
            return
        async for r in self._purchase_core(player, event, item_name, quantity):
            yield r

    @staticmethod
    def _extract_buy_rest(raw: str) -> str:
        """从原始消息提取「购买」之后的 ``<物品> [数量]``。

        例如 ``/丹阁 购买 炼气丹 1`` -> ``炼气丹 1``；不含「购买」则返回空串。
        """
        m = re.search(r"购买\s*(.*)$", raw or "")
        return m.group(1).strip() if m else ""

    @player_required
    async def _buy_from_pavilion(self, player: Player, event: AstrMessageEvent,
                                pavilion_id: str, pavilion_label: str):
        """限定阁购买（如 ``/丹阁 购买 炼气丹 1``）。

        直接从原始消息中提取「购买」之后的 ``<物品> [数量]``，避免依赖框架
        普通参数只传首词的限制。解析时关闭原始消息兜底，避免把
        ``/丹阁 购买 ...`` 整体误当物品名。
        """
        rest = self._extract_buy_rest(event.get_message_str())
        item_name, quantity = self._parse_buy_target(
            rest, event, use_raw_fallback=False)
        if not item_name:
            yield event.plain_result(
                f"用法：{pavilion_label} 购买 <物品名> [数量]\n"
                f"示例：{pavilion_label} 购买 炼气丹 1"
            )
            return
        async for r in self._purchase_core(player, event, item_name, quantity, pavilion_id):
            yield r

    async def _purchase_core(self, player: Player, event: AstrMessageEvent,
                            item_name: str, quantity: int, pavilion_id: str = None):
        """购买核心逻辑（全局购买与限定阁购买共用）。

        ``pavilion_id`` 指定时只在该阁内查找；为 ``None`` 时在所有阁查找。
        """
        found_pavilion, target_item = await self._find_item_in_pavilions(item_name, pavilion_id)
        if not target_item:
            if pavilion_id:
                yield event.plain_result(
                    f"【{item_name}】不在本阁出售或已售罄，请检查名称或等待刷新。")
            else:
                yield event.plain_result(f"没有找到【{item_name}】，请检查物品名称或等待刷新。")
            return

        stock = target_item.get('stock', 0)
        if quantity > stock:
            yield event.plain_result(f"【{item_name}】库存不足，当前库存: {stock}。")
            return

        # 限购：单次购买数量上限
        buy_limit = int(self.config.get("SHOP_BUY_LIMIT", 0) or 0)
        if buy_limit > 0 and quantity > buy_limit:
            yield event.plain_result(f"单次限购 {buy_limit} 个，您想购买 {quantity} 个，请分次购买。")
            return

        # 购买冷却（防刷）
        user_id = event.get_sender_id()
        cooldown = int(self.config.get("SHOP_BUY_COOLDOWN", 0) or 0)
        if cooldown > 0:
            last_buy = self._buy_cooldowns.get(user_id, 0)
            now_ts = int(time.time())
            remain = cooldown - (now_ts - last_buy)
            if last_buy and remain > 0:
                yield event.plain_result(f"购买过于频繁，请 {remain} 秒后再试。")
                return

        # 价格计算（含会员折扣）
        base_price = target_item['price']
        original_price = target_item.get('original_price', base_price)
        shop_discount = target_item.get('discount', 1.0)
        is_vip = self.shop_manager.is_vip(user_id)
        vip_rate = self.shop_manager.vip_discount_rate() if is_vip else 1.0
        unit_final = max(1, round(base_price * vip_rate))
        total_price = unit_final * quantity
        total_original = original_price * quantity

        result_lines = []

        await self.db.conn.execute("BEGIN IMMEDIATE")
        try:
            # 事务内重新读取玩家，确保扣费基于最新数据（单一权威校验）
            player = await self.db.get_player_by_id(user_id)
            if player.gold < total_price:
                await self.db.conn.rollback()
                vip_hint = "（会员价）" if is_vip else ""
                yield event.plain_result(
                    f"灵石不足！\n【{target_item['name']}】单价: {unit_final} 灵石{vip_hint}\n"
                    f"购买数量: {quantity}\n需要灵石: {total_price}\n你的灵石: {player.gold}"
                )
                return

            reserved, _, remaining = await self.db.decrement_shop_item_stock(found_pavilion, item_name, quantity, external_transaction=True)
            if not reserved:
                await self.db.conn.rollback()
                yield event.plain_result(f"【{item_name}】已售罄，请等待刷新。")
                return

            granted, grant_msg = await self._grant_purchased_item(player, target_item, quantity)
            if not granted:
                await self.db.conn.rollback()
                yield event.plain_result(grant_msg)
                return
            result_lines.append(grant_msg)

            player.gold -= total_price
            await self.db.update_player(player)
            await self.db.conn.commit()

            # 记录购买冷却（防刷）
            if cooldown > 0:
                self._buy_cooldowns[user_id] = int(time.time())

            # 回执：显示原价 / 商店折扣 / 会员折扣 / 实付 / 省下灵石
            result_lines.append("——")
            if original_price != base_price:
                result_lines.append(f"原价: {total_original} 灵石（商店{shop_discount * 10:.1f}折）")
            else:
                result_lines.append(f"原价: {total_original} 灵石")
            if is_vip:
                result_lines.append(f"会员折扣: {vip_rate * 10:.1f}折")
            result_lines.append(f"实付: {total_price} 灵石（剩余 {player.gold}）")
            saved = total_original - total_price
            if saved > 0:
                result_lines.append(f"共省下: {saved} 灵石")
            result_lines.append(f"剩余库存: {remaining}" if remaining > 0 else "该物品已售罄！")
            yield event.plain_result("\n".join(result_lines))
        except Exception as e:
            await self.db.conn.rollback()
            logger.error(f"购买异常: {e}")
            raise

    def _get_acquire_hint(self, item_type: str) -> str:
        """根据类型返回获取提示"""
        return self.ITEM_ACQUIRE_HINTS.get(item_type, "商店刷新或活动奖励")

    async def handle_item_info(self, event: AstrMessageEvent, item_name: str = ""):
        """查询物品/丹药的具体效果与获取方式"""
        if not item_name or item_name.strip() == "":
            yield event.plain_result(
                "请指定要查询的物品名称\n"
                "用法：物品信息 <名称>\n"
                "示例：物品信息 筑基丹"
            )
            return

        item = self.shop_manager.find_item_by_name(item_name.strip())
        if not item:
            yield event.plain_result(f"未找到物品【{item_name}】，请检查名称或等待刷新。")
            return

        detail_text = self.shop_manager.get_item_details(item)
        acquire_hint = self._get_acquire_hint(item.get('type', ''))

        lines = [
            detail_text,
            f"获取途径：{acquire_hint}",
            "💡 使用 /丹阁、/器阁、/百宝阁 查看当前售卖物品"
        ]
        yield event.plain_result("\n".join(lines))

    async def _apply_legacy_pill_effects(self, player: Player, item: dict, quantity: int) -> tuple:
        """应用旧系统丹药效果（items.json中的丹药）

        Args:
            player: 玩家对象
            item: 物品配置字典
            quantity: 购买数量

        Returns:
            (是否成功, 消息)
        """
        effects = item.get('data', {}).get('effect', {})
        if not effects:
            return False, f"丹药【{item['name']}】无效果配置。"

        effect_msgs = []
        pill_name = item['name']

        # 处理各种效果（乘以数量）
        for _ in range(quantity):
            # 恢复/扣除气血
            if 'add_hp' in effects:
                hp_change = effects['add_hp']
                if player.cultivation_type == "体修":
                    old_blood = player.blood_qi
                    player.blood_qi = max(0, min(player.max_blood_qi, player.blood_qi + hp_change))
                    if hp_change > 0:
                        effect_msgs.append(f"气血+{player.blood_qi - old_blood}")
                    else:
                        effect_msgs.append(f"气血{hp_change}")
                else:
                    old_qi = player.spiritual_qi
                    player.spiritual_qi = max(0, min(player.max_spiritual_qi, player.spiritual_qi + hp_change))
                    if hp_change > 0:
                        effect_msgs.append(f"灵气+{player.spiritual_qi - old_qi}")
                    else:
                        effect_msgs.append(f"灵气{hp_change}")

            # 增加修为
            if 'add_experience' in effects:
                exp_gain = effects['add_experience']
                player.experience += exp_gain
                effect_msgs.append(f"修为+{exp_gain}")

            # 增加最大气血/灵气上限
            if 'add_max_hp' in effects:
                max_hp_gain = effects['add_max_hp']
                if player.cultivation_type == "体修":
                    player.max_blood_qi += max_hp_gain
                    effect_msgs.append(f"最大气血+{max_hp_gain}")
                else:
                    player.max_spiritual_qi += max_hp_gain
                    effect_msgs.append(f"最大灵气+{max_hp_gain}")

            # 增加灵力（映射到法伤）
            if 'add_spiritual_power' in effects:
                sp_gain = effects['add_spiritual_power']
                player.magic_damage += sp_gain
                effect_msgs.append(f"法伤+{sp_gain}")

            # 增加精神力
            if 'add_mental_power' in effects:
                mp_gain = effects['add_mental_power']
                player.mental_power += mp_gain
                effect_msgs.append(f"精神力+{mp_gain}")

            # 增加攻击力（映射到物伤）
            if 'add_attack' in effects:
                atk_gain = effects['add_attack']
                player.physical_damage += atk_gain
                if atk_gain > 0:
                    effect_msgs.append(f"物伤+{atk_gain}")
                else:
                    effect_msgs.append(f"物伤{atk_gain}")

            # 增加防御力（映射到物防）
            if 'add_defense' in effects:
                def_gain = effects['add_defense']
                player.physical_defense += def_gain
                if def_gain > 0:
                    effect_msgs.append(f"物防+{def_gain}")
                else:
                    effect_msgs.append(f"物防{def_gain}")

            # 增加/扣除灵石
            if 'add_gold' in effects:
                gold_change = effects['add_gold']
                player.gold += gold_change
                if gold_change > 0:
                    effect_msgs.append(f"灵石+{gold_change}")
                else:
                    effect_msgs.append(f"灵石{gold_change}")

            # 处理突破成功率加成（添加为临时效果，持续1小时）
            if 'add_breakthrough_bonus' in effects:
                bonus = effects['add_breakthrough_bonus']
                current_effects = player.get_active_pill_effects()
                new_effect = {
                    "pill_name": pill_name,
                    "subtype": "breakthrough_boost",
                    "breakthrough_bonus": bonus,
                    "expiry_time": int(time.time()) + 3600,  # 1小时有效期
                }
                current_effects.append(new_effect)
                player.set_active_pill_effects(current_effects)
                if bonus > 0:
                    effect_msgs.append(f"突破成功率+{int(bonus*100)}%(1小时)")
                else:
                    effect_msgs.append(f"突破成功率{int(bonus*100)}%(1小时)")

        # 确保属性不为负
        player.physical_damage = max(0, player.physical_damage)
        player.magic_damage = max(0, player.magic_damage)
        player.physical_defense = max(0, player.physical_defense)
        player.magic_defense = max(0, player.magic_defense)
        player.mental_power = max(0, player.mental_power)
        player.spiritual_qi = min(player.spiritual_qi, player.max_spiritual_qi)
        player.blood_qi = min(player.blood_qi, player.max_blood_qi)

        await self.db.update_player(player)

        # 去重效果消息
        unique_effects = list(dict.fromkeys(effect_msgs))
        effects_str = "、".join(unique_effects[:5])  # 最多显示5个效果
        if len(unique_effects) > 5:
            effects_str += "..."

        qty_str = f"x{quantity}" if quantity > 1 else ""
        return True, f"服用【{pill_name}】{qty_str}成功！效果：{effects_str}"
