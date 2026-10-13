"""Part B pipeline: normalise, dedupe, match and classify workshop applications.

Run with `uv run python -m united_board_accessment.pipeline`; results go to outputs/.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from united_board_accessment.reference_data import (
    COUNTRIES,
    COUNTRY_NAMES,
    INSTITUTION_ALIASES,
    INSTITUTIONS,
)

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "outputs"

# ASSUMPTION, not decided yet: an application with endorsement = "No" goes to review,
# because eligibility is a staff decision and the pipeline must not guess.
# Set to False to treat such records like any other.
ENDORSEMENT_NO_NEEDS_REVIEW = True

TEXT_COLS = ["full_name", "email", "institution", "country", "position", "endorsement"]
REQUIRED = list(TEXT_COLS)
IDENTITY_FIELDS = ["full_name", "institution", "country"]  # database wins on update
CONTACT_FIELDS = ["email"]  # application wins on update
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")  # the dot after @ is required

NEW, UPDATE, REVIEW = "New", "Update existing record", "Needs review"
NO_NAME = "(no name given)"

IMPORT_COLS = ["submission_ids", "submitted", *TEXT_COLS, "status"]
LOG_COLS = ["id", "applicant", "issue", "original", "incoming", "resolved", "action", "follow_up"]

# field: (issue label, action) for plain cleanup changes
CLEANUP = {
    "full_name": ("Name case", "Standardized"),
    "institution": ("Institution variant", "Mapped to master name"),
    "country": ("Country code", "Expanded"),
}

# DRAFT: Part A is not written yet, so edit this once the proposal is final.
NOTE = """\
Most problems in this batch are data-entry problems, and the proposed workflow stops them at the form.

Dropdown lists for country and institution prevent variants and typos such as "PH", \
"Silliman Univ.", "Madras Christian Coll.", "UKDW" and "Central Philipine University". \
Email format validation and required fields prevent "dimas.prakoso@uksw", the blank name \
(id 11) and the blank position (id 8): the form cannot be submitted until they are fixed. \
Duplicate detection on email catches Rohan Varghese's second submission and keeps the latest. \
Each submission is then matched against the database automatically. A match on email, or on \
name, institution and country together, becomes an update. Anything uncertain goes to a \
review queue and is not imported.

Automation cannot settle genuine conflicts or eligibility. Marisol Tavera's name matches a \
record at a different institution, and Sekar Wulandari's endorsement is "No". The workflow \
flags both, but staff make the decision."""
assert len(NOTE.split()) <= 150, "Note must be 150 words or fewer"


# --- Normalisation ---------------------------------------------------------


def clean_text(s: str) -> str:
    """Trim and collapse repeated whitespace."""
    return re.sub(r"\s+", " ", s).strip()


def to_key(s: str) -> str:
    """Lower-case and turn each run of spaces/punctuation into one underscore."""
    return re.sub(r"[\W_]+", "_", s.lower()).strip("_")


COUNTRY_CODE_LOOKUP = {to_key(k): v for k, v in COUNTRIES.items()}
COUNTRY_NAME_LOOKUP = {to_key(k): v for k, v in COUNTRY_NAMES.items()}
INSTITUTION_LOOKUP = {to_key(i): i for i in INSTITUTIONS}
INSTITUTION_LOOKUP.update({to_key(k): v for k, v in INSTITUTION_ALIASES.items()})


def norm_country(s: str) -> tuple[str, bool]:
    """Return (display name, known): codes first, then names. No guessing."""
    cleaned = clean_text(s)
    key = to_key(cleaned)
    if key in COUNTRY_CODE_LOOKUP:
        return COUNTRY_CODE_LOOKUP[key], True
    if key in COUNTRY_NAME_LOOKUP:
        return COUNTRY_NAME_LOOKUP[key], True
    return cleaned, False


def norm_institution(s: str) -> tuple[str, bool]:
    """Return (master name, known) from the institution list and its aliases."""
    cleaned = clean_text(s)
    key = to_key(cleaned)
    if key in INSTITUTION_LOOKUP:
        return INSTITUTION_LOOKUP[key], True
    return cleaned, False


def norm_name(s: str) -> str:
    """Title-case a name only when it is entirely lower-case or entirely upper-case."""
    cleaned = clean_text(s)
    return cleaned.title() if cleaned.islower() or cleaned.isupper() else cleaned


def norm_email(s: str) -> str:
    """Clean and lower-case. Validation is separate."""
    return clean_text(s).lower()


def is_valid_email(s: str) -> bool:
    return bool(EMAIL_RE.match(s))


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with every value normalised, plus the known / date_valid flags."""
    out = df.copy()
    out["full_name"] = out["full_name"].map(norm_name)
    out["email"] = out["email"].map(norm_email)
    for col, fn in (("country", norm_country), ("institution", norm_institution)):
        pairs = [fn(value) for value in out[col]]
        out[col] = [name for name, _ in pairs]
        out[f"{col}_known"] = [known for _, known in pairs]
    for col in ("position", "endorsement", "submitted"):
        if col in out:
            out[col] = out[col].map(clean_text)
    if "submitted" in out:
        out["submitted_dt"] = pd.to_datetime(out["submitted"], format="%d %b %Y", errors="coerce")
        out["date_valid"] = out["submitted_dt"].notna()
    return out


