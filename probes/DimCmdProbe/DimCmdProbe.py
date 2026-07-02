import adsk.core
import adsk.drawing
import os
import datetime

app = None
ui  = None
handlers = []

CMD_ID   = 'FusionDrawingSingleDimensionCmd'
LOG_PATH = os.path.join(os.path.expanduser('~'), 'Desktop', 'dim_cmd_probe.txt')


def snapshot_sheet(label):
    """Probe every accessible attribute on the active drawing sheet."""
    try:
        draw_doc = adsk.drawing.DrawingDocument.cast(app.activeDocument)
        if not draw_doc:
            return
        sheet = draw_doc.drawing.activeSheet
        lines = [f'\n{"="*60}', f'{label}  {datetime.datetime.now().isoformat(timespec="milliseconds")}', f'{"="*60}']

        # All non-callable, non-private attributes on sheet
        for attr in sorted(dir(sheet)):
            if attr.startswith('_'):
                continue
            try:
                val = getattr(sheet, attr)
                if callable(val):
                    continue
                lines.append(f'sheet.{attr} = {val}')
            except Exception as e:
                lines.append(f'sheet.{attr}: ERR {e}')

        # Collections — probe count + first item's type and dir
        for col_name in [
            'drawingViews', 'views', 'annotations', 'dimensions',
            'linearDimensions', 'radialDimensions', 'angularDimensions',
            'customTables', 'sketches', 'bodies', 'notes', 'symbols',
            'hatchPatterns', 'centerMarks', 'centerLines',
        ]:
            try:
                col = getattr(sheet, col_name)
                count = col.count
                lines.append(f'\nsheet.{col_name}.count = {count}')
                if count > 0:
                    item = col.item(0)
                    lines.append(f'  [0] objectType = {item.objectType}')
                    lines.append(f'  [0] dir = {", ".join(d for d in sorted(dir(item)) if not d.startswith("_"))}')
            except Exception as e:
                lines.append(f'sheet.{col_name}: {e}')

        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
    except Exception as e:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f'{label} SNAPSHOT ERROR: {e}\n')


class StartingHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        if args.commandId == CMD_ID:
            snapshot_sheet('BEFORE — dimension command starting')


class TerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        if args.commandId == CMD_ID:
            reason = args.terminationReason
            snapshot_sheet(f'AFTER  — dimension command terminated (reason={reason})')


def run(context):
    global app, ui
    app = adsk.core.Application.get()
    ui  = app.userInterface

    with open(LOG_PATH, 'w', encoding='utf-8') as f:
        f.write('DimCmdProbe v2 — place one dimension on the drawing, then stop this add-in.\n')

    h1 = StartingHandler()
    h2 = TerminatedHandler()
    ui.commandStarting.add(h1)
    ui.commandTerminated.add(h2)
    handlers.extend([h1, h2])


def stop(context):
    global ui
    if ui:
        for h in handlers:
            try:
                ui.commandStarting.remove(h)
            except Exception:
                pass
            try:
                ui.commandTerminated.remove(h)
            except Exception:
                pass
    handlers.clear()
