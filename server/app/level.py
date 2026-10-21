"""难度分级：词表 + 词形还原 + 语速 + 语音占比。

思路：
- 以 Dolch 220 sight words（美国 K-3 高频词）为"应该认识"的基线
- 专有名词（Pip、Omi 这类）不算生词，否则每集生词率都会虚高
- 词形还原后统计（running / ran / runs 算一个词）
- 综合生词率、语速、句长给出 L1–L5

想要更准，可以把 Oxford 3000 之类的词表丢进 wordlists/extra.txt（一行一个词），
启动时会自动并入基线。
"""
from __future__ import annotations

import re
from pathlib import Path

WORDLIST_DIR = Path(__file__).parent / "wordlists"

DOLCH_220 = """
a and away big blue can come down find for funny go help here i in is it jump little
look make me my not one play red run said see the three to two up we where yellow you
all am are at ate be black brown but came did do eat four get good have he into like
must new no now on our out please pretty ran ride saw say she so soon that there they
this too under want was well went what white who will with yes
after again an any as ask by could every fly from give going had has her him his how
just know let live may of old once open over put round some stop take thank them then
think walk were when
always around because been before best both buy call cold does dont fast first five
found gave goes green its made many off or pull read right sing sit sleep tell their
these those upon us use very wash which why wish work would write your
about better bring carry clean cut done draw drink eight fall far full got grow hold
hot hurt if keep kind laugh light long much myself never only own pick seven shall show
six small start ten today together try warm
"""

# 补充：儿童动画里极高频、但不在 Dolch 里的词
EXTRA_COMMON = """
mom dad mommy daddy baby friend friends hello hi bye goodbye oh wow hey yeah yep nope
thank thanks sorry okay ok let lets dont cant wont didnt doesnt isnt arent wasnt couldnt
wouldnt shouldnt im its thats whats youre were theyre hes shes ive youll weve id
good bad big small happy sad angry scared hungry tired funny silly nice cute
one two three four five six seven eight nine ten first last next more most very too
again still just only also even really quite almost together everyone someone nobody
something nothing anything everything somewhere nowhere
look see watch listen hear say tell ask answer know think remember forget
come go run walk jump climb push pull carry hold give take put open close start stop
wait hurry help save find lose catch hide look
water fire rain wind sun moon star sky tree leaf leaves flower grass stone rock
ant bee fox elephant bird fish butterfly spider beetle queen king baby mother father
day night morning today tomorrow yesterday time now later soon
big huge tiny little tall short long fast slow strong weak heavy light hot cold warm
good better best bad worse worst
"""


def _load_extra() -> set[str]:
    extra = set(EXTRA_COMMON.split())
    path = WORDLIST_DIR / "extra.txt"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            word = line.strip().lower()
            if word and not word.startswith("#"):
                extra.add(word)
    return extra


BASE_WORDS = {word.lower() for word in DOLCH_220.split()} | _load_extra()

TOKEN_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
SENTENCE_RE = re.compile(r"[.!?]+")

try:  # 有 simplemma 就用它做词形还原，没有就退回后缀规则
    import simplemma

    def lemmatize(word: str) -> str:
        return simplemma.lemmatize(word.lower(), lang="en")

except Exception:  # pragma: no cover
    def lemmatize(word: str) -> str:
        lowered = word.lower()
        for suffix, replacement in (
            ("ies", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", ""), ("'s", ""),
        ):
            if lowered.endswith(suffix) and len(lowered) > len(suffix) + 2:
                return lowered[: -len(suffix)] + replacement
        return lowered


def _proper_nouns(text: str) -> set[str]:
    """近似判断专有名词：只以大写形式出现，且从不出现在句首。"""
    capitals: dict[str, int] = {}
    lowercase: set[str] = set()

    for match in TOKEN_RE.finditer(text):
        word = match.group()
        start = match.start()
        # 往前找最近的非空白字符，判断是否句首
        prefix = text[:start].rstrip()
        sentence_initial = not prefix or prefix[-1] in ".!?\"'“”‘’\n"

        if word[0].isupper():
            if not sentence_initial:
                capitals[word.lower()] = capitals.get(word.lower(), 0) + 1
        else:
            lowercase.add(word.lower())

    return {word for word in capitals if word not in lowercase}


def analyze_text(text: str, speech_s: float) -> dict:
    """返回难度指标。speech_s 用于算语速（WPM）。"""
    tokens = [match.group() for match in TOKEN_RE.finditer(text or "")]
    if not tokens:
        return {
            "words": 0,
            "unique_words": 0,
            "oov_ratio": 0.0,
            "avg_sentence_len": 0.0,
            "wpm": 0.0,
            "level_score": 0.0,
            "level": None,
            "oov_words": [],
            "proper_nouns": [],
        }

    proper = _proper_nouns(text)
    lemmas = [lemmatize(token) for token in tokens]
    unique = set(lemmas)

    oov = {word for word in unique if word not in BASE_WORDS and word not in proper}
    oov_ratio = round(len(oov) / len(unique), 3) if unique else 0.0

    sentences = [part for part in SENTENCE_RE.split(text or "") if part.strip()]
    avg_sentence_len = round(len(tokens) / len(sentences), 1) if sentences else float(len(tokens))

    minutes = speech_s / 60 if speech_s > 0 else 0
    wpm = round(len(tokens) / minutes, 1) if minutes > 0 else 0.0

    # 生词率为主（60%），语速与句长为辅
    score = (
        0.6 * oov_ratio
        + 0.2 * min(wpm / 150.0, 1.0)
        + 0.2 * min(avg_sentence_len / 12.0, 1.0)
    )
    score = round(score, 3)

    if oov_ratio <= 0.10:
        level = "L1"
    elif oov_ratio <= 0.18:
        level = "L2"
    elif oov_ratio <= 0.28:
        level = "L3"
    elif oov_ratio <= 0.40:
        level = "L4"
    else:
        level = "L5"
    # 语速过快对听力是额外门槛，向上调一档
    if wpm >= 120 and level != "L5":
        level = f"L{int(level[1]) + 1}"

    return {
        "words": len(tokens),
        "unique_words": len(unique),
        "oov_ratio": oov_ratio,
        "avg_sentence_len": avg_sentence_len,
        "wpm": wpm,
        "level_score": score,
        "level": level,
        "oov_words": sorted(oov)[:60],
        "proper_nouns": sorted(proper),
    }
