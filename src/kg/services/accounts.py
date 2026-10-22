"""Accounts, companies and company roles (docs/KG_V2_PLAN.md section 3). stdlib only (pbkdf2 password hashes)."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets

from kg.config import now_iso
from kg.services.permissions import ENTITY, REGULATOR, PermissionDenied, require

USER_TYPES = (ENTITY, REGULATOR)
_ITER = 120_000
MIN_PASSWORD = 6

_GEO = {"ა": "a", "ბ": "b", "გ": "g", "დ": "d", "ე": "e", "ვ": "v", "ზ": "z", "თ": "t", "ი": "i", "კ": "k", "ლ": "l",
        "მ": "m", "ნ": "n", "ო": "o", "პ": "p", "ჟ": "zh", "რ": "r", "ს": "s", "ტ": "t", "უ": "u", "ფ": "p", "ქ": "k",
        "ღ": "gh", "ყ": "q", "შ": "sh", "ჩ": "ch", "ც": "ts", "ძ": "dz", "წ": "ts", "ჭ": "ch", "ხ": "kh", "ჯ": "j",
        "ჰ": "h"}


class AccountError(ValueError):
    """A registration / login problem the UI shows to the user (``code`` is a stable key for translations)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# ----------------------------------------------------------------------------------------------- passwords
def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, h = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iters))
        return algo == "pbkdf2_sha256" and hmac.compare_digest(dk.hex(), h)
    except Exception:  # noqa: BLE001
        return False


