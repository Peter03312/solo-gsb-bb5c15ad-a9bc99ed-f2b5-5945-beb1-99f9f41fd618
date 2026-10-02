#!/usr/bin/env bash
# 区域集合变更审计历史调用示例（只读，按全局 revision 升序游标分页）。
#
# 演示：
#   1. 登记 / 修订 / 删除单区 → 各一条 create/update/delete 记录
#   2. 一次批量变更（update+delete+create）→ 整批一条 batch 记录，
#      summary 携带操作总数、按类型计数与逐操作结果
#   3. 首页/下一页游标分页（cursor/limit/next_cursor）
#   4. 失败事务（非法时段 422）不留记录
#   5. 幂等重试（Idempotency-Key）只回放原响应、不新增记录
#   6. 游标/页大小非法（cursor<0、limit 越界）统一 422
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_region_changes.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }
WIDE_FROM="2020-01-01T00:00:00Z"
WIDE_TO="2030-01-01T00:00:00Z"
# 批量变更要求至少一条轨迹在变更后集合下安全：走 y=900 的高空直线
TRAJ='[[{"x":-50,"y":900,"t":"2026-10-02T00:00:00Z"},{"x":950,"y":900,"t":"2026-10-02T00:02:00Z"}]]'

echo "== 0. 清理：删除全部既有区域（404 可忽略），便于从空历史开始演示 =="
for id in $(curl -fsS "$BASE_URL/api/regions" | python3 -c 'import sys,json; print(" ".join(str(r["id"]) for r in json.load(sys.stdin)))' 2>/dev/null); do
  curl -sS -o /dev/null -X DELETE "$BASE_URL/api/regions/$id" || true
done

echo "== 1. 空历史：空数组 + next_cursor=null =="
curl -fsS "$BASE_URL/api/region-changes" | python3 -m json.tool

echo "== 2. 单区登记 A（revision 推进，产生一条 create 记录） =="
curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d "{
        \"name\": \"A区-审计演示\",
        \"polygon\": [[0,0],[100,0],[100,100],[0,100]],
        \"valid_from\": \"$WIDE_FROM\",
        \"valid_to\":   \"$WIDE_TO\"
      }" | python3 -m json.tool
A_ID=$(curl -fsS "$BASE_URL/api/regions" | python3 -c 'import sys,json; print([r for r in json.load(sys.stdin) if r["name"]=="A区-审计演示"][0]["id"])')

echo "== 3. 单区修订 A（改名；产生一条 update 记录，version+1） =="
curl -fsS -X PATCH "$BASE_URL/api/regions/$A_ID" \
  -H 'Content-Type: application/json' \
  -d '{"name": "A区-已修订"}' | python3 -m json.tool

echo "== 4. 再登记 B、C，随后一次批量变更（修订 B + 删除 C + 新建 D） =="
B_ID=$(curl -fsS -X POST "$BASE_URL/api/regions" -H 'Content-Type: application/json' \
  -d "{\"name\":\"B区\",\"polygon\":[[0,0],[50,0],[50,50],[0,50]],\"valid_from\":\"$WIDE_FROM\",\"valid_to\":\"$WIDE_TO\"}" \
  | J "['region']['id']")
C_ID=$(curl -fsS -X POST "$BASE_URL/api/regions" -H 'Content-Type: application/json' \
  -d "{\"name\":\"C区\",\"polygon\":[[100,100],[150,100],[150,150],[100,150]],\"valid_from\":\"$WIDE_FROM\",\"valid_to\":\"$WIDE_TO\"}" \
  | J "['region']['id']")
