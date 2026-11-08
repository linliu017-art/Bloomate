#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
TMP_DIR="$(mktemp -d)"
PORT="${BLOOMATE_TEST_PORT:-18761}"
PID=""
CSRF=""
curl() { command curl "$@" -b "$TMP_DIR/cookies" -c "$TMP_DIR/cookies" -H 'Origin: http://127.0.0.1:3001' -H "X-Bloomate-CSRF: $CSRF"; }
cleanup() { if [ -n "$PID" ]; then kill "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true; fi; rm -rf "$TMP_DIR"; }
trap cleanup EXIT
if curl -s --max-time 1 "http://127.0.0.1:$PORT/api/v1/garden" >/dev/null; then echo '端口已被占用，检查停止。'; exit 1; fi
start() {
  (cd backend && BLOOMATE_DB="$TMP_DIR/data.sqlite3" BLOOMATE_TEST_MODEL=1 MODEL_API_KEY='' exec .venv/bin/python ../scripts/restart_test_server.py "$PORT") > "$TMP_DIR/server.log" 2>&1 & PID=$!
  for i in $(seq 1 50); do curl -sf "http://127.0.0.1:$PORT/api/v1/auth/session" >/dev/null && return 0; sleep 0.1; done
  echo '服务未能启动。'; cat "$TMP_DIR/server.log"; exit 1
}
start
echo '已启动隔离测试服务（模型与邮件服务均为mock，不代表真实收码）。'
SESSION="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/auth/guest")"
CSRF="$(printf '%s' "$SESSION" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])')"
CHALLENGE="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/auth/email/challenges" -H 'Content-Type: application/json' -d '{"email":"restart@example.com","terms_version":"local-preview-v1","privacy_version":"local-preview-v1"}' | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["challenge_id"])')"
VERIFY="$(backend/.venv/bin/python -c 'import json,sys; print(json.dumps({"challenge_id":sys.argv[1],"code":"135790"}))' "$CHALLENGE")"
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/auth/email/verify" -H 'Content-Type: application/json' -d "$VERIFY" >/dev/null
CSRF="$(curl -sf "http://127.0.0.1:$PORT/api/v1/auth/session" | backend/.venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); assert d["kind"]=="account"; print(d["csrf_token"])')"
echo '已通过真实认证接口建立隔离测试会话，邮件身份由测试夹具提供。'
TODAY="$(backend/.venv/bin/python -c 'from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())')"
RESULT="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plans" -H 'Content-Type: application/json' -d '{"kind":"goal","goal":"重启检查独有标记","availability":"每天十分钟","session_budget_minutes":10}')"
JOB="$(printf '%s' "$RESULT" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
for i in $(seq 1 50); do
  STATE="$(curl -sf "http://127.0.0.1:$PORT/api/v1/plans/$JOB")"
  if printf '%s' "$STATE" | backend/.venv/bin/python -c 'import json,sys; sys.exit(json.load(sys.stdin)["status"]!="done")'; then break; fi
  sleep 0.1
done
PLANT="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/proposals/$JOB/confirm" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/plants/$PLANT/actions/0" -H 'Content-Type: application/json' -d '{"completed":true}' >/dev/null
echo '已写入确认过的植物和完成步骤。'
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/plants/$PLANT/actions/0" -H 'Content-Type: application/json' -d '{"completed":false}' >/dev/null
DATE_PAYLOAD="$(backend/.venv/bin/python -c 'import json,sys; print(json.dumps({"due_date":sys.argv[1]}))' "$TODAY")"
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/plants/$PLANT/actions/0/date" -H 'Content-Type: application/json' -d "$DATE_PAYLOAD" >/dev/null
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$PLANT/actions" -H 'Content-Type: application/json' -d '{"text":"手动新增步骤"}' >/dev/null
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/plants/$PLANT/actions/1/cancel" -H 'Content-Type: application/json' -d '{"cancelled":true}' >/dev/null
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$PLANT/actions" -H 'Content-Type: application/json' -d '{"text":"已完成的步骤"}' >/dev/null
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/plants/$PLANT/actions/2" -H 'Content-Type: application/json' -d '{"completed":true}' >/dev/null
ADJUSTMENT="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$PLANT/adjustments" -H 'Content-Type: application/json' -d '{"step_index":0,"difficulty":"步骤太难"}' | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
for i in $(seq 1 50); do
  STATE="$(curl -sf "http://127.0.0.1:$PORT/api/v1/adjustments/$ADJUSTMENT")"
  if printf '%s' "$STATE" | backend/.venv/bin/python -c 'import json,sys; sys.exit(json.load(sys.stdin)["status"]!="done")'; then break; fi
  sleep 0.1