def check(df: pd.DataFrame) -> pd.DataFrame:
    """Re-normalise, then add email_valid and missing (list of blank required fields)."""
    out = normalise(df)
    out["email_valid"] = [is_valid_email(email) for email in out["email"]]
    out["missing"] = [[f for f in REQUIRED if not rec[f]] for rec in out.to_dict("records")]
    return out


# --- Issues log ------------------------------------------------------------


def label(field: str) -> str:
    """Column name as plain words: "full_name" -> "full name"."""
    return field.replace("_", " ")


def log_issue(
    log: list[dict],
    app_id: int,
    applicant: str,
    issue: str,
    action: str,
    incoming: str = "",
    resolved: str = "",
    follow_up: str = "",
    original: str = "",
) -> None:
    """Add one row to the issues log.

    `original` is the value in the database, `incoming` the value the applicant sent and
    `resolved` the value used. A blank `resolved` on a "Needs review" row means still open.
    """
    row = (int(app_id), applicant, issue, original, incoming, resolved, action, follow_up)
    log.append(dict(zip(LOG_COLS, row)))


def cleanup_label(field: str, raw_value: str) -> tuple[str, str]:
    """Issue label and action for a cleanup change. A country that is not a code is a variant."""
    if field == "country" and to_key(raw_value) not in COUNTRY_CODE_LOOKUP:
        return "Country variant", "Mapped to master name"
    return CLEANUP[field]


def log_cleanup(raw: pd.DataFrame, norm: pd.DataFrame, log: list[dict]) -> None:
    """Log each value that normalisation changed, by id, so merged-away rows are covered."""
    raw, norm = raw.set_index("id"), norm.set_index("id")
    for field in CLEANUP:
        for app_id in raw.index[raw[field] != norm[field]]:
            who = norm.at[app_id, "full_name"] or NO_NAME
            issue, action = cleanup_label(field, raw.at[app_id, field])
            log_issue(log, app_id, who, issue, action, raw.at[app_id, field], norm.at[app_id, field])


# --- Dedupe ----------------------------------------------------------------


def dedupe(apps: pd.DataFrame, log: list[dict]) -> pd.DataFrame:
    """Merge submissions sharing an email: the latest wins, its blanks filled from earlier ones."""
    work = apps.sort_values(["submitted_dt", "id"]).copy()
    work[TEXT_COLS] = work[TEXT_COLS].replace("", pd.NA)  # last() skips NA, not ""
    # A row with an invalid date cannot be ordered and a row with no email has nothing
    # to merge on. Each gets a key of its own, so it stays a separate record.
    mergeable = work["date_valid"] & work["email"].notna()
    key = work["email"].where(mergeable, "id:" + work["id"].astype(str))
    groups = work.groupby(key, sort=False)
    people = groups[["id", "submitted", *TEXT_COLS]].last()
    people[TEXT_COLS] = people[TEXT_COLS].fillna("")
    people["merged_ids"] = groups["id"].agg(lambda ids: ids.tolist())
    people = check(people.sort_values("id").reset_index(drop=True))

    latest = apps.set_index("id")
    for rec in people.to_dict("records"):
        app_id, ids = rec["id"], rec["merged_ids"]
        if len(ids) == 1:
            continue
        who = rec["full_name"] or NO_NAME
        merged = f"ids {', '.join(map(str, ids))}"
        log_issue(log, app_id, who, "Duplicate submission", "Merged; latest kept", merged, f"id {app_id}")
        for field in TEXT_COLS:
            if rec[field] and not latest.at[app_id, field]:
                issue = f"Blank {label(field)}"
                log_issue(log, app_id, who, issue, "Filled from earlier submission", "", rec[field])
        for dropped in ids[:-1]:
            log_issue(log, dropped, who, f"Duplicate of id {app_id}", "Excluded from import")
    return people


# --- Matching and classification -------------------------------------------


def person_key(rec: dict) -> str:
    return "|".join(to_key(rec[f]) for f in IDENTITY_FIELDS)


def find_match(rec: dict, existing: list[dict]) -> tuple[str, dict | None]:
    """Return (kind, existing record). Kind is "email", "person", "name" or ""."""
    name = to_key(rec["full_name"])
    tests = {
        "email": lambda e: rec["email"] and e["email"] == rec["email"],
        "person": lambda e: name and person_key(e) == person_key(rec),
        "name": lambda e: name and to_key(e["full_name"]) == name,  # never trusted alone
    }
    for kind, test in tests.items():
        for existing_rec in existing:
            if test(existing_rec):
                return kind, existing_rec
    return "", None