def slugify(name: str) -> str:
    s = "".join(_GEO.get(c, c) for c in name.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "co-" + hashlib.sha1(name.encode()).hexdigest()[:8]


# ----------------------------------------------------------------------------------------------- users
def public_user(row: dict | None) -> dict | None:
    if not row:
        return None
    return {k: row[k] for k in ("id", "email", "name", "user_type", "company_id", "created_at")}


def get_user(store, user_id: int) -> dict | None:
    return public_user(store.q1("SELECT * FROM users WHERE id=?", (user_id,)))


def company_of(store, user: dict) -> dict | None:
    if not user or not user.get("company_id"):
        return None
    return get_company(store, user["company_id"])


def register(store, *, email: str, name: str, password: str, user_type: str, entity_types: list[str] | None = None,
             company: dict | None = None, join_company: str | None = None) -> dict:
    """Create a user. A regulated-entity user either creates a company (``company`` = {name, entity_type,
    reg_number, props}) or joins an existing one (``join_company`` = company id or exact name)."""
    email = (email or "").strip().lower()
    if "@" not in email or len(email) < 5:
        raise AccountError("bad_email", "invalid e-mail")
    if len(password or "") < MIN_PASSWORD:
        raise AccountError("short_password", f"password must have at least {MIN_PASSWORD} characters")
    if not (name or "").strip():
        raise AccountError("no_name", "name is required")
    if user_type not in USER_TYPES:
        raise AccountError("bad_user_type", "user type must be regulator or entity")
    if store.q1("SELECT 1 FROM users WHERE email=?", (email,)):
        raise AccountError("email_taken", "e-mail already registered")
    company_id = None
    if user_type == ENTITY:
        if company:
            company_id = create_company(store, entity_types=entity_types, **company)["id"]
        elif join_company:
            c = store.q1("SELECT id FROM companies WHERE id=? OR name=?", (join_company, join_company))
            if not c:
                raise AccountError("no_company", "company not found")
            company_id = c["id"]
        else:
            raise AccountError("company_required", "a regulated-entity user needs a company")
    uid = store.x("INSERT INTO users(email,name,password_hash,user_type,company_id,created_at) VALUES(?,?,?,?,?,?)",
                  (email, name.strip(), hash_password(password), user_type, company_id, now_iso()))
    return get_user(store, uid)


def authenticate(store, email: str, password: str) -> dict | None:
    row = store.q1("SELECT * FROM users WHERE email=?", ((email or "").strip().lower(),))
    if row and verify_password(password or "", row["password_hash"]):
        return public_user(row)
    return None


# ----------------------------------------------------------------------------------------------- companies
def create_company(store, *, name: str, entity_type: str, reg_number: str | None = None, props: dict | None = None,
                   entity_types: list[str] | None = None, company_id: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise AccountError("no_company_name", "company name is required")
    if entity_types and entity_type not in entity_types:
        raise AccountError("bad_entity_type", f"entity type must be one of {entity_types}")
    base = company_id or slugify(name)
    cid, n = base, 1
    while store.q1("SELECT 1 FROM companies WHERE id=?", (cid,)):
        if company_id:
            raise AccountError("company_exists", "company id already exists")
        n += 1
        cid = f"{base}-{n}"
    store.x("INSERT INTO companies(id,name,entity_type,reg_number,props,created_at) VALUES(?,?,?,?,?,?)",
            (cid, name, entity_type, reg_number, json.dumps(props or {}, ensure_ascii=False), now_iso()))
    return get_company(store, cid)


def get_company(store, company_id: str) -> dict | None:
    c = store.q1("SELECT * FROM companies WHERE id=?", (company_id,))
    if c:
        c["props"] = json.loads(c["props"] or "{}")
    return c


def list_companies(store, actor: dict, query: str | None = None) -> list[dict]:
    """Regulator company search (by name or registration number)."""
    require(actor, "company.search")
    q = f"%{(query or '').strip().lower()}%"
    rows = store.q("SELECT * FROM companies WHERE lower(name) LIKE ? OR lower(coalesce(reg_number,'')) LIKE ? "
                   "OR lower(id) LIKE ? ORDER BY name", (q, q, q))
    for r in rows:
        r["props"] = json.loads(r["props"] or "{}")
    return rows


def update_company(store, actor: dict, company_id: str, *, name: str | None = None, entity_type: str | None = None,
                   reg_number: str | None = None, props: dict | None = None, entity_types: list[str] | None = None):
    require(actor, "company.edit", company_id)
    c = get_company(store, company_id)
    if c is None:
        raise AccountError("no_company", "company not found")
    if entity_type and entity_types and entity_type not in entity_types:
        raise AccountError("bad_entity_type", f"entity type must be one of {entity_types}")
    store.x("UPDATE companies SET name=?, entity_type=?, reg_number=?, props=? WHERE id=?",
            (name or c["name"], entity_type or c["entity_type"], reg_number if reg_number is not None else c["reg_number"],
             json.dumps({**c["props"], **(props or {})}, ensure_ascii=False), company_id))
    return get_company(store, company_id)


# ----------------------------------------------------------------------------------------------- roles
def add_role(store, actor: dict, company_id: str, name: str) -> dict:
    require(actor, "company.edit", company_id)
    name = (name or "").strip()
    if not name:
        raise AccountError("no_role_name", "role name is required")
    store.x("INSERT OR IGNORE INTO company_roles(company_id,name) VALUES(?,?)", (company_id, name))
    return store.q1("SELECT * FROM company_roles WHERE company_id=? AND name=?", (company_id, name))


def remove_role(store, actor: dict, company_id: str, role_id: int):
    require(actor, "company.edit", company_id)
    if not store.q1("SELECT 1 FROM company_roles WHERE id=? AND company_id=?", (role_id, company_id)):
        raise AccountError("no_role", "role not found")
    store.x("DELETE FROM user_roles WHERE role_id=?", (role_id,))
    store.x("DELETE FROM company_roles WHERE id=?", (role_id,))


def list_roles(store, company_id: str) -> list[dict]:
    return store.q("SELECT * FROM company_roles WHERE company_id=? ORDER BY name", (company_id,))


def assign_role(store, actor: dict, company_id: str, user_id: int, role_id: int):
    require(actor, "company.edit", company_id)
    u = store.q1("SELECT company_id FROM users WHERE id=?", (user_id,))
    r = store.q1("SELECT company_id FROM company_roles WHERE id=?", (role_id,))
    if not u or not r or u["company_id"] != company_id or r["company_id"] != company_id:
        raise PermissionDenied("user and role must belong to the company")
    store.x("INSERT OR IGNORE INTO user_roles(user_id,role_id) VALUES(?,?)", (user_id, role_id))


def unassign_role(store, actor: dict, company_id: str, user_id: int, role_id: int):
    require(actor, "company.edit", company_id)
    store.x("DELETE FROM user_roles WHERE user_id=? AND role_id=?", (user_id, role_id))


def members(store, actor: dict, company_id: str) -> list[dict]:
    """Users of a company with their role names (visible to the company's users and to regulators)."""
    require(actor, "task.view", company_id)
    rows = store.q("SELECT id,email,name,user_type,company_id,created_at FROM users WHERE company_id=? ORDER BY name",
                   (company_id,))
    for r in rows:
        r["roles"] = [x["name"] for x in store.q(
            "SELECT cr.name FROM user_roles ur JOIN company_roles cr ON cr.id=ur.role_id WHERE ur.user_id=?",
            (r["id"],))]
    return rows
