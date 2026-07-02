# GhostForge — Research Findings

All findings from the reverse-engineering session (Danecca, June–July 2026).

---

## 1. The `adsk.drawing` API Wall

### What Fusion exposes

Running `dir()` on every object in the drawing tree:

```
DrawingDocument
  .drawing → Drawing
    .activeSheet → Sheet       ← WORKS
    .sheets      → ERROR: "API Function not yet implemented"

Sheet
  .customTables → CustomTables  ← WORKS (the only useful collection)
  .isValid
  .objectType
  .this / .thisown (SWIG internals)
  .drawingViews  → AttributeError: no attribute
  .dimensions    → AttributeError: no attribute
  .annotations   → AttributeError: no attribute
  .centerMarks   → AttributeError: no attribute
  .centerLines   → AttributeError: no attribute
  (all other annotation/view collections) → AttributeError
```

### The before/after test

A dimension placed manually by the user is **completely invisible** to the Python API. The Sheet object is byte-for-byte identical before and after placement (only the SWIG pointer address changes, which is normal object identity, not new state).

This confirms Autodesk has not bound drawing annotation objects to the Python layer. The C++ objects exist (Fusion renders them) but the Python wrappers are not written.

### customTables — the one working collection

`sheet.customTables` is fully functional:
- `ct.createInput()` → returns input object with `rowCount`, `columnCount`
- `ct.add(inp)` → creates and places the table (user places it with mouse)
- `table.updateCellData(rowIndex, columnIndex, data)` → fills cells
- `table.position = adsk.core.Point2D.create(x, y)` → sets position

**Important:** `updateCellData` must be called on a committed (placed) table. Calling it on the pre-placement object discards data. Use a two-pass approach: first run creates the table, second run fills it.

**Note on Autodesk typo:** The parameter is spelled `coulmnIndex` in one API version. Use positional passing.

---

## 2. The Command System

### `commandStarting` works in Drawing workspace

`ui.commandStarting` fires for all commands including drawing workspace commands. The event args expose `args.commandId` (string). This was the mechanism used to sniff command IDs.

```python
class StartingHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        print(args.commandId)  # works

ui.commandStarting.add(StartingHandler())
```

### `commandCreated` does NOT work on built-in drawing commands

`commandDefinition.commandCreated.add(handler)` does not fire when Fusion's built-in drawing commands (views, dimensions, etc.) are invoked. Autodesk does not expose this hook for built-in commands. This is confirmed by experiment — zero calls despite multiple manual invocations of `FusionDrawingSingleDimensionCmd`.

### Captured drawing command IDs

From CommandRecorder session (2026-06-30):

| Command ID | Function |
|---|---|
| `FusionDrawingViewBaseCommand` | Create base (front) view |
| `FusionDrawingViewProjectCommand` | Create projected view |
| `FusionDrawingSingleDimensionCmd` | Place dimension (context-aware: linear/radial/angular) |
| `FusionDrawingCenterlineCommand` | Add centreline |
| `FusionDrawingCentermarkCommand` | Add centre mark |
| `CommitCommand` | Confirm / place current operation |
| `SelectCommand` | Selection tool (active between commands) |

Termination reason codes: `1` = success, `2` = cancelled (Escape), `4` = preempted by next command.

---

## 3. The AutoCAD Back Channel

### Fusion's drawing engine is AutoCAD

Fusion ships AutoCAD's DWG engine embedded. Evidence:
- Drawing files are DWG format internally
- Fusion exports `.dwg` natively
- The text command `FusionDoc.ExecuteAcadCommand` routes to the AutoCAD engine
- Geometry drawn via this route persists after save/close/reopen

### `FusionDoc.ExecuteAcadCommand`

```python
app.executeTextCommand('FusionDoc.ExecuteAcadCommand _.COMMANDNAME args')
```

The `_` prefix is AutoCAD's locale-bypass convention (runs the English command name regardless of Fusion's display language).

