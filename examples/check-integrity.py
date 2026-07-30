# /// script
# requires-python = ">=3.11"
# ///
"""Verifie la signature de chaque fichier telecharge; supprime les faux (pages HTML)."""
import io
import sys
from collections import Counter
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
DEST = Path(r"C:\Users\clara\Desktop\breslev-pdf")
DELETE = "--delete" in sys.argv

SIG = {".pdf": b"%PDF", ".docx": b"PK", ".doc": None}  # .doc: signatures multiples, on ne teste pas

bad, stats = [], Counter()
for p in sorted(DEST.iterdir()):
    if not p.is_file() or p.suffix.lower() not in SIG:
        continue
    stats[p.suffix.lower()] += 1
    exp = SIG[p.suffix.lower()]
    head = p.open("rb").read(8)
    if p.stat().st_size < 1024 or (exp and not head.startswith(exp)):
        bad.append((p, head[:20]))

print("fichiers verifies:", dict(stats))
print("suspects:", len(bad))
for p, head in bad[:10]:
    print(f"  {p.stat().st_size:8} o  {head!r}  {p.name[:70]}")

if bad and DELETE:
    for p, _ in bad:
        p.unlink()
    print(f"\nsupprimes: {len(bad)}")
elif bad:
    print("\n(relancer avec --delete pour supprimer)")
