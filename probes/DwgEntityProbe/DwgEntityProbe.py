import adsk.core
import adsk.fusion
import adsk.drawing
import os

app = None
ui  = None
handlers = []

CMD_ID       = 'FusionGadgets_DwgEntityProbe'
CMD_NAME     = 'DWG Entity Probe'
WORKSPACE_ID = 'FusionDocumentationEnvironment'
TAB_ID       = 'FusionDocTab'

DESKTOP      = os.path.join(os.path.expanduser('~'), 'Desktop')
DUMP_PATH    = os.path.join(DESKTOP, 'dwg_dump.txt')
LSP_PATH     = os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'Temp', 'ghostforge_probe.lsp')
SCR_PATH     = os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'Temp', 'ghostforge_probe.scr')

# ---------------------------------------------------------------------------
# LISP probe program
# ---------------------------------------------------------------------------
# Written to LSP_PATH, then loaded by the .scr.
# Dumps all DWG entities (type, handle, layer, key coords) plus layer table.
# Also attempts to find viewports and dimension entities.

LISP_PROBE = r'''
(defun _gf_probe (/ f ent data etype pt p2 sc cnt lyr blk)
  (setq f (open "{dump}" "w"))

  ; Safe write helper
  (defun _w (s) (write-line s f))

  ; Safe field reader — returns "-" if group code absent
  (defun _g (code data / v)
    (setq v (cdr (assoc code data)))
    (if v (vl-princ-to-string v) "-"))

  (_w (strcat "SPACE: " (if (= (getvar "CVPORT") 1) "PAPER" "MODEL")))
  (_w (strcat "TILEMODE: " (itoa (getvar "TILEMODE"))))
  (_w (strcat "DWGNAME: " (getvar "DWGNAME")))
  (_w "")

  ; --- Method 1: entnext (walks ALL entities, ignores frozen/locked) ---
  (_w "=== ENTITIES via entnext ===")
  (setq ent (entnext)
        cnt 0)
  (if (not ent)
    (_w "  entnext returned nil — no entities at all")
    (while ent
      (setq data (vl-catch-all-apply 'entget (list ent)))
      (if (vl-catch-all-error-p data)
        (_w (strcat "  HDL=? ERROR: " (vl-catch-all-error-message data)))
        (progn
          (setq etype (_g 0 data))
          (_w (strcat "TYPE=" etype
                      "  HDL=" (_g 5 data)
                      "  LYR=" (_g 8 data)))
          (cond
            ((= etype "INSERT")
             (_w (strcat "  BLOCK=" (_g 2 data)))
             (setq pt (cdr (assoc 10 data)))
             (if pt (_w (strcat "  AT=" (rtos (car pt) 2 4) "," (rtos (cadr pt) 2 4))))
             (setq sc (cdr (assoc 41 data)))
             (if sc (_w (strcat "  XSCALE=" (rtos sc 2 6)))))
            ((= etype "LINE")
             (setq pt (cdr (assoc 10 data)) p2 (cdr (assoc 11 data)))
             (if (and pt p2)
               (_w (strcat "  " (rtos (car pt) 2 3) "," (rtos (cadr pt) 2 3)
                           " -> " (rtos (car p2) 2 3) "," (rtos (cadr p2) 2 3)))))
            ((= etype "CIRCLE")
             (setq pt (cdr (assoc 10 data)))
             (if pt (_w (strcat "  CTR=" (rtos (car pt) 2 3) "," (rtos (cadr pt) 2 3)
                                "  R=" (rtos (cdr (assoc 40 data)) 2 3)))))
            ((= etype "VIEWPORT")
             (setq pt (cdr (assoc 10 data)))
             (if pt (_w (strcat "  CTR=" (rtos (car pt) 2 3) "," (rtos (cadr pt) 2 3)
                                "  W=" (rtos (cdr (assoc 40 data)) 2 3)
                                "  H=" (rtos (cdr (assoc 41 data)) 2 3))))
             (setq sc (cdr (assoc 45 data)))
             (if sc (_w (strcat "  VIEW_H=" (rtos sc 2 6)))))
            ((= etype "DIMENSION")
             (setq pt (cdr (assoc 10 data)))
             (if pt (_w (strcat "  DEFPT=" (rtos (car pt) 2 3) "," (rtos (cadr pt) 2 3))))
             (_w (strcat "  TEXT=" (_g 1 data) "  DTYPE=" (_g 70 data))))
            ((= etype "MTEXT")
             (setq pt (cdr (assoc 10 data)))
             (if pt (_w (strcat "  AT=" (rtos (car pt) 2 3) "," (rtos (cadr pt) 2 3))))
             (_w (strcat "  TEXT=" (substr (_g 1 data) 1 60))))
          )
          (setq cnt (1+ cnt))
        )
      )
      (setq ent (entnext ent))
    )
  )
  (_w (strcat "TOTAL via entnext: " (itoa cnt)))

  ; --- Method 2: entlast sanity check ---
  (_w "")
  (_w "=== ENTLAST ===")
  (setq ent (vl-catch-all-apply 'entlast nil))
  (if (vl-catch-all-error-p ent)
    (_w (strcat "  ERROR: " (vl-catch-all-error-message ent)))
    (if ent
      (progn
        (setq data (entget ent))
        (_w (strcat "  Last entity TYPE=" (_g 0 data) "  HDL=" (_g 5 data))))
      (_w "  nil — database empty")))

  ; --- Block definitions ---
  (_w "")
  (_w "=== BLOCK DEFINITIONS ===")
  (setq blk (vl-catch-all-apply 'tblnext (list "BLOCK" T)))
  (if (vl-catch-all-error-p blk)
    (_w (strcat "  tblnext error: " (vl-catch-all-error-message blk)))
    (while (and blk (not (vl-catch-all-error-p blk)))
      (_w (strcat "  BLOCK: " (cdr (assoc 2 blk))))
      (setq blk (vl-catch-all-apply 'tblnext (list "BLOCK")))))

  ; --- Layer table ---
  (_w "")
  (_w "=== LAYERS ===")
  (setq lyr (vl-catch-all-apply 'tblnext (list "LAYER" T)))
  (if (vl-catch-all-error-p lyr)
    (_w (strcat "  tblnext error: " (vl-catch-all-error-message lyr)))
    (while (and lyr (not (vl-catch-all-error-p lyr)))
      (_w (strcat "  LAYER: " (cdr (assoc 2 lyr))))
      (setq lyr (vl-catch-all-apply 'tblnext (list "LAYER")))))

  ; === Full group-code dump of key entities by handle ===
  ; _dump_ent: write every DXF group code from the entity at the given handle
  (defun _dump_ent (hdl / ent data pair code val xs)
    (_w (strcat "--- hdl=" hdl " ---"))
    (setq ent (vl-catch-all-apply 'handent (list hdl)))
    (cond
      ((vl-catch-all-error-p ent)
       (_w (strcat "  handent ERR: " (vl-catch-all-error-message ent))))
      ((not ent)
       (_w "  not found"))
      (T
       ; plain entget only — xdata request returns group -3 with nested
       ; lists that crash the foreach; skip it
       (setq data (vl-catch-all-apply 'entget (list ent)))
       (if (vl-catch-all-error-p data)
         (_w (strcat "  entget ERR: " (vl-catch-all-error-message data)))
         (foreach pair data
           (setq code (car pair) val (cdr pair))
           (cond
             ((listp val)
              (setq xs (strcat (rtos (car val) 2 4) "," (rtos (cadr val) 2 4)))
              (if (caddr val)
                (setq xs (strcat xs "," (rtos (caddr val) 2 4))))
              (_w (strcat "  " (vl-princ-to-string code) " = POINT " xs)))
             ((numberp val)
              (_w (strcat "  " (vl-princ-to-string code) " = " (vl-princ-to-string val))))
             (T
              (_w (strcat "  " (vl-princ-to-string code) " = " (vl-princ-to-string val))))
           )
         )
       )
      )
    )
  )

  (_w "")
  (_w "=== DIMENSION full entget (FD_Dimensions) ===")
  (_dump_ent "3444")
  (_dump_ent "35D5")
  (_dump_ent "3801")

  (_w "")
  (_w "=== ACDBVIEWREPBLOCKREFERENCE full entget ===")
  (_dump_ent "3270")
  (_dump_ent "32DE")

  (_w "")
  (_w "=== DRAWINGVIEW full entget ===")
  (_dump_ent "3274")
  (_dump_ent "32E1")

  ; === Entities inside the drawing view content blocks ===
  ; The ACDBVIEWREPBLOCKREFERENCE entities reference *S25 (front view)
  ; and *S27 (right view). These blocks contain projected 2D geometry.
  ; Walk the first 30 entities in each block and print type+handle+coords.
  (defun _walk_block (bname / bobj ent data etype cnt pt p2)
    (_w (strcat "--- Block " bname " ---"))
    (setq bobj (vl-catch-all-apply 'tblobjname (list "BLOCK" bname)))
    (if (vl-catch-all-error-p bobj)
      (_w (strcat "  tblobjname ERR: " (vl-catch-all-error-message bobj)))
      (if (not bobj)
        (_w "  not found")
        (progn
          (setq ent (vl-catch-all-apply 'entnext (list bobj))
                cnt 0)
          (while (and ent (< cnt 30))
            (setq data (vl-catch-all-apply 'entget (list ent)))
            (if (not (vl-catch-all-error-p data))
              (progn
                (setq etype (cdr (assoc 0 data)))
                (_w (strcat "  [" (itoa cnt) "] " etype
                            "  HDL=" (cdr (assoc 5 data))))
                (cond
                  ((or (= etype "LINE") (= etype "LWPOLYLINE"))
                   (setq pt (cdr (assoc 10 data)) p2 (cdr (assoc 11 data)))
                   (if pt (_w (strcat "    FROM=" (rtos (car pt) 2 3)
                                      "," (rtos (cadr pt) 2 3))))
                   (if p2 (_w (strcat "    TO=" (rtos (car p2) 2 3)
                                      "," (rtos (cadr p2) 2 3)))))
                  ((= etype "CIRCLE")
                   (setq pt (cdr (assoc 10 data)))
                   (if pt (_w (strcat "    CTR=" (rtos (car pt) 2 3)
                                      "," (rtos (cadr pt) 2 3)
                                      "  R=" (rtos (cdr (assoc 40 data)) 2 3)))))
                )
                (setq cnt (1+ cnt))
              )
            )
            (setq ent (vl-catch-all-apply 'entnext (list ent)))
            (if (vl-catch-all-error-p ent) (setq ent nil))
          )
          (_w (strcat "  Total shown: " (itoa cnt)))
        )
      )
    )
  )

  (_w "")
  (_w "=== BLOCK *S25 (front view projected geometry) ===")
  (_walk_block "*S25")

  (_w "")
  (_w "=== BLOCK *S27 (right view projected geometry) ===")
  (_walk_block "*S27")

  (_w "")
  (_w "=== VIEWPORT full entget ===")
  (_dump_ent "3267")
  (_dump_ent "32D5")

  (_w "")
  (_w "=== DONE ===")
  (close f)
  (princ "\nDump complete.")
)
(_gf_probe)
'''.strip()


