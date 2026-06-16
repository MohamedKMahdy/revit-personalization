// generate_architecture_doc.js
// Creates "System Architecture.docx" for the BIM Personalization thesis

const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, HeadingLevel, BorderStyle, WidthType,
  ShadingType, VerticalAlign, PageNumber, TableOfContents, LevelFormat,
  PageBreak, Tab, TabStopType, TabStopPosition
} = require("docx");
const fs = require("fs");

// ── Measurements (A4, 2.5cm margins) ─────────────────────────────────────────
const PAGE_W    = 11906;
const PAGE_H    = 16838;
const MARGIN    = 1418; // ~2.5cm
const CONTENT_W = PAGE_W - MARGIN * 2; // 9070

// ── Colour palette ────────────────────────────────────────────────────────────
const CLR = {
  revitBlue:    "1F4E79",
  revitLight:   "BDD7EE",
  pythonGreen:  "375623",
  pythonLight:  "E2EFDA",
  agentOrange:  "833C00",
  agentLight:   "FCE4D6",
  storageGray:  "595959",
  storageLight: "EDEDED",
  autdeskTeal:  "00616A",
  autdeskLight: "D9EEF0",
  tableHead:    "2E75B6",
  tableHeadTxt: "FFFFFF",
  tableRow1:    "FFFFFF",
  tableRow2:    "EEF4FB",
  codeBg:       "F2F2F2",
  border:       "BFBFBF",
  accentRed:    "C00000",
  accentGreen:  "375623",
  textDark:     "000000",
  mutedGray:    "595959",
};

// ── Helpers ───────────────────────────────────────────────────────────────────
const thinBorder = { style: BorderStyle.SINGLE, size: 1, color: CLR.border };
const allBorders = { top: thinBorder, bottom: thinBorder, left: thinBorder, right: thinBorder };
const noBorder   = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
const noBorders  = { top: noBorder, bottom: noBorder, left: noBorder, right: noBorder };

function h1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    children: [new TextRun({ text, bold: true, font: "Arial", size: 32 })],
    spacing: { before: 360, after: 200 },
  });
}

function h2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    children: [new TextRun({ text, bold: true, font: "Arial", size: 28 })],
    spacing: { before: 280, after: 160 },
  });
}

function h3(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_3,
    children: [new TextRun({ text, bold: true, font: "Arial", size: 24 })],
    spacing: { before: 200, after: 120 },
  });
}

function para(text, opts = {}) {
  return new Paragraph({
    alignment: AlignmentType.JUSTIFIED,
    spacing: { before: 100, after: 160, line: 276 },
    children: [new TextRun({ text, font: "Arial", size: 22, ...opts })],
  });
}

function paraRuns(runs) {
  return new Paragraph({
    alignment: AlignmentType.JUSTIFIED,
    spacing: { before: 100, after: 160, line: 276 },
    children: runs,
  });
}

function run(text, opts = {}) {
  return new TextRun({ text, font: "Arial", size: 22, ...opts });
}

function bold(text) { return run(text, { bold: true }); }
function italic(text) { return run(text, { italics: true }); }
function code(text) { return run(text, { font: "Courier New", size: 20 }); }

function codePara(text) {
  return new Paragraph({
    spacing: { before: 80, after: 80, line: 240 },
    shading: { type: ShadingType.CLEAR, fill: CLR.codeBg },
    indent: { left: 360 },
    children: [new TextRun({ text, font: "Courier New", size: 18 })],
  });
}

function codeBlock(lines) {
  return lines.map(l => codePara(l));
}

function caption(text) {
  return new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 80, after: 240 },
    children: [new TextRun({ text, font: "Arial", size: 20, italics: true, color: CLR.mutedGray })],
  });
}

function note(text) {
  return new Paragraph({
    alignment: AlignmentType.JUSTIFIED,
    spacing: { before: 80, after: 160 },
    indent: { left: 360 },
    children: [new TextRun({ text, font: "Arial", size: 20, italics: true, color: CLR.mutedGray })],
  });
}

function spacer(lines = 1) {
  return Array(lines).fill(null).map(() =>
    new Paragraph({ spacing: { before: 0, after: 0 }, children: [new TextRun("")] })
  );
}

function pageBreak() {
  return new Paragraph({ children: [new PageBreak()] });
}

// Bullet list using numbering reference
function bullet(text, level = 0) {
  return new Paragraph({
    numbering: { reference: "bullets", level },
    spacing: { before: 60, after: 60 },
    children: [new TextRun({ text, font: "Arial", size: 22 })],
  });
}

function numbered(text, level = 0) {
  return new Paragraph({
    numbering: { reference: "numbers", level },
    spacing: { before: 60, after: 60 },
    children: [new TextRun({ text, font: "Arial", size: 22 })],
  });
}

// ── Table helpers ─────────────────────────────────────────────────────────────
function headerCell(text, width, bgColor = CLR.tableHead) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: { type: ShadingType.CLEAR, fill: bgColor },
    borders: allBorders,
    margins: { top: 80, bottom: 80, left: 140, right: 140 },
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({
      alignment: AlignmentType.CENTER,
      children: [new TextRun({ text, font: "Arial", size: 20, bold: true, color: CLR.tableHeadTxt })],
    })],
  });
}

function dataCell(text, width, isAlt = false, align = AlignmentType.LEFT, color = null) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: { type: ShadingType.CLEAR, fill: isAlt ? CLR.tableRow2 : CLR.tableRow1 },
    borders: allBorders,
    margins: { top: 80, bottom: 80, left: 140, right: 140 },
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({
      alignment: align,
      children: [new TextRun({ text, font: "Arial", size: 20, color: color || CLR.textDark })],
    })],
  });
}

function dataCellCode(text, width, isAlt = false) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: { type: ShadingType.CLEAR, fill: isAlt ? CLR.tableRow2 : CLR.tableRow1 },
    borders: allBorders,
    margins: { top: 80, bottom: 80, left: 140, right: 140 },
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({
      children: [new TextRun({ text, font: "Courier New", size: 18 })],
    })],
  });
}

function dataCellRuns(runs, width, isAlt = false) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: { type: ShadingType.CLEAR, fill: isAlt ? CLR.tableRow2 : CLR.tableRow1 },
    borders: allBorders,
    margins: { top: 80, bottom: 80, left: 140, right: 140 },
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({ children: runs.map(r => new TextRun({ ...r, font: r.font || "Arial", size: r.size || 20 })) })],
  });
}

function coloredBoxCell(text, width, bgColor, textColor = "FFFFFF") {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: { type: ShadingType.CLEAR, fill: bgColor },
    borders: allBorders,
    margins: { top: 120, bottom: 120, left: 160, right: 160 },
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({
      alignment: AlignmentType.CENTER,
      children: [new TextRun({ text, font: "Arial", size: 20, bold: true, color: textColor })],
    })],
  });
}

function coloredBoxCellMulti(paragraphs, width, bgColor) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: { type: ShadingType.CLEAR, fill: bgColor },
    borders: allBorders,
    margins: { top: 120, bottom: 120, left: 160, right: 160 },
    verticalAlign: VerticalAlign.CENTER,
    children: paragraphs,
  });
}

function simpleTable(headers, rows, colWidths, totalWidth = CONTENT_W) {
  const headerRow = new TableRow({
    tableHeader: true,
    children: headers.map((h, i) => headerCell(h, colWidths[i])),
  });
  const dataRows = rows.map((row, ri) =>
    new TableRow({
      children: row.map((cell, ci) => {
        if (typeof cell === "string") return dataCell(cell, colWidths[ci], ri % 2 === 1);
        if (cell.code) return dataCellCode(cell.text, colWidths[ci], ri % 2 === 1);
        if (cell.runs) return dataCellRuns(cell.runs, colWidths[ci], ri % 2 === 1);
        return dataCell(cell.text || "", colWidths[ci], ri % 2 === 1, cell.align || AlignmentType.LEFT, cell.color);
      }),
    })
  );
  return new Table({
    width: { size: totalWidth, type: WidthType.DXA },
    columnWidths: colWidths,
    rows: [headerRow, ...dataRows],
  });
}