done
echo '已写入未确认的调整建议。'
HABIT_RESULT="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plans" -H 'Content-Type: application/json' -d '{"kind":"habit","goal":"重启检查习惯","availability":"每天十分钟","session_budget_minutes":10,"habit_completion_minutes":10}')"
HABIT_JOB="$(printf '%s' "$HABIT_RESULT" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
for i in $(seq 1 50); do
  STATE="$(curl -sf "http://127.0.0.1:$PORT/api/v1/plans/$HABIT_JOB")"
  if printf '%s' "$STATE" | backend/.venv/bin/python -c 'import json,sys; sys.exit(json.load(sys.stdin)["status"]!="done")'; then break; fi
  sleep 0.1
done
HABIT="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/proposals/$HABIT_JOB/confirm" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
STAGE="$(backend/.venv/bin/python -c 'import json,sys; print(json.dumps({"start_date":sys.argv[1],"end_date":sys.argv[1],"timezone":"Asia/Shanghai","weekdays":list(range(7)),"completion_standard":"阅读十分钟"}))' "$TODAY")"
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$HABIT/habit-stage" -H 'Content-Type: application/json' -d "$STAGE" >/dev/null
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/plants/$HABIT/habit-occurrences/$TODAY" -H 'Content-Type: application/json' -d '{"completed":true,"performed":true}' >/dev/null
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/plants/$HABIT/growth/standard" -H 'Content-Type: application/json' -d '{"threshold":1}' >/dev/null
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$HABIT/growth/confirm" >/dev/null
echo '已写入习惯周期和当日打卡。'
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/plants/$HABIT/pause" -H 'Content-Type: application/json' -d '{"paused":true}' >/dev/null
ADVICE="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$HABIT/habit-advice" -H 'Content-Type: application/json' -d '{"difficulty":"时间不足"}' | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
for i in $(seq 1 50); do
  STATE="$(curl -sf "http://127.0.0.1:$PORT/api/v1/habit-advice/$ADVICE")"
  if printf '%s' "$STATE" | backend/.venv/bin/python -c 'import json,sys; sys.exit(json.load(sys.stdin)["status"]!="done")'; then break; fi
  sleep 0.1
done
SCHEDULE="$(backend/.venv/bin/python -c 'import json,sys; from datetime import date,timedelta; print(json.dumps({"end_date":(date.fromisoformat(sys.argv[1])+timedelta(days=14)).isoformat(),"weekdays":[0,2,4]}))' "$TODAY")"
SCHEDULE_ID="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$HABIT/habit-schedule-proposals" -H 'Content-Type: application/json' -d "$SCHEDULE" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
echo '已写入暂停状态和待确认的周期调整。'
FUTURE_RESULT="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plans" -H 'Content-Type: application/json' -d '{"kind":"habit","goal":"重启检查未来习惯","availability":"每天十分钟","session_budget_minutes":10,"habit_completion_minutes":10}')"
FUTURE_JOB="$(printf '%s' "$FUTURE_RESULT" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
for i in $(seq 1 50); do
  STATE="$(curl -sf "http://127.0.0.1:$PORT/api/v1/plans/$FUTURE_JOB")"
  if printf '%s' "$STATE" | backend/.venv/bin/python -c 'import json,sys; sys.exit(json.load(sys.stdin)["status"]!="done")'; then break; fi
  sleep 0.1