# ---------------------------------------------------------------------------
# InvokeDrawingCmdById trial variations
# ---------------------------------------------------------------------------

INVOKE_TRIALS = [
    # ── Sequence A: dim command + click simulation attempts ────────────────────
    # AcadParameters feeds AutoCAD's queue, not Fusion's native command loop.
    # Try Fusion-side click/pick equivalents after starting the dim command.
    # Cork top edge at paper-space (132, 235.776).
    'FusionDoc.InvokeDrawingCmdById FusionDrawingSingleDimensionCmd',
    'FusionDoc.SetCursorPos 132 235.776',     # position cursor on top edge
    'FusionDoc.Click',                        # try bare click
    'FusionDoc.LeftClick',
    'FusionDoc.ClickLeft',
    'FusionDoc.MouseClick',
    'FusionDoc.Pick',
    'FusionDoc.PickAt 132 235.776',
    'FusionDoc.SendClick 132 235.776',
    'FusionDoc.FusionInput 132 235.776',      # hypothetical Fusion equivalent of AcadParameters

    # ── Sequence B: probe *S25 block entity handles ───────────────────────────
    # The ACDBVIEWREPBLOCKREFERENCE for the front view references block *S25.
    # *S25 contains the projected 2D geometry (LINE entities for cork edges).
    # Read its first few entities to find handles we can try with SelectObject.
    # (Handled in the LISP section below — see LISP_PROBE update.)
]


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
                '<b>DWG Entity Probe</b><br>'
                'Dumps the full DWG entity database via AutoLISP, then probes '
                'FusionDoc.InvokeDrawingCmdById with several parameter variations.<br><br>'
                'Open a drawing with at least one view before clicking OK.<br>'
                'Results: <tt>Desktop/dwg_dump.txt</tt> and the message box.',
                6, True)
            h_ex  = ExecuteHandler()
            h_des = DestroyHandler()
            cmd.execute.add(h_ex)
            cmd.destroy.add(h_des)
            handlers.extend([h_ex, h_des])
        except Exception as e:
            if ui:
                ui.messageBox(f'DwgEntityProbe dialog error:\n{e}')


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        _pending['ok'] = True


