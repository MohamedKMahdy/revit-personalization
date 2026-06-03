// generate_plugin_doc.js — 3-page plugin overview document

const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, HeadingLevel, BorderStyle, WidthType,
  ShadingType, VerticalAlign, PageNumber, LevelFormat, PageBreak,
  Tab, TabStopType, TabStopPosition
} = require("docx");
const fs = require("fs");

// ── Page / layout constants (A4, 2 cm margins) ────────────────────────────────
const PAGE_W    = 11906;
const PAGE_H    = 16838;
const MARGIN    = 1134; // ~2 cm
const CW        = PAGE_W - MARGIN * 2; // 9638 DXA content width

// ── Colours ───────────────────────────────────────────────────────────────────
const BLUE      = "1F4E79";
const BLUE_LT   = "BDD7EE";
const BLUE_MID  = "2E75B6";
const GREEN     = "375623";
const GREEN_LT  = "E2EFDA";
const ORANGE    = "833C00";
const ORANGE_LT = "FCE4D6";
const TEAL      = "006064";
const TEAL_LT   = "E0F7FA";
const GRAY      = "595959";
const GRAY_LT   = "F2F2F2";
const RED       = "C00000";
const WHITE     = "FFFFFF";
const BLACK     = "000000";
const ROW_ALT   = "EEF4FB";

// ── Border helpers ────────────────────────────────────────────────────────────
const thin  = c => ({ style: BorderStyle.SINGLE, size: 1, color: c || "BFBFBF" });
const thick = c => ({ style: BorderStyle.SINGLE, size: 6, color: c || BLUE_MID });
const none  = ()  => ({ style: BorderStyle.NONE,   size: 0, color: WHITE });
const allB  = c => ({ top: thin(c), bottom: thin(c), left: thin(c), right: thin(c) });
const noB   = ()  => ({ top: none(), bottom: none(), left: none(), right: none() });

// ── Typography helpers ────────────────────────────────────────────────────────
const r = (text, opts = {}) =>
  new TextRun({ text, font: "Arial", size: 22, ...opts });

const rCode = (text, opts = {}) =>
  new TextRun({ text, font: "Courier New", size: 20, ...opts });

const rBold   = t => r(t, { bold: true });
const rItalic = t => r(t, { italics: true });
const rGray   = t => r(t, { color: GRAY, italics: true });

function para(runs, opts = {}) {
  const rr = Array.isArray(runs) ? runs : [r(runs)];
  return new Paragraph({
    alignment: AlignmentType.JUSTIFIED,
    spacing: { before: 80, after: 120, line: 268 },
    ...opts,
    children: rr,
  });
}

function h1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 0, after: 160 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: BLUE_MID, space: 3 } },
    children: [r(text, { bold: true, size: 30, color: BLUE })],
  });
}

function h2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 200, after: 100 },
    children: [r(text, { bold: true, size: 24, color: BLUE_MID })],
  });
}

function bullet(runs) {
  const rr = Array.isArray(runs) ? runs : [r(runs)];
  return new Paragraph({
    numbering: { reference: "bullets", level: 0 },
    spacing: { before: 40, after: 40 },
    children: rr,
  });
}

function codeLine(text) {
  return new Paragraph({
    spacing: { before: 30, after: 30 },
    indent: { left: 200 },
    shading: { type: ShadingType.CLEAR, fill: GRAY_LT },
    children: [rCode(text)],
  });
}

function spacer() {
  return new Paragraph({ spacing: { before: 0, after: 60 }, children: [r("")] });
}

// ── Table helpers ─────────────────────────────────────────────────────────────
function hdrCell(text, w, bg = BLUE_MID) {
  return new TableCell({
    width: { size: w, type: WidthType.DXA },
    shading: { type: ShadingType.CLEAR, fill: bg },
    borders: allB(),
    margins: { top: 80, bottom: 80, left: 130, right: 130 },
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({
      alignment: AlignmentType.CENTER,
      children: [r(text, { bold: true, size: 19, color: WHITE })],
    })],
  });
}