done
FUTURE_HABIT="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/proposals/$FUTURE_JOB/confirm" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
FUTURE_STAGE="$(backend/.venv/bin/python -c 'import json,sys; from datetime import date,timedelta; d=date.fromisoformat(sys.argv[1]); print(json.dumps({"start_date":(d+timedelta(days=2)).isoformat(),"end_date":(d+timedelta(days=10)).isoformat(),"timezone":"Asia/Shanghai","weekdays":list(range(7)),"completion_standard":"阅读十分钟"}))' "$TODAY")"
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$FUTURE_HABIT/habit-stage" -H 'Content-Type: application/json' -d "$FUTURE_STAGE" >/dev/null
FUTURE_CHANGE="$(backend/.venv/bin/python -c 'import json,sys; from datetime import date,timedelta; d=date.fromisoformat(sys.argv[1]); print(json.dumps({"start_date":d.isoformat(),"end_date":(d+timedelta(days=10)).isoformat(),"weekdays":list(range(7))}))' "$TODAY")"
FUTURE_PROPOSAL="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$FUTURE_HABIT/habit-schedule-proposals" -H 'Content-Type: application/json' -d "$FUTURE_CHANGE" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
echo '已写入未来习惯和待确认的开始日期调整。'
MULTI_RESULT="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plans" -H 'Content-Type: application/json' -d '{"kind":"habit","goal":"重启检查多阶段习惯","availability":"每天十分钟"}')"
MULTI_JOB="$(printf '%s' "$MULTI_RESULT" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
MULTI="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/proposals/$MULTI_JOB/confirm" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$MULTI/habit-stage" -H 'Content-Type: application/json' -d "$STAGE" >/dev/null
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/plants/$MULTI/habit-occurrences/$TODAY" -H 'Content-Type: application/json' -d '{"completed":true,"performed":true}' >/dev/null
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/plants/$MULTI/growth/standard" -H 'Content-Type: application/json' -d '{"threshold":1}' >/dev/null
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$MULTI/growth/confirm" >/dev/null
NEXT_STAGE="$(backend/.venv/bin/python -c 'import json,sys; from datetime import date,timedelta; d=date.fromisoformat(sys.argv[1]); print(json.dumps({"start_date":(d+timedelta(days=1)).isoformat(),"end_date":(d+timedelta(days=7)).isoformat(),"timezone":"Asia/Shanghai","weekdays":list(range(7)),"completion_standard":"阅读十分钟","growth_threshold":4}))' "$TODAY")"
NEXT_ID="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$MULTI/next-stage-proposals" -H 'Content-Type: application/json' -d "$NEXT_STAGE" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/habit-next-proposals/$NEXT_ID/confirm" >/dev/null
echo '已写入习惯阶段收藏和下一阶段。'
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/memory/preference" -H 'Content-Type: application/json' -d '{"enabled":true}' >/dev/null
MEMORY="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/memory/items" -H 'Content-Type: application/json' -d '{"content":"重启验证偏好"}' | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/memory/preference" -H 'Content-Type: application/json' -d '{"enabled":false}' >/dev/null
echo '已写入授权记忆并关闭使用。'
CARE_JOB="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plans" -H 'Content-Type: application/json' -d '{"kind":"goal","goal":"重启检查园丁关心","availability":"每天十分钟"}' | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
CARE_PLANT="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/proposals/$CARE_JOB/confirm" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$CARE_PLANT/actions" -H 'Content-Type: application/json' -d '{"text":"第二个行动"}' >/dev/null
CARE_DATE1="$(backend/.venv/bin/python -c 'import json,sys; from datetime import date,timedelta; print(json.dumps({"due_date":str(date.fromisoformat(sys.argv[1])-timedelta(days=2))}))' "$TODAY")"
CARE_DATE2="$(backend/.venv/bin/python -c 'import json,sys; from datetime import date,timedelta; print(json.dumps({"due_date":str(date.fromisoformat(sys.argv[1])-timedelta(days=1))}))' "$TODAY")"
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/plants/$CARE_PLANT/actions/0/date" -H 'Content-Type: application/json' -d "$CARE_DATE1" >/dev/null
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/plants/$CARE_PLANT/actions/1/date" -H 'Content-Type: application/json' -d "$CARE_DATE2" >/dev/null
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/care/refresh" >/dev/null
curl -sf "http://127.0.0.1:$PORT/api/v1/care" | backend/.venv/bin/python -c 'import json,sys; assert any(m["plant_title"]=="重启检查园丁关心" for m in json.load(sys.stdin)["messages"])'
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/care/preference" -H 'Content-Type: application/json' -d '{"enabled":false}' >/dev/null
echo '已写入站内关心并关闭提醒。'
ORDERED_PAYLOAD="$(backend/.venv/bin/python -c 'import json,sys; d=sys.argv[1]; print(json.dumps({"kind":"habit","goal":"重启检查先定周期","availability":"十分钟","habit_schedule":{"start_date":d,"end_date":d,"timezone":"Asia/Shanghai","weekdays":list(range(7)),"completion_standard":"阅读十分钟","growth_threshold":1}}))' "$TODAY")"
ORDERED_JOB="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plans" -H 'Content-Type: application/json' -d "$ORDERED_PAYLOAD" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
for i in $(seq 1 50); do
  STATE="$(curl -sf "http://127.0.0.1:$PORT/api/v1/plans/$ORDERED_JOB")"
  if printf '%s' "$STATE" | backend/.venv/bin/python -c 'import json,sys; sys.exit(json.load(sys.stdin)["status"]!="done")'; then break; fi
  sleep 0.1
