"""GhostForge — Fusion 360 drawing automation add-in.

Single panel in the Drawing workspace (FusionDocTab) containing:
  • Sketch Annotate  — places all sketch parameters as text
  • Dim Annotate     — places 3 AcDbRotatedDimension entities (L / W / T)
  • DWG Probe        — diagnostic entity dump + command trials (dev tool)
"""
import adsk.core, adsk.fusion, adsk.drawing, os, math

# ─── Globals ─────────────────────────────────────────────────────────────────
app      = None
ui       = None
handlers = []
_pending = {'sketch': {}, 'dims': {}, 'probe': {}}   # namespaced per command

# ─── Constants ───────────────────────────────────────────────────────────────
WORKSPACE_ID = 'FusionDocumentationEnvironment'
GF_TAB_ID    = 'GhostForge_Tab'
GF_TAB_NAME  = 'GhostForge'

# One panel per command so each shows as a standalone button in the tab
GF_SA_PANEL  = 'GhostForge_SA_Panel'
GF_DA_PANEL  = 'GhostForge_DA_Panel'
GF_DP_PANEL  = 'GhostForge_DP_Panel'

CMD_SKETCH   = 'GhostForge_SketchAnnotate'
CMD_DIMS     = 'GhostForge_DimAnnotate'
CMD_PROBE    = 'GhostForge_DwgProbe'

INTERNAL_TO_MM = 10.0
TEMP_DIR   = os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'Temp')
DESKTOP    = os.path.join(os.path.expanduser('~'), 'Desktop')
_RESOURCES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources')


def _tmp(name):
    return os.path.join(TEMP_DIR, name)


# ─── Shared helpers ───────────────────────────────────────────────────────────

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


_TYPE_MAP = {
    'SketchLinearDimension':             'Lin',
    'SketchAngularDimension':            'Ang',
    'SketchRadialDimension':             'Rad',
    'SketchDiameterDimension':           'Dia',
    'SketchOffsetDimension':             'Off',
    'SketchEllipseMajorRadiusDimension': 'Elp',
    'SketchEllipseMinorRadiusDimension': 'Elp',
}


def collect_all_params(design):
    """All sketch dimensions, extrude depths, and remaining parameters."""
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
                    label = _TYPE_MAP.get(raw, 'Dim')
                    rows.append((param.name, param.value * INTERNAL_TO_MM, label))
                except Exception:
                    continue
            if rows:
                result.setdefault(sketch.name, []).extend(rows)

        extrude_rows = []
        for i in range(comp.features.extrudeFeatures.count):
            try:
                ef = comp.features.extrudeFeatures.item(i)
                for side in ('extentOne', 'extentTwo'):
                    try:
                        dist = adsk.fusion.DistanceExtentDefinition.cast(getattr(ef, side))
                        if dist:
                            p = dist.distance
                            if p:
                                extrude_rows.append(
                                    (p.name, p.value * INTERNAL_TO_MM, ef.name))
                    except Exception:
                        pass
            except Exception:
                continue
        if extrude_rows:
            result['Extrude'] = extrude_rows

    known = {name for rows in result.values() for name, _, _ in rows}
    other = []
    try:
        for i in range(design.allParameters.count):
            p = design.allParameters.item(i)
            if p.name in known:
                continue
            unit = (p.unit or '').strip()
            if unit in ('', 'ul', 'unitless'):
                continue
            if unit == 'deg':
                other.append((p.name, math.degrees(p.value), 'deg'))
            else:
                other.append((p.name, p.value * INTERNAL_TO_MM, 'Feature'))
    except Exception:
        pass
    if other:
        result['Other Features'] = other
    return result


def collect_top3_lengths(design):
    """Top 3 positive length parameters by mm value, largest first."""
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
                    val = p.value * INTERNAL_TO_MM
                    if val > 0:
                        seen.add(p.name)
                        rows.append((p.name, val))
                except Exception:
                    pass
        for i in range(comp.features.extrudeFeatures.count):
            try:
                ef = comp.features.extrudeFeatures.item(i)
                for side in ('extentOne', 'extentTwo'):
                    try:
                        dist = adsk.fusion.DistanceExtentDefinition.cast(getattr(ef, side))
                        if dist:
                            p = dist.distance
                            if p and p.name not in seen:
                                val = p.value * INTERNAL_TO_MM
                                if val > 0:
                                    seen.add(p.name)
                                    rows.append((p.name, val))
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
            val = p.value * INTERNAL_TO_MM
            if val > 0:
                seen.add(p.name)
                rows.append((p.name, val))
    except Exception:
        pass

    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:3]


