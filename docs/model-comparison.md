# small.en vs medium.en 对比报告

日期：2026-10-21
样本：9 集，9.7 分钟音频（同一批 `prepared/asr/*.wav`）
参数：除模型外完全一致（int8、3 线程、VAD 关闭、同套幻觉过滤）

---

## 一、总账

| 指标 | small.en | medium.en | 差异 |
|---|---|---|---|
| 总词数 | 603 | **626** | +23（+3.8%） |
| 唯一词 | 246 | **249** | +3 |
| 合并去重后词汇量 | — | — | 254 |
| 耗时（纯推理） | 67 秒 | ~210 秒 | 3.1× |
| 模型加载/下载 | 5 秒 | 492 秒（首次下载 1.5GB） | — |
| 模型体积 | ~480 MB | ~1.5 GB | 3× |
| 速度 | 8.6× 实时 | **2.8× 实时** | — |

---

## 二、逐集词数

| 集 | small.en | medium.en | 差异 |
|---|---|---|---|
| 大嘴狐 S01E01 小蚂蚁大奇迹 | 75 | **98** | **+23** |
| 大嘴狐 S01E02 小蚂蚁搬家的微观世界 | 78 | 78 | 0 |
| 大嘴狐 S01E03 小蚂蚁大英雄 | 88 | 89 | +1 |
| 皮普 S01E01 哎呀，我的腿麻了 | 20 | 20 | 0 |
| 皮普 S01E02 朋友帮助朋友 | 73 | 68 | −5 |
| 皮普 S01E03 甜蜜的陷阱 | 24 | 24 | 0 |
| 皮普 S01E04 皮普的第一个VLOG | 34 | 34 | 0 |
| 皮普 S01E05 躲雨的友谊 | 21 | 20 | −1 |
| 皮普 S01E06 进入蚂蚁王国 | 190 | **195** | +5 |
| **合计** | **603** | **626** | **+23** |

---

## 三、词汇差异（核心问题）

**结论：词汇量差异极小，只有 3%。**

| 集合 | 数量 |
|---|---|
| 两者共有 | 241 |
| 仅 medium 有 | 8 |
| 仅 small 有 | 5 |
| 合并去重 | 254 |

**仅 medium 识别出的词（small 漏掉或识别错）：**

`job`（small 识别成 "Job!"）、`experts`（small 是 "expert"）、`yay`、`uh`、`hmm`、`ant's`（所有格）、`ray`、`phew`

**仅 small 有的词：**

`expert`、`wren`、`hm`、`b`（"B." 应为 "Bee"）、`yeah`

这 13 个词里，**真正影响理解的是 `b` → `Bee` 和 `wren` → `ray`** 两处误识，其余是语气词、复数、所有格这类细微差别。

---

## 四、真正的差距不在词汇，在"漏句"

词汇只差 3%，但**单集内容完整性差异很大**。看 S01E01（小蚂蚁大奇迹）：

**small.en（75 词，中间一大段没有标点、还漏了内容）：**
> ...I can't get out help where are you don't worry I have a plan let's dig here push together don't give up keep going look we made a path one last step you saved me...

**medium.en（98 词，补回了 6 句对白）：**
> ...I can't get out. **Help!** Where are you? Don't worry, I have a plan. **Come on, let's help! We are coming!** Let's dig here! Push together, don't give up! Keep going, **good job! Dig! Dig! Dig!** Look! We made a path! **Come on, elephant! You can do it!** One last step. **Yay! You're out!** You saved me...

small 漏掉了 "We are coming!"、"Dig! Dig! Dig!"、"Come on, elephant! You can do it!"、"Yay! You're out!" 这些**有实义的句子**，而且合并成一大段无标点文本。

**medium 的短板**：S01E02（朋友帮助朋友）后半段反而丢了标点，合并成流水句（"anyone else? huh look what now wait what is that..."），并且漏掉了 "Pip," 这个称呼。所以它不是全面碾压。

---

## 五、结论与选型建议

| 用途 | 推荐 | 理由 |
|---|---|---|
| **生词表、难度分级** | **small.en 够用** | 词汇集合 96% 一致，8 个新增词不影响分级判断 |
| **全文搜索索引** | small.en 够用 | 关键词基本都在 |
| **小测出题、字幕** | **medium.en** | 漏句会直接导致题目缺上下文 |
| **给孩子看的文本** | 两者都需人工校对 | 都有错词 |

**成本权衡**：

- 这台开发机：medium 2.8× 实时 → 900 分钟素材约 **5.4 小时**，一次性可接受
- NAS 的 N100：medium 估计 0.6–0.8× 实时 → **20 小时以上**，不可行

**建议方案（混合）**：

1. **存量全量用 medium.en，在开发机上跑**（5.4 小时，一次性）
2. **NAS 上只跑 small.en 处理新增集数**（每晚几集，无感）
3. 分级和搜索用 small 结果即可，出题和字幕取 medium 结果

如果嫌麻烦，**纯 small.en 也能撑起整个产品**——差的只是 3% 的词汇和少量漏句。

---

## 六、决策记录

**2026-10-21 决定：统一使用 `small.en`。**

理由：

1. 词汇差异仅 3%（246 vs 249 唯一词，合并 254），对生词表和分级算法没有实质影响
2. medium 的优势集中在"补回漏句"，但漏句主要影响出题和字幕，不影响词汇统计
3. 成本差距是 3.1 倍耗时 + 3 倍模型体积；N100 上 medium 全量需要 20 小时以上，不可行
4. 保留逃生舱：若某集出题时发现上下文缺失，可用 `--model medium.en` 对该集单独重跑

**执行方式**：全流程（存量 + 新增）统一 `small.en`；`prepared/transcript/` 为唯一有效结果，`prepared/transcript-medium/` 仅作为本次对比的证据保留，不进入后续流程。