// ── Architecture Diagram (layered table) ──────────────────────────────────────
function buildArchDiagram() {
  const W = CONTENT_W;
  const half = Math.floor(W / 2);

  function boxPara(text, bold_ = false, size = 19, color = "FFFFFF") {
    return new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { before: 40, after: 40 },
      children: [new TextRun({ text, font: "Arial", size, bold: bold_, color })],
    });
  }
  function smallPara(text, color = "FFFFFF") {
    return new Paragraph({
      alignment: AlignmentType.LEFT,
      spacing: { before: 20, after: 20 },
      children: [new TextRun({ text, font: "Courier New", size: 16, color })],
    });
  }

  const dividerBorder = { style: BorderStyle.SINGLE, size: 6, color: CLR.revitBlue };
  const dividerBorders = { top: dividerBorder, bottom: dividerBorder, left: dividerBorder, right: dividerBorder };

  // Row 1: Revit 2027 header banner
  const revitBanner = new TableRow({
    children: [new TableCell({
      columnSpan: 2,
      width: { size: W, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: CLR.revitBlue },
      borders: dividerBorders,
      margins: { top: 80, bottom: 80, left: 160, right: 160 },
      children: [boxPara("Revit 2027 (Windows Process)", true, 22, "FFFFFF")],
    })],
  });

  // Row 2: Autodesk Assistant | C# Add-in
  const revitInner = new TableRow({
    children: [
      coloredBoxCellMulti([
        boxPara("Autodesk Assistant", true, 19, "FFFFFF"),
        boxPara("(Tech Preview — Chat Panel)", false, 17, "BDD7EE"),
        smallPara("Chat: \"What routines have I learned?\""),
        smallPara("Calls our MCP server tools"),
        smallPara("Calls Autodesk read-only tools"),
      ], half, CLR.autdeskTeal),
      coloredBoxCellMulti([
        boxPara("C# RevitLogger Add-in", true, 19, "FFFFFF"),
        smallPara("App.cs + ActionCapture.cs"),
        smallPara("ElementSnapshot.cs (param diffs)"),
        smallPara("LogWriter.cs (async JSONL)"),
        smallPara("RoutineDetector.cs  [TODO]"),
        smallPara("ShortcutRunner.cs   [TODO]"),
        smallPara("NotificationUI.xaml [TODO]"),
      ], W - half, CLR.revitBlue),
    ],
  });

  // Row 3: Autodesk Public MCP Server
  const autdeskMcp = new TableRow({
    children: [new TableCell({
      columnSpan: 2,
      width: { size: W, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: CLR.autdeskLight },
      borders: allBorders,
      margins: { top: 80, bottom: 80, left: 160, right: 160 },
      children: [
        boxPara("Autodesk Public MCP Server  (READ-ONLY, localhost:3000)", true, 19, CLR.autdeskTeal),
        boxPara("get_elements_by_category  |  get_active_view  |  get_loaded_families  |  (no write operations)", false, 17, CLR.textDark),
      ],
    })],
  });

  // Arrow row
  function arrowRow(text) {
    return new TableRow({
      children: [new TableCell({
        columnSpan: 2,
        width: { size: W, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: "FFFFFF" },
        borders: noBorders,
        margins: { top: 40, bottom: 40, left: 0, right: 0 },
        children: [new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [new TextRun({ text, font: "Arial", size: 18, color: CLR.mutedGray, italics: true })],
        })],
      })],
    });
  }

  // Data storage row
  const storageBanner = new TableRow({
    children: [new TableCell({
      columnSpan: 2,
      width: { size: W, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: CLR.storageLight },
      borders: allBorders,
      margins: { top: 80, bottom: 80, left: 160, right: 160 },
      children: [
        boxPara("Local Data Storage  (%LOCALAPPDATA%\\RevitPersonalization\\)", true, 19, CLR.storageGray),
        boxPara("logs\\session_*.jsonl   |   shortcuts\\*.json   |   ipc\\pending_execution.json   |   ipc\\execution_result_*.json", false, 17, CLR.textDark),
      ],
    })],
  });

  // Python MCP Server
  const third = Math.floor(W / 3);
  const pythonBanner = new TableRow({
    children: [new TableCell({
      columnSpan: 2,
      width: { size: W, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: CLR.pythonLight },
      borders: allBorders,
      margins: { top: 80, bottom: 80, left: 160, right: 160 },
      children: [
        boxPara("Python MCP Server  (FastMCP, port 3100)  --  Registered with Autodesk Assistant", true, 19, CLR.pythonGreen),
        boxPara("Resources: logs://candidate_routines  |  logs://routine/{id}/examples", false, 17, CLR.textDark),
        boxPara("Tools: analyze_pattern  |  generate_command  |  execute_revit_command (via IPC)  |  query_model (read-only)  |  list_shortcuts", false, 17, CLR.textDark),
      ],
    })],
  });

  // Orchestrator
  const orchBanner = new TableRow({
    children: [new TableCell({
      columnSpan: 2,
      width: { size: W, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: CLR.agentLight },
      borders: allBorders,
      margins: { top: 80, bottom: 80, left: 160, right: 160 },
      children: [
        boxPara("Multi-Agent Orchestrator  (orchestrator/agents.py)", true, 19, CLR.agentOrange),
        boxPara("Pattern Agent: claude-opus-4-8 + extended thinking  -->  Extracts Motif JSON from k examples", false, 17, CLR.textDark),
        boxPara("Macro Agent:   claude-sonnet-4-6                    -->  Converts Motif to MCP tool call sequence", false, 17, CLR.textDark),
      ],
    })],
  });

  return new Table({
    width: { size: W, type: WidthType.DXA },
    columnWidths: [half, W - half],
    rows: [
      revitBanner, revitInner, autdeskMcp,
      arrowRow("writes JSONL logs | reads IPC files"),
      storageBanner,
      arrowRow("Python reads logs + IPC results"),
      pythonBanner,
      arrowRow("feeds candidate routines and examples"),
      orchBanner,
    ],
  });
}

// ── Data flow table ───────────────────────────────────────────────────────────
function dataFlowTable(phase, color, steps) {
  const rows = steps.map((s, i) => new TableRow({
    children: [
      new TableCell({
        width: { size: 800, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: i % 2 ? CLR.tableRow2 : CLR.tableRow1 },
        borders: allBorders,
        margins: { top: 60, bottom: 60, left: 120, right: 120 },
        verticalAlign: VerticalAlign.CENTER,
        children: [new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [new TextRun({ text: String(i + 1), font: "Arial", size: 20, bold: true, color })],
        })],
      }),
      new TableCell({
        width: { size: CONTENT_W - 800, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: i % 2 ? CLR.tableRow2 : CLR.tableRow1 },
        borders: allBorders,
        margins: { top: 60, bottom: 60, left: 140, right: 140 },
        children: [new Paragraph({
          children: s.map(r => new TextRun({ ...r, font: r.font || "Arial", size: r.size || 20 })),
        })],
      }),
    ],
  }));

  const headerRow = new TableRow({
    tableHeader: true,
    children: [
      new TableCell({
        columnSpan: 2,
        width: { size: CONTENT_W, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: color },
        borders: allBorders,
        margins: { top: 80, bottom: 80, left: 140, right: 140 },
        children: [new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [new TextRun({ text: phase, font: "Arial", size: 22, bold: true, color: "FFFFFF" })],
        })],
      }),
    ],
  });

  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: [800, CONTENT_W - 800],
    rows: [headerRow, ...rows],
  });
}

// ═══════════════════════════════════════════════════════════════════════════════
// DOCUMENT CONTENT
// ═══════════════════════════════════════════════════════════════════════════════

const children = [];

// ── TITLE PAGE ────────────────────────────────────────────────────────────────
children.push(
  ...spacer(8),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 0, after: 200 },
    children: [new TextRun({ text: "SYSTEM ARCHITECTURE", font: "Arial", size: 52, bold: true, color: CLR.revitBlue })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 0, after: 400 },
    children: [new TextRun({ text: "Agent-Augmented BIM Log Mining for", font: "Arial", size: 34, color: CLR.mutedGray })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 0, after: 600 },
    children: [new TextRun({ text: "Personalized Action Generation", font: "Arial", size: 34, color: CLR.mutedGray })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: CLR.revitBlue, space: 1 } },
    spacing: { before: 0, after: 400 },
    children: [new TextRun({ text: "", font: "Arial", size: 22 })],
  }),
  ...spacer(2),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 0, after: 120 },
    children: [new TextRun({ text: "MSc Thesis — Technical Documentation", font: "Arial", size: 24, italics: true, color: CLR.mutedGray })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 0, after: 120 },
    children: [new TextRun({ text: "Revit 2027  |  .NET 10  |  Python 3.10  |  Claude API", font: "Arial", size: 22, color: CLR.mutedGray })],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 0, after: 120 },
    children: [new TextRun({ text: "June 2026", font: "Arial", size: 22, color: CLR.mutedGray })],
  }),
  pageBreak(),
);

