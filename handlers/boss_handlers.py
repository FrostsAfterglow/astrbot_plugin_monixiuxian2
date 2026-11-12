# handlers/boss_handlers.py
from typing import Tuple, Optional, Dict, Any
from astrbot.api.event import AstrMessageEvent
from ..managers.boss_manager import BossManager
from ..data.data_manager import DataBase


class BossHandlers:
    def __init__(self, db: DataBase, boss_mgr: BossManager):
        self.db = db
        self.boss_mgr = boss_mgr

    async def handle_boss_info(self, event: AstrMessageEvent):
        """查询世界Boss"""
        success, msg, _ = await self.boss_mgr.get_boss_info()
        yield event.plain_result(msg)

    async def handle_boss_fight(self, user_id: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """挑战世界Boss（旧「挑战Boss」指令：直接打世界Boss本体，兼容保留）。"""
        success, msg, battle_result = await self.boss_mgr.challenge_boss(user_id, solo=False)
        return success, msg, battle_result

    async def handle_boss_fight_solo(self, user_id: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """挑战单人boss：打满血个人镜像，独享奖励，不改世界Boss本体血池。"""
        success, msg, battle_result = await self.boss_mgr.challenge_boss(user_id, solo=True)
        return success, msg, battle_result

    async def handle_boss_fight_all(self, user_id: str, name: str = "") -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """挑战全服boss：多人共享血池、按贡献分奖池（融合互动插件讨伐）。"""
        success, msg, result = await self.boss_mgr.challenge_all_boss(user_id, name)
        return success, msg, result

    async def get_raid_info(self) -> Tuple[bool, str]:
        """全服讨伐进度与伤害榜。"""
        return await self.boss_mgr.get_raid_info()

    async def handle_spawn_boss(self) -> Tuple[bool, str, Optional[Any]]:
        """生成世界Boss"""
        success, msg, boss = await self.boss_mgr.auto_spawn_boss()
        return success, msg, boss
