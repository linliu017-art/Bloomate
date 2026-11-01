#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
TMP_DIR="$(mktemp -d)"
PORT="${BLOOMATE_TEST_PORT:-18761}"
PID=""
cleanup() { if [ -n "$PID" ]; then kill "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true; fi; rm -rf "$TMP_DIR"; }
trap cleanup EXIT
if curl -s --max-time 1 "http://127.0.0.1:$PORT/api/v1/garden" >/dev/null; then echo '端口已被占用，检查停止。'; exit 1; fi
start() {
  (cd backend && BLOOMATE_DB="$TMP_DIR/data.sqlite3" BLOOMATE_TEST_MODEL=1 MODEL_API_KEY='' exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT") > "$TMP_DIR/server.log" 2>&1 & PID=$!
  for i in $(seq 1 50); do curl -sf "http://127.0.0.1:$PORT/api/v1/garden" >/dev/null && return 0; sleep 0.1; done
  echo '服务未能启动。'; cat "$TMP_DIR/server.log"; exit 1
}
start
echo '已启动隔离测试服务。'
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
echo '重启后记录恢复，检查通过。'
