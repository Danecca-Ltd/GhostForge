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

**TEXT command termination — BROKEN:** Fusion's embedded AutoCAD runs `_.TEXT` in DTEXT (dynamic text) mode. Blank lines do **not** exit the command — they are treated as empty continuation lines. Every subsequent line in the script is placed as literal text on the sheet. `_.TEXT` cannot be used for multi-entity text placement via `.scr` files.

### AutoLISP `entmake` — confirmed working

Fusion's embedded AutoCAD engine includes AutoLISP. A `.scr` file can contain AutoLISP expressions directly (one per line), and they execute immediately with no interactive exit sequence required.

```python
# Each line in the .scr file creates one TEXT entity:
'(entmake (list (cons 0 "TEXT")'
' (cons 1 "annotation text")'
' (cons 10 (list 10.0 250.0 0.0))'   # insertion point x,y,z in mm
' (cons 40 4.0)'                       # text height in mm
' (cons 50 0.0)))'                     # rotation in radians
```

This is the **preferred mechanism for placing text**. DXF group codes for TEXT: `0` = entity type, `1` = string, `10` = insertion point, `40` = height, `50` = rotation (radians).

No exit sequence needed. Each `(entmake ...)` call is self-contained and returns immediately. The script can contain arbitrarily many of them.

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

## 7. Python API Gotchas

### `app.documents.item()` returns typed subclass, not `Document`

`app.documents.item(i)` returns `adsk.fusion.FusionDocument` (for design documents) or `adsk.core.DrawingDocument` (for drawings), not the base `adsk.core.Document`. The typed subclasses do not expose `.documentType`. To find an open design without triggering `AttributeError`, try-cast to a Design product directly:

```python
def find_open_design():
    for i in range(app.documents.count):
        try:
            doc    = app.documents.item(i)
            design = adsk.fusion.Design.cast(
                doc.products.itemByProductType('DesignProductType'))
            if design:
                return design
        except Exception:
            continue
    return None
```

### SWIG proxy attribute loss between event handlers

Stashing a value on `args.command` in `ExecuteHandler` (e.g. `args.command._my_flag = True`) is **not visible** in `DestroyHandler`. Each handler receives a different Python SWIG proxy wrapping the same underlying C++ command object. Python attributes set on one proxy do not propagate to another. Use a **module-level dict** instead:

```python
_pending = {}

class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        _pending['x'] = args.command.commandInputs.itemById('x').value
        _pending['ok'] = True

class DestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        if not _pending.pop('ok', False):
            return   # user cancelled
        x = _pending['x']
        ...
```

### Feature parameter access

| Feature | What works | What doesn't |
|---|---|---|
| `ExtrudeFeature` | `DistanceExtentDefinition.cast(ef.extentOne).distance` → `ModelParameter` | — |
| `FilletFeature` | `design.allParameters` minus already-known names | `ff.parameters` not bound in current API |
| All features | `design.allParameters` (all model params including sketch dims) | No `.parentFeature` on `ModelParameter` |

### Unit-aware parameter handling

`ModelParameter.value` is always in Fusion internal units. `ModelParameter.unit` tells you the display type:

| `.unit` value | Internal unit | Display conversion |
|---|---|---|
| `'mm'`, `'cm'`, `'in'` etc. | cm | multiply by 10 → mm |
| `'deg'` | radians | `math.degrees(value)` |
| `''` or `'ul'` | dimensionless | skip — internal weights, not engineering values |

Never blindly multiply all parameters by 10. Dimensionless parameters (e.g. `TangencyWeight = 1`) become `10 mm` otherwise.

---

## 8. AutoLISP Subset — Confirmed Constraints (Session 3, 2026-07-02)

Fusion's embedded AutoLISP engine is a restricted subset. The following are confirmed absent:

| Function | Symptom | Workaround |
|---|---|---|
| `foreach` | Silent crash / no execution | Replace with `while` / `cdr` loop |
| `stringp` | Undefined function | Remove predicate; use `if val` or `if (= ...)` |
| `listp` | Undefined function | Remove predicate; always treat pair as alist |
| `equal` | Silent crash on string comparison | Use `=` for all comparisons (works for strings too) |
| `entmake LAYER` | Returns `nil` | Use `command "_.-LAYER" "N" name "C" color name ""` |
| `entmake DIMENSION` | Returns `nil` | Use bare `command "_.DIMLINEAR"` |
| `(getvar "CTAB")` | Returns boolean `T` | Extract layout from DRAWINGVIEW entity group 410 |
| `(getvar "DIMSTYLE")` | Returns boolean `T` | Find via `(tblsearch "DIMSTYLE" name)` |

**Special-form wrapping:** `command` is a special form and **cannot** be wrapped in `vl-catch-all-apply`. Calling `(vl-catch-all-apply (quote command) ...)` causes the script to hang silently. Use bare `(command ...)` calls. Do not confuse `_.LAYER` (opens a dialog, hangs) with `_.-LAYER` (dash prefix = non-interactive, scriptable).

**Confirmed available:** `vl-catch-all-apply`, `vl-catch-all-error-p`, `vl-princ-to-string`, `open`/`close`/`write-line`, `strcat`/`rtos`/`itoa`, `setq`/`if`/`while`/`and`/`not`/`or`/`cond`, `entnext`/`entget`/`entdel`/`entmake` (TEXT), `assoc`/`car`/`cdr`/`cadr`/`list`/`append`/`nth`/`length`, `tblsearch`, `getvar`/`setvar` (most), `=`/`<`/`>`, `abs`/`max`, `princ`, `command`, `defun`.