// ── TABLE OF CONTENTS ─────────────────────────────────────────────────────────
children.push(
  h1("Table of Contents"),
  new TableOfContents("Table of Contents", { hyperlink: true, headingStyleRange: "1-3" }),
  pageBreak(),
);

// ═══════════════════════════════════════════════════════════════════════════════
// SECTION 1 — SYSTEM OVERVIEW
// ═══════════════════════════════════════════════════════════════════════════════
children.push(
  h1("1. System Overview"),
  para("This document describes the technical architecture of the BIM Personalization system developed as part of the MSc thesis \"Agent-Augmented BIM Log Mining for Personalized Action Generation.\" The system addresses four research gaps identified in §3.1 of the thesis, specifically targeting the Custom Element Instantiation loop: the repeated sequence of placing a BIM element, setting its parameters, and applying an annotation tag."),
  para("The system observes a Revit user's repetitive modelling actions, automatically detects recurring workflows, and uses a multi-agent Large Language Model (LLM) pipeline to convert those workflows into one-click shortcuts. All data processing and AI inference occur locally on the user's workstation; no project geometry or sensitive model data is transmitted to external servers."),

  h2("1.1 Three-Layer Architecture"),
  para("The system is organised into three decoupled layers, each with a single well-defined responsibility:"),

  bullet("Observe: The C# RevitLogger add-in captures every authoring action in real time by subscribing to Revit 2027 API events. It writes enriched, structured JSONL log files to local disk."),
  bullet("Understand: The Python MCP Server and multi-agent orchestrator read the log files, detect repeated episode patterns, and use Claude API agents to extract generalized workflow motifs."),
  bullet("Act: The C# add-in's ShortcutRunner component (in development) executes learned shortcuts directly via Revit API transactions, triggered via a file-based IPC protocol."),

  ...spacer(1),

  h2("1.2 Autodesk Ecosystem Integration"),
  para("Revit 2027 ships with two AI-related components that are directly relevant to this thesis. Understanding their actual capabilities — particularly the confirmed read-only limitation of the Public MCP Server — informed key architectural decisions."),
  ...spacer(1),

  simpleTable(
    ["Autodesk Component", "What It Does", "Confirmed Limitation", "Our Role"],
    [
      [
        { runs: [{ text: "Autodesk Public MCP Server", bold: true }, { text: " (Tech Preview)" }] },
        "Read-only MCP server exposed by Revit on localhost:3000. Supports model queries: element counts, parameter values, view info.",
        { text: "READ-ONLY confirmed April 2026. Cannot create, modify, or delete elements.", color: CLR.accentRed },
        "Model context queries only (precondition checks before suggesting shortcuts)"
      ],
      [
        { runs: [{ text: "Autodesk Assistant", bold: true }, { text: " (Tech Preview)" }] },
        "AI chat panel embedded in Revit UI. Natural language queries, task automation, schedule creation.",
        "Supports custom MCP server registration. AI reasoning uses Autodesk cloud.",
        "Our Python MCP server registered as additional endpoint. Users query routines and trigger shortcuts via natural language."
      ],
    ],
    [2200, 2400, 2200, 2270],
    CONTENT_W
  ),
  ...spacer(1),
  note("Key architectural finding: The Autodesk Public MCP Server is confirmed read-only in its Tech Preview. All model modification (element placement, parameter setting, tag creation) is performed directly by the C# add-in via the Revit API, using a file-based IPC protocol to receive instructions from the Python layer."),
  ...spacer(1),
);

// ═══════════════════════════════════════════════════════════════════════════════
// SECTION 2 — HIGH-LEVEL ARCHITECTURE
// ═══════════════════════════════════════════════════════════════════════════════
children.push(
  pageBreak(),
  h1("2. High-Level Architecture"),
  para("Figure 1 presents a layered view of the complete system. Each horizontal band represents a logical layer. Vertical position indicates data flow direction: data originates at the top (inside the Revit 2027 process) and flows downward through local storage to the Python processing layer and the multi-agent orchestrator. Execution commands flow upward from the orchestrator back through the Python MCP server and IPC protocol to the C# add-in."),
  ...spacer(1),
  buildArchDiagram(),
  caption("Figure 1. System architecture — layered view. Colour coding: blue = Revit/C# layer, teal = Autodesk ecosystem, gray = local storage, green = Python MCP server, orange = multi-agent orchestrator."),
  ...spacer(1),
  para("The architecture deliberately avoids any direct coupling between the Python orchestrator and the Revit API. The C# add-in is the only component that holds a valid Revit API context; it writes data to shared files (JSONL logs, IPC request/response files) that the Python layer reads and writes asynchronously. This separation means the agent pipeline can be developed, tested, and evaluated entirely without a running Revit instance, using synthetic log data."),
);

