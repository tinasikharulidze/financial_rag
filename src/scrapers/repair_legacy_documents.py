import json
from pathlib import Path

from bs4 import BeautifulSoup
import re


INPUT_FILE = Path(
    "matsne_data/documents.jsonl"
)

RAW_DOCUMENTS_DIR = Path(
    "matsne_data/documents_raw"
)

OUTPUT_FILE = Path(
    "matsne_data/documents_repaired.jsonl"
)


def clean_text(text):
    if not text:
        return ""

    text = text.replace(
        "\xa0",
        " "
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def parse_document(html, metadata):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    maindoc = soup.select_one(
        "#maindoc"
    )

    if not maindoc:
        raise ValueError(
            "Could not find #maindoc"
        )

    parts = []

    # --------------------------------------------------------
    # Modern Matsne structure
    # --------------------------------------------------------

    content_elements = maindoc.select(
        '[id$="_Content"]'
    )

    if content_elements:

        for part in content_elements:

            part_id = part.get("id")

            text = clean_text(
                part.get_text(
                    " ",
                    strip=True
                )
            )

            if not part_id:
                continue

            structure_id = part_id[
                :-len("_Content")
            ]

            components = [
                c
                for c in structure_id.split(";")
                if c
            ]

            part_type = None

            for component in components:

                if ":" in component:

                    key, value = component.split(
                        ":",
                        1
                    )

                    if key != "DOCUMENT":
                        part_type = key
                        break

            if part_type is None:
                part_type = "UNKNOWN"

            enclosure_number = None

            for component in components:

                if component.startswith(
                    "ENCLOSURE:"
                ):
                    enclosure_number = component.split(
                        ":",
                        1
                    )[1]

            parts.append({
                "id": part_id,
                "structure": components,
                "type": part_type,
                "enclosure": enclosure_number,
                "text": text,
            })

    # --------------------------------------------------------
    # Legacy / Word-style structure
    # --------------------------------------------------------

    else:

        paragraphs = maindoc.select("p")

        for index, paragraph in enumerate(
            paragraphs
        ):

            text = clean_text(
                paragraph.get_text(
                    " ",
                    strip=True
                )
            )

            if not text:
                continue

            classes = paragraph.get(
                "class",
                []
            )

            paragraph_type = (
                classes[0]
                if classes
                else "paragraph"
            )

            parts.append({
                "id": f"LEGACY:{index:04d}",
                "structure": [
                    "LEGACY",
                    paragraph_type,
                ],
                "type": paragraph_type,
                "enclosure": None,
                "text": text,
            })

    result = dict(metadata)

    result["parts"] = parts

    result["text"] = "\n\n".join(
        part["text"]
        for part in parts
        if part["text"]
    )

    result["part_count"] = len(parts)

    return result


# ============================================================
# REPAIR ONLY EMPTY DOCUMENTS
# ============================================================

documents = []

with INPUT_FILE.open(
    "r",
    encoding="utf-8"
) as f:

    for line in f:

        documents.append(
            json.loads(line)
        )


repaired = 0
still_empty = 0

with OUTPUT_FILE.open(
    "w",
    encoding="utf-8"
) as out:

    for document in documents:

        # Already extracted correctly.
        if document.get("part_count", 0) > 0:

            out.write(
                json.dumps(
                    document,
                    ensure_ascii=False
                ) + "\n"
            )

            continue

        document_id = document[
            "document_id"
        ]

        raw_path = (
            RAW_DOCUMENTS_DIR
            / f"{document_id}.html"
        )

        print(
            f"Repairing {document_id}..."
        )

        if not raw_path.exists():

            print(
                f"  RAW HTML NOT FOUND"
            )

            still_empty += 1

            out.write(
                json.dumps(
                    document,
                    ensure_ascii=False
                ) + "\n"
            )

            continue

        html = raw_path.read_text(
            encoding="utf-8"
        )

        try:

            repaired_document = parse_document(
                html,
                document
            )

            if repaired_document[
                "part_count"
            ] > 0:

                print(
                    f"  OK: "
                    f"{repaired_document['part_count']} parts, "
                    f"{len(repaired_document['text'])} chars"
                )

                out.write(
                    json.dumps(
                        repaired_document,
                        ensure_ascii=False
                    ) + "\n"
                )

                repaired += 1

            else:

                print(
                    f"  STILL EMPTY"
                )

                out.write(
                    json.dumps(
                        document,
                        ensure_ascii=False
                    ) + "\n"
                )

                still_empty += 1

        except Exception as exc:

            print(
                f"  ERROR: {exc}"
            )

            out.write(
                json.dumps(
                    document,
                    ensure_ascii=False
                ) + "\n"
            )

            still_empty += 1


print()
print(
    f"Repaired: {repaired}"
)

print(
    f"Still empty: {still_empty}"
)

print(
    f"Total documents: {len(documents)}"
)

print(
    f"Output: {OUTPUT_FILE}"
)