done
ORDERED_PLANT="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/proposals/$ORDERED_JOB/confirm" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
REV_JOB="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plans" -H 'Content-Type: application/json' -d '{"kind":"habit","goal":"重启检查旧计划修订","availability":"十分钟"}' | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
for i in $(seq 1 50); do
  STATE="$(curl -sf "http://127.0.0.1:$PORT/api/v1/plans/$REV_JOB")"
  if printf '%s' "$STATE" | backend/.venv/bin/python -c 'import json,sys; sys.exit(json.load(sys.stdin)["status"]!="done")'; then break; fi
  sleep 0.1
done
REV_PLANT="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/proposals/$REV_JOB/confirm" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$REV_PLANT/habit-stage" -H 'Content-Type: application/json' -d "$STAGE" >/dev/null
REVISION="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$REV_PLANT/habit-plan-revisions" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
for i in $(seq 1 50); do
  STATE="$(curl -sf "http://127.0.0.1:$PORT/api/v1/habit-plan-revisions/$REVISION")"
  if printf '%s' "$STATE" | backend/.venv/bin/python -c 'import json,sys; sys.exit(json.load(sys.stdin)["status"]!="done")'; then break; fi
  sleep 0.1
done
echo '已写入先定周期的计划和待确认的旧计划修订。'
FOLLOW_JOB="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plans" -H 'Content-Type: application/json' -d '{"kind":"goal","goal":"重启检查后续日期","availability":"十分钟"}' | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
FOLLOW_PLANT="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/proposals/$FOLLOW_JOB/confirm" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/plants/$FOLLOW_PLANT/actions/0" -H 'Content-Type: application/json' -d '{"completed":true}' >/dev/null
FOLLOW_PROPOSAL="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$FOLLOW_PLANT/goal-next-proposals" -H 'Content-Type: application/json' -d '{"title":"后续阶段","steps":["安排日期的步骤"]}' | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
FOLLOW_STAGE="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/goal-next-proposals/$FOLLOW_PROPOSAL/confirm" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["stage_id"])')"
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/goal-stages/$FOLLOW_STAGE/actions/0/date" -H 'Content-Type: application/json' -d "$DATE_PAYLOAD" >/dev/null
echo '已写入目标后续阶段步骤日期。'
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/goal-stages/$FOLLOW_STAGE/actions/0" -H 'Content-Type: application/json' -d '{"completed":true}' >/dev/null
GARDENER_JOB="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/plants/$FOLLOW_PLANT/goal-next-generation" -H 'Content-Type: application/json' -d '{"title":"继续优化","details":"重启检查建议","duration_days":3,"daily_minutes":20}' | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
for i in $(seq 1 50); do
  STATE="$(curl -sf "http://127.0.0.1:$PORT/api/v1/plans/$GARDENER_JOB")"
  if printf '%s' "$STATE" | backend/.venv/bin/python -c 'import json,sys; sys.exit(json.load(sys.stdin)["status"]!="done")'; then break; fi
  sleep 0.1