// ═══════════════════════════════════════════════════════════════════════════════
// SECTION 3 — DATA FLOW
// ═══════════════════════════════════════════════════════════════════════════════
children.push(
  pageBreak(),
  h1("3. Data Flow"),
  para("The system operates in four distinct phases. The first phase (logging) runs continuously whenever Revit is open. The remaining three phases are triggered on demand — either via the command-line orchestrator, via the Autodesk Assistant chat interface, or (in the planned implementation) automatically via the in-Revit WPF notification UI."),

  h2("3.1 Logging Phase"),
  para("The logging phase runs silently in the background for the entire duration of a Revit session. Every committed transaction is intercepted and recorded."),
  ...spacer(1),
  dataFlowTable("Phase 1: Logging (always active while Revit is open)", CLR.revitBlue, [
    [{ text: "User completes an action in Revit (e.g., places a door, edits a parameter, adds a tag). Revit commits the transaction to the document." }],
    [{ text: "The " }, { text: "DocumentChanged", font: "Courier New" }, { text: " event fires. " }, { text: "ActionCapture.ProcessEvent()", font: "Courier New" }, { text: " is called once per transaction." }],
    [{ text: "A " }, { text: "RecordContext", font: "Courier New" }, { text: " is created, containing a unique " }, { text: "transaction_id", font: "Courier New" }, { text: ", the undo-stack label (" }, { text: "transaction_name", font: "Courier New" }, { text: "), timestamp, and active view context." }],
    [{ text: "Each modified element is classified: FamilyInstance in an authoring category -> Place or SetParam record. IndependentTag added -> Tag record." }],
    [{ text: "For SetParam: " }, { text: "ElementSnapshot.GetChanges()", font: "Courier New" }, { text: " compares current parameter values against the cached baseline, returning only changed parameters with before/after values." }],
    [{ text: "ActionRecord objects are enqueued in a thread-safe " }, { text: "BlockingCollection", font: "Courier New" }, { text: " (capacity 2000). The Revit UI thread returns immediately." }],
    [{ text: "A background Task dequeues records and writes each as a JSON line to " }, { text: "session_YYYYMMDD_HHmmss_<docHash>.jsonl", font: "Courier New" }, { text: ", flushing after every line." }],
  ]),
  ...spacer(1),

  h2("3.2 Detection Phase"),
  para("Routine detection identifies repeated structural patterns across recorded episodes. An episode is the complete sequence of actions performed on a single element: from its initial placement through all parameter changes to its final tagging."),
  ...spacer(1),
  dataFlowTable("Phase 2: Detection (on demand via CLI or MCP server query)", CLR.pythonGreen, [
    [{ text: "log_reader.py", font: "Courier New" }, { text: " reads all " }, { text: "session_*.jsonl", font: "Courier New" }, { text: " files, parsing only ActionRecord lines (skipping session_start and session_end markers)." }],
    [{ text: "Records are sorted by " }, { text: "timestamp_unix", font: "Courier New" }, { text: " and grouped by " }, { text: "element_id", font: "Courier New" }, { text: ". For Tag records, the " }, { text: "tagged_element_id", font: "Courier New" }, { text: " field links the tag to the element episode." }],
    [{ text: "Only episodes that include at least one Place record are retained. This ensures complete episodes (element witnessed from placement)." }],
    [{ text: "A structural signature is computed per episode: " }, { text: "\"<category>|<family>|Place,SetParam(Mark),Tag\"", font: "Courier New" }, { text: ". Two episodes with identical signatures are instances of the same routine." }],
    [{ text: "Episodes are grouped by signature. Groups with 2 or more members become " }, { text: "CandidateRoutine", font: "Courier New" }, { text: " objects, sorted by count descending." }],
  ]),
  ...spacer(1),

  h2("3.3 Extraction Phase"),
  para("The extraction phase applies the multi-agent LLM pipeline to convert k recorded examples of a candidate routine into a generalized Motif and an executable tool call sequence. This phase can be triggered via the command-line orchestrator or via the Autodesk Assistant chat interface after the Python MCP server has been registered."),
  ...spacer(1),
  dataFlowTable("Phase 3: Extraction (orchestrator or Autodesk Assistant)", CLR.agentOrange, [
    [{ text: "k examples are fetched from " }, { text: "log_reader.get_routine_examples(id, k)", font: "Courier New" }, { text: ". Each example is a list of ActionRecord dicts for one element episode." }],
    [{ text: "The Pattern Agent (claude-opus-4-8 with extended thinking) receives all k examples. It identifies the invariant action sequence and classifies each SetParam step as constant (same value in every example) or variable (value differs across examples)." }],
    [{ text: "The Pattern Agent returns a Motif JSON object containing: name, description, ordered steps (action_type, family_name, param_name, param_value, param_value_type, tag_family_name), preconditions, and parameters_to_prompt." }],
    [{ text: "The Macro Agent (claude-sonnet-4-6) receives the Motif and translates it into an ordered list of MCP tool calls: place_element, set_parameter (with constant values or {{ParamName}} placeholders), create_annotation_tag." }],
    [{ text: "The user reviews a dry-run preview of the tool call sequence and confirms. A " }, { text: "ShortcutConfig.json", font: "Courier New" }, { text: " is saved to " }, { text: "shortcuts/<id>.json", font: "Courier New" }, { text: "." }],
  ]),
  ...spacer(1),

  h2("3.4 Execution Phase"),
  para("The execution phase applies a saved shortcut to the live Revit model. Because the Autodesk Public MCP Server is read-only, execution is handled by the C# add-in via a file-based IPC protocol. The Python layer writes a request file; the C# add-in detects it, executes the Revit API transaction, and writes a result file."),
  ...spacer(1),
  dataFlowTable("Phase 4: Execution (C# add-in via file IPC)", CLR.revitBlue, [
    [{ text: "Execution is triggered by the user (CLI --execute flag, WPF button in Revit, or Autodesk Assistant chat command)." }],
    [{ text: "revit_bridge.execute_shortcut(shortcut_id, params)", font: "Courier New" }, { text: " writes " }, { text: "ipc/pending_execution.json", font: "Courier New" }, { text: " containing the shortcut ID and any runtime parameter overrides." }],
    [{ text: "The C# " }, { text: "ShortcutRunner.cs", font: "Courier New" }, { text: " FileSystemWatcher detects the new file. It reads the corresponding " }, { text: "ShortcutConfig.json", font: "Courier New" }, { text: " from the shortcuts directory." }],
    [{ text: "A Revit API transaction is opened. For each step: place_element -> FamilyInstanceCreationData; set_parameter -> fi.get_Parameter(name).Set(value); create_annotation_tag -> IndependentTag.Create()." }],
    [{ text: "The transaction is committed. " }, { text: "ipc/execution_result_<id>.json", font: "Courier New" }, { text: " is written with status, created element IDs, and timestamp." }],
    [{ text: "Python polls for the result file (250ms interval, 30s timeout) and returns it to the caller." }],
  ]),
  ...spacer(1),

  h2("3.5 Model Context Queries"),
  para("Before suggesting or executing a shortcut, the system optionally queries the live model via the Autodesk Public MCP Server to verify preconditions: confirming that the required family is loaded, checking the active view type, and counting existing elements. These queries use the read-only tools exposed by the Autodesk server at localhost:3000. If the server is unreachable, the system proceeds without precondition checking."),
);

// ═══════════════════════════════════════════════════════════════════════════════
// SECTION 4 — AUTODESK ECOSYSTEM
// ═══════════════════════════════════════════════════════════════════════════════
children.push(
  pageBreak(),
  h1("4. Autodesk Ecosystem Integration"),

  h2("4.1 Autodesk Public MCP Server — Read-Only Queries"),
  para("The Autodesk Public MCP Server is a Tech Preview feature bundled with Revit 2027. It automatically starts when a project document is open and exposes a JSON-RPC 2.0 endpoint at localhost:3000. External tools, including Claude Desktop and other MCP clients, can connect to this endpoint to query the live model."),
  para("The server exposes six tool groups. As confirmed in the April 2026 Tech Preview release, all tools are read-only: no modifications to the model are possible through this interface."),
  ...spacer(1),
  simpleTable(
    ["Tool Group", "Example Tools", "Our Usage"],
    [
      ["Model queries", "get_elements_by_category, get_element_parameters, get_active_view", "Precondition checks: verify family is loaded, check active view type"],
      ["Sheet management", "get_sheets, get_views", "Not used in current implementation"],
      ["Room management", "get_rooms, get_room_boundaries", "Not used in current implementation"],
      ["Schedules", "get_schedules, get_schedule_data", "Not used in current implementation"],
      ["Exports", "export_to_dwg, export_to_ifc", "Not used in current implementation"],
      ["Element operations", "Query elements and their properties (read-only)", "Family availability checks"],
    ],
    [2500, 3500, 3070],
    CONTENT_W
  ),
  ...spacer(1),
  paraRuns([bold("Confirmed limitation (April 2026 Tech Preview): "), italic("\"The toolset is limited to read-only operations at the moment — no Revit modifications are possible.\"")]),
  para("This limitation directly motivated the architectural decision to route all model modifications through the C# add-in rather than the Autodesk server. The read-only server is retained in the architecture specifically for its model context query capabilities, which are valuable for precondition checking and do not require any special write permissions."),

  h2("4.2 Autodesk Assistant — Conversational Interface"),
  para("The Autodesk Assistant is an AI-powered chat panel embedded in the Revit 2027 user interface. Unlike the Public MCP Server, the Assistant has broader internal capabilities including schedule creation and element tagging via natural language. Critically for this thesis, it supports the registration of additional custom local MCP servers as supplementary endpoints."),
  para("By registering the Python personalization MCP server (server.py, port 3100) with the Autodesk Assistant, users gain a natural language interface to the personalization system without requiring a separate application. This directly addresses Research Gap 4 identified in the thesis: the absence of proactive, real-time shortcut suggestion integrated with the Autodesk ecosystem."),
  ...spacer(1),
  h3("4.2.1 Registration Procedure"),
  para("Registration requires a one-time configuration in the Autodesk Assistant settings panel:"),
  numbered("Start the Python MCP server: python mcp_server/server.py (port 3100)"),
  numbered("In Revit, open Autodesk Assistant and navigate to Settings -> Add MCP Server"),
  numbered("Enter the server name (revit-personalization) and URL (http://localhost:3100/sse)"),
  numbered("The Assistant discovers all exposed tools and resources automatically via MCP introspection"),
  ...spacer(1),
  h3("4.2.2 Natural Language Interaction Examples"),
  ...spacer(1),
  simpleTable(
    ["User Input (in Autodesk Assistant chat)", "MCP Call Triggered", "System Response"],
    [
      ["\"What repetitive routines have I been doing?\"", "Resource: logs://candidate_routines", "Lists all detected CandidateRoutine objects with labels and repeat counts"],
      ["\"Show me examples of my door placement routine\"", "Resource: logs://routine/{id}/examples", "Returns k recorded action sequences for inspection"],
      ["\"Save my door routine as a shortcut\"", "Tool: generate_command(motif, name)", "Pattern Agent extracts motif; Macro Agent generates tool sequence; shortcut saved"],
      ["\"Run my door shortcut with Mark D-105\"", "Tool: execute_revit_command(id, params)", "Python writes IPC request; C# add-in executes Place + SetParam + Tag in Revit"],
      ["\"How many doors are on Level 1?\"", "Autodesk: get_elements_by_category", "Answered by Autodesk's own model query tool (read-only)"],
    ],
    [3000, 2500, 3570],
    CONTENT_W
  ),
  ...spacer(1),

  h2("4.3 Why the C# Add-in Handles Execution"),
  para("The Autodesk Assistant can perform some model modifications via its own internal tool groups (for example, creating schedules or tagging elements). However, it is not suitable as the execution engine for learned shortcuts for four reasons:"),
  bullet("Non-determinism: The Assistant operates via natural language prompts, meaning the same shortcut invocation may produce slightly different results depending on the AI's interpretation. Learned shortcuts require exactly reproducible execution."),
  bullet("No concept of stored shortcuts: The Assistant has no mechanism to store a named sequence of steps with specific parameter values derived from recorded user behaviour."),
  bullet("Cloud AI dependency: The Assistant uses Autodesk's cloud AI services for its reasoning, which raises privacy concerns for firms with strict IP policies."),
  bullet("Transaction control: Executing multiple coordinated Revit API calls in a single atomic transaction requires direct API access that only the C# add-in provides."),
  ...spacer(1),
  para("The C# add-in's ShortcutRunner component executes shortcuts deterministically from a stored ShortcutConfig.json file: once the user confirms the shortcut, every subsequent execution is byte-for-byte identical for the same parameter inputs."),
);

