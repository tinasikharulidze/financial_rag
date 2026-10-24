# Goni chat evaluation

20 scripted conversations (5 per use case: redrafting, challenging a flag, applicability, graph exploration), 2-3 messages each,
in `chat_cases.csv`. The documents were picked at random from the bank's nine non-capital-adequacy documents.

```
python eval/chat/run_chat_eval.py --models flash --dry-run   # the plan
python eval/chat/run_chat_eval.py --models flash             # heaviest conversations first; stops by itself when the quota is used up
python eval/chat/run_chat_eval.py --models gemma             # later
```

Run the same command again to continue. `--redo-failed`, `--force`, `--only RD,CH`, `--ids RD-01`, `--limit 5` are in the script's `--help`.
Results are written to `eval/chat/results/<models>/` (`summary.csv`, `transcripts.md`, `results.jsonl`).
The runner works on a copy of `data/kg/kg.sqlite`; the real database is never written to. All checks are automatic: no second
model grades the answers, so the only quota used is the chatbot's own.
