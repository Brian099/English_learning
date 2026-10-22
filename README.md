# 儿童英语磨耳朵 · 学习管理与监督工具

给孩子看的英语动画做「管理 + 监督 + 学习闭环」：NAS 后端 + 家长网页端 + 学生网页端。
内容入库、ASR 转写、难度分级、听/看双模式播放、有效时长统计、计划与报表。

设计文档见 [`docs/DESIGN.md`](docs/DESIGN.md)。**Android 原生客户端尚未包含**（网页版已可用）。

```
.
├─ README.md
├─ .github/workflows/docker.yml  推送 main 自动构建镜像到 Docker Hub
├─ docs/                     设计与部署文档
│  ├─ DESIGN.md              设计稿 v0.2
│  ├─ deploy-fnos.md         **飞牛 fnOS 部署步骤**
│  ├─ adding-videos.md       **新增视频怎么操作（增量）**
│  ├─ sample-analysis.md     样本技术分析（编码/响度/时长/命名）
│  ├─ transcription-report.md 转写结果与参数调优
│  ├─ model-comparison.md    small.en vs medium.en 选型依据
│  └─ frontend-testing.md    前端验证报告（含真机问题记录）
├─ deploy/                   docker-compose、nginx、.env 模板
├─ server/                   FastAPI 应用
│  ├─ Dockerfile
│  ├─ requirements.txt
│  └─ app/
│     ├─ main.py             入口（含静态前端托管）
│     ├─ config.py           环境变量配置
│     ├─ db.py               数据库（SQLite / Postgres 双支持 + 轻量迁移）
│     ├─ models.py           数据表（含 transcript）
│     ├─ scheduler.py        自动排片（复习 + 难度 + 时长装填）
│     ├─ security.py         家长令牌 / 设备令牌 / 只读双令牌
│     ├─ level.py            难度分级（Dolch 词表 + 词形还原）
│     ├─ scan.py             ffprobe 增量扫描
│     ├─ web/                家长端 + 学生端 + 自检页（原生 HTML/JS/CSS，无构建）
│     └─ routers/            auth / content / ingest / play / report / plan / student
└─ tools/
   ├─ prep.py                预处理：过滤/改名/响度/抽音轨（增量安全）
   ├─ transcribe.py          ASR 转写（faster-whisper）
   ├─ add_videos.py          **新增视频一条龙：预处理 → 转写 → 导入**
   ├─ import_content.py      导入清单与转写（本地 → 服务端）
   ├─ smoke_test.py          接口冒烟测试（37 项）
   ├─ ui_test.mjs            家长端 UI 测试（34 项，jsdom）
   ├─ ui_test_student.mjs    学生端 UI 测试（43 项，jsdom）
   ├─ pipwrap.py             沙箱下可用的 pip
   ├─ diag_segments.py       转写阈值调试
   └─ compare_transcripts.py 模型对比
```

> `prepared/`（处理后的视频/音轨/转写）与 `视频样本/`（原始素材）体积大且含版权内容，**不入库**，由 `tools/prep.py` 按需重建。

---

## 数据库：默认 SQLite，不用 Postgres

**这个规模（1–3 个孩子）SQLite 完全够用**，而且运维成本低一个数量级。

| | SQLite（默认） | Postgres（可选） |
|---|---|---|
| 部署 | 无额外容器，一个文件 | 多一个容器 + 卷 |
| 备份 | 停容器拷 `kids.db` | `pg_dump` |
| 并发写 | 单写入者（WAL 下读不阻塞） | 多写入者 |
| 本机验证 | ✅ 32 项冒烟测试通过 | ❌ 未实测 |
| 适合规模 | 几个孩子、几千条事件 | 班级级、多租户 |

写入量估算：1 个孩子每天约 10–20 次事件上报，一年几千条——SQLite 处理这个量级毫无压力。

已开的 pragma（`app/db.py`）：`journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`、`foreign_keys=ON`。

**唯一红线**：数据库文件必须放 **NAS 本地盘**，绝不能放 SMB/NFS 共享目录——网络文件系统的锁不可靠，SQLite 会损坏。

要换 Postgres，改 `.env` 里的 `DATABASE_URL` 并取消 compose 文件末尾的注释即可，代码不用动。