// ═══════════════════════════════════════════════════════════════════════════════
// SECTION 5 — COMPONENT REFERENCE
// ═══════════════════════════════════════════════════════════════════════════════
children.push(
  pageBreak(),
  h1("5. Component Reference"),

  h2("5.1 C# Add-in — RevitLogger/"),
  para("The C# add-in is the only component that runs inside the Revit 2027 process. It is deliberately minimal — logging only, no pattern detection, no AI — to minimize Revit startup time and reduce the risk of destabilising the host application. All intelligence resides in the Python layer."),
  ...spacer(1),
  simpleTable(
    ["File", "Status", "Primary Responsibility"],
    [
      [{ code: true, text: "App.cs" }, "Complete", "IExternalApplication entry point. Subscribes to DocumentChanged, DocumentOpened, DocumentCreated, DocumentClosing. Manages one session (ActionCapture + LogWriter) per open project document."],
      [{ code: true, text: "ActionCapture.cs" }, "Complete", "Handles DocumentChangedEventArgs. Classifies added/modified elements as Place, SetParam, or Tag. Applies authoring category filter. Creates ActionRecord objects."],
      [{ code: true, text: "ElementSnapshot.cs" }, "Complete", "In-memory parameter value cache keyed by element_id. Provides before/after diffs for SetParam detection. Filters out read-only and auto-computed parameters."],
      [{ code: true, text: "LogWriter.cs" }, "Complete", "Thread-safe async JSONL writer using BlockingCollection<object> and System.Text.Json. Background Task writes and flushes after every record. Sidecar error file on failure."],
      [{ code: true, text: "ActionRecord.cs" }, "Complete", "C# DTO for the enriched BIM log schema (Jang & Lee 2023). All fields decorated with [JsonPropertyName] for snake_case JSON output."],
      [{ code: true, text: "SessionInfo.cs" }, "Complete", "Session metadata written as line 1 of each JSONL file. Document path SHA-1 hashed for privacy. Revit version and document title captured."],
      [{ code: true, text: "RoutineDetector.cs" }, "TODO", "Rolling buffer of last 50 ActionRecords. Checks for repeated structural signatures after each new record. Raises event when a candidate routine reaches minimum repeat threshold."],
      [{ code: true, text: "ShortcutRunner.cs" }, "TODO", "FileSystemWatcher on ipc/ directory. Reads pending_execution.json, loads ShortcutConfig, executes Place/SetParam/Tag steps in a Revit transaction. Writes execution_result_*.json."],
      [{ code: true, text: "NotificationUI.xaml" }, "TODO", "Non-modal WPF toast window. Displays routine label and step count. Provides Learn as Shortcut, Run Shortcut, and Dismiss actions."],
    ],
    [2200, 1100, 5770],
    CONTENT_W
  ),
  ...spacer(1),

  h2("5.2 Shared Data Schemas — shared/schemas.py"),
  para("The Python shared schemas module (Pydantic v2) defines the data contract between all Python components. All field names use snake_case, matching the JSON keys produced by the C# add-in. Any component that reads log files or communicates with the MCP server uses these models for validation and serialisation."),
  ...spacer(1),
  simpleTable(
    ["Model", "Used By", "Description"],
    [
      ["ActionRecord", "log_reader, orchestrator", "One atomic BIM authoring event. Maps 1:1 to the C# ActionRecord DTO."],
      ["RoutineExample", "log_reader, orchestrator", "One recorded repetition of a candidate routine: example_id, session_id, recorded_at, list of ActionRecords."],
      ["CandidateRoutine", "MCP server resources, orchestrator input", "A detected candidate routine: id, label, action_signature, count, confidence, list of RoutineExamples."],
      ["MotifStep / Motif", "Pattern Agent output, Macro Agent input, server tools", "Generalised routine representation. MotifStep has action_type, family_name, param_name, param_value, param_value_type, tag_family_name."],
      ["ShortcutConfig", "Saved to disk; loaded by ShortcutRunner", "Complete shortcut definition: shortcut_id, name, Motif, mcp_tool_sequence (list of tool/arguments dicts)."],
    ],
    [1800, 2600, 4670],
    CONTENT_W
  ),
  ...spacer(1),

  h2("5.3 Python MCP Server — mcp_server/"),
  ...spacer(1),
  simpleTable(
    ["File", "Responsibility"],
    [
      ["log_reader.py", "Parses JSONL session files. Groups ActionRecords by element_id into episodes. Computes structural signatures. Groups matching episodes into CandidateRoutine objects. Also loads synthetic test data from tests/synthetic_logs/."],
      ["server.py", "FastMCP server (port 3100). Exposes 2 resources (logs://candidate_routines, logs://routine/{id}/examples) and 5 tools (analyze_pattern, generate_command, execute_revit_command, query_model, list_shortcuts). Registered with Autodesk Assistant as additional MCP endpoint."],
      ["revit_bridge.py", "Two-channel integration layer. model_query() sends read-only HTTP requests to Autodesk Public MCP Server (localhost:3000). execute_shortcut() writes IPC request files and polls for C# add-in results."],
    ],
    [2200, 6870],
    CONTENT_W
  ),
  ...spacer(1),

  h2("5.4 Multi-Agent Orchestrator — orchestrator/"),
  ...spacer(1),
  simpleTable(
    ["File", "Model", "Role"],
    [
      ["pattern_agent.py", "claude-opus-4-8 + extended thinking", "Receives k RoutineExample dicts. Identifies invariant action sequence. Classifies each SetParam as constant or variable. Returns Motif JSON with steps, preconditions, and parameters_to_prompt."],
      ["macro_agent.py", "claude-sonnet-4-6", "Receives Motif JSON. Translates each step to a Revit MCP tool call (place_element, set_parameter, create_annotation_tag) with appropriate argument values or runtime placeholders."],
      ["agents.py", "CLI coordinator", "Fetches routine examples, calls both agents in sequence, displays dry-run preview, saves ShortcutConfig on user confirmation. Supports --list, --execute, --auto-confirm, --params flags."],
    ],
    [1800, 2600, 4670],
    CONTENT_W
  ),
  ...spacer(1),

  h2("5.5 Evaluation Harness — eval/run_experiment.py"),
  para("The evaluation harness measures Pattern Agent accuracy as a function of k (the number of example episodes provided). For each (routine, k, repetition) cell, the harness runs the Pattern Agent, scores the returned Motif against the ground-truth episode structure, and records step_match_accuracy, param_coverage, spurious_steps, token usage, and latency. Results are written to results/performance_vs_k.csv for inclusion in the thesis evaluation tables (§5)."),
);

