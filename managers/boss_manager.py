# managers/boss_manager.py
"""
Boss系统管理器 - 处理Boss生成、战斗、奖励等逻辑
参照NoneBot2插件的xiuxian_boss实现
"""

import asyncio
import json
import random
import time
from typing import Tuple, Dict, Optional, List, TYPE_CHECKING
from ..data.data_manager import DataBase
from ..models_extended import Boss, UserStatus
from ..models import Player
from .combat_manager import CombatManager, CombatStats
from ..boss_raid import RaidState, raid_damage, raid_share

if TYPE_CHECKING:
    from ..core import StorageRingManager


class BossManager:
    """Boss系统管理器"""
    
    # Boss境界配置
    BOSS_LEVELS = [
        {"name": "练气", "level_index": 0, "hp_mult": 1.0, "atk_mult": 1.0, "reward_mult": 1.0},
        {"name": "筑基", "level_index": 3, "hp_mult": 1.5, "atk_mult": 1.2, "reward_mult": 1.5},
        {"name": "金丹", "level_index": 6, "hp_mult": 2.0, "atk_mult": 1.5, "reward_mult": 2.0},
        {"name": "元婴", "level_index": 9, "hp_mult": 2.5, "atk_mult": 1.8, "reward_mult": 2.5},
        {"name": "化神", "level_index": 12, "hp_mult": 3.0, "atk_mult": 2.0, "reward_mult": 3.0},
        {"name": "炼虚", "level_index": 15, "hp_mult": 4.0, "atk_mult": 2.5, "reward_mult": 4.0},
        {"name": "合体", "level_index": 18, "hp_mult": 5.0, "atk_mult": 3.0, "reward_mult": 5.0},
        {"name": "大乘", "level_index": 21, "hp_mult": 6.0, "atk_mult": 3.5, "reward_mult": 6.0},
    ]
    
    # Boss名称池
    BOSS_NAMES = [
        "血魔", "邪修", "魔头", "妖王", "魔君",
        "异兽", "凶兽", "妖尊", "魔尊", "邪帝",
        "天魔", "地魔", "魔神", "妖神", "邪神"
    ]
    
    # Boss物品掉落表
    BOSS_DROP_TABLE = {
        "low": [  # 低级Boss (练气-金丹)
            {"name": "灵兽内丹", "weight": 40, "min": 1, "max": 2},
            {"name": "妖兽精血", "weight": 30, "min": 1, "max": 3},
            {"name": "玄铁", "weight": 30, "min": 3, "max": 6},
        ],
        "mid": [  # 中级Boss (元婴-化神)
            {"name": "灵兽内丹", "weight": 30, "min": 2, "max": 4},
            {"name": "星辰石", "weight": 25, "min": 2, "max": 4},
            {"name": "天材地宝", "weight": 20, "min": 1, "max": 2},
            {"name": "功法残页", "weight": 25, "min": 1, "max": 2},
        ],
        "high": [  # 高级Boss (炼虚及以上)
            {"name": "天材地宝", "weight": 30, "min": 2, "max": 4},
            {"name": "混沌精华", "weight": 25, "min": 1, "max": 2},
            {"name": "神兽之骨", "weight": 20, "min": 1, "max": 1},
            {"name": "远古秘籍", "weight": 15, "min": 1, "max": 1},
            {"name": "仙器碎片", "weight": 10, "min": 1, "max": 1},
        ],
    }
    
    # 全服讨伐（挑战全服boss）参数：血池/奖池倍率与出手冷却
    RAID_STATE_KEY = "boss_raid_state"
    RAID_HP_MULT = 3.0          # 全服血池 = 世界Boss血量 × 3
    RAID_REWARD_MULT = 4.0      # 全服奖池 = 世界Boss灵石 × 4（多人分）
    RAID_COOLDOWN = 10          # 同一玩家两次出手的最小间隔（秒）
    SOLO_COOLDOWN = 30          # 单人副本挑战冷却（秒），防止无限刷奖励

    def __init__(self, db: DataBase, combat_mgr: CombatManager, config_manager=None, storage_ring_manager: "StorageRingManager" = None):
        self.db = db
        self.combat_mgr = combat_mgr
        self.storage_ring_manager = storage_ring_manager
        self.config = config_manager.boss_config if config_manager else {}
        self.levels = self.config.get("levels", self.BOSS_LEVELS)
        self._raid_lock = asyncio.Lock()          # 保护全服讨伐的读改写，避免并发超发
        self._last_raid_hit: Dict[str, float] = {}  # uid -> 上次出手时间戳
        self._last_solo_hit: Dict[str, float] = {}  # uid -> 上次单人挑战时间戳
    
    async def spawn_boss(
        self,
        base_exp: int = 100000,
        level_config: Optional[Dict] = None
    ) -> Tuple[bool, str, Optional[Boss]]:
        """
        生成Boss
        
        Args:
            base_exp: 基础修为（用于计算属性）
            level_config: Boss等级配置，如果为None则随机选择
            
        Returns:
            (成功标志, 消息, Boss对象)
        """
        # 检查是否已有存活的Boss
        existing_boss = await self.db.ext.get_active_boss()
        if existing_boss:
            return False, f"❌ 当前已有Boss『{existing_boss.boss_name}』存在！", None
        
        # 选择Boss等级
        if not level_config:
            level_config = random.choice(self.levels)
        
        # 生成Boss名称
        boss_name = random.choice(self.BOSS_NAMES) + f"·{level_config['name']}境"
        
        # 计算Boss属性
        hp_mult = level_config["hp_mult"]
        atk_mult = level_config["atk_mult"]
        reward_mult = level_config["reward_mult"]
        
        # Boss的HP和ATK基于修为计算
        max_hp = int(base_exp * hp_mult // 2)
        atk = int(base_exp * atk_mult // 10)
        
        # 灵石奖励
        stone_reward = int(base_exp * reward_mult // 10)
        
        # Boss防御力（高境界Boss有减伤）
        defense = 0
        if level_config["level_index"] >= 15:  # 炼虚及以上
            defense = random.randint(40, 90)  # 40%-90%减伤
        
        # 创建Boss
        boss = Boss(
            boss_id=0,  # 自动生成
            boss_name=boss_name,
            boss_level=level_config["name"],
            hp=max_hp,
            max_hp=max_hp,
            atk=atk,
            defense=defense,
            stone_reward=stone_reward,
            create_time=int(time.time()),
            status=1  # 1=存活
        )
        
        boss_id = await self.db.ext.create_boss(boss)
        boss.boss_id = boss_id

        # 每次刷新世界Boss时，同步重置一局「全服讨伐」（融合互动插件多人讨伐）
        try:
            await self._save_raid(
                RaidState.from_boss(boss, self.RAID_HP_MULT, self.RAID_REWARD_MULT)
            )
        except Exception as e:
            # 讨伐局初始化失败不应阻断Boss刷新
            from astrbot.api import logger
            logger.warning(f"【修仙插件】初始化全服讨伐局失败: {e}")

        msg = f"""
👹 Boss降临
━━━━━━━━━━━━━━━

{boss_name}降临世间！

境界：{level_config["name"]}
HP：{max_hp}
ATK：{atk}
防御：{defense}%减伤
奖励：{stone_reward}灵石

快来挑战吧！
        """.strip()
        
        return True, msg, boss
    
    async def challenge_boss(
        self,
        user_id: str,
        solo: bool = False
    ) -> Tuple[bool, str, Optional[Dict]]:
        """
        挑战Boss（单人副本模式）。

        Args:
            user_id: 挑战者ID
            solo: True=单人副本：基于当前世界 Boss 生成一份满血镜像，
                  1v1 完整战斗，个人独享奖励，**不修改世界 Boss 本体血量/状态**。
                  这样「挑战单人boss」与「挑战全服boss」共用同一只刷新的 Boss
                  却互不抢血池。False=旧行为（直接打世界 Boss 本体）。

        Returns:
            (成功标志, 消息, 战斗结果)
        """
        # 1. 检查玩家
        player = await self.db.get_player_by_id(user_id)
        if not player:
            return False, "❌ 你还未踏入修仙之路！", None

        # 2. 检查Boss是否存在
        boss = await self.db.ext.get_active_boss()
        if not boss:
            return False, "❌ 当前没有Boss！", None

        # 2.5 单人副本冷却（防无限刷奖励），并生成满血镜像
        if solo:
            now = time.time()
            last = self._last_solo_hit.get(str(user_id), 0.0)
            if now - last < self.SOLO_COOLDOWN:
                wait = int(self.SOLO_COOLDOWN - (now - last))
                return False, f"⏳ 单人副本冷却中，请 {wait} 秒后再挑战。", None
            self._last_solo_hit[str(user_id)] = now
            boss = Boss(
                boss_id=-1,  # 镜像不落库
                boss_name=boss.boss_name,
                boss_level=boss.boss_level,
                hp=boss.max_hp,
                max_hp=boss.max_hp,
                atk=boss.atk,
                defense=boss.defense,
                stone_reward=boss.stone_reward,
                create_time=int(now),
                status=1,
            )
        
        # 3. 检查玩家状态
        user_cd = await self.db.ext.get_user_cd(user_id)
        if not user_cd:
            await self.db.ext.create_user_cd(user_id)
            user_cd = await self.db.ext.get_user_cd(user_id)
        
        if user_cd.type != UserStatus.IDLE:
            return False, "❌ 你当前正忙，无法挑战Boss！", None
        
        # 4. 计算玩家战斗属性
        # 获取buff加成
        impart_info = await self.db.ext.get_impart_info(user_id)
        hp_buff = impart_info.impart_hp_per if impart_info else 0.0
        mp_buff = impart_info.impart_mp_per if impart_info else 0.0
        atk_buff = impart_info.impart_atk_per if impart_info else 0.0
        crit_rate_buff = impart_info.impart_know_per if impart_info else 0.0
        
        # 计算HP/MP/ATK
        if player.hp == 0 or player.mp == 0:
            # 如果没有初始化战斗属性，先计算
            hp, mp = self.combat_mgr.calculate_hp_mp(player.experience, hp_buff, mp_buff)
            atk = self.combat_mgr.calculate_atk(player.experience, player.atkpractice, atk_buff)
            player.hp = hp
            player.mp = mp
            player.atk = atk
            await self.db.update_player(player)
        else:
            # 使用现有属性
            hp = player.hp
            mp = player.mp
            atk = player.atk
        
        # 创建玩家战斗属性
        player_stats = CombatStats(
            user_id=user_id,
            name=player.user_name if player.user_name else f"道友{user_id[:6]}",
            hp=hp,
            max_hp=int(player.experience * (1 + hp_buff) // 2),
            mp=mp,
            max_mp=int(player.experience * (1 + mp_buff)),
            atk=atk,
            defense=0,  # 可以根据装备添加
            crit_rate=int(crit_rate_buff * 100),  # 转换为百分比
            exp=player.experience
        )
        
        # 创建Boss战斗属性
        boss_stats = CombatStats(
            user_id=str(boss.boss_id),
            name=boss.boss_name,
            hp=boss.hp,
            max_hp=boss.max_hp,
            mp=boss.max_hp,  # Boss的MP等于HP
            max_mp=boss.max_hp,
            atk=boss.atk,
            defense=boss.defense,
            crit_rate=30,  # Boss固定30%会心率
            exp=boss.stone_reward  # 奖励存在exp字段
        )
        
        # 5. 开始战斗
        battle_result = self.combat_mgr.player_vs_boss(player_stats, boss_stats)
        
        # 6. 处理战斗结果
        winner = battle_result["winner"]
        reward = battle_result["reward"]
        
        if winner == user_id:
            # 玩家胜利
            if not solo:
                boss.status = 0  # 标记Boss为已击败
                await self.db.ext.defeat_boss(boss.boss_id)
            
            # 发放奖励
            player.gold += reward
            
            # 物品掉落
            item_msg = ""
            dropped_items = []
            if self.storage_ring_manager:
                dropped_items = await self._roll_boss_drops(player, boss)
                if dropped_items:
                    item_lines = []
                    for item_name, count in dropped_items:
                        success, _ = await self.storage_ring_manager.store_item(player, item_name, count, silent=True)
                        if success:
                            item_lines.append(f"  · {item_name} x{count}")
                        else:
                            item_lines.append(f"  · {item_name} x{count}（储物戒已满，丢失）")
                    if item_lines:
                        item_msg = "\n\n📦 获得物品：\n" + "\n".join(item_lines)
            
            result_msg = f"""
🎉 挑战成功！
━━━━━━━━━━━━━━━

你成功击败了『{boss.boss_name}』！

战斗回合数：{battle_result['rounds']}
获得灵石：{reward}{item_msg}

{player_stats.name}
HP：{battle_result['player_final_hp']}/{player_stats.max_hp}
            """.strip()
        else:
            # 玩家失败（单人副本为镜像，不写回世界Boss血量）
            boss.hp = battle_result["boss_final_hp"]
            if not solo:
                await self.db.ext.update_boss(boss)
            
            result_msg = f"""
💀 挑战失败
━━━━━━━━━━━━━━━

你被『{boss.boss_name}』击败了！

战斗回合数：{battle_result['rounds']}
安慰奖：{reward}灵石

{boss.boss_name} 剩余HP：{boss.hp}/{boss.max_hp}
            """.strip()
            
            # 即使失败也给予部分奖励
            if reward > 0:
                player.gold += reward
        
        # 更新玩家HP/MP
        player.hp = battle_result["player_final_hp"]
        player.mp = battle_result["player_final_mp"]
        await self.db.update_player(player)
        
        # 返回完整战斗日志
        combat_log = "\n".join(battle_result["combat_log"])
        full_msg = combat_log + "\n\n" + result_msg
        
        return True, full_msg, battle_result
    
    async def get_boss_info(self) -> Tuple[bool, str, Optional[Boss]]:
        """
        获取当前Boss信息
        
        Returns:
            (成功标志, 消息, Boss对象)
        """
        boss = await self.db.ext.get_active_boss()
        if not boss:
            return False, "❌ 当前没有Boss！", None
        
        hp_percent = (boss.hp / boss.max_hp) * 100
        
        msg = f"""
👹 当前Boss
━━━━━━━━━━━━━━━

名称：{boss.boss_name}
境界：{boss.boss_level}

HP：{boss.hp}/{boss.max_hp} ({hp_percent:.1f}%)
ATK：{boss.atk}
防御：{boss.defense}%减伤

奖励：{boss.stone_reward}灵石

⚔️ /挑战单人boss —— 单人副本，独享奖励
👥 /挑战全服boss —— 全服协力，按伤害贡献分奖池
📊 /讨伐状态 —— 查看全服讨伐进度与伤害榜
        """.strip()
        
        return True, msg, boss

    # ===== 全服讨伐（挑战全服boss）—— 融合「互动」插件多人讨伐机制 =====

    async def _load_raid(self) -> Optional[RaidState]:
        """从 system_config 载入当前讨伐局；不存在或损坏返回 None。"""
        try:
            raw = await self.db.ext.get_system_config(self.RAID_STATE_KEY)
            if not raw:
                return None
            return RaidState.from_dict(json.loads(raw))
        except Exception:
            return None

    async def _save_raid(self, raid: RaidState):
        """把讨伐局序列化进 system_config（单行 JSON，重启不丢）。"""
        await self.db.ext.set_system_config(
            self.RAID_STATE_KEY, json.dumps(raid.to_dict(), ensure_ascii=False)
        )

    async def _ensure_raid(self) -> Tuple[bool, Optional[RaidState], str]:
        """取得与当前存活世界Boss对应的、未结束的讨伐局。

        Returns:
            ``(是否可用, 讨伐局, 提示)``。若没有存活Boss、或本轮已讨伐完成，
            返回不可用并给出提示；否则必要时按当前Boss即时重建一局。
        """
        boss = await self.db.ext.get_active_boss()
        if not boss:
            return False, None, "❌ 当前没有世界Boss，等待刷新后再来讨伐！"

        raid = await self._load_raid()
        if raid and raid.boss_id == int(boss.boss_id) and not raid.finished:
            return True, raid, ""
        if raid and raid.boss_id == int(boss.boss_id) and raid.finished:
            return False, raid, f"🏆 『{raid.boss_name}』已被全服讨伐击败，等待下一只Boss刷新！"

        # 首次 / Boss 已更替：按当前Boss重建
        raid = RaidState.from_boss(boss, self.RAID_HP_MULT, self.RAID_REWARD_MULT)
        await self._save_raid(raid)
        return True, raid, ""

    def _player_power(self, player: Player) -> int:
        """把玩家战力折算为讨伐伤害基数（攻击力优先，缺失时按修为推算）。"""
        atk = getattr(player, "atk", 0) or 0
        if atk <= 0:
            atk = self.combat_mgr.calculate_atk(
                getattr(player, "experience", 0) or 0,
                getattr(player, "atkpractice", 0) or 0,
                0.0,
            )
        return max(1, int(atk))

    async def challenge_all_boss(
        self, user_id: str, name: str = ""
    ) -> Tuple[bool, str, Optional[Dict]]:
        """挑战全服boss：对共享血池出手一次，累计贡献，击杀后按贡献分奖池。

        融合互动插件「讨伐」：多人共享一条血池、damages 累计伤害、伤害榜、
        最后一击、raid_share 按贡献分配灵石奖池。用 _raid_lock 串行化读改写，
        避免并发重复结算/超发。

        Returns:
            (成功标志, 战报, 结果字典)。结果含 killed/shares/killer/board 等，
            供 main 层判断是否广播击杀。
        """
        async with self._raid_lock:
            player = await self.db.get_player_by_id(user_id)
            if not player:
                return False, "❌ 你还未踏入修仙之路！", None

            uid = str(user_id)
            now = time.time()
            last = self._last_raid_hit.get(uid, 0.0)
            if now - last < self.RAID_COOLDOWN:
                wait = int(self.RAID_COOLDOWN - (now - last))
                return False, f"⏳ 讨伐出手冷却中，请 {wait} 秒后再战。", None

            ok, raid, err = await self._ensure_raid()
            if not ok:
                return False, err, None

            display = name or (player.user_name if player.user_name else f"道友{uid[:6]}")
            power = self._player_power(player)
            dmg, _mult = raid_damage(power)
            dealt, killed = raid.attack(uid, display, dmg)
            self._last_raid_hit[uid] = now

            result: Dict = {
                "killed": killed, "dealt": dealt, "power": power,
                "raid_name": raid.boss_name, "shares": {}, "killer": "",
                "board": [], "reward_pool": raid.reward_pool,
            }

            if not killed:
                await self._save_raid(raid)
                msg = (
                    f"⚔️ 你对『{raid.boss_name}』造成 {dealt} 点伤害！"
                    f"（战力 {power}）\n"
                    f"{raid.render()}\n"
                    f"你的累计伤害 {raid.damages.get(uid, 0)}"
                    f"（出手 {raid.attacks.get(uid, 0)} 次） · "
                    f"全服共 {raid.attendance()} 人讨伐\n"
                    f"💰 击杀后按伤害贡献瓜分 {raid.reward_pool} 灵石奖池"
                )
                return True, msg, result

            # 最后一击：结算奖池、发放灵石、标记世界Boss被击败
            shares, killer = raid.settle()
            paid: Dict[str, int] = {}
            for target_uid, gold in shares.items():
                if gold <= 0:
                    continue
                p = await self.db.get_player_by_id(target_uid)
                if p:
                    p.gold += gold
                    await self.db.update_player(p)
                    paid[target_uid] = gold
            await self.db.ext.defeat_boss(raid.boss_id)
            await self._save_raid(raid)

            board = raid.damage_board(8)
            result.update({
                "shares": paid, "killer": killer,
                "killer_name": raid.names.get(killer, f"道友{killer[:6]}"),
                "board": board,
                "names": dict(raid.names),
                "attendance": raid.attendance(),
            })

            killer_line = raid.names.get(killer, f"道友{killer[:6]}")
            lines = [
                f"💥 『{raid.boss_name}』被全服协力击败！",
                f"🗡️ 最后一击：{killer_line}",
                raid.render(),
                f"你本次造成 {dealt} 点伤害，累计 {raid.damages.get(uid, 0)}，"
                f"分得 {paid.get(uid, 0)} 灵石",
                f"👥 共 {raid.attendance()} 人参战，"
                f"奖池 {raid.reward_pool} 灵石已按伤害贡献分配",
                "",
                "📊 伤害榜：",
            ]
            for i, (bu, bd, ba) in enumerate(board, 1):
                lines.append(
                    f"  {i}. {raid.names.get(bu, f'道友{bu[:6]}')} —— {bd}（{ba}次）"
                )
            return True, "\n".join(lines), result

    async def get_raid_info(self) -> Tuple[bool, str]:
        """查看全服讨伐进度与伤害榜（/讨伐状态）。"""
        boss = await self.db.ext.get_active_boss()
        raid = await self._load_raid()
        if not boss and not raid:
            return False, "当前没有全服讨伐，等待世界Boss刷新。"
        if raid and raid.finished:
            return True, (
                f"🏆 上一轮『{raid.boss_name}』已被讨伐（最后一击："
                f"{raid.names.get(raid.killed_by, '未知')}）。\n"
                f"等待下一只Boss刷新后开启新的全服讨伐。"
            )
        if raid and boss and raid.boss_id == int(boss.boss_id):
            lines = [f"👹 全服讨伐 · {raid.boss_name}", raid.render(),
                     f"👥 参战 {raid.attendance()} 人 · 累计伤害 {raid.total_damage}",
                     f"💰 奖池 {raid.reward_pool} 灵石（击杀后按贡献分配）"]
            board = raid.damage_board(8)
            if board:
                lines.append("📊 伤害榜：")
                for i, (bu, bd, ba) in enumerate(board, 1):
                    lines.append(
                        f"  {i}. {raid.names.get(bu, f'道友{bu[:6]}')} —— {bd}（{ba}次）"
                    )
            else:
                lines.append("还没人出手，发送 /挑战全服boss 一起讨伐！")
            lines.append("💡 单人也可发送 /挑战单人boss 打个人副本")
            return True, "\n".join(lines)
        return False, "当前没有进行中的全服讨伐，发送 /挑战全服boss 开打！"

    async def auto_spawn_boss(self, player_count: int = 0) -> Tuple[bool, str, Optional[Boss]]:
        """
        自动生成Boss（定时任务使用）
        根据服务器玩家数量和平均等级自动调整Boss难度
        
        Args:
            player_count: 玩家数量（用于调整难度）
            
        Returns:
            (成功标志, 消息, Boss对象)
        """
        # 检查是否已有Boss
        existing_boss = await self.db.ext.get_active_boss()
        if existing_boss:
            return False, "当前已有Boss存在", None
        
        # 获取所有玩家的平均等级
        all_players = await self.db.get_all_players()
        if not all_players:
            # 没有玩家，生成低级Boss
            level_config = self.levels[0]
            base_exp = 50000
        else:
            # 计算平均修为
            total_exp = sum(p.experience for p in all_players)
            avg_exp = total_exp // len(all_players) if all_players else 50000
            
            # 根据平均修为选择Boss等级
            for config in reversed(self.levels):
                if avg_exp >= config.get("level_index", 0) * 10000:
                    level_config = config
                    break
            else:
                level_config = self.levels[0]
            
            # Boss修为比平均稍高
            base_exp = int(avg_exp * 1.2)
        
        # 生成Boss
        return await self.spawn_boss(base_exp, level_config)
    
    async def _roll_boss_drops(self, player: Player, boss: Boss) -> List[Tuple[str, int]]:
        """
        根据Boss等级随机掉落物品
        
        Args:
            player: 玩家对象
            boss: Boss对象
            
        Returns:
            掉落物品列表 [(物品名, 数量), ...]
        """
        dropped_items = []
        
        # 根据Boss等级确定掉落表
        boss_level_index = 0
        for level in self.levels:
            if level["name"] == boss.boss_level:
                boss_level_index = level["level_index"]
                break
        
        if boss_level_index <= 6:  # 练气-金丹
            drop_table = self.BOSS_DROP_TABLE["low"]
        elif boss_level_index <= 12:  # 元婴-化神
            drop_table = self.BOSS_DROP_TABLE["mid"]
        else:  # 炼虚及以上
            drop_table = self.BOSS_DROP_TABLE["high"]
        
        # Boss击杀100%掉落至少1件物品
        total_weight = sum(item["weight"] for item in drop_table)
        roll = random.randint(1, total_weight)
        
        current_weight = 0
        for item in drop_table:
            current_weight += item["weight"]
            if roll <= current_weight:
                count = random.randint(item["min"], item["max"])
                dropped_items.append((item["name"], count))
                break
        
        # 高级Boss有70%概率额外掉落
        if boss_level_index >= 9:  # 元婴及以上
            extra_chance = 50 if boss_level_index < 15 else 70
            if random.randint(1, 100) <= extra_chance:
                roll = random.randint(1, total_weight)
                current_weight = 0
                for item in drop_table:
                    current_weight += item["weight"]
                    if roll <= current_weight:
                        count = random.randint(item["min"], item["max"])
                        dropped_items.append((item["name"], count))
                        break
        
        return dropped_items
