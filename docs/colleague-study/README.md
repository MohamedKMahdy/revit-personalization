# Colleague logging study — deployment package

_Purpose: collect real multi-user Revit workflow logs (2–5 colleagues, ≥2–3 weeks of normal work) for
the thesis's cross-user meta-learning experiment (leave-one-user-out) and the n=3–6 acceleration study._

## What a participant does (5 minutes, then nothing)

1. **Install** the generalBIMlog add-in (copy the `RevitLogger` folder + `.addin` file into
   `%APPDATA%\Autodesk\Revit\Addins\<2025|2026>\` — same files as on the researcher's machine).
2. **Read + sign** `CONSENT.md` (one page).
3. **Work normally.** The add-in passively logs modelling actions (place / set-parameter / tag events).
   No behavior change is asked of the participant; there is no UI.

## What is (and isn't) collected

| Collected | NOT collected |
|---|---|
| action type (Place/SetParam/Tag/Delete…), element category, family/type/level/view names, parameter names + values (e.g. Mark "TU 234"), timestamps, session ids | model geometry, file contents, screenshots, keystrokes, anything outside Revit |
| `user_name`, `document_title`, `project_guid` — **pseudonymized before leaving the machine** (see below) | |

## Collection + anonymization (end of the logging period)

On each participant's machine (or with the participant watching):

```powershell
# 1. anonymize IN PLACE-COPY: writes an anonymized copy + a private mapping file
python anonymize_logs.py --user-label user_A     # user_B, user_C, ... per participant

# 2. hand over ONLY the anonymized folder (participants may inspect it first — it's readable JSON)
```

`anonymize_logs.py` replaces `user_name`, `document_title`, `project_guid` with stable pseudonyms and
scrubs the real username from EVERY string field (it leaks into e.g. 3D view names). The mapping file
(`anonymize_mapping.json`) stays with the participant / researcher only, never enters the analysis repo.

## Where the raw logs live

`%APPDATA%\Autodesk\Revit\Addins\<ver>\RevitLogger\Logs\eventlog\*.json`

## Researcher checklist (per participant)

- [ ] Sweco IT clearance for the add-in install (it is a local, offline logger — no network traffic).
- [ ] Signed consent stored.
- [ ] Add-in deployed; confirm a `session_start` line appears after the first Revit start.
- [ ] After ≥2 weeks: run the anonymizer, collect the anonymized folder, verify no real names remain
      (`grep` for the username), archive under `eval/data/users/<user_label>/`.

## How the data is used

- **Leave-one-user-out meta-learning experiment** (`eval/meta_learning_eval.py`): population prior built
  from N−1 users, evaluated on the held-out user (cold vs. population vs. personal prior).
- **Foreign-prior negative control**: user A's *personal* prior applied to user B (should not help).
- **Acceleration study**: per-participant manual-vs-assisted action counts (`eval/process_acceleration.py --userstudy`).