// ═══════════════════════════════════════════════════════════════════════════════
// SECTION 6 — IPC PROTOCOL
// ═══════════════════════════════════════════════════════════════════════════════
children.push(
  pageBreak(),
  h1("6. Python to C# IPC Protocol"),
  para("Because the Python orchestrator and the C# add-in run in separate processes (Python subprocess vs. the Revit 2027 process), they communicate via files written to a shared directory: %LOCALAPPDATA%\\RevitPersonalization\\ipc\\. This approach was chosen over alternatives (named pipes, sockets, COM) because it requires no custom server in the add-in, is robust to process restarts, and leaves a visible audit trail."),

  h2("6.1 Request File — pending_execution.json"),
  para("The Python layer writes this file when a shortcut execution is requested. The C# add-in's FileSystemWatcher is notified immediately after the file is created."),
  ...spacer(1),
  ...codeBlock([
    "// %LOCALAPPDATA%\\RevitPersonalization\\ipc\\pending_execution.json",
    "{",
    "  \"shortcut_id\": \"a1b2c3d4\",",
    "  \"params\": {",
    "    \"Mark\": \"D-105\"",
    "  },",
    "  \"requested_at\": 1779509164.268",
    "}",
  ]),
  ...spacer(1),
  simpleTable(
    ["Field", "Type", "Description"],
    [
      ["shortcut_id", "string", "ID of the ShortcutConfig to execute. Must match a file in shortcuts/<id>.json."],
      ["params", "object", "Runtime parameter overrides. Keys are parameter names; values replace {{ParamName}} placeholders in the tool call sequence."],
      ["requested_at", "float", "Unix timestamp (seconds). Used by ShortcutRunner to detect stale requests (e.g., if Revit was restarted)."],
    ],
    [2000, 1200, 5870],
    CONTENT_W
  ),
  ...spacer(1),

  h2("6.2 Response File — execution_result_{id}.json"),
  para("The C# add-in writes this file after completing (or failing) the requested execution. Python polls for its existence with a 250ms interval and 30-second timeout."),
  ...spacer(1),
  ...codeBlock([
    "// %LOCALAPPDATA%\\RevitPersonalization\\ipc\\execution_result_a1b2c3d4.json",
    "{",
    "  \"shortcut_id\": \"a1b2c3d4\",",
    "  \"status\": \"success\",",
    "  \"steps_executed\": 3,",
    "  \"element_ids_created\": [3327603, 3327683],",
    "  \"executed_at\": 1779509167.5",
    "}",
  ]),
  ...spacer(1),
  simpleTable(
    ["Field", "Type", "Description"],
    [
      ["status", "string", "\"success\" or \"error\". On error, an additional \"error_message\" field is included."],
      ["steps_executed", "integer", "Number of tool call steps that completed successfully."],
      ["element_ids_created", "array", "Revit element IDs of all newly created elements (placed family instances and tags)."],
      ["executed_at", "float", "Unix timestamp when execution completed. Used to verify freshness."],
    ],
    [2200, 1200, 5670],
    CONTENT_W
  ),
  ...spacer(1),

  h2("6.3 Protocol Sequence"),
  para("The following sequence describes the complete round-trip from a shortcut execution request to result delivery:"),
  numbered("Python writes pending_execution.json to the ipc/ directory."),
  numbered("C# FileSystemWatcher.Created event fires immediately."),
  numbered("C# reads the request, loads the ShortcutConfig from shortcuts/<id>.json."),
  numbered("C# opens a Revit API transaction and executes each step (Place, SetParam, Tag)."),
  numbered("C# commits the transaction and writes execution_result_<id>.json."),
  numbered("Python detects the result file (next 250ms poll cycle) and reads it."),
  numbered("Python deletes the result file and returns the result to the caller."),
  ...spacer(1),
  note("Limitation: The current implementation does not handle concurrent shortcut requests. If two execution requests are written simultaneously, the second request may overwrite the first pending_execution.json. For the single-user thesis evaluation scenario, this is not a concern, but it should be noted as a limitation in the thesis."),
);

// ═══════════════════════════════════════════════════════════════════════════════
// SECTION 7 — TECHNOLOGY CHOICES
// ═══════════════════════════════════════════════════════════════════════════════
children.push(
  pageBreak(),
  h1("7. Technology Choices and Rationale"),
  para("Each technology choice was motivated by specific constraints or requirements. This section documents the rationale to support reproducibility and to guide future extensions of the system."),
  ...spacer(1),
  simpleTable(
    ["Technology", "Rationale"],
    [
      ["C# / .NET 10 (add-in)", "The Revit 2027 API requires a CLR-based plugin. Only code running inside the Revit process can subscribe to DocumentChanged and execute transactions. .NET 10 is required because the Revit 2027 SDK assemblies target System.Runtime version 10.0."],
      ["System.Text.Json (no NuGet)", "Organisation IT policy blocks external NuGet package sources. System.Text.Json is built into .NET 10 and provides full JSON serialisation support without dependencies."],
      ["JSONL (JSON Lines format)", "Append-only format: each line is independently parseable. If Revit crashes mid-session, all lines written before the crash are valid. Compatible with streaming log analysis tools (jq, Python line-by-line reading)."],
      ["File-based IPC (ipc/ directory)", "No custom HTTP server or named-pipe infrastructure required in the C# add-in. FileSystemWatcher is built into .NET. The shared directory provides a natural audit trail. Survives process restarts on either side."],
      ["FastMCP (Python)", "Single-file declarative MCP server definition. Compatible with MCP Inspector, Claude Desktop, and the Autodesk Assistant registration mechanism. The mcp Python library handles protocol framing."],
      ["Pydantic v2 (Python)", "Strong runtime validation of the schema contract between components. model_dump() and model_validate_json() provide clean serialisation without boilerplate. Extra field tolerance (extra=ignore) ensures forward compatibility."],
      ["claude-opus-4-8 + extended thinking", "Extended thinking provides the deepest available reasoning for the constant/variable parameter classification task. Classifying a parameter as constant when it is actually variable would produce an incorrect shortcut that silently overwrites user intent. Opus with thinking minimises this risk."],
      ["claude-sonnet-4-6 (Macro Agent)", "The Macro Agent's task (translating a structured Motif JSON to a structured tool call sequence) is a deterministic format conversion, not a reasoning task. Sonnet provides fast, cost-effective structured output generation."],
      ["No cloud log upload", "Thesis §3.1 gap 3 explicitly identifies privacy and IP constraints as a barrier to BIM log analysis. The system was designed from the start to keep all log data local: only action type strings and parameter name/value pairs are sent to the Claude API, never model geometry."],
    ],
    [2500, 6570],
    CONTENT_W
  ),
);

