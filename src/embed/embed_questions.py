import json
from pathlib import Path

import pandas as pd
import torch
from sentence_transformers import SentenceTransformer


# ============================================================
# CONFIG
# ============================================================

INPUT_FILE = Path(
    "/kaggle/input/datasets/tinasikhbse/financial-rag-eval-questions/eval_questions_v2.csv"
)

OUTPUT_FILE = Path("eval_questions_bge_m3.jsonl")

MODEL_NAME = "BAAI/bge-m3"
BATCH_SIZE = 16


# ============================================================
# LOAD QUESTIONS
# ============================================================

print(f"Loading questions from: {INPUT_FILE}")

df = pd.read_csv(INPUT_FILE)

print(f"Loaded {len(df):,} questions.")
print(f"Columns: {list(df.columns)}")

questions = df["კითხვა"].fillna("").astype(str).tolist()


# ============================================================
# LOAD MODEL
# ============================================================

device = "cuda" if torch.cuda.is_available() else "cpu"

print(f"Device: {device}")
print(f"Loading model: {MODEL_NAME}")

model = SentenceTransformer(
    MODEL_NAME,
    device=device,
)

print("Model loaded.")


# ============================================================
# EMBEDDINGS
# ============================================================

print("Encoding questions...")

embeddings = model.encode(
    questions,
    batch_size=BATCH_SIZE,
    show_progress_bar=True,
    convert_to_tensor=False,
)

print("Encoding complete.")


# ============================================================
# SAVE
# ============================================================

OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

with OUTPUT_FILE.open("w", encoding="utf-8") as f:

    for row, embedding in zip(df.to_dict("records"), embeddings):

        record = {
            "question": row["კითხვა"],
            "type": row["ტიპი"],
            "doc_id": row["დოკუმენტის_ID"],
            "source_parent": row["წყარო_პარენტი"],
            "source_child": row["წყარო_ჩაილდი"],
            "evidence": row["მტკიცებულება"],
            "answer": row["პასუხი"],
            "batch": row["ბეჩი"],
            "embedding": embedding.tolist(),
        }

        f.write(
            json.dumps(
                record,
                ensure_ascii=False,
            )
            + "\n"
        )


print(f"Saved embeddings to: {OUTPUT_FILE}")