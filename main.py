
"""命令行入口，默认由人类控制玩家 1，其余玩家由 AI 决策。"""

import argparse
import logging
from pathlib import Path

from pydantic import ValidationError

from config import GameConfig, PRESETS
from game.reporting import configure_logging, final_summary
from game.runner import GameRunner


def main() -> int:
    """解析启动参数、创建本局资源并运行所选流程，统一处理退出与 SDK 关闭。"""
    parser = argparse.ArgumentParser(description="WerewolfArena AI 狼人杀")
    parser.add_argument("--seed", type=int, default=None, help="固定随机种子")
    parser.add_argument("--max-rounds", type=int, default=10, help="最大完整夜昼轮数")
    parser.add_argument("--discussion-rounds", type=int, default=2, help="每日按编号循环发言的轮数")
    parser.add_argument("--preset", choices=tuple(PRESETS), default="standard12", help="标准 12 人局或原有 8 人局")
    participation = parser.add_mutually_exclusive_group()
    participation.add_argument("--human", type=int, default=1, choices=range(1, 13), metavar="ID", help="你控制的玩家编号，默认 1")
    participation.add_argument("--auto", dest="human", action="store_const", const=None, help="全 AI 自动对局")
    parser.add_argument("--mode", choices=("auto", "mock", "deepseek"), default="auto", help="玩家决策来源")
    parser.add_argument("--workflow", choices=("langgraph", "python"), default="langgraph", help="流程编排方式")
    parser.add_argument("--debug", action="store_true", help="额外写入含完整私有信息的管理员日志")
    parser.add_argument("--log-dir", type=Path, default=Path(__file__).resolve().parent / "logs", help="日志输出目录")
    args = parser.parse_args()
    clients = []
    agents = None
    try:
        config = GameConfig(
            max_rounds=args.max_rounds, seed=args.seed, discussion_rounds=args.discussion_rounds,
            preset=args.preset,
        )
        if args.human is not None and args.human > config.player_count:
            raise ValueError("真人座位超出本局人数")
        from llm.profiles import ProfileRegistry

        registry = ProfileRegistry(Path(__file__).resolve().parent)
        selections = {pid: args.mode for pid in range(1, config.player_count + 1)}
        if args.mode == "deepseek":
            pool = registry.routing_setup(config.player_count, args.human or 0)["deepseek_pool"] or ["deepseek"]
            ai_ids = [pid for pid in range(1, config.player_count + 1) if pid != args.human]
            selections.update({pid: pool[index % len(pool)] for index, pid in enumerate(ai_ids)})
        # 真人不需要创建远程连接，也不受该座位的 API 环境配置约束。
        if args.human is not None:
            selections[args.human] = "mock"
        agents, clients = registry.create_agents(config.player_count, selections, args.seed)
        if args.human is not None:
            from agents.human import HumanAgent

            agents[args.human] = HumanAgent()
        files = configure_logging(args.log_dir, debug=args.debug)
    except (ValueError, OSError) as exc:
        for client in clients:
            client.close()
        if isinstance(exc, ValidationError):
            message = "请检查接口配置的字段与格式"
        else:
            message = str(exc) if isinstance(exc, ValueError) else "请检查日志目录权限"
        parser.error(f"启动配置无效，{message}")

    logger = logging.getLogger("werewolf_arena.public")
    try:
        logger.info("AI 决策模式为 %s，流程编排为 %s，随机种子为 %s。", args.mode, args.workflow, args.seed)
        for warning in registry.warnings:
            logger.warning("%s", warning)
        for pid, agent in agents.items():
            if not agent.human_controlled:
                logger.info("玩家 %s 使用 %s。", pid, agent.profile_name)
        if args.human is not None:
            logger.info("你控制玩家 %s，其余玩家由 AI 控制，每天按编号发言 %s 轮。", args.human, args.discussion_rounds)
        runner = GameRunner(config, agents)
        if args.workflow == "langgraph":
            from workflow.graph import run_graph

            run_graph(runner)
        else:
            runner.run()
        logger.info("%s", final_summary(runner.engine, runner.fallback_count))
        if clients:
            logger.info("模型请求 %s 次，已报告 token 用量 %s。",
                        sum(client.request_count for client in clients), sum(client.total_tokens for client in clients))
        logger.info("公开日志已保存到 %s", files.public)
        if files.admin is not None:
            logger.info("管理员调试日志已保存到 %s", files.admin)
        return 0
    except KeyboardInterrupt:
        logger.info("游戏已由用户中断，已发生的公开事件保存在日志中。")
        return 130
    except EOFError:
        logger.info("输入已结束，游戏在当前阶段停止，公开过程保存在日志中。")
        return 1
    finally:
        for client in clients:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
