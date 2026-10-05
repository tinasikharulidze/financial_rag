import json
from pathlib import Path

import torch
from sentence_transformers import SentenceTransformer


# ============================================================
# CONFIG
# ============================================================

INPUT_FILE = Path("/kaggle/input/datasets/tinasikhbse/financial-rag-children/children.jsonl")
OUTPUT_FILE = Path("children_bge_m3.jsonl")

MODEL_NAME = "BAAI/bge-m3"
BATCH_SIZE = 16


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
# LOAD CHILDREN
# ============================================================

children = []

with INPUT_FILE.open("r", encoding="utf-8") as f:
    for line in f:
        if line.strip():
            children.append(json.loads(line))

print(f"Loaded {len(children)} children.")


# ============================================================
# EMBED
# ============================================================

texts = [child["retrieval_text"] for child in children]

print("Starting embedding...")

embeddings = model.encode(
    texts,
    batch_size=BATCH_SIZE,
    normalize_embeddings=True,
    show_progress_bar=True,
)

print("Embedding finished.")

# ============================================================
# SAVE
# ============================================================

OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

with OUTPUT_FILE.open("w", encoding="utf-8") as f:

    for child, embedding in zip(children, embeddings):

        record = {
            "child_id": child["chunk_id"],
            "parent_id": child["parent_id"],
            "doc_id": child["doc_id"],
            "kind": child["kind"],
            "text": child["text"],
            "retrieval_text": child["retrieval_text"],
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