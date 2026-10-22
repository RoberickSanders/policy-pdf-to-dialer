#!/usr/bin/env python3
"""Prior Coverage PDF -> dialer-ready CSV.

Turns policy-summary PDFs (one or many policies per file, wrapped across pages)
into one row per *household*, with the best phone first, a lead score, and the
call script to use. Built for a life insurance agency's daily lead book.

Rules that matter:
  * Never invent data. Every value comes from the document; unparseable blocks STOP the run.
  * Dummy phones like (000) 000-0000 are dropped.
  * People are merged into households when they share a phone or a street address.
  * Script type is chosen by explicit, testable rules (see script_type()).

Input: a folder of .pdf (read with `pdftotext -layout`) or .txt exports.
Output: dialer.csv + dialer.xlsx (if openpyxl is installed) + run summary JSON.
"""
from __future__ import annotations

import argparse, csv, json, re, subprocess, sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

NAME_START = re.compile(r"^([A-Z][A-Z0-9'\-\. ]+?)\s+\(Age:\s*(\d+)\)\s+Sex:([MF])", re.M)
PHONE_RE = re.compile(r"\((\d{3})\)\s*(\d{3})-(\d{4})")
DUMMY_PHONES = {"(000) 000-0000"}
CITY_STATE_ZIP = re.compile(r"([A-Z][A-Z .'-]+),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?")
# Trailing union/source tags that some exports glue onto the street line
TRAILING_TAG = re.compile(r"\s+(LOCAL\s+\d+|UNION\s+\S+|REF\s+\S+|TEAM\s+\d+)\s*$", re.I)
RECENT_MONTHS = 18
COLUMNS = ["Name", "Email", "Mobile #", "Other #", "Custom ID", "Lead Score", "Household Premium",
           "Policies", "Active Policies", "Lapsed Policies", "Household Status", "Adults",
           "Script Type", "Household Members", "Address", "Outcome", "Notes"]


class ParseError(SystemExit):
    pass


def fail(msg: str):
    raise ParseError("STOP: " + msg)


def read_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        out = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, check=True).stdout
        return out.replace("\x0c", "\n<<<PAGEBREAK>>>\n")
    return path.read_text(errors="replace")


def norm_phone(s: str | None) -> str:
    m = PHONE_RE.search(s or "")
    if not m:
        return ""
    p = f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
    return "" if p in DUMMY_PHONES else p


def title(s: str) -> str:
    return " ".join(w if w in {"APT", "PO"} else w.capitalize() for w in s.split()).replace("Apt", "Apt")


def parse_block(block: str, src: str) -> dict:
    m = NAME_START.search(block)
    if not m:
        fail(f"name/age/sex not found in {src}")
    raw_name, age, sex = re.sub(r"\s+", " ", m.group(1).strip()), int(m.group(2)), m.group(3)
    first_line = block[m.start():].splitlines()[0]
    street = TRAILING_TAG.sub("", re.split(r"Sex:[MF]\s+", first_line, maxsplit=1)[1].strip())
    stage = re.search(r"Stage:\s+(\w+)", block)
    if not stage or stage.group(1) not in {"Active", "Lapse"}:
        fail(f"Stage missing/unknown for {raw_name} in {src}")
    annual = re.search(r"^Annual\s+([\d.]+)", block, re.M)
    if not annual:
        fail(f"Annual premium missing for {raw_name} in {src}")
    csz = CITY_STATE_ZIP.search(block)
    if not csz:
        fail(f"city/state/zip missing for {raw_name} in {src}")
    after_zip = block[csz.end():].split("\n", 1)[0]
    pol = re.search(r"\b(\d{6,8})\b", after_zip)
    if not pol:
        fail(f"policy number missing for {raw_name} in {src}")
    dob = re.search(r"DOB:(\d{1,2}/\d{1,2}/\d{4})", block)
    issue = re.search(r"Issue Date\s+(\d{4}-\d{1,2}-\d{1,2})", block)
    email = re.search(r"\b([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})\b", block, re.I)
    body = block.split("Comment:", 1)[0]
    return {
        "name": title(raw_name), "first": title(raw_name.split()[0]), "age": age, "sex": sex,
        "dob": dob.group(1) if dob else "",
        "phone": norm_phone(re.search(r"Ph:\s*(\([^\n]*?\d{4})?", block).group(0) if "Ph:" in block else ""),
        "sec_phone": norm_phone((re.search(r"Sec Ph:\s*(\(\d{3}\)\s*\d{3}-\d{4})", block) or [None, ""])[1]),
        "street": title(street), "city": title(csz.group(1)), "state": csz.group(2), "zip": csz.group(3),
        "policy": pol.group(1), "status": stage.group(1), "annual": float(annual.group(1)),
        "issue": date.fromisoformat(issue.group(1)) if issue else None,
        "accidental": bool(re.search(r"\bADB\b|AD&D", body)),
        "email": email.group(1) if email else "", "source": src,
    }


def parse_file(path: Path) -> list[dict]:
    text = read_text(path)
    starts = list(NAME_START.finditer(text))
    if not starts:
        fail(f"no policy blocks in {path.name}")
    blocks = [text[s.start(): (starts[i + 1].start() if i + 1 < len(starts) else len(text))] for i, s in enumerate(starts)]
    return [parse_block(b.replace("<<<PAGEBREAK>>>", ""), path.name) for b in blocks]


