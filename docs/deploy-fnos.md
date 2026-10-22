# 在飞牛 fnOS 上部署

整套服务只有两个容器：`api`（FastAPI + 前端）和 `web`（nginx）。
数据库默认用 SQLite（一个文件），不需要额外的数据库容器。

**零配置**：改一行媒体路径就能跑，不需要 `.env`。

镜像由 GitHub Actions 自动构建并推送到 Docker Hub：**`brian9909/moer:latest`**。
NAS 上**不需要装 Python、不需要编译**，只要能跑 Docker。

---

## 一、目录规划

fnOS 的存储卷一般是 `/vol1/<用户ID>/`，先建两个目录：

```
/vol1/1000/docker/kids-english/      ← 部署文件（compose、nginx 配置、数据）
/vol1/1000/media/english-kids/       ← 视频
```

媒体目录结构（**第一层文件夹名 = 系列名**）：

```
english-kids/
├─ Peppa Pig/
│  ├─ Peppa Pig_S01E01_Muddy Puddles.mp4
│  └─ Peppa Pig_S01E02_Mr Dinosaur.mp4
└─ Alphablocks/
   └─ Alphablocks_S01E01.mp4
```

---

## 二、部署服务

### 1. 上传部署文件

把仓库 `deploy/` 目录的内容传到 `/vol1/1000/docker/kids-english/`：

```
kids-english/
├─ docker-compose.yml
├─ README.md
└─ nginx/default.conf
```

### 2. 改媒体路径（唯一要改的地方）

`docker-compose.yml` 里有两处相同的路径，**改成你自己的视频目录**：

```yaml
volumes:
  - /vol1/1000/media/english-kids:/media:ro     # ← api 服务
  ...
  - /vol1/1000/media/english-kids:/media:ro     # ← web 服务（必须一致）
```

> 容器内路径固定是 `/media`，只读挂载——程序改不了你的原始文件。

### 3. 启动

```bash
cd /vol1/1000/docker/kids-english
docker compose pull
docker compose up -d
docker compose ps
```

看到 `api` 和 `web` 都是 `Up` 即可。

### 4. 验证

```bash
curl -s http://127.0.0.1:18080/api/health
```

应返回：

```json
{"ok": true, "version": "0.3.0", "media_root": "/media", "tz": "Asia/Shanghai", "database": "sqlite+aiosqlite"}
```

浏览器打开 `http://<NAS内网IP>:18080/`，默认密码 **`kids1234`**。

想改密码：改 compose 里 `PARENT_PASSWORD` 那一行，再 `docker compose up -d`。

> fnOS 的 Docker 面板也支持导入 Compose 项目（「Docker → 项目 → 新建」），
> 效果一样，但首次排查问题建议走 SSH，日志更直观。

---

## 三、内容入库

视频拷进媒体目录后，**家长端 → 设置 → 内容维护 → 扫描视频目录**。

系统自动接着做：

| 步骤 | 说明 |
|---|---|
| 统一音量 | EBU R128 两遍归一，目标 −16 LUFS |
| 抽音轨 | 96k AAC（约视频 1/6），听模式用 |
| 提取对白 | faster-whisper 转写 + 时间戳 |
| 难度分级 | 生词率、语速 → L1–L5 |

**首次入库会下载转写模型（约 480MB）**，缓存在 `data/models/`，重建容器不重下。

设置页会显示进度：`统一音量 / 转音频 / 提取对白 3/12`。
100 集约 1.5–2 小时（N100），后台跑，不影响播放。

细节见 [`adding-videos.md`](adding-videos.md)。

---

## 四、给孩子用

1. 家长端「设置」→ 点学生旁边的「配对码」，拿到 6 位数字
2. 平板/手机打开 `http://<NAS内网IP>:18080/student.html`
3. 输入配对码 → 进入今日任务

**今日任务不用手动排**：当天没有计划时，学生端一打开就自动生成
（复习到期 + 难度匹配 + 时长装填），家长在「计划」页可以随时覆盖。

---

## 五、接反向代理与外网

compose 只暴露一个 HTTP 端口 `18080`，**HTTPS 由你自己的反代做**：

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
| 端口 | frp/路由器**只映射反代的 443**，不要映射 18080 |
| 家长密码 | 改掉默认的 `kids1234` |
| 视频目录 | compose 里已 `:ro` 只读挂载 |
| 数据库 | `data/kids.db`，不监听任何端口 |
| 备份 | 停容器后拷 `data/kids.db` 一个文件即可 |
| 密钥 | `data/.app_secret` 自动生成，别删（删了所有登录失效） |

更稳的做法：外网只走 **Tailscale / WireGuard**，DDNS/frp 只做备用。

---

## 六、更新

```bash
docker compose pull && docker compose up -d
```

数据库结构变化会自动补列，不用手工处理。

---

## 七、排错

| 现象 | 原因与处理 |
|---|---|
| 页面能打开但播放失败（错误码 4） | ① 设备令牌过期 → 学生端点「重新配对」；② `SERVE_MEDIA` 被改成了 `direct`（生产应留 `accel`） |
| 媒体 404 | compose 里两处媒体路径不一致，或视频没放对目录 |
| 入库一直卡住 | `docker compose logs -f api`，多半是模型下载失败，检查 `HF_ENDPOINT` |
| `data/` 权限错误 | 确认目录属于运行 Docker 的用户 |
| 数据库损坏 | 确认 `data/` 在 NAS **本地盘**，不要放 SMB/NFS 共享 |
| 想换 Postgres | 取消 compose 末尾注释，加 `DATABASE_URL` 环境变量，代码不用动 |

---

## 八、镜像构建（维护者）

镜像由 `.github/workflows/docker.yml` 在推送 `main` 时自动构建并推送到 Docker Hub。

**首次需要配置两个仓库 Secrets**（Settings → Secrets and variables → Actions）：

| 名称 | 值 |
|---|---|
| `DOCKERHUB_USERNAME` | `brian9909` |
| `DOCKERHUB_TOKEN` | Docker Hub 的 **Access Token**（Read & Write 权限） |

没配置时工作流会自动跳过构建（黄色警告，不报红）。

---

## 九、从源码构建（可选）

```bash
cd /vol1/1000/docker
git clone https://github.com/Brian099/English_learning.git
cd English_learning/deploy
# 取消 docker-compose.yml 里 build: 段的注释
docker compose up -d --build
```

需要 NAS 上有 git，首次构建约 1–2 分钟。