done
echo '已写入园丁后续阶段建议。'
curl -sf "http://127.0.0.1:$PORT/api/v1/flowers" | backend/.venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); assert any(f["species"]=="lavender" and f["unlocked"] for f in d["flowers"])'
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/flowers/seen" -H 'Content-Type: application/json' -d '{"species":["lavender"]}' >/dev/null
echo '已写入成果解锁与花种已读状态。'
kill "$PID"; wait "$PID" 2>/dev/null || true; PID=''
echo '已停止服务。'
start
curl -sf "http://127.0.0.1:$PORT/api/v1/garden" | backend/.venv/bin/python -c 'import json,sys; assert any(p["title"] == "重启检查独有标记" and p["constraints"]["session_budget_minutes"] == 10 for p in json.load(sys.stdin)["plants"])'
curl -sf "http://127.0.0.1:$PORT/api/v1/plants/$PLANT/actions" | backend/.venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["actions"][0]["completed"] is False'
curl -sf "http://127.0.0.1:$PORT/api/v1/today" | backend/.venv/bin/python -c 'import json,sys; assert any(a["plant_title"]=="重启检查独有标记" for a in json.load(sys.stdin)["goals_today"])'
curl -sf "http://127.0.0.1:$PORT/api/v1/adjustments/$ADJUSTMENT" | backend/.venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["status"] == "done"'
curl -sf "http://127.0.0.1:$PORT/api/v1/plants/$PLANT/growth" | backend/.venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["progress_percent"] == 50.0'
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/adjustments/$ADJUSTMENT/confirm" -H 'Content-Type: application/json' -d '{"steps":["调整后的步骤一","调整后的步骤二"]}' >/dev/null
curl -sf "http://127.0.0.1:$PORT/api/v1/plants/$PLANT/actions" | backend/.venv/bin/python -c 'import json,sys; rows=json.load(sys.stdin)["actions"]; assert len(rows)==4 and rows[0]["text"]=="调整后的步骤一" and rows[1]["text"]=="调整后的步骤二" and rows[2]["cancelled"] is True and rows[3]["completed"] is True'
curl -sf "http://127.0.0.1:$PORT/api/v1/plants/$PLANT/growth" | backend/.venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["progress_percent"] == 50.0'
curl -sf "http://127.0.0.1:$PORT/api/v1/plants/$HABIT/habit-stage" | backend/.venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["occurrences"][0]["completed"] is True'
curl -sf "http://127.0.0.1:$PORT/api/v1/plants/$HABIT/growth" | backend/.venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["achieved"] is True'
curl -sf "http://127.0.0.1:$PORT/api/v1/plants/$HABIT/pause" | backend/.venv/bin/python -c 'import json,sys; p=json.load(sys.stdin); assert p["paused"] is True and p["threshold"]==1 and p["completed"]==1'
curl -sf "http://127.0.0.1:$PORT/api/v1/habit-advice/$ADVICE" | backend/.venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["status"] == "done"'
curl -sf "http://127.0.0.1:$PORT/api/v1/habit-schedule-proposals/$SCHEDULE_ID" | backend/.venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["status"] == "pending"'
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/habit-schedule-proposals/$SCHEDULE_ID/confirm" >/dev/null
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/plants/$HABIT/pause" -H 'Content-Type: application/json' -d '{"paused":false}' >/dev/null
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/plants/$HABIT/habit-occurrences/$TODAY" -H 'Content-Type: application/json' -d '{"completed":false}' >/dev/null
curl -sf "http://127.0.0.1:$PORT/api/v1/plants/$HABIT/growth" | backend/.venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["achieved"] is False'
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/habit-schedule-proposals/$FUTURE_PROPOSAL/confirm" >/dev/null
curl -sf "http://127.0.0.1:$PORT/api/v1/plants/$FUTURE_HABIT/habit-stage" | backend/.venv/bin/python -c 'import json,sys; s=json.load(sys.stdin); assert s["stage"]["status"]=="active" and s["occurrences"][0]["date"]==s["stage"]["today"]'
curl -sf "http://127.0.0.1:$PORT/api/v1/plants/$MULTI/habit-stages" | backend/.venv/bin/python -c 'import json,sys; x=json.load(sys.stdin); assert len(x["collections"])==1 and x["collections"][0]["snapshot"]["completed"]==1'
curl -sf "http://127.0.0.1:$PORT/api/v1/plants/$MULTI/growth" | backend/.venv/bin/python -c 'import json,sys; x=json.load(sys.stdin); assert x["threshold"]==4 and x["achieved"] is False'
curl -sf "http://127.0.0.1:$PORT/api/v1/memory" | backend/.venv/bin/python -c 'import json,sys; x=json.load(sys.stdin); assert x["enabled"] is False and any(i["content"]=="重启验证偏好" for i in x["items"])'
curl -sf -X DELETE "http://127.0.0.1:$PORT/api/v1/memory/items/$MEMORY" >/dev/null
curl -sf "http://127.0.0.1:$PORT/api/v1/care" | backend/.venv/bin/python -c 'import json,sys; x=json.load(sys.stdin); assert x["enabled"] is False and x["messages"]==[]'
curl -sf -X PUT "http://127.0.0.1:$PORT/api/v1/care/preference" -H 'Content-Type: application/json' -d '{"enabled":true}' >/dev/null
curl -sf "http://127.0.0.1:$PORT/api/v1/care" | backend/.venv/bin/python -c 'import json,sys; assert any(m["plant_title"]=="重启检查园丁关心" for m in json.load(sys.stdin)["messages"])'
curl -sf "http://127.0.0.1:$PORT/api/v1/plants/$ORDERED_PLANT/habit-stage" | backend/.venv/bin/python -c 'import json,sys; s=json.load(sys.stdin); assert s["stage"]["start_date"]==s["stage"]["end_date"] and len(s["occurrences"])==1'
curl -sf "http://127.0.0.1:$PORT/api/v1/plants/$ORDERED_PLANT/growth" | backend/.venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["threshold"]==1'
curl -sf "http://127.0.0.1:$PORT/api/v1/habit-plan-revisions/$REVISION" | backend/.venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["status"]=="done"'
curl -sf "http://127.0.0.1:$PORT/api/v1/plants/$FOLLOW_PLANT/goal-stages" | backend/.venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["stages"][0]["dates"][0]==sys.argv[1]' "$TODAY"
curl -sf "http://127.0.0.1:$PORT/api/v1/plans/$GARDENER_JOB" | backend/.venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); assert d["status"]=="done" and d["proposal"]["steps"] and d["followup_time"]=={"duration_days":3,"daily_minutes":20}'
curl -sf "http://127.0.0.1:$PORT/api/v1/completion-notices" | backend/.venv/bin/python -c 'import json,sys; assert any(n["plant_id"]==sys.argv[1] for n in json.load(sys.stdin)["notices"])' "$FOLLOW_PLANT"
curl -sf "http://127.0.0.1:$PORT/api/v1/collections" | backend/.venv/bin/python -c 'import json,sys; assert any(i["plant_id"]==sys.argv[1] and i["kind"]=="habit" for i in json.load(sys.stdin)["collections"])' "$MULTI"
curl -sf "http://127.0.0.1:$PORT/api/v1/today" | backend/.venv/bin/python -c 'import json,sys; assert any(a["text"]=="安排日期的步骤" for a in json.load(sys.stdin)["goals_today"])'
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/habit-plan-revisions/$REVISION/confirm" >/dev/null
curl -sf "http://127.0.0.1:$PORT/api/v1/auth/session" | backend/.venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["kind"]=="account"'
STATUS="$(command curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/v1/garden")"
[ "$STATUS" = '401' ] || { echo '未登录访问没有被拒绝。'; exit 1; }
curl -sf "http://127.0.0.1:$PORT/api/v1/flowers" | backend/.venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); f=next(f for f in d["flowers"] if f["species"]=="lavender"); assert f["unlocked"] and not f["is_new"] and d["counts"]["habit"]>=1'
echo '重启后花种解锁、去重成果及已读状态恢复。'
echo '重启后业务记录与可撤销会话恢复；未登录请求被拒绝，检查通过。'    

