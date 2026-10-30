"""GhostForge — Fusion 360 drawing automation add-in.

Single panel in the Drawing workspace (FusionDocTab) containing:
  • Sketch Annotate  — places all sketch parameters as text
  • Dim Annotate     — places 3 AcDbRotatedDimension entities (L / W / T)
  • Dim Checklist    — feature-by-feature checklist for selective annotation
  • DWG Probe        — diagnostic entity dump + command trials (dev tool)
"""
import adsk.core, adsk.fusion, adsk.drawing, os, math, json, re

# ─── Globals ─────────────────────────────────────────────────────────────────
app      = None
ui       = None
handlers = []
_pending = {'sketch': {}, 'dims': {}, 'probe': {}, 'fc': {}}   # namespaced per command

# ─── Constants ───────────────────────────────────────────────────────────────
WORKSPACE_ID = 'FusionDocumentationEnvironment'
GF_TAB_ID    = 'GhostForge_Tab'
GF_TAB_NAME  = 'GhostForge'

# One panel per command so each shows as a standalone button in the tab
GF_SA_PANEL  = 'GhostForge_SA_Panel'
GF_DA_PANEL  = 'GhostForge_DA_Panel'
GF_FC_PANEL  = 'GhostForge_FC_Panel'
GF_DP_PANEL  = 'GhostForge_DP_Panel'

CMD_SKETCH   = 'GhostForge_SketchAnnotate'
CMD_DIMS     = 'GhostForge_DimAnnotate'
CMD_FC       = 'GhostForge_FeatureChecklist'
CMD_PROBE    = 'GhostForge_DwgProbe'

INTERNAL_TO_MM = 10.0
TEMP_DIR   = os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'Temp')
DESKTOP    = os.path.join(os.path.expanduser('~'), 'Desktop')
_RESOURCES  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources')
_STATE_DIR  = os.path.dirname(os.path.abspath(__file__))


def _state_file(drawing_doc=None):
    """Return the per-drawing state file path, or a generic fallback."""
    if drawing_doc:
        safe = re.sub(r'[^\w\-]', '_', drawing_doc.name)[:60]
        return os.path.join(_STATE_DIR, f'fc_state_{safe}.json')
    return os.path.join(_STATE_DIR, 'fc_state.json')


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


def _fillet_radius_mm(feat):
    """Extract radius in mm from a FilletFeature using multiple strategies.

    Strategy 1 (preferred): feat.edgeSets.item(j).radius — ModelParameter on a
    ConstantRadiusFilletEdgeSet; this is the documented Fusion API path.
    Strategy 2 (fallback): direct attributes on the feature object itself.
    """
    try:
        for j in range(feat.edgeSets.count):
            try:
                r = feat.edgeSets.item(j).radius
                if r is not None and hasattr(r, 'value') and r.value > 0:
                    return r.value * INTERNAL_TO_MM
            except Exception:
                pass
    except Exception:
        pass
    for attr in ('constantRadius', 'radius'):
        try:
            r = getattr(feat, attr)
            if r is not None and hasattr(r, 'value') and r.value > 0:
                return r.value * INTERNAL_TO_MM
        except Exception:
            pass
    return None


def get_fillet_radius(design):
    """Return the first fillet feature radius in mm, or None."""
    try:
        for comp in design.allComponents:
            try:
                ff = comp.features.filletFeatures
                for i in range(ff.count):
                    try:
                        r = _fillet_radius_mm(ff.item(i))
                        if r is not None:
                            return round(r, 4)
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass
    return None


# ─── Python Drawing API helpers ───────────────────────────────────────────────

DIM_GAP  = 10.0  # mm offset from view bbox to first dim line
DIM_STEP =  8.0  # mm between stacked dim lines (same view, same role)


def find_open_drawing():
    """Return the first open DrawingDocument, or None."""
    for i in range(app.documents.count):
        try:
            doc = app.documents.item(i)
            drawing = adsk.drawing.DrawingDocument.cast(doc)
            if drawing is not None:
                return drawing
        except Exception:
            continue
    return None


def _dp(x_mm, y_mm):
    return adsk.drawing.DrawingPoint_create(adsk.core.Point2D.create(x_mm, y_mm))


def _view_bbox(view):
    """Return (x0, y0, x1, y1) in mm paper-space from view curves, or None."""
    xs, ys = [], []
    for ci in range(view.viewCurves.count):
        c = view.viewCurves.item(ci)
        for pt in (c.startPoint, c.endPoint):
            try:
                co = pt.coordinate
                xs.append(co.x)
                ys.append(co.y)
            except Exception:
                pass
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def _clear_dims(drawing_doc):
    """Delete all DrawingDimension objects on the active sheet."""
    sheet   = drawing_doc.drawing.activeSheet
    drawing = drawing_doc.drawing
    coll    = adsk.core.ObjectCollection.create()
    dims    = sheet.drawingDimensions
    for i in range(dims.count):
        coll.add(dims.item(i))
    if coll.count > 0:
        drawing.deleteEntities(coll)


_ORI_NAMES = {0: 'Front', 1: 'Back', 2: 'Top', 3: 'Bottom',
              4: 'Right', 5: 'Left', 6: 'IsoTR', 7: 'IsoTL', 8: 'IsoBR', 9: 'IsoBL'}


def _enumerate_views(drawing_doc):
    """Return list of view_info dicts for views eligible for dim placement."""
    sheet  = drawing_doc.drawing.activeSheet
    result = []
    for i in range(sheet.views.count):
        v  = sheet.views.item(i)
        pass  # all view types included (Section=2, Detail=3 both have usable geometry)
        bb = _view_bbox(v)
        if bb is None:
            continue
        x0, y0, x1, y1 = bb
        ori   = None
        scale = 1.0
        try:
            ori = v.orientation
        except Exception:
            pass
        try:
            scale = float(v.scale)
        except Exception:
            try:
                scale = float(v.scaleValue)
            except Exception:
                pass
        scale_str = f'{scale:.4g}' if scale != 1.0 else '1:1'
        if v.type == 0 and ori is not None and ori in _ORI_NAMES:
            label = f'{_ORI_NAMES[ori]} (Base)'
        elif v.type == 0:
            label = f'Base {i}'
        elif v.type == 2:
            label = f'Section {i} ({scale_str})'
        elif v.type == 3:
            label = f'Detail {i} ({scale_str})'
        else:
            label = f'View {i} ({x1-x0:.0f}×{y1-y0:.0f}mm)'
        result.append({
            'idx': i, 'view': v, 'bbox': bb,
            'cx': (x0 + x1) / 2, 'cy': (y0 + y1) / 2,
            'w': x1 - x0, 'h': y1 - y0, 'label': label,
            'scale': scale,
        })
    return result


def _extreme_view_pts(view):
    """Return the extreme ViewCurve startPoint/endPoint objects (actual curve points).

    addLinearDimension requires start/end to be on view curves — DrawingPoint_create()
    coordinates are rejected. We find the extreme curve endpoints in each direction.
    Returns dict with keys min_x, max_x, min_y, max_y → DrawingPoint or None.
    """
    best = {'min_x': None, 'max_x': None, 'min_y': None, 'max_y': None}
    vals = {'min_x': float('inf'), 'max_x': float('-inf'),
            'min_y': float('inf'), 'max_y': float('-inf')}
    for ci in range(view.viewCurves.count):
        c = view.viewCurves.item(ci)
        for pt in (c.startPoint, c.endPoint):
            try:
                co = pt.coordinate
                if co.x < vals['min_x']:
                    vals['min_x'] = co.x; best['min_x'] = pt
                if co.x > vals['max_x']:
                    vals['max_x'] = co.x; best['max_x'] = pt
                if co.y < vals['min_y']:
                    vals['min_y'] = co.y; best['min_y'] = pt
                if co.y > vals['max_y']:
                    vals['max_y'] = co.y; best['max_y'] = pt
            except Exception:
                pass
    return best


def _guess_dim_orient(raw_type, dim_obj):
    """Return 'H' or 'V' by inspecting sketch geometry. Defaults to 'H'."""
    try:
        if raw_type == 'SketchLinearDimension':
            sp = dim_obj.startSketchPoint.geometry
            ep = dim_obj.endSketchPoint.geometry
            return 'H' if abs(ep.x - sp.x) >= abs(ep.y - sp.y) else 'V'
        elif raw_type == 'SketchOffsetDimension':
            ent = dim_obj.entityOne
            sp  = ent.startSketchPoint.geometry
            ep  = ent.endSketchPoint.geometry
            dx  = abs(ep.x - sp.x)
            dy  = abs(ep.y - sp.y)
            # Offset is perpendicular to the entity: horizontal entity → vertical offset
            return 'V' if dx >= dy else 'H'
    except Exception:
        pass
    return 'H'