_pending = {}


class DestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        if not _pending.pop('ok', False):
            return

        results = []

        try:
            # --- Part 1: AutoLISP entity dump ---
            lsp_content = LISP_PROBE.replace('{dump}', DUMP_PATH.replace('\\', '/'))
            with open(LSP_PATH, 'w', encoding='ascii', errors='replace') as f:
                f.write(lsp_content)

            scr_content = f'(load "{LSP_PATH.replace(chr(92), "/")}")\n'
            with open(SCR_PATH, 'w', encoding='ascii', errors='replace') as f:
                f.write(scr_content)

            safe_scr = SCR_PATH.replace('\\', '/')
            r = app.executeTextCommand(
                f'FusionDoc.ExecuteAcadCommand _.SCRIPT "{safe_scr}"')
            results.append(f'LISP dump: {"OK" if r is not None else "no return"} → {DUMP_PATH}')

        except Exception as e:
            results.append(f'LISP dump ERROR: {e}')

        # --- Part 2: InvokeDrawingCmdById trials ---
        results.append('')
        results.append('=== InvokeDrawingCmdById trials ===')
        for trial in INVOKE_TRIALS:
            try:
                r = app.executeTextCommand(trial)
                results.append(f'OK   {trial!r}')
                if r:
                    results.append(f'     -> {r!r}')
            except Exception as e:
                results.append(f'ERR  {trial!r}')
                results.append(f'     -> {e}')

        summary = '\n'.join(results)
        ui.messageBox(f'DwgEntityProbe complete.\n\n{summary}')


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
        for i in range(tab.toolbarPanels.count):
            ctrl = tab.toolbarPanels.item(i).controls.itemById(CMD_ID)
            if ctrl:
                ctrl.deleteMe()
        old = ui.commandDefinitions.itemById(CMD_ID)
        if old:
            old.deleteMe()

        cmd_def = ui.commandDefinitions.addButtonDefinition(
            CMD_ID, CMD_NAME,
            'Dump DWG entity database via AutoLISP; probe InvokeDrawingCmdById')

        h = CreatedHandler()
        cmd_def.commandCreated.add(h)
        handlers.append(h)

        panel = tab.toolbarPanels.itemById('InspectPanel')
        if panel is None:
            panel = tab.toolbarPanels.add('FusionGadgets_DrawingTools', 'Fusion Gadgets', '', False)

        ctrl = panel.controls.addCommand(cmd_def)
        ctrl.isPromoted = True

    except Exception as e:
        if ui:
            ui.messageBox(f'DwgEntityProbe run() error:\n{e}')


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