# Privacy regression uses only this script's disposable database and mock provider.
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/auth/account/export" | backend/.venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); assert d["format"]=="bloomate-personal-data-v1" and len(d["data"]["plants"])>2; assert "auth_sessions" not in d["data"] and d["agreements"]'
echo '本人业务及同意记录可导出，未包含会话凭证。'
cp "$TMP_DIR/cookies" "$TMP_DIR/cookies-before-erasure"
backend/.venv/bin/python - "$TMP_DIR/data.sqlite3" "$TMP_DIR/pre-erasure.sqlite3" <<'PY'
import sqlite3,sys
with sqlite3.connect(sys.argv[1]) as source, sqlite3.connect(sys.argv[2]) as target:
    source.backup(target)
PY
ERASURE_ID="$(curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/auth/account/deletion-challenges" | backend/.venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["challenge_id"])')"
ERASURE_BODY="$(backend/.venv/bin/python -c 'import json,sys; print(json.dumps({"challenge_id":sys.argv[1],"code":"135790","confirmation":"注销Bloomate账号"}))' "$ERASURE_ID")"
curl -sf -X POST "http://127.0.0.1:$PORT/api/v1/auth/account/delete" -H 'Content-Type: application/json' -d "$ERASURE_BODY" | backend/.venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["status"]=="succeeded"'
STATUS="$(command curl -s -b "$TMP_DIR/cookies-before-erasure" -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/v1/garden")"
[ "$STATUS" = '401' ] || { echo '注销后旧会话仍可读取数据。'; exit 1; }
kill "$PID"; wait "$PID" 2>/dev/null || true; PID=''
cp "$TMP_DIR/pre-erasure.sqlite3" "$TMP_DIR/data.sqlite3"
start
STATUS="$(command curl -s -b "$TMP_DIR/cookies-before-erasure" -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/v1/garden")"
[ "$STATUS" = '401' ] || { echo '旧备份恢复复活了已注销账号。'; exit 1; }
curl -sf "http://127.0.0.1:$PORT/api/v1/auth/deletion-status/$ERASURE_ID" | backend/.venv/bin/python -c 'import json,sys; assert json.load(sys.stdin)["status"]=="succeeded"'
backend/.venv/bin/python - "$TMP_DIR/data.sqlite3" <<'PY_REWARDS'
import sqlite3,sys
with sqlite3.connect(sys.argv[1]) as db:
    for table in ('flower_achievements','flower_unlocks'):
        assert db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
PY_REWARDS
echo '旧备份恢复后，已注销账号的成果与花种奖励也未复活。'
echo '隔离账号注销、旧会话撤销、旧备份恢复后防复活与回执查询全部通过。'
if [ "${BLOOMATE_VERIFY_CLOUD:-0}" = '1' ]; then
  backend/.venv/bin/python scripts/cloud_external_smoke.py verify
  echo '真实云端重新发布后的会话、植物、完成记录与健康检查通过。'
fi
