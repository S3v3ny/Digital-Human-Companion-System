"""
SoulChat 多轮咨询语料入库脚本（一次性运行）。

补充 PsyQA 在「真实多轮安抚话术」上的覆盖。与 build_kb.py 隔离：
  - 独立集合 soulchat_knowledge，可单独重建、不影响 PsyQA
  - 每段对话只取首轮 user→assistant 配对（最丰富的求助陈述 + 首个咨询回应）
  - 跨全量对话均匀采样到 MAX_PAIRS 条，保证主题多样性

运行：
  python build_kb_soulchat.py
"""

import glob
import os

import chromadb
import pandas as pd
from sentence_transformers import SentenceTransformer

# ── 配置 ──────────────────────────────────────────────────────────────────────
MAX_PAIRS = 80000          # 入库上限；调大需重建集合（先删 vector_db/ 下对应集合）
MIN_USER_LEN = 15          # 用户陈述最短长度，过滤无意义短句
EMBED_BATCH = 256

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE_DIR, "datasets", "SoulChat", "data")
_BGE_LOCAL = os.path.join(_BASE_DIR, "models", "bge-small-zh-v1.5")
COLLECTION_NAME = "soulchat_knowledge"


def collect_first_pairs() -> list[dict]:
    """从所有 parquet 收集每段对话的首轮 user→assistant 配对。"""
    seen_user = set()
    pairs = []
    for path in sorted(glob.glob(os.path.join(_DATA_DIR, "*.parquet"))):
        df = pd.read_parquet(path, columns=["id", "topic", "messages"])
        for _, row in df.iterrows():
            msgs = row["messages"]
            if len(msgs) < 2:
                continue
            u, a = msgs[0], msgs[1]
            if u["role"] != "user" or a["role"] != "assistant":
                continue
            user_text = (u["content"] or "").strip()
            asst_text = (a["content"] or "").strip()
            if len(user_text) < MIN_USER_LEN or not asst_text:
                continue
            if user_text in seen_user:   # 去重，避免重复求助语句
                continue
            seen_user.add(user_text)
            pairs.append({
                "id": str(row["id"]),
                "topic": str(row["topic"]),
                "user": user_text,
                "asst": asst_text,
            })
    return pairs


def even_sample(items: list, cap: int) -> list:
    """跨全量均匀采样到 cap 条（保留主题多样性，结果可复现）。"""
    if len(items) <= cap:
        return items
    stride = len(items) / cap
    return [items[int(i * stride)] for i in range(cap)]


def main():
    print("正在加载 Embedding 模型...")
    embedder = SentenceTransformer(_BGE_LOCAL if os.path.isdir(_BGE_LOCAL) else "BAAI/bge-small-zh-v1.5")

    client = chromadb.PersistentClient(path="./vector_db")
    collection = client.get_or_create_collection(name=COLLECTION_NAME)
    if collection.count() > 0:
        print(f"集合 {COLLECTION_NAME} 已有 {collection.count()} 条数据，跳过初始化。")
        return

    print("正在读取 SoulChat 对话并提取首轮配对...")
    pairs = collect_first_pairs()
    print(f"有效首轮配对 {len(pairs)} 条，采样上限 {MAX_PAIRS}")
    pairs = even_sample(pairs, MAX_PAIRS)
    print(f"采样后 {len(pairs)} 条，开始向量化...")

    documents = [f"【来访者】: {p['user']}\n【咨询师回应】: {p['asst']}" for p in pairs]
    ids = [f"soulchat_{p['id']}" for p in pairs]
    # id 去重（极少数对话 id 可能重复），保证 ChromaDB 不报错
    seen_id, uniq_docs, uniq_ids = set(), [], []
    for doc, _id in zip(documents, ids):
        if _id in seen_id:
            _id = f"{_id}_{len(uniq_ids)}"
        seen_id.add(_id)
        uniq_docs.append(doc)
        uniq_ids.append(_id)
    documents, ids = uniq_docs, uniq_ids

    total = len(documents)
    embeddings = []
    for i in range(0, total, EMBED_BATCH):
        chunk = documents[i:i + EMBED_BATCH]
        embeddings.extend(embedder.encode(chunk).tolist())
        print(f"已向量化 {min(i + EMBED_BATCH, total)} / {total}")

    print("正在写入 ChromaDB...")
    add_batch = client.get_max_batch_size()
    for i in range(0, total, add_batch):
        collection.add(
            embeddings=embeddings[i:i + add_batch],
            documents=documents[i:i + add_batch],
            ids=ids[i:i + add_batch],
        )
        print(f"已写入 {min(i + add_batch, total)} / {total}")

    print(f"[OK] SoulChat 入库完成，共 {collection.count()} 条！")


if __name__ == "__main__":
    main()
