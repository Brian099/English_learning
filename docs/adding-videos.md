# 新增视频怎么操作

**文件名你自己整理好，程序不改名。** 拷进去 → 点一下扫描 → 系统自动转音频、提取对白、算难度分级。

---

## 标准流程（三步）

### 1. 把文件拷进媒体目录

拷到 `docker-compose.yml` 里映射的那个目录（默认 `/vol1/1000/media/english-kids`）。
**第一层文件夹名 = 系列名**：

```
/vol1/1000/media/english-kids/
├─ Peppa Pig/
│  ├─ Peppa Pig_S01E01_Muddy Puddles.mp4
│  └─ Peppa Pig_S01E02_Mr Dinosaur.mp4
└─ Alphablocks/
   └─ Alphablocks_S01E01_Alphablocks.mp4
```

直接放在根目录会归到「未分类」系列。

命名建议（不是强制，但排片和显示都靠它）：

```
<系列>_S<季>E<集>_<标题>.mp4
```

### 2. 扫描

家长端 → **设置 → 内容维护 → 扫描视频目录**

### 3. 等它自己跑完

扫描完**自动开始**，设置页会显示进度：

```
统一音量 / 转音频 / 提取对白 3/12
```

每集依次做四件事：

| 步骤 | 说明 |
|---|---|
| **统一音量** | EBU R128 两遍归一，目标 −16 LUFS。不同来源素材响度能差 14 dB，统一后孩子不用来回调音量 |
| **抽音轨** | 96k AAC（约视频 1/6），听模式用 |
| **提取对白** | faster-whisper 转写 + 时间戳 |
| **难度分级** | 词表对照算生词率、语速，切 L1–L5 |

跑完每集会得到：

| 产出 | 用途 |
|---|---|
| 归一化音轨（−16 LUFS） | 听模式播放，音量一致 |
| 转写全文 + 时间戳 | 内容库查看、全文检索 |
| 难度分级 L1–L5 + 生词率 + 语速 | 自动排片按级别选内容 |
| 语音时长 | 完成判定用（听满 90% 语音算完成） |

**首次入库会下载转写模型（约 480MB）**，之后从 `/models` 卷读，不再下载。

> 归一化结果写在容器的 `/cache` 里，**不会改动你的原始文件**（媒体目录是只读挂载）。
> 听模式播的是归一化音轨；看模式默认仍用原视频，想让看模式也统一音量就设
> `NORMALIZE_VIDEO=1`（会额外占一份视频空间）。

---

## 处理时间参考

| 环节 | 单集耗时（N100） |
|---|---|
| 音量测量（第一遍） | 约 2–4 秒 |
| 抽音轨（第二遍归一化） | 约 3–6 秒 |
| 抽 ASR 输入 | 约 2–5 秒 |
| 转写（small.en，int8，3 线程） | 约 3–4 倍实时 → 3 分钟的集约 50 秒 |
| **合计** | **约 1–1.5 分钟 / 集** |

100 集大约 1.5–2 小时，可以挂着跑，不影响播放（后台任务）。

想换更准的模型：compose 里把 `WHISPER_MODEL` 改成 `medium.en`（准一些，慢约 3 倍）。
N100 上 medium 约 1 倍实时，100 集要 5 小时以上。

---

## 增量行为

| 情况 | 结果 |
|---|---|
| 重复扫描 | 文件没变就跳过，秒级完成 |
| 新增几集 | 只处理新增的 |
| 想重新处理某一集 | 删掉它的转写记录，再点一次入库处理 |
| 内容重复（换名复制） | 数据库里会出现两条，需要手动删 |

扫描按 **相对路径** 认记录，所以：

- 移动/改名文件 = 新的一条记录（旧的还在库里）
- 覆盖同名文件 = 更新那条记录（时长、哈希会刷新）

---

## 命令行等价操作

```bash
BASE=http://<NAS>:18080
TOKEN=$(curl -s -X POST $BASE/api/admin/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"你的密码"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

# 扫描（会自动触发入库）
curl -s -X POST $BASE/api/scan -H "Authorization: Bearer $TOKEN"

# 手动触发入库（只处理还没转写的）
curl -s -X POST $BASE/api/maintenance/ingest -H "Authorization: Bearer $TOKEN"

# 查进度
curl -s $BASE/api/maintenance/status -H "Authorization: Bearer $TOKEN"
```

---

## 想先在电脑上处理（可选）

如果你有一大批素材、想先过滤和整理，可以在 Windows 上跑预处理流水线，
它会在**拷贝之前**完成过滤非英语内容、统一响度、抽音轨、转写分级：

```bash
python tools/add_videos.py --src "D:\新素材" \
    --server http://192.168.1.10:18080 --password 你的密码
```

适合的场景：

- 素材里混着中文内容，需要按系列过滤
- 音量忽大忽小，需要统一到 −16 LUFS
- 想先在本机跑完再一次性传，不占 NAS 的 CPU

**注意**：`prep.py` 会清洗文件名（去抖音文案、emoji、hash）。你既然自己整理好了名字，
可以只用它的响度统一和转写能力，或者干脆跳过这一步、走上面的标准流程。

---

## 常见问题

### 扫描后没反应？

看设置页的进度文字，或查日志：

```bash
docker compose logs -f api | grep -i ingest
```

### 转写失败怎么办？

常见原因：

| 现象 | 原因 |
|---|---|
| `容器里没有 ffmpeg` | 镜像不对，确认用的是 `brian9909/moer:latest` |
| `转写失败: ...` | 模型下载失败（网络），检查 `HF_ENDPOINT` 是否可达 |
| 某几集一直失败 | 音频异常，单独看日志里的 `last_error` |

### 能只转音频不转写吗？

compose 里把 `AUTO_INGEST` 改成 `0`，扫描就只入库元数据。
然后手动调 `/api/maintenance/extract-audio` 只抽音轨。

### 模型太大，想省空间？

`small.en` 约 480MB。想更小可以用 `base.en`（约 150MB，准确率略低）：

```ini
WHISPER_MODEL=base.en
```
