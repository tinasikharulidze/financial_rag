"""ID and path builders, Georgian ordinal resolver, ordering keys.

Path convention (see docs/KG_ARCHITECTURE.md section 3.1)::

    art6            article 6                 art9_1     article 9^1
    art6/p4         paragraph 4 of article 6  art6/p4.1  paragraph 4^1
    art6/p5/sბ      sub-point ბ               art6/p5/sბ.ე  nested sub-point ბ.ე
    ord3            article 3 of the approving order (not part of the regulation text)
    annex2          an annex of the regulation

Node ID formats: ``act:<doc>``, ``amend:<doc>``, ``prov:<act>/<path>``, ``pv:<act>/<path>@<date>``,
``obl:<act>/<path>#<n>@<date>``, ``ent:<name>``, ``ext:<key>``, ``doc:<code>@<ver>``, ``cl:<doc>/<section>``.
"""
from __future__ import annotations

import re

GEO = "აბგდევზთიკლმნოპჟრსტუფქღყშჩცძწჭხჯჰ"  # Georgian alphabet in Matsne order (33 letters)
_GEO_IDX = {c: i + 1 for i, c in enumerate(GEO)}

ORD_WORDS = {
    "პირველ": 1, "მეორე": 2, "მესამე": 3, "მეოთხე": 4, "მეხუთე": 5, "მეექვსე": 6,
    "მეშვიდე": 7, "მერვე": 8, "მეცხრე": 9, "მეათე": 10,
}
ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}

_SUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")

# one ordinal: "მე-13", "62-ე", "29", "პირველ(ი|ს|ის)", "8^1", "9¹"
RE_ORD = re.compile(
    r"(?:მე-(?P<me>\d+)|(?P<ne>\d+)-ე|(?P<n>\d+)|(?P<w>პირველ)\w*)(?:\s*[\^]\s*(?P<sup>\d+))?(?:-ე)?"
)


def norm_sup(s: str) -> str:
    """Normalise unicode superscripts to the ``^`` convention."""
    out = []
    for ch in s:
        if ch in "⁰¹²³⁴⁵⁶⁷⁸⁹":
            out.append("^" + ch.translate(_SUP))
        else:
            out.append(ch)
    return re.sub(r"\^(\d)\^(\d)", r"^\1\2", "".join(out))


def parse_ordinal(token: str):
    """'მე-13' -> (13, None); '8^1' -> (8, 1); 'პირველი' -> (1, None); None if no ordinal."""
    token = norm_sup(token.strip())
    m = RE_ORD.fullmatch(token) or RE_ORD.match(token)
    if not m:
        for w, v in ORD_WORDS.items():
            if token.startswith(w):
                return (v, None)
        return None
    if m.group("w"):
        n = 1
    else:
        n = int(m.group("me") or m.group("ne") or m.group("n"))
    sup = int(m.group("sup")) if m.group("sup") else None
    return (n, sup)


def find_ordinals(text: str):
    """All ordinals in a text span, in order, including simple ranges 'მე-3–მე-5' and 'a, b და c'."""
    text = norm_sup(text)
    found = []
    for m in RE_ORD.finditer(text):
        if m.group("w"):
            found.append((1, None))
            continue
        n = int(m.group("me") or m.group("ne") or m.group("n"))
        found.append((n, int(m.group("sup")) if m.group("sup") else None))
    return found


def roman_to_int(s: str) -> int | None:
    s = s.strip().upper()
    if not s or any(c not in ROMAN for c in s):
        return None
    total = 0
    for i, c in enumerate(s):
        v = ROMAN[c]
        if i + 1 < len(s) and ROMAN[s[i + 1]] > v:
            total -= v
        else:
            total += v
    return total


# ---------------------------------------------------------------- path builders
def art_seg(n: int, sup: int | None = None) -> str:
    return f"art{n}" + (f"_{sup}" if sup else "")


def para_seg(n: int, sup: int | None = None) -> str:
    return f"p{n}" + (f".{sup}" if sup else "")


def sub_seg(letter: str, sup: int | None = None, sub: str | None = None, sub_sup: int | None = None) -> str:
    """Sub-point segment: sა, sა_1, sბ.ე, sბ.ბ_1 (nested letter after the dot, superscript after underscore)."""
    s = "s" + letter + (f"_{sup}" if sup and not sub else "")
    if sub:
        s += "." + sub + (f"_{sub_sup}" if sub_sup else "")
    return s


def join_path(*segs: str | None) -> str:
    return "/".join(s for s in segs if s)


def prov_id(act_doc: str, path: str) -> str:
    return f"prov:{act_doc}/{path}"


def pv_id(act_doc: str, path: str, valid_from: str) -> str:
    return f"pv:{act_doc}/{path}@{valid_from}"


def act_id(doc: str) -> str:
    return f"act:{doc}"


def amend_id(doc: str) -> str:
    return f"amend:{doc}"


def parse_path(path: str):
    """Split a path into (article, paragraph, sub) segments (each may be None)."""
    parts = path.split("/")
    art = parts[0]
    para = next((p for p in parts[1:] if p.startswith("p")), None)
    sub = next((p for p in parts[1:] if p.startswith("s")), None)
    return art, para, sub


# ---------------------------------------------------------------- ordering keys
def _num_key(seg: str | None, prefix: str):
    if not seg:
        return (0, 0)
    m = re.match(rf"{prefix}(\d+)(?:[_.](\d+))?", seg)
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2) or 0))


def _sub_key(seg: str | None):
    if not seg:
        return (0, 0, 0, 0)
    m = re.match(r"s(.)(?:_(\d+))?(?:\.(.)(?:_(\d+))?)?", seg)
    if not m:
        return (0, 0, 0, 0)
    return (_GEO_IDX.get(m.group(1), 99), int(m.group(2) or 0),
            _GEO_IDX.get(m.group(3), 0) if m.group(3) else 0, int(m.group(4) or 0))


def path_sort_key(path: str):
    """Natural order of a provision path: order articles, annexes last, then paragraph, then sub-point."""
    first = path.split("/")[0]
    if first.startswith("ord"):
        group = 0
    elif first.startswith("annex"):
        group = 2
    else:
        group = 1
    art, para, sub = parse_path(path)
    pref = "ord" if group == 0 else ("annex" if group == 2 else "art")
    return (group, _num_key(art, pref), _num_key(para, "p"), _sub_key(sub))


def display_path(path: str) -> str:
    """Human label in Georgian, e.g. art6/p4.1/sბ -> 'მუხლი 6, პუნქტი 4¹, ქვეპუნქტი ბ'."""
    sup = "⁰¹²³⁴⁵⁶⁷⁸⁹"

    def fmt(n, s):
        return str(n) + ("".join(sup[int(d)] for d in str(s)) if s else "")

    out = []
    for seg in path.split("/"):
        if seg.startswith("art"):
            n, s = _num_key(seg, "art")
            out.append("მუხლი " + fmt(n, s))
        elif seg.startswith("ord"):
            n, s = _num_key(seg, "ord")
            out.append("ბრძანების მუხლი " + str(n))
        elif seg.startswith("annex"):
            n, s = _num_key(seg, "annex")
            out.append("დანართი " + str(n))
        elif seg.startswith("p"):
            n, s = _num_key(seg, "p")
            out.append("პუნქტი " + fmt(n, s))
        elif seg.startswith("s"):
            out.append("ქვეპუნქტი " + seg[1:].replace("_", "^"))
    return ", ".join(out)
