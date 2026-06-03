"""
训练 SOS-1K TF-IDF 危机分类器，一次性运行。

标签映射（参照 SOS-1K 11 级标注规范）：
  L0-L3 → none    （日常负面情绪 / 绝望感，无自杀意念）
  L4-L5 → medium  （有自杀愿望，无具体方式或计划）
  L6-L10 → high   （有具体计划/方式，或正在进行中）

输出：
  models/crisis-bert/classifier.pkl
"""

import glob
import os
import pickle

import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.pipeline import Pipeline

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_BASE = os.path.join(ROOT, "datasets", "SOS-1K", "suicideDataProcessing", "data")
MODEL_DIR = os.path.join(ROOT, "models", "crisis-bert")
MODEL_PATH = os.path.join(MODEL_DIR, "classifier.pkl")


def label_to_level(label: int) -> str:
    if label <= 3:
        return "none"
    if label <= 5:
        return "medium"
    return "high"


def load_split(pattern, *, include_test=False):
    files = glob.glob(pattern)
    if include_test:
        test_path = os.path.join(DATA_BASE, "fine-grained", "test_data.tsv")
        files.append(test_path)
    dfs = [pd.read_csv(f, sep="\t") for f in files if os.path.exists(f)]
    df = pd.concat(dfs, ignore_index=True)
    df["level"] = df["myLabel"].apply(label_to_level)
    return df


def main():
    train_pattern = os.path.join(DATA_BASE, "fine-grained", "fold*.tsv")
    test_path = os.path.join(DATA_BASE, "fine-grained", "test_data.tsv")

    train_df = load_split(train_pattern)
    test_df = pd.read_csv(test_path, sep="\t")
    test_df["level"] = test_df["myLabel"].apply(label_to_level)

    print(f"Train: {len(train_df)} | Test: {len(test_df)}")
    print("Train distribution:\n", train_df["level"].value_counts().to_string())

    pipeline = Pipeline([
        ("tfidf", TfidfVectorizer(
            analyzer="char",
            ngram_range=(1, 3),
            max_features=15000,
            sublinear_tf=True,
        )),
        ("clf", LogisticRegression(
            C=1.0,
            max_iter=2000,
            class_weight="balanced",
            solver="lbfgs",
            multi_class="multinomial",
            random_state=42,
        )),
    ])

    pipeline.fit(train_df["comment"], train_df["level"])

    y_pred = pipeline.predict(test_df["comment"])
    print("\n=== 测试集评估 ===")
    print(classification_report(
        test_df["level"], y_pred,
        labels=["none", "medium", "high"],
        zero_division=0,
    ))
    print("混淆矩阵 (none / medium / high):")
    print(confusion_matrix(test_df["level"], y_pred, labels=["none", "medium", "high"]))

    # 检查高置信度样本的准确率（决定 crisis.py 中的置信度阈值）
    proba = pipeline.predict_proba(test_df["comment"])
    classes = list(pipeline.classes_)
    max_proba = proba.max(axis=1)
    pred_levels = [classes[i] for i in proba.argmax(axis=1)]
    correct = [p == t for p, t in zip(pred_levels, test_df["level"])]

    for threshold in [0.70, 0.75, 0.80, 0.85, 0.90]:
        mask = max_proba >= threshold
        if mask.sum() == 0:
            continue
        acc = sum(c for c, m in zip(correct, mask) if m) / mask.sum()
        print(f"  置信度 ≥ {threshold:.2f}: {mask.sum():3d} 样本, 准确率 {acc:.3f}")

    os.makedirs(MODEL_DIR, exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(pipeline, f)
    print(f"\n模型已保存: {MODEL_PATH}")


if __name__ == "__main__":
    main()
