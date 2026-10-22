"""The command line end to end (fake model): every v2 command, error codes, resumability and model switching."""
import json
import os
from pathlib import Path

import pytest
import yaml
from fake_llm import FakeGemini

import kg.cli as cli
from kg.llm import LLM as RealLLM

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def run(kg, tmp_path, monkeypatch, capsys):
    cfg, store = kg
    cfg_file = tmp_path / "kg.yaml"
    cfg_file.write_text(yaml.safe_dump(json.loads(json.dumps(dict(cfg))), allow_unicode=True), encoding="utf-8")
    fake = FakeGemini()
    monkeypatch.setattr(cli, "LLM", lambda c, s, run_id=None: RealLLM(c, s, run_id, backend=fake,
                                                                        cache_dir=tmp_path / "llmcache"))

    def go(*argv):
        capsys.readouterr()
        code = cli.main(["--config", str(go.cfg_file), *argv])
        out = capsys.readouterr()
        try:
            data = json.loads(out.out)
        except ValueError:
            data = None
        return code, data, out.err
    go.fake, go.cfg_file, go.store = fake, cfg_file, store
    return go


def test_full_cli_flow(run):
    assert run("migrate")[1]["tables"]["users"] == 0
    code, data, _ = run("extract-obligations", "--dry-run")
    assert code == 0 and data["result"]["requests_estimated"] > 5 and sum(run.fake.calls.values()) == 0
    code, data, _ = run("extract-obligations")
    assert code == 0 and data["result"]["acts"]["4964423"]["obligations"] > 40
    calls = sum(run.fake.calls.values())
    run("extract-obligations")
    assert sum(run.fake.calls.values()) == calls                           # free the second time

    code, data, _ = run("seed-demo")
    assert code == 0 and len(data["result"]["documents"]) == 7
    version = next(d for d in data["result"]["documents"] if "POL-RM-014" in d)

    code, data, err = run("pipeline", version, "--as", "bank@demo.ge", "--now", "2025-01-01")
    assert code == 0 and "not confirmed" in data["result"]["stopped"]      # the user must confirm the profile first
    code, data, _ = run("map", version, "--dry-run", "--now", "2025-01-01")
    assert code == 0 and data["result"]["requests_total"] > 0
    code, data, _ = run("pipeline", version, "--as", "bank@demo.ge", "--confirm-profile", "--domain", "model_risk",
                        "--now", "2025-01-01")
    assert code == 0 and data["result"]["tasks"]["created"] > 0
    n = sum(run.fake.calls.values())
    code, data, _ = run("pipeline", version, "--as", "bank@demo.ge", "--confirm-profile", "--now", "2025-01-01")
    assert code == 0 and sum(run.fake.calls.values()) == n                  # nothing new asked

    code, rows, _ = run("tasks", "--as", "bank@demo.ge")
    assert code == 0 and rows and rows[0]["score"] >= rows[-1]["score"]
    code, rows2, _ = run("tasks", "--as", "nbg@demo.ge", "--company", "bank-a", "--priority", rows[0]["priority"])
    assert code == 0 and rows2
    code, ex, _ = run("explain-task", rows[0]["key"], "--as", "nbg@demo.ge")
    assert code == 0 and ex["ranking"]["score"] == rows[0]["score"]

    code, data, _ = run("recompute")
    assert code == 0 and "synced" in data["result"] and sum(run.fake.calls.values()) == n

    # accept everything, build the revised text, publish
    from kg.services import decisions as D
    from kg.services.accounts import get_user
    owner = get_user(run.store, run.store.q1("SELECT id FROM users WHERE email='bank@demo.ge'")["id"])
    for r in rows[:2]:
        D.accept(run.store, owner, r["key"])
    code, data, _ = run("revise", version, "--as", "bank@demo.ge")
    assert code == 0 and data["result"]["changed_clauses"] + data["result"]["added"] >= 1
    code, data, err = run("revise", version, "--as", "nbg@demo.ge")
    assert code == 1 and "may not" in err                                   # a regulator cannot build revised text

    code, st, _ = run("status")
    assert code == 0 and st["companies"] == 1 and st["tasks"]["open"] > 0 and st["llm"]["models"]


