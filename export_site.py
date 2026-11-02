"""Write the website's data files (docs/data/india.json, us.json) from the latest scan output."""
import json, pathlib
root = pathlib.Path(__file__).parent
for m, tab in (("india", "Board - India"), ("us", "Board - US")):
    files = sorted((root / "data" / m / "sheet_rows").glob("*.json"))
    if not files: continue
    last = json.loads(files[-1].read_text())
    head = last["headers"]["Changes"]
    changes = [r for f in files[-10:] for r in json.loads(f.read_text())["rows"].get("Changes", [])]
    out = root / "docs" / "data"; out.mkdir(parents=True, exist_ok=True)
    (out / f"{m}.json").write_text(json.dumps({"board": last["replace"][tab]["values"], "changes": [head] + changes}, default=str))