function cell(runs, w, alt = false, align = AlignmentType.LEFT) {
  const rr = Array.isArray(runs) ? runs : [r(runs, { size: 20 })];
  return new TableCell({
    width: { size: w, type: WidthType.DXA },
    shading: { type: ShadingType.CLEAR, fill: alt ? ROW_ALT : WHITE },
    borders: allB(),
    margins: { top: 70, bottom: 70, left: 130, right: 130 },
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({ alignment: align, children: rr })],
  });
}

function cellCode(text, w, alt = false) {
  return new TableCell({
    width: { size: w, type: WidthType.DXA },
    shading: { type: ShadingType.CLEAR, fill: alt ? ROW_ALT : WHITE },
    borders: allB(),
    margins: { top: 70, bottom: 70, left: 130, right: 130 },
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({ children: [rCode(text, { size: 18 })] })],
  });
}

function table(headers, rows, widths) {
  return new Table({
    width: { size: CW, type: WidthType.DXA },
    columnWidths: widths,
    rows: [
      new TableRow({ tableHeader: true, children: headers.map((h, i) => hdrCell(h, widths[i])) }),
      ...rows.map((row, ri) => new TableRow({
        children: row.map((c, ci) => {
          if (typeof c === "string") return cell(c, widths[ci], ri % 2 === 1);
          if (c.code) return cellCode(c.v, widths[ci], ri % 2 === 1);
          if (c.runs) return cell(c.runs, widths[ci], ri % 2 === 1, c.align);
          return cell(c.v || "", widths[ci], ri % 2 === 1);
        }),
      })),
    ],
  });
}

// ── Coloured action badge ─────────────────────────────────────────────────────
function actionBadge(label, bg, text = WHITE) {
  return new TableCell({
    width: { size: 1200, type: WidthType.DXA },
    shading: { type: ShadingType.CLEAR, fill: bg },
    borders: allB(bg),
    margins: { top: 120, bottom: 120, left: 120, right: 120 },
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({
      alignment: AlignmentType.CENTER,
      children: [r(label, { bold: true, size: 20, color: text })],
    })],
  });
}

function actionRow(label, bg, descRuns, exampleRuns, alt = false) {
  return new TableRow({
    children: [
      actionBadge(label, bg),
      new TableCell({
        width: { size: 3700, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: alt ? ROW_ALT : WHITE },
        borders: allB(),
        margins: { top: 80, bottom: 80, left: 130, right: 130 },
        children: [new Paragraph({ children: descRuns })],
      }),
      new TableCell({
        width: { size: CW - 1200 - 3700, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: alt ? ROW_ALT : WHITE },
        borders: allB(),
        margins: { top: 80, bottom: 80, left: 130, right: 130 },
        children: [new Paragraph({ children: exampleRuns })],
      }),
    ],
  });
}

// ── Info box ──────────────────────────────────────────────────────────────────
function infoBox(labelText, labelBg, bodyRuns) {
  return new Table({
    width: { size: CW, type: WidthType.DXA },
    columnWidths: [1400, CW - 1400],
    rows: [new TableRow({
      children: [
        new TableCell({
          width: { size: 1400, type: WidthType.DXA },
          shading: { type: ShadingType.CLEAR, fill: labelBg },
          borders: allB(labelBg),
          margins: { top: 120, bottom: 120, left: 140, right: 140 },
          verticalAlign: VerticalAlign.CENTER,
          children: [new Paragraph({
            alignment: AlignmentType.CENTER,
            children: [r(labelText, { bold: true, size: 20, color: WHITE })],
          })],
        }),
        new TableCell({
          width: { size: CW - 1400, type: WidthType.DXA },
          shading: { type: ShadingType.CLEAR, fill: WHITE },
          borders: allB("BFBFBF"),
          margins: { top: 100, bottom: 100, left: 160, right: 160 },
          children: [new Paragraph({ children: bodyRuns })],
        }),
      ],
    })],
  });
}

