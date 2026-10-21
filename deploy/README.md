# 部署包（飞牛 fnOS / 任意 Linux）

**这个目录是自包含的**——整个拷到 NAS 上就能跑，不需要源码、不需要在 NAS 上构建。
镜像从 Docker Hub 拉取：`brian9909/moer:latest`

```
kids-english/
├─ docker-compose.yml
├─ .env.example          ← 复制成 .env 后修改
├─ README.md             ← 本文件
└─ nginx/
   └─ default.conf       ← nginx 配置（视频直出 + 反代 API）
```

---

## 四步启动

```bash
# 1. 建目录并放好文件
mkdir -p /vol1/1000/docker/kids-english
cd /vol1/1000/docker/kids-english
# 把本目录内容拷进来

# 2. 配置
cp .env.example .env
vi .env      # 至少改 MEDIA_ROOT / APP_SECRET / PARENT_PASSWORD

# 3. 启动
docker compose pull
docker compose up -d

# 4. 验证
curl -s http://127.0.0.1:18080/api/health
```

看到 `{"ok":true,...}` 就成了，浏览器打开 `http://<NAS内网IP>:18080/`。

`.env` 里的 `APP_SECRET` 用下面命令生成：

```bash
openssl rand -hex 32
```

---

## 目录里会多出什么

启动后会在当前目录创建：

```
data/sqlite/kids.db      ← 数据库（备份就是拷这个文件）
data/cache/              ← 缓存（可随时删）
```

**`data/` 必须放 NAS 本地盘，不要放 SMB/NFS 共享**——网络文件系统的锁不可靠，
SQLite 会静默损坏。

---

## 媒体文件放哪

`.env` 里的 `MEDIA_ROOT` 指向视频目录，容器内只读挂载。结构必须是：

```
english-kids/
├─ video/<系列>/<系列>_S01E01_<标题>.mp4
└─ audio/<系列>/<系列>_S01E01_<标题>.m4a
```

这个结构由仓库里的 `tools/prep.py` 生成：把 `prepared/video` 和 `prepared/audio`
两个子目录整体拷到 `MEDIA_ROOT` 下即可。

---

## 导入内容

视频文件到位后，在你自己的电脑上（有 `prepared/manifest.json` 和转写结果）跑：

```bash
python tools/import_content.py \
  --base http://<NAS内网IP>:18080 \
  --password <你的家长密码>
```

它会导入清单并逐集上传转写，同时自动算出难度分级 L1–L5。

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
| 看 nginx 日志 | `docker compose logs -f web` |
| 重启 | `docker compose restart` |
| 停止 | `docker compose down` |
| 备份数据库 | `docker compose stop && cp data/sqlite/kids.db ~/kids-backup.db` |

---

## 从源码构建（可选）

本目录默认拉 Docker Hub 镜像。想自己构建：

1. 把整个仓库 clone 下来
2. 进到仓库的 `deploy/` 目录
3. 取消 `docker-compose.yml` 里 `build:` 段的注释（context 指向 `../server`）
4. `docker compose up -d --build`

---

完整部署说明（反代、HTTPS、安全红线、排错）见仓库里的
[`docs/deploy-fnos.md`](https://github.com/Brian099/English_learning/blob/main/docs/deploy-fnos.md)。
