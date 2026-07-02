import adsk.core, adsk.fusion, adsk.drawing
import os, math

app = None
ui  = None
handlers = []
_pending = {}

CMD_ID       = 'FusionGadgets_DimAnnotate'
CMD_NAME     = 'Dim Annotate'
WORKSPACE_ID = 'FusionDocumentationEnvironment'
TAB_ID       = 'FusionDocTab'
PANEL_ID     = 'InspectPanel'

INTERNAL_TO_MM = 10.0
SCR_PATH = os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'Temp',
                        'ghostforge_dimannotate.scr')


# ── Design parameter collection ───────────────────────────────────────────────

def find_open_design():
    for i in range(app.documents.count):
        try:
            doc = app.documents.item(i)
            design = adsk.fusion.Design.cast(
                doc.products.itemByProductType('DesignProductType'))
            if design:
                return design
        except Exception:
            continue
    return None


def collect_top3_lengths(design):
    """
    Returns top 3 positive length parameters by value, largest first.
    [(name, value_mm), ...]
    Reads sketch dims → extrude extents → allParameters (minus already seen).
    Skips dimensionless (unit='') and angles (unit='deg').
    """
    seen = set()
    rows = []

    for comp in design.allComponents:
        for sketch in comp.sketches:
            for dim in sketch.sketchDimensions:
                try:
                    p = dim.parameter
                    if p is None or p.name in seen:
                        continue
                    unit = (p.unit or '').strip()
                    if unit in ('', 'ul', 'deg'):
                        continue
                    val_mm = p.value * INTERNAL_TO_MM
                    if val_mm <= 0:
                        continue
                    seen.add(p.name)
                    rows.append((p.name, val_mm))
                except Exception:
                    pass

        for i in range(comp.features.extrudeFeatures.count):
            try:
                ef = comp.features.extrudeFeatures.item(i)
                for side in ('extentOne', 'extentTwo'):
                    try:
                        ext = getattr(ef, side)
                        dist = adsk.fusion.DistanceExtentDefinition.cast(ext)
                        if dist:
                            p = dist.distance
                            if p and p.name not in seen:
                                val_mm = p.value * INTERNAL_TO_MM
                                if val_mm > 0:
                                    seen.add(p.name)
                                    rows.append((p.name, val_mm))
                    except Exception:
                        pass
            except Exception:
                pass

    try:
        for i in range(design.allParameters.count):
            p = design.allParameters.item(i)
            unit = (p.unit or '').strip()
            if p.name in seen or unit in ('', 'ul', 'deg'):
                continue
            val_mm = p.value * INTERNAL_TO_MM
            if val_mm > 0:
                seen.add(p.name)
                rows.append((p.name, val_mm))
    except Exception:
        pass

    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:3]


# ── LISP script builder ───────────────────────────────────────────────────────