REV=$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")
echo "B=$B_ID C=$C_ID 当前 revision=$REV"
curl -fsS -X POST "$BASE_URL/api/regions/batch" \
  -H 'Content-Type: application/json' \
  -d "{
        \"expected_revision\": $REV,
        \"operations\": [
          {\"action\": \"update\", \"id\": $B_ID, \"name\": \"B区-批内修订\"},
          {\"action\": \"delete\", \"id\": $C_ID},
          {\"action\": \"create\", \"name\": \"D区-批内新建\",
           \"polygon\": [[200,200],[260,200],[260,260],[200,260]],
           \"valid_from\": \"$WIDE_FROM\", \"valid_to\": \"$WIDE_TO\"}
        ],
        \"trajectories\": $TRAJ
      }" | python3 -m json.tool

echo "== 5. 删除 A（产生一条 delete 记录） =="
curl -fsS -X DELETE "$BASE_URL/api/regions/$A_ID" | python3 -m json.tool

echo "== 6. 查询审计历史首页（按 revision 升序；单区 summary=null，批量带摘要） =="
curl -fsS "$BASE_URL/api/region-changes?limit=2" | python3 -m json.tool

echo "== 7. 用 next_cursor 翻页直到取完 =="
CURSOR=0
PAGE=0
while :; do
  PAGE=$((PAGE+1))
  RESP=$(curl -fsS "$BASE_URL/api/region-changes?limit=2&cursor=$CURSOR")
  echo "-- 第 $PAGE 页（cursor=$CURSOR） --"
  RESP="$RESP" python3 - <<'PY'
import json, os
resp = json.loads(os.environ["RESP"])
for c in resp["changes"]:
    s = c["summary"]
    extra = "" if s is None else (
        f" total={s['total']}(c{s['create_count']}/"
        f"u{s['update_count']}/d{s['delete_count']})"
    )
    print(f"  revision={c['revision']:>3} {c['change_type']:<6} "
          f"region_ids={c['region_ids']}{extra} at={c['occurred_at']}")
PY
  NEXT=$(echo "$RESP" | J "['next_cursor']")
  if [ "$NEXT" = "None" ]; then
    echo "已到末页（next_cursor=null）"
    break
  fi
  CURSOR=$NEXT
done

echo "== 8. 失败事务不留记录：非法时段（valid_from>=valid_to）返回 422 =="
N_BEFORE=$(curl -fsS "$BASE_URL/api/region-changes?limit=500" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["changes"]))')
curl -sS -o /tmp/rc_bad.json -w '非法时段 HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions" -H 'Content-Type: application/json' \
  -d "{\"name\":\"非法\",\"polygon\":[[0,0],[1,0],[1,1],[0,1]],\"valid_from\":\"$WIDE_TO\",\"valid_to\":\"$WIDE_FROM\"}"
python3 -m json.tool /tmp/rc_bad.json
N_AFTER=$(curl -fsS "$BASE_URL/api/region-changes?limit=500" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["changes"]))')
echo "失败前后记录数：$N_BEFORE -> $N_AFTER（应相同）"

echo "== 9. 幂等重试不新增记录：带键删除 B，重复提交只回放原响应 =="
# 键内加时间戳，保证脚本可重复执行（复用旧键删除另一区域会 409 冲突）
RUN_KEY="rc-demo-del-b-$(date +%s)"
curl -fsS -X DELETE "$BASE_URL/api/regions/$B_ID" \
  -H "Idempotency-Key: $RUN_KEY" | python3 -m json.tool
N_FIRST=$(curl -fsS "$BASE_URL/api/region-changes?limit=500" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["changes"]))')
curl -sS -o /tmp/rc_replay.json -w '同键重放 HTTP %{http_code}（仍为 200，非 404）\n' \
  -X DELETE "$BASE_URL/api/regions/$B_ID" \
  -H "Idempotency-Key: $RUN_KEY"
python3 -m json.tool /tmp/rc_replay.json
N_REPLAY=$(curl -fsS "$BASE_URL/api/region-changes?limit=500" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["changes"]))')
echo "首次删除后/同键重放后记录数：$N_FIRST -> $N_REPLAY（重放不新增）"

echo "== 10. 参数非法统一 422 validation_error =="
curl -sS -o /tmp/rc_cur.json -w 'cursor=-1 HTTP %{http_code}\n' "$BASE_URL/api/region-changes?cursor=-1"
python3 -m json.tool /tmp/rc_cur.json
curl -sS -o /dev/null -w 'limit=0    HTTP %{http_code}\n' "$BASE_URL/api/region-changes?limit=0"
curl -sS -o /dev/null -w 'limit=501  HTTP %{http_code}\n' "$BASE_URL/api/region-changes?limit=501"
curl -sS -o /dev/null -w 'cursor=x  HTTP %{http_code}\n' "$BASE_URL/api/region-changes?cursor=x"

echo "完成。审计历史为只读：GET /api/region-changes（省略 cursor 即从最早一页开始）。"