def _search_linear_pts(view, target_paper_mm, orient='H', tol=0.005):
    """Return the ViewCurve endpoint pair whose orient-axis separation best matches
    target_paper_mm (minimum error), subject to:
      - error < tol (paper-space tolerance, default 0.005 mm)
      - perpendicular separation < 2.0 mm (endpoints must be nearly aligned)
    Arc/circle CENTER POINTS are included so dims to hole centers can be found.
    Returns (None, None) if no qualifying pair is found.
    """
    pts = []
    for ci in range(view.viewCurves.count):
        c = view.viewCurves.item(ci)
        for pt in (c.startPoint, c.endPoint):
            try:
                co = pt.coordinate
                pts.append((co.x, co.y, pt))
            except Exception:
                pass
        # Include center of arcs/circles — enables dims-to-hole-center
        try:
            cp = c.centerPoint
            co = cp.coordinate
            pts.append((co.x, co.y, cp))
        except Exception:
            pass

    best_pti, best_ptj, best_err = None, None, tol

    # Two-pass: tight perp first (avoids false matches), then slightly looser.
    # _verify_placed_dim acts as the safety net for any false positives.
    for perp_limit in (2.0, 3.0):
        for i in range(len(pts)):
            xi, yi, pti = pts[i]
            for j in range(i + 1, len(pts)):
                xj, yj, ptj = pts[j]
                dx, dy = abs(xj - xi), abs(yj - yi)
                main = dx if orient == 'H' else dy
                perp = dy if orient == 'H' else dx
                err  = abs(main - target_paper_mm)
                if err < best_err and perp <= perp_limit:
                    best_err = err
                    best_pti = pti
                    best_ptj = ptj
        if best_pti is not None:
            break  # found in this pass; skip looser pass

    return best_pti, best_ptj


def _verify_placed_dim(placed, expected_mm, tol=0.5):
    """Check the placed dim's displayed text against the expected model value.

    Fusion shows the TRUE 3D distance, which can differ from the paper-space
    distance we searched for when the endpoints have depth differences.  If the
    text can be parsed and its value is farther than tol from expected_mm, the
    dim is deleted and None is returned.  If the text is absent or unparseable
    the dim is kept (benefit of the doubt).
    """
    try:
        t = placed.text
        if t:
            m = re.search(r'(\d+(?:[.,]\d+)?)', t)
            if m:
                val = float(m.group(1).replace(',', '.'))
                # Text is in document units (usually mm).
                # Accept if within tol; also try cm (val × 10) as fallback.
                if abs(val - expected_mm) <= tol:
                    return placed
                if abs(val * 10.0 - expected_mm) <= tol:
                    return placed
                placed.deleteMe()
                return None
    except Exception:
        pass
    return placed  # can't verify — keep


def _place_linear_dim(sheet, view_info, model_value_mm, orient='H', slot=0):
    """Search the view for geometry matching model_value_mm and place a linear dim.

    orient 'H' → expects a horizontal pair, places text above the view bbox.
    orient 'V' → expects a vertical pair, places text left of the view bbox.
    slot staggers successive dims of the same orient in the same view.
    Returns the placed DrawingDimension object, or None on failure.
    """
    x0, y0, x1, y1 = view_info['bbox']
    scale  = view_info.get('scale', 1.0) or 1.0
    dims   = sheet.drawingDimensions
    target = model_value_mm * scale

    pt1, pt2 = _search_linear_pts(view_info['view'], target, orient)
    if pt1 is None:
        return None

    co1 = pt1.coordinate
    co2 = pt2.coordinate
    mx  = (co1.x + co2.x) / 2
    my  = (co1.y + co2.y) / 2

    if orient == 'H':
        place = _dp(mx, y1 + DIM_GAP + slot * DIM_STEP)
    else:
        place = _dp(x0 - DIM_GAP - slot * DIM_STEP, my)

    before = dims.count
    dims.addLinearDimension(pt1, pt2, place)
    if dims.count > before:
        return _verify_placed_dim(dims.item(dims.count - 1), model_value_mm)
    return None


def _arc_scale(view_info):
    return view_info.get('scale', 1.0) or 1.0


def _arc_info(c, scale=1.0):
    """Return (cx, cy, r) all in paper-space mm for a ViewCurve arc/circle, or None.

    Approach 1: centerPoint + startPoint — distance(center, start) = radius (paper mm).
    Approach 2: centerPoint + endPoint  — fallback when startPoint throws (closed circle).
    Approach 3: centerPoint + geometry.radius — for Circle type=2 curves where
                start/end are inaccessible. c.geometry.radius is model-space cm;
                multiply by 10*scale to reach paper-space mm.
    Note: approach 3 was previously a chord-midpoint circumscribed circle formula
          which was mathematically wrong (chord midpoint ≠ arc midpoint).
    """
    # Approach 1: center + start (works for Arc type=1 and some Circle type=2)
    try:
        cp = c.centerPoint.coordinate
        sp = c.startPoint.coordinate
        r  = math.hypot(sp.x - cp.x, sp.y - cp.y)
        if r > 0.01:
            return cp.x, cp.y, r
    except Exception:
        pass

    # Approach 2: center + end (closed circles often have no distinct startPoint)
    try:
        cp = c.centerPoint.coordinate
        ep = c.endPoint.coordinate
        r  = math.hypot(ep.x - cp.x, ep.y - cp.y)
        if r > 0.01:
            return cp.x, cp.y, r
    except Exception:
        pass

    # Approach 3: model-space geometry radius for complete circles (Circle type=2)
    # where start/end are not accessible. c.geometry is a Circle3D or Arc3D in
    # model-space (cm); .radius gives the model-space radius. Converting:
    #   paper_mm = model_cm * 10.0 * scale
    try:
        cp   = c.centerPoint.coordinate   # paper-space center (mm) — must exist
        r_cm = c.geometry.radius          # model-space radius (cm)
        r    = r_cm * 10.0 * scale        # → paper-space mm
        if r > 0.01:
            return cp.x, cp.y, r
    except Exception:
        pass

    return None


def _place_radial_dim(sheet, view_info, target_r_mm):
    """Find arc/circle in view closest to target_r_mm and place a radial dim.
    Uses best-match search (0.1 mm tolerance on radius).
    Returns the placed DrawingDimension object, or None on failure.
    """
    v     = view_info['view']
    dims  = sheet.drawingDimensions
    scale = _arc_scale(view_info)
    best_err, best_c, best_cx, best_cy, best_r = 0.1, None, None, None, None
    for ci in range(v.viewCurves.count):
        c   = v.viewCurves.item(ci)
        inf = _arc_info(c, scale)
        if inf is None:
            continue
        cx, cy, r = inf
        err = abs(r - target_r_mm * scale)
        if err < best_err:
            best_err, best_c, best_cx, best_cy, best_r = err, c, cx, cy, r
    if best_c is None:
        return None
    before = dims.count
    dims.addRadialDimension(best_c, _dp(best_cx + best_r + DIM_GAP, best_cy))
    if dims.count > before:
        return _verify_placed_dim(dims.item(dims.count - 1), target_r_mm)
    return None


def _place_diameter_dim(sheet, view_info, target_d_mm):
    """Find arc/circle matching diameter target_d_mm and place Ø dim.

    Best-match arc search (0.1 mm radius tolerance).  If no arc is found,
    falls back to a linear search — the diameter can appear as two parallel
    lines when the cylinder is viewed from the side.  In that case the text
    is overridden to include the Ø prefix.
    Returns the placed DrawingDimension object, or None on failure.
    """
    v           = view_info['view']
    dims        = sheet.drawingDimensions
    scale       = _arc_scale(view_info)
    target_r_mm = target_d_mm / 2.0
    best_err, best_c, best_cx, best_cy, best_r = 0.1, None, None, None, None
    for ci in range(v.viewCurves.count):
        c   = v.viewCurves.item(ci)
        inf = _arc_info(c, scale)
        if inf is None:
            continue
        cx, cy, r = inf
        err = abs(r - target_r_mm * scale)
        if err < best_err:
            best_err, best_c, best_cx, best_cy, best_r = err, c, cx, cy, r
    if best_c is not None:
        before = dims.count
        dims.addDiameterDimension(best_c, _dp(best_cx + best_r + DIM_GAP, best_cy))
        if dims.count > before:
            return _verify_placed_dim(dims.item(dims.count - 1), target_d_mm)
    # Fallback: diameter appears as two parallel lines (cylinder side view)
    placed = _place_linear_dim(sheet, view_info, target_d_mm, orient='H')
    if placed is not None:
        try:
            placed.text = f'Ø{target_d_mm:.2f}'  # add Ø prefix
        except Exception:
            pass
    return placed