LISP_TEMPLATE = r'''
(defun _gf_dim_annotate (/ _all_assoc _mkdim ent data views front side
                            fx fy fscale fw fh sx sy sscale sw sh
                            f_left f_right f_top f_bot s_left s_right s_top
                            margin dim_gap layout style g40s g10s
                            v_len v_wid v_thk pair)

  (setq v_len  {v_len}
        v_wid  {v_wid}
        v_thk  {v_thk}
        margin  3.25
        dim_gap 10.0
        layout  (getvar "CTAB")
        style   (if (tblsearch "DIMSTYLE" "Dim-ISO-25") "Dim-ISO-25" "Standard"))

  ; Collect all occurrences of group code `code` in an entget alist
  (defun _all_assoc (code alist / r p)
    (setq r (quote ()))
    (foreach p alist
      (if (= (car p) code) (setq r (append r (list (cdr p))))))
    r
  )

  ; entmake one AcDbRotatedDimension
  (defun _mkdim (val ex1 ey1 ex2 ey2 dpx dpy tpx tpy rot)
    (entmake
      (list (cons 0 "DIMENSION") (cons 8 "GF_Dimensions")
            (cons 67 1) (cons 410 layout) (cons 70 32)
            (cons 10 (list dpx dpy 0.0)) (cons 11 (list tpx tpy 0.0))
            (cons 12 (list 0.0 0.0 0.0)) (cons 1 "") (cons 71 5)
            (cons 72 1) (cons 41 1.0) (cons 42 val)
            (cons 73 0) (cons 74 0) (cons 75 0)
            (cons 52 0.0) (cons 53 0.0) (cons 54 0.0) (cons 51 0.0)
            (cons 210 (list 0.0 0.0 1.0)) (cons 3 style)
            (cons 13 (list ex1 ey1 0.0)) (cons 14 (list ex2 ey2 0.0))
            (cons 15 (list 0.0 0.0 0.0)) (cons 16 (list 0.0 0.0 0.0))
            (cons 40 0.0) (cons 50 rot)))
  )

  ; Delete all entities on GF_Dimensions (idempotent refresh)
  (setq ent (entnext))
  (while ent
    (setq data (vl-catch-all-apply (quote entget) (list ent)))
    (if (and (not (vl-catch-all-error-p data))
             (equal (cdr (assoc 8 data)) "GF_Dimensions"))
      (vl-catch-all-apply (quote entdel) (list ent))
    )
    (setq ent (entnext ent))
  )

  ; Create GF_Dimensions layer if absent (color 30 = orange)
  (if (not (tblsearch "LAYER" "GF_Dimensions"))
    (command "_.LAYER" "_N" "GF_Dimensions" "_C" "30" "GF_Dimensions" "")
  )

  ; Walk entities to find DRAWINGVIEW records
  (setq views (quote ()) ent (entnext))
  (while ent
    (setq data (vl-catch-all-apply (quote entget) (list ent)))
    (if (and (not (vl-catch-all-error-p data))
             (= (cdr (assoc 0 data)) "DRAWINGVIEW"))
      (progn
        ; group 40 appears 4×: scale, 0, cx, cy
        ; group 10 appears 2×: bbox lower-left, bbox upper-right
        (setq g40s (_all_assoc 40 data)
              g10s (_all_assoc 10 data))
        (if (and (>= (length g40s) 4) (>= (length g10s) 2))
          (setq views (append views (list
            (list
              (nth 2 g40s) (nth 3 g40s) ; cx, cy
              (nth 0 g40s)              ; scale
              (abs (- (car  (nth 1 g10s)) (car  (nth 0 g10s)))) ; bbox_w
              (abs (- (cadr (nth 1 g10s)) (cadr (nth 0 g10s)))) ; bbox_h
            )
          )))
        )
      )
    )
    (setq ent (entnext ent))
  )

  ; Need at least 2 views (front + side)
  (if (< (length views) 2)
    (progn (princ "\nDimAnnotate: need at least 2 drawing views."))
    (progn

      ; Wider bbox_w = front view; narrower = side view
      (if (>= (nth 3 (nth 0 views)) (nth 3 (nth 1 views)))
        (setq front (nth 0 views) side (nth 1 views))
        (setq front (nth 1 views) side (nth 0 views))
      )
      (setq fx (nth 0 front) fy (nth 1 front) fscale (nth 2 front)
            fw (nth 3 front) fh (nth 4 front))
      (setq sx (nth 0 side)  sy (nth 1 side)  sscale (nth 2 side)
            sw (nth 3 side)  sh (nth 4 side))

      ; Part edges in paper space (scale-aware, part centered on viewport origin)
      (setq f_left  (- fx (* v_wid fscale 0.5))
            f_right (+ fx (* v_wid fscale 0.5))
            f_top   (+ fy (* v_len fscale 0.5))
            f_bot   (- fy (* v_len fscale 0.5))
            s_left  (- sx (* v_thk sscale 0.5))
            s_right (+ sx (* v_thk sscale 0.5))
            s_top   (+ sy (* v_len sscale 0.5)))

      ; DIM 1 — LENGTH (vertical), left of front view
      (_mkdim v_len
              f_left f_top  f_left f_bot
              (- f_left dim_gap) f_bot
              (- f_left dim_gap 5.0) fy
              4.71239)

      ; DIM 2 — WIDTH (horizontal), above front view
      (_mkdim v_wid
              f_left f_top  f_right f_top
              f_right (+ f_top dim_gap)
              fx      (+ f_top dim_gap 3.5)
              0.0)

      ; DIM 3 — THICKNESS (horizontal), above side view
      ; Text goes to the right for narrow parts
      (_mkdim v_thk
              s_left s_top  s_right s_top
              s_right (+ s_top dim_gap)
              (+ s_right (max 8.0 (/ v_thk 2.0))) (+ s_top dim_gap 3.5)
              0.0)

      (princ (strcat "\nDimAnnotate: placed 3 dimensions."
                     " L=" (rtos v_len 2 2)
                     " W=" (rtos v_wid 2 2)
                     " T=" (rtos v_thk 2 2)))
    )
  )
)
(_gf_dim_annotate)
'''


