import adsk.core
import adsk.drawing
import os


def run(context):
    app = adsk.core.Application.get()
    ui = app.userInterface

    try:
        drawDoc = adsk.drawing.DrawingDocument.cast(app.activeDocument)
        if not drawDoc:
            ui.messageBox('Open a Drawing document first, then run this script.')
            return

        drawing = drawDoc.drawing
        lines = []

        # --- Top-level Drawing object ---
        lines.append('=== adsk.drawing.Drawing ===')
        lines.append(', '.join(sorted(dir(drawing))))

        # --- Try sheets collection ---
        try:
            sheets = drawing.sheets
            lines.append(f'\n=== drawing.sheets  count={sheets.count} ===')
        except Exception as e:
            lines.append(f'\ndrawing.sheets: {e}')

        # --- Try activeSheet ---
        sheet = None
        try:
            sheet = drawing.activeSheet
            lines.append(f'\n=== drawing.activeSheet: EXISTS ===')
            lines.append(', '.join(sorted(dir(sheet))))
        except Exception as e:
            lines.append(f'\ndrawing.activeSheet: {e}')

        if sheet:
            # --- Try views on sheet ---
            for attr in ('drawingViews', 'views', 'drawingView', 'baseViews'):
                try:
                    views = getattr(sheet, attr)
                    lines.append(f'\n=== sheet.{attr}  count={views.count} ===')
                    if views.count > 0:
                        view = views.item(0)
                        lines.append(f'--- View[0] dir ---')
                        lines.append(', '.join(sorted(dir(view))))

                        # --- Try dimension collections on view ---
                        for dim_attr in ('linearDimensions', 'angularDimensions',
                                         'radialDimensions', 'dimensions', 'annotations',
                                         'sketchDimensions', 'modelDimensions'):
                            try:
                                col = getattr(view, dim_attr)
                                lines.append(f'  view.{dim_attr}: EXISTS  count={col.count}')
                                lines.append(f'  dir: {", ".join(sorted(dir(col)))}')
                            except Exception as e:
                                lines.append(f'  view.{dim_attr}: {e}')
                except Exception as e:
                    lines.append(f'sheet.{attr}: {e}')

            # --- Probe customTables ---
            try:
                ct = sheet.customTables
                lines.append(f'\n=== sheet.customTables  count={ct.count} ===')
                lines.append(f'dir: {", ".join(sorted(dir(ct)))}')

                # createInput takes no args
                try:
                    inp = ct.createInput()
                    lines.append(f'\ncreateInput(): SUCCESS')
                    lines.append(f'input dir: {", ".join(sorted(dir(inp)))}')

                    # Probe all settable properties on the input
                    for prop in sorted(dir(inp)):
                        if prop.startswith('_') or prop in ('cast', 'classType', 'isValid', 'objectType', 'this', 'thisown'):
                            continue
                        try:
                            val = getattr(inp, prop)
                            lines.append(f'  inp.{prop} = {val}')
                        except Exception as e:
                            lines.append(f'  inp.{prop}: {e}')

                    # Create a real table
                    try:
                        inp.rowCount = 3
                        inp.columnCount = 4
                        table = ct.add(inp)
                        lines.append(f'\nct.add(inp) 3x4: SUCCESS')

                        # --- Probe updateCellData signature ---
                        # Try no args
                        try:
                            table.updateCellData()
                        except Exception as e:
                            lines.append(f'updateCellData(): {e}')

                        # Try formats that make sense for a table
                        data_attempts = [
                            ('list of lists',       [['Feature','Dim','Nominal','Tol'],['Sketch1','d1','25.0','±0.1'],['Sketch1','d2','10.0','±0.05']]),
                            ('flat list',           ['Feature','Dim','Nominal','Tol','Sketch1','d1','25.0','±0.1']),
                            ('list of dicts',       [{'Feature':'Sketch1','Dim':'d1','Nominal':'25.0','Tol':'±0.1'}]),
                            ('csv string',          'Feature,Dim,Nominal,Tol\nSketch1,d1,25.0,±0.1'),
                            ('dict with rows/cols', {'rows':[['Feature','Dim'],['Sketch1','d1']],'columnCount':2,'rowCount':2}),
                        ]
                        for label, data in data_attempts:
                            try:
                                table.updateCellData(data)
                                lines.append(f'updateCellData({label}): SUCCESS')
                                break
                            except Exception as e:
                                lines.append(f'updateCellData({label}): {e}')

                        # --- Probe position / placement ---
                        for pos_attempt in [
                            ('setPosition(pt)',    lambda: table.setPosition(adsk.core.Point2D.create(50, 50))),
                            ('position = pt',      lambda: setattr(table, 'position', adsk.core.Point2D.create(50, 50))),
                            ('origin = pt',        lambda: setattr(table, 'origin', adsk.core.Point2D.create(50, 50))),
                            ('placementPoint',     lambda: getattr(table, 'placementPoint')),
                        ]:
                            try:
                                pos_attempt[1]()
                                lines.append(f'{pos_attempt[0]}: SUCCESS')
                            except Exception as e:
                                lines.append(f'{pos_attempt[0]}: {e}')

                    except Exception as e:
                        lines.append(f'\nct.add(3x4): {e}')

                except Exception as e:
                    lines.append(f'\ncreateInput(): {e}')

                if ct.count > 0:
                    t = ct.item(0)
                    lines.append(f'--- customTables[0] dir ---')
                    lines.append(', '.join(sorted(dir(t))))
            except Exception as e:
                lines.append(f'\nsheet.customTables: {e}')

            # --- Try dimension collections on sheet ---
            for dim_attr in ('linearDimensions', 'angularDimensions',
                             'radialDimensions', 'dimensions', 'annotations',
                             'sketchDimensions', 'modelDimensions'):
                try:
                    col = getattr(sheet, dim_attr)
                    lines.append(f'\nsheet.{dim_attr}: EXISTS  count={col.count}')
                    lines.append(f'dir: {", ".join(sorted(dir(col)))}')
                except Exception as e:
                    lines.append(f'sheet.{dim_attr}: {e}')

        # --- Try dimension collections directly on drawing ---
        for dim_attr in ('linearDimensions', 'angularDimensions',
                         'radialDimensions', 'dimensions', 'annotations'):
            try:
                col = getattr(drawing, dim_attr)
                lines.append(f'\ndrawing.{dim_attr}: EXISTS  count={col.count}')
            except Exception as e:
                lines.append(f'drawing.{dim_attr}: {e}')

        # --- Write results ---
        out = os.path.join(os.path.expanduser('~'), 'Desktop', 'fusion_drawing_probe.txt')
        with open(out, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

        ui.messageBox(f'Done. Results on Desktop:\nfusion_drawing_probe.txt')

    except Exception as e:
        if ui:
            ui.messageBox(f'Probe failed: {e}')