def classify(rec: dict, existing: list[dict], log: list[dict]) -> dict:
    """Give one merged record its status and import-ready values; log each decision."""
    app_id, who = rec["id"], rec["full_name"] or NO_NAME
    flagged: list[str] = []

    def hold(issue: str, follow_up: str, incoming: str = "", original: str = "") -> None:
        log_issue(log, app_id, who, issue, REVIEW, incoming, follow_up=follow_up, original=original)
        flagged.append(issue)

    if not rec["date_valid"]:
        hold("Invalid date", "Confirm submission date", rec["submitted"])
    if rec["email"] and not rec["email_valid"]:
        hold("Invalid email", "Confirm email with applicant", rec["email"])
    for field in rec["missing"]:
        hold(f"Missing {label(field)}", f"Request {label(field)} from applicant")
    for field in ("institution", "country"):
        if rec[field] and not rec[f"{field}_known"]:
            hold(f"Unrecognised {field}", f"Confirm {field}", rec[field])

    kind, match = find_match(rec, existing)
    differs = [
        f for f in ("institution", "country") if match and rec[f] and to_key(rec[f]) != to_key(match[f])
    ]
    fields = " and ".join(differs)
    applied = ", ".join(rec[f] for f in differs)
    in_database = ", ".join(match[f] for f in differs)
    if kind == "email" and differs:
        hold(f"Email match, {fields} differs", f"Confirm {fields} with applicant", applied, in_database)
    if kind == "name":
        hold("Name-only match", "Confirm whether same person", applied, in_database)
    if ENDORSEMENT_NO_NEEDS_REVIEW and to_key(rec["endorsement"]) == "no":
        hold("Endorsement = No", "Staff to decide eligibility", rec["endorsement"])

    values = {f: rec[f] for f in TEXT_COLS}
    if flagged:
        status = REVIEW
    elif kind in ("email", "person"):
        status = UPDATE
        how = "email" if kind == "email" else "name + institution + country"
        log_issue(log, app_id, who, f"Matched by {how}", UPDATE)
        for f in CONTACT_FIELDS + IDENTITY_FIELDS:
            if match[f] != rec[f]:
                keep_database = f in IDENTITY_FIELDS
                issue = f"{label(f).capitalize()} differs"
                action = "Database value kept" if keep_database else "Incoming value used"
                resolved = match[f] if keep_database else rec[f]
                log_issue(log, app_id, who, issue, action, rec[f], resolved, original=match[f])
        values.update({f: match[f] for f in IDENTITY_FIELDS})
    else:
        status = NEW

    return {
        "submission_ids": ", ".join(map(str, rec["merged_ids"])),
        "submitted": rec["submitted_dt"].date().isoformat() if rec["date_valid"] else rec["submitted"],
        **values,
        "status": status,
    }


# --- Pipeline and outputs --------------------------------------------------


def load(data_dir: Path = DATA_DIR) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read the raw applications and existing records. Blanks stay as ""."""
    apps = pd.read_csv(data_dir / "applications.csv", keep_default_na=False)
    existing = pd.read_csv(data_dir / "existing_records.csv", keep_default_na=False)
    return apps, existing


def run(apps_raw: pd.DataFrame, existing_raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run every step. Returns (import-ready list indexed by latest id, issues log)."""
    log: list[dict] = []
    apps = normalise(apps_raw)
    existing = normalise(existing_raw)
    existing["existing_id"] = range(len(existing))  # temporary id: row position

    log_cleanup(apps_raw, apps, log)
    people = dedupe(apps, log)
    existing_recs = existing.to_dict("records")
    rows = [classify(rec, existing_recs, log) for rec in people.to_dict("records")]

    import_ready = pd.DataFrame(rows, columns=IMPORT_COLS, index=pd.Index(people["id"], name="id"))
    issues = pd.DataFrame(log, columns=LOG_COLS).sort_values("id", kind="stable", ignore_index=True)
    return import_ready, issues


def write_outputs(import_ready: pd.DataFrame, issues: pd.DataFrame, out_dir: Path = OUT_DIR) -> Path:
    """Write the import-ready list and issues log as CSV and the note as text."""
    out_dir.mkdir(parents=True, exist_ok=True)
    import_ready.to_csv(out_dir / "import_ready_list.csv", index=False)
    issues.to_csv(out_dir / "issues_log.csv", index=False)
    (out_dir / "note.txt").write_text(NOTE + "\n", encoding="utf-8")
    return out_dir


def main() -> None:
    import_ready, issues = run(*load())
    out_dir = write_outputs(import_ready, issues)
    print(import_ready["status"].value_counts().to_string())
    print(f"{len(import_ready)} people, {len(issues)} issues-log rows, note {len(NOTE.split())} words")
    print(f"Wrote import_ready_list.csv, issues_log.csv and note.txt to {out_dir}")


if __name__ == "__main__":
    main()
