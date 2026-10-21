# 儿童英语磨耳朵 · 学习管理与监督工具 设计文档

版本：v0.2（客户端 + NAS 后端架构）
场景：自家 1–3 个孩子，Android 平板/手机，NAS 提供后端与内容源，架构为将来扩到班级留位

---

## 0. 实现进度（2026-10-21）

| 部分 | 状态 |
|---|---|
| 预处理（过滤/改名/响度/抽音轨） | ✅ 9/9 实测通过，见 `sample-analysis.md` |
| 转写（faster-whisper small.en） | ✅ 9/9 实测通过，603 词，见 `transcription-report.md` |
| 模型选型 | ✅ 定为 small.en，见 `model-comparison.md` |
| 后端 API | ✅ 33 项冒烟测试全过（**SQLite 验证**，Postgres 未实测） |
| 数据库选型 | ✅ **默认 SQLite**（1–3 人规模足够，备份=拷文件）；Postgres 作为可选 |
| 难度分级 | ✅ 已实现（Dolch 220 + 词形还原 + WPM + 语音占比） |
| 家长 Web 端 | ✅ 4 个页面（概览/内容库/计划/设置） |
| 学生 Web 端 | ✅ 配对/今日任务/听看双模式/记录（43 项 UI 测试通过） |
| **自动排片** | ✅ 复习到期 + 难度匹配 + 时长装填 + 重复补足（`scheduler.py`） |
| Android 客户端 | ❌ 未开始 |

---

## 0.1 v0.2 相对 v0.1 的变化

- 从"纯单机 App"改为**客户端 + NAS 服务端**
- 视频源统一放 NAS，平板按需下载或直连流播，不再每台设备拷一份
- 新增**家长 Web 端**（手机浏览器打开），这是后端带来的核心收益
- 新增**离线优先同步模型**（事件日志 + 幂等上报）
- 明确安全边界：**不把 NAS 端口暴露公网**，远程走 Tailscale/WireGuard

---

## 1. 目标与非目标

**目标**
- 孩子端：熄屏后台连播，2 步内开始，离线可用
- 家长端：任意设备打开网页就能看"今天/本周听了吗、听了多久、哪集异常"
- 内容端：NAS 上一次入库，所有设备共享；家长在网页批量分级、打标签、排计划
- 数据可信：有效收听时长按真实播放秒数计算，可下钻到每集明细

**非目标**
- 不做多租户 SaaS、不做社区、不做排行榜
- 不做人脸/录屏监控
- 不做实时视频转码农场（只对不兼容格式按需转码并缓存）
- 不把 NAS 直接暴露到公网

---

## 2. 总体架构

```
┌──────────────── Android App（孩子端）────────────────┐
│ Kotlin + Compose + Media3                            │
│  ├ Room：内容缓存 / 下载队列 / 事件队列 / 本地统计    │
│  ├ 播放：本地文件优先 → 否则 HTTP Range 直连 NAS      │
│  └ Sync：每 30s 批量上报事件（幂等）；增量拉取计划    │
└───────────────┬──────────────────────────────────────┘
                │ 局域网 HTTP(S) / 远程 WireGuard
┌───────────────▼──────── NAS（Docker Compose）────────┐
│  nginx      静态视频 Range + 反代 + TLS              │
│  api        FastAPI：内容/计划/事件/报表/认证         │
│  worker     ffprobe 扫描、缩略图、按需转码缓存        │
│  db         PostgreSQL 16                            │
│  web        家长端 SPA（同一 API，浏览器访问）        │
│  volume     /media  ← NAS 上的视频目录（只读挂载）    │
└──────────────────────────────────────────────────────┘
```

**为什么这样切**
- 视频走 nginx 静态 Range，不经应用层，省 CPU、天然支持 seek/续传
- api 只管元数据与事件，负载极轻，1–3 个孩子一台 NAS 绰绰有余
- worker 把 ffprobe/ffmpeg 这类重活从请求路径里摘出去
- 家长 Web 与 App 共用一套 API，避免做第二个客户端

---

## 3. 技术选型

| 层 | 选择 | 理由 |
|---|---|---|
| 客户端 | Kotlin + Compose + Media3 + Room | 熄屏后台播放最可靠；音轨可单独禁用 |
| API | FastAPI (Python) | 代码量小、自带 OpenAPI、ffprobe/pandas 处理顺手 |
| DB | PostgreSQL 16 | 容器化零成本，将来扩班级不用迁移 |
| 视频 | 原文件 Range 直出，不做转码 | Android 对 H.264/AAC MP4 兼容最好，转码是浪费 |
| 缩略图 | worker 用 ffmpeg 抽首帧 | 一次性生成，落盘缓存 |
| 家长端 | 单页应用（React/Vue 或服务端模板） | 手机浏览器为主，无需 App |
| 远程访问 | Tailscale / WireGuard | **不开放公网端口** |
| 部署 | docker-compose.yml 一份 | 飞牛/群晖/任意 Linux 都能跑 |

