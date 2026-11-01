"""Evaluate revised Qwen prompt on prior cases and new synthetic holdouts."""
from pathlib import Path
import evaluate_models as evaluation

root = Path(__file__).resolve().parents[1]
evaluation.OUT = root / 'docs' / '评测' / '千问提示词v2测试原始结果.json'
evaluation.MODELS = {'qwen': evaluation.MODELS['qwen']}
evaluation.PROMPT = (root / 'docs' / '评测' / '千问提示词-v2.txt').read_text(encoding='utf-8')
evaluation.CASES = list(evaluation.CASES) + [
    ('H01','goal','准备一次10分钟的部门分享','从未做过类似分享','两周内每周两次，每次20分钟','主题是介绍一个简单的工作经验'),
    ('H02','habit','每周练琴两次','曾经学过一点，阶段目标还没想清楚','每次15分钟','先试三周，每次练满15分钟'),
    ('H03','goal','今天做完整市场调研报告','还没有任何素材','今天只剩10分钟','愿意先做一个问题清单'),
    ('H04','habit','练习早起','不愿透露健康或睡眠细节','每天最多5分钟','先从固定起床提醒开始，试两周'),
    ('H05','goal','一周内完成摄影入门','刚开始没有经验，原来每周两小时，后来改为每周半小时','总共30分钟','以更正后的半小时为准'),
]
evaluation.run()