def build_dim_scr(params, scr_path):
    """Write the .scr file (just a LOAD of the inline LISP)."""
    v_len = params[0][1]
    v_wid = params[1][1]
    v_thk = params[2][1]

    lsp = LISP_TEMPLATE.format(
        v_len=f'{v_len:.4f}',
        v_wid=f'{v_wid:.4f}',
        v_thk=f'{v_thk:.4f}',
    )

    with open(scr_path, 'w', encoding='ascii', errors='replace') as f:
        f.write(lsp.strip() + '\n')


# ── Command handlers ──────────────────────────────────────────────────────────

class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd    = args.command
            inputs = cmd.commandInputs

            design = find_open_design()
            if not design:
                info = '<b>No open design found.</b> Open both the design and its drawing.'
            else:
                params = collect_top3_lengths(design)
                if len(params) < 3:
                    info = (f'<b>Only {len(params)} length parameter(s) found</b> — need 3.<br>'
                            f'Ensure the design has sketch dims, an extrude, and a fillet.')
                else:
                    info = (
                        f'<b>Parameters (auto-detected, largest first):</b><br>'
                        f'&nbsp; Length&nbsp;&nbsp;&nbsp;: <b>{params[0][0]}</b>'
                        f' = {params[0][1]:.2f} mm<br>'
                        f'&nbsp; Width&nbsp;&nbsp;&nbsp;&nbsp;: <b>{params[1][0]}</b>'
                        f' = {params[1][1]:.2f} mm<br>'
                        f'&nbsp; Thickness: <b>{params[2][0]}</b>'
                        f' = {params[2][1]:.2f} mm<br><br>'
                        f'Dims placed on layer <b>GF_Dimensions</b>.<br>'
                        f'Run again after design changes to refresh.'
                    )

            inputs.addTextBoxCommandInput('info', '', info, 9, True)

            h_ex  = ExecuteHandler()
            h_des = DestroyHandler()
            cmd.execute.add(h_ex)
            cmd.destroy.add(h_des)
            handlers.extend([h_ex, h_des])
        except Exception as e:
            if ui:
                ui.messageBox(f'DimAnnotate dialog error:\n{e}')


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        _pending['ok'] = True


class DestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        if not _pending.pop('ok', False):
            return

        design = find_open_design()
        if not design:
            ui.messageBox('DimAnnotate: no open design found.')
            return

        params = collect_top3_lengths(design)
        if len(params) < 3:
            ui.messageBox(f'DimAnnotate: need 3 length parameters, found {len(params)}.')
            return

        build_dim_scr(params, SCR_PATH)

        safe_path = SCR_PATH.replace('\\', '/')
        app.executeTextCommand(f'FusionDoc.ExecuteAcadCommand _.SCRIPT "{safe_path}"')

        ui.messageBox(
            f'DimAnnotate complete.\n\n'
            f'Length    : {params[0][0]} = {params[0][1]:.1f} mm\n'
            f'Width     : {params[1][0]} = {params[1][1]:.1f} mm\n'
            f'Thickness : {params[2][0]} = {params[2][1]:.1f} mm\n\n'
            f'Placed on layer GF_Dimensions (orange).\n'
            f'Run again after design changes to refresh.'
        )


# ── Add-in lifecycle ──────────────────────────────────────────────────────────

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
            'Place or refresh dimension annotations on the active drawing sheet.\n'
            'Reads design parameters; places AcDbRotatedDimension entities on GF_Dimensions layer.')

        h = CreatedHandler()
        cmd_def.commandCreated.add(h)
        handlers.append(h)

        panel = tab.toolbarPanels.itemById(PANEL_ID)
        if panel is None:
            panel = tab.toolbarPanels.add(
                'FusionGadgets_DrawingTools', 'Fusion Gadgets', '', False)

        ctrl = panel.controls.addCommand(cmd_def)
        ctrl.isPromoted = True

    except Exception as e:
        if ui:
            ui.messageBox(f'DimAnnotate run() error:\n{e}')


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