---

## 4. 数据模型（服务端）

```sql
-- 内容
video(id, rel_path, title, series, level, duration_ms, cover_path,
      tags, file_hash, size_bytes, mtime, status, created_at)
series(id, name, level, description, sort_order)

-- 学生与设备
student(id, name, avatar, daily_target_min, active)
device(id, student_id, name, token_hash, last_seen_at, platform)

-- 计划
plan(id, student_id, date, target_min, note)
plan_item(id, plan_id, video_id, order_index, kind)  -- kind: new | review

-- 播放事件（只追加，幂等）
play_event(id, event_id UNIQUE, student_id, device_id, video_id,
           started_at, ended_at, effective_ms, max_position_ms,
           seek_count, avg_rate, completed, client_version)

-- 聚合（写入时更新，报表 O(1)）
daily_stat(student_id, date, effective_ms, completed_count, session_count,
           PRIMARY KEY (student_id, date))
review_item(student_id, video_id, due_date, stage, last_score)

-- 二期
quiz(id, video_id, title) / question(id, quiz_id, type, payload, answer)
quiz_attempt(id, student_id, quiz_id, score, detail, created_at)
```

**同步原则**
- 客户端只上报**事件**，服务端只做聚合 → 天然无冲突
- `event_id`（UUIDv7）唯一约束 → 重传安全，断网补传不重复计数
- 客户端拉取用 `since` 游标（`updated_at`）→ 只取增量
- 配置类数据（计划、标签、分级）**以服务端为准**，客户端只读

---

## 5. 客户端要点

### 5.1 播放（磨耳朵的核心）
- `MediaSessionService` + 前台服务通知，声明 `FOREGROUND_SERVICE_MEDIA_PLAYBACK`
- **音频优先模式**：`setVideoTrackSelectionParameters { setDisabled(true) }`，只解码音轨
- 磨耳朵模式下隐藏进度条（防拖动）
- **底部遮罩（必须）**：素材是硬字幕、双行双语艺术字，关不掉。磨耳朵模式必须叠加底部约 20% 高度的纯黑遮罩，否则孩子直接读中文译文、完全绕过"听"
- 遮罩高度按屏高百分比配置（横屏/竖屏两档默认），家长可拖动微调一次并记住
- 连播、单集循环、0.8–1.2 倍速；熄屏/后台继续
- 引导加入电池优化白名单；针对小米/华为/OPPO 给自启设置指引

### 5.2 有效时长（可信度核心）
```
每 1 秒采样：
  if (isPlaying && !isBuffering && !isSeeking && rate <= 1.25) effectiveMs += 1000
```
- seek 后 2 秒内的进度不计入；倍速 >1.25 不判完成
- 完成判定：`max_position_ms / speech_ms >= 0.9` 且 `effective_ms >= 10_000`
  （**不能用固定 30 秒门槛**：样本中最短的集只有 20 秒，3 集不足 30 秒会被永久误杀）
- 每次进入/退出播放器生成一条 `play_event`，实时入本地队列

### 5.3 离线优先
- 下载整集 MP4 到 App 私有目录，边下边播（HTTP Range）
- 离线时事件入队，联网后批量 flush（每批 ≤200 条，失败退避重试）
- 计划与内容元数据本地缓存，冷启动不依赖网络
- 下载策略：家长在 Web 端勾选"预下载本系列"，App 后台拉取

### 5.4 设备配对
```
家长在 Web 端点"添加设备" → 生成 6 位配对码（5 分钟有效）
孩子在 App 输入配对码 → 换取长期 device token（存 Keystore）
```

---

### 5.5 转写（ASR，替代 OCR）

素材无字幕轨、画面底部是**艺术字双行双语硬字幕** → OCR 准确率仅 60–80%，且需处理逐帧检测与去重，工程量更大。**转写路线定为 faster-whisper（ASR），OCR 不做。**

```
ffmpeg 抽音轨（16k mono, highpass=80, loudnorm=I=-16）→ faster-whisper small.en int8
→ 关闭 VAD + 幻觉过滤 → segments(带时间戳) 入库
```

**实测结论（9 集样本，见 `transcription-report.md`）：这套素材上必须关闭 VAD。**

| VAD 设置 | 总词数 | 说明 |
|---|---|---|
| 阈值 0.5（库默认） | 308 | 大量对白被整段切走 |
| 阈值 0.25 | 497 | 改善但仍损失 |
| **关闭** | **603** | 采用 |

原因：音乐/音效连续铺底（静音占比仅 0–8%），Silero VAD 把"音乐+对白"判为非语音。同理 `no_speech_prob` 也失效——正常对白段该值可达 0.92，按常规阈值 0.6 过滤会丢掉一半真实内容。

