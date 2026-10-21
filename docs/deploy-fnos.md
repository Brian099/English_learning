# 在飞牛 fnOS 上部署

整套服务只有两个容器：`api`（FastAPI + 前端）和 `web`（nginx）。
数据库默认用 SQLite（一个文件），不需要额外的数据库容器。

镜像由 GitHub Actions 自动构建并推送到 Docker Hub：**`brian9909/moer:latest`**。
NAS 上**不需要装 Python、不需要编译**，只要能跑 Docker。

---

## 一、目录规划

fnOS 的存储卷一般是 `/vol1/<用户ID>/`，先建两个目录：

```
/vol1/1000/docker/kids-english/      ← 部署文件（compose、nginx 配置、数据库）
/vol1/1000/media/english-kids/       ← 视频与音轨
```

媒体目录结构（**由 `tools/prep.py` 生成，保持原样即可**）：

```
english-kids/
├─ video/<系列>/<系列>_S01E01_<标题>.mp4
└─ audio/<系列>/<系列>_S01E01_<标题>.m4a
```

> `prepared/manifest.json` 里的路径是相对 `prepared/` 的，所以把 `prepared/` 里的
> `video/` 和 `audio/` 两个子目录整体拷到 `english-kids/` 下即可。

---

## 二、部署服务

### 1. 上传部署文件

把仓库里的 `deploy/` 目录内容传到 `/vol1/1000/docker/kids-english/`，结构如下：

```
kids-english/
├─ docker-compose.yml
├─ .env
└─ nginx/default.conf
```

### 2. 配置 `.env`

```bash
cd /vol1/1000/docker/kids-english
cp .env.example .env
```

编辑 `.env`，至少要改这四项：

```ini
MEDIA_ROOT=/vol1/1000/media/english-kids
APP_SECRET=<openssl rand -hex 32 生成的随机串>
PARENT_PASSWORD=<你自己的家长密码，建议 16 位以上>
HTTP_PORT=18080
```

`APP_SECRET` 生成方式：

```bash
openssl rand -hex 32
```

### 3. 启动

```bash
docker compose pull
docker compose up -d
docker compose ps
```

看到 `api` 和 `web` 两个容器都是 `Up` 即可。

镜像地址是 `brian9909/moer:latest`，**公开可拉，不需要登录**。想单独验证一下：

```bash
docker pull brian9909/moer:latest
```

### 4. 验证

```bash
curl -s http://127.0.0.1:18080/api/health
```

应返回：

```json
{"ok": true, "version": "0.3.0", "media_root": "/media", "tz": "Asia/Shanghai", "database": "sqlite+aiosqlite"}
```

浏览器打开 `http://<NAS内网IP>:18080/`，用 `.env` 里的 `PARENT_PASSWORD` 登录。

> fnOS 的 Docker 面板也支持导入 Compose 项目（「Docker → 项目 → 新建」），
> 效果一样，但首次排查问题建议走 SSH，日志更直观。

---

## 三、导入内容

视频文件放好之后，还要把**清单和转写**导进数据库。这一步在你的 Windows 电脑上跑
（因为需要 `prepared/manifest.json` 和 `prepared/transcript/`）：

```bash
python tools/import_content.py \
  --base http://192.168.1.10:18080 \
  --password 你的家长密码 \
  --student 小明
```

它会做三件事：

1. 登录并确保有学生
2. 导入 `manifest.json`（系列、标题、时长、音轨路径）
3. 逐集上传转写，**同时自动算出难度分级 L1–L5**

输出示例：

```
✓ 登录成功：http://192.168.1.10:18080
✓ 已有学生：小明
✓ 导入清单：新增 9，更新 0，跳过 0
  小蚂蚁大奇迹: L3 · 生词率 26% · 唯一词 50 · 60 WPM
  ...
✓ 完成：9 集转写入库
```

> 只想要元数据不导入转写：加 `--skip-transcript`。

