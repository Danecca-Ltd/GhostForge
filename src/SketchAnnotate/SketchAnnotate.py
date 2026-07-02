import adsk.core
import adsk.fusion
import adsk.drawing
import os

app = None
ui  = None
handlers = []

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
        doc = app.documents.item(i)
        if doc.documentType == adsk.core.DocumentTypes.FusionDesignDocumentType:
            return adsk.fusion.Design.cast(
                doc.products.itemByProductType('DesignProductType'))
    return None


def collect_by_sketch(design):
    """Return dict: sketch_name -> [(param_name, value_mm, type_label)]."""
    result = {}
    for comp in design.allComponents:
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
                result[sketch.name] = rows
    return result


def build_scr(sketch_groups, x, y_start, height, spacing, scr_path):
    """Write AutoCAD .scr placing one TEXT entity per sketch dimension.

    Sheet coordinate Y decreases downward (Y-up system, so lines stack visually down).
    Each TEXT block:
        _.TEXT
        x,y
        height
        0           <- rotation
        content
                    <- blank line = Enter with no text = exit TEXT command
    """
    lines = []
    y = float(y_start)
    total = 0

    def add_text(content):
        nonlocal y
        lines.append('_.TEXT')
        lines.append(f'{float(x):.2f},{y:.2f}')
        lines.append(f'{float(height):.2f}')
        lines.append('0')
        lines.append(content)
        lines.append('')
        y -= float(spacing)

    for sketch_name, dims in sketch_groups.items():
        add_text(f'[[ {sketch_name} ]]')
        y -= float(spacing) * 0.4

        for param_name, value_mm, dim_type in dims:
            add_text(f'{param_name} = {value_mm:.3f} mm  ({dim_type})')
            total += 1

        y -= float(spacing) * 0.6

    # Trailing blanks to exit any command left active by the script
    lines += ['', '', '']

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
        # Capture spinner values here (integer = mm directly).
        # Do NOT call executeTextCommand here — reentrancy crashes Fusion.
        # Instead, stash values on the command object for DestroyHandler.
        cmd    = args.command
        inputs = cmd.commandInputs
        cmd._sa_x      = inputs.itemById('x_pos').value
        cmd._sa_y      = inputs.itemById('y_pos').value
        cmd._sa_height = inputs.itemById('txt_height').value
        cmd._sa_ok     = True


class DestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        cmd = args.command
        if not getattr(cmd, '_sa_ok', False):
            return  # user cancelled

        try:
            x      = cmd._sa_x       # mm = sheet coordinate units directly
            y      = cmd._sa_y
            height = cmd._sa_height
            spacing = height * 2.0   # 2× height = comfortable line spacing

            design = find_open_design()
            if design is None:
                ui.messageBox(
                    'SketchAnnotate: no open Design found.\n'
                    'Open the referenced design document first, then try again.')
                return

            sketch_groups = collect_by_sketch(design)
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