// ── Flow step row ─────────────────────────────────────────────────────────────
function flowRow(num, text, alt = false) {
  return new TableRow({
    children: [
      new TableCell({
        width: { size: 600, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: BLUE_MID },
        borders: allB(BLUE_MID),
        margins: { top: 70, bottom: 70, left: 100, right: 100 },
        verticalAlign: VerticalAlign.CENTER,
        children: [new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [r(num, { bold: true, size: 22, color: WHITE })],
        })],
      }),
      new TableCell({
        width: { size: CW - 600, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: alt ? ROW_ALT : WHITE },
        borders: allB(),
        margins: { top: 70, bottom: 70, left: 160, right: 160 },
        children: [new Paragraph({ children: Array.isArray(text) ? text : [r(text, { size: 21 })] })],
      }),
    ],
  });
}

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE 1
// ═══════════════════════════════════════════════════════════════════════════════
const children = [];

// ── Header banner ─────────────────────────────────────────────────────────────
children.push(
  new Table({
    width: { size: CW, type: WidthType.DXA },
    columnWidths: [CW],
    rows: [new TableRow({ children: [
      new TableCell({
        width: { size: CW, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: BLUE },
        borders: noB(),
        margins: { top: 240, bottom: 240, left: 360, right: 360 },
        children: [
          new Paragraph({ alignment: AlignmentType.LEFT, spacing: { before: 0, after: 80 },
            children: [r("RevitLogger Add-in", { bold: true, size: 40, color: WHITE })] }),
          new Paragraph({ alignment: AlignmentType.LEFT, spacing: { before: 0, after: 0 },
            children: [r("What it captures and how it works", { size: 24, color: BLUE_LT })] }),
        ],
      }),
    ]})],
  }),
  spacer(),
);

// ── What is it ────────────────────────────────────────────────────────────────
children.push(
  h1("What Is the RevitLogger?"),
  para([
    r("RevitLogger is a "), rBold("C# add-in"), r(" that runs silently inside Revit 2027. It watches everything you do in a project and writes a structured record of your authoring actions to a log file on your computer. "),
    r("It never interrupts your work, never slows Revit down, and never sends any data outside your machine."),
  ]),
  para([
    r("Its purpose is to collect the raw material needed to learn your personal workflows — specifically, "),
    rBold("Custom Element Instantiation loops"), r(": the repeated sequence of placing an element, setting its parameters, and tagging it. Once enough repetitions are recorded, the AI pipeline can recognise the pattern and offer to automate it as a one-click shortcut."),
  ]),
  spacer(),
);

// ── How it loads ─────────────────────────────────────────────────────────────
children.push(
  h1("How It Loads and Runs"),
  para([r("The add-in is registered in Revit's add-ins folder via a "), rCode("RevitLogger.addin"), r(" manifest file. Revit loads it automatically every time it starts. You will see a security dialog on the first launch — click "), rBold("Always Load"), r(" to suppress it on future starts.")]),
  spacer(),
  h2("Session lifecycle"),
  para("A session begins when you open or create a project document and ends when you close it or exit Revit. The sequence is:"),
  spacer(),
  new Table({
    width: { size: CW, type: WidthType.DXA },
    columnWidths: [600, CW - 600],
    rows: [
      flowRow("1", "Revit fires DocumentOpened or DocumentCreated."),
      flowRow("2", ["A unique ", rCode("session_id"), r(" is generated. A new log file is created on disk."), r(" A one-line session_start record is written as the first entry.")], true),
      flowRow("3", ["The add-in subscribes to ", rCode("DocumentChanged"), r(" — this event fires once per committed Revit transaction.")]),
      flowRow("4", "Every time you complete an action (place, edit, tag), the event fires and the action is recorded.", true),
      flowRow("5", ["When the document closes, a ", rCode("session_end"), r(" record is written and the file is sealed.")]),
    ],
  }),
  spacer(),
  h2("Transaction-level precision"),
  para([
    r("The add-in hooks into Revit at the "), rBold("transaction level"), r(", not the click level. A Revit transaction is the atomic unit of work — one undo step. This means if a single user action creates multiple elements simultaneously, they are all grouped under the same "), rCode("transaction_id"), r(", which correctly represents one user intent. This design follows Jang & Lee (2023), who identified transaction grouping as essential for reproducible BIM log analysis."),
  ]),
  spacer(),
  h2("Non-blocking I/O"),
  para([
    r("Writing to disk happens on a "), rBold("background thread"), r(" using a thread-safe queue ("), rCode("BlockingCollection"), r("). The Revit UI thread never waits for disk I/O — it drops the record into the queue and returns immediately. The background writer flushes after every line, so if Revit crashes, all records up to the crash are already safely on disk."),
  ]),
);

