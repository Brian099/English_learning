#!/usr/bin/env python3
"""对比两次转写结果：词数、词汇差异、逐集差异。

用法：
    python tools/compare_transcripts.py --a prepared/transcript --b prepared/transcript-medium \
        --label-a small.en --label-b medium.en
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

TOKEN_RE = re.compile(r"[a-z']+")


def load(dir_path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for json_path in sorted(dir_path.glob("*.json")):
        data = json.loads(json_path.read_text(encoding="utf-8"))
        out[json_path.stem] = data
    return out


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--a", required=True)
    parser.add_argument("--b", required=True)
    parser.add_argument("--label-a", default="A")
    parser.add_argument("--label-b", default="B")
    args = parser.parse_args()

    a = load(Path(args.a))
    b = load(Path(args.b))
    keys = [k for k in a if k in b]

    print(f"对比 {args.label_a} vs {args.label_b}，共 {len(keys)} 集\n")

    print(f"{'集':<46} {args.label_a:>8} {args.label_b:>8} {'差异':>7}")
    print("-" * 74)
    total_a = total_b = 0
    for key in keys:
        wa = len(a[key]["text"].split())
        wb = len(b[key]["text"].split())
        total_a += wa
        total_b += wb
        name = key if len(key) <= 44 else key[:43] + "…"
        print(f"{name:<46} {wa:>8} {wb:>8} {wb - wa:>+7}")
    print("-" * 74)
    print(f"{'合计':<46} {total_a:>8} {total_b:>8} {total_b - total_a:>+7}")

    # 词汇集合对比
    vocab_a: Counter[str] = Counter()
    vocab_b: Counter[str] = Counter()
    for key in keys:
        vocab_a.update(tokens(a[key]["text"]))
        vocab_b.update(tokens(b[key]["text"]))

    set_a, set_b = set(vocab_a), set(vocab_b)
    only_b = set_b - set_a
    only_a = set_a - set_b
    common = set_a & set_b

    print(f"\n=== 词汇集合 ===")
    print(f"{args.label_a} 唯一词 {len(set_a)}，{args.label_b} 唯一词 {len(set_b)}")
    print(f"共有 {len(common)}，仅 {args.label_b} 有 {len(only_b)}，仅 {args.label_a} 有 {len(only_a)}")
    print(f"合并去重后总词汇量 {len(set_a | set_b)}")

    print(f"\n=== 仅 {args.label_b} 识别出的词（{args.label_a} 漏掉或错了）===")
    for word, count in sorted(((w, vocab_b[w]) for w in only_b), key=lambda x: -x[1])[:40]:
        print(f"  {word:<18} ×{count}")

    print(f"\n=== 仅 {args.label_a} 有的词（{args.label_b} 没识别出）===")
    for word, count in sorted(((w, vocab_a[w]) for w in only_a), key=lambda x: -x[1])[:40]:
        print(f"  {word:<18} ×{count}")

    # 逐集文本对照（取差异最大的 3 集）
    ranked = sorted(keys, key=lambda k: -abs(len(b[k]["text"].split()) - len(a[k]["text"].split())))[:3]
    for key in ranked:
        print(f"\n=== {key} ===")
        print(f"--- {args.label_a} ---\n{a[key]['text']}")
        print(f"--- {args.label_b} ---\n{b[key]['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