# ─── SketchAnnotate ───────────────────────────────────────────────────────────

def _build_sketch_scr(sketch_groups, x, y_start, height, spacing, scr_path):
    lines = []
    y     = float(y_start)
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


class SACreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd    = args.command
            inputs = cmd.commandInputs
            inputs.addTextBoxCommandInput(
                'info', '',
                '<b>Sketch Annotate</b><br>'
                'Places all sketch dimensions, extrude depths, and feature parameters '
                'as persistent text on the active drawing sheet.<br><br>'
                'Set the anchor point and text height (mm).',
                4, True)
            inputs.addIntegerSpinnerCommandInput('x_pos',      'Start X (mm)',     0, 10000, 1,  10)
            inputs.addIntegerSpinnerCommandInput('y_pos',      'Start Y (mm)',     0, 10000, 1, 250)
            inputs.addIntegerSpinnerCommandInput('txt_height', 'Text height (mm)', 1,    50, 1,   4)
            h_ex = SAExecuteHandler()
            h_ds = SADestroyHandler()
            cmd.execute.add(h_ex)
            cmd.destroy.add(h_ds)
            handlers.extend([h_ex, h_ds])
        except Exception as e:
            if ui:
                ui.messageBox(f'Sketch Annotate dialog error:\n{e}')


class SAExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        inputs = args.command.commandInputs
        _pending['sketch']['ok'] = True
        _pending['sketch']['x']  = inputs.itemById('x_pos').value
        _pending['sketch']['y']  = inputs.itemById('y_pos').value
        _pending['sketch']['h']  = inputs.itemById('txt_height').value


class SADestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        if not _pending['sketch'].pop('ok', False):
            return
        x = _pending['sketch'].pop('x', 10)
        y = _pending['sketch'].pop('y', 250)
        h = _pending['sketch'].pop('h', 4)

        design = find_open_design()
        if not design:
            ui.messageBox('Sketch Annotate: no open design found.')
            return

        groups   = collect_all_params(design)
        if not groups:
            ui.messageBox('Sketch Annotate: no sketch dimensions found in the design.')
            return

        scr_path = _tmp('ghostforge_sketch.scr')
        total    = _build_sketch_scr(groups, x, y, h, h * 2.0, scr_path)
        app.executeTextCommand(
            f'FusionDoc.ExecuteAcadCommand _.SCRIPT "{scr_path.replace(chr(92), "/")}"')
        ui.messageBox(
            f'Sketch Annotate complete.\n\n'
            f'Placed {total} annotation{"s" if total != 1 else ""} '
            f'from {len(groups)} group{"s" if len(groups) != 1 else ""}.')


# ─── DimAnnotate ─────────────────────────────────────────────────────────────

