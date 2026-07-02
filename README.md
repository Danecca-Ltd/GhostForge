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
| `FusionDoc.ExecuteAcadCommand _.DIMLINEAR` | ✓ | Real dimension annotation |
| `FusionDoc.AcadParameters` | ✓ | Coordinates and numeric values |
| `FusionDoc.ExecuteAcadCommand _.SCRIPT` | ✓ | Script files for text content |
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
probes/             Runtime probes used during research
  DrawingProbe/     Enumerates the full adsk.drawing object tree at runtime
  CommandRecorder/  Hooks commandStarting — logs all drawing command IDs
  DimCmdProbe/      Before/after snapshot of sheet when dimension command fires
  AcadTest/         Progressive tests of FusionDoc.ExecuteAcadCommand

src/
  SketchDimTable/   Add-in: places sketch dimensions as a custom table (two-pass)
  SketchAnnotate/   Add-in: places sketch parameters as text annotations via .scr
```

## SketchAnnotate

The first production add-in built on the GhostForge mechanism. Reads every sketch dimension from the open design and places them as persistent text entities on the active drawing sheet using a generated AutoCAD `.scr` script.

**Usage:** Open both the Design and the Drawing. Switch to the Drawing workspace. Click **Sketch Annotate** (Dimensions panel). Set start position (X, Y in mm from sheet origin) and text height. Click OK.

**Output format:**
```
[[ Sketch1 ]]
d1 = 25.000 mm  (Lin)
d2 = 50.000 mm  (Lin)
r1 = 10.000 mm  (Rad)

[[ Sketch2 ]]
...
```

Text persists after save, close, and reopen — it is stored in the DWG layer of the drawing file.

## Status

SketchAnnotate is working. Known limitations:

- Text is unassociative — it does not update when the design changes
- No view-position awareness — annotations are placed at a user-specified fixed location rather than near the corresponding view geometry
- `adsk.drawing` still cannot read back what was placed (Autodesk has not bound annotation objects to Python)

Open research threads: `FusionDoc.InvokeDrawingCmdById`, `FusionDoc.SetCursorPos + SelectObject`, April 2026 PMI API additions in `adsk.fusion`.
