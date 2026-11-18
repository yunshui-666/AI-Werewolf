# 编写狼人杀博弈策略

先选一个行为进行修改，例如村民如何站边、狼人何时悍跳、预言家何时公开查验。用固定种子的多局结果和公开过程检查变化，再扩展到其他行为。

## 决策入口

每位玩家通过统一接口获取个人视图、当前动作和合法候选目标。

```python
def decide(self, view, action, candidates):
    ...
    return Decision(target_id=target, text=speech, suspicions=private_suspicions)
```

`view` 包含本人身份、公开发言、历史票型和自己的私有信息。`text` 在公开发言动作中被公布，`suspicions` 写入本人的私有记忆。技能与投票目标需要来自 `candidates`，主持人会执行规则校验。

## 先从现有策略入手

| 修改位置 | 可以调整的行为 |
| --- | --- |
| `agents/strategy.py` 中的 `analyze_view` | 如何解释查验声明、对跳、投票承诺与实际票型 |
| `agents/strategy.py` 中的 `StrategyWeights` | 各种证据对怀疑程度的影响 |
| `agents/player.py` 中的 `MockAgent._select_target` | 投票、查验和夜间击杀的取舍 |
| `agents/player.py` 中的 `MockAgent._speech` | 悍跳、回应质疑、公开查验和控票表达 |
| `agents/prompts.py` 中的 `ROLE_PROMPTS` | DeepSeek 针对角色采用的目标与策略 |
| `agents/prompts.py` 中的 `ACTION_PROMPTS` | 每种行动需要比较的收益与风险 |

### 怀疑分数

当前实现按玩家计算可调的启发式分数，正分表示更可疑。系数用于排序，具体数值需要通过对局实验调整。

- 与本人私有事实冲突的查验声明，提高声明者的可疑程度。
- 同一声明者对同一目标给出不同查验结果，提高声明者的可疑程度。
- 公开查杀会提高目标的可疑程度，同时根据声明者的矛盾情况降低该信号的影响。
- 公开投票承诺与最终选票不一致，会留下需要解释的行为记录。
- 多名玩家公开指向同一目标，会影响形成放逐多数的判断。
- 自己的真实查验结果具有最高优先级，查验狼人设为 100，查验好人设为 -100。

识别离线策略的声明时，可以使用以下常见表达。

```text
我是预言家，我查验玩家 3 为狼人。
我查验玩家 4 为好人。
我准备投玩家 3。
```

离线解析器通过常用中文表达提取信号。自由发言仍会完整进入公开历史，DeepSeek 会同时读取原文和启发式分析。

### 调整权重

例如，希望 AI 更重视矛盾与投票一致性，可以在创建 Agent 时传入自定义权重。

```python
from agents.player import MockAgent
from agents.strategy import StrategyWeights

weights = StrategyWeights(
    contradiction=7.0,
    vote_deviation=2.0,
    public_pressure=0.15,
)
agent = MockAgent(seed=42, weights=weights)
```

提高 `contradiction` 会更快质疑前后矛盾的预言家声明。提高 `vote_deviation` 会更重视投票承诺。降低 `public_pressure` 会减少单纯跟随公开票势的倾向。一次调整一个参数，观察误判和放逐结果如何变化。

## 按角色设计收益

### 村民

目标是通过放逐狼人推动好人胜利。可以比较以下因素。

1. 身份声明和查验记录是否自洽。
2. 声明者是否持续保护某名玩家，保护理由是否合理。
3. 谁在关键时点改变票势，理由是否与新证据对应。
4. 当前目标获得多数票的可能性，以及坚持该目标会产生什么结果。

第二轮发言可以要求对跳双方分别解释查验顺序，随后公开自己的投票意向。若态度变化，给出导致变化的具体发言或事件。

### 预言家

查验目标可以结合信息价值与影响力。优先考虑尚未查验、正在对跳或会影响多人投票的玩家。公开身份时，需要权衡当日放逐收益与下一夜的生存风险。

当前离线策略在查到存活狼人、出现其他预言家声明或局面进入少人数时公开信息。可以修改这一条件，实验更早跳身份和更晚公开的效果。

### 狼人

狼人知道队友，可以同时考虑阵营协作与个人伪装。

- 悍跳预言家，编造能保持一致的查验记录，并引导好人投向其他好人。
- 一名狼人承担身份争夺，另一名参与质疑、跟票和控票，减少两人同时暴露的机会。
- 识别公开宣称预言家的对手，并评估击杀后哪些玩家会改变站边。
- 队友已经受到多数公开投票意向时，考虑切割以争取后续信任。

当前离线策略由最小狼人 ID 尝试伪装预言家，另一名狼人参与配合。逻辑型或怀疑型狼人面对队友受到过半公开投票意向时，允许投向队友。可以调整悍跳触发时机、切割条件和人格差异，观察双方胜率与游戏过程的变化。

## 给 DeepSeek 写策略 Prompt

角色 Prompt 可以描述目标、可用手段、需要比较的风险，以及希望维持的行为一致性。

例如，在狼人 Prompt 中加入以下策略要求。

```text
决定悍跳前，比较场上已公开的身份声明与队友位置。
如果选择伪装预言家，维护逐夜一致的查验叙述。
如果队友正被多数人怀疑，评估保队友和切割分别会带来多少后续控票空间。
公开发言时，用可公开的证据说服其他人，并给出当前想推动的投票。
```

每次调用已经提供完整的个人可见历史。可以让模型把怀疑理由写入 `suspicions`，把希望公开的部分写入 `text`。后续轮次会再次看到本人的怀疑记忆和已经公开的发言，便于保持策略连续性。

## 自定义投票规则示例

以下示例只调整好人阵营的投票偏好，其余行动继续使用已有策略。可把类放在 `agents/player.py` 或单独的策略模块，再在 `main.py` 创建 Agent 时使用。

```python
from dataclasses import replace

from agents.player import Action, MockAgent
from agents.strategy import analyze_view
from game.state import Role


class ConsistencyAgent(MockAgent):
    def decide(self, view, action, candidates):
        decision = super().decide(view, action, candidates)
        if action is Action.VOTE and view.role is not Role.WEREWOLF:
            analysis = analyze_view(view, self.weights)
            target = max(candidates, key=analysis.score)
            return replace(
                decision,
                target_id=target,
                suspicions=((target, analysis.reason(target)),),
            )
        return decision
```

随后可以在此基础上加入新证据项或改变同分时的取舍。`tests/test_strategy.py` 提供了对跳、假查验、改票、狼人协作和切割的可复用场景。

## 验证修改

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe main.py --auto --seed 42 --debug
.\.venv\Scripts\python.exe main.py --auto --seed 43 --debug
.\.venv\Scripts\python.exe main.py --mode deepseek
```

先检查有没有非法行动和私有信息泄露，再比较胜率、关键误判和策略是否按预期触发。管理员日志包含全部身份与私有决策，公开日志便于从玩家视角回顾推理。固定 seed 的对局适合回归检查，多种 seed 的对局适合观察角色位置与人格变化的影响。