_LISP_DIM = r'''
(defun _gf_dim (/ _mkdim _dbg
                   ent data next_ent views front side lv
                   fx fy fscale fw fh sx sy sscale sw sh
                   f_left f_right f_top f_bot s_left s_right s_top
                   dim_gap layout r1 r2 r3 orig_layer
                   g40s g10s pair _style _layer _dbgf)

  ; ── Setup ──────────────────────────────────────────────────────────────
  ; NOTE: (getvar "CTAB") and (getvar "DIMSTYLE") return boolean T in
  ;       Fusion's LISP — NOT strings.  Get layout from DRAWINGVIEW g410.
  (setq v_len   {v_len}
        v_wid   {v_wid}
        v_thk   {v_thk}
        dim_gap 10.0
        layout  "Sheet1")

  ; ── Debug helpers ───────────────────────────────────────────────────────
  (setq _dbgf (open "{debug_path}" "w"))
  (defun _dbg (s) (write-line s _dbgf))

  (_dbg "=== GhostForge DimAnnotate ===")
  (_dbg (strcat "CVPORT="    (vl-princ-to-string (getvar "CVPORT"))
                "  TILEMODE=" (vl-princ-to-string (getvar "TILEMODE"))))
  (_dbg (strcat "v_len=" (rtos v_len 2 4)
                "  v_wid=" (rtos v_wid 2 4)
                "  v_thk=" (rtos v_thk 2 4)))

  ; ── Helpers ─────────────────────────────────────────────────────────────
  ; NOTE: foreach is NOT available in Fusion's LISP subset.
  ;       All alist walks use while/cdr instead.

  (defun _mkdim (val ex1 ey1 ex2 ey2 dpx dpy tpx tpy rot)
    ; No groups 67/410 — CVPORT=1 TILEMODE=0 already means paper space.
    ; group 3 (DIMSTYLE name) = "" → use current style; avoids tblsearch.
    (entmake
      (list (cons 0 "DIMENSION") (cons 8 _layer)
            (cons 70 32)
            (cons 10 (list dpx dpy 0.0)) (cons 11 (list tpx tpy 0.0))
            (cons 12 (list 0.0 0.0 0.0)) (cons 1 "") (cons 71 5)
            (cons 72 1) (cons 41 1.0) (cons 42 val)
            (cons 73 0) (cons 74 0) (cons 75 0)
            (cons 52 0.0) (cons 53 0.0) (cons 54 0.0) (cons 51 0.0)
            (cons 210 (list 0.0 0.0 1.0)) (cons 3 _style)
            (cons 13 (list ex1 ey1 0.0)) (cons 14 (list ex2 ey2 0.0))
            (cons 15 (list 0.0 0.0 0.0)) (cons 16 (list 0.0 0.0 0.0))
            (cons 40 0.0) (cons 50 rot)))
  )

  ; ── Delete existing GF_Dimensions entities ──────────────────────────────
  ; Save next BEFORE deleting to avoid broken entnext on soft-deleted ent
  (setq ent (entnext))
  (while ent
    (setq data     (vl-catch-all-apply (quote entget) (list ent)))
    (setq next_ent (entnext ent))
    (if (and (not (vl-catch-all-error-p data))
             (= (cdr (assoc 8 data)) "GF_Dimensions"))
      (vl-catch-all-apply (quote entdel) (list ent))
    )
    (setq ent next_ent)
  )

  ; ── Ensure layer exists ─────────────────────────────────────────────────
  ; Use _.-LAYER (dash = non-interactive/scriptable version, no dialog).
  ; Do NOT wrap command in vl-catch-all-apply — command is a special form.
  ; Fall back to layer "0" if creation fails so dims still appear.
  (setq _layer "0")
  (if (tblsearch "LAYER" "GF_Dimensions")
    (progn
      (_dbg "layer GF_Dimensions exists")
      (setq _layer "GF_Dimensions")
    )
    (progn
      (_dbg "creating layer via command _.-LAYER")
      (command "_.-LAYER" "N" "GF_Dimensions" "C" "30" "GF_Dimensions" "")
      (if (tblsearch "LAYER" "GF_Dimensions")
        (progn (_dbg "layer created OK") (setq _layer "GF_Dimensions"))
        (_dbg "layer creation failed - using layer 0")
      )
    )
  )
  (_dbg (strcat "using layer=" _layer))

  ; ── Find a valid DIMSTYLE ────────────────────────────────────────────────
  ; getvar "DIMSTYLE" returns boolean T in Fusion — use tblsearch instead
  (setq _style
    (cond
      ((tblsearch "DIMSTYLE" "FD_Dimensions_Style") "FD_Dimensions_Style")
      ((tblsearch "DIMSTYLE" "Standard")            "Standard")
      (T                                            "Standard")
    ))
  (_dbg (strcat "DIMSTYLE=" _style))

  ; ── Collect DRAWINGVIEW records: cx, cy, scale, bbox_w, bbox_h ──────────
  ; group 40 x4: scale, 0, cx, cy   |   group 10 x2: bbox LL, bbox UR
  (_dbg "starting entity walk")
  (setq views nil ent (entnext))
  (_dbg (strcat "first ent=" (vl-princ-to-string ent)))
  (while ent
    (setq data (vl-catch-all-apply (quote entget) (list ent)))
    (if (and (not (vl-catch-all-error-p data))
             (= (cdr (assoc 0 data)) "DRAWINGVIEW"))
      (progn
        (_dbg (strcat "found DRAWINGVIEW hdl=" (vl-princ-to-string (cdr (assoc 5 data)))))
        (setq lv (cdr (assoc 410 data)))
        (if lv (setq layout lv))   ; group 410 is always a string — no stringp needed
        ; Collect all group-40 and group-10 values via while/cdr
        ; stringp/listp/foreach not in Fusion's AutoLISP subset
        (setq g40s nil  g10s nil  pair data)
        (while pair
          (cond
            ((= (car (car pair)) 40) (setq g40s (append g40s (list (cdr (car pair))))))
            ((= (car (car pair)) 10) (setq g10s (append g10s (list (cdr (car pair))))))
          )
          (setq pair (cdr pair))
        )
        (_dbg (strcat "  g40s#=" (itoa (length g40s)) "  g10s#=" (itoa (length g10s))))
        (if (and (>= (length g40s) 4) (>= (length g10s) 2))
          (setq views (append views (list
            (list (nth 2 g40s) (nth 3 g40s) (nth 0 g40s)
                  (abs (- (car  (nth 1 g10s)) (car  (nth 0 g10s))))
                  (abs (- (cadr (nth 1 g10s)) (cadr (nth 0 g10s))))))))
        )
      )
    )
    (setq ent (entnext ent))
  )

  (_dbg (strcat "layout=" layout "  views=" (itoa (length views))))

  (if (< (length views) 2)
    (_dbg "Need >=2 drawing views — abort")
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

      (setq f_left  (- fx (* v_wid fscale 0.5))
            f_right (+ fx (* v_wid fscale 0.5))
            f_top   (+ fy (* v_len fscale 0.5))
            f_bot   (- fy (* v_len fscale 0.5))
            s_left  (- sx (* v_thk sscale 0.5))
            s_right (+ sx (* v_thk sscale 0.5))
            s_top   (+ sy (* v_len sscale 0.5)))

      (_dbg (strcat "front: cx=" (rtos fx 2 3) " cy=" (rtos fy 2 3)
                    " sc=" (rtos fscale 2 4)
                    " | f_left=" (rtos f_left 2 3) " f_top=" (rtos f_top 2 3)
                    " f_right=" (rtos f_right 2 3) " f_bot=" (rtos f_bot 2 3)))
      (_dbg (strcat "side:  cx=" (rtos sx 2 3) " cy=" (rtos sy 2 3)
                    " s_top=" (rtos s_top 2 3)))

      ; ── Attempt 1: entmake DIMENSION ──────────────────────────────────
      (_dbg "--- entmake DIMENSION attempt ---")
      (setq r1 (_mkdim v_len
                       f_left f_top  f_left f_bot
                       (- f_left dim_gap) f_bot
                       (- f_left dim_gap 5.0) fy  1.5708))
      (setq r2 (_mkdim v_wid
                       f_left f_top  f_right f_top
                       f_right (+ f_top dim_gap)
                       fx (+ f_top dim_gap 3.5)  0.0))
      (setq r3 (_mkdim v_thk
                       s_left s_top  s_right s_top
                       s_right (+ s_top dim_gap)
                       (+ s_right (max 8.0 (/ v_thk 2.0)))
                       (+ s_top dim_gap 3.5)  0.0))
      (_dbg (strcat "entmake: r1=" (vl-princ-to-string r1)
                    "  r2=" (vl-princ-to-string r2)
                    "  r3=" (vl-princ-to-string r3)))

      ; ── Attempt 2: command _.DIMLINEAR (if entmake returned nil) ──────
      (if (not (or r1 r2 r3))
        (progn
          (_dbg "--- entmake failed, trying command _.DIMLINEAR ---")
          (setq orig_layer (getvar "CLAYER"))
          (setvar "CLAYER" _layer)

          ; Length — vertical (both pts share same X → DIMLINEAR auto-selects vertical)
          (command "_.DIMLINEAR"
            (list f_left f_top 0.0)
            (list f_left f_bot 0.0)
            (list (- f_left dim_gap) fy 0.0))
          (_dbg "DIMLINEAR length placed")

          ; Width — horizontal (both pts share same Y → auto-selects horizontal)
          (command "_.DIMLINEAR"
            (list f_left f_top 0.0)
            (list f_right f_top 0.0)
            (list fx (+ f_top dim_gap) 0.0))
          (_dbg "DIMLINEAR width placed")

          ; Thickness — horizontal
          (command "_.DIMLINEAR"
            (list s_left s_top 0.0)
            (list s_right s_top 0.0)
            (list sx (+ s_top dim_gap) 0.0))
          (_dbg "DIMLINEAR thickness placed")

          (setvar "CLAYER" orig_layer)
          (_dbg "--- DIMLINEAR fallback done ---")
        )
      )
    )
  )

  (close _dbgf)
  (princ "\nGF dim done.")
)
(_gf_dim)
'''


