import adsk.core
import adsk.drawing
import os
import datetime

app = None
ui  = None
handlers = []

CMD_ID       = 'FusionGadgets_AcadTest'
CMD_NAME     = 'Test AcadCommand'
WORKSPACE_ID = 'FusionDocumentationEnvironment'
TAB_ID       = 'FusionDocTab'
PANEL_ID     = 'FusionGadgets_DrawingTools'
LOG_PATH     = os.path.join(os.path.expanduser('~'), 'Desktop', 'acad_test.txt')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log_panels():
    """Log all panel IDs currently in the Drawing workspace tab."""
    lines = ['\n=== Drawing workspace panels ===']
    try:
        ws  = ui.workspaces.itemById(WORKSPACE_ID)
        tab = ws.toolbarTabs.itemById(TAB_ID)
        for i in range(tab.toolbarPanels.count):
            p = tab.toolbarPanels.item(i)
            lines.append(f'  [{i}] id={p.id!r}  name={p.name!r}')
    except Exception as e:
        lines.append(f'  ERROR: {e}')
    return lines


def try_cmd(label, *cmds):
    """Run one or more text commands and return result lines."""
    lines = [f'\n--- {label} ---']
    for cmd in cmds:
        try:
            result = app.executeTextCommand(cmd)
            lines.append(f'  OK   {cmd!r}')
            if result:
                lines.append(f'       -> {result!r}')
        except Exception as e:
            lines.append(f'  ERR  {cmd!r}')
            lines.append(f'       -> {e}')
    return lines


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = args.command

            inputs = cmd.commandInputs
            inputs.addTextBoxCommandInput(
                'info', '',
                'Click <b>OK</b> to run AutoCAD command tests on the active sheet.<br>'
                'Results: Desktop/acad_test.txt',
                3, True)

            h_ex  = ExecuteHandler()
            h_des = DestroyHandler()
            cmd.execute.add(h_ex)
            cmd.destroy.add(h_des)
            handlers.extend([h_ex, h_des])
        except Exception as e:
            if ui:
                ui.messageBox(f'CreatedHandler error: {e}')


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        pass   # real work in destroy — calling executeTextCommand here crashes Fusion


class DestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            with open(LOG_PATH, 'w', encoding='utf-8') as f:
                f.write(f'AcadCommand Test 6  {datetime.datetime.now().isoformat(timespec="seconds")}\n')

            def log(lines):
                with open(LOG_PATH, 'a', encoding='utf-8') as f:
                    f.write('\n'.join(lines) + '\n')

            # Write a .scr script file — newlines = Enter in AutoCAD scripts
            # TEXT command: start_point / height / rotation / content / (blank=end)
            scr_path = os.path.join(os.path.expanduser('~'), 'Desktop', 'fusion_test.scr')
            scr_lines = [
                '_.TEXT',
                '50,50',
                '10',
                '0',
                'd1 = 25.400 mm',
                '',            # Enter after text = end TEXT command
                '_.TEXT',
                '50,75',
                '10',
                '0',
                'd2 = 50.800 mm',
                '',
                '_.DIMLINEAR',
                '50,120',
                '150,120',
                '100,105',
            ]
            with open(scr_path, 'w', encoding='ascii') as f:
                f.write('\n'.join(scr_lines) + '\n')

            log([f'\nScript written to: {scr_path}'])

            # Run the script — path with forward slashes, no spaces
            safe_path = scr_path.replace('\\', '/')
            log(try_cmd('SCRIPT execute',
                f'FusionDoc.ExecuteAcadCommand _.SCRIPT "{safe_path}"'))

        except Exception as e:
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(f'\nFATAL: {e}\n')


# ---------------------------------------------------------------------------
# Add-in lifecycle
# ---------------------------------------------------------------------------

def run(context):
    global app, ui
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface

        # Remove any stale button controls left over from a previous run
        ws  = ui.workspaces.itemById(WORKSPACE_ID)
        tab = ws.toolbarTabs.itemById(TAB_ID)
        for i in range(tab.toolbarPanels.count):
            p = tab.toolbarPanels.item(i)
            ctrl = p.controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()

        # Remove stale command definition
        old = ui.commandDefinitions.itemById(CMD_ID)
        if old:
            old.deleteMe()

        cmd_def = ui.commandDefinitions.addButtonDefinition(
            CMD_ID, CMD_NAME,
            'Test FusionDoc.ExecuteAcadCommand — places lines, text, dimensions')

        h = CreatedHandler()
        cmd_def.commandCreated.add(h)
        handlers.append(h)

        # Add to Inspect panel (confirmed to exist from first run)
        panel = tab.toolbarPanels.itemById('InspectPanel')
        if panel is None:
            panel = tab.toolbarPanels.itemById(PANEL_ID)
        if panel is None:
            panel = tab.toolbarPanels.add(PANEL_ID, 'Fusion Gadgets', '', False)

        ctrl = panel.controls.addCommand(cmd_def)
        ctrl.isPromoted = True

    except Exception as e:
        if ui:
            ui.messageBox(f'AcadTest run() error:\n{e}')


def stop(context):
    global ui
    if ui:
        cmd_def = ui.commandDefinitions.itemById(CMD_ID)
        if cmd_def:
            cmd_def.deleteMe()

        try:
            ws  = ui.workspaces.itemById(WORKSPACE_ID)
            tab = ws.toolbarTabs.itemById(TAB_ID)
            panel = tab.toolbarPanels.itemById(PANEL_ID)
            if panel:
                ctrl = panel.controls.itemById(CMD_ID)
                if ctrl:
                    ctrl.deleteMe()
                if panel.controls.count == 0:
                    panel.deleteMe()
        except Exception:
            pass

    handlers.clear()
