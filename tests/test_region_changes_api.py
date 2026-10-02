"""区域集合变更审计历史 API 集成测试，需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_region_changes_api.py
脚本会清空 regions/region_meta/passes/commitments/observations/
observation_events/idempotency_keys/region_changes 后跑全套用例，
结束时再次清空，避免影响其它测试脚本。
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


def iso_z(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def future(seconds: float) -> str:
    return iso_z(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def changes(client, **params):
    r = client.get("/api/region-changes", params=params or {})
    assert r.status_code == 200, r.text
    return r.json()


WIDE = ("2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z")

db.init_pool()
client = TestClient(app)


def reset_all():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE observation_events, observations, idempotency_keys, "
                "commitments, passes, region_changes, regions RESTART IDENTITY"
            )
            cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
        conn.commit()


def change_count() -> int:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM region_changes")
            return cur.fetchone()[0]


reset_all()

# ---- 无任何变更：空数组 + next_cursor=null ----
page = changes(client)
check("无记录返回空数组与 null",
      page["changes"] == [] and page["next_cursor"] is None, str(page))

# 省略参数与显式 cursor=0&limit=100 等价
check("省略参数等价 cursor=0/limit=100",
      changes(client, cursor=0, limit=100) == page)

# ---- 单区登记：revision=1，type=create ----
r = client.post("/api/regions", json={
    "name": "A区", "polygon": [[0, 0], [100, 0], [100, 100], [0, 100]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("登记 A 201 revision=1", r.status_code == 201, r.text)
id_a = r.json()["region"]["id"]
page = changes(client)
check("登记后一条审计 create@1",
      len(page["changes"]) == 1 and page["next_cursor"] is None, str(page))
c = page["changes"][0]
check("create 记录字段齐全",
      c["revision"] == 1 and c["change_type"] == "create"
      and c["region_ids"] == [id_a] and c["summary"] is None
      and c["occurred_at"].endswith("Z"), str(c))

# ---- 单区修订：revision=2，type=update，version+1（审计不含 version 明细） ----
r = client.patch(f"/api/regions/{id_a}", json={"name": "A区-改名"})
check("修订 A 200 revision=2", r.status_code == 200 and r.json()["revision"] == 2, r.text)
page = changes(client)
check("审计 2 条且按 revision 升序",
      [x["revision"] for x in page["changes"]] == [1, 2]
      and [x["change_type"] for x in page["changes"]] == ["create", "update"],
      str([(x["revision"], x["change_type"]) for x in page["changes"]]))
u = page["changes"][1]
check("update 记录 revision=2 / 区域编号 / summary=null",
      u["change_type"] == "update" and u["region_ids"] == [id_a]
      and u["summary"] is None, str(u))

# ---- 单区删除：revision=3，type=delete ----
r = client.delete(f"/api/regions/{id_a}")
check("删除 A 200 revision=3", r.status_code == 200 and r.json()["revision"] == 3, r.text)
page = changes(client)
d = page["changes"][2]
check("delete 记录 revision=3 / 区域编号 / summary=null",
      d["revision"] == 3 and d["change_type"] == "delete"
      and d["region_ids"] == [id_a] and d["summary"] is None, str(d))
check("revision 连续递增 1,2,3",
      [x["revision"] for x in page["changes"]] == [1, 2, 3])

# 区域已物理删除，审计仍保留其编号
check("删除后审计仍保留区域编号",
      client.get(f"/api/regions/{id_a}").status_code == 404
      and all(id_a in x["region_ids"] for x in page["changes"]))

# ---- 失败事务不留记录：404（删除不存在区域）、422（非法时段）、409（凭证保护） ----
n_before = change_count()
r = client.delete("/api/regions/999999")
check("删除不存在区域 404", r.status_code == 404, r.text)
r = client.post("/api/regions", json={
    "name": "非法时段", "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]],
    "valid_from": WIDE[1], "valid_to": WIDE[0],
})
check("非法时段登记 422", r.status_code == 422, r.text)
# 凭证保护：无区域时为远处未来轨迹签发凭证，再登记压住该轨迹的区域 -> 409
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 50, "t": future(60)},
    {"x": 350, "y": 50, "t": future(120)},
]})
check("签发凭证 201", r.status_code == 201, r.text)
r = client.post("/api/regions", json={
    "name": "挡路区", "polygon": [[0, 0], [400, 0], [400, 100], [0, 100]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
check("凭证保护挡路区登记 409 pass_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "pass_conflict", r.text)
# 批量失败：revision 冲突 409 与轨迹不安全 422
cur_rev = client.get("/api/revision").json()["revision"]
r = client.post("/api/regions/batch", json={
    "expected_revision": cur_rev + 100,
    "operations": [{"action": "create", "name": "冲突批",
                    "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]],
                    "valid_from": WIDE[0], "valid_to": WIDE[1]}],
    "trajectories": [[
        {"x": -50, "y": 900, "t": future(60)},
        {"x": 350, "y": 900, "t": future(120)},
    ]],
})
check("批量 revision 冲突 409",
      r.status_code == 409 and r.json()["error"]["code"] == "revision_conflict", r.text)
r = client.post("/api/regions/batch", json={
    "expected_revision": cur_rev,
    "operations": [{"action": "create", "name": "不安全批",
                    "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
                    "valid_from": WIDE[0], "valid_to": WIDE[1]}],
    "trajectories": [[
        {"x": 0, "y": 5, "t": future(60)},
        {"x": 5, "y": 5, "t": future(120)},
    ]],
})
check("批量轨迹不安全 422",
      r.status_code == 422 and r.json()["error"]["code"] == "unsafe_trajectory", r.text)
check("全部失败事务不留审计记录、revision 不变",
      change_count() == n_before
      and client.get("/api/revision").json()["revision"] == cur_rev,
      f"{n_before} -> {change_count()}")

# ---- 凭证/承诺/观察的签发与取消不产生区域变更记录 ----
r = client.post("/api/commitments", json={
    "points": [{"x": -50, "y": 700, "t": 0}, {"x": 350, "y": 700, "t": 60}],
    "window_start": future(120), "window_end": future(600),
})
check("签发承诺 201", r.status_code == 201, r.text)
cid = r.json()["id"]
r = client.post(f"/api/commitments/{cid}/cancel")
check("取消承诺 200", r.status_code == 200, r.text)
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 200, "t": future(60)},
    {"x": 350, "y": 200, "t": future(120)},
]})
check("登记观察 201", r.status_code == 201, r.text)
oid = r.json()["id"]
r = client.post(f"/api/observations/{oid}/cancel")
check("取消观察 200", r.status_code == 200, r.text)
# 上面还签发过一张凭证，把它也取消
with db.pool.connection() as conn, conn.cursor() as cur:
    cur.execute("SELECT id FROM passes ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
if row:
    check("取消凭证 200",
          client.post(f"/api/passes/{row[0]}/cancel").status_code == 200)
check("凭证/承诺/观察签发与取消均不产生审计记录、不推进 revision",
      change_count() == n_before
      and client.get("/api/revision").json()["revision"] == cur_rev)

# ---- 批量变更：整批一条 batch 记录，summary 携带逐操作与计数 ----
# 先登记两个区域 B、C（revision 继续推进），再做 create+update+delete 混合批
r = client.post("/api/regions", json={
    "name": "B区", "polygon": [[0, 0], [50, 0], [50, 50], [0, 50]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
id_b = r.json()["region"]["id"]
r = client.post("/api/regions", json={
    "name": "C区", "polygon": [[100, 100], [150, 100], [150, 150], [100, 150]],
    "valid_from": WIDE[0], "valid_to": WIDE[1],
})
id_c = r.json()["region"]["id"]
rev_before_batch = client.get("/api/revision").json()["revision"]
n_before = change_count()
r = client.post("/api/regions/batch", json={
    "expected_revision": rev_before_batch,
    "operations": [
        {"action": "update", "id": id_b, "name": "B区-修订"},
        {"action": "delete", "id": id_c},
        {"action": "create", "name": "D区",
         "polygon": [[200, 200], [260, 200], [260, 260], [200, 260]],
         "valid_from": WIDE[0], "valid_to": WIDE[1]},
    ],
    "trajectories": [[
        {"x": -50, "y": 900, "t": future(60)},
        {"x": 950, "y": 900, "t": future(120)},
    ]],
})
check("混合批量 200", r.status_code == 200, r.text)
batch_resp = r.json()
batch_rev = batch_resp["revision"]
check("批量只推进一次 revision",
      batch_rev == rev_before_batch + 1 and change_count() == n_before + 1)
page = changes(client, cursor=rev_before_batch)
check("游标后恰好一条 batch 记录，next_cursor=null",
      len(page["changes"]) == 1 and page["next_cursor"] is None, str(page))
b = page["changes"][0]
new_id = [op["region_id"] for op in batch_resp["operations"]
          if op["action"] == "create"][0]
check("batch 记录类型/revision/区域编号顺序",
      b["change_type"] == "batch" and b["revision"] == batch_rev
      and b["region_ids"] == [id_b, id_c, new_id], str(b))
s = b["summary"]
check("批量摘要计数 total=3 / 1 each",
      s["total"] == 3 and s["create_count"] == 1
      and s["update_count"] == 1 and s["delete_count"] == 1, str(s))
check("摘要逐操作与成功响应 operations 完全一致",
      s["operations"] == batch_resp["operations"], str(s["operations"]))
check("批量记录 occurred_at 为 UTC Z", b["occurred_at"].endswith("Z"))

# ---- 幂等重试不新增记录 ----
# 单区删除幂等：D 区
r1 = client.delete(f"/api/regions/{new_id}", headers={"Idempotency-Key": "hist-del-d"})
check("带键删除 200", r1.status_code == 200, r1.text)
n_after = change_count()
rev_after = r1.json()["revision"]
r2 = client.delete(f"/api/regions/{new_id}", headers={"Idempotency-Key": "hist-del-d"})
check("同键删除重试回放原 200（非 404）",
      r2.status_code == 200 and r2.json() == r1.json(), r2.text)
check("幂等重试不新增审计记录、不重复推进 revision",
      change_count() == n_after
      and client.get("/api/revision").json()["revision"] == rev_after)
# 批量幂等
body = {
    "expected_revision": rev_after,
    "operations": [{"action": "delete", "id": id_b}],
    "trajectories": [[
        {"x": -50, "y": 900, "t": future(60)},
        {"x": 950, "y": 900, "t": future(120)},
    ]],
}
r1 = client.post("/api/regions/batch", json=body,
                 headers={"Idempotency-Key": "hist-batch-del-b"})
check("带键批量删除 200", r1.status_code == 200, r1.text)
n_after = change_count()
r2 = client.post("/api/regions/batch", json=body,
                 headers={"Idempotency-Key": "hist-batch-del-b"})
check("批量同键重试回放原响应", r2.status_code == 200 and r2.json() == r1.json(), r2.text)
check("批量幂等重试不新增审计记录", change_count() == n_after)

# ---- 游标分页：revision 升序、limit、next_cursor，全集无重复无遗漏 ----
# 再制造若干单区变更，凑足分页数据
seed_ids = []
for i in range(5):
    rr = client.post("/api/regions", json={
        "name": f"分页区-{i}",
        "polygon": [[800 + i, 0], [810 + i, 0], [810 + i, 10], [800 + i, 10]],
        "valid_from": WIDE[0], "valid_to": WIDE[1],
    })
    seed_ids.append(rr.json()["region"]["id"])
all_page = changes(client, limit=500)
all_revs = [x["revision"] for x in all_page["changes"]]
check("全量按 revision 严格升序且唯一",
      all_revs == sorted(all_revs) and len(all_revs) == len(set(all_revs)),
      str(all_revs))

p1 = changes(client, limit=3)
check("首页 limit=3 返回 3 条且 next_cursor=第 3 条 revision",
      [x["revision"] for x in p1["changes"]] == all_revs[:3]
      and p1["next_cursor"] == all_revs[2], str(p1))
p2 = changes(client, cursor=p1["next_cursor"], limit=3)
check("第二页从第 4 条开始",
      [x["revision"] for x in p2["changes"]] == all_revs[3:6], str(p2))

# 全程翻页，无重复无遗漏
seen: list[int] = []
cur = 0
while True:
    pg = changes(client, cursor=cur, limit=4)
    seen.extend(x["revision"] for x in pg["changes"])
    if pg["next_cursor"] is None:
        break
    check("非末页本页填满 4 条", len(pg["changes"]) == 4)
    cur = pg["next_cursor"]
check("游标分页遍历全集、无重复无遗漏、末页 next_cursor=null", seen == all_revs,
      f"{len(seen)} vs {len(all_revs)}")

# 越界游标：空页 null
tail = changes(client, cursor=all_revs[-1] + 1000)
check("越界游标返回空页 next_cursor=null",
      tail["changes"] == [] and tail["next_cursor"] is None, str(tail))
# 中间游标只返回严格大于游标的记录
mid = all_revs[2]
pg = changes(client, cursor=mid, limit=500)
check("cursor=N 只返回 revision>N",
      all(x["revision"] > mid for x in pg["changes"])
      and [x["revision"] for x in pg["changes"]] == [r for r in all_revs if r > mid])

# ---- 分页参数非法：统一 422 validation_error ----
r = client.get("/api/region-changes", params={"cursor": -1})
check("cursor<0 返回 422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error", r.text)
r = client.get("/api/region-changes", params={"cursor": "abc"})
check("cursor 非整数返回 422", r.status_code == 422, r.text)
r = client.get("/api/region-changes", params={"limit": 0})
check("limit=0 返回 422", r.status_code == 422, r.text)
r = client.get("/api/region-changes", params={"limit": 501})
check("limit>500 返回 422", r.status_code == 422, r.text)
r = client.get("/api/region-changes", params={"limit": "abc"})
check("limit 非整数返回 422", r.status_code == 422, r.text)
r = client.get("/api/region-changes", params={"unknown_param": 1})
check("多余查询参数：FastAPI 默认忽略仍 200", r.status_code == 200, r.text)

# ---- 只读：不接受写方法 ----
r = client.post("/api/region-changes")
check("POST 审计端点 405", r.status_code == 405, r.status_code)

# ---- 并发写入 + 分页：审计 revision 唯一，与当前 revision 严格一致 ----
# 多个创建并发提交；审计行数应恰好等于 revision 增量，且 revision 集合 = 1..当前值
base_n = change_count()
base_rev = client.get("/api/revision").json()["revision"]
K = 8
barrier = threading.Barrier(K)
errors: list = []
elock = threading.Lock()


def create_worker(i):
    barrier.wait()
    rr = client.post("/api/regions", json={
        "name": f"并发审计-{i}",
        "polygon": [[600 + i, 0], [605 + i, 0], [605 + i, 5], [600 + i, 5]],
        "valid_from": WIDE[0], "valid_to": WIDE[1],
    })
    if rr.status_code != 201:
        with elock:
            errors.append((i, rr.status_code, rr.text[:120]))


threads = [threading.Thread(target=create_worker, args=(i,)) for i in range(K)]
for t in threads:
    t.start()
for t in threads:
    t.join(30)
check("并发登记全部 201（无 5xx/死锁）", not errors, str(errors))
final_rev = client.get("/api/revision").json()["revision"]
page = changes(client, limit=500)
new_revs = [x["revision"] for x in page["changes"] if x["revision"] > base_rev]
check("并发后审计新增行数 == revision 增量",
      len(new_revs) == final_rev - base_rev == K,
      f"{len(new_revs)} vs {final_rev - base_rev}")
check("并发审计 revision 连续唯一（无重复无遗漏）",
      sorted(new_revs) == list(range(base_rev + 1, final_rev + 1)), str(new_revs))
check("并发新增均为 create 单区记录（summary=null）",
      all(x["change_type"] == "create" and x["summary"] is None
          for x in page["changes"] if x["revision"] > base_rev))

# ---- 清理，避免影响其它测试脚本 ----
reset_all()

print()
if failures:
    print(f"{len(failures)} 个失败")
    sys.exit(1)
print("全部区域集合变更审计历史 API 集成用例通过")
