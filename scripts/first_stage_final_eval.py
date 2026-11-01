"""Final synthetic API checks using the selected real model; no credentials printed."""
import json, sys
from pathlib import Path
from fastapi.testclient import TestClient
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app import main
OUT=ROOT/'docs'/'评测'/'第一轮最终真实样例.json'
if OUT.exists(): raise SystemExit('Existing results retained.')
CASES=[
 ('F01','goal','虚构终测F01：一个月准备三分钟英文读书分享','英语基础一般，介绍一本喜欢的书','每天15分钟',15,None),
 ('F02','goal','虚构终测F02：两天做求职作品集','没有整理现成素材，可先做一页提纲','每天10分钟',10,None),
 ('F03','habit','虚构终测F03：睡前阅读30天','每次读满10分钟才算完成','每天10分钟',10,10),
 ('F04','habit','虚构终测F04：每周三次轻度运动持续四周','核心活动至少15分钟算完成','每次20分钟',20,15),
 ('F05','goal','虚构终测F05：两周准备一次工作分享','原来每天30分钟，现在更正为每天5分钟','每天5分钟',5,None),
]
main.init_db()
with main.connect() as db: before={r['reservation_id'] for r in db.execute('SELECT reservation_id FROM model_call_metrics')}
results=[]
for case_id,kind,goal,bg,availability,budget,completion in CASES:
 jobs=[]; record={'case':case_id,'kind':kind,'status':'started','questions':[]}
 try:
  with TestClient(main.app) as client:
   r=client.post('/api/v1/plans',json={'kind':kind,'goal':goal,'background':bg,'availability':availability,'session_budget_minutes':budget,'habit_completion_minutes':completion})
   r.raise_for_status(); job=r.json()['id']; jobs.append(job)
   for _ in range(4):
    state=client.get('/api/v1/plans/'+job).json()
    if state['status']=='error': record.update(status='error',error_code=state['error']['code']); break
    if state.get('proposal'):
     record.update(proposal=state['proposal'],constraints=state['constraints'],check=state.get('constraint_check'))
     response=client.post('/api/v1/proposals/'+job+'/confirm'); response.raise_for_status()
     plant=response.json(); garden=client.get('/api/v1/garden').json()['plants']
     saved=next((p for p in garden if p['id']==plant['id']),None)
     record['status']='passed' if saved and saved['constraints']['session_budget_minutes']==budget and saved['constraints']['habit_completion_minutes']==completion else 'save_mismatch'
     break
    if not state.get('question'): record['status']='unexpected_state'; break
    record['questions'].append(state['question'])
    response=client.post('/api/v1/plans/'+job+'/reply',json={'answer':'其他信息暂不确定，请根据已知条件给可修改的最小方案。'})
    response.raise_for_status(); job=response.json()['id']; jobs.append(job)
   else: record['status']='no_plan'
 except Exception as exc: record.update(status='error',exception_type=type(exc).__name__)
 finally:
  with main.connect() as db:
   for j in jobs:
    db.execute('DELETE FROM plants WHERE job_id=? AND owner_id=?',(j,main.OWNER))
    db.execute('DELETE FROM jobs WHERE id=? AND owner_id=?',(j,main.OWNER))
  results.append(record)
  print(case_id,record['status'],flush=True)
  OUT.write_text(json.dumps({'results':results,'status':'running'},ensure_ascii=False,indent=2),encoding='utf-8')
with main.connect() as db:
 calls=[dict(r) for r in db.execute('SELECT * FROM model_call_metrics') if r['reservation_id'] not in before]
OUT.write_text(json.dumps({'results':results,'status':'done','call_metrics':calls},ensure_ascii=False,indent=2),encoding='utf-8')
