"""命令行日志与终局回顾。私有信息使用独立管理员 logger。"""

from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
import sys
from uuid import uuid4

from game.engine import GameEngine, OUTCOME_NAMES, ROLE_NAMES


@dataclass(frozen=True)
class LogFiles:
    """记录本次运行的公开日志与可选管理员日志路径，便于入口统一显示保存位置。"""
    public: Path
    admin: Path | None


def configure_logging(log_dir: Path, *, debug: bool = False) -> LogFiles:
    """为一次命令行运行创建独立 UTF-8 日志。公开输出与完整管理员状态使用不同 logger 和文件。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    public_path = log_dir / f"game_{run_id}.log"
    admin_path = log_dir / f"admin_{run_id}.log" if debug else None
    formatter = logging.Formatter("%(message)s")
    for name in ("werewolf_arena.public", "werewolf_arena.admin"):
        logger = logging.getLogger(name)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logger.propagate = False
    public_logger = logging.getLogger("werewolf_arena.public")
    public_logger.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(public_path, mode="x", encoding="utf-8")
    file_handler.setFormatter(formatter)
    public_logger.addHandler(console)
    public_logger.addHandler(file_handler)
    admin_logger = logging.getLogger("werewolf_arena.admin")
    admin_logger.setLevel(logging.DEBUG if debug else logging.CRITICAL + 1)
    if admin_path is not None:
        admin_handler = logging.FileHandler(admin_path, mode="x", encoding="utf-8")
        admin_handler.setFormatter(formatter)
        admin_logger.addHandler(admin_handler)
    else:
        admin_logger.addHandler(logging.NullHandler())
    # SDK 的详细调试信息可能带请求正文，命令行默认控制在警告级别。
    for name in ("openai", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return LogFiles(public_path, admin_path)


def final_summary(engine: GameEngine, fallback_count: int = 0) -> str:
    """在终局身份读取通过后生成复盘文本，包括胜利阵营、角色、存活情况和关键死亡事件。"""
    roles = engine.final_roles()
    state = engine.state
    lines = [
        "\n========== 终局总结 ==========",
        f"结果：{OUTCOME_NAMES[state.outcome]}，共 {state.round_number} 轮，降级行动 {fallback_count} 次。",
        "\n========== 全部身份 ==========",
    ]
    for pid, role in roles.items():
        player = state.players[pid]
        status = "存活" if player.alive else "出局"
        lines.append(f"玩家 {pid}：{ROLE_NAMES[role]}，{player.personality.value}，{status}")
    lines.append("\n========== 过程回顾 ==========")
    for event in state.public_events:
        if event.kind in {"night_death", "exile", "vote_tie"}:
            lines.append(f"第 {event.round_number} 轮　{event.text}")
    return "\n".join(lines)