// ═══════════════════════════════════════════════════════════════════════════════
// SECTION 8 — DEPLOYMENT TOPOLOGY
// ═══════════════════════════════════════════════════════════════════════════════
children.push(
  pageBreak(),
  h1("8. Deployment Topology"),
  para("The entire system runs on a single Windows workstation with Revit 2027 installed. There are no external server dependencies beyond the Anthropic API (called only during the extraction phase when the orchestrator is invoked). The deployment footprint is three items: the compiled C# DLL deployed to the Revit add-ins directory, the Python repository, and the shared data directory created automatically at runtime."),

  h2("8.1 File System Layout"),
  ...spacer(1),
  ...codeBlock([
    "Developer machine (Windows, Revit 2027 installed)",
    "",
    "C:\\Program Files\\Autodesk\\Revit 2027\\",
    "  RevitAPI.dll, RevitAPIUI.dll          (reference only, not shipped)",
    "",
    "%APPDATA%\\Autodesk\\Revit\\Addins\\2027\\",
    "  RevitLogger.addin                     (manifest, deployed once)",
    "  RevitLogger.dll                       (built + deployed via deploy.ps1)",
    "",
    "%LOCALAPPDATA%\\RevitPersonalization\\",
    "  logs\\session_*.jsonl                  (add-in writes at runtime)",
    "  logs\\_diag.txt                        (diagnostic trace, add-in writes)",
    "  shortcuts\\*.json                      (orchestrator writes, add-in reads)",
    "  ipc\\pending_execution.json            (Python writes, C# reads)",
    "  ipc\\execution_result_*.json           (C# writes, Python reads)",
    "",
    "revit-personalization\\  (this Git repository)",
    "  mcp_server\\server.py    -> python mcp_server/server.py  (port 3100)",
    "  orchestrator\\agents.py  -> python orchestrator/agents.py --routine-id ...",
    "  eval\\run_experiment.py  -> python eval/run_experiment.py",
    "",
    "Autodesk Public MCP Server              (bundled with Revit 2027)",
    "  auto-starts when project is open, port 3000, read-only",
  ]),
  ...spacer(1),

  h2("8.2 Build and Deployment"),
  para("The C# add-in is built using the .NET SDK command-line tools and deployed using the included deploy.ps1 PowerShell script. Revit must be closed before deployment because it locks the DLL while running."),
  ...spacer(1),
  ...codeBlock([
    "# Build",
    "cd RevitLogger",
    "dotnet build -c Release",
    "",
    "# Deploy (close Revit first)",
    ".\\deploy.ps1",
  ]),
  ...spacer(1),

  h2("8.3 Autodesk Assistant Registration"),
  para("After starting the Python MCP server, register it with the Autodesk Assistant using the following one-time procedure:"),
  numbered("Start the MCP server: python mcp_server/server.py"),
  numbered("Open Revit 2027 and click the Autodesk Assistant icon in the ribbon"),
  numbered("Navigate to Settings -> MCP Servers -> Add Server"),
  numbered("Enter name: revit-personalization and URL: http://localhost:3100/sse"),
  numbered("Click Save. The Assistant will introspect the server and confirm tool discovery."),
  ...spacer(1),
  para("Once registered, the Autodesk Assistant can call all tools and resources exposed by server.py. The registration persists across Revit sessions; the Python server must be running for the tools to be available."),
);

// ═══════════════════════════════════════════════════════════════════════════════
// SECTION 9 — DATA SCHEMA (REVIT PLUGIN)
// ═══════════════════════════════════════════════════════════════════════════════
children.push(
  pageBreak(),
  h1("9. Data Schema: RevitLogger Add-in"),
  para("The RevitLogger add-in implements the enhanced BIM logging schema proposed by Jang & Lee (2023) and extended with the lexicon from Jang et al. (2023). This section documents every field captured in the log files, its source in the Revit API, and the scientific justification for its inclusion."),

  h2("9.1 Session Start Record"),
  para("Written as line 1 of every JSONL session file. Provides metadata for session-level analysis and privacy-preserving project identification."),
  ...spacer(1),
  ...codeBlock([
    "{",
    "  \"record_type\":    \"session_start\",",
    "  \"schema_version\": \"2.0\",",
    "  \"session_id\":     \"sess_20260523035731\",",
    "  \"timestamp_utc\":  \"2026-05-23T03:57:31.935Z\",",
    "  \"revit_version\":  \"Autodesk Revit 2027\",",
    "  \"document_hash\":  \"69003c58b5f0\",",
    "  \"document_title\": \"Snowdon Towers Sample Architectural\"",
    "}",
  ]),
  ...spacer(1),
  simpleTable(
    ["Field", "Why Collected"],
    [
      ["record_type: \"session_start\"", "Allows parsers to distinguish metadata lines from action lines without inspecting all fields."],
      ["schema_version: \"2.0\"", "Forward-compatibility versioning. Parsers can handle log files from different add-in versions gracefully."],
      ["session_id", "Groups all records in this file. Referenced by CandidateRoutine objects to trace which session produced which episodes."],
      ["timestamp_utc", "Absolute time anchor. Enables cross-session timeline analysis and session duration measurement."],
      ["revit_version", "Documents the authoring tool version. Required because API behaviour and available element types differ between Revit versions."],
      ["document_hash", "First 12 hex chars of SHA1(doc.PathName.ToLower()). Identifies the project without exposing the file system path (privacy — thesis §3.1 gap 3)."],
      ["document_title", "Human-readable project identifier derived from the filename. Used for log inspection and debugging only."],
    ],
    [2800, 6270],
    CONTENT_W
  ),
  ...spacer(1),

  h2("9.2 Place Record"),
  para("Emitted when a FamilyInstance is added to the document in an authoring category. This is the opening record of a Custom Element Instantiation episode."),
  ...spacer(1),
  simpleTable(
    ["Field", "Revit API Source", "Why Collected"],
    [
      ["action_type: \"Place\"", "Code logic (AddedElementIds + FamilyInstance)", "Primary action taxonomy from Jang et al. (2023) AEI lexicon. Identifies episode start."],
      ["operation_class: \"Model\"", "Enum assignment", "Jang et al. (2023) AEI taxonomy: Model / Parameter / Annotation / View. Enables class-level filtering without string matching."],
      ["transaction_id", "Guid per DocumentChangedEventArgs", "Groups all records from one atomic Revit transaction. Jang & Lee (2023) §3.2: essential for reproducibility — one user intent may modify multiple elements."],
      ["transaction_name", "e.GetTransactionNames()", "The undo-stack label (e.g., \"Door\", \"Place Component\"). Provides semantic intent without requiring NLP."],
      ["element_id", "fi.Id.Value (long cast to int)", "Primary key for episode grouping. All SetParam and Tag records for this element carry the same element_id."],
      ["element_category", "fi.Category.Name", "Used as first component of episode signature for routine detection (e.g., \"Doors\")."],
      ["family_name", "fi.Symbol.Family.Name", "Most important field for routine detection. Two placements of the same family are candidate instances of the same routine."],
      ["type_name", "fi.Symbol.Name", "Specific type within the family (e.g., \"36\\\" x 84\\\"\"). Detects whether the user always selects the same type."],
      ["level_name", "doc.GetElement(fi.LevelId).Name", "Spatial context. Jang & Lee (2023): many routines are level-specific."],
      ["phase_name", "PHASE_CREATED BuiltInParameter", "Temporal project context. Required for full reproducibility per Jang & Lee (2023) §4.1."],
      ["host_category", "fi.Host?.Category?.Name", "For hosted elements: distinguishes wall-hosted from curtain-wall-hosted doors."],
      ["view_id / view_name / view_type", "doc.ActiveView properties", "Required Jang & Lee schema field. ViewType (FloorPlan / Elevation / 3D) used for precondition detection."],
    ],
    [2000, 2200, 4870],
    CONTENT_W
  ),
  ...spacer(1),

  h2("9.3 SetParam Record"),
  para("Emitted when a parameter value changes on a tracked FamilyInstance. The ElementSnapshot mechanism provides the before value, which is not available from the DocumentChangedEventArgs alone."),
  ...spacer(1),
  simpleTable(
    ["Field", "Source", "Why Collected"],
    [
      ["param_name", "p.Definition.Name", "Parameter being changed (e.g., \"Mark\", \"Fire Rating\"). Used by Pattern Agent to identify which parameters belong to the routine."],
      ["param_storage_type", "p.StorageType.ToString()", "String / Integer / Double. Tells the execution layer how to set the value via the Revit API."],
      ["param_value_before", "ElementSnapshot cache", "Jang & Lee (2023) §3.3: before/after diffs enable audit trails and undo-aware replay. Null for first modification after placement."],
      ["param_value_after", "p.AsString() / AsInteger() / AsDouble()*304.8", "New value. Pattern Agent uses this across k examples to classify as constant or variable. Doubles converted from decimal feet to millimetres."],
    ],
    [2200, 2200, 4670],
    CONTENT_W
  ),
  ...spacer(1),

  h2("9.4 Tag Record"),
  para("Emitted when an IndependentTag is added to the document. Tags are captured only on creation; modifications (leader repositioning) are explicitly suppressed as noise. The tagged_element_id field links the tag to its element episode."),
  ...spacer(1),
  simpleTable(
    ["Field", "Source", "Why Collected"],
    [
      ["tag_family_name", "doc.GetElement(tag.GetTypeId()) as FamilySymbol", "Which tag family was applied. The routine may always use \"Door Tag\" (constant) or vary by element type."],
      ["tagged_element_id", "tag.GetTaggedReferences()[0] -> doc.GetElement(ref).Id", "Critical for episode linkage. Attaches the Tag record to the element episode in log_reader.py. Without this, tags are orphaned and the Place -> SetParam -> Tag episode cannot be reconstructed."],
    ],
    [2200, 2200, 4670],
    CONTENT_W
  ),
  ...spacer(1),

  h2("9.5 Parameter Exclusion List"),
  para("The ElementSnapshot.ShouldTrack() method excludes parameters that change automatically as side-effects of other operations rather than through direct user intent. Including them would produce spurious SetParam records and confuse the Pattern Agent's constant/variable classification."),
  ...spacer(1),
  simpleTable(
    ["Excluded Parameter", "Reason"],
    [
      ["Area, Volume, Perimeter", "Computed from geometry. Change whenever the element's geometry changes, not because the user set them."],
      ["Phase Created, Phase Demolished", "Set automatically by Revit based on the active phase at placement time."],
      ["Work Plane, Host, Workset", "Structural / collaborative metadata set by Revit infrastructure, not by the designer."],
      ["Design Option", "Project organisation metadata. Not part of a design intent workflow."],
      ["Image", "Not a meaningful shortcut parameter."],
      ["Moves With Nearby Elements", "Internal Revit behaviour flag, not user design intent."],
      ["Room: Name, Room: Number, Space: Name, Space: Number", "Computed from spatial relationships. Change whenever room boundaries change."],
      ["Family, Family and Type", "Read-only type descriptors. Cannot be set via parameter assignment."],
    ],
    [3000, 6070],
    CONTENT_W
  ),
);

