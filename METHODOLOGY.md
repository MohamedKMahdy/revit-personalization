# Project Methodology — Agent-Augmented BIM Log Mining for Personalized Action Generation

## Goal

Build a proof-of-concept system that observes a designer working in Revit, learns their
repeated "Custom Element Instantiation" routines (place an element, set parameters, tag it)
from a few demonstrations, and then offers to replay that routine as a one-click shortcut.
The research contribution is the **personalization layer**, not the Revit write tooling.

---

## Architecture (three decoupled parts)

### 1. Local C# Revit Add-in — Logging Only

Subscribes to Revit API events (`DocumentChanged` and element-level events), filters out
view navigation and non-authoring noise, and abstracts events into semantic actions:

- `Place(FamilyType)`
- `SetParam(ElementId, Name, Value)`
- `Tag(ElementId, TagFamily)`

Writes completed action sequences to local JSON log files. Maintains a rolling buffer and
uses simple heuristics (repeated subsequence detection within a session) to flag candidate
routines, then shows a small non-intrusive prompt:

> "You seem to be repeating this 7-step routine. Learn as shortcut?"

**This add-in never writes to the model.**

---

### 2. Local Python Personalization MCP Server

Sits between the agents and the write-capable backend. Exposes:

**Resources:**
- `logs:list_candidate_routines`
- `logs:get_routine_examples(id, k)`
- `model:query_state(query)` ← reads via the backend

**Tools:**
- `analyze_pattern(sequences)`
- `generate_command(motif, name)`
- `execute_revit_command(name or payload)`

Maps a learned motif onto an ordered, parameterized sequence of backend tool calls, applies
safeguards (category filters, precondition and dry-run checks), and logs every call.
**Does not perform model writes itself.**

---

### 3. Multi-Agent Orchestrator (LangGraph / AutoGen / plain code)

**Pattern Agent**
- Input: k example sequences
- Method: few-shot in-context learning
- Output: generalized motif JSON (ordered steps, parameter rules, preconditions)

**Macro / Command Agent**
- Input: motif JSON
- Maps motif to concrete backend tool-call sequence
- Resolves family types and parameter names against current model state
- After user confirmation: calls `execute_revit_command`

---

## Backend — Do Not Build Your Own Write Tooling

All model reads and writes go through **mcp-servers-for-revit**:
https://github.com/mcp-servers-for-revit/mcp-servers-for-revit

An open-source write-capable MCP server. First task is to stand it up against the target
Revit version and confirm it can read state and perform place, set-parameter, and tag.
**Pin a specific commit.** If a needed operation is missing, add it as a backend tool
extension rather than working around it.

---

## Hard Constraints

1. The official Revit Public MCP Server is read-only — **never route writes through it**
2. Revit API writes must run on the main thread inside a transaction — respect the backend's
   transaction handling and never call the API from a background thread
3. All telemetry stays local on the workstation — **no logs leave the machine**
4. Every execution requires **explicit user confirmation** before any write
5. Keep the motif as the stable interface so the execution backend stays swappable

---

## Build Order (match this sequence)

1. **Stand up and validate mcp-servers-for-revit** (read + place/set/tag) against the target
   Revit version. Validate end-to-end: local agent through backend to live model on one
   minimal operation.
2. **C# edge logger:** event capture, action abstraction, JSON logging, rolling buffer,
   routine detection, suggestion UI, basic visual summaries.
3. **Personalization MCP server + both agents:** achieve one full pipeline from detected
   routine to suggested shortcut to successful execution for one workflow.
4. **Evaluation harness** for the two metrics below.

---

## Evaluation

### Process Acceleration
- Task completion time
- Number of actions
- Number and severity of post-run corrections
- Manual versus assisted comparison

### Sample Efficiency
- Vary k ∈ {1, 3, 5, 10}
- Record success rate and quality score (0–3 rubric)
- Produce performance-versus-k curves
- Output: `results/performance_vs_k.csv`

---

## Starting Point

> **Validate the mcp-servers-for-revit backend against the installed Revit version and
> confirm it can place an element, set a parameter, and tag it. Report what it covers and
> what is missing before building anything else.**