---

## 四、给孩子用

1. 家长端「设置」→ 点学生旁边的「配对码」，拿到 6 位数字
2. 平板/手机打开 `http://<NAS内网IP>:18080/student.html`
3. 输入配对码 → 进入今日任务

**今日任务不用手动排**：当天没有计划时，学生端一打开就自动生成
（复习到期 + 难度匹配 + 时长装填），家长在「计划」页可以随时覆盖。

---

## 五、接反向代理与外网

现有 Nginx / Traefik 反代到 `http://<NAS内网IP>:18080` 即可。

**必须加 HTTPS**，如果要用 DDNS/frp 暴露到公网：

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

### 安全红线

| 项 | 要求 |
|---|---|
| 端口 | frp/路由器**只映射反代的 443**，不要映射 18080、8000、5432 |
| 传输 | 必须 HTTPS；frpc 开 `tls_enable = true` |
| 家长密码 | 16 位以上随机；别和 NAS 登录密码相同 |
| 视频目录 | compose 里已 `:ro` 只读挂载 |
| 数据库 | SQLite 文件在 `deploy/data/sqlite/`，不监听任何端口 |
| 备份 | 停容器后拷 `deploy/data/sqlite/kids.db` 一个文件即可 |

更稳的做法：外网只走 **Tailscale / WireGuard**，DDNS/frp 只做备用。

---

## 六、更新

镜像由 GitHub 自动构建，更新就是两步：

```bash
cd /vol1/1000/docker/kids-english
docker compose pull
docker compose up -d
```

数据库结构有变化时，`app/db.py` 里的轻量迁移会自动补列，不用手工处理。

---

## 七、排错

| 现象 | 原因与处理 |
|---|---|
| 页面能打开但播放失败（错误码 4） | ① 设备令牌过期 → 学生端点「重新配对」；② `.env` 里 `SERVE_MEDIA` 被设成了 `direct`（生产应留 `accel`） |
| `docker compose up` 报 `APP_SECRET` 未设置 | `.env` 没写或没生效，检查文件是否在 `docker-compose.yml` 同目录 |
| 媒体 404 | `MEDIA_ROOT` 路径不对，或视频没拷到 `video/` 子目录下 |
| 日志看什么 | `docker compose logs -f api`（应用日志）、`docker compose logs -f web`（nginx） |
| 数据库损坏 | 确认 `deploy/data/` 在 NAS **本地盘**，不要放 SMB/NFS 共享 |
| 想换 Postgres | 取消 compose 末尾注释，改 `.env` 的 `DATABASE_URL`，代码不用动 |

---

## 八、镜像构建（维护者）

镜像由 `.github/workflows/docker.yml` 在推送 `main` 时自动构建并推送到 Docker Hub。

**首次需要配置两个仓库 Secrets**（Settings → Secrets and variables → Actions → New repository secret）：

| 名称 | 值 |
|---|---|
| `DOCKERHUB_USERNAME` | `brian9909` |
| `DOCKERHUB_TOKEN` | Docker Hub 的 **Access Token**（Account Settings → Security → New Access Token，权限选 Read & Write） |

> 不要用 Docker Hub 的登录密码，用 Access Token。
> 没配置时工作流会自动跳过构建（黄色警告，不报红）。

配置好后手动触发一次：Actions → build-image → Run workflow。

每次推送到 `main`（且 `server/` 有变化）会自动重新构建，标签为 `latest` 和 `sha-xxxxxxx`。

---

## 九、不用 Docker Hub 镜像的替代方案

如果不想依赖 GHCR，也可以直接在 NAS 上构建：

```bash
cd /vol1/1000/docker
git clone https://github.com/Brian099/English_learning.git
cd English_learning/deploy
cp .env.example .env && vi .env
docker compose up -d --build
```

需要 NAS 上有 git 和构建环境（Docker 自带即可），首次构建约 1–2 分钟。