// ── PAGE BREAK ────────────────────────────────────────────────────────────────
children.push(new Paragraph({ children: [new PageBreak()] }));

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE 2
// ═══════════════════════════════════════════════════════════════════════════════
children.push(
  h1("What Is Captured — The Three Action Types"),
  para([
    r("Every logged record belongs to one of three action types, following the lexicon defined by "),
    rBold("Jang et al. (2023)"), r(" for BIM authoring log analysis. Each type captures a different phase of the Custom Element Instantiation loop."),
  ]),
  spacer(),
);

// Action types table
children.push(
  new Table({
    width: { size: CW, type: WidthType.DXA },
    columnWidths: [1200, 3700, CW - 1200 - 3700],
    rows: [
      // Header row
      new TableRow({
        tableHeader: true,
        children: [
          hdrCell("Action", 1200),
          hdrCell("What it records", 3700),
          hdrCell("Real example from your session", CW - 1200 - 3700),
        ],
      }),
      actionRow("PLACE", BLUE, [
        rBold("Element placement."), r(" Fires when you place a door, window, column, furniture, or any other hosted/independent family. Captures the family name, type, level, phase, host, and view."),
      ], [
        rCode("family_name:"), r(" \"Door-Passage-Single-Full_Lite\""), r("\n"),
        rCode("type_name:"), r(" \"36\" x 84\"\""), r("\n"),
        rCode("level_name:"), r(" \"L1 - Block 35\""), r("\n"),
        rCode("host_category:"), r(" \"Walls\""),
      ]),
      actionRow("SET PARAM", GREEN, [
        rBold("Parameter change."), r(" Fires when you edit any user-settable parameter on a placed element. Records the parameter name and both the value "), rBold("before"), r(" and "), rBold("after"), r(" the change — not just the new value."),
      ], [
        rCode("param_name:"), r(" \"Mark\""), r("\n"),
        rCode("param_value_before:"), r(" \"3C09\""), r("\n"),
        rCode("param_value_after:"), r(" \"DD1\""), r("\n"),
        rCode("param_storage_type:"), r(" \"String\""),
      ], true),
      actionRow("TAG", TEAL, [
        rBold("Annotation tag."), r(" Fires when you tag an element. Records the tag family used and the ID of the element being tagged — linking the tag back to its Place and SetParam records to form a complete episode."),
      ], [
        rCode("tag_family_name:"), r(" \"Door Tag\""), r("\n"),
        rCode("tagged_element_id:"), r(" 3327603"), r("\n"),
        rCode("element_category:"), r(" \"Door Tags\""), r("\n"),
        rCode("view_type:"), r(" \"FloorPlan\""),
      ]),
    ],
  }),
  spacer(),
);