def _build_dim_scr(params, scr_path):
    debug_path = _tmp('gf_dim_debug.txt').replace('\\', '/')
    lsp = _LISP_DIM.format(
        v_len=f'{params[0][1]:.4f}',
        v_wid=f'{params[1][1]:.4f}',
        v_thk=f'{params[2][1]:.4f}',
        debug_path=debug_path,
    )
    with open(scr_path, 'w', encoding='ascii', errors='replace') as f:
        f.write(lsp.strip() + '\n')


class DACreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd    = args.command
            inputs = cmd.commandInputs
            design = find_open_design()
            if not design:
                info = '<b>No open design found.</b><br>Open the referenced design, then retry.'
            else:
                params = collect_top3_lengths(design)
                if len(params) < 3:
                    info = (f'<b>Only {len(params)} length parameter(s) found — need 3.</b><br>'
                            'Ensure the design has sketch dimensions, an extrude, and a fillet.')
                else:
                    info = (
                        '<b>Dim Annotate</b><br>'
                        'Places three AcDbRotatedDimension entities (L / W / T) on '
                        'layer <b>GF_Dimensions</b> (orange). Run again to refresh.<br><br>'
                        '<b>Auto-detected parameters:</b><br>'
                        f'&nbsp; Length&nbsp;&nbsp;&nbsp;: <b>{params[0][0]}</b>'
                        f' = {params[0][1]:.2f} mm<br>'
                        f'&nbsp; Width&nbsp;&nbsp;&nbsp;&nbsp;: <b>{params[1][0]}</b>'
                        f' = {params[1][1]:.2f} mm<br>'
                        f'&nbsp; Thickness: <b>{params[2][0]}</b>'
                        f' = {params[2][1]:.2f} mm'
                    )
            inputs.addTextBoxCommandInput('info', '', info, 9, True)
            h_ex = DAExecuteHandler()
            h_ds = DADestroyHandler()
            cmd.execute.add(h_ex)
            cmd.destroy.add(h_ds)
            handlers.extend([h_ex, h_ds])
        except Exception as e:
            if ui:
                ui.messageBox(f'Dim Annotate dialog error:\n{e}')


class DAExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        _pending['dims']['ok'] = True


class DADestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        if not _pending['dims'].pop('ok', False):
            return
        design = find_open_design()
        if not design:
            ui.messageBox('Dim Annotate: no open design.')
            return
        params = collect_top3_lengths(design)
        if len(params) < 3:
            ui.messageBox(f'Dim Annotate: need 3 length parameters, found {len(params)}.')
            return
        scr_path = _tmp('ghostforge_dims.scr')
        _build_dim_scr(params, scr_path)
        app.executeTextCommand(
            f'FusionDoc.ExecuteAcadCommand _.SCRIPT "{scr_path.replace(chr(92), "/")}"')
        debug_path = _tmp('gf_dim_debug.txt')
        ui.messageBox(
            f'Dim Annotate ran.\n\n'
            f'Length    : {params[0][0]} = {params[0][1]:.1f} mm\n'
            f'Width     : {params[1][0]} = {params[1][1]:.1f} mm\n'
            f'Thickness : {params[2][0]} = {params[2][1]:.1f} mm\n\n'
            f'Check drawing for dims on layer GF_Dimensions.\n'
            f'Debug log: {debug_path}')


# ─── DWG Probe ───────────────────────────────────────────────────────────────

_DUMP_PATH = os.path.join(DESKTOP, 'ghostforge_probe.txt').replace('\\', '/')

