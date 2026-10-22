# 部署包（飞牛 fnOS / 任意 Linux）

**单容器、零配置**：改一行媒体路径就能跑，不需要 `.env`，不需要在 NAS 上构建。
镜像从 Docker Hub 拉取：`brian9909/moer:latest`

```
kids-english/
├─ docker-compose.yml    ← 就这一个文件（加上本说明）
└─ README.md
```

## 为什么只有一个容器

`api` 容器同时负责后端、前端页面和媒体分发，媒体文件由它按 Range 直接发送。
实测吞吐：

| 场景 | 实测 |
|---|---|
| 42 MB 视频整段 | 165 MB/s |
| 1.5 MB 音轨整段 | 81 MB/s |
| Range 请求 1 MB 片段 | 10 ms |

1080p 视频流只需要 0.3 MB/s，余量几十倍。**1–3 个孩子完全够用**，不需要额外的 nginx。

---

## 三步启动

```bash
# 1. 建目录，把本目录内容拷进去
mkdir -p /vol1/1000/docker/kids-english
cd /vol1/1000/docker/kids-english

# 2. 改 docker-compose.yml 里的媒体路径（唯一要改的地方）
#    搜索 /vol1/1000/media/english-kids，改成你自己的视频目录
vi docker-compose.yml

# 3. 启动
docker compose up -d
docker compose ps
```

浏览器打开 `http://<NAS内网IP>:18080/`，默认密码 `kids1234`。

想改密码：改 compose 里 `PARENT_PASSWORD` 那一行，`docker compose up -d` 生效。

---

## 只暴露一个 HTTP 端口

```yaml
ports:
  - "18080:8000"
```

HTTPS 由你自己的反向代理做，compose 不管证书。反代指向 `http://<NAS内网IP>:18080` 即可。

**反代注意**：媒体是流式传输，建议在反代里对 `/api/media/` 关掉缓冲：

```nginx
location /api/media/ {
    proxy_pass http://192.168.1.10:18080;
    proxy_buffering off;
    proxy_max_temp_file_size 0;
    proxy_read_timeout 300s;
}
```

---

## 目录里会多出什么

启动后在 compose 同级创建 `data/`：

```
data/
├─ kids.db            ← 数据库（备份就是拷这个文件）
├─ .app_secret        ← 登录令牌密钥，自动生成，别删
├─ cache/             ← 音轨、归一化视频、ASR 输入（可随时删，会自动重建）
└─ models/            ← 转写模型（约 480MB，删了会重新下载）
```

**`data/` 必须放 NAS 本地盘，不要放 SMB/NFS 共享**——网络文件系统的锁不可靠，
SQLite 会静默损坏。

---

## 媒体文件放哪

compose 里这一行决定视频目录（容器内固定挂到 `/media`，只读）：

```yaml
- /vol1/1000/media/english-kids:/media:ro
```

目录结构（**第一层文件夹名 = 系列名**）：

```
english-kids/
├─ Peppa Pig/
│  ├─ Peppa Pig_S01E01_Muddy Puddles.mp4
│  └─ Peppa Pig_S01E02_Mr Dinosaur.mp4
└─ Alphablocks/
   └─ Alphablocks_S01E01.mp4
```

> 容器内固定挂到 `/media`（只读），程序改不了你的原始文件。

---

## 入库：拷进去点一下就行

1. 把视频拷进媒体目录（文件名你自己整理，程序不改名）
2. 家长端 → **设置 → 内容维护 → 扫描视频目录**

系统会自动接着做：统一音量（−16 LUFS）→ 抽音轨 → 提取对白 → 难度分级。
首次会下载转写模型（约 480MB）。

---

## 更新

```bash
docker compose pull && docker compose up -d
```

数据库结构变化会自动补列，不用手工处理。

---

## 常用命令

| 操作 | 命令 |
|---|---|
| 看应用日志 | `docker compose logs -f api` |
| 重启 | `docker compose restart` |
| 停止 | `docker compose down` |
| 备份数据库 | `docker compose stop && cp data/kids.db ~/kids-backup.db` |
| 重跑全部入库 | 删掉 `data/cache` 后，在设置页点「入库处理」 |

---

## 想调的参数（都在 compose 里）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PARENT_PASSWORD` | `kids1234` | 家长端密码，**建议改** |
| `AUTO_INGEST` | `1` | 扫描后自动跑流水线；`0` 只入库元数据 |
| `NORMALIZE_AUDIO` | `1` | 统一音量到 −16 LUFS |
| `NORMALIZE_VIDEO` | `0` | 看模式也归一化（额外占视频空间） |
| `TARGET_LUFS` | `-16` | 目标响度 |
| `WHISPER_MODEL` | `small.en` | 或 `base.en`（更快）/ `medium.en`（更准） |
| `WHISPER_THREADS` | `3` | 留一核给 NAS |
| `TZ` | `Asia/Shanghai` | 统计切天用的时区 |

---

## 从源码构建（可选）

本目录默认拉 Docker Hub 镜像。想自己构建：

1. clone 整个仓库
2. 进到仓库的 `deploy/` 目录
3. 取消 `docker-compose.yml` 里 `build:` 段的注释（context 指向 `../server`）
4. `docker compose up -d --build`

---

完整部署说明（反代、HTTPS、安全红线、排错）见仓库里的
[`docs/deploy-fnos.md`](https://github.com/Brian099/English_learning/blob/main/docs/deploy-fnos.md)。
