# Participant information & consent — Revit workflow logging study

**Study:** Master's thesis *"Meta Learning for Realtime Behavioral Personalization"* (TUM),
researcher: Mohamed K. Mahdy (Sweco / TUM). Contact: mohamedkmahdy@outlook.com

**What happens:** A small Revit add-in (generalBIMlog) runs on your machine for ~2–3 weeks and passively
records your modelling actions inside Revit: the kind of action (place / set parameter / tag / delete),
the element's category / family / type / level / view names, parameter names and values (e.g. door Mark
numbers), and timestamps. It records **no** geometry, file contents, screenshots, keystrokes, or anything
outside Revit, and it sends **nothing over the network** — logs stay on your machine until you hand them over.

**Personal data & pseudonymization (GDPR):** Your Windows/Revit user name and project names appear in the
raw logs. **Before any data leaves your machine**, an anonymization script replaces them with pseudonyms
(e.g. `user_B`, `project_3f2a`) and scrubs your name from all text fields. You may inspect the anonymized
files (readable JSON) before handing them over. The pseudonym mapping stays private with the researcher
and is deleted after the thesis is graded.

**Purpose & use:** The anonymized logs are used to (a) detect repeated personal workflows, (b) evaluate
whether an assistant that learned from *other* users helps a *new* user faster (the thesis experiment),
and (c) measure potential time/action savings. Results appear in the thesis in aggregate / pseudonymized
form only.

**Voluntary:** Participation is voluntary. You can pause the logger (disable the add-in) or withdraw at
any time without giving a reason; on withdrawal your data is deleted.

---

I have read the above and agree to participate.

Name: ______________________  Date: ____________  Signature: ______________________
