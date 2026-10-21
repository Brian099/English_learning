# 新增视频怎么操作

三种场景，都支持增量：**已处理过的不会重跑，集号不会撞，旧记录不会丢。**

---

## 一条命令（推荐）

```bash
python tools/add_videos.py --src "D:\新素材" \
    --server http://192.168.1.10:18080 --password 你的家长密码
```

它会依次做三件事：

| 步骤 | 说明 |
|---|---|
| 1. 预处理 | 过滤非英语系列 → 清洗抖音文件名 → 统一响度 → 抽音轨（视频流不重编码） |
| 2. 转写 | faster-whisper 转文字，**跳过已转写的** |
| 3. 导入 | 清单 + 转写推到服务端，自动算难度分级 |

跑完会提示需要拷哪些文件到 NAS。

时间参考：每个视频约 **30–60 秒**（两遍响度分析最耗时），几百集挂机一晚。

---

## 分步做（想控制每一步）

### 1. 预处理

```bash
python tools/prep.py --src "D:\新素材" --out prepared
```

增量规则：

- **同名且内容没变** → 跳过（不重新转码）
- **内容重复**（换了文件名但文件一样）→ 跳过
- **集号接着排**：`大嘴狐` 已有 E01–E03，新增的自动是 E04
- **旧记录保留**：`--src` 只指向新素材目录也不会把之前的记录冲掉

### 2. 拷文件到 NAS

只需拷新增的，用 rsync 增量最省事：

```bash
rsync -av --ignore-existing prepared/video/ user@nas:/vol1/1000/media/english-kids/video/
rsync -av --ignore-existing prepared/audio/ user@nas:/vol1/1000/media/english-kids/audio/
```

没装 rsync 就在文件管理器里按修改时间排序，只拖新出现的目录。

### 3. 转写

```bash
python tools/transcribe.py --src prepared/asr --out prepared/transcript --skip-existing
```

`--skip-existing` 只处理还没有 JSON 的音频，已经转过的直接跳过。

### 4. 导入

```bash
python tools/import_content.py --base http://192.168.1.10:18080 --password 你的密码
```

导入是按路径 upsert 的，重复跑不会产生重复数据。

---

## 常见问题

### 改了文件名会怎样？

会被当成**新的一集**重新处理，并分配新集号——旧记录还留着，等于多了一集。

如果只是想改标题：**别改文件名**，去家长端「内容库」点开改标题（`PATCH /api/videos/{id}`）。

### 想重新处理某一集（比如响度没调好）

```bash
python tools/prep.py --src "某个目录" --out prepared --force
```

`--force` 忽略"已处理"判断，强制重跑。注意它会重新编码，输出文件覆盖原文件。

### 新素材和旧素材不在同一个盘

没关系，`--src` 每次可以指向不同目录，只要 `--out` 一直是 `prepared`，集号和清单就连续。

### 加了一整批新系列

直接跑，新系列会自动创建（系列名 = 文件名开头的 `(括号内容)`，或目录名）。

### 想调整排除规则

```bash
python tools/prep.py --src "D:\新素材" --out prepared --exclude 睡前故事 知识屋 中文 儿歌
```

`--exclude` 是系列名关键字黑名单，命中就整个系列跳过。

### 转写想换模型（某几集质量不满意）

单集重跑：

```bash
python tools/transcribe.py --src prepared/asr --out prepared/transcript --model medium.en
# 先删掉那一集的 json，再跑（否则 --skip-existing 会跳过）
rm prepared/transcript/<那一集>.json
```

---

## 增量行为速查

| 情况 | 结果 |
|---|---|
| 同一批素材重复跑 | 全部跳过，秒级完成 |
| 新增几集到已有系列 | 集号续排（E04、E05…），旧记录保留 |
| 换名但内容相同 | 识别为重复，跳过 |
| 同名但内容变了 | 重新处理，覆盖原记录 |
| `--src` 指向别的目录 | 旧记录照样保留（按 `original_name` 判断在不在本次目录里） |

---

## 数据一致性

导入是按 `video_out` 相对路径 upsert 的：

- 路径不变 → 更新元数据
- 路径变了（比如改了集号）→ 新增一集，旧的留在库里

所以**别随手改集号**。真要改，先在家长端把旧的删掉（暂未提供删除接口，可以直接改数据库）。
