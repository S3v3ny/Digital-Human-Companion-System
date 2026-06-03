import os
import json
import chromadb
from sentence_transformers import SentenceTransformer

# ==========================================
# 1. 初始化核心组件
# ==========================================
print("正在加载 Embedding 模型...")
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_BGE_LOCAL = os.path.join(_BASE_DIR, "models", "bge-small-zh-v1.5")
embedder = SentenceTransformer(_BGE_LOCAL if os.path.isdir(_BGE_LOCAL) else 'BAAI/bge-small-zh-v1.5')

print("正在初始化 ChromaDB 本地持久化存储...")
# PersistentClient 会在当前目录下生成一个 vector_db 文件夹，把数据写进硬盘
client = chromadb.PersistentClient(path="./vector_db")

# 获取或创建一个集合 (类似关系型数据库中的 Table)
# 针对欧氏距离 (L2) 进行了默认优化
collection = client.get_or_create_collection(name="psy_cbt_knowledge")


# ==========================================
# 2. 准备数据源 (改为读取本地文件)
# ==========================================
print("正在读取本地 JSON 文件...")
with open('psy_data.json', 'r', encoding='utf-8') as f:
    # 将 JSON 文件加载为 Python 的列表(List)
    real_data = json.load(f) 
    print(f"成功读取了 {len(real_data)} 条数据！")


# ==========================================
# 3. 数据处理与入库 (修改遍历逻辑)
# ==========================================
def ingest_data():
    if collection.count() > 0:
        print(f"知识库已存在 {collection.count()} 条数据，跳过初始化。")
        return

    print("开始向量化 (几万条数据可能需要跑几分钟)...")
    documents = [] 
    ids = []       
    
    
    for item in real_data:
        # 1. 提取来访者的痛点信息 (标题 + 描述 + 关键词)
        # 用 get 方法防止某些字段为空导致报错
        q_title = item.get("question", "")
        q_desc = item.get("description", "")
        keywords = item.get("keywords", "")
        q_id = item.get("questionID", "unknown")
        
        # 将标题和详细描述拼在一起，这是将来 RAG 匹配用户提问的核心诱饵
        user_text = f"{q_title}。详细情况：{q_desc}"
        
        # 2. 提取专家回复 (关键：一个问题可能有多个专家回答，所以要加一层循环)
        answers_list = item.get("answers", [])
        
        for ans_idx, ans_item in enumerate(answers_list):
            expert_text = ans_item.get("answer_text", "")
            
            # 如果答案是空的，直接跳过
            if not expert_text:
                continue
                
            # 3. 组装最终喂给向量库的终极文本
            # 加入【关键词】能极大提升 ChromaDB 在空间距离计算时的准确率
            combined_text = f"【标签】: {keywords}\n【来访者问题】: {user_text}\n【干预策略】: {expert_text}"
            documents.append(combined_text)
            
            # 4. 生成绝对唯一的 ID (原问题ID + 专家回答序号)
            ids.append(f"psyqa_{q_id}_ans_{ans_idx}")
    

    # 调用模型批量生成向量
    embeddings = embedder.encode(documents).tolist()

    print("正在将向量写入 ChromaDB...")

    batch_size = client.get_max_batch_size()
    total = len(documents)

    for i in range(0, total, batch_size):
        batch_embeddings = embeddings[i:i+batch_size]
        batch_documents = documents[i:i+batch_size]
        batch_ids = ids[i:i+batch_size]

        collection.add(
            embeddings=batch_embeddings,
            documents=batch_documents,
            ids=batch_ids
        )
        print(f"已写入 {min(i+batch_size, total)} / {total} 条数据")

    print("🎉 真实数据入库完成！")


# ==========================================
# 4. 模拟检索逻辑 (Retrieval)
# ==========================================
def search_knowledge(user_query: str, top_k: int = 1):
    print(f"\n--- 收到用户提问: '{user_query}' ---")
    
    # 将用户的实时提问也变成向量
    query_embedding = embedder.encode([user_query]).tolist()

    # 在向量库中查找距离最近的 top_k 个文档
    results = collection.query(
        query_embeddings=query_embedding,
        n_results=top_k
    )

    # 提取查到的文档文本
    retrieved_docs = results['documents'][0]
    
    print(">>> 检索到的权威知识:")
    for i, doc in enumerate(retrieved_docs):
        print(f"[{i+1}] {doc}")
    
    return retrieved_docs

# ==========================================
# 主运行入口
# ==========================================
if __name__ == "__main__":
    # 执行入库（仅首次运行有效）
    ingest_data()
    
    # 模拟真实用户的不同问法，看看向量检索的泛化能力
    # 注意：即便用词不一样，只要“语义距离”近，它也能查出来
    search_knowledge("我感觉生活没有意义。")
    search_knowledge("我紧张得快喘不过气了怎么办？")