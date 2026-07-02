import adsk.core
import adsk.fusion
import adsk.drawing

INTERNAL_TO_MM = 10.0
HEADERS = ['Sketch', 'Parameter', 'Nominal (mm)', 'Type']

TYPE_MAP = {
    'SketchLinearDimension':             'Linear',
    'SketchAngularDimension':            'Angular',
    'SketchRadialDimension':             'Radial',
    'SketchDiameterDimension':           'Diameter',
    'SketchOffsetDimension':             'Offset',
    'SketchEllipseMajorRadiusDimension': 'Ellipse Major R',
    'SketchEllipseMinorRadiusDimension': 'Ellipse Minor R',
}


def collect_dimensions(design):
    rows = []
    for comp in design.allComponents:
        for sketch in comp.sketches:
            for dim in sketch.sketchDimensions:
                try:
                    param = dim.parameter
                    if param is None:
                        continue
                    raw_type  = dim.classType().split('::')[-1]
                    label     = TYPE_MAP.get(raw_type, raw_type.replace('Sketch', '').replace('Dimension', ''))
                    value_mm  = param.value * INTERNAL_TO_MM
                    rows.append((sketch.name, param.name, f'{value_mm:.3f}', label))
                except Exception:
                    continue
    return rows


def find_open_design(app):
    for i in range(app.documents.count):
        doc = app.documents.item(i)
        if doc.documentType == adsk.core.DocumentTypes.FusionDesignDocumentType:
            return adsk.fusion.Design.cast(
                doc.products.itemByProductType('DesignProductType'))
    return None


def fill_table(table, rows):
    for c, h in enumerate(HEADERS):
        table.updateCellData(0, c, h)
    for r, (sketch, param, nominal, dim_type) in enumerate(rows, start=1):
        table.updateCellData(r, 0, sketch)
        table.updateCellData(r, 1, param)
        table.updateCellData(r, 2, nominal)
        table.updateCellData(r, 3, dim_type)


def run(context):
    app = adsk.core.Application.get()
    ui  = app.userInterface

    try:
        draw_doc = adsk.drawing.DrawingDocument.cast(app.activeDocument)
        if not draw_doc:
            ui.messageBox('Run this script from an open Drawing document.')
            return

        design = find_open_design(app)
        if not design:
            ui.messageBox('No open Design found.\nOpen the referenced design, then run again.')
            return

        rows  = collect_dimensions(design)
        if not rows:
            ui.messageBox('No sketch dimensions found in the design.')
            return

        sheet = draw_doc.drawing.activeSheet
        ct    = sheet.customTables

        # ct.count may be unreliable — try item(0) directly as well
        existing_table = None
        if ct.count > 0:
            existing_table = ct.item(ct.count - 1)
        else:
            try:
                existing_table = ct.item(0)
            except Exception:
                existing_table = None

        if existing_table is not None:
            fill_table(existing_table, rows)
            ui.messageBox(f'Table filled — {len(rows)} dimension{"s" if len(rows) != 1 else ""}.')
        else:
            # No table yet — create empty one sized for our data
            inp             = ct.createInput()
            inp.rowCount    = len(rows) + 1   # +1 for header
            inp.columnCount = len(HEADERS)
            ct.add(inp)
            ui.messageBox(
                f'Place the table on the sheet, then run this script again to populate it '
                f'({len(rows)} dimension{"s" if len(rows) != 1 else ""} ready).')

    except Exception as e:
        if ui:
            ui.messageBox(f'SketchDimTable error:\n{e}')