---

## 一、在飞牛 fnOS 上部署

### 1. 准备目录

SSH 或文件管理器里建两个目录（路径按你实际卷改，fnOS 一般是 `/vol1/<用户id>/`）：

```
/vol1/1000/docker/kids-english/          # 本仓库 deploy/ 的内容放这里
/vol1/1000/media/english-kids/           # 你的视频资源包根目录
```

视频目录结构建议：**第一层文件夹名 = 系列名**，例如

```
english-kids/
├─ Peppa Pig/
│  ├─ S01E01 Muddy Puddles.mp4
│  └─ S01E02 Mr Dinosaur Is Lost.mp4
└─ Alphablocks/
   └─ S01E01.mp4
```

扫描时第一层目录名自动成为 `series`，文件名成为标题。

### 2. 上传代码并配置

```bash
cd /vol1/1000/docker/kids-english
cp .env.example .env
# 编辑 .env：MEDIA_ROOT、APP_SECRET、PARENT_PASSWORD
# 数据库默认用 SQLite，文件落在 deploy/data/sqlite/kids.db，不需要额外配置
```

`APP_SECRET` 生成一串随机字符：

```bash
openssl rand -hex 32
```

### 3. 启动

```bash
docker compose up -d --build
docker compose ps          # 只有 api 和 web 两个容器
docker compose logs -f api
```

`/api/health` 返回 `{"ok":true,...}` 即成功。

> fnOS 的 Docker 界面也能导入 compose 项目，但首次 `--build` 建议走 SSH，日志更直观。

### 4. 接你的反向代理

现有 Nginx/Traefik 反代到 `http://<NAS内网IP>:18080` 即可。

**必须加 HTTPS**，因为你要用 DDNS/frp 暴露到公网。Nginx 示例：

```nginx
server {
    listen 443 ssl http2;
    server_name kids.example.com;

    ssl_certificate     /path/fullchain.pem;
    ssl_certificate_key /path/privkey.pem;

    # 登录接口限速，防爆破
    location /api/admin/login {
        limit_req zone=login burst=3 nodelay;
        proxy_pass http://192.168.1.10:18080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        proxy_pass http://192.168.1.10:18080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }
}
# http{} 里加：limit_req_zone $binary_remote_addr zone=login:10m rate=6r/m;
```

---

## 二、安全红线（用了 DDNS/frp 就一定要看）

| 项 | 要求 |
|---|---|
| 端口 | frp/路由器**只映射反代的 443**，不要映射 18080、8000、5432 |
| 传输 | 必须 HTTPS；frpc 开 `tls_enable = true`，或走 https 端口 |
| 家长密码 | 至少 16 位随机；`PARENT_PASSWORD` 别和 NAS 登录密码相同 |
| 视频目录 | compose 里已 `:ro` 只读挂载，API 无任何写文件接口 |
| 数据库 | SQLite 文件在 `deploy/data/sqlite/`，不监听任何端口；用 Postgres 时也不映射宿主机端口 |
| 设备令牌 | 只存 sha256；丢了就在家长端 `DELETE /api/devices/{id}` 撤销 |
| 家长令牌 | 默认 30 天有效；`APP_SECRET` 一改即全部失效 |
| 备份 | 停容器后拷 `deploy/data/sqlite/kids.db` 一个文件即可；视频不用备份 |

更稳的做法：外网只走 **Tailscale / WireGuard**，DDNS/frp 只保留内网穿透做备用。

---

## 三、冒烟测试（部署后按顺序跑一遍）