def collect_dim_list(design):
    """Return ALL annotatable dimensions from the model (sketch dims, extrudes, fillets)."""
    result  = []
    seen    = set()

    for comp in design.allComponents:
        for sketch in comp.sketches:
            for dim in sketch.sketchDimensions:
                try:
                    p = dim.parameter
                    if p is None or p.name in seen:
                        continue
                    unit = (p.unit or '').strip()
                    if unit in ('', 'ul'):
                        continue
                    if unit == 'deg':
                        val = math.degrees(p.value)
                    else:
                        val = p.value * INTERNAL_TO_MM
                    if val <= 0:
                        continue
                    seen.add(p.name)
                    raw      = dim.classType().split('::')[-1]
                    dim_type = _TYPE_MAP.get(raw, 'Lin')
                    if raw == 'SketchRadialDimension':
                        entry_type  = 'radial'
                        entry_role  = 'R'
                        entry_str   = f'R{val:.2f} mm'
                    elif raw == 'SketchDiameterDimension':
                        entry_type  = 'diameter'
                        entry_role  = 'D'
                        entry_str   = f'Ø{val:.2f} mm'   # Ø prefix
                    elif unit == 'deg':
                        entry_type  = 'angular'
                        entry_role  = 'A'
                        entry_str   = f'{val:.2f}°'
                    else:
                        entry_type  = 'linear'
                        entry_role  = 'W'
                        entry_str   = f'{val:.2f} mm'
                    orient = _guess_dim_orient(raw, dim) if entry_type == 'linear' else None
                    result.append({
                        'id':         p.name,
                        'type':       entry_type,
                        'role':       entry_role,
                        'orient':     orient,
                        'label':      f'{p.name}  [{sketch.name} · {dim_type}]',
                        'params_str': entry_str,
                        'value':      val,
                    })
                except Exception:
                    continue

    for comp in design.allComponents:
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
                                    result.append({
                                        'id':         p.name,
                                        'type':       'linear',
                                        'role':       'L',
                                        'orient':     'V',
                                        'label':      f'{p.name}  [Extrude · {ef.name}]',
                                        'params_str': f'{val:.2f} mm',
                                        'value':      val,
                                    })
                    except Exception:
                        pass
            except Exception:
                pass

    for comp in design.allComponents:
        try:
            for i in range(comp.features.filletFeatures.count):
                feat = comp.features.filletFeatures.item(i)
                r    = _fillet_radius_mm(feat)
                if r is None:
                    continue
                fid = f'R_{feat.name}'
                if fid in seen:
                    continue
                seen.add(fid)
                edge_count = 0
                try:
                    for j in range(feat.edgeSets.count):
                        edge_count += feat.edgeSets.item(j).edges.count
                except Exception:
                    pass
                prefix = f'{edge_count}x ' if edge_count > 1 else ''
                result.append({
                    'id':         fid,
                    'type':       'radial',
                    'role':       'R',
                    'label':      f'Fillet ({feat.name})',
                    'params_str': f'{prefix}R{r:.2f} mm',
                    'value':      r,
                    'edge_count': edge_count,
                })
        except Exception:
            pass

    # Revolve features — collect rotation angle
    for comp in design.allComponents:
        try:
            for i in range(comp.features.revolveFeatures.count):
                try:
                    rf  = comp.features.revolveFeatures.item(i)
                    ext = adsk.fusion.AngleExtentDefinition.cast(rf.extentOne)
                    if ext:
                        p = ext.angle
                        if p and p.name not in seen:
                            val = math.degrees(p.value)
                            if val > 0:
                                seen.add(p.name)
                                result.append({
                                    'id':         p.name,
                                    'type':       'angular',
                                    'role':       'A',
                                    'orient':     None,
                                    'label':      f'{p.name}  [Revolve · {rf.name}]',
                                    'params_str': f'{val:.2f}°',
                                    'value':      val,
                                })
                except Exception:
                    pass
        except Exception:
            pass

    return result


def collect_feature_list(design):
    """Return annotatable features with sketch associations for the checklist dialog."""
    result = []
    params = collect_top3_lengths(design)
    param_str = (f'L={params[0][1]:.1f}, W={params[1][1]:.1f}, T={params[2][1]:.1f} mm'
                 if len(params) >= 3 else '?')

    for comp in design.allComponents:
        try:
            for i in range(comp.features.extrudeFeatures.count):
                feat = comp.features.extrudeFeatures.item(i)
                sketch_name = None
                try:
                    sketch_name = feat.profile.parentSketch.name
                except Exception:
                    pass
                label = (f'{feat.name}  ({sketch_name})' if sketch_name else feat.name)
                result.append({'id': f'extrude_{i}', 'type': 'extrude',
                               'label': label, 'params_str': param_str})
        except Exception:
            pass

        try:
            for i in range(comp.features.filletFeatures.count):
                feat = comp.features.filletFeatures.item(i)
                r_raw = _fillet_radius_mm(feat)
                r = round(r_raw, 1) if r_raw is not None else None
                result.append({'id': f'fillet_{i}', 'type': 'fillet',
                               'label': feat.name,
                               'params_str': f'R{r:.1f} mm' if r is not None else 'R?'})
        except Exception:
            pass

    return result