---

## 9. DRAWINGVIEW Entity Structure (Session 3, 2026-07-02)

Drawing views are stored as `DRAWINGVIEW` entities in the DWG database. They can be walked using `entnext`/`entget`.

**Group codes of interest:**

| Group | Occurrences | Content |
|---|---|---|
| `0` | 1 | `"DRAWINGVIEW"` |
| `5` | 1 | Entity handle (hex string) |
| `410` | 1 | Layout name (e.g. `"Sheet1"`) — use this instead of `getvar "CTAB"` |
| `40` | 4 | In order: `scale`, `0.0`, `cx`, `cy` (paper-space centre in mm) |
| `10` | 2 | In order: bbox lower-left, bbox upper-right (paper-space mm) |

**Extracting view geometry:**

```lisp
; Collect all group-40 and group-10 values via while/cdr (no foreach)
(setq g40s nil  g10s nil  pair (entget ent))
(while pair
  (cond
    ((= (car (car pair)) 40) (setq g40s (append g40s (list (cdr (car pair))))))
    ((= (car (car pair)) 10) (setq g10s (append g10s (list (cdr (car pair))))))
  )
  (setq pair (cdr pair))
)
; Then:
; scale  = (nth 0 g40s)
; cx, cy = (nth 2 g40s), (nth 3 g40s)  ← view centre in paper-space mm
; bbox_w = (abs (- (car  (nth 1 g10s)) (car  (nth 0 g10s))))
; bbox_h = (abs (- (cadr (nth 1 g10s)) (cadr (nth 0 g10s))))
```

**Identifying front vs side view:** The front (larger) view has a wider bbox_w than the side (narrower) view. Use `(>= (nth 3 view_a) (nth 3 view_b))` to sort.

**Confirmed coordinates (Cork part, 111×26×3 mm at 1:1 scale):**

| View | cx | cy | scale |
|---|---|---|---|
| Front | 142.035 | 180.276 | 1.0 |
| Side | 225.421 | 180.276 | 1.0 |

Part edge positions from view centre: `f_left = cx - v_wid*scale*0.5`, `f_top = cy + v_len*scale*0.5`, etc.

---

## 10. Layer Operations (Session 3, 2026-07-02)

### Creating a layer

`entmake LAYER` returns `nil` in Fusion's engine — not supported. Use the non-interactive dash form of the LAYER command:

```lisp
(command "_.-LAYER" "N" "GF_Dimensions" "C" "30" "GF_Dimensions" "")
```

- `"N"` = new layer name follows
- `"GF_Dimensions"` = layer name
- `"C"` = color assignment follows
- `"30"` = AutoCAD color index 30 (orange)
- `"GF_Dimensions"` = apply color to this layer
- `""` = exit

Check existence first with `(tblsearch "LAYER" "GF_Dimensions")`.

### Placing entities on a layer

```lisp
; entmake: (cons 8 layer_name)
; command: (setvar "CLAYER" layer_name) before, restore after
```

### Confirmed DIMSTYLE names

In Fusion drawings, `(tblsearch "DIMSTYLE" "FD_Dimensions_Style")` returns `nil`. `(tblsearch "DIMSTYLE" "Standard")` returns the entry. Use `"Standard"` as the fallback.

---

## 11. GhostForge Unified Add-In Architecture (Session 3, 2026-07-02)

`src/GhostForge/` replaces the separate `SketchAnnotate` and `DimAnnotate` add-ins with a single unified add-in. It creates a dedicated **GhostForge** tab at the top level of the Drawing workspace (same hierarchy as the built-in Drawing / Manage / Utilities tabs) via:

```python
ws  = ui.workspaces.itemById('FusionDocumentationEnvironment')
tab = ws.toolbarTabs.add('GhostForge_Tab', 'GhostForge')
```

Three separate panels (one per command) prevent the flyout dropdown that appears when multiple commands share one panel. Each command is promoted:

```python
ctrl.isPromoted         = True
ctrl.isPromotedByDefault = True
```

Button icons live in `resources/<name>/16x16.png`, `32x32.png`, `64x64.png` and are referenced by absolute path as the 4th argument to `addButtonDefinition`.

---

## 12. Unexplored Leads

- **`FusionDoc.InvokeDrawingCmdById FusionDrawingSingleDimensionCmd`** — Confirmed working: starts Fusion's native dimension command. However Fusion's C++ selection loop does not respond to `FusionDoc.AcadParameters` — the command waits for user mouse input and cannot be driven programmatically this way. Cursor position can be set via `FusionDoc.SetCursorPos` (confirmed working), but click simulation (Click, LeftClick, PickAt, SendClick) remains untested.
- **`FusionDoc.SelectObject`** — Returns "Set invalid object selector" for all handle formats tried (hex handles, space-separated coords, comma-separated coords). Format unknown.
- **April 2026 PMI API additions** — Autodesk added 40+ typed PMI objects to `adsk.fusion` (Design workspace). 3D model annotations, not drawing-sheet objects. Unexplored.
- **`DIMLINEAR T` text override** — AutoCAD's DIMLINEAR accepts a `T` option to override the displayed text. Could show a sketch parameter value independent of the paper-space measurement — useful if view scale or geometry placement is imprecise.
- **Complete drawing from a single `.scr` file** — All annotation for a multi-view drawing could be driven by one generated script, keeping Fusion's Python side trivially thin.