// ═══════════════════════════════════════════════════════════════════════════════
// SECTION 10 — PRIVACY AND DATA HANDLING
// ═══════════════════════════════════════════════════════════════════════════════
children.push(
  pageBreak(),
  h1("10. Privacy and Data Handling"),
  para("Privacy and intellectual property protection are explicit requirements identified in the thesis (§3.1 gap 3). The system was designed from the start to minimise data exposure. This section documents exactly what data is and is not collected, and where each category of data goes."),

  h2("10.1 What Is Collected"),
  simpleTable(
    ["Data Category", "What Specifically", "Where It Goes"],
    [
      ["Action types", "\"Place\", \"SetParam\", \"Tag\" strings", "Local JSONL files only"],
      ["Element categories", "\"Doors\", \"Windows\", etc.", "Local JSONL files only"],
      ["Family and type names", "\"Door-Passage-Single-Full_Lite\", \"36\\\" x 84\\\"\"", "Local JSONL files + Claude API calls"],
      ["Parameter names", "\"Mark\", \"Fire Rating\", \"Width\"", "Local JSONL files + Claude API calls"],
      ["Parameter values", "\"D-101\", \"60\", \"914\" (in mm)", "Local JSONL files + Claude API calls"],
      ["Level and phase names", "\"L1 - Block 35\", \"New Construction\"", "Local JSONL files only"],
      ["View name and type", "\"L1\", \"FloorPlan\"", "Local JSONL files only"],
      ["Document hash", "SHA1(path)[0:12] — not reversible", "Local JSONL files only"],
      ["Document title", "Filename without extension", "Local JSONL files only"],
      ["Revit version string", "\"Autodesk Revit 2027\"", "Local JSONL files only"],
    ],
    [2500, 2500, 4070],
    CONTENT_W
  ),
  ...spacer(1),

  h2("10.2 What Is NOT Collected"),
  simpleTable(
    ["Not Collected", "Reason"],
    [
      ["Element geometry (coordinates, dimensions)", "Not needed for routine detection. Would require serialising Revit geometry objects, significantly increasing log file size."],
      ["Document file path", "Replaced by SHA-1 hash (first 12 hex chars). The full path is never written to any file."],
      ["User identity", "No user account, machine ID, or login name is captured."],
      ["Model contents not touched by the user", "Only elements appearing in DocumentChangedEventArgs.GetAddedElementIds() and GetModifiedElementIds() are processed."],
      ["View navigation (pan, zoom, rotate)", "These do not trigger DocumentChanged at the API subscription level."],
      ["Selection changes", "Selection is not a document modification."],
      ["Undo / Redo operations", "No special handling. Undoing a placement causes a delete event; the element is removed from the ElementSnapshot cache."],
      ["Position and rotation changes", "ModifiedElementIds includes moved elements, but ElementSnapshot diffs only user-accessible Parameter objects. XYZ position is not a Parameter."],
    ],
    [3000, 6070],
    CONTENT_W
  ),
  ...spacer(1),

  h2("10.3 Claude API Data Exposure"),
  para("The only data sent to an external service is in the Claude API calls made by the Pattern Agent and Macro Agent. These calls contain:"),
  bullet("Family names and type names of the elements in the routine examples"),
  bullet("Parameter names and values (both constant and variable) observed across k examples"),
  bullet("Action type strings (\"Place\", \"SetParam\", \"Tag\")"),
  bullet("Level and view type context strings"),
  ...spacer(1),
  para("No document geometry, file paths, user identities, or project structure data are included in these calls. The API calls are made over HTTPS to Anthropic's servers and are subject to Anthropic's privacy policy. For organisations with strict data residency requirements, the Pattern Agent and Macro Agent models can be replaced with locally-hosted LLMs compatible with the Anthropic SDK interface."),

  h2("10.4 Autodesk Assistant Data Exposure"),
  para("When the Python MCP server is registered with the Autodesk Assistant, the user's natural language chat messages are processed by Autodesk's cloud AI. The MCP tool call responses from our server (candidate routine labels, shortcut names) are also visible to Autodesk's infrastructure. The actual JSONL log file contents are never transmitted — only the structured JSON responses from our MCP tools."),
);

// ═══════════════════════════════════════════════════════════════════════════════
// BUILD & SAVE
// ═══════════════════════════════════════════════════════════════════════════════
const doc = new Document({
  numbering: {
    config: [
      {
        reference: "bullets",
        levels: [{
          level: 0, format: LevelFormat.BULLET, text: "•",
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } },
        }],
      },
      {
        reference: "numbers",
        levels: [{
          level: 0, format: LevelFormat.DECIMAL, text: "%1.",
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } },
        }],
      },
    ],
  },
  styles: {
    default: {
      document: { run: { font: "Arial", size: 22 } },
    },
    paragraphStyles: [
      {
        id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 32, bold: true, font: "Arial", color: CLR.revitBlue },
        paragraph: { spacing: { before: 480, after: 240 }, outlineLevel: 0,
          border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: CLR.revitBlue, space: 4 } } },
      },
      {
        id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 26, bold: true, font: "Arial", color: CLR.revitBlue },
        paragraph: { spacing: { before: 320, after: 160 }, outlineLevel: 1 },
      },
      {
        id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 24, bold: true, font: "Arial", color: CLR.mutedGray },
        paragraph: { spacing: { before: 240, after: 120 }, outlineLevel: 2 },
      },
    ],
  },
  sections: [{
    properties: {
      page: {
        size: { width: PAGE_W, height: PAGE_H },
        margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN },
      },
    },
    headers: {
      default: new Header({
        children: [new Paragraph({
          border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: CLR.revitBlue, space: 4 } },
          children: [
            new TextRun({ text: "System Architecture — Agent-Augmented BIM Log Mining", font: "Arial", size: 18, color: CLR.mutedGray }),
            new TextRun({ children: [new Tab(), new Tab()], font: "Arial", size: 18 }),
          ],
          tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
        })],
      }),
    },
    footers: {
      default: new Footer({
        children: [new Paragraph({
          border: { top: { style: BorderStyle.SINGLE, size: 4, color: CLR.revitBlue, space: 4 } },
          children: [
            new TextRun({ text: "MSc Thesis — Technical Documentation  |  Revit 2027 + Claude API", font: "Arial", size: 18, color: CLR.mutedGray }),
            new TextRun({ children: [new Tab(), new TextRun({ children: [PageNumber.CURRENT], font: "Arial", size: 18, color: CLR.mutedGray })], font: "Arial", size: 18 }),
          ],
          tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
        })],
      }),
    },
    children,
  }],
});

Packer.toBuffer(doc).then(buf => {
  const outPath = "C:\\Users\\DE1E7A\\revit-personalization\\docs\\System Architecture.docx";
  fs.writeFileSync(outPath, buf);
  console.log("Created: " + outPath);
}).catch(err => {
  console.error("ERROR:", err.message);
  process.exit(1);
});