```bash
BASE=http://192.168.1.10:18080

# 1) 健康检查
curl -s $BASE/api/health

# 2) 家长登录
TOKEN=$(curl -s -X POST $BASE/api/admin/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"你的PARENT_PASSWORD"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
echo $TOKEN

# 3) 建一个学生
curl -s -X POST $BASE/api/students -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"小明","daily_target_min":20}'

# 4) 扫描视频目录
curl -s -X POST $BASE/api/scan -H "Authorization: Bearer $TOKEN"

# 5) 生成配对码（student_id 用上一步返回的 id）
curl -s -X POST $BASE/api/devices/pair-code -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"student_id":1,"device_name":"平板"}'

# 6) 模拟设备配对，拿设备令牌
DEV=$(curl -s -X POST $BASE/api/devices/pair -H 'Content-Type: application/json' \
  -d '{"code":"上一步的6位码","device_name":"平板","platform":"android"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# 7) 拉视频列表
curl -s "$BASE/api/videos?limit=5" -H "Authorization: Bearer $DEV"

# 8) 取媒体地址，确认 Range 可用（应返回 206 和 Content-Range）
curl -sI -H "Authorization: Bearer $DEV" -H 'Range: bytes=0-1023' $BASE/api/media/1

# 9) 上报一条播放事件
curl -s -X POST $BASE/api/events -H "Authorization: Bearer $DEV" \
  -H 'Content-Type: application/json' \
  -d '{"events":[{"event_id":"0192f0aa-0000-7000-8000-000000000001","video_id":1,
       "started_at":"2026-10-21T15:00:00+08:00","ended_at":"2026-10-21T15:03:00+08:00",
       "effective_ms":180000,"max_position_ms":180000,"seek_count":0,"avg_rate":1.0,
       "completed":true}]}'

# 10) 家长看今日报表
curl -s "$BASE/api/report/1/today" -H "Authorization: Bearer $TOKEN"
```

第 8 步是关键：**返回 206 说明 nginx 的 X-Accel-Redirect + Range 通了**，ExoPlayer 才能拖进度和续传。

---

## 四、接口一览

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/api/health` | 无 | 健康检查（含数据库类型） |
| POST | `/api/admin/login` | 密码 | 家长登录，返回 30 天令牌 |
| GET/POST | `/api/students` | 家长 | 学生列表 / 新建 |
| PATCH | `/api/students/{id}` | 家长 | 改名 / 改每日目标 |
| POST | `/api/scan` | 家长 | 增量扫描视频目录，**自动接着转音频 + 提取对白** |
| POST | `/api/maintenance/ingest` | 家长 | 手动触发入库流水线（只处理未转写的） |
| GET | `/api/maintenance/status` | 家长 | 入库进度 |
| GET | `/api/maintenance/pending` | 家长 | 还有多少集没处理 |
| POST | `/api/import/manifest` | 家长 | **导入 `tools/prep.py` 的 manifest.json** |
| GET | `/api/series` | 读权限 | 系列列表 |
| GET | `/api/videos` | 读权限 | 视频列表（系列/难度/时长/关键词筛选） |
| GET | `/api/videos/{id}` | 读权限 | 视频详情（含难度指标） |
| PATCH | `/api/videos/{id}` | 家长 | 改标题/级别/标签 |
| POST | `/api/videos/{id}/transcript` | 家长 | **写入转写并自动分级** |
| GET | `/api/videos/{id}/transcript` | 读权限 | 读转写 |
| GET | `/api/media/{id}` | 读权限 | 播放地址（`?track=audio` 取音轨） |
| POST | `/api/devices/pair-code` | 家长 | 生成 6 位配对码 |
| POST | `/api/devices/pair` | 配对码 | 换长期设备令牌 |
| GET/DELETE | `/api/devices` | 家长 | 设备列表 / 撤销 |
| POST | `/api/events` | 设备 | 批量上报播放事件（幂等） |
| GET | `/api/plans/{student}/{day}` | 读权限 | 拉计划（含来源 auto/manual） |
| PUT | `/api/plans/{student}/{day}` | 家长 | 手动下发计划 |
| POST | `/api/plans/{student}/{day}/auto` | 家长 | **自动排片**（复习+难度匹配+时长装填） |
| GET | `/api/dashboard` | 家长 | **家长首页聚合（全部学生 + 本周）** |
| GET | `/api/report/{student}/today` | 家长 | 今日 |
| GET | `/api/report/{student}/week` | 家长 | 近 7 天 |
| GET | `/api/report/{student}/attention` | 家长 | 关注项 |

> 「读权限」= 家长令牌或设备令牌都行；写操作严格区分。家长 Web 端与孩子端 App 共用一套接口。

---

## 五、视频预处理（`tools/prep.py`）

在把视频放进 NAS 之前先跑一遍，解决"内容混入、文件名不可用、音量不一致"三个问题。

```bash
# 先看会怎么处理，不实际转码
python tools/prep.py --src "视频样本" --out prepared --dry-run

