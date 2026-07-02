# GhostForge

> Reverse-engineering Fusion 360's drawing engine to build a shadow drawing module.

GhostForge is a research project and add-in library for Autodesk Fusion 360. It documents the gap between what Fusion's published Python drawing API offers (almost nothing) and what the underlying AutoCAD drawing engine can actually do — then exploits that gap to place persistent drawing geometry, annotations, and dimensions programmatically.

The name: we are forging a ghost of Fusion's own drawing module, one that works through side channels the published API leaves open.

---

## The Problem

Fusion 360's Python drawing API (`adsk.drawing`) exposes four classes:

- `DrawingDocument`
- `Drawing`
- `DrawingExportManager`
- `PDFExportOptions`

The `Sheet` object — the thing you actually want to write to — exposes exactly one useful collection: `customTables`. Everything else (`drawingViews`, `dimensions`, `annotations`, `centerMarks`) returns `'Sheet' object has no attribute '...'`.

Dimensions placed by the user are invisible to Python. Before and after placing a dimension manually, the Sheet object is identical to the API. There is no Python hook into drawing annotations.

This is not a bug. Autodesk has deliberately not bound those C++ objects to the Python layer yet.

## The Discovery

Fusion's drawing workspace is built on AutoCAD's DWG engine. Fusion ships AutoCAD embedded — the drawing files are DWG format internally, which is why Fusion can export `.dwg` natively.

The text command system (`app.executeTextCommand()`) has a back channel into that embedded engine:

```python
app.executeTextCommand('FusionDoc.ExecuteAcadCommand _.LINE 50,50 150,50 ')
```

This places a real line on the drawing sheet that:
- Renders immediately
- Persists through save, close, and reopen
- Is indistinguishable from a line drawn manually

The same mechanism works for circles, dimensions, and — via an AutoCAD script file — text with arbitrary content.

## Key Findings

| Mechanism | Works | Notes |
|---|---|---|
| `FusionDoc.ExecuteAcadCommand _.LINE` | ✓ | Persistent geometry |
| `FusionDoc.ExecuteAcadCommand _.CIRCLE` | ✓ | Persistent geometry |
| `FusionDoc.AcadParameters` | ✓ | Coordinates and numeric values |
| `FusionDoc.ExecuteAcadCommand _.SCRIPT` | ✓ | Executes AutoCAD script files |
| AutoLISP `(entmake ...)` — TEXT | ✓ | **Best mechanism for text** — no exit sequence needed |
| AutoLISP `(entnext)` / `(entget)` | ✓ | Read any DWG entity including DRAWINGVIEW |
| AutoLISP `(command "_.-LAYER" ...)` | ✓ | Layer creation (dash prefix = non-interactive) |
| AutoLISP `(command "_.DIMLINEAR" ...)` | ✓ | Place linear dimensions non-interactively |
| AutoLISP `(tblsearch "LAYER"/"DIMSTYLE" ...)` | ✓ | Table lookups |
| Custom tab at Drawing workspace top level | ✓ | `ws.toolbarTabs.add()` — same hierarchy as Drawing/Manage/Utilities |
| `ExtrudeFeature` depth via `DistanceExtentDefinition` | ✓ | Full access to extrude parameters |
| `design.allParameters` | ✓ | All model parameters including sketch dims |
| `FusionDoc.SetCursorPos` | ✓ | Sets cursor position in paper-space mm |
| `FusionDoc.InvokeDrawingCmdById` | ✓ (partial) | Starts native Fusion commands; cannot drive selection loop |
| AutoLISP `(entmake ...)` — DIMENSION | ✗ | Returns nil; Fusion engine does not support this entity type |
| AutoLISP `(entmake ...)` — LAYER | ✗ | Returns nil; use `command "_.-LAYER"` instead |
| AutoLISP `foreach`, `stringp`, `listp`, `equal` | ✗ | Not in Fusion's AutoLISP subset |
| `(getvar "CTAB")` / `(getvar "DIMSTYLE")` | ✗ | Returns boolean T; use `tblsearch` and DRAWINGVIEW group 410 |
| `vl-catch-all-apply (quote command)` | ✗ | `command` is a special form; wrapping it hangs the script |
| `_.TEXT` in `.scr` for multi-entity placement | ✗ | DTEXT mode — blank lines don't exit, subsequent lines become text |
| `FusionDoc.SelectObject` | ✗ | "Set invalid object selector" for all handle formats tried |
| `FilletFeature.parameters` | ✗ | Not bound in current Fusion Python API |
| `adsk.drawing` Sheet annotations | ✗ | Not bound in Python API |
| `commandCreated` on built-in drawing commands | ✗ | Not exposed for built-in cmds |
| `FusionDoc.IpeInput` | ✗ | Crashes Python thread if no IPE active |

