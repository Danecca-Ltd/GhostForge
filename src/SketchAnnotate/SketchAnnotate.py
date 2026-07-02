import adsk.core
import adsk.fusion
import adsk.drawing
import os
import math

app = None
ui  = None
handlers = []
_pending = {}   # cross-handler value store (avoids SWIG proxy attribute loss)

CMD_ID       = 'FusionGadgets_SketchAnnotate'
CMD_NAME     = 'Sketch Annotate'
WORKSPACE_ID = 'FusionDocumentationEnvironment'
TAB_ID       = 'FusionDocTab'

INTERNAL_TO_MM = 10.0

TYPE_MAP = {
    'SketchLinearDimension':             'Lin',
    'SketchAngularDimension':            'Ang',
    'SketchRadialDimension':             'Rad',
    'SketchDiameterDimension':           'Dia',
    'SketchOffsetDimension':             'Off',
    'SketchEllipseMajorRadiusDimension': 'Elp',
    'SketchEllipseMinorRadiusDimension': 'Elp',
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def collect_all_params(design):
    """Return ordered dict: group_name -> [(param_name, value_mm, label)].

    Groups: one per sketch, then 'Extrude', then 'Fillet'.
    Extrude depths come from DistanceExtentDefinition.distance (reliable).
    Fillet radii come from FilletFeature.parameters (API v2+; skipped if absent).
    """
    result = {}

    for comp in design.allComponents:

        # --- Sketch dimensions ---
        for sketch in comp.sketches:
            rows = []
            for dim in sketch.sketchDimensions:
                try:
                    param = dim.parameter
                    if param is None:
                        continue
                    raw   = dim.classType().split('::')[-1]
                    label = TYPE_MAP.get(raw, 'Dim')
                    rows.append((param.name, param.value * INTERNAL_TO_MM, label))
                except Exception:
                    continue
            if rows:
                result.setdefault(sketch.name, []).extend(rows)

        # --- Extrude depths ---
        extrude_rows = []
        for i in range(comp.features.extrudeFeatures.count):
            try:
                ef = comp.features.extrudeFeatures.item(i)
                for side in ('extentOne', 'extentTwo'):
                    try:
                        ext = getattr(ef, side)
                        dist_def = adsk.fusion.DistanceExtentDefinition.cast(ext)
                        if dist_def:
                            p = dist_def.distance
                            if p:
                                extrude_rows.append(
                                    (p.name, p.value * INTERNAL_TO_MM, ef.name))
                    except Exception:
                        pass
            except Exception:
                continue
        if extrude_rows:
            result['Extrude'] = extrude_rows

    # --- Remaining parameters (fillet radii, chamfers, etc.) ---
    # FilletFeature doesn't expose .parameters in all Fusion API versions.
    # Fallback: allParameters minus what sketch/extrude already captured.
    known = {name for rows in result.values() for name, _, _ in rows}
    other_rows = []
    try:
        all_params = design.allParameters
        for i in range(all_params.count):
            p = all_params.item(i)
            if p.name not in known:
                unit = (p.unit or '').strip()
                if unit in ('', 'ul', 'unitless'):
                    continue                        # dimensionless (TangencyWeight etc.) — skip
                if unit == 'deg':
                    display_val = math.degrees(p.value)
                    label = 'deg'
                else:
                    display_val = p.value * INTERNAL_TO_MM   # cm → mm
                    label = 'Feature'
                other_rows.append((p.name, display_val, label))
    except Exception:
        pass
    if other_rows:
        result['Other Features'] = other_rows

    return result


def build_scr(sketch_groups, x, y_start, height, spacing, scr_path):
    """Write .scr using AutoLISP entmake to create TEXT entities directly.

    One (entmake ...) call per annotation — no interactive command, no exit sequence.
    DXF group codes for TEXT: 0=entity type, 1=string, 10=insertion point,
    40=height, 50=rotation (radians, 0.0 = horizontal).
    """
    lines = []
    y = float(y_start)
    total = 0

    def add_text(content):
        nonlocal y
        safe = content.replace('\\', '\\\\').replace('"', "'")
        lines.append(
            f'(entmake (list (cons 0 "TEXT")'
            f' (cons 1 "{safe}")'
            f' (cons 10 (list {float(x):.4f} {y:.4f} 0.0))'
            f' (cons 40 {float(height):.4f})'
            f' (cons 50 0.0)))'
        )
        y -= float(spacing)

    for sketch_name, dims in sketch_groups.items():
        add_text(f'== {sketch_name} ==')
        y -= float(spacing) * 0.4

        for param_name, val, label in dims:
            if label == 'deg':
                add_text(f'{param_name} = {val:.2f} deg')
            else:
                add_text(f'{param_name} = {val:.3f} mm  {label}')
            total += 1

        y -= float(spacing) * 0.6

    with open(scr_path, 'w', encoding='ascii', errors='replace') as f:
        f.write('\n'.join(lines) + '\n')

    return total


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd    = args.command
            inputs = cmd.commandInputs

            inputs.addTextBoxCommandInput(
                'info', '',
                'Places sketch parameter annotations on the active drawing sheet '
                'as persistent text using the embedded AutoCAD engine.<br><br>'
                'Set the top-left anchor point and text height (all values in mm).',
                4, True)

            # Integer spinners — value IS the mm coordinate, no unit conversion
            inputs.addIntegerSpinnerCommandInput('x_pos',      'Start X (mm)',         0, 10000, 1,  10)
            inputs.addIntegerSpinnerCommandInput('y_pos',      'Start Y (mm)',         0, 10000, 1, 250)
            inputs.addIntegerSpinnerCommandInput('txt_height', 'Text height (mm)',     1,    50, 1,   4)

            h_ex  = ExecuteHandler()
            h_des = DestroyHandler()
            cmd.execute.add(h_ex)
            cmd.destroy.add(h_des)
            handlers.extend([h_ex, h_des])

        except Exception as e:
            if ui:
                ui.messageBox(f'SketchAnnotate dialog error:\n{e}')


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        # Capture spinner values into module-level dict.
        # Cannot stash on args.command — SWIG proxy attributes are lost between handlers.
        # Do NOT call executeTextCommand here — reentrancy crashes Fusion.
        inputs = args.command.commandInputs
        _pending['x']      = inputs.itemById('x_pos').value
        _pending['y']      = inputs.itemById('y_pos').value
        _pending['height'] = inputs.itemById('txt_height').value
        _pending['ok']     = True


class DestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        if not _pending.pop('ok', False):
            return  # user cancelled

        try:
            x      = _pending['x']
            y      = _pending['y']
            height = _pending['height']
            spacing = height * 2.0   # 2× height = comfortable line spacing

            design = find_open_design()
            if design is None:
                ui.messageBox(
                    'SketchAnnotate: no open Design found.\n'
                    'Open the referenced design document first, then try again.')
                return

            sketch_groups = collect_all_params(design)
            if not sketch_groups:
                ui.messageBox(
                    'SketchAnnotate: no sketch dimensions found in the design.\n'
                    'Add driven dimensions to your sketches first.')
                return

            scr_path = os.path.join(
                os.path.expanduser('~'), 'AppData', 'Local', 'Temp',
                'ghostforge_annotate.scr')

            total = build_scr(sketch_groups, x, y, height, spacing, scr_path)
            safe_path = scr_path.replace('\\', '/')

            app.executeTextCommand(
                f'FusionDoc.ExecuteAcadCommand _.SCRIPT "{safe_path}"')

            s_count = len(sketch_groups)
            ui.messageBox(
                f'SketchAnnotate complete.\n\n'
                f'Placed {total} annotation{"s" if total != 1 else ""} '
                f'from {s_count} sketch{"es" if s_count != 1 else ""}.\n\n'
                f'Starting position: ({x}, {y}) mm\n'
                f'Script: {scr_path}')

        except Exception as e:
            if ui:
                ui.messageBox(f'SketchAnnotate error:\n{e}')


# ---------------------------------------------------------------------------
# Add-in lifecycle
# ---------------------------------------------------------------------------

def run(context):
    global app, ui
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface

        ws  = ui.workspaces.itemById(WORKSPACE_ID)
        tab = ws.toolbarTabs.itemById(TAB_ID)

        # Remove stale controls from any previous load
        for i in range(tab.toolbarPanels.count):
            ctrl = tab.toolbarPanels.item(i).controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        old = ui.commandDefinitions.itemById(CMD_ID)
        if old:
            old.deleteMe()

        cmd_def = ui.commandDefinitions.addButtonDefinition(
            CMD_ID, CMD_NAME,
            'Place sketch parameter annotations on the active drawing sheet')

        h = CreatedHandler()
        cmd_def.commandCreated.add(h)
        handlers.append(h)

        # Prefer Dimensions panel; fall back to Inspect panel
        panel = (tab.toolbarPanels.itemById('DimensionsPanel') or
                 tab.toolbarPanels.itemById('InspectPanel'))
        if panel is None:
            panel = tab.toolbarPanels.add(
                'FusionGadgets_DrawingTools', 'Fusion Gadgets', '', False)

        ctrl = panel.controls.addCommand(cmd_def)
        ctrl.isPromoted = True

    except Exception as e:
        if ui:
            ui.messageBox(f'SketchAnnotate run() error:\n{e}')


def stop(context):
    global ui, handlers
    if ui:
        old = ui.commandDefinitions.itemById(CMD_ID)
        if old:
            old.deleteMe()
        try:
            ws  = ui.workspaces.itemById(WORKSPACE_ID)
            tab = ws.toolbarTabs.itemById(TAB_ID)
            for i in range(tab.toolbarPanels.count):
                ctrl = tab.toolbarPanels.item(i).controls.itemById(CMD_ID)
                if ctrl:
                    ctrl.deleteMe()
        except Exception:
            pass
    handlers.clear()
