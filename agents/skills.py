"""游戏内角色 skill 加载器。

身份分配后只加载该角色的 SKILL.md。文档是角色策略知识，真正的技能权限
仍由 game.rules 校验，因此修改攻略不会授予 Agent 额外的游戏能力。
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "role_skills"
SUPPORTED_ROLES = {"werewolf", "seer", "villager", "witch", "hunter", "guard"}


@dataclass(frozen=True)
class RoleSkill:
    """不可变的角色知识，安全地放入该玩家自己的可见快照。"""

    role: str
    name: str
    instructions: str


@lru_cache(maxsize=6)
def load_role_skill(role: str) -> RoleSkill:
    """从白名单路径读取，缓存文档以避免每次决策重复访问磁盘。"""
    if role not in SUPPORTED_ROLES:
        raise ValueError("未知角色 skill")
    content = (SKILLS_ROOT / role / "SKILL.md").read_text(encoding="utf-8-sig")
    title = content.splitlines()[0].lstrip("# ").strip()
    return RoleSkill(role, title, content)
