import adsk.core
import adsk.drawing
import os
import datetime

app = None
ui  = None
LOG_PATH = os.path.join(os.path.expanduser('~'), 'Desktop', 'fusion_commands.tsv')
handlers = []


class CommandStartingHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        try:
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(f'{datetime.datetime.now().isoformat(timespec="milliseconds")}'
                        f'\tSTARTING\t{args.commandId}\n')
        except Exception as e:
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(f'ERROR_STARTING\t{e}\n')


class CommandTerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    def notify(self, args):
        try:
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(f'{datetime.datetime.now().isoformat(timespec="milliseconds")}'
                        f'\tTERMINATED\t{args.commandId}\t{args.terminationReason}\n')
        except Exception as e:
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(f'ERROR_TERMINATED\t{e}\n')


def run(context):
    global app, ui
    app = adsk.core.Application.get()
    ui  = app.userInterface

    with open(LOG_PATH, 'w', encoding='utf-8') as f:
        f.write('timestamp\tevent\tcommand_id\tdetail\n')

    h1 = CommandStartingHandler()
    h2 = CommandTerminatedHandler()
    ui.commandStarting.add(h1)
    ui.commandTerminated.add(h2)
    handlers.extend([h1, h2])
    # Silent start — go operate the drawing workspace freely


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
