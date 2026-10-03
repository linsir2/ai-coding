#!/usr/bin/env python3
"""Rebuild the R10a retrieval corpus: 78 notes, questions categorised by measurement.

Why this script exists rather than a hand-edited JSON: the paraphrase/lexical
split is **derived**, not declared.  A question is ``paraphrase`` exactly when the
lexical ranker misses its target in the top 3 — that is the property we want to
measure.  Hand-labelling it is how the previous corpus ended up with questions
that shared three content words with their answer and called it paraphrase.

Run from the repo root:

    .venv/bin/python scripts/build_retrieval_corpus.py --check
    .venv/bin/python scripts/build_retrieval_corpus.py --write

``--check`` is the red/green gate: it exits 1 when a well-formedness rule breaks.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.memory.benchmark import load_corpus  # noqa: E402
from ai_coding.memory.relevance import RecallParams, rank_notes  # noqa: E402

CORPUS = ROOT / "scripts" / "retrieval_corpus.json"
NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)

#: Questions whose answer the lexical ranker must NOT find in the top 3.
#: Kept as (query, [relevant ids]) — the ids are verified to exist below.
CANDIDATES: list[tuple[str, list[str]]] = [
    # carried over from the 52-note corpus, minus the two defects
    ("怎么让新同事快速上手这个项目", ["n16", "n27", "n43"]),
    ("担心模型把前面发过的东西再算一次钱", ["n06"]),
    ("我不想每次都被问要不要批准", ["n18"]),
    ("怎么防止工具输出把网页搞坏", ["n45"]),
    ("长期记性是怎么做的", ["n07", "n14"]),
    ("卡住的命令怎么收场", ["n10", "n40"]),
    ("代码评审之前要做什么", ["n02", "n20"]),
    ("我不想被过度设计绑架", ["n35"]),
    ("怎么把过去几轮的记忆压缩成一句话", ["n29", "n13"]),
    ("which relevance algorithm do we use", ["n32"]),
    # first authoring pass
    ("我们平时是怎么管理外部依赖的", ["n01"]),
    ("跑测试这件事有没有指定的姿势", ["n02"]),
    ("模型服务是挂在哪家的", ["n03"]),
    ("调试真实终端交互要开哪个开关", ["n04"]),
    ("写提交说明时有什么不该出现的符号", ["n05"]),
    ("省下来的对话费用靠什么拿到", ["n06"]),
    ("模型总共能从几个地方读到我们的笔记", ["n07"]),
    ("跑一次之后会留下哪些排查材料", ["n08"]),
    ("跟用户沟通的时候能不能打太极", ["n09"]),
    ("半路叫停一个进程要注意什么", ["n10"]),
    ("为什么不借用现成的那类代理库", ["n11"]),
    ("派生出来的执行者默认能看到多少上文", ["n12"]),
    ("聊太长了怎么办", ["n13"]),
    ("存这些记录用的是什么格式", ["n14"]),
    ("六份说明文档什么时候跟着改", ["n16"]),
    ("静态检查按哪个版本为下限", ["n17"]),
    ("外部服务默认会不会弹窗拦一下", ["n18"]),
    ("临时产物分别落在哪个路径", ["n19"]),
    ("开工之前要先落什么", ["n20"]),
    ("哪个开关一关纪律就松了", ["n21"]),
    ("捞回来的东西什么时候算被留住", ["n22"]),
    ("第三方观测库装了没有", ["n23"]),
    ("每步的花费是怎么被盯住的", ["n24"]),
    ("日常沟通用什么语言", ["n25"]),
    ("本机代码放在哪", ["n27"]),
    ("字段大小写不一致是怎么对上的", ["n28"]),
    ("临时记一句和长期记一笔有什么不同", ["n29"]),
    ("采到一半出问题能不能挪位置", ["n30"]),
    ("排序是怎么算出来的", ["n31"]),
    ("中文是怎么切开的", ["n34"]),
    ("设计上的复杂度要控制在什么程度", ["n35"]),
    ("光跑单元测试够不够", ["n36"]),
    ("收尾自检放在哪里", ["n37"]),
    ("这些东西最多攒多少", ["n38"]),
    ("出了岔子该放行还是该拦住", ["n39"]),
    ("等待批准最长会被卡多久", ["n40"]),
    ("派生出来的执行者还能再派生吗", ["n41"]),
    ("同一时间最多跑几个", ["n42"]),
    ("为什么不让它直接翻会话存档", ["n44"]),
    ("导出网页时哪里容易出安全问题", ["n45"]),
    # second pass, worded to avoid sharing content words with the answer
    ("平时那些第三方包怎么统一起来的", ["n01"]),
    ("验证代码时命令行该怎么敲", ["n02"]),
    ("后端接的是哪家的云服务", ["n03"]),
    ("记录变更时哪种字符要避开", ["n05"]),
    ("答复提问的人时能不能含糊其辞", ["n09"]),
    ("被叫出来的帮手一开始继承多少历史", ["n12"]),
    ("那几份说明书每轮要不要跟着动", ["n16"]),
    ("代码规范按哪一年版本兜底", ["n17"]),
    ("远端能力是不是每次都要先过一道人工", ["n18"]),
    ("哪一处能绕开那条硬约束", ["n21"]),
    ("随手留的短句跟真正入库的记录差在哪", ["n29"]),
    ("提取过程失败之后指针还能不能动", ["n30"]),
    ("汉字串是怎么拆成可检索单元的", ["n34"]),
    ("方案要不要往简单里做", ["n35"]),
    ("只验代码本身够不够", ["n36"]),
    ("验收的东西归谁保管", ["n37"]),
    ("帮手自己还能不能再叫帮手", ["n41"]),
    ("为什么不把存档原文喂进去", ["n44"]),
    ("生成页面时哪一步可能被人注入", ["n45"]),
    # third pass: the notes added with this task were previously untested
    ("两种把关一个在动作前一个在收尾处", ["n63"]),
    ("出错了怎么把原因带回给模型", ["n66"]),
    ("一轮最多能跑多久就自动掐断", ["n67"]),
    ("额度用完了会怎样", ["n69"]),
    ("为什么不选那种别人写好的编排框架", ["n71"]),
    ("出错信息该放哪", ["n72"]),
    ("测试里怎么固定住时间", ["n73"]),
    ("敏感信息可以落到版本库里吗", ["n74"]),
    ("注释该用什么语言", ["n75"]),
    ("能先把东西合进去以后再补验证吗", ["n76"]),
    ("一次提交该塞几件事", ["n77"]),
    ("说明里的数值能估吗", ["n78"]),
    ("每轮结束会记哪种用量", ["n61"]),
    ("存档靠哪个字段做老数据兼容", ["n65"]),
    ("隔离机制对谁生效", ["n68"]),
    ("排查材料怎么分堆", ["n70"]),
    ("还剩多少东西是悬着的", ["n53", "n54", "n55", "n56", "n57", "n58", "n59", "n60"]),
]

#: Questions nothing in the corpus answers.  ``怎么让模型别一直重复问同一个问题``
#: was a paraphrase item in the old corpus whose two targets (injection surfaces,
#: harvest cursor) do not answer it — no note anywhere mentions deduplicating a
#: repeated question, so it is a no-answer question, not a paraphrase one.
NO_ANSWER: list[str] = [
    "怎么让模型别一直重复问同一个问题",
    "生产环境的数据库密码是什么",
    "团队现在有多少人",
    "上线日期定在哪天",
    "服务器的公网地址是多少",
    "这个月的预算是多少",
    "客户端要支持哪些浏览器",
    "昨天的评审会决定了什么",
    "我们的域名是怎么解析的",
]

#: Dropped from the old corpus: the question asks how to do semantic retrieval and
#: its only target says the project deliberately does *not* do it.  Circular, and
#: it cannot distinguish a good ranker from one that merely echoes the query.
DROPPED = "怎么做语义检索"


def top3(notes, query: str) -> list[str]:
    params = RecallParams()
    return [hit.note.id for hit in rank_notes(notes, query, params=params, now=NOW, limit=3)]


def build() -> dict:
    data = json.loads(CORPUS.read_text(encoding="utf-8"))
    notes_json = data["notes"]
    path = CORPUS
    notes, _ = load_corpus(path, now=NOW)

    lexical: list[dict] = []
    paraphrase: list[dict] = []
    for query, relevant in CANDIDATES:
        item = {"query": query, "relevant": sorted(relevant)}
        found = top3(notes, query)
        if any(note_id in found for note_id in relevant):
            lexical.append(item)
        else:
            paraphrase.append(item)

    kept = [
        q
        for q in data["queries"]
        if q["category"] not in ("paraphrase", "noanswer")
        and q["query"] != DROPPED
        and q["query"] not in NO_ANSWER
    ]
    data["queries"] = (
        kept
        + [{**item, "category": "lexical"} for item in lexical]
        + [{**item, "category": "paraphrase"} for item in paraphrase]
        + [{"category": "noanswer", "query": q, "relevant": []} for q in NO_ANSWER]
    )
    assert notes_json is data["notes"]
    return data


def check(data: dict) -> list[str]:
    problems: list[str] = []
    ids = {note["id"] for note in data["notes"]}
    if len(ids) != len(data["notes"]):
        problems.append("note ids are not unique")
    kinds = Counter(note["kind"] for note in data["notes"])
    if kinds["open_thread"] < 8:
        problems.append(f"open_thread notes = {kinds['open_thread']}, need >= 8")
    queries = data["queries"]
    texts = [q["query"] for q in queries]
    if len(set(texts)) != len(texts):
        problems.append("duplicate query strings")
    for q in queries:
        if not q["query"].strip():
            problems.append("blank query")
        for note_id in q["relevant"]:
            if note_id not in ids:
                problems.append(f"{q['query']!r} points at unknown id {note_id}")
    cats = Counter(q["category"] for q in queries)
    if cats["paraphrase"] < 40:
        problems.append(f"paraphrase questions = {cats['paraphrase']}, need >= 40")
    if DROPPED in texts:
        problems.append(f"{DROPPED!r} is circular and must stay out")
    for q in queries:
        if q["category"] == "paraphrase" and not q["relevant"]:
            problems.append(f"{q['query']!r} is paraphrase but has no target")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the corpus file")
    parser.add_argument("--check", action="store_true", help="only report well-formedness")
    args = parser.parse_args(argv)

    data = build()
    problems = check(data)
    cats = Counter(q["category"] for q in data["queries"])
    kinds = Counter(n["kind"] for n in data["notes"])
    print(f"notes {len(data['notes'])} {dict(kinds)}")
    print(f"queries {len(data['queries'])} {dict(cats)}")
    if problems:
        for line in problems:
            print(f"  FAIL {line}")
        return 1
    print("well-formed OK")
    if args.write and not args.check:
        CORPUS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {CORPUS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
