import requests
from bs4 import BeautifulSoup
from pathlib import Path
import time


# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://matsne.gov.ge"
SEARCH_URL = f"{BASE_URL}/ka/document/search"

MAX_PAGES = 11
RESULTS_PER_PAGE = 100
REQUEST_DELAY = 1.0

OUTPUT_DIR = Path("matsne_data")
RAW_SEARCH_DIR = OUTPUT_DIR / "search_pages"

RAW_SEARCH_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ka,en;q=0.8",
})


# ============================================================
# LOAD SEARCH PAGE
# ============================================================

def get_search_form():
    response = session.get(
        SEARCH_URL,
        timeout=30
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    # Try the known form ID first
    form = soup.select_one("#_document_search_form")

    # Fallback: find form containing the query field
    if form is None:
        for candidate in soup.find_all("form"):
            if candidate.find(attrs={"name": "query"}):
                form = candidate
                break

    if form is None:
        raise RuntimeError(
            "Could not find the Matsne document search form."
        )

    return response, soup, form


# ============================================================
# EXTRACT DEFAULT FORM PARAMETERS
# ============================================================

def get_form_defaults(form):
    """
    Extract the existing values from the Matsne search form.

    This allows us to preserve hidden fields and default
    form settings rather than constructing everything manually.
    """

    params = {}

    # --------------------------------------------------------
    # INPUTS
    # --------------------------------------------------------

    for inp in form.find_all("input"):

        name = inp.get("name")

        if not name:
            continue

        input_type = inp.get("type", "text")

        # Don't submit buttons
        if input_type in {
            "submit",
            "button",
            "reset",
            "file"
        }:
            continue

        # Checkboxes / radio buttons only submit if selected
        if input_type in {
            "checkbox",
            "radio"
        }:
            if inp.has_attr("checked"):
                params[name] = inp.get(
                    "value",
                    "on"
                )

        else:
            params[name] = inp.get(
                "value",
                ""
            )

    # --------------------------------------------------------
    # SELECTS
    # --------------------------------------------------------

    for select in form.find_all("select"):

        name = select.get("name")

        # matsne-language-switcher has no name
        # so it cannot be submitted as a normal form field
        if not name:
            continue

        selected = select.find(
            "option",
            selected=True
        )

        if selected:
            params[name] = selected.get(
                "value",
                ""
            )

        else:
            first = select.find("option")

            params[name] = (
                first.get("value", "")
                if first
                else ""
            )

    return params


# ============================================================
# LOAD FORM
# ============================================================

print("Loading Matsne search page...")

initial_response, initial_soup, form = get_search_form()

print(
    "Initial response:",
    initial_response.status_code
)

print(
    "Form:",
    form.get("id")
)


# ============================================================
# BUILD SEARCH PARAMETERS
# ============================================================

params = get_form_defaults(form)


# ------------------------------------------------------------
# SEARCH CONFIGURATION
# ------------------------------------------------------------

# Empty text query
params["query"] = ""

# Search target
params["query_target"] = "title"

# ------------------------------------------------------------
# DOCUMENT FILTER
# მხოლოდ ძირითადი (კონსოლიდირებული)
# ------------------------------------------------------------

params["type"] = "main"

# ------------------------------------------------------------
# DOCUMENT TOPIC
# ------------------------------------------------------------

params["label"] = (
    "ბიუჯეტი, საბანკო, საფინანსო საკითხები და გადასახადები, "
    "ლიცენზიები და ნებართვები, სახელმწიფო შესყიდვები, "
    "აუდიტორული საქმიანობა"
)

# ------------------------------------------------------------
# STATUS
# მოქმედი
# ------------------------------------------------------------

params["status"] = "active"

# No additional status
params["additional_status"] = ""

# ------------------------------------------------------------
# PAGINATION
# ------------------------------------------------------------

params["limit"] = str(RESULTS_PER_PAGE)

# Keep Matsne's default sorting
params["sort"] = "signingDate_desc"


# ============================================================
# PRINT SEARCH CONFIGURATION
# ============================================================

print("\nSearch configuration:")
print("---------------------")

print("Language: Georgian (/ka/)")
print("Document filter: მხოლოდ ძირითადი (კონსოლიდირებული)")
print("Document type value:", params["type"])
print("Topic:", params["label"])
print("Status: მოქმედი")
print("Status value:", params["status"])
print("Results per page:", params["limit"])
print("Pages:", MAX_PAGES)


# ============================================================
# DOWNLOAD SEARCH RESULTS
# ============================================================

for page in range(1, MAX_PAGES + 1):

    print(
        f"\nDownloading search page "
        f"{page}/{MAX_PAGES}..."
    )

    params["page"] = str(page)

    response = session.get(
        SEARCH_URL,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    # Save raw HTML
    output_file = (
        RAW_SEARCH_DIR /
        f"page_{page}.html"
    )

    output_file.write_text(
        response.text,
        encoding="utf-8"
    )

    print(
        "Status:",
        response.status_code
    )

    print(
        "URL:",
        response.url
    )

    print(
        "Saved:",
        output_file
    )

    # --------------------------------------------------------
    # Basic verification
    # --------------------------------------------------------

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    returned_form = soup.select_one(
        "#_document_search_form"
    )

    if returned_form:

        # Type
        type_select = returned_form.select_one(
            "#edit-type"
        )

        if type_select:
            selected = type_select.find(
                "option",
                selected=True
            )

            if selected:
                print(
                    "Returned document filter:",
                    selected.get_text(
                        " ",
                        strip=True
                    ),
                    "=",
                    selected.get("value")
                )

        # Status
        status_select = returned_form.select_one(
            "#edit-status"
        )

        if status_select:
            selected = status_select.find(
                "option",
                selected=True
            )

            if selected:
                print(
                    "Returned status:",
                    selected.get_text(
                        " ",
                        strip=True
                    ),
                    "=",
                    selected.get("value")
                )

        # Limit
        limit_select = returned_form.select_one(
            "#limit"
        )

        if limit_select:
            selected = limit_select.find(
                "option",
                selected=True
            )

            if selected:
                print(
                    "Returned limit:",
                    selected.get_text(
                        " ",
                        strip=True
                    ),
                    "=",
                    selected.get("value")
                )

    # --------------------------------------------------------
    # Don't hammer the public website
    # --------------------------------------------------------

    if page < MAX_PAGES:
        time.sleep(REQUEST_DELAY)


print("\nDone.")
print(
    f"Downloaded {MAX_PAGES} search pages "
    f"({MAX_PAGES * RESULTS_PER_PAGE} maximum results)."
)