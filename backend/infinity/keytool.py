"""Read-only tooling to investigate unreadable stored credentials. It NEVER writes to the database and NEVER prints keys.

    python -m infinity.keytool status
    printf '<label>\\t<candidate key>\\n...' | python -m infinity.keytool try-keys

`try-keys` reads candidate APP_SECRET_KEY values (one per line, optionally "label<TAB>key") from stdin and tests every
candidate - as given, stripped of whitespace/CR/BOM, and with surrounding quotes removed - against each encrypted record
using the application's own SecretBox. Output has labels and 12-hex fingerprints only. Exit 0 if a candidate decrypts the
`llm` record (or, when there is none, every record), 1 otherwise.
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from .config import get_config
from .crypto import SecretBox, key_fingerprint
from .db import session_scope
from .models import AppSetting, Source


def _records() -> list[tuple[str, str]]:
    with session_scope() as db:
        out = []
        row = db.get(AppSetting, "llm")
        if row and row.secret_enc:
            out.append(("llm (AI provider keys)", row.secret_enc))
        for src in db.scalars(select(Source).where(Source.secret_enc.is_not(None))):
            out.append((f"source #{src.id} {src.name!r}", src.secret_enc))
        return out


def variants(raw: str) -> list[tuple[str, str]]:
    """Plausible effective values of one configured key (BOM/CR/whitespace/quotes differ between .env parsers)."""
    seen, out = set(), []
    stripped = raw.replace("\ufeff", "").strip()
    unquoted = stripped[1:-1] if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'" else stripped
    for name, value in (("as-is", raw.rstrip("\n")), ("stripped", stripped), ("unquoted", unquoted)):
        if value and value not in seen and len(value) >= 16:
            seen.add(value)
            out.append((name, value))
    return out


def test_candidates(candidates: list[tuple[str, str]], records: list[tuple[str, str]]) -> list[dict]:
    results = []
    for label, raw in candidates:
        for variant, key in variants(raw):
            box = SecretBox(key)
            ok = {name: box.can_decrypt(tok) for name, tok in records}
            results.append({"label": label, "variant": variant, "fingerprint": key_fingerprint(key), "ok": ok})
    return results


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    records = _records()
    current = get_config().secret_key
    cur_box = SecretBox(current)
    if cmd == "status":
        print(f"current APP_SECRET_KEY: length {len(current)}, fingerprint {key_fingerprint(current)}")
        if not records:
            print("no encrypted records stored")
        for name, tok in records:
            print(f"  {name}: {'readable' if cur_box.can_decrypt(tok) else 'UNREADABLE with the current key'} (token length {len(tok)})")
        return 0 if all(cur_box.can_decrypt(t) for _, t in records) else 1
    if cmd == "try-keys":
        candidates = []
        for line in sys.stdin.read().splitlines():
            if not line.strip():
                continue
            label, _, key = line.partition("\t") if "\t" in line else (f"candidate {len(candidates) + 1}", "", line)
            candidates.append((label, key))
        if not records:
            print("no encrypted records stored; nothing to test against")
            return 1
        res = test_candidates(candidates, records)
        # collapse identical fingerprints
        best, matched = {}, False
        for r in res:
            n_ok = sum(r["ok"].values())
            key = (r["fingerprint"],)
            best.setdefault(key, {"fp": r["fingerprint"], "labels": [], "variant": r["variant"], "n": n_ok, "total": len(r["ok"]), "llm": r["ok"].get("llm (AI provider keys)")})
            if r["label"] not in best[key]["labels"]:
                best[key]["labels"].append(r["label"])
        print(f"tested {len(candidates)} candidate line(s) -> {len(best)} distinct key value(s) against {len(records)} encrypted record(s); current key fingerprint {key_fingerprint(current)}")
        for b in sorted(best.values(), key=lambda x: -x["n"]):
            status = "MATCH" if b["n"] == b["total"] else ("partial" if b["n"] else "no match")
            if b["n"]:
                matched = matched or b["llm"] is not False
            print(f"  fp {b['fp']}  [{status}] {b['n']}/{b['total']} records  (variant: {b['variant']})  from: {'; '.join(b['labels'])[:200]}")
        return 0 if matched else 1
    print("usage: python -m infinity.keytool status | try-keys", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
