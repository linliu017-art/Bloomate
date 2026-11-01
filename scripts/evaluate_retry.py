from pathlib import Path
import evaluate_models as evaluation

evaluation.OUT = Path(__file__).resolve().parents[1] / 'docs' / '评测' / '第一轮DeepSeek补测原始结果.json'
evaluation.MODELS = {'deepseek': evaluation.MODELS['deepseek']}
evaluation.CASES = [case for case in evaluation.CASES if case[0] in ('E06', 'E07', 'E08')]
evaluation.run()