**Critical constraint:** Must be called from `command_destroy`, NOT from `command_created` or `command_execute`. Calling it during the command lifecycle causes reentrancy into the command engine and crashes Fusion. This was documented independently by [schneik80/PowerTools-Document-Tools](https://github.com/schneik80/PowerTools-Document-Tools).

### Working AutoCAD commands (confirmed)

```python
# Line between two points
app.executeTextCommand('FusionDoc.ExecuteAcadCommand _.LINE 50,50 150,50 ')

# Circle at centre with radius
app.executeTextCommand('FusionDoc.ExecuteAcadCommand _.CIRCLE 150,100 30')

# Linear dimension via AcadParameters
app.executeTextCommand('FusionDoc.ExecuteAcadCommand _.DIMLINEAR')
app.executeTextCommand('FusionDoc.AcadParameters 50,100')   # pt1
app.executeTextCommand('FusionDoc.AcadParameters 150,100')  # pt2
app.executeTextCommand('FusionDoc.AcadParameters 100,120')  # dim line pos
```

### `FusionDoc.AcadParameters`

Sends input to the currently running AutoCAD command. Works for:
- Point coordinates: `50,100`
- Numeric values: `10`, `0`
- AutoCAD option keywords: `_E`, `_ALL`

Does **not** work for:
- Free-form text content (TEXT command body)
- Empty string as Enter (`AcadParameters ` → "Not enough parameters")

### Text content via `.scr` script files

AutoCAD `.scr` script files treat each line as one Enter keypress. This is the mechanism for passing text content non-interactively:

```python
scr_content = """_.TEXT
50,50
10
0
d1 = 25.400 mm

_.DIMLINEAR
50,100
150,100
100,120
"""

scr_path = 'C:/Users/user/Desktop/fusion_annot.scr'
with open(scr_path, 'w', encoding='ascii') as f:
    f.write(scr_content)

app.executeTextCommand(f'FusionDoc.ExecuteAcadCommand _.SCRIPT "{scr_path}"')
```

**Confirmed working:** Text label `d1 = 25.400 mm` appeared on drawing sheet and persisted after save/reopen.

**TEXT command termination:** Each TEXT invocation requires a blank line to end it (TEXT repeats by default asking for next position). Use double blank line to be safe.

### `FusionDoc.IpeInput`

Listed in the text command surface as "Send input within IPE". Expects exactly 2 parameters. **Do not call it when no IPE (In-Place Editor) is active** — it crashes the Python execution thread at the C++ level. The crash is not catchable by Python `try/except Exception`.

---

## 4. Sheet Coordinate System

- **Unit:** 1 coordinate unit = 1 mm
- **Confirmed by:** DIMLINEAR placed between (50,200) and (150,200) showed value `100`
- **Origin:** Sheet corner (bottom-left assumed, consistent with AutoCAD default)
- **Y axis:** Increasing upward (AutoCAD convention)
- **Scale:** Paper-space coordinates. A drawing view at 1:2 scale means model geometry appears at half the model dimensions in sheet coordinates.

---

## 5. Drawing Workspace Panels

From the Drawing workspace (`FusionDocumentationEnvironment`, tab `FusionDocTab`):

| Index | Panel ID | Panel Name |
|---|---|---|
| 0 | `ViewsPanel` | Create |
| 1 | `ConstraintsPanel` | Constraints |
| 2 | `ModifyPanel` | Modify |
| 3 | `GeometryPanel` | Geometry |
| 4 | `DimensionsPanel` | Dimensions |
| 5 | `TextPanel` | Text |
| 6 | `SymbolsPanel` | Symbols |
| 7 | `InsertPanel` | Insert |
| 8 | `BillOfMaterialsPanel` | Tables |
| 9 | `AutomationPanel` | Automation |
| 10 | `BlockPanel` | Finish Title Block |
| 11 | `OutputPanel` | Export |
| 12 | `InspectPanel` | Inspect |
| 13 | `StopSketchEditPanel` | Finish Sketch |

Add buttons to `InspectPanel` or create a new panel on `FusionDocTab`.

---

## 6. FusionDoc Text Command Surface (Partial)

Source: [kantoku-code/Fusion360_Small_Tools_for_Developers](https://github.com/kantoku-code/Fusion360_Small_Tools_for_Developers/blob/master/TextCommands/TextCommands_txt_Ver2_0_8176.txt)

Regenerate the full current list inside Fusion:
```
TextCommands.List /Hidden
```
Or via Python API:
```python
import neu_dev; neu_dev.list_functions()
```

### Drawing-relevant commands

```
FusionDoc.ExecuteAcadCommand          Execute AutoCAD Command
FusionDoc.AcadParameters              Send parameters for AutoCAD Command
FusionDoc.IntermediateAcadParameters  Send parameters for AutoCAD Command
FusionDoc.SelectObject                Select object in AutoCAD Command
FusionDoc.SelectObjects               Select objects in AutoCAD Command
FusionDoc.SetCursorPos                Set cursor position
FusionDoc.IpeInput                    Send input within IPE (2 params; dangerous if no IPE active)
FusionDoc.InvokeDrawingCmdById        Execute Fusion Doc drawing command by id
FusionDoc.DrawingViewBaseCmd          Create a drawing base view
FusionDoc.DrawingViewProjectCmdDef    Create a drawing project view
FusionDoc.DrawingViewSectionCmdDef    Create a Section drawing view
FusionDoc.DrawingViewDetailCmdDef     Create a Detail drawing view
FusionDoc.FusionDocLinearDimensionCmdDef   Linear Dimension
FusionDoc.FusionDocRadialDimensionCmdDef   Radial Dimension
FusionDoc.FusionDocAngularDimensionCmdDef  Angular Dimension
FusionDoc.FusionDocDiameterDimensionCmdDef Diameter Dimension
FusionDoc.FusionDocAlignedDimensionCmdDef  Aligned Dimension
FusionDoc.FusionDocBaselineDimensionCmdDef Baseline Dimension
FusionDoc.FusionDocChainDimensionCmdDef    Chain Dimension
FusionDoc.FusionDocOrdinateDimensionCmdDef Ordinate Dimension
FusionDoc.FusionDocMTextCmdDef             MText
FusionDoc.FusionDocLeaderCmdDef            Leader
FusionDoc.FusionDocBalloonCmdDef           Balloon
FusionDoc.DrawingSheetNewCmd          Create a drawing sheet
FusionDoc.DrawingSheetDeleteCmd       Delete a drawing sheet
FusionDoc.DrawingSheetActivateCmd     Activate a drawing sheet
FusionDoc.DrawingSheetRenameCmd       Rename a drawing sheet
FusionDoc.InsertTitleBlockCmd         Insert a title block
FusionDoc.ExportPDFCmd                Output PDF
FusionDoc.ExportPDFSheetCmd           Output current sheet to PDF
FusionDoc.NewDrawingDocumentCmd       Create a drawing document
FusionDoc.CreateTemplate              CreateDrawingTemplate
```

---

## 7. Unexplored Leads

- **`FusionDoc.InvokeDrawingCmdById`** — "Execute Fusion Doc drawing command by id". Unknown parameter format. Could be the programmatic gateway to placing Fusion-native annotations (with associativity to model geometry) rather than raw AutoCAD geometry.
- **`FusionDoc.SetCursorPos`** + **`FusionDoc.SelectObject`** — Could enable selecting drawing view entities before firing a dimension command, producing associative (model-linked) dimensions rather than paper-space dimensions.
- **April 2026 PMI API additions** — Autodesk added 40+ typed PMI (Product and Manufacturing Information) objects to `adsk.fusion` (Design workspace). These are 3D annotations on the model, not drawing-sheet objects. Unexplored as of this writing. May be a cleaner path to MBD-style annotation.
- **`DIMLINEAR` text override** — AutoCAD's DIMLINEAR accepts a `T` option to override the displayed value. Could be used to place a dimension that shows a sketch parameter value even when the paper-space geometry doesn't match the model dimension exactly.
- **Script file with full drawing** — Since `.scr` can drive any AutoCAD command, a complete drawing annotation run (multiple views' worth of sketch parameters) could be driven by a single generated script file.
