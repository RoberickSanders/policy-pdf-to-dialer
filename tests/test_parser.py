from datetime import date
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import parse_prior_coverage as p

EX = Path(__file__).resolve().parents[1] / "examples" / "input"

def test_sample_book():
    pol = [x for f in sorted(EX.glob("*.txt")) for x in p.parse_file(f)]
    assert len(pol) == 4
    rows = p.build_rows(pol, date(2026, 1, 15), "T")
    assert len(rows) == 3                                  # Jane + John merged into one household
    sam = next(r for r in rows if r["Name"] == "Sam Demo")
    assert sam["Mobile #"] == "(555) 010-0303"             # dummy primary dropped, secondary used
    jane = next(r for r in rows if r["Name"] == "Jane Q Sample")
    assert jane["Script Type"] == "Under-65 Review + Recommended"
    assert jane["Lapsed Policies"] == 1
