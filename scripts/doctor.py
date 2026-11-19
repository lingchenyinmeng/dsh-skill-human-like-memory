"""记忆与人格体检：一条命令看清三个库的健康状况。

只读。所有连接都是 SQLite `mode=ro`，不写、不删、不动原库。
输出只有统计数字和计数，**不打印任何记忆原文、人名或人格内容**。

用法：
    python doctor.py              # 人话报告
    python doctor.py --json       # 机器可读（给看板 / 报告生成器）
    python doctor.py --verbose    # 附带每项的判定依据

退出码：
    0  没发现严重问题
    1  有「高」优先级待办
    2  环境不对（找不到 AstrBot、三个库一个都没读到）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import paths  # noqa: E402

W = 78
HOUR = 3600.0
DAY = 86400.0

CAND_TTL = 48 * HOUR      # 候选区：48 小时内要定性
DORM_TTL = 7 * DAY        # 待定区默认 7 天（库里有 expires_at 时以库为准）
BACKLOG_WARN = 200        # 人格积压超过这么多条就提醒

PERSONA_LAYERS = (
    ("traits", "大五"),
    ("adaptations", "价值观/目标"),
    ("narrative", "生命叙事"),
    ("gestalt", "格式塔"),
    ("schema", "图式"),
    ("core", "核心层"),
    ("voice", "说话风格"),
)


# ────────────────────────────── 基础工具 ──────────────────────────────

def ro(path: str) -> sqlite3.Connection | None:
    """只读打开；不存在或打不开返回 None。"""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    except sqlite3.Error:
        try:
            con = sqlite3.connect(
                "file:" + str(p).replace(os.sep, "/") + "?mode=ro", uri=True, timeout=3
            )
        except sqlite3.Error:
            return None
    con.row_factory = sqlite3.Row
    return con


def has_table(con: sqlite3.Connection, name: str) -> bool:
    try:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (name,),
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def cols(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def one(con: sqlite3.Connection | None, sql: str, args=(), default=0):
    if con is None:
        return default
    try:
        r = con.execute(sql, args).fetchone()
        return r[0] if r is not None and r[0] is not None else default
    except sqlite3.Error:
        return default


def sample(con: sqlite3.Connection | None, sql: str, args=()) -> sqlite3.Row | None:
    if con is None:
        return None
    try:
        return con.execute(sql, args).fetchone()
    except sqlite3.Error:
        return None


def num(v) -> float | None:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def ago(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 0:
        return "刚刚"
    d = seconds / DAY
    if d >= 1:
        return f"{d:.0f} 天"
    return f"{seconds / HOUR:.0f} 小时"


def fmt_ts(ts) -> str:
    f = num(ts)
    if not f:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(f))


def now() -> float:
    return time.time()


# ────────────────────────────── 各段检查 ──────────────────────────────

def check_env() -> dict:
    home = paths.find_astrbot_home()
    dbs = paths.db_paths()
    info = {
        "astrbot_home": home,
        "home_found": bool(home),
        "databases": {},
    }
    for key in ("memory", "people", "personality"):
        p = dbs[key]
        exists = os.path.isfile(p)
        size = os.path.getsize(p) if exists else 0
        info["databases"][key] = {
            "path": p,
            "exists": exists,
            "size_kb": round(size / 1024, 1),
            "tables": [],
        }
    return info


def check_memory(con: sqlite3.Connection | None) -> dict:
    out = {
        "available": con is not None,
        "atoms": 0,
        "by_status": {},
        "expired_now": 0,
        "candidate_aging": 0,
        "dormant_24h": 0,
        "dormant_48h": 0,
        "archived_7d": 0,
        "duplicate_groups": 0,
        "duplicate_atoms": 0,
        "empty_evidence": 0,
        "sensitive": 0,
        "backlog_note": "",
    }
    if con is None or not has_table(con, "atoms"):
        return out

    out["atoms"] = one(con, "SELECT COUNT(*) FROM atoms")
    for r in _rows(con, "SELECT status, COUNT(*) FROM atoms GROUP BY 1 ORDER BY 2 DESC"):
        out["by_status"][r[0] or "(空)"] = r[1]

    c = cols(con, "atoms")
    t = now()
    if "expires_at" in c:
        # 库自己带 expires_at，就信它——别拿文档里的默认 TTL 去猜。
        out["expired_now"] = one(
            con, "SELECT COUNT(*) FROM atoms WHERE expires_at IS NOT NULL AND expires_at < ?", (t,)
        )
        out["dormant_24h"] = one(
            con,
            "SELECT COUNT(*) FROM atoms WHERE status='dormant' AND expires_at < ?",
            (t + DAY,),
        )
        out["dormant_48h"] = one(
            con,
            "SELECT COUNT(*) FROM atoms WHERE status='dormant' AND expires_at < ?",
            (t + 2 * DAY,),
        )
        out["archived_7d"] = one(
            con,
            "SELECT COUNT(*) FROM atoms WHERE status='archived' AND expires_at < ?",
            (t + 7 * DAY,),
        )
    elif "said_at" in c:
        # 老库没这一列：退回文档默认（待定 7 天）。
        out["dormant_24h"] = one(
            con,
            "SELECT COUNT(*) FROM atoms WHERE status='dormant' AND said_at < ?",
            (t - (DORM_TTL - DAY),),
        )
        out["dormant_48h"] = one(
            con,
            "SELECT COUNT(*) FROM atoms WHERE status='dormant' AND said_at < ?",
            (t - (DORM_TTL - 2 * DAY),),
        )

    if "said_at" in c:
        out["candidate_aging"] = one(
            con,
            "SELECT COUNT(*) FROM atoms WHERE status='candidate' AND said_at < ?",
            (t - CAND_TTL,),
        )

    if "content" in c:
        out["duplicate_groups"] = one(
            con,
            "SELECT COUNT(*) FROM (SELECT content FROM atoms "
            "WHERE COALESCE(content,'') <> '' GROUP BY content HAVING COUNT(*) > 1)",
        )
        out["duplicate_atoms"] = one(
            con,
            "SELECT COALESCE(SUM(c - 1), 0) FROM (SELECT COUNT(*) c FROM atoms "
            "WHERE COALESCE(content,'') <> '' GROUP BY content HAVING COUNT(*) > 1)",
        )

    if "evidence" in c:
        out["empty_evidence"] = one(
            con, "SELECT COUNT(*) FROM atoms WHERE COALESCE(evidence, '') = ''"
        )
    if "privacy" in c:
        out["sensitive"] = one(
            con, "SELECT COUNT(*) FROM atoms WHERE privacy = 'sensitive'"
        )
    return out


def check_people(con: sqlite3.Connection | None, mem: sqlite3.Connection | None) -> dict:
    out = {
        "available": con is not None,
        "people": 0,
        "me": 0,
        "unknown": 0,
        "relations": 0,
        "with_tone": 0,
        "low_conf": 0,
        "orphans": 0,
        "dangling": 0,
        "duplicate_relations": 0,
        "events": 0,
    }
    if con is None or not has_table(con, "people"):
        return out

    out["people"] = one(con, "SELECT COUNT(*) FROM people")
    pc = cols(con, "people")
    if "is_me" in pc:
        out["me"] = one(con, "SELECT COUNT(*) FROM people WHERE is_me = 1")
    if "is_unknown" in pc:
        out["unknown"] = one(con, "SELECT COUNT(*) FROM people WHERE is_unknown = 1")

    if has_table(con, "relations"):
        rc = cols(con, "relations")
        cur = "to_ts IS NULL" if "to_ts" in rc else "1=1"
        out["relations"] = one(con, f"SELECT COUNT(*) FROM relations WHERE {cur}")
        if "tone" in rc:
            out["with_tone"] = one(
                con,
                f"SELECT COUNT(*) FROM relations WHERE {cur} AND COALESCE(tone,'') <> ''",
            )
        if "confidence" in rc:
            out["low_conf"] = one(
                con, f"SELECT COUNT(*) FROM relations WHERE {cur} AND confidence < 0.5"
            )
        if "a_id" in rc and "b_id" in rc:
            out["dangling"] = one(
                con,
                f"SELECT COUNT(*) FROM relations r WHERE {cur} AND ("
                "NOT EXISTS (SELECT 1 FROM people p WHERE p.person_id = r.a_id) OR "
                "NOT EXISTS (SELECT 1 FROM people p WHERE p.person_id = r.b_id))",
            )
            if "rel_type" in rc:
                out["duplicate_relations"] = one(
                    con,
                    "SELECT COUNT(*) FROM (SELECT a_id, b_id, rel_type FROM relations "
                    f"WHERE {cur} GROUP BY 1, 2, 3 HAVING COUNT(*) > 1)",
                )
        if "is_me" in pc:
            out["orphans"] = one(
                con,
                f"SELECT COUNT(*) FROM people p WHERE COALESCE(p.is_me,0)=0 "
                f"AND COALESCE(p.is_unknown,0)=0 AND NOT EXISTS ("
                f"SELECT 1 FROM relations r WHERE {cur.replace('to_ts IS NULL', 'r.to_ts IS NULL')} "
                "AND (r.a_id = p.person_id OR r.b_id = p.person_id))",
            )

    if has_table(con, "tone_history"):
        out["events"] = one(con, "SELECT COUNT(*) FROM tone_history")
    return out


def check_persona(con: sqlite3.Connection | None, atom_count: int) -> dict:
    out = {
        "available": con is not None,
        "snapshots": 0,
        "version": None,
        "created_at": None,
        "total_atoms": None,
        "backlog": 0,
        "events": 0,
        "last_evolve_at": None,
        "layers_present": 0,
        "layers_missing": [],
    }
    if con is None or not has_table(con, "personality_snapshots"):
        return out

    out["snapshots"] = one(con, "SELECT COUNT(*) FROM personality_snapshots")
    if has_table(con, "personality_events"):
        out["events"] = one(con, "SELECT COUNT(*) FROM personality_events")

    row = sample(
        con,
        "SELECT * FROM personality_snapshots ORDER BY version DESC, id DESC LIMIT 1",
    )
    if row is not None:
        keys = set(row.keys())
        out["version"] = row["version"] if "version" in keys else None
        out["created_at"] = num(row["created_at"]) if "created_at" in keys else None
        out["total_atoms"] = row["total_atoms"] if "total_atoms" in keys else None
        for field, label in PERSONA_LAYERS:
            if field not in keys:
                out["layers_missing"].append(label)
                continue
            raw = row[field]
            try:
                value = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                value = raw
            if isinstance(value, str):
                ok = bool(value.strip())
            elif isinstance(value, (dict, list)):
                ok = len(value) > 0
            else:
                ok = value not in (None, 0, "")
            if ok:
                out["layers_present"] += 1
            else:
                out["layers_missing"].append(label)
        if out["total_atoms"] and atom_count:
            out["backlog"] = max(0, atom_count - int(out["total_atoms"]))

    st = _state_value(con, "last_evolve_at")
    out["last_evolve_at"] = num(st)
    return out


def _rows(con: sqlite3.Connection, sql: str, args=()) -> list:
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


def _state_value(con: sqlite3.Connection, key: str):
    """personality_state 是键值表，列名可能是 key/value 或 k/v，探一下。"""
    if not has_table(con, "personality_state"):
        return None
    c = cols(con, "personality_state")
    for kcol in ("key", "k", "name"):
        for vcol in ("value", "v", "val"):
            if kcol in c and vcol in c:
                r = sample(
                    con, f"SELECT {vcol} FROM personality_state WHERE {kcol} = ?", (key,)
                )
                if r is not None:
                    return r[0]
    return None


# ────────────────────────────── 待办与结论 ──────────────────────────────

def build_todos(env: dict, mem: dict, ppl: dict, per: dict) -> list[dict]:
    todos: list[dict] = []

    def add(level: str, title: str, action: str, command: str = "") -> None:
        todos.append(
            {"level": level, "title": title, "action": action, "command": command}
        )

    dbs = env["databases"]
    missing = [k for k, v in dbs.items() if not v["exists"]]
    if missing:
        add(
            "高",
            f"缺 {len(missing)} 个库（{'/'.join(missing)}）",
            "先确认对应插件装上了、并且至少跑过一次；路径不对就设 ASTRBOT_HOME。",
            "python query.py --stats",
        )
    if not env["home_found"]:
        add(
            "高",
            "找不到 AstrBot 数据目录",
            "设环境变量 ASTRBOT_HOME 指向含 data\\ 的那一层，或在 skill 目录写 config.json。",
        )

    if mem["available"]:
        if mem["expired_now"]:
            add(
                "高",
                f"{mem['expired_now']} 条记忆已过有效期还留在库里",
                "清理任务可能没在跑。先备份，再确认插件的定时清理是否正常。",
            )
        if mem["dormant_24h"]:
            add(
                "中",
                f"{mem['dormant_24h']} 条待定记忆 24 小时内到期（48h 内 {mem['dormant_48h']} 条）",
                "到点就被删。想留的现在捞出来重新定性，其余让它自然清掉。",
            )
        if mem["candidate_aging"]:
            add(
                "中",
                f"{mem['candidate_aging']} 条候选超过 48 小时没定性",
                "候选超时会被推到待定区。先备份，再让她在 AstrBot 里把想要的定性。",
            )
        if mem["duplicate_atoms"]:
            add(
                "中",
                f"{mem['duplicate_atoms']} 条重复记忆（{mem['duplicate_groups']} 组）",
                "同一句话被记了多次。合并前先备份，合并能明显降噪。",
                "python query.py --recent 20",
            )
        if mem["atoms"] and mem["empty_evidence"] > mem["atoms"] * 0.1:
            add(
                "低",
                f"{mem['empty_evidence']} 条记忆没标证据分层",
                "没证据分层就没法判断可信度，新记忆尽量补上。",
            )

    if ppl["available"]:
        if ppl["dangling"]:
            add(
                "中",
                f"{ppl['dangling']} 条关系指向不存在的人",
                "人对不上号，关系图会断。先跑一次人物重建或手工合并。",
            )
        if ppl["duplicate_relations"]:
            add(
                "中",
                f"{ppl['duplicate_relations']} 组重复关系边",
                "同一对人同一种关系有多条，会影响检索权重。",
            )
        if ppl["people"] and ppl["orphans"] > max(5, ppl["people"] * 0.1):
            add(
                "低",
                f"{ppl['orphans']} 个人没有任何关系边",
                "孤立人物多了说明抽取只认人、不认关系。检查关系抽取是否在跑。",
            )
        if ppl["low_conf"]:
            add(
                "低",
                f"{ppl['low_conf']} 条关系置信度低于 0.5",
                "低置信关系容易被误用，建议找机会确认或降权。",
            )

    if per["available"]:
        if per["layers_missing"]:
            add(
                "高",
                "人格缺层：" + "、".join(per["layers_missing"]),
                "有一层是空的，人格会显得单薄或前后不一致。让她重新演化一次补齐。",
                "python query.py --personality",
            )
        if per["backlog"] >= BACKLOG_WARN:
            add(
                "中",
                f"人格积压 {per['backlog']} 条记忆",
                f"当前人格基于 {per['total_atoms']} 条，库里已有 {per['total_atoms'] + per['backlog']} 条。"
                "积压太多人格会落后于她的经历。",
                "python evolve.py",
            )
        if per["snapshots"] and not per["events"]:
            add(
                "低",
                "人格只有初始版本，没有变化记录",
                "这要么是刚装上，要么是演化没写事件。确认演化真的在跑。",
                "python query.py --why",
            )

    order = {"高": 0, "中": 1, "低": 2}
    todos.sort(key=lambda t: (order[t["level"]], t["title"]))
    return todos


def verdict_of(todos: list[dict], env: dict) -> str:
    if not any(env["databases"][k]["exists"] for k in env["databases"]):
        return "环境不对"
    if any(t["level"] == "高" for t in todos):
        return "该清理了"
    if any(t["level"] == "中" for t in todos):
        return "亚健康"
    return "健康"


# ────────────────────────────── 渲染 ──────────────────────────────

def hr(title: str) -> None:
    print()
    print("─" * W)
    print(title)
    print("─" * W)


def kv(label: str, value, unit: str = "") -> None:
    print(f"  {label:<14}{value}{unit}")


def render(env: dict, mem: dict, ppl: dict, per: dict, todos: list[dict], verdict: str) -> None:
    print("═" * W)
    print("  记忆与人格体检 · doctor")
    print(f"  生成时间 {time.strftime('%Y-%m-%d %H:%M')}    只读，未改动任何数据")
    print("═" * W)

    hr("1. 环境")
    kv("AstrBot", env["astrbot_home"] or "未找到")
    for key, label in (("memory", "记忆库"), ("people", "人库"), ("personality", "人格库")):
        d = env["databases"][key]
        kv(label, "✓ " + (f"{d['size_kb']} KB" if d["exists"] else "缺失"))

    hr("2. 记忆")
    if not mem["available"]:
        print("  （没读到 memory.db，跳过）")
    else:
        kv("原子总数", mem["atoms"])
        if mem["by_status"]:
            kv("状态", "  ".join(f"{k}={v}" for k, v in mem["by_status"].items()))
        kv("已过有效期", mem["expired_now"], " 条")
        kv("候选超 48h", mem["candidate_aging"], " 条")
        kv("待定 24h 内到期", mem["dormant_24h"], f" 条（48h 内 {mem['dormant_48h']}）")
        kv("遗忘库 7 天内删", mem["archived_7d"], " 条")
        kv("重复", f"{mem['duplicate_groups']} 组 / {mem['duplicate_atoms']} 条")
        kv("缺证据分层", mem["empty_evidence"], " 条")
        kv("隐私敏感", mem["sensitive"], " 条")

    hr("3. 人")
    if not ppl["available"]:
        print("  （没读到 people.db，跳过）")
    else:
        kv("人物", ppl["people"], f"（我 {ppl['me']} / 未识别 {ppl['unknown']}）")
        kv("当前关系", ppl["relations"], f"（有底色 {ppl['with_tone']}）")
        kv("孤立人物", ppl["orphans"], " 个")
        kv("断边", ppl["dangling"], " 条")
        kv("重复关系", ppl["duplicate_relations"], " 组")
        kv("低置信", ppl["low_conf"], " 条")

    hr("4. 人格")
    if not per["available"]:
        print("  （没读到 personality.db，跳过）")
    else:
        kv("当前版本", f"v{per['version']}" if per["version"] is not None else "—")
        kv("生成时间", fmt_ts(per["created_at"]))
        kv("快照数", per["snapshots"], f"（变化事件 {per['events']}）")
        kv("层级完整", f"{per['layers_present']}/7",
           ("  缺：" + "、".join(per["layers_missing"])) if per["layers_missing"] else "")
        kv("人格积压", per["backlog"], " 条")
        kv("上次演化", fmt_ts(per["last_evolve_at"]))

    hr("5. 待办（按优先级）")
    if not todos:
        print("  没有需要处理的项。")
    else:
        for i, t in enumerate(todos, 1):
            print(f"  [{t['level']}] {i}. {t['title']}")
            print(f"         {t['action']}")
            if t["command"]:
                print(f"         → {t['command']}")

    print()
    print("─" * W)
    print(f"  结论：{verdict}")
    print("─" * W)
    if verdict != "健康":
        print("  跑任何修改前先备份三个 .db 文件（复制一份就行）。")
    print()


def to_json(env, mem, ppl, per, todos, verdict) -> str:
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "verdict": verdict,
        "env": env,
        "memory": mem,
        "people": ppl,
        "persona": per,
        "todos": todos,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ────────────────────────────── 入口 ──────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="记忆与人格体检（只读）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--verbose", action="store_true", help="附带各库表清单")
    args = ap.parse_args()

    env = check_env()
    dbs = paths.db_paths()

    mem_con = ro(dbs["memory"])
    ppl_con = ro(dbs["people"])
    per_con = ro(dbs["personality"])

    try:
        mem = check_memory(mem_con)
        ppl = check_people(ppl_con, mem_con)
        per = check_persona(per_con, mem["atoms"])

        if args.verbose:
            for key, con in (("memory", mem_con), ("people", ppl_con), ("personality", per_con)):
                if con is None:
                    continue
                env["databases"][key]["tables"] = [
                    r[0] for r in _rows(con, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1")
                ]
    finally:
        for con in (mem_con, ppl_con, per_con):
            if con is not None:
                con.close()

    todos = build_todos(env, mem, ppl, per)
    verdict = verdict_of(todos, env)

    if args.json:
        print(to_json(env, mem, ppl, per, todos, verdict))
    else:
        render(env, mem, ppl, per, todos, verdict)

    if verdict == "环境不对":
        return 2
    if any(t["level"] == "高" for t in todos):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