_LISP_PROBE = r'''
(defun _gf_probe (/ f ent data etype cnt pt)
  (setq f (open "{dump}" "w"))
  (defun _w (s) (write-line s f))
  (defun _g (code data / v) (setq v (cdr (assoc code data))) (if v (vl-princ-to-string v) "-"))

  (_w (strcat "SPACE: " (if (= (getvar "CVPORT") 1) "PAPER" "MODEL")))
  (_w (strcat "TILEMODE: " (itoa (getvar "TILEMODE"))))
  (_w "")
  (_w "=== ENTITIES ===")
  (setq ent (entnext) cnt 0)
  (while ent
    (setq data (vl-catch-all-apply (quote entget) (list ent)))
    (if (not (vl-catch-all-error-p data))
      (progn
        (setq etype (_g 0 data))
        (_w (strcat "TYPE=" etype "  HDL=" (_g 5 data) "  LYR=" (_g 8 data)))
        (cond
          ((= etype "DIMENSION")
           (_w (strcat "  val=" (_g 42 data) "  DTYPE=" (_g 70 data) "  STY=" (_g 3 data)))
           (setq pt (cdr (assoc 10 data)))
           (if pt (_w (strcat "  defpt=" (rtos (car pt) 2 3) "," (rtos (cadr pt) 2 3)))))
          ((= etype "DRAWINGVIEW")
           (_w "  (Fusion DRAWINGVIEW entity)"))
          ((= etype "INSERT")
           (_w (strcat "  BLOCK=" (_g 2 data))))
          ((= etype "VIEWPORT")
           (setq pt (cdr (assoc 10 data)))
           (if pt (_w (strcat "  CTR=" (rtos (car pt) 2 3) "," (rtos (cadr pt) 2 3)
                              "  W=" (rtos (cdr (assoc 40 data)) 2 3)))))
        )
        (setq cnt (1+ cnt))
      )
    )
    (setq ent (entnext ent))
  )
  (_w (strcat "TOTAL: " (itoa cnt)))
  (_w "=== DONE ===")
  (close f)
  (princ "\nGF_Probe complete.")
)
(_gf_probe)
'''

_PROBE_TRIALS = [
    ('InvokeDrawingCmdById FusionDrawingSingleDimensionCmd',
     'FusionDoc.InvokeDrawingCmdById FusionDrawingSingleDimensionCmd'),
    ('SetCursorPos 142 180',
     'FusionDoc.SetCursorPos 142 180'),
    ('Click',
     'FusionDoc.Click'),
    ('LeftClick',
     'FusionDoc.LeftClick'),
    ('PickAt 142 180',
     'FusionDoc.PickAt 142 180'),
    ('SendClick 142 180',
     'FusionDoc.SendClick 142 180'),
]


class DPCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd    = args.command
            inputs = cmd.commandInputs
            inputs.addTextBoxCommandInput(
                'info', '',
                '<b>DWG Probe</b><br>'
                'Dumps the DWG entity database via AutoLISP and probes '
                'InvokeDrawingCmdById + click simulation commands.<br><br>'
                'Output: <tt>Desktop/ghostforge_probe.txt</tt><br>'
                'Open a drawing with at least one view before running.',
                6, True)
            h_ex = DPExecuteHandler()
            h_ds = DPDestroyHandler()
            cmd.execute.add(h_ex)
            cmd.destroy.add(h_ds)
            handlers.extend([h_ex, h_ds])
        except Exception as e:
            if ui:
                ui.messageBox(f'DWG Probe dialog error:\n{e}')


class DPExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        _pending['probe']['ok'] = True


class DPDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        if not _pending['probe'].pop('ok', False):
            return
        results = []

        try:
            lsp_path = _tmp('ghostforge_probe.lsp')
            scr_path = _tmp('ghostforge_probe.scr')
            lsp = _LISP_PROBE.replace('{dump}', _DUMP_PATH)
            with open(lsp_path, 'w', encoding='ascii', errors='replace') as fh:
                fh.write(lsp)
            with open(scr_path, 'w', encoding='ascii', errors='replace') as fh:
                fh.write(f'(load "{lsp_path.replace(chr(92), "/")}")\n')
            app.executeTextCommand(
                f'FusionDoc.ExecuteAcadCommand _.SCRIPT "{scr_path.replace(chr(92), "/")}"')
            results.append('Dump → Desktop/ghostforge_probe.txt')
        except Exception as e:
            results.append(f'Dump ERR: {e}')

        results.append('')
        for label, cmd_str in _PROBE_TRIALS:
            try:
                r = app.executeTextCommand(cmd_str)
                results.append(f'OK  {label}')
                if r:
                    results.append(f'    → {r!r}')
            except Exception as e:
                results.append(f'ERR {label}')
                results.append(f'    → {e}')

        ui.messageBox('DWG Probe\n\n' + '\n'.join(results))


# ─── Panel management ─────────────────────────────────────────────────────────

_ALL_CMD_IDS = (CMD_SKETCH, CMD_DIMS, CMD_PROBE)


def _cleanup_ui():
    """Remove GhostForge tab (and everything in it) plus all command definitions."""
    try:
        ws  = ui.workspaces.itemById(WORKSPACE_ID)
        tab = ws.toolbarTabs.itemById(GF_TAB_ID)
        if tab:
            tab.deleteMe()
    except Exception:
        pass
    for cmd_id in _ALL_CMD_IDS:
        try:
            old = ui.commandDefinitions.itemById(cmd_id)
            if old:
                old.deleteMe()
        except Exception:
            pass


def _get_or_create_tab():
    ws  = ui.workspaces.itemById(WORKSPACE_ID)
    tab = ws.toolbarTabs.itemById(GF_TAB_ID)
    if tab is None:
        tab = ws.toolbarTabs.add(GF_TAB_ID, GF_TAB_NAME)
    return tab


def _get_or_create_panel(tab, panel_id, panel_name):
    p = tab.toolbarPanels.itemById(panel_id)
    if p is None:
        p = tab.toolbarPanels.add(panel_id, panel_name, '', False)
    return p


def _add_cmd(panel, cmd_id, name, tooltip, created_cls, resource_folder=''):
    cmd_def = ui.commandDefinitions.addButtonDefinition(
        cmd_id, name, tooltip, resource_folder)
    h = created_cls()
    cmd_def.commandCreated.add(h)
    handlers.append(h)
    ctrl = panel.controls.addCommand(cmd_def)
    ctrl.isPromoted         = True
    ctrl.isPromotedByDefault = True


# ─── Add-in lifecycle ─────────────────────────────────────────────────────────

def run(context):
    global app, ui
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface
        _cleanup_ui()

        tab = _get_or_create_tab()

        _add_cmd(
            _get_or_create_panel(tab, GF_SA_PANEL, 'Sketch'),
            CMD_SKETCH, 'Sketch Annotate',
            'Place all sketch dimensions and feature parameters as text on '
            'the active drawing sheet.',
            SACreatedHandler)

        _add_cmd(
            _get_or_create_panel(tab, GF_DA_PANEL, 'Dims'),
            CMD_DIMS, 'Dim Annotate',
            'Place three DIMENSION entities (length / width / thickness) derived '
            'from the open design. Run again after design changes to refresh.',
            DACreatedHandler,
            resource_folder=os.path.join(_RESOURCES, 'dim_icon'))

        _add_cmd(
            _get_or_create_panel(tab, GF_DP_PANEL, 'Probe'),
            CMD_PROBE, 'DWG Probe',
            'Dump DWG entity database and probe InvokeDrawingCmdById click '
            'simulation commands. Results -> Desktop/ghostforge_probe.txt.',
            DPCreatedHandler)

    except Exception as e:
        if ui:
            ui.messageBox(f'GhostForge run() error:\n{e}')


def stop(context):
    global handlers
    _cleanup_ui()
    handlers.clear()
