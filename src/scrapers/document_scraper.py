import json
import logging
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://matsne.gov.ge"

SEARCH_PAGES_DIR = Path(
    "matsne_data/search_pages"
)

RAW_DOCUMENTS_DIR = Path(
    "matsne_data/documents_raw"
)

OUTPUT_FILE = Path(
    "matsne_data/documents.jsonl"
)

FAILED_FILE = Path(
    "matsne_data/failed_documents.json"
)

START_PAGE = 1
END_PAGE = 11

REQUEST_DELAY = 2
TIMEOUT = 60


# ============================================================
# DIRECTORIES
# ============================================================

RAW_DOCUMENTS_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

retry = Retry(
    total=5,
    connect=5,
    read=5,
    status=5,
    backoff_factor=1,
    status_forcelist=[
        429,
        500,
        502,
        503,
        504,
    ],
    allowed_methods=[
        "GET"
    ],
)

adapter = HTTPAdapter(
    max_retries=retry
)

session.mount(
    "https://",
    adapter
)

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 "
        "(X11; Linux x86_64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/154.0 Safari/537.36"
    )
})


# ============================================================
# HELPERS
# ============================================================

def clean_text(text):
    """
    Normalize whitespace while preserving Georgian text.
    """

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


def extract_document_id(url):
    """
    Extract Matsne document ID from URL.
    """

    match = re.search(
        r"/document/view/(\d+)",
        url
    )

    if not match:
        return None

    return match.group(1)


# ============================================================
# SEARCH RESULT PARSER
# ============================================================

def parse_search_page(path):
    """
    Parse one saved Matsne search-result HTML page.
    """

    logger.info(
        "Parsing search page: %s",
        path
    )

    html = path.read_text(
        encoding="utf-8"
    )

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    result_items = soup.select(
        "div.document-search-result-item"
    )

    documents = []

    for item in result_items:

        link = item.select_one(
            ".panel-heading a"
        )

        if not link:
            continue

        title = clean_text(
            link.get_text(
                " ",
                strip=True
            )
        )

        href = link.get(
            "href",
            ""
        )

        if href.startswith("/"):
            url = BASE_URL + href
        else:
            url = href

        document_id = extract_document_id(
            url
        )

        if not document_id:
            continue

        metadata_items = item.select(
            ".panel-body ul li"
        )

        metadata = []

        for li in metadata_items:

            value = clean_text(
                li.get_text(
                    " ",
                    strip=True
                )
            )

            if value and value != "●":
                metadata.append(value)

        document_type = (
            metadata[0]
            if len(metadata) > 0
            else None
        )

        issuing_authority = (
            metadata[1]
            if len(metadata) > 1
            else None
        )

        document_number = (
            metadata[2]
            if len(metadata) > 2
            else None
        )

        signing_date = (
            metadata[3]
            if len(metadata) > 3
            else None
        )

        documents.append({
            "document_id": document_id,
            "title": title,
            "url": url,
            "language": "ka",
            "document_type": document_type,
            "issuing_authority": issuing_authority,
            "document_number": document_number,
            "signing_date": signing_date,
        })

    return documents


# ============================================================
# DOCUMENT PARSER
# ============================================================

def parse_document(
    html,
    metadata
):
    """
    Extract structured document parts
    from Matsne's #maindoc.

    Supports:
    1. Modern Matsne documents with *_Content elements.
    2. Legacy/Word-style Matsne documents where
       legal text is directly contained in #maindoc.
    """

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

    # ========================================================
    # 1. MODERN MATSNE STRUCTURE
    # ========================================================

    content_elements = maindoc.select(
        '[id$="_Content"]'
    )

    if content_elements:

        for part in content_elements:

            part_id = part.get(
                "id"
            )

            text = clean_text(
                part.get_text(
                    " ",
                    strip=True
                )
            )

            if not part_id:
                continue

            # Remove "_Content"
            structure_id = part_id[
                :-len("_Content")
            ]

            components = structure_id.split(
                ";"
            )

            components = [
                c
                for c in components
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

    # ========================================================
    # 2. LEGACY / WORD-STYLE STRUCTURE
    # ========================================================

    else:

        logger.info(
            "No *_Content elements found; "
            "using legacy #maindoc parser."
        )

        paragraphs = maindoc.select(
            "p"
        )

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

            # Try to preserve the original
            # Matsne/Word paragraph class.
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
# COLLECT DOCUMENT LIST
# ============================================================

all_documents = []

for page_number in range(
    START_PAGE,
    END_PAGE + 1
):

    page_path = (
        SEARCH_PAGES_DIR
        / f"page_{page_number}.html"
    )

    if not page_path.exists():

        logger.warning(
            "Search page does not exist: %s",
            page_path
        )

        continue

    documents = parse_search_page(
        page_path
    )

    logger.info(
        "Page %s: %s documents",
        page_number,
        len(documents)
    )

    all_documents.extend(
        documents
    )


# ============================================================
# REMOVE DUPLICATES
# ============================================================

unique_documents = {}

for document in all_documents:

    unique_documents[
        document["document_id"]
    ] = document

all_documents = list(
    unique_documents.values()
)

logger.info(
    "Unique documents: %s",
    len(all_documents)
)


# ============================================================
# DOWNLOAD + PARSE DOCUMENTS
# ============================================================

failed_documents = []

existing_ids = set()

if OUTPUT_FILE.exists():

    with OUTPUT_FILE.open(
        "r",
        encoding="utf-8"
    ) as f:

        for line in f:

            try:

                document = json.loads(
                    line
                )

                existing_ids.add(
                    document["document_id"]
                )

            except json.JSONDecodeError:
                continue


with OUTPUT_FILE.open(
    "a",
    encoding="utf-8"
) as output:

    for index, metadata in enumerate(
        all_documents,
        start=1
    ):

        document_id = metadata[
            "document_id"
        ]

        if document_id in existing_ids:

            logger.info(
                "[%s/%s] Already processed: %s",
                index,
                len(all_documents),
                document_id
            )

            continue

        logger.info(
            "[%s/%s] Downloading %s",
            index,
            len(all_documents),
            document_id
        )

        raw_path = (
            RAW_DOCUMENTS_DIR
            / f"{document_id}.html"
        )

        try:

            response = session.get(
                metadata["url"],
                timeout=TIMEOUT
            )

            response.raise_for_status()

            html = response.text

            # Save raw HTML.
            raw_path.write_text(
                html,
                encoding="utf-8"
            )

            document = parse_document(
                html,
                metadata
            )

            output.write(
                json.dumps(
                    document,
                    ensure_ascii=False
                )
                + "\n"
            )

            output.flush()

            logger.info(
                "  parts=%s text=%s chars",
                document["part_count"],
                len(document["text"])
            )

        except Exception as exc:

            logger.exception(
                "Failed: %s",
                document_id
            )

            failed_documents.append({
                "document_id": document_id,
                "url": metadata["url"],
                "error": str(exc),
            })

        time.sleep(
            REQUEST_DELAY
        )


# ============================================================
# SAVE FAILURES
# ============================================================

FAILED_FILE.write_text(
    json.dumps(
        failed_documents,
        ensure_ascii=False,
        indent=2
    ),
    encoding="utf-8"
)

logger.info(
    "Finished."
)

logger.info(
    "Processed documents: %s",
    len(all_documents)
)

logger.info(
    "Failed documents: %s",
    len(failed_documents)
)