def test_upload_profile_and_publish_via_cli(run, tmp_path):
    run("extract-obligations")
    run("seed-demo", "--no-docs")
    pdf = tmp_path / "policy.pdf"
    pdf.write_bytes((ROOT / "model_risk_company_docs" / "D1_Model_Risk_Management_Policy_POL-RM-014.pdf").read_bytes())
    code, data, _ = run("upload", str(pdf), "--as", "bank@demo.ge")
    assert code == 0 and data["result"]["status"] == "ok"
    version = data["result"]["version_id"]
    assert run("upload", str(pdf), "--as", "bank@demo.ge")[1]["result"]["status"] == "duplicate"
    code, data, _ = run("profile", version, "--as", "bank@demo.ge", "--confirm", "--domain", "model_risk")
    assert code == 0 and data["result"]["confirmed"] and data["result"]["domains"] == ["model_risk"]
    code, data, _ = run("publish", version, "--as", "bank@demo.ge")
    assert code == 0 and data["result"]["status"] == "published"
    assert run("publish", version, "--as", "bank@demo.ge")[0] == 1


def test_errors_and_budget_codes(run):
    code, _, err = run("tasks", "--as", "nobody@x.ge")
    assert code == 1 and "unknown user" in err
    code, _, err = run("create-user", "--email", "bad", "--name", "n", "--password", "secret1", "--type", "regulator")
    assert code == 1
    code, data, _ = run("create-user", "--email", "r2@x.ge", "--name", "R", "--password", "secret1", "--type", "regulator")
    assert code == 0 and data["result"]["user_type"] == "regulator"
    # a tiny daily budget stops the extraction cleanly with exit code 2; finished batches stay
    cfg = yaml.safe_load(run.cfg_file.read_text(encoding="utf-8"))
    cfg["llm"]["daily_request_limit"] = 3
    cfg["llm"]["models"] = ["m/only"]
    run.cfg_file = run.cfg_file.with_name("kg_small_budget.yaml")        # load_config caches per file name
    run.cfg_file.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    code, _, err = run("extract-obligations")
    assert code == 2 and "budget exhausted" in err.lower()
    assert run.fake.calls["extract_obligations"] == 3


def test_single_model_switch_is_config_only(run):
    cfg = yaml.safe_load(run.cfg_file.read_text(encoding="utf-8"))
    cfg["llm"]["single_model"] = "gemini/some-other-model"
    cfg["llm"]["daily_request_limit"] = None
    run.cfg_file.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    code, st, _ = run("status")
    assert st["llm"]["single_model"] == "gemini/some-other-model" and st["llm"]["models"] == ["gemini/some-other-model"]
    assert st["llm"]["daily_request_limit"] is None and st["llm"]["budget_remaining"] is None
    code, data, _ = run("smoke-llm")
    assert code == 0 and data["result"]["model"] == "gemini/some-other-model" and data["result"]["daily_limit"] is None


def test_count_tokens_and_legacy_commands_still_exist(run):
    code, data, _ = run("count-tokens")
    assert code == 0 and len(data["documents"]) == 7
    assert all(d["whole_document_mode"] == (d["tokens"] <= data["whole_doc_max_tokens"]) for d in data["documents"])
    for legacy in ("map-coverage", "check-doc", "check-upload", "replay", "ingest-company-docs"):
        assert legacy in cli.build_parser().format_help()


def test_rebuild_graph_is_idempotent_and_obligations_come_back_for_free(run):
    from collections import Counter
    run("extract-obligations")
    before = Counter(n["type"] for n in run.store.nodes())
    ids = sorted(n["id"] for n in run.store.nodes("Obligation"))
    calls = sum(run.fake.calls.values())
    code, data, _ = run("rebuild-graph")
    assert code == 0
    mid = Counter(n["type"] for n in run.store.nodes())
    for t in ("ProvisionVersion", "Provision", "Act", "AmendingAct", "ExternalProvision"):
        assert mid[t] == before[t], t
    code, _, _ = run("extract-obligations")
    assert code == 0 and sum(run.fake.calls.values()) == calls
    assert sorted(n["id"] for n in run.store.nodes("Obligation")) == ids


def test_check_command_runs_the_background_job(run):
    run("extract-obligations")
    code, data, _ = run("seed-demo")
    version = next(d for d in data["result"]["documents"] if "EG-01" in d)
    code, data, err = run("check", version, "--as", "bank@demo.ge")
    assert code == 1 and "profile" in err                       # not confirmed yet: refused with a message
    run("profile", version, "--as", "bank@demo.ge", "--confirm")
    code, data, _ = run("check", version, "--as", "bank@demo.ge")
    assert code == 0 and data["result"]["tasks"]["created"] >= 0
    j = run.store.q1("SELECT * FROM check_jobs WHERE version_id=?", (version,))
    assert j["status"] == "done" and j["pid"] == os.getpid()