def households(policies: list[dict]) -> list[list[dict]]:
    """Union-find: same phone or same street+zip => same household."""
    parent = list(range(len(policies)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    seen: dict[str, int] = {}
    for i, p in enumerate(policies):
        for key in filter(None, [p["phone"], p["sec_phone"], f"{p['street']}|{p['zip']}".lower()]):
            if key in seen:
                parent[find(i)] = find(seen[key])
            else:
                seen[key] = i
    groups: dict[int, list[dict]] = {}
    for i, p in enumerate(policies):
        groups.setdefault(find(i), []).append(p)
    return list(groups.values())


def months_ago(d: date, today: date) -> int:
    return (today.year - d.year) * 12 + today.month - d.month


def script_type(primary: dict, members: list[dict], today: date) -> str:
    """Pick the call script.
    1) primary has active accidental coverage and age <= 50 -> 'Under-65 Review' (+ 'Recommended' if recent issue)
    2) any active policy issued in the last 18 months -> 'Recommended'
    3) otherwise by age: <= 50 'Under-65 Review', else 'Senior Review'
    """
    recent = any(m["status"] == "Active" and m["issue"] and months_ago(m["issue"], today) <= RECENT_MONTHS for m in members)
    acc = primary["status"] == "Active" and primary["accidental"]
    by_age = "Under-65 Review" if primary["age"] <= 50 else "Senior Review"
    if acc and primary["age"] <= 50:
        return "Under-65 Review + Recommended" if recent else "Under-65 Review"
    return "Recommended" if recent else by_age


def lead_score(members: list[dict], today: date) -> int:
    active = [m for m in members if m["status"] == "Active"]
    s = 40 + 10 * len(active) + min(30, int(sum(m["annual"] for m in active) / 50))
    if any(m["issue"] and months_ago(m["issue"], today) <= RECENT_MONTHS for m in active):
        s += 10
    if any(m["status"] == "Lapse" for m in members):
        s += 5  # lapsed coverage = reinstatement conversation
    return min(100, s)


def build_rows(policies: list[dict], today: date, prefix: str) -> list[dict]:
    rows = []
    for n, members in enumerate(households(policies), 1):
        adults = [m for m in members if m["age"] >= 18]
        primary = max(adults or members, key=lambda m: (m["status"] == "Active", m["annual"]))
        phones = Counter(p for m in members for p in (m["phone"], m["sec_phone"]) if p)
        mobile = primary["phone"] or (phones.most_common(1)[0][0] if phones else "")
        others = [p for p in phones if p != mobile]
        active = [m for m in members if m["status"] == "Active"]
        rows.append({
            "Name": primary["name"], "Email": next((m["email"] for m in members if m["email"]), ""),
            "Mobile #": mobile, "Other #": ", ".join(others), "Custom ID": f"{prefix}-{n:04d}",
            "Lead Score": lead_score(members, today),
            "Household Premium": f"{sum(m['annual'] for m in active):.2f}",
            "Policies": ", ".join(m["policy"] for m in members),
            "Active Policies": len(active), "Lapsed Policies": len(members) - len(active),
            "Household Status": "Active" if active else "Lapsed", "Adults": len(adults),
            "Script Type": script_type(primary, members, today),
            "Household Members": "; ".join(f"{m['name']} ({m['age']})" for m in members),
            "Address": f"{primary['street']}, {primary['city']}, {primary['state']} {primary['zip']}",
            "Outcome": "", "Notes": "" if mobile else "NO VALID PHONE - review",
        })
    rows.sort(key=lambda r: -r["Lead Score"])
    return rows


_PHONEISH = re.compile(r"^[+-]?[\d\s().-]+$")

def safe_cell(v):
    """Stop spreadsheet formula injection: prefix text starting with = + - @ (tab/CR too) with '. Plain numbers/phones are left alone."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") and not _PHONEISH.match(v):
        return "'" + v
    return v


def write(rows: list[dict], out: Path):
    rows = [{k: safe_cell(v) for k, v in r.items()} for r in rows]
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "dialer.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS); w.writeheader(); w.writerows(rows)
    try:
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active; ws.title = "Dialer"; ws.append(COLUMNS)
        for r in rows:
            ws.append([r[c] for c in COLUMNS])
        wb.save(out / "dialer.xlsx")
    except ImportError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_dir", type=Path)
    ap.add_argument("--out", type=Path, default=Path("output"))
    ap.add_argument("--today", default=date.today().isoformat(), help="book date (YYYY-MM-DD)")
    ap.add_argument("--prefix", default="BOOK")
    a = ap.parse_args(argv)
    today = date.fromisoformat(a.today)
    files = sorted(p for p in a.input_dir.iterdir() if p.suffix.lower() in {".pdf", ".txt"})
    if not files:
        fail(f"no .pdf/.txt files in {a.input_dir}")
    policies = [p for f in files for p in parse_file(f)]
    rows = build_rows(policies, today, f"{a.prefix}-{today:%Y%m%d}")
    write(rows, a.out)
    summary = {"files": len(files), "policies": len(policies), "households": len(rows),
               "no_phone": sum(1 for r in rows if not r["Mobile #"]),
               "scripts": Counter(r["Script Type"] for r in rows), "generated": datetime.now().isoformat(timespec="seconds")}
    (a.out / "run-summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