Sheet coordinate system: **1 unit = 1 mm**, origin at sheet corner, Y increasing upward.

## Credits and References

- [kantoku-code/Fusion360_Small_Tools_for_Developers](https://github.com/kantoku-code/Fusion360_Small_Tools_for_Developers) — TextCommands list, the original source that revealed `FusionDoc.ExecuteAcadCommand` and the full command surface
- [schneik80/PowerTools-Document-Tools](https://github.com/schneik80/PowerTools-Document-Tools) — First known real-world usage of `FusionDoc.ExecuteAcadCommand` in a production add-in (Open DWG command), including the critical `command_destroy` safety pattern
- Autodesk Community forum post by kantoku that revealed AutoCAD commands draw persistent geometry in Fusion drawings

## Repository Structure

```
probes/               Runtime probes used during research
  DrawingProbe/       Enumerates the full adsk.drawing object tree at runtime
  CommandRecorder/    Hooks commandStarting — logs all drawing command IDs
  DimCmdProbe/        Before/after snapshot of sheet when dimension command fires
  AcadTest/           Progressive tests of FusionDoc.ExecuteAcadCommand
  DwgEntityProbe/     Walks the DWG entity database — dumps entity types and group codes

src/
  SketchDimTable/     Add-in: places sketch dimensions as a custom table (two-pass, archived)
  SketchAnnotate/     Add-in: places sketch parameters as text annotations (archived)
  DimAnnotate/        Add-in: standalone DimAnnotate prototype (archived)
  GhostForge/         Unified add-in — Sketch Annotate + Dim Annotate + DWG Probe in one panel
```

## GhostForge (Unified Add-In)

`src/GhostForge/` is the current production add-in. It creates a dedicated **GhostForge** tab at the top level of the Drawing workspace (same level as the built-in Drawing / Manage / Utilities tabs) with three promoted commands:

**Sketch Annotate** — Reads every sketch dimension and feature parameter from the open design and places them as persistent text entities on the active drawing sheet. User specifies start position (X, Y in mm) and text height.

**Dim Annotate** — Reads part length/width/thickness from the open design, finds the front and side DRAWINGVIEW entities on the active sheet, computes edge positions from view geometry, and places three linear dimension annotations automatically (no user interaction after OK).

**DWG Probe** — Walks the full DWG entity database and dumps entity types, group codes, and handles to `Desktop/ghostforge_probe.txt`. Used for ongoing research.

### Dim Annotate architecture

```
Python (command_destroy):
  1. Read design parameters → v_len, v_wid, v_thk (mm)
  2. Generate AutoLISP script (_gf_dim function) with values embedded
  3. Write to %TEMP%/ghostforge_dims.scr
  4. app.executeTextCommand('FusionDoc.ExecuteAcadCommand _.SCRIPT "path"')

AutoLISP (_gf_dim):
  1. Create layer GF_Dimensions (orange) via command "_.-LAYER"
  2. Find DIMSTYLE via tblsearch
  3. Walk DWG entities with entnext/entget — find DRAWINGVIEW entities
  4. Extract view centres and scales from group 40 (×4) and group 10 (×2)
  5. Identify front view (wider bbox) and side view (narrower)
  6. Compute part edge positions in paper-space mm
  7. Attempt entmake DIMENSION (returns nil — engine limitation)
  8. Fallback: command "_.DIMLINEAR" with bare command calls (no vl-catch-all-apply)
  9. Write debug log to %TEMP%/gf_dim_debug.txt
```

**Note on AutoLISP constraints:** Fusion's embedded engine is a strict subset. See FINDINGS.md §8 for the full list of absent functions and workarounds.

### Output format (Sketch Annotate)

```
== Sketch1 ==
d1 = 111.000 mm  Lin
d2 = 26.000 mm  Lin

== Extrude ==
d3 = 3.000 mm  Extrude1

== Other Features ==
d4 = 3.000 mm  Feature
```

## Status

| Feature | Status |
|---|---|
| Custom GhostForge tab in Drawing workspace | Working |
| Sketch Annotate (text from design params) | Working |
| DWG Probe (entity database dump) | Working |
| Dim Annotate — view geometry reading | Working |
| Dim Annotate — layer creation | Working |
| Dim Annotate — dimension placement (DIMLINEAR) | In progress — bare `command` fix deployed v1.2.6 |
| Associative dimensions (linked to model geometry) | Blocked — Fusion selection loop not addressable |

Open research: cursor simulation (SetCursorPos + click commands), FusionDoc.SelectObject handle format, April 2026 PMI API additions.