def _diag_design(design):
    """Return a short diagnostic string for debugging parameter collection."""
    try:
        n_comp   = design.allComponents.count
        n_sketch = sum(c.sketches.count for c in design.allComponents)
        n_dims   = sum(s.sketchDimensions.count
                       for c in design.allComponents for s in c.sketches)
        try:
            n_params = design.allParameters.count
        except Exception:
            n_params = '?'
        return (f'design="{design.parentDocument.name}"  '
                f'components={n_comp}  sketches={n_sketch}  '
                f'sketchDims={n_dims}  allParams={n_params}')
    except Exception as e:
        return f'diag failed: {e}'


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
(defun _gf_dim (/ _mkdim _dbg _fixdim _de _dd _emr
                   ent data next_ent views front side lv
                   fx fy fscale fw fh sx sy sscale sw sh
                   f_left f_right f_top f_bot s_left s_right s_top
                   dim_gap layout r1 r2 r3
                   g40s g10s pair _style _dbgf
                   _ae _ae_found _ad _ar _acx _acy _adx _ady _adl _apx _apy)

  ; ── Setup ──────────────────────────────────────────────────────────────
  ; NOTE: (getvar "CTAB") and (getvar "DIMSTYLE") return boolean T in
  ;       Fusion's LISP — NOT strings.  Get layout from DRAWINGVIEW g410.
  (setq v_len           {v_len}
        v_wid           {v_wid}
        v_thk           {v_thk}
        v_fillet        {v_fillet}
        v_place_extrude {v_place_extrude}
        v_place_fillet  {v_place_fillet}
        dim_gap         10.0
        layout          "Sheet1")

  ; ── Debug helpers ───────────────────────────────────────────────────────
  (setq _dbgf (open "{debug_path}" "w"))
  (defun _dbg (s) (write-line s _dbgf))

  ; Fix groups 10 (dim-line definition point), 13/14 (ext line origins), 42
  ; (measurement) on the last-placed dimension so snap errors don't matter.
  ; Group 10 must be consistent with group 14 or AutoCAD suppresses ext line 2:
  ;   vertical dim   → g10 = (dimline_x, o2.y, 0)
  ;   horizontal dim → g10 = (o2.x, dimline_y, 0)
  (defun _fixdim (o1 o2 g10 mval)
    (setq _de (entlast))
    (setq _dd (entget _de))
    (if _dd
      (progn
        (if (assoc 10 _dd) (setq _dd (subst (cons 10 g10) (assoc 10 _dd) _dd)))
        (if (assoc 13 _dd) (setq _dd (subst (cons 13 o1) (assoc 13 _dd) _dd)))
        (if (assoc 14 _dd) (setq _dd (subst (cons 14 o2) (assoc 14 _dd) _dd)))
        (if (assoc 42 _dd) (setq _dd (subst (cons 42 (float mval)) (assoc 42 _dd) _dd)))
        (setq _emr (entmod _dd))
        (entupd _de)
        (_dbg (strcat "  _fixdim ok=" (vl-princ-to-string (not (not _emr)))
                      " g10=" (vl-princ-to-string g10)
                      " val=" (rtos mval 2 3)))
      )
      (_dbg "  _fixdim: entget nil")
    )
  )

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
    (entmake
      (list (cons 0 "DIMENSION") (cons 8 "0")
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

          ; Pick extension line origins 0.5mm OUTSIDE the view bounding box.
          ; No drawing geometry exists beyond the bbox, so Fusion snap has
          ; nothing to grab — exact coordinates are used.
          ; _T text override ensures model value regardless of measured distance.

          ; Force both extension lines on before any DIMLINEAR — Dim-ISO-25
          ; may have DIMSE1 or DIMSE2 set which would suppress one silently.
          (if v_place_extrude
            (progn
              (_dbg (strcat "DIMSE1=" (vl-princ-to-string (getvar "DIMSE1"))
                            "  DIMSE2=" (vl-princ-to-string (getvar "DIMSE2"))))
              (setvar "DIMSE1" 0)
              (setvar "DIMSE2" 0)
              (_dbg (strcat "after setvar: DIMSE1=" (vl-princ-to-string (getvar "DIMSE1"))
                            "  DIMSE2=" (vl-princ-to-string (getvar "DIMSE2"))))

              ; Length — vertical; initial picks establish vertical orientation.
              ; _fixdim: g10 = (dimline_x, o2.y, 0) keeps ext line 2 horizontal.
              (command "_.DIMLINEAR"
                (list (- f_left 0.5) f_top 0.0)
                (list (- f_left 0.5) f_bot 0.0)
                (list (- f_left dim_gap) fy 0.0))
              (_dbg "DIMLINEAR length placed")
              (_fixdim (list f_left f_top 0.0) (list f_left f_bot 0.0)
                       (list (- f_left dim_gap) f_bot 0.0) v_len)

              ; Width — horizontal; g10 = (o2.x, dimline_y, 0).
              (command "_.DIMLINEAR"
                (list f_left (+ f_top 0.5) 0.0)
                (list f_right (+ f_top 0.5) 0.0)
                (list fx (+ f_top dim_gap) 0.0))
              (_dbg "DIMLINEAR width placed")
              (_fixdim (list f_left f_top 0.0) (list f_right f_top 0.0)
                       (list f_right (+ f_top dim_gap) 0.0) v_wid)

              ; Thickness — horizontal; side view top edge.
              (command "_.DIMLINEAR"
                (list s_left (+ s_top 0.5) 0.0)
                (list s_right (+ s_top 0.5) 0.0)
                (list sx (+ s_top dim_gap) 0.0))
              (_dbg "DIMLINEAR thickness placed")
              (_fixdim (list s_left s_top 0.0) (list s_right s_top 0.0)
                       (list s_right (+ s_top dim_gap) 0.0) v_thk)
            )
          )

          ; ── Fillet radius dimension ─────────────────────────────────────
          ; Arc geometry lives inside DRAWINGVIEW block defs — unreachable by
          ; (entnext). Compute the top-right corner arc position from view geometry.
          (if (and v_fillet v_place_fillet)
            (progn
              ; Top-right corner: arc centre is R inside the bbox corner
              (setq _acx (- f_right (* v_fillet fscale))
                    _acy (- f_top   (* v_fillet fscale))
                    _ar  (* v_fillet fscale))
              ; Pick at 45° from corner — point lies exactly on the arc surface
              (setq _adx 0.7071  _ady 0.7071)
              (setq _apx (+ _acx (* _ar _adx))
                    _apy (+ _acy (* _ar _ady)))
              (_dbg (strcat "DIMRADIUS pick=(" (rtos _apx 2 2) "," (rtos _apy 2 2)
                            ") ctr=(" (rtos _acx 2 2) "," (rtos _acy 2 2) ")"))
              (setvar "DIMSE1" 0) (setvar "DIMSE2" 0)
              (command "_.DIMRADIUS"
                (list _apx _apy 0.0)
                (list (+ _acx (* (+ _ar dim_gap) _adx))
                      (+ _acy (* (+ _ar dim_gap) _ady))
                      0.0))
              (_dbg "DIMRADIUS fired")
            )
          )

          (command "_.REGEN")
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


def _build_dim_scr(params, fillet_r, scr_path, place_extrude=True, place_fillet=True):
    debug_path = _tmp('gf_dim_debug.txt').replace('\\', '/')
    v_fillet_str = f'{fillet_r:.4f}' if fillet_r is not None else 'nil'
    lsp = _LISP_DIM.format(
        v_len=f'{params[0][1]:.4f}',
        v_wid=f'{params[1][1]:.4f}',
        v_thk=f'{params[2][1]:.4f}',
        v_fillet=v_fillet_str,
        v_place_extrude='T' if place_extrude else 'nil',
        v_place_fillet='T'  if place_fillet  else 'nil',
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
                fillet_r = get_fillet_radius(design)
                if len(params) < 3:
                    info = (f'<b>Only {len(params)} length parameter(s) found — need 3.</b><br>'
                            'Ensure the design has sketch dimensions, an extrude, and a fillet.<br><br>'
                            f'<small>{_diag_design(design)}</small>')
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
        design      = find_open_design()
        drawing_doc = find_open_drawing()
        if not design:
            ui.messageBox('Dim Annotate: no open design.')
            return
        params = collect_top3_lengths(design)
        if len(params) < 3:
            ui.messageBox(f'Dim Annotate: need 3 length parameters, found {len(params)}.\n'
                          f'{_diag_design(design)}')
            return
        fillet_r = get_fillet_radius(design)

        if not drawing_doc:
            # LISP fallback
            scr_path = _tmp('ghostforge_dims.scr')
            _build_dim_scr(params, fillet_r, scr_path)
            app.executeTextCommand(
                f'FusionDoc.ExecuteAcadCommand _.SCRIPT "{scr_path.replace(chr(92), "/")}"')
            ui.messageBox('Dim Annotate (LISP fallback — no drawing document found).\n'
                          f'Debug log: {_tmp("gf_dim_debug.txt")}')
            return

        views = _enumerate_views(drawing_doc)
        if not views:
            ui.messageBox('Dim Annotate: no eligible views on active sheet.')
            return

        _clear_dims(drawing_doc)
        sheet    = drawing_doc.drawing.activeSheet
        dims_lst = collect_dim_list(design)
        placed   = []
        for dim in dims_lst:
            vi = views[0]
            try:
                if dim['type'] == 'linear':
                    _place_linear_dim(sheet, dim['role'], vi)
                    placed.append(dim['label'])
                elif dim['type'] == 'radial' and fillet_r is not None:
                    if _place_radial_dim(sheet, vi, dim['value']):
                        placed.append(dim['label'])
            except Exception as e:
                placed.append(f"{dim['label']} ERR: {e}")

        fillet_line = f'Fillet    : R{fillet_r:.1f} mm\n' if fillet_r is not None else 'Fillet    : not found\n'
        ui.messageBox(
            f'Dim Annotate complete.\n\n'
            f'Length    : {params[0][0]} = {params[0][1]:.1f} mm\n'
            f'Width     : {params[1][0]} = {params[1][1]:.1f} mm\n'
            f'Thickness : {params[2][0]} = {params[2][1]:.1f} mm\n'
            f'{fillet_line}'
            f'\nPlaced: {len(placed)} dim(s)')


# ─── Dim Checklist ───────────────────────────────────────────────────────────

def _load_fc_state(drawing_doc=None):
    """Return (placements, statuses) dicts from the per-drawing JSON state file."""
    try:
        with open(_state_file(drawing_doc), 'r') as f:
            data = json.load(f)
            return data.get('placements', {}), data.get('statuses', {})
    except Exception:
        return {}, {}


def _save_fc_state(dims_list, placements_by_idx, views, statuses, drawing_doc=None):
    """Persist placements and statuses keyed by view label for cross-session stability."""
    by_label = {}
    for dim in dims_list:
        v_idx = placements_by_idx.get(dim['id'])
        if v_idx is not None and v_idx < len(views):
            by_label[dim['id']] = views[v_idx]['label']
        else:
            by_label[dim['id']] = None
    try:
        with open(_state_file(drawing_doc), 'w') as f:
            json.dump({'placements': by_label, 'statuses': statuses}, f, indent=2)
    except Exception:
        pass


def _place_dims_from_placements(sheet, dims_list, placements, views):
    """Clear all dims and re-place every assigned dim.
    Returns {dim_id: True/False} for each dim that had a view assigned."""
    results = {}
    slots   = {}
    for dim in dims_list:
        v_idx = placements.get(dim['id'])
        if v_idx is None or v_idx >= len(views):
            continue
        vi = views[v_idx]
        try:
            if dim['type'] == 'linear':
                orient = dim.get('orient') or 'H'
                key    = (v_idx, orient)
                slot   = slots.get(key, 0)
                slots[key] = slot + 1
                ok = _place_linear_dim(sheet, vi, dim['value'], orient=orient, slot=slot)
            elif dim['type'] == 'radial':
                ok = bool(_place_radial_dim(sheet, vi, dim['value']))
            elif dim['type'] == 'diameter':
                ok = bool(_place_diameter_dim(sheet, vi, dim['value']))
            else:
                ok = False
            results[dim['id']] = ok
        except Exception:
            results[dim['id']] = False
    return results


def _write_place_debug(dim, view_info, sheet):
    """Write a diagnostic entry to Desktop/ghostforge_place_debug.txt when placement fails.

    Logs how many points were found in the view, the target distance, and the
    closest matching pair (so we can see if the issue is perp or tolerance).
    """
    try:
        v      = view_info['view']
        scale  = view_info.get('scale', 1.0) or 1.0
        orient = dim.get('orient') or 'H'
        target = dim['value'] * scale
        lines  = [
            f'=== PLACE FAIL: {dim["label"]}  value={dim["value"]:.3f}mm  '
            f'type={dim["type"]}  orient={orient} ===',
            f'  view: {view_info["label"]}  type={v.type}  scale={scale:.4g}  '
            f'target={target:.3f}mm  curves={v.viewCurves.count}',
        ]
        pts = []
        for ci in range(v.viewCurves.count):
            c = v.viewCurves.item(ci)
            for pt in (c.startPoint, c.endPoint):
                try:
                    co = pt.coordinate
                    pts.append((co.x, co.y))
                except Exception:
                    pass
            try:
                co = c.centerPoint.coordinate
                pts.append((co.x, co.y))
            except Exception:
                pass
        lines.append(f'  pts_collected={len(pts)}')
        if pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            lines.append(f'  pts_bbox: x={min(xs):.2f}..{max(xs):.2f} '
                         f'y={min(ys):.2f}..{max(ys):.2f}')
        best_err, best_perp = float('inf'), float('inf')
        for i in range(len(pts)):
            xi, yi = pts[i]
            for j in range(i + 1, len(pts)):
                xj, yj = pts[j]
                dx, dy = abs(xj - xi), abs(yj - yi)
                main = dx if orient == 'H' else dy
                perp = dy if orient == 'H' else dx
                err = abs(main - target)
                if err < best_err:
                    best_err, best_perp = err, perp
        if best_err < float('inf'):
            lines.append(f'  closest_pair: err={best_err:.4f}mm  perp={best_perp:.2f}mm'
                         f'  (need err<0.005 AND perp<=3.0)')
        else:
            lines.append('  no point pairs (pts list empty)')
        path = os.path.join(DESKTOP, 'ghostforge_place_debug.txt')
        with open(path, 'a', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n\n')
    except Exception:
        pass


class FCCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd    = args.command
            inputs = cmd.commandInputs
            try:
                cmd.isAutoTerminate = False
            except Exception:
                pass

            design      = find_open_design()
            drawing_doc = find_open_drawing()
            if not design:
                inputs.addTextBoxCommandInput(
                    'fc_info', '',
                    '<b>No open design.</b><br>Open the referenced design first.',
                    2, True)
                return

            dims_list  = collect_dim_list(design)
            views      = _enumerate_views(drawing_doc) if drawing_doc else []
            saved_placements, saved_statuses = _load_fc_state(drawing_doc)

            # Resolve saved view labels → current view indices
            label_to_idx = {v['label']: j for j, v in enumerate(views)}
            placements = {}  # {dim_id: view_idx or None}
            for dim in dims_list:
                lbl = saved_placements.get(dim['id'])
                placements[dim['id']] = label_to_idx.get(lbl)  # None if label gone

            _pending['fc']['dims_list']  = dims_list
            _pending['fc']['views']      = views
            _pending['fc']['placements'] = placements
            _pending['fc']['statuses']   = dict(saved_statuses)

            no_drawing = '' if drawing_doc else '<br><i>No drawing open — Place buttons will be inactive.</i>'
            inputs.addTextBoxCommandInput(
                'fc_desc', '',
                '<b>Dim Checklist</b><br>'
                'Select a view for each dimension, then click <b>Place</b>. '
                'Click <b>Place</b> again after changing the view to move the dim. '
                'State is saved between sessions.' + no_drawing,
                4, True)

            # 5 columns: name | value | view | place-btn | status
            table = inputs.addTableCommandInput('fc_table', 'Dimensions', 5, '4:2:4:1:3')
            table.hasGrid = True
            table.maximumVisibleRows = 12
            table.minimumVisibleRows = min(max(len(dims_list) + 1, 2), 6)

            def _hdr(col, text):
                h = inputs.addTextBoxCommandInput(f'fch_{col}', '', f'<b>{text}</b>', 1, True)
                table.addCommandInput(h, 0, col)

            _hdr(0, 'Dimension')
            _hdr(1, 'Value')
            _hdr(2, 'View')
            _hdr(3, '')
            _hdr(4, 'Status')

            view_labels = ['(skip)'] + [v['label'] for v in views]

            for i, dim in enumerate(dims_list):
                row = i + 1

                n  = inputs.addTextBoxCommandInput(f'fc_name_{i}', '', dim['label'],      1, True)
                p  = inputs.addTextBoxCommandInput(f'fc_val_{i}',  '', dim['params_str'], 1, True)
                dd = inputs.addDropDownCommandInput(
                    f'fc_view_{i}', '', adsk.core.DropDownStyles.TextListDropDownStyle)

                saved_idx = placements.get(dim['id'])  # view index or None
                for j, lbl in enumerate(view_labels):
                    # j=0 is "(skip)", j=1+ maps to views[j-1]
                    if saved_idx is not None:
                        sel = (j == saved_idx + 1)
                    else:
                        sel = (j == 0)
                    dd.listItems.add(lbl, bool(sel))

                # isCheckBox=True so the value resets False→True→False each click;
                # isCheckBox=False sticks at True and won't re-fire inputChanged.
                btn = inputs.addBoolValueInput(f'fc_place_{i}', 'Place', True, '', False)

                status_text = saved_statuses.get(dim['id'], '—')
                st = inputs.addTextBoxCommandInput(f'fc_status_{i}', '', status_text, 1, True)

                table.addCommandInput(n,   row, 0)
                table.addCommandInput(p,   row, 1)
                table.addCommandInput(dd,  row, 2)
                table.addCommandInput(btn, row, 3)
                table.addCommandInput(st,  row, 4)

            h_ic = FCInputChangedHandler()
            h_ex = FCExecuteHandler()
            h_ds = FCDestroyHandler()
            cmd.inputChanged.add(h_ic)
            cmd.execute.add(h_ex)
            cmd.destroy.add(h_ds)
            handlers.extend([h_ic, h_ex, h_ds])
        except Exception as e:
            if ui:
                ui.messageBox(f'Dim Checklist dialog error:\n{e}')


class FCInputChangedHandler(adsk.core.InputChangedEventHandler):
    """Handles Place button clicks: places/replaces only the one clicked dim.

    Each dim's DrawingDimension object is stored in _pending['fc']['placed_objs']
    so that clicking Place again moves the dim without touching any other dim.
    """
    def notify(self, args):
        try:
            inp = args.input
            if not inp.id.startswith('fc_place_'):
                return
            if not inp.value:
                return  # ignore reset-to-False firing

            i = int(inp.id.split('_')[-1])
            dims_list   = _pending['fc'].get('dims_list', [])
            views       = _pending['fc'].get('views', [])
            placements  = _pending['fc'].get('placements', {})
            statuses    = _pending['fc'].setdefault('statuses', {})
            placed_objs = _pending['fc'].setdefault('placed_objs', {})
            inputs      = args.inputs

            if i >= len(dims_list):
                inp.value = False
                return

            dim    = dims_list[i]
            dim_id = dim['id']

            # Read only this dim's dropdown
            dd    = inputs.itemById(f'fc_view_{i}')
            v_idx = None
            if dd and dd.selectedItem:
                sel   = dd.selectedItem.index
                v_idx = sel - 1 if sel > 0 else None
            placements[dim_id] = v_idx

            # Delete the previous DrawingDimension for this dim (if any)
            old = placed_objs.get(dim_id)
            if old is not None:
                try:
                    old.deleteMe()
                except Exception:
                    pass
                placed_objs[dim_id] = None

            # Place in the selected view
            new_obj = None
            if v_idx is not None and v_idx < len(views):
                drawing_doc = find_open_drawing()
                if drawing_doc:
                    sheet  = drawing_doc.drawing.activeSheet
                    vi     = views[v_idx]
                    orient = dim.get('orient') or 'H'
                    # Slot = how many other dims are already placed in same view+orient
                    slot = sum(
                        1 for d in dims_list
                        if d['id'] != dim_id
                        and placed_objs.get(d['id']) is not None
                        and placements.get(d['id']) == v_idx
                        and (d.get('orient') or 'H') == orient
                    )
                    try:
                        if dim['type'] == 'linear':
                            new_obj = _place_linear_dim(sheet, vi, dim['value'],
                                                        orient=orient, slot=slot)
                            if new_obj is None:
                                # Try opposite orientation — sketch orient may not
                                # match the paper-space direction in projected views.
                                alt = 'V' if orient == 'H' else 'H'
                                new_obj = _place_linear_dim(sheet, vi, dim['value'],
                                                            orient=alt, slot=slot)
                        elif dim['type'] == 'radial':
                            new_obj = _place_radial_dim(sheet, vi, dim['value'])
                        elif dim['type'] == 'diameter':
                            new_obj = _place_diameter_dim(sheet, vi, dim['value'])
                        # angular: no auto-placement; handled as "manual" below
                    except Exception:
                        new_obj = None
                    if new_obj is None and v_idx is not None:
                        _write_place_debug(dim, vi, sheet)
                    placed_objs[dim_id] = new_obj

            # Update only this dim's status cell and persist it
            st = inputs.itemById(f'fc_status_{i}')
            if st:
                if dim.get('type') == 'angular':
                    status_str = '∠ manual'
                elif v_idx is None or v_idx >= len(views):
                    status_str = '—'
                elif new_obj is not None:
                    status_str = f'✓ {views[v_idx]["label"]}'
                else:
                    status_str = f'✗ not found in {views[v_idx]["label"]}'
                st.text = status_str
                statuses[dim_id] = status_str

            _save_fc_state(dims_list, placements, views, statuses, find_open_drawing())
            inp.value = False  # reset for next click
        except Exception:
            pass


class FCExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        # Dims are placed individually via Place buttons; OK just closes the dialog.
        _pending['fc'].clear()


class FCDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        # Dims are placed individually and state is saved; nothing to undo on cancel.
        _pending['fc'].clear()


# ─── DWG Probe ───────────────────────────────────────────────────────────────

_DUMP_PATH = os.path.join(DESKTOP, 'ghostforge_probe.txt').replace('\\', '/')

_LISP_PROBE = r'''
(defun _gf_probe (/ f ent data etype cnt pt _pg)
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
           (_w "  [DRAWINGVIEW — all groups:]")
           (setq _pg data)
           (while _pg
             (_w (strcat "    g" (itoa (caar _pg))
                         " = " (vl-princ-to-string (cdar _pg))))
             (setq _pg (cdr _pg))
           ))
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
                'the Python drawing API (views, dims, live call signatures).<br><br>'
                'Output: <tt>Desktop/ghostforge_api_probe.txt</tt><br>'
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

        # ── 0. View Curve Coordinate Dump — the key diagnostic for arc detection ──
        _vc_path = os.path.join(DESKTOP, 'ghostforge_view_curves.txt')
        try:
            with open(_vc_path, 'w', encoding='utf-8') as _vf:
                def _vw(s): _vf.write(s + '\n')
                _vw('=== GhostForge View Curve Coordinates ===')
                _vw('Each curve: [idx] ct=curveType sp=(x,y) ep=(x,y) ctr=(x,y) geom.r=cm')
                _vw('ct: 0=Line 1=Arc 2=Circle 3=Ellipse 4=Spline 5=Polyline')
                _vw('')
                _dd = adsk.drawing.DrawingDocument.cast(app.activeDocument)
                if _dd is None:
                    _vw('No drawing document open.')
                else:
                    _sh = _dd.drawing.activeSheet
                    for _vi in range(_sh.views.count):
                        _v = _sh.views.item(_vi)
                        _sc = 1.0
                        try:
                            _sc = float(_v.scale)
                        except Exception:
                            try:
                                _sc = float(_v.scaleValue)
                            except Exception:
                                pass
                        _vw(f'VIEW[{_vi}] type={_v.type} scale={_sc:.4g} '
                            f'curves={_v.viewCurves.count}')
                        _ctr_found = 0
                        for _ci in range(_v.viewCurves.count):
                            _c = _v.viewCurves.item(_ci)
                            _row = [f'  [{_ci:3d}]']
                            # curveType
                            _ct = '?'
                            for _a in ('curveType', 'type'):
                                try:
                                    _ct = str(getattr(_c, _a))
                                    break
                                except Exception:
                                    pass
                            _row.append(f'ct={_ct}')
                            # startPoint
                            try:
                                _co = _c.startPoint.coordinate
                                _row.append(f'sp=({_co.x:.3f},{_co.y:.3f})')
                            except Exception:
                                _row.append('sp=ERR')
                            # endPoint
                            try:
                                _co = _c.endPoint.coordinate
                                _row.append(f'ep=({_co.x:.3f},{_co.y:.3f})')
                            except Exception:
                                _row.append('ep=ERR')
                            # centerPoint
                            try:
                                _co = _c.centerPoint.coordinate
                                _row.append(f'ctr=({_co.x:.3f},{_co.y:.3f})')
                                _ctr_found += 1
                            except Exception:
                                _row.append('ctr=ERR')
                            # geometry.radius (model-space cm)
                            try:
                                _r = _c.geometry.radius
                                _row.append(f'geom.r={_r:.5f}cm')
                            except Exception:
                                pass
                            _vw(' '.join(_row))
                        _vw(f'  → centerPoints accessible: {_ctr_found}/{_v.viewCurves.count}')
                        _vw('')
                _vw('=== DONE ===')
            results.append(f'View curves -> Desktop/ghostforge_view_curves.txt')
        except Exception as _e:
            results.append(f'View curve dump ERR: {_e}')
        results.append('')

        # ── 1. Python Drawing API probe — runs first, own file, never overwritten ──
        _api_path = os.path.join(DESKTOP, 'ghostforge_api_probe.txt')
        api_summary = '?'
        try:
            with open(_api_path, 'w', encoding='utf-8') as fh:
                def _aw(s): fh.write(s + '\n')
                _aw('=== GhostForge Drawing API Probe ===')

                # Discover what adsk.drawing exposes
                _aw('--- adsk.drawing module attrs ---')
                try:
                    for a in sorted(x for x in dir(adsk.drawing) if not x.startswith('_')):
                        _aw(f'  {a}')
                except Exception as e:
                    _aw(f'  dir ERR: {e}')

                drawing_doc = adsk.drawing.DrawingDocument.cast(app.activeDocument)
                if drawing_doc is None:
                    _aw('DrawingDocument.cast -> None')
                    api_summary = 'cast->None'
                else:
                    _aw(f'DrawingDocument cast OK: {drawing_doc.name}')
                    api_summary = 'DrawingDoc OK'

                    # Discover all attributes on the DrawingDocument instance
                    _aw('--- DrawingDocument instance attrs ---')
                    try:
                        for a in sorted(x for x in dir(drawing_doc) if not x.startswith('_')):
                            _aw(f'  {a}')
                    except Exception as e:
                        _aw(f'  dir ERR: {e}')

                    # Go through drawing_doc.drawing (the Drawing object)
                    _aw('--- drawing_doc.drawing ---')
                    try:
                        drawing = drawing_doc.drawing
                        _aw(f'  drawing = {drawing!r}')
                        _aw('  --- Drawing instance attrs ---')
                        for a in sorted(x for x in dir(drawing) if not x.startswith('_')):
                            _aw(f'    {a}')
                        _aw('  --- Drawing attribute attempts ---')
                        for attr in ('sheets', 'activeSheet', 'drawingViews',
                                     'activeDrawingView', 'views', 'name'):
                            try:
                                val = getattr(drawing, attr)
                                _aw(f'    {attr} = {val!r}')
                                # If sheets, iterate it
                                if attr == 'sheets':
                                    try:
                                        _aw(f'    sheets.count = {val.count}')
                                        api_summary = f'sheets={val.count}'
                                        for si in range(min(val.count, 2)):
                                            sheet = val.item(si)
                                            _aw(f'    Sheet[{si}]: {sheet.name!r}')
                                            _aw(f'    Sheet[{si}] attrs: {[x for x in dir(sheet) if not x.startswith("_")]}')
                                    except Exception as e2:
                                        _aw(f'    sheets iterate ERR: {e2}')
                            except AttributeError:
                                _aw(f'    {attr}: no attr')
                            except Exception as e:
                                _aw(f'    {attr}: ERR {e}')
                    except AttributeError:
                        _aw('  drawing_doc.drawing: no attr')
                        api_summary = 'no .drawing'
                    except Exception as e:
                        _aw(f'  drawing_doc.drawing ERR: {e}')
                        api_summary = f'drawing ERR:{e}'

                    # Probe activeSheet fully
                    _aw('--- drawing.activeSheet ---')
                    try:
                        sheet = drawing_doc.drawing.activeSheet
                        _aw(f'  activeSheet = {sheet!r}')
                        _aw('  --- Sheet instance attrs ---')
                        for a in sorted(x for x in dir(sheet) if not x.startswith('_')):
                            _aw(f'    {a}')
                        _aw('  --- Sheet attribute attempts ---')
                        for attr in ('drawingViews', 'views', 'name', 'drawingViewDefinitions',
                                     'background', 'height', 'width'):
                            try:
                                val = getattr(sheet, attr)
                                _aw(f'    {attr} = {val!r}')
                                if attr in ('drawingViews', 'views'):
                                    try:
                                        _aw(f'    {attr}.count = {val.count}')
                                        api_summary = f'{attr}={val.count} (NEW API!)'
                                        for vi in range(min(val.count, 12)):
                                            v = val.item(vi)
                                            parts = [f'    [{vi}]']
                                            for vattr in ('name', 'type', 'scaleValue',
                                                          'viewType', 'drawingViewType',
                                                          'typeName', 'scale'):
                                                try:
                                                    parts.append(f'{vattr}={getattr(v, vattr)!r}')
                                                except Exception:
                                                    parts.append(f'{vattr}=N/A')
                                            try:
                                                pos = v.position
                                                parts.append(f'pos=({pos.x:.1f},{pos.y:.1f})')
                                            except Exception:
                                                pass
                                            _aw(' '.join(parts))
                                            # Probe orientation and viewCurves count
                                            try:
                                                ori = v.orientation
                                                _aw(f'      orientation = {ori!r}  type={type(ori).__name__}')
                                            except Exception as eo:
                                                _aw(f'      orientation ERR: {eo}')
                                            try:
                                                vc = v.viewCurves
                                                _aw(f'      viewCurves.count = {vc.count}')
                                            except Exception as evc:
                                                _aw(f'      viewCurves ERR: {evc}')
                                            # Also dump all attrs on first view
                                            if vi == 0:
                                                try:
                                                    _aw(f'      (View[0] full dir: {[x for x in dir(v) if not x.startswith("_")]})')
                                                except Exception:
                                                    pass
                                    except Exception as e2:
                                        _aw(f'    {attr} iterate ERR: {e2}')
                                        api_summary = f'{attr} ERR:{e2}'
                            except AttributeError:
                                _aw(f'    {attr}: no attr')
                            except Exception as e:
                                _aw(f'    {attr}: ERR {e}')
                    except Exception as e:
                        _aw(f'  activeSheet ERR: {e}')

                # ── Oct 2026 dimension placement API ──
                _aw('--- adsk.drawing dimension API (Oct 2026) ---')
                for cls_name in ('LinearDimension', 'RadialDimension',
                                 'AngularDimension', 'DiameterDimension',
                                 'LinearDimensionDefinition', 'RadialDimensionDefinition',
                                 'DrawingDimension', 'DrawingDimensions',
                                 'View', 'Views', 'Sheet', 'Sheets',
                                 'BaseViewInput', 'DrawingView', 'DrawingViews'):
                    try:
                        cls = getattr(adsk.drawing, cls_name)
                        _aw(f'  {cls_name}: FOUND ({cls!r})')
                    except AttributeError:
                        _aw(f'  {cls_name}: not in adsk.drawing')
                    except Exception as e:
                        _aw(f'  {cls_name}: ERR {e}')

                # ── ViewTypes enum integer values ──
                _aw('--- ViewTypes enum values ---')
                try:
                    vt = adsk.drawing.ViewTypes
                    for name in dir(vt):
                        if name.startswith('_'):
                            continue
                        try:
                            _aw(f'  ViewTypes.{name} = {getattr(vt, name)!r}')
                        except Exception:
                            pass
                except Exception as e:
                    _aw(f'  ViewTypes ERR: {e}')

                # ── ViewOrientationTypes enum values ──
                _aw('--- ViewOrientationTypes enum values ---')
                try:
                    vot = adsk.drawing.ViewOrientationTypes
                    for name in dir(vot):
                        if name.startswith('_'):
                            continue
                        try:
                            _aw(f'  ViewOrientationTypes.{name} = {getattr(vot, name)!r}')
                        except Exception:
                            pass
                except Exception as e:
                    _aw(f'  ViewOrientationTypes ERR: {e}')

                # ── Drawing Sketches ──
                _aw('--- sheet.sketches ---')
                try:
                    sheet2 = drawing_doc.drawing.activeSheet
                    sketches = sheet2.sketches
                    _aw(f'  sketches = {sketches!r}')
                    _aw(f'  sketches.count = {sketches.count}')
                    _aw(f'  DrawingSketches dir: {[x for x in dir(sketches) if not x.startswith("_")]}')
                    if sketches.count > 0:
                        sk = sketches.item(0)
                        _aw(f'  sketch[0] = {sk!r}')
                        _aw(f'  sketch[0] dir: {[x for x in dir(sk) if not x.startswith("_")]}')
                        for attr in ('lines', 'arcs', 'circles', 'ellipses', 'rectangles',
                                     'sketchLines', 'sketchArcs', 'sketchTexts',
                                     'texts', 'annotations', 'entities',
                                     'name', 'isVisible'):
                            try:
                                val = getattr(sk, attr)
                                _aw(f'    sketch[0].{attr} = {val!r}')
                                try:
                                    _aw(f'      .count = {val.count}')
                                    if val.count > 0:
                                        first = val.item(0)
                                        _aw(f'      [0] dir: {[x for x in dir(first) if not x.startswith("_")]}')
                                except Exception:
                                    pass
                            except AttributeError:
                                _aw(f'    sketch[0].{attr}: no attr')
                            except Exception as e:
                                _aw(f'    sketch[0].{attr}: ERR {e}')
                    else:
                        _aw('  (no sketches on sheet — trying add())')
                        try:
                            add_attrs = [x for x in dir(sketches) if 'add' in x.lower()]
                            _aw(f'  add-related attrs: {add_attrs}')
                        except Exception as e:
                            _aw(f'  add attr scan ERR: {e}')
                except Exception as e:
                    _aw(f'  sheet.sketches ERR: {e}')

                # ── DrawingDimensions method signatures via help() ──
                _aw('--- drawingDimensions method signatures ---')
                try:
                    import io
                    sheet4 = drawing_doc.drawing.activeSheet
                    dims4  = sheet4.drawingDimensions
                    for method_name in ('addLinearDimension', 'addRadialDimension',
                                        'addAngularDimension', 'addDiameterDimension',
                                        'addArcLengthDimension', 'addJoggedRadiusDimension'):
                        try:
                            method = getattr(dims4, method_name)
                            buf = io.StringIO()
                            import sys
                            old_stdout = sys.stdout
                            sys.stdout = buf
                            try:
                                help(method)
                            finally:
                                sys.stdout = old_stdout
                            _aw(f'  {method_name}:')
                            for line in buf.getvalue().splitlines():
                                _aw(f'    {line}')
                        except Exception as em:
                            _aw(f'  {method_name} help ERR: {em}')
                            try:
                                _aw(f'  {method_name}.__doc__ = {getattr(dims4, method_name).__doc__!r}')
                            except Exception:
                                pass
                except Exception as e:
                    _aw(f'  signatures ERR: {e}')

                # ── DrawingSketches.add signature ──
                _aw('--- DrawingSketches.add signature ---')
                try:
                    sheet5 = drawing_doc.drawing.activeSheet
                    sk5    = sheet5.sketches
                    buf = io.StringIO()
                    old_stdout = sys.stdout
                    sys.stdout = buf
                    try:
                        help(sk5.add)
                    finally:
                        sys.stdout = old_stdout
                    for line in buf.getvalue().splitlines():
                        _aw(f'  {line}')
                except Exception as e:
                    _aw(f'  sketches.add help ERR: {e}')

                # ── Drawing Dimensions (read existing) ──
                _aw('--- sheet.drawingDimensions ---')
                try:
                    sheet3 = drawing_doc.drawing.activeSheet
                    dims = sheet3.drawingDimensions
                    _aw(f'  drawingDimensions = {dims!r}')
                    _aw(f'  drawingDimensions.count = {dims.count}')
                    _aw(f'  DrawingDimensions dir: {[x for x in dir(dims) if not x.startswith("_")]}')
                    if dims.count > 0:
                        d = dims.item(0)
                        _aw(f'  dim[0] dir: {[x for x in dir(d) if not x.startswith("_")]}')
                        for attr in ('value', 'text', 'type', 'dimensionType',
                                     'position', 'origin', 'startPoint', 'endPoint',
                                     'isAssociated', 'view'):
                            try:
                                val = getattr(d, attr)
                                _aw(f'    dim[0].{attr} = {val!r}')
                            except AttributeError:
                                _aw(f'    dim[0].{attr}: no attr')
                            except Exception as e:
                                _aw(f'    dim[0].{attr}: ERR {e}')
                    else:
                        _aw('  (no dims on sheet — trying add-related attrs)')
                        add_attrs = [x for x in dir(dims) if 'add' in x.lower()]
                        _aw(f'  add-related attrs: {add_attrs}')
                except Exception as e:
                    _aw(f'  sheet.drawingDimensions ERR: {e}')

                # ── Live call probe — TypeError messages reveal exact signatures ──
                _aw('--- Live call probe (TypeError -> signature) ---')
                try:
                    sheet6 = drawing_doc.drawing.activeSheet
                    dims6  = sheet6.drawingDimensions
                    views6 = sheet6.views

                    # DrawingPoint_create — try no-args then (x, y)
                    pt_ok = None
                    for pt_args in [(), (0.0, 0.0)]:
                        try:
                            pt_ok = adsk.drawing.DrawingPoint_create(*pt_args)
                            _aw(f'  DrawingPoint_create{pt_args} -> OK {pt_ok!r}')
                            _aw(f'    dir: {[x for x in dir(pt_ok) if not x.startswith("_")]}')
                            break
                        except Exception as e:
                            _aw(f'  DrawingPoint_create{pt_args} -> {type(e).__name__}: {e}')

                    # addLinearDimension / addRadialDimension with no args
                    for mname in ('addLinearDimension', 'addRadialDimension',
                                  'addDiameterDimension'):
                        try:
                            getattr(dims6, mname)()
                        except Exception as e:
                            _aw(f'  {mname}() -> {type(e).__name__}: {e}')

                    if views6.count > 0:
                        v0 = views6.item(0)
                        vc = v0.viewCurves
                        _aw(f'  view[0] type={v0.type}  viewCurves.count={vc.count}')
                        if vc.count > 0:
                            c0 = vc.item(0)
                            _aw(f'  viewCurve[0] dir: {[x for x in dir(c0) if not x.startswith("_")]}')
                            # Try addLinearDimension with plausible arg combos
                            for sig_args, sig_label in [
                                ((v0, c0, c0, pt_ok), 'view,curve,curve,point'),
                                ((c0, c0, pt_ok),     'curve,curve,point'),
                                ((v0, c0, c0),        'view,curve,curve'),
                            ]:
                                if None in sig_args:
                                    continue
                                try:
                                    dims6.addLinearDimension(*sig_args)
                                    _aw(f'  addLinearDimension({sig_label}) -> OK!')
                                except Exception as e:
                                    _aw(f'  addLinearDimension({sig_label}) -> {type(e).__name__}: {e}')
                            # Try addRadialDimension
                            for sig_args, sig_label in [
                                ((v0, c0, pt_ok), 'view,curve,point'),
                                ((c0, pt_ok),     'curve,point'),
                                ((v0, c0),        'view,curve'),
                            ]:
                                if None in sig_args:
                                    continue
                                try:
                                    dims6.addRadialDimension(*sig_args)
                                    _aw(f'  addRadialDimension({sig_label}) -> OK!')
                                except Exception as e:
                                    _aw(f'  addRadialDimension({sig_label}) -> {type(e).__name__}: {e}')
                except Exception as e:
                    _aw(f'  live probe ERR: {e}')

                # ── ViewCurveTypes enum ──
                _aw('--- ViewCurveTypes enum ---')
                try:
                    vct = adsk.drawing.ViewCurveTypes
                    for name in sorted(x for x in dir(vct) if not x.startswith('_')):
                        try:
                            _aw(f'  ViewCurveTypes.{name} = {getattr(vct, name)!r}')
                        except Exception:
                            pass
                except Exception as e:
                    _aw(f'  ViewCurveTypes ERR: {e}')

                # ── ViewCurve geometry — per-curve attrs across all views ──
                _aw('--- ViewCurve geometry per view ---')
                try:
                    sheet7 = drawing_doc.drawing.activeSheet
                    for vi in range(sheet7.views.count):
                        v7  = sheet7.views.item(vi)
                        vc7 = v7.viewCurves
                        _aw(f'  view[{vi}] type={v7.type} curves={vc7.count}')
                        arcs_found = 0
                        for ci in range(min(vc7.count, 60)):
                            c = vc7.item(ci)
                            row = [f'    [{ci}]']
                            for attr in ('type', 'curveType'):
                                try:
                                    row.append(f'{attr}={getattr(c, attr)!r}')
                                except Exception:
                                    pass
                            # Geometry per curve type
                            for attr in ('startPoint', 'endPoint',        # lines
                                         'center', 'radius',               # arcs/circles
                                         'startAngle', 'endAngle',         # arcs
                                         'geometry', 'worldGeometry'):     # generic
                                try:
                                    val = getattr(c, attr)
                                    if hasattr(val, 'x'):
                                        row.append(f'{attr}=({val.x:.2f},{val.y:.2f})')
                                    else:
                                        row.append(f'{attr}={val!r}')
                                except AttributeError:
                                    pass
                                except Exception as e:
                                    row.append(f'{attr}=ERR:{e}')
                            _aw(' '.join(row))
                            # Full dir on first curve of each view
                            if ci == 0:
                                _aw(f'      dir: {[x for x in dir(c) if not x.startswith("_")]}')
                except Exception as e:
                    _aw(f'  ViewCurve geometry ERR: {e}')

                # ── isModified baseline (must come before any new sections) ──
                _mod_baseline = drawing_doc.isModified
                _aw(f'--- isModified baseline: {_mod_baseline} ---')

                def _check_modified(label):
                    now = drawing_doc.isModified
                    if now != _mod_baseline:
                        _aw(f'  *** DOCUMENT MODIFIED after: {label} ***')

                # ── DrawingPoint.coordinate.x/.y — the one remaining unknown ──
                _aw('--- DrawingPoint.coordinate.x/.y ---')
                try:
                    _sheet_coord = drawing_doc.drawing.activeSheet
                    for _vi in range(_sheet_coord.views.count):
                        _v = _sheet_coord.views.item(_vi)
                        if _v.viewCurves.count == 0:
                            continue
                        _c = _v.viewCurves.item(0)
                        _sp = _c.startPoint
                        _ep = _c.endPoint
                        _co = _sp.coordinate
                        _aw(f'  view[{_vi}] type={_v.type} curve[0] type={_c.type}')
                        _aw(f'    startPoint.coordinate type={type(_co).__name__}')
                        _aw(f'    coord dir: {[x for x in dir(_co) if not x.startswith("_")]}')
                        for _attr in ('x', 'y', 'z', 'asArray'):
                            try:
                                _val = getattr(_co, _attr)
                                if callable(_val):
                                    _aw(f'    coord.{_attr}() = {_val()!r}')
                                else:
                                    _aw(f'    coord.{_attr} = {_val!r}')
                            except Exception as _ae:
                                _aw(f'    coord.{_attr}: ERR {_ae}')
                        # Same for endPoint
                        _eco = _ep.coordinate
                        for _attr in ('x', 'y'):
                            try:
                                _aw(f'    endPoint.coord.{_attr} = {getattr(_eco, _attr)!r}')
                            except Exception as _ae:
                                _aw(f'    endPoint.coord.{_attr}: ERR {_ae}')
                        break  # only need one view
                    _check_modified('coordinate read')
                except Exception as _e:
                    _aw(f'  coordinate ERR: {_e}')

                # ── Per-view bounding boxes using .coordinate ──
                _aw('--- Per-view bounding boxes (coordinate) ---')
                try:
                    _sheet_bbox = drawing_doc.drawing.activeSheet
                    for _vi in range(_sheet_bbox.views.count):
                        _v = _sheet_bbox.views.item(_vi)
                        _xs, _ys = [], []
                        for _ci in range(_v.viewCurves.count):
                            _c = _v.viewCurves.item(_ci)
                            for _pt in (_c.startPoint, _c.endPoint):
                                try:
                                    _co = _pt.coordinate
                                    _xs.append(_co.x)
                                    _ys.append(_co.y)
                                except Exception:
                                    pass
                        if _xs:
                            _aw(f'  view[{_vi}] type={_v.type}'
                                f' x={min(_xs):.3f}..{max(_xs):.3f}'
                                f' y={min(_ys):.3f}..{max(_ys):.3f}'
                                f' w={max(_xs)-min(_xs):.3f}'
                                f' h={max(_ys)-min(_ys):.3f}')
                        else:
                            _aw(f'  view[{_vi}] type={_v.type}: no coords')
                    _check_modified('bounding box')
                except Exception as _e:
                    _aw(f'  bbox ERR: {_e}')

                # ── DrawingPoint_create(Point2D) coordinate round-trip ──
                _aw('--- DrawingPoint_create coordinate round-trip ---')
                try:
                    _pt2d = adsk.core.Point2D.create(12.34, 56.78)
                    _dp   = adsk.drawing.DrawingPoint_create(_pt2d)
                    _co   = _dp.coordinate
                    _aw(f'  DrawingPoint_create(Point2D(12.34, 56.78)):')
                    _aw(f'    .coordinate type = {type(_co).__name__}')
                    for _attr in ('x', 'y'):
                        try:
                            _aw(f'    .coordinate.{_attr} = {getattr(_co, _attr)!r}')
                        except Exception as _ae:
                            _aw(f'    .coordinate.{_attr}: ERR {_ae}')
                    _check_modified('DrawingPoint_create')
                except Exception as _e:
                    _aw(f'  round-trip ERR: {_e}')

                _aw(f'--- isModified final: {drawing_doc.isModified} ---')
                _aw('=== DONE ===')
        except Exception as e:
            api_summary = f'file-write-ERR:{e}'
        # Summary at top so msgbox truncation can't hide it
        results.append(f'API probe: {api_summary}')
        results.append('  -> Desktop/ghostforge_api_probe.txt')
        results.append('')

        # ── 2. LISP entity dump (async — overwrites probe.txt after messagebox) ──
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
            results.append('DWG dump -> Desktop/ghostforge_probe.txt')
        except Exception as e:
            results.append(f'DWG dump ERR: {e}')

        results.append('')
        for label, cmd_str in _PROBE_TRIALS:
            try:
                r = app.executeTextCommand(cmd_str)
                results.append(f'OK  {label}')
                if r:
                    results.append(f'    -> {r!r}')
            except Exception as e:
                results.append(f'ERR {label}')
                results.append(f'    -> {e}')

        ui.messageBox('DWG Probe\n\n' + '\n'.join(results))


# ─── Panel management ─────────────────────────────────────────────────────────

_ALL_CMD_IDS = (CMD_SKETCH, CMD_DIMS, CMD_FC, CMD_PROBE)


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
    try:
        stale = panel.controls.itemById(cmd_id)
        if stale:
            stale.deleteMe()
    except Exception:
        pass
    ctrl = panel.controls.addCommand(cmd_def)
    ctrl.isPromoted          = True
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
            _get_or_create_panel(tab, GF_FC_PANEL, 'Checklist'),
            CMD_FC, 'Dim Checklist',
            'Feature checklist: select which features to annotate on the drawing. '
            'Shows each feature with its associated sketch and parameters.',
            FCCreatedHandler,
            resource_folder=os.path.join(_RESOURCES, 'fc_icon'))

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