- 幻觉过滤只能靠三条：`compression_ratio > 2.4` 丢、`avg_logprob < -1.0` 丢、相邻重复去重，再加字幕组套话黑名单（"please subscribe" 等）
- `condition_on_previous_text=False` 必开，切断上下文，显著降低复读幻觉
- 幂等键：`file_hash + model + 参数版本`，避免重复跑
- 产物用途：自动分级、生词表、出题、全文搜索、`speech_ms` 修正完成判定
- 人工校对闭环：家长端可改 transcript，改动版本化（双语硬字幕可直接对着画面核对，校对成本低）
- 性能：开发机 8.6× 实时（int8/3 线程）；N100 预计 3–4×，900 分钟素材需夜间跑 4–5 小时

**模型选型：`small.en`（已定，不再评估）**

9 集 A/B 实测（`model-comparison.md`）：medium.en 总词数只多 3.8%（603 → 626），唯一词只多 3 个（246 → 249），合并去重后词汇量 254——**词汇差异 3%，不值得 3.1 倍耗时**。medium 的真正优势是补回漏句（S01E01 补回 6 句），但代价是 N100 上 20 小时+/全量，不可行。

结论：全流程统一用 `small.en`；出题/字幕若发现上下文缺失，再对单集临时用 medium 重跑。

---

## 6. 家长 Web 端（后端的主要价值）

**今日**：每个孩子一张卡——有效时长 / 目标、完成集数、连续天数
**本周**：柱状图（每天分钟数）、内容分布（哪些系列在反复听）
**关注项**（不是"警告"）：
- 某集只听了 <30% 就退出，连续 2 次
- 某天时长为 0
- 某系列重复次数异常高（可能难度不合适）
**内容管理**：目录扫描结果 → 批量改系列/级别/标签、合并重名、标记不适合
**计划**：按周模板排片（每天 20 分钟 = 2–3 集），一键下发到设备
**导出**：CSV / 月度小结

---

## 7. 部署与安全

```yaml
# docker-compose.yml 结构（示意）
services:
  nginx:   # 80/443，静态 /media Range + 反代 /api
  api:     # FastAPI，内网 8080，仅 nginx 可达
  worker:  # ffprobe/ffmpeg 任务，挂 /media 只读 + /cache 读写
  db:      # postgres:16，volume 持久化，不映射端口
volumes:
  media:   # NAS 视频目录（只读）
  cache:   # 缩略图 / 转码缓存
  pgdata:
```

**安全红线**
- 视频目录容器内**只读挂载**，API 不提供任何写文件接口
- 数据库不映射宿主机端口
- 外网访问只走 Tailscale/WireGuard，路由器上不开端口转发
- 家长端强制密码登录；App 用设备 token，不用明文密码

---

## 8. 功能分期

| 阶段 | 内容 | 验收 |
|---|---|---|
| M0 | compose 起 api+db+nginx，健康检查 | `curl /api/health` 通，视频能 Range 播放 |
| M1 | 内容扫描入库 + 缩略图 + 家长 Web 内容列表 | NAS 上一个目录扫进库，网页能浏览/改标签 |
| M2 | Android App：库 / 播放器 / 熄屏连播 / 事件上报 | 平板熄屏连听 3 集，网页立刻看到记录 |
| M3 | 每日计划 + 家长今日/本周报表 + 关注项 | 家长手机上能判断"今天达标了吗" |
| M4 | 离线下载 + 断网补传 | 飞行模式听 3 集，联网后数据补齐且不重复 |
| M5 | 小测 + 生词 + 间隔复习 | 听完能测，错题回到对应集 |
| M6 | 时段限制、多孩子切换、月度导出 | 可长期日常使用 |

---

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| NAS 挂了孩子就听不了 | 客户端离线缓存 + 已下载集数可离线播放 |
| 同步重复计数 | `event_id` 唯一约束 + 服务端幂等写入 |
| 后台播放被系统杀 | 前台服务 + 电池白名单 + 机型指引 |
| 家长嫌网页麻烦 | 只做 3 个数字：今天几分钟、连续几天、有没有异常 |
| 视频格式不兼容 | worker 按需转码为 H.264/AAC 720p 并缓存，仅对不兼容文件触发 |
| 版权 | 资源仅家庭自用，NAS 不对外提供下载 |

---

## 10. 可选的"少写代码"路线

若想更快见效：**Jellyfin 管视频库 + 播放进度**，自研只做"学习层"（计划、有效时长、报表）。
- 省掉：扫描入库、缩略图、转码、多端播放器
- 代价：拿不到"音频优先、熄屏计时、防拖动、有效时长"这些磨耳朵的关键能力，进度也只能到"播到第几秒"
- 结论：适合 M0–M1 过渡验证，终态仍建议自研客户端