children.push(
  h2("Fields shared by all three action types"),
  para("Every record — regardless of type — carries these context fields:"),
  spacer(),
  table(
    ["Field", "What it contains", "Why it matters"],
    [
      [{ code: true, v: "transaction_id" }, "12-character random ID, same for all records in one Revit undo-step", "Groups actions that belong to one user intent (Jang & Lee 2023)"],
      [{ code: true, v: "transaction_name" }, "Revit's undo-stack label: \"Door\", \"Modify element attributes\"", "Provides semantic intent without needing NLP"],
      [{ code: true, v: "timestamp_unix" }, "Unix epoch float, e.g. 1779509164.268", "Enables time-gap analysis and episode sorting"],
      [{ code: true, v: "element_id" }, "Revit element ID integer", "Primary key that links Place + SetParam + Tag into one episode"],
      [{ code: true, v: "view_name / view_type" }, "\"L1\" / \"FloorPlan\"", "Detects which view type is required as a precondition for the shortcut"],
      [{ code: true, v: "session_id" }, "\"sess_20260523035731\"", "Groups all records from one Revit session for episode detection"],
    ],
    [1900, 3500, CW - 1900 - 3500]
  ),
  spacer(),
  h2("The episode: how the three types connect"),
  para([
    r("The "), rBold("episode"), r(" is the core unit of analysis. One episode = everything done to one element from placement to tagging. The "), rCode("element_id"), r(" field is the thread that connects all three record types:"),
  ]),
  spacer(),
  infoBox("EPISODE", BLUE, [
    rBold("Place"), r(" (element_id: 3327603)  →  "),
    rBold("SetParam"), r(" Mark (element_id: 3327603)  →  "),
    rBold("Tag"), r(" (tagged_element_id: 3327603)"),
    r("   =   one Custom Element Instantiation loop"),
  ]),
  spacer(),
  para([
    r("When the same episode structure repeats two or more times across a session — same family, same parameter sequence — it becomes a "), rBold("Candidate Routine"), r(" that the AI pipeline can learn from."),
  ]),
);

// ── PAGE BREAK ────────────────────────────────────────────────────────────────
children.push(new Paragraph({ children: [new PageBreak()] }));

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE 3
// ═══════════════════════════════════════════════════════════════════════════════
children.push(
  h1("What Is NOT Captured"),
  para("The add-in deliberately filters out a large class of events to avoid noise and to protect the privacy of your project data."),
  spacer(),
  table(
    ["Not captured", "Reason"],
    [
      ["View navigation (pan, zoom, orbit, section box)", "These do not trigger DocumentChanged at all — Revit does not consider them document modifications"],
      ["Element selection / deselection", "Selection is not a document modification"],
      ["Element moves and rotations", "Position is not a trackable Parameter — the diff engine finds no changed parameter values"],
      ["Tag leader drag / repositioning", "Tag modify events are explicitly suppressed — they are noise, not authoring intent"],
      ["Undo and Redo", "Undo causes a deletion event; the element is removed from the parameter cache. No special undo record is written"],
      ["View creation, sheet setup, annotation lines", "Only FamilyInstance elements in the 20 authoring categories are processed"],
      ["Auto-computed parameters (Area, Volume, Phase Created, Workset, Room: Name)", "These change as side-effects of other operations, not through user intent. Including them would corrupt the Pattern Agent's constant/variable classification"],
      ["Your file path", "Replaced by an SHA-1 hash (first 12 hex characters). The full path is never stored anywhere"],
      ["Element geometry or coordinates", "Not needed for pattern detection and would make log files significantly larger"],
    ],
    [3200, CW - 3200]
  ),
  spacer(),
);