# 实际处理
python tools/prep.py --src "视频样本" --out prepared --exclude 闹闹知识屋

# 参数
--exclude       系列名关键字黑名单（默认含 睡前故事/知识屋/中文/儿歌）
--target-lufs   目标响度，默认 -16
--limit N       只处理前 N 个，调试用
```

**产出结构**

```
prepared/
├─ video/<系列>/<系列>_S01E01_<标题>.mp4   统一 -16 LUFS，视频流原样复制（不损失画质）
├─ audio/<系列>/<系列>_S01E01_<标题>.m4a   磨耳朵音轨，64k AAC，体积约视频的 1/36
├─ asr/<系列>/<系列>_S01E01_<标题>.wav     16k 单声道，直接喂 Whisper
├─ manifest.csv                            Excel 可开，含处理前后指标
└─ manifest.json                           机器可读，后续喂给 /api/scan
```

**做四件事**

1. **过滤**：系列名命中黑名单直接跳过（中文睡前故事不算英语学习时长）
2. **改名**：去掉抖音文案/话题标签/emoji/32 位 hash，标题优先取 `《》`，其次取 `｜` 或 `_` 之后的第一段
3. **统一响度**：两遍 `loudnorm` 线性归一，视频流 `-c:v copy` 不重编码
4. **抽两条音轨**：播放用 m4a + ASR 用 wav

**实测结果（9 个样本）**

| 指标 | 处理前 | 处理后 |
|---|---|---|
| 响度 | −7.56 ~ −21.65 LUFS（跨度 14 dB） | −16.03 ~ −16.69 LUFS |
| 真峰值 | 最高 +2.92 dBTP（削波） | 全部 ≤ −1.0 dBTP |
| 音轨体积 | — | 163.2 MB 视频 → 4.6 MB 音轨（1/36） |

**注意**

- 集号是**按文件名排序自动分配**的，不代表官方集序；`manifest.json` 会记住编号，重复运行不会乱
- 极少数集因峰值限制无法线性归一到位（实测 1 集落在 −16.7），属正常，误差 < 1 dB
- 脚本不依赖 Docker 和 NAS，Windows 本地可跑

---

## 六、转写（`tools/transcribe.py`）

把 `prepared/asr/*.wav` 转成带时间戳的文本。结果见 [`docs/transcription-report.md`](docs/transcription-report.md)。

### 一次性准备

```bash
# 依赖装在项目内 .asr-libs，不污染全局 Python
# 注意：本机沙箱下 pip 直接跑会失败（tempfile.mkdtemp 建的目录不可写），必须用 pipwrap
python tools/pipwrap.py install --target .asr-libs faster-whisper
```

模型首次运行自动下载到 `.hf-cache`（走 hf-mirror，直连 huggingface 太慢）。

### 运行

```bash
python tools/transcribe.py --src prepared/asr --out prepared/transcript
python tools/transcribe.py --src prepared/asr --out prepared/transcript --model medium.en
python tools/transcribe.py --src prepared/asr --out prepared/transcript --limit 1   # 试跑
```

**产出**

```
prepared/transcript/
├─ <名称>.json     完整 segments（start/end/text/logprob/压缩比）
├─ <名称>.srt      字幕
├─ transcript.csv  汇总：语音时长、占比、词数、WPM
└─ all.txt         全文，便于通读校对
```

### 参数教训（重要，别照抄通用配置）

| 参数 | 通用做法 | **本项目实测** |
|---|---|---|
| VAD | 开启，阈值 0.5 | **必须关闭**：0.5 → 308 词，0.25 → 497 词，关闭 → 603 词 |
| `no_speech_prob` 过滤 | >0.6 丢弃 | **失效**：正常对白段可达 0.92，阈值放到 0.98 |
| `condition_on_previous_text` | 默认 True | **必须 False**，切断上下文抑制复读幻觉 |
| 幻觉过滤 | 靠 VAD | 只能靠压缩比 + 去重 + 套话黑名单 |

原因：素材音乐/音效连续铺底（静音占比仅 0–8%），VAD 把"音乐+对白"整段判为非语音。

### 辅助工具

- `tools/pipwrap.py` — 绕过沙箱下 pip 的临时目录问题
- `tools/diag_segments.py` — 打印未过滤的原始 segment 及质量指标，用于调阈值
  ```bash
  python tools/diag_segments.py --file "prepared/asr/xxx.wav" --vad off
  ```

---

## 七、网页前端

两个界面，都由 FastAPI 直接托管，**不需要单独的前端服务或构建步骤**：

| 界面 | 地址 | 使用者 |
|---|---|---|
| 家长端 | `/` | 家长（密码登录） |
| 学生端 | `/student.html` | 孩子（6 位配对码，无需账号） |

### 家长端

| 页面 | 内容 |
|---|---|
| **概览** | 每个学生一张卡：今日分钟 / 目标进度条、连续天数、本周 7 天柱状图 |
| **内容库** | 按系列和难度筛选，显示时长、语音占比、WPM、难度徽章；点开看转写全文 |
| **计划** | **自动排片**一键生成当天内容，也可手动勾选覆盖；显示来源（自动/手动） |
| **设置** | 加学生、配对码、撤销设备、扫描目录（自动入库）、学生端入口 |

### 学生端

为"磨耳朵"专门设计的极简界面：

| 功能 | 说明 |
|---|---|
| **配对** | 输入家长给的 6 位码，换长期设备令牌存本地 |
| **今日任务** | 进度环（已听分钟 / 目标）、完成集数、任务列表 |
| **听 / 看 双模式** | 顶部一键切换，**切换时保持播放进度**；模式偏好本地记忆 |
| **听模式** | 只播音轨（体积 1/36）、熄屏继续播、无画面无字幕 |
| **看模式** | 播视频（有画面）、熄屏会暂停、可叠加遮字幕条 |
| **硬字幕遮挡** | 仅看模式可用，默认关闭；开启后盖住底部 20%（可调 8–40%） |
| **播放控制** | 连播、单集循环、0.8/1.0/1.2 倍速、锁屏媒体控制 |
| **音量调节** | 滑块 0–100%，本地记忆；满音量提示保护听力 |
| **人声增强** | 可选开关：Web Audio 压缩器压低音乐、突出对白（可能影响熄屏播放） |
| **有效时长** | 每 1 秒采样，只在"真在播 + 没拖动 + 倍速 ≤1.25"时累加，两种模式都计 |
| **离线补传** | 事件先进 localStorage 队列，联网后自动补传 |

**关键实现点**：

- `<audio>`/`<video>` 不能自定义请求头，所以媒体地址用 `?token=` 传设备令牌（局域网自用可接受；公网部署建议给媒体地址加短期签名）
- **音频在手机浏览器里可以熄屏继续播放**（视频不行），这是"听模式"只播音轨的根本原因
- 切换模式时记录 `currentTime`，换源后回填，所以听一半切到看不会从头开始
- 进度条只读（防拖动），拖动会记 `seek_count` 并让该段不计入有效时长
- 遮罩只在看模式有意义——听模式没有画面，也就没有字幕可读
- **音量统一在服务端做**（入库时归一到 −16 LUFS），播放端只调音量，不做二次归一化：EBU R128 需要整段测量，浏览器里算不出来
- 人声增强用 `DynamicsCompressorNode`（阈值 −24 dB、4:1、补偿增益 1.35）。它会让音频走 Web Audio 图，**部分浏览器在熄屏后会挂起 AudioContext**，所以默认关闭

### 媒体怎么发：`SERVE_MEDIA`

| 值 | 行为 | 用在哪 |
|---|---|---|
| `accel`（默认） | API 只回 `X-Accel-Redirect`，nginx 直出文件 | NAS 生产环境 |
| `direct` | FastAPI 自己按 Range 流式返回 | 本地开发（没有 nginx） |

**本地开发一定要设 `SERVE_MEDIA=direct`**，否则浏览器收到空响应体，播放器一片安静——接口返回 200 但没有任何字节，现象很像"坏了"。

技术选型：原生 HTML/CSS/JS，约 1200 行，无框架无构建。界面只有几个页面、使用者是家长和孩子，上 React 的构建链维护成本大于收益。

### 自动排片（`server/app/scheduler.py`）

**家长不需要每天挑片。** 当天没有计划时，学生端一打开就自动生成一份并落库。

排片规则（按优先级）：

| 规则 | 说明 |
|---|---|
| **复习优先** | 听过的内容按 1/3/7/15/30 天间隔到期复习，到期的排在最前（占 40% 预算） |
| **难度匹配** | 新内容挑"当前级别 ±1"，级别由最近 5 次完成内容的中位数推断 |
| **时长装填** | 按每日目标装填，允许超 20%，单日最多 8 集 |
| **多样性** | 同系列连续不超过 2 集（软约束：排不满会放宽） |
| **补足重复** | 新内容不够时，用"最久没听"的旧内容补满——磨耳朵本来就靠重复 |
| **不重复当天** | 当天已完成的集不再排 |

**优先级链**：复习到期 → 新内容 → 旧内容重复。三者都按预算装填。

家长手动保存会把 `source` 改回 `manual`，之后自动排片不会再覆盖当天的计划。

> 内容不够时接口会返回 `message`："内容不足：库里只够排 X 分钟（目标 Y 分钟），建议补充视频或调低每日目标"——库小的时候早点知道比排个半空计划好。

---

## 八、本地开发（不用 Docker）

后端支持 SQLite，可以在没有 Postgres 的机器上完整跑通。

```bash
# 1. 依赖装到项目内（沙箱下必须用 pipwrap，见第六节）
python tools/pipwrap.py install --target .server-libs \
    uvicorn sqlalchemy aiosqlite pydantic-settings itsdangerous simplemma greenlet

# 2. 启动（本地没有 nginx，必须用 direct 模式，否则播放器拿不到数据）
export PYTHONPATH=$PWD/.server-libs
export DATABASE_URL="sqlite+aiosqlite:///$PWD/dev.db"
export MEDIA_ROOT="$PWD/prepared"
export SERVE_MEDIA=direct
export APP_SECRET=dev-secret
export PARENT_PASSWORD=dev
cd server && python -m uvicorn app.main:app --port 8010

# 3. 灌样本数据（清单 + 转写 + 自动分级）
python tools/import_content.py --base http://127.0.0.1:8010 --password dev

# 4. 冒烟测试（32 项）
python tools/smoke_test.py --base http://127.0.0.1:8010 --password dev

# 5. 前端 UI 测试（家长端 30 项 + 学生端 24 项，用 jsdom 真跑前端代码）
npm install --prefix .ui-test jsdom@24     # 仅第一次
node tools/ui_test.mjs --base http://127.0.0.1:8010 --password dev
node tools/ui_test_student.mjs --base http://127.0.0.1:8010 --password dev
node tools/ui_test.mjs --base http://127.0.0.1:8010 --password dev --dump   # 打印各页面渲染文本
```

`smoke_test.py` 测接口层，`ui_test.mjs` / `ui_test_student.mjs` 测界面层（模拟点击、断言 DOM）。两者合起来覆盖了「数据对不对」和「界面有没有正确显示」。详见 [`docs/frontend-testing.md`](docs/frontend-testing.md)。

Windows 下环境变量用 `$env:NAME="..."` 写法。

**一个只有真跑才能发现的坑**：SQLite 读回的 `DateTime` **不带时区**，和 `datetime.now(UTC)` 比较会抛 `TypeError`。已在 `routers/auth.py` 用 `_as_utc()` 统一处理。写新代码时凡是拿数据库时间做比较，都要走这个函数。

---

## 九、已知限制（M0/M1 阶段）

- 表结构用 `create_all` 建，**没有迁移**；改字段需要手工处理，二期上 Alembic
- 缩略图/按需转码未实现（worker 位待补）
- 扫描是同步阻塞的：目录很大时请求会等较久，二期改后台任务 + 进度查询
- 未做登录失败计数与锁定（依赖反代限速）
- 前端没有自动化测试，只有 `smoke_test.py` 覆盖接口层
- **只在 SQLite 上验证过**；Postgres 路径（`asyncpg`、`timestamptz`）尚未实测，首次部署到 NAS 时要重点看第三节冒烟测试
- 分级用的词表只有 Dolch 220 + 补充常用词，没有 Oxford 3000；把词表丢进 `server/app/wordlists/extra.txt`（一行一个词）即可增强
