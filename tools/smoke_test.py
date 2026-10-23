#!/usr/bin/env python3
"""后端冒烟测试：把所有接口按真实使用顺序跑一遍。

用法：
    python tools/smoke_test.py --base http://127.0.0.1:8010 --password dev
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

PASS, FAIL = 0, 0


def call(base, path, method="GET", body=None, token=None, expect_error=False):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        if expect_error:
            return {"__status": exc.code}
        raise RuntimeError(f"{method} {path} -> {exc.code}: {exc.read().decode()[:300]}") from exc


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}{(' — ' + detail) if detail else ''}")
    else:
        FAIL += 1
        print(f"  ✗ {name}{(' — ' + detail) if detail else ''}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--password", default="dev")
    args = parser.parse_args()
    base = args.base

    print("1. 健康检查与静态页面")
    health = call(base, "/api/health")
    check("GET /api/health", health.get("ok") is True, f"db={health.get('database')}")
    html = urllib.request.urlopen(base + "/", timeout=15).read().decode()
    check("GET / 返回前端页面", "英语磨耳朵" in html, f"{len(html)} 字节")
    check("GET /app.js 可访问", "state" in urllib.request.urlopen(base + "/app.js", timeout=15).read().decode())

    print("\n2. 账号登录与权限")
    owner = call(base, "/api/admin/login", "POST",
                 {"username": "parent", "password": args.password})["token"]
    check("主账号登录", bool(owner))
    check("错误密码被拒",
          call(base, "/api/admin/login", "POST",
               {"username": "parent", "password": "wrong"}, expect_error=True).get("__status") == 401)
    check("无令牌访问被拒", call(base, "/api/students", expect_error=True).get("__status") == 401)

    # 子账号：没有就建一个（学生和计划只能子账号管）
    member_name, member_pwd = "smoke-member", "smoke123456"
    probe = call(base, "/api/admin/login", "POST",
                 {"username": member_name, "password": member_pwd}, expect_error=True)
    if probe.get("__status") == 401:
        call(base, "/api/accounts", "POST",
             {"username": member_name, "password": member_pwd, "display_name": "冒烟子账号"}, owner)
    token = call(base, "/api/admin/login", "POST",
                 {"username": member_name, "password": member_pwd})["token"]
    check("子账号登录", bool(token))

    check("主账号不能管学生", call(base, "/api/students", token=owner, expect_error=True).get("__status") == 403)
    check("子账号不能做内容维护",
          call(base, "/api/scan/preview", "POST", token=token, expect_error=True).get("__status") == 403)
    check("子账号不能管账号",
          call(base, "/api/accounts", token=token, expect_error=True).get("__status") == 403)
    check("子账号可以看内容库", call(base, "/api/videos?limit=1", token=token) is not None)

    overview = call(base, "/api/overview", token=owner)
    check("主账号概览含资源统计",
          overview["content"]["videos"] >= 1 and overview["family"]["accounts"] >= 1,
          f"视频 {overview['content']['videos']} 集 · 账号 {overview['family']['accounts']} 个")
    check("子账号不能看主账号概览",
          call(base, "/api/overview", token=token, expect_error=True).get("__status") == 403)

    print("\n3. 学生")
    students = call(base, "/api/students", token=token)
    if not students:
        call(base, "/api/students", "POST", {"name": "冒烟测试", "daily_target_min": 20}, token)
        students = call(base, "/api/students", token=token)
    student_id = students[0]["id"]
    check("GET /api/students", len(students) > 0, f"{students[0]['name']} (id={student_id})")

    print("\n4. 内容库")
    series = call(base, "/api/series", token=token)
    check("GET /api/series", len(series) > 0, f"{len(series)} 个系列")
    videos = call(base, "/api/videos?limit=500", token=token)
    check("GET /api/videos", videos["total"] > 0, f"{videos['total']} 集")
    graded = [v for v in videos["items"] if v["level"]]
    check("视频带难度分级", len(graded) > 0, f"{len(graded)} 集已分级")
    first = videos["items"][0]
    check("视频带时长与语音占比", first["duration_ms"] > 0, f"{first['duration_ms']}ms, 语音 {first['speech_pct']}%")
    filtered = call(base, f"/api/videos?level={graded[0]['level']}", token=token)
    check("按难度筛选", filtered["total"] >= 1, f"{graded[0]['level']} → {filtered['total']} 集")

    print("\n5. 转写")
    detail = call(base, f"/api/videos/{first['id']}", token=token)
    check("GET /api/videos/{id}", detail["id"] == first["id"])
    try:
        transcript = call(base, f"/api/videos/{first['id']}/transcript", token=token)
        check("GET 转写", transcript["words"] > 0, f"{transcript['words']} 词")
    except RuntimeError:
        check("GET 转写", False, "该集没有转写")

    print("\n6. 设备配对")
    code = call(base, "/api/devices/pair-code", "POST",
                {"student_id": student_id, "device_name": "冒烟平板"}, token)["code"]
    check("POST /api/devices/pair-code", len(code) == 6, code)
    paired = call(base, "/api/devices/pair", "POST",
                  {"code": code, "device_name": "冒烟平板", "platform": "android"})
    device_token = paired["token"]
    check("POST /api/devices/pair", bool(device_token), f"device_id={paired['device_id']}")
    check("配对码不可重复使用",
          call(base, "/api/devices/pair", "POST", {"code": code}, expect_error=True).get("__status") == 400)
    check("设备令牌可读内容库", call(base, "/api/videos?limit=1", token=device_token)["total"] > 0)
    check("设备令牌不能改计划（需要家长）",
          call(base, f"/api/plans/{student_id}/2026-01-01", "PUT", {"target_min": 20, "video_ids": []},
               device_token, expect_error=True).get("__status") == 401)

    print("\n7. 播放事件与幂等")
    now = datetime.now(timezone.utc)
    started = now - timedelta(minutes=3)
    event_id = f"smoke-{uuid.uuid4()}"
    event = {
        "event_id": event_id,
        "video_id": first["id"],
        "started_at": started.isoformat(),
        "ended_at": now.isoformat(),
        "effective_ms": 180000,
        "max_position_ms": first["duration_ms"],
        "seek_count": 0,
        "avg_rate": 1.0,
        "completed": True,
        "client_version": "smoke",
    }
    result = call(base, "/api/events", "POST", {"events": [event]}, device_token)
    check("首次上报被接受", result["accepted"] == 1, json.dumps(result, ensure_ascii=False))
    repeat = call(base, "/api/events", "POST", {"events": [event]}, device_token)
    check("重复上报不重复计数", repeat["accepted"] == 0 and repeat["duplicate"] == 1,
          json.dumps(repeat, ensure_ascii=False))
    bad = dict(event, event_id=f"smoke-{uuid.uuid4()}", video_id=999999)
    rejected = call(base, "/api/events", "POST", {"events": [bad]}, device_token)
    check("不存在的视频被拒绝", rejected["rejected"] == 1, json.dumps(rejected, ensure_ascii=False))

    print("\n8. 报表")
    today = call(base, f"/api/report/{student_id}/today", token=token)
    check("今日报表有有效时长", today["effective_min"] > 0, f"{today['effective_min']} 分钟 / 目标 {today['target_min']}")
    week = call(base, f"/api/report/{student_id}/week", token=token)
    check("本周报表 7 天", len(week["days"]) == 7, f"合计 {week['total_min']} 分钟")
    attention = call(base, f"/api/report/{student_id}/attention", token=token)
    check("关注项可返回", "short_sessions" in attention)
    dashboard = call(base, "/api/dashboard", token=token)
    check("家长首页聚合", len(dashboard["students"]) > 0, f"{len(dashboard['students'])} 个学生")

    print("\n9. 计划")
    day = today["day"]
    ids = [v["id"] for v in videos["items"][:2]]
    put = call(base, f"/api/plans/{student_id}/{day}", "PUT",
               {"target_min": 15, "note": "冒烟测试", "video_ids": ids}, token)
    check("PUT 计划", put["items"] == len(ids), f"{put['items']} 集")
    plan = call(base, f"/api/plans/{student_id}/{day}", token=token)
    check("GET 计划回读", len(plan["items"]) == len(ids) and plan["target_min"] == 15)
    check("设备令牌可读计划", len(call(base, f"/api/plans/{student_id}/{day}", token=device_token)["items"]) == len(ids))

    print("\n9b. 学生端响度数据")
    today_payload = call(base, "/api/student/today", token=device_token)
    items_with_lufs = [it for it in today_payload["items"]
                       if "lufs" in it and "audio_lufs" in it]
    check("学生端任务带响度字段", len(items_with_lufs) == len(today_payload["items"]),
          f"{len(items_with_lufs)}/{len(today_payload['items'])} 集带 lufs/audio_lufs")
    measured = [it["audio_lufs"] for it in today_payload["items"] if it.get("audio_lufs") is not None]
    check("有已测量的归一化响度", len(measured) > 0,
          f"{len(measured)} 集已测，范围 {min(measured):.1f}~{max(measured):.1f} LUFS" if measured else "无")

    print("\n10. 媒体播放地址")
    def fetch_media(path, token_value, extra_headers=None):
        req = urllib.request.Request(base + path)
        req.add_header("Authorization", f"Bearer {token_value}")
        for key, value in (extra_headers or {}).items():
            req.add_header(key, value)
        with urllib.request.urlopen(req, timeout=30) as resp:
            # 键统一小写，避免大小写差异导致断言假失败
            headers = {key.lower(): value for key, value in resp.headers.items()}
            return resp.status, headers, resp.read(2048)

    status, headers, body = fetch_media(f"/api/media/{first['id']}", token)
    check("家长令牌可取播放数据",
          status == 200 and headers.get("accept-ranges") == "bytes" and len(body) > 0,
          f"{headers.get('content-type')} · {len(body)}B")

    status, headers, body = fetch_media(f"/api/media/{first['id']}?track=audio", device_token)
    check("设备令牌可取音轨数据",
          status == 200 and headers.get("content-type", "").startswith("audio/"),
          headers.get("content-type", ""))

    status, headers, body = fetch_media(f"/api/media/{first['id']}?track=audio", token,
                                       {"Range": "bytes=0-1023"})
    check("Range 请求返回 206",
          status == 206 and headers.get("content-range", "").startswith("bytes 0-1023/"),
          headers.get("content-range", ""))

    check("不存在的视频返回 404",
          call(base, "/api/media/999999", token=token, expect_error=True).get("__status") == 404)

    print("\n11. 自动排片")
    auto = call(base, f"/api/plans/{student_id}/{day}/auto?target_min=10", "POST", token=token)
    check("POST 自动排片", auto["items"] >= 1,
          f"{auto['items']} 集 · {auto.get('total_min')} 分钟 · 级别 {auto.get('level')}")
    check("返回构成（复习/新/重复）",
          all(key in auto for key in ("review_count", "new_count", "repeat_count")),
          f"复习 {auto['review_count']} / 新 {auto['new_count']} / 重复 {auto['repeat_count']}")
    auto_plan = call(base, f"/api/plans/{student_id}/{day}", token=token)
    check("计划来源标记为 auto", auto_plan.get("source") == "auto", str(auto_plan.get("source")))
    call(base, f"/api/plans/{student_id}/{day}", "PUT",
         {"target_min": 10, "video_ids": [videos["items"][0]["id"]]}, token)
    check("手动保存后来源变 manual",
          call(base, f"/api/plans/{student_id}/{day}", token=token).get("source") == "manual")

    print("\n12. 改每日目标同步今日计划")
    student_name = students[0]["name"]
    # 先恢复成自动排片，模拟"学生端已自动生成今日计划"
    call(base, f"/api/plans/{student_id}/{day}/auto?target_min=20", "POST", token=token)
    before = call(base, "/api/student/today", token=device_token)
    before_target = before["target_min"]

    # 一周 7 天统一改成 before+20
    new_target = before_target + 20
    patched = call(base, f"/api/students/{student_id}", "PATCH",
                   {"name": student_name, "daily_target_min": new_target,
                    "weekday_targets": [new_target] * 7}, token)
    check("PATCH 学生返回新目标", patched["daily_target_min"] == new_target,
          f"{before_target} → {patched['daily_target_min']}")
    check("自动计划随之重排", patched.get("plan_rebuilt") is True, str(patched.get("plan_rebuilt")))

    after = call(base, "/api/student/today", token=device_token)
    check("学生端目标时长立即生效", after["target_min"] == new_target,
          f"{before_target} → {after['target_min']} 分钟")
    check("重排后任务不为空", len(after["items"]) >= 1, f"{len(after['items'])} 集")

    # 手动计划：只改目标数字，不动家长挑好的内容
    call(base, f"/api/plans/{student_id}/{day}", "PUT",
         {"target_min": 25, "video_ids": [videos["items"][0]["id"]]}, token)
    manual_plan = call(base, f"/api/plans/{student_id}/{day}", token=token)
    check("手动计划只更新目标数字",
          manual_plan["target_min"] == 25 and len(manual_plan["items"]) == 1,
          f"目标 {manual_plan['target_min']} 分钟 · {len(manual_plan['items'])} 集")

    # 还原，避免影响其他断言
    call(base, f"/api/students/{student_id}", "PATCH",
         {"name": student_name, "daily_target_min": 20,
          "weekday_targets": [20] * 7}, token)

    print("\n13. 计划页改目标也写回默认目标")
    call(base, f"/api/plans/{student_id}/{day}", "PUT",
         {"target_min": 30, "video_ids": [videos["items"][0]["id"]]}, token)
    after_put = call(base, "/api/students", token=token)[0]
    check("手动存计划写回默认目标", after_put["daily_target_min"] == 30,
          f"默认目标 {after_put['daily_target_min']} 分钟")

    call(base, f"/api/plans/{student_id}/{day}/auto?target_min=15", "POST", token=token)
    after_auto = call(base, "/api/students", token=token)[0]
    check("自动排片写回默认目标", after_auto["daily_target_min"] == 15,
          f"默认目标 {after_auto['daily_target_min']} 分钟")

    # 还原成 20 分钟并自动排片
    call(base, f"/api/students/{student_id}", "PATCH",
         {"name": student_name, "daily_target_min": 20}, token)

    print("\n14. 按星期目标与一周排片")
    weekday_targets = [15, 15, 15, 15, 15, 30, 30]
    saved = call(base, f"/api/students/{student_id}", "PATCH",
                 {"name": student_name, "daily_target_min": 20,
                  "weekday_targets": weekday_targets}, token)
    check("保存周一~周日目标", saved.get("weekday_targets") == weekday_targets,
          str(saved.get("weekday_targets")))

    week = call(base, f"/api/schedule/{student_id}?days=7", token=token)
    check("返回 7 天排片", len(week["days"]) == 7, f"{len(week['days'])} 天")
    from datetime import date as _date
    weekday_ok = all(
        day["target_min"] == weekday_targets[_date.fromisoformat(day["day"]).weekday()]
        for day in week["days"]
    )
    check("每天按星期取目标", weekday_ok,
          " ".join(f"{d['weekday']}{d['target_min']}" for d in week["days"]))

    flat = [item["video_id"] for day in week["days"] for item in day["items"]]
    check("一周内不重复排同一集", len(flat) == len(set(flat)),
          f"{len(flat)} 集，去重 {len(set(flat))}")

    if flat:
        victim = flat[0]
        done_now = datetime.now(timezone.utc)
        call(base, "/api/events", "POST", {"events": [{
            "event_id": f"smoke-done-{uuid.uuid4()}",
            "video_id": victim,
            "started_at": (done_now - timedelta(minutes=2)).isoformat(),
            "ended_at": done_now.isoformat(),
            "effective_ms": 600000,
            "max_position_ms": 600000,
            "seek_count": 0,
            "avg_rate": 1.0,
            "completed": True,
            "client_version": "smoke",
        }]}, device_token)
        week2 = call(base, f"/api/schedule/{student_id}?days=7", token=token)
        # 今天那份是已落库的快照（学生端正在用），不参与"屏蔽"判断；
        # 看今天之后的日子有没有再排它
        flat2 = [item["video_id"] for day in week2["days"][1:] for item in day["items"]]
        check("听过的集不再出现在后续排片里", victim not in flat2, f"video {victim}")

    # 还原成每天 20 分钟
    call(base, f"/api/students/{student_id}", "PATCH",
         {"name": student_name, "daily_target_min": 20,
          "weekday_targets": [20] * 7}, token)

    # 还原成自动排片，避免影响后续测试（学生端滑动测试需要多集）
    call(base, f"/api/plans/{student_id}/{day}/auto?target_min=20", "POST", token=token)

    print(f"\n{'='*46}\n通过 {PASS}，失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