children.push(
  h1("Where Your Logs Are Saved"),
  para([
    r("Every Revit session creates a new "), rBold("JSONL file"), r(" (JSON Lines — one JSON object per line) in:"),
  ]),
  spacer(),
  infoBox("LOG FOLDER", BLUE_MID, [
    rCode("C:\\Users\\DE1E7A\\AppData\\Local\\RevitPersonalization\\logs\\"),
  ]),
  spacer(),
  para([r("Each file is named: "), rCode("session_YYYYMMDD_HHmmss_<projectHash>.jsonl")]),
  para([
    r("The "), rCode("<projectHash>"), r(" is the same for every session on the same project, so you can easily group sessions by project. You currently have "), rBold("two session files"), r(" from your testing on 23 May 2026, totalling 18 recorded actions."),
  ]),
  spacer(),
  table(
    ["Your current session files", "Size", "Contents"],
    [
      [{ code: true, v: "session_20260523_033200_69003c58b5f0.jsonl" }, "4 KB", "7 action records — Place → SetParam(Mark) → Tag × 3 doors"],
      [{ code: true, v: "session_20260523_035731_69003c58b5f0.jsonl" }, "19 KB", "7 action records — same pattern, captured after diagnostic fix"],
    ],
    [4400, 900, CW - 4400 - 900]
  ),
  spacer(),
  h2("Quick inspection command"),
  para([r("Run this from the project folder to see a human-readable summary of all captured actions:")]),
  spacer(),
  codeLine("cd C:\\Users\\DE1E7A\\revit-personalization"),
  codeLine("python check_logs.py"),
  spacer(),
  para([r("This prints:")]),
  codeLine("  PLACE    Doors | Door-Passage-Single-Full_Lite"),
  codeLine("  SETPARAM Mark = 'DD1'  [Doors]"),
  codeLine("  TAG      element 3327603"),
  spacer(),
  h2("Diagnostic file"),
  para([
    r("If you ever suspect the add-in is not capturing actions, check "),
    rCode("logs\\_diag.txt"), r(" in the same folder. It contains millisecond-timestamped traces of every event the add-in handled, including document open/close events, DocKey matches, and write-loop confirmations."),
  ]),
  spacer(),
  table(
    ["Diagnostic entry type", "What it tells you"],
    [
      ["=== RevitLogger OnStartup ===", "Add-in loaded successfully by Revit"],
      ["OnDocumentOpened: title=... path=...", "A project was opened and a new session started"],
      ["OnDocumentChanged: found=True added=1", "An action was captured — found=True means the session key matched"],
      ["WriteLoop: item written and flushed", "The record was successfully written to disk"],
      ["OnDocumentChanged: found=False", "Session key mismatch — document may have been opened before the add-in loaded"],
    ],
    [3200, CW - 3200]
  ),
);

// ═══════════════════════════════════════════════════════════════════════════════
// BUILD DOCUMENT
// ═══════════════════════════════════════════════════════════════════════════════
const doc = new Document({
  numbering: {
    config: [{
      reference: "bullets",
      levels: [{
        level: 0, format: LevelFormat.BULLET, text: "•",
        alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 600, hanging: 300 } } },
      }],
    }],
  },
  styles: {
    default: { document: { run: { font: "Arial", size: 22 } } },
    paragraphStyles: [
      {
        id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 30, bold: true, font: "Arial", color: BLUE },
        paragraph: { spacing: { before: 240, after: 140 }, outlineLevel: 0 },
      },
      {
        id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 24, bold: true, font: "Arial", color: BLUE_MID },
        paragraph: { spacing: { before: 180, after: 100 }, outlineLevel: 1 },
      },
    ],
  },
  sections: [{
    properties: {
      page: {
        size: { width: PAGE_W, height: PAGE_H },
        margin: { top: MARGIN, right: MARGIN, bottom: MARGIN + 200, left: MARGIN },
      },
    },
    headers: {
      default: new Header({
        children: [new Paragraph({
          border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: BLUE_MID, space: 3 } },
          spacing: { before: 0, after: 80 },
          children: [
            r("RevitLogger Add-in  —  BIM Authoring Log Capture", { size: 18, color: GRAY }),
            new TextRun({ children: [new Tab()], font: "Arial", size: 18 }),
          ],
          tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
        })],
      }),
    },
    footers: {
      default: new Footer({
        children: [new Paragraph({
          border: { top: { style: BorderStyle.SINGLE, size: 4, color: BLUE_MID, space: 3 } },
          spacing: { before: 80, after: 0 },
          children: [
            r("Revit 2027  |  .NET 10  |  Jang & Lee (2023) schema", { size: 18, color: GRAY }),
            new TextRun({
              children: [new Tab(), PageNumber.CURRENT],
              font: "Arial", size: 18, color: GRAY,
            }),
          ],
          tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
        })],
      }),
    },
    children,
  }],
});

Packer.toBuffer(doc).then(buf => {
  const out = "C:\\Users\\DE1E7A\\revit-personalization\\docs\\RevitLogger Plugin Overview.docx";
  fs.writeFileSync(out, buf);
  console.log("Created: " + out);
}).catch(e => { console.error(e.message); process.exit(1); });
