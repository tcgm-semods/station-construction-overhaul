"""Station Construction Overhaul:

Rebuilds the player-station "Construction" tab so it is readable at a glance
and every common action is one click away. Progress is the headline feature:

  * A summary strip with building / paused counts, one combined progress bar
    and the longest remaining ETA.
  * One card per job: name, quantity, a large bar with an exact percentage
    (one decimal), a live ETA and inline Pause/Resume + Cancel buttons.
  * Every blueprint row has a BUILD button (and an xN... button when the
    blueprint has more than one use), a tech badge, remaining uses and a cost
    summary, plus tech-tier filter chips next to the search box.

How it hooks in (the Construction tab is drawn inline inside the huge
RenderMixin._draw_station_overlay, there is no standalone method to patch):

  * _draw_station_overlay is wrapped. The original runs first and draws the
    window chrome and tab strip (and its own, now redundant, Construction
    body). We then restore the plate behind the tab body and paint ours over
    it. The game's own cancel-confirm modal and blueprint popup are hidden
    for the duration of the original call (otherwise they would be painted
    underneath our body) and redrawn by us afterwards.
  * We publish the SAME attributes the game's own input code reads
    (_ps_cb_proj_rects, _ps_cb_bp_rects, _ps_cb_search_rect, the *_panel_rect
    wheel-routing rects, the scrollbar track/thumb geometry, and the
    yes/no/panel rects inside _ps_cb_cancel_confirm). So row selection,
    mouse-wheel scrolling, scrollbar dragging, search typing, the blueprint
    popup, the right-click Build menu and the cancel confirmation keep
    working through the game's code; this mod only adds the new buttons, via
    the client.event hook, and consumes a click only when it lands on one.
  * Progress comes from the server as a 0..1 float per project. ETA is
    derived client-side from how fast that float has actually been moving
    (a sliding window of observed changes), and is cleared whenever a job is
    paused/resumed so a pause never poisons the estimate.
  * While the tab is open the queue is re-requested every POLL_S seconds so
    the bars move without reopening the tab.

If anything in our drawing raises, the error is logged once and the mod
stops touching the tab, so the vanilla Construction tab is shown instead.
"""
import sys
import time
from collections import deque

CB_TAB = "Construction Bay"
POLL_S = 15.0           # queue re-request cadence while the tab is open
ETA_WINDOW_S = 300.0    # how far back progress samples inform the ETA
MAX_VISIBLE_CARDS = 3

_st = {"cls": None, "orig": None, "logger": None, "failed": False, "rm": None}

_tech_filter = [None]          # None = all tiers, else str(tech)
_samples = {}                  # project_id -> deque[(monotonic, progress)]
_paused_prev = {}              # project_id -> bool
_last_poll = [0.0]
_was_active = [False]
_keys_logged = [False]

# Hit rects for the buttons this mod adds, rebuilt every drawn frame.
_hits = {"proj": [], "build": [], "amount": [], "chips": [], "clear": None}


def _clear_hits():
    _hits["proj"] = []
    _hits["build"] = []
    _hits["amount"] = []
    _hits["chips"] = []
    _hits["clear"] = None


# ── Small helpers ────────────────────────────────────────────────────────

def _fmt_duration(seconds):
    seconds = max(0, int(seconds + 0.5))
    if seconds >= 86400:
        return f"{seconds // 86400}d {(seconds % 86400) // 3600:02d}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds}s"


def _fit_text(font, text, max_w):
    if max_w <= 0:
        return ""
    if font.size(text)[0] <= max_w:
        return text
    ell = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.size(text[:mid].rstrip() + ell)[0] <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + ell) if lo > 0 else ell


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _tech_label(tech):
    text = str(tech).strip()
    return f"T{text}" if text.isdigit() else (text or "-")


def _tech_sort_key(tech):
    text = str(tech)
    return (0, int(text), text) if text.isdigit() else (1, 0, text)


# ── Progress / ETA tracking ──────────────────────────────────────────────

def _update_samples(projects, now):
    live = set()
    for proj in projects:
        pid = proj.get("project_id")
        if pid is None:
            continue
        live.add(pid)
        paused = bool(proj.get("paused"))
        progress = _num(proj.get("progress"))
        samples = _samples.setdefault(pid, deque(maxlen=64))
        # A pause/resume flip invalidates the rate: the gap would read as
        # "this job is very slow".
        if _paused_prev.get(pid) is not None and _paused_prev[pid] != paused:
            samples.clear()
        _paused_prev[pid] = paused
        if paused:
            continue
        if samples and progress < samples[-1][1]:
            samples.clear()          # progress went backwards: new baseline
        if not samples or progress != samples[-1][1]:
            samples.append((now, progress))
    for pid in list(_samples):
        if pid not in live:
            _samples.pop(pid, None)
            _paused_prev.pop(pid, None)


def _eta_seconds(pid, progress, now):
    """Remaining seconds, or None while there is not enough data yet."""
    samples = _samples.get(pid)
    if not samples or len(samples) < 2:
        return None
    t_last, p_last = samples[-1]
    first = samples[0]
    for t, p in samples:
        if t_last - t <= ETA_WINDOW_S:
            first = (t, p)
            break
    t0, p0 = first
    if t_last <= t0 or p_last <= p0:
        return None
    rate = (p_last - p0) / (t_last - t0)
    if rate <= 0:
        return None
    return max(0.0, (1.0 - p_last) / rate - (now - t_last))


# ── Cost helpers ─────────────────────────────────────────────────────────

_LEGACY_COSTS = (
    ("cost_metal", "Metal"), ("cost_silicon", "Silicon"),
    ("cost_copper", "Copper"), ("cost_titanium", "Titanium"),
    ("cost_gold", "Gold"), ("cost_oil", "Oil"), ("cost_wood", "Wood"),
    ("cost_microchips", "Microchips"), ("cost_colonists", "Colonists"),
    ("cost_rations", "Rations"),
)


def _cost_groups(bp):
    """Return (initial, periodic) lists of (name, value) like the game popup."""
    initial, periodic = [], []
    if bp.get("cost_credits", 0):
        periodic.append(("Credits", bp["cost_credits"]))
    if "resource_costs" in bp:
        for rc in bp.get("resource_costs") or []:
            target = initial if rc.get("timing") == "initial" else periodic
            target.append((
                rc.get("display_name")
                or str(rc.get("resource", "")).replace("_", " ").title(),
                rc.get("amount", 0)))
    else:
        for key, label in _LEGACY_COSTS:
            if bp.get(key, 0):
                periodic.append((label, bp[key]))
    for ing in bp.get("ingredients") or []:
        initial.append((
            ing.get("display_name")
            or str(ing.get("item_type", "")).replace("_", " ").title(),
            f"x{ing.get('amount', 1)}"))
    return initial, periodic


_REQ_KEYS = ("resource_costs", "ingredients", "manhours", "cost_credits",
             "description") + tuple(k for k, _ in _LEGACY_COSTS)


def _requirements_for(proj, blueprints):
    """Best-effort cost data for an in-progress job, or None.

    The server merges blueprint data into queue rows, so the job may already
    carry its costs; otherwise find its blueprint by id, then by name.
    """
    src = {}
    bid, name = proj.get("id"), proj.get("name")
    match = None
    for bp in blueprints:
        if bid is not None and bp.get("id") == bid:
            match = bp
            break
    if match is None and name:
        for bp in blueprints:
            if name in (bp.get("output_name"), bp.get("display_name")):
                match = bp
                break
    if match:
        src.update(match)
    for key in _REQ_KEYS:
        if key in proj:
            src[key] = proj[key]
    return src if any(k in src for k in _REQ_KEYS) else None


def _fmt_value(rm, value):
    fmt = getattr(rm, "format_tooltip_value", None)
    if fmt is not None:
        try:
            return str(fmt(value))
        except Exception:
            pass
    return str(value)


def _cost_summary(rm, bp):
    initial, periodic = _cost_groups(bp)
    parts = []
    if initial:
        parts.append("Initial: " + ", ".join(
            f"{n} {_fmt_value(rm, v)}" for n, v in initial))
    if periodic:
        parts.append("Periodic: " + ", ".join(
            f"{n} {_fmt_value(rm, v)}" for n, v in periodic))
    if bp.get("manhours", 0):
        parts.append(f"{_fmt_value(rm, bp['manhours'])} manhours")
    return "  |  ".join(parts) if parts else "No listed cost"


# ── Drawing ──────────────────────────────────────────────────────────────

def _draw_construction(host, pygame, screen):
    rm = _st["rm"]
    S = host._s

    def F(size, bold=False):
        return host._F(size + 1, bold=bold)

    def IF(size, bold=False):
        return host._instrument_font(size + 1, bold=bold)

    def pal(name, default):
        return getattr(rm, name, default)

    C_PLATE = pal("_PS_PLATE_BG", (8, 14, 26))
    C_CANVAS = pal("_PS_CANVAS_BG", (6, 12, 22))
    C_HEADER = pal("_PS_HEADER_BG", (14, 28, 48))
    C_GRID = pal("_PS_GRID", (24, 40, 62))
    C_STEEL = pal("_PS_STEEL", (70, 100, 140))
    C_ACCENT = pal("_PS_ACCENT", (86, 164, 232))
    C_TEXT = pal("_PS_TEXT", (180, 215, 245))
    C_VALUE = pal("_PS_VALUE", (230, 245, 255))
    C_DIM = pal("_PS_CAPTION", (100, 130, 165))
    C_OK = pal("_PS_SUCCESS", (90, 210, 130))
    C_AMBER = pal("_PS_ENERGY", (240, 190, 80))
    C_WARN = pal("_PS_WARN", (235, 100, 90))
    C_BG = pal("_PS_CONTROL_BG", (11, 24, 43))
    C_HOV = pal("_PS_CONTROL_HOV", (18, 42, 72))
    C_SEL = pal("_PS_CONTROL_SEL", (18, 58, 94))
    C_ROW = pal("_PS_ROW_BG", (7, 15, 28))
    C_ROW_ALT = pal("_PS_ROW_ALT", (10, 20, 36))
    C_ACT_BG = pal("_PS_ACTION_BG", (18, 52, 86))
    C_ACT_HOV = pal("_PS_ACTION_HOV", (30, 78, 126))
    C_ACT_BORDER = pal("_PS_ACTION_BORDER", (86, 164, 232))

    with host._lock:
        ov = host._station_overlay
    if ov is None:
        return
    body = ov.get("body_rect")
    tabs = getattr(host, "_ps_tab_rects", None) or {}
    if body is None or not tabs:
        return
    data = ov.get("data") or {}
    sid = data.get("attached_station_id")
    projects = list(data.get("construction_projects") or [])
    blueprints = list(data.get("blueprints") or [])
    now = time.monotonic()
    mx, my = pygame.mouse.get_pos()
    win_w, win_h = screen.get_size()

    if projects and not _keys_logged[0]:
        _keys_logged[0] = True
        logger = _st["logger"]
        if logger is not None:
            logger.info("construction project keys: %s",
                        sorted(projects[0].keys()))

    if not _was_active[0]:
        _was_active[0] = True
        _last_poll[0] = now
    elif sid is not None and now - _last_poll[0] >= POLL_S:
        _last_poll[0] = now
        try:
            host._send_fn(f"PS_REQUEST_CONSTRUCTION_BAY {sid}")
        except Exception:
            pass

    _update_samples(projects, now)

    # ── Wipe the vanilla body, keeping tab strip + divider ───────────────
    tab_bottom = max(r.bottom for r in tabs.values())
    wipe_top = tab_bottom + S(11)
    try:
        plate = host._hud_plate_layers(
            body.w, body.h, S(48), chamfer_max=S(18))["plate"]
        rel_top = wipe_top - body.y
        screen.blit(plate, (body.x, wipe_top),
                    pygame.Rect(0, rel_top, body.w, max(0, body.h - rel_top)))
    except Exception:
        wipe = pygame.Surface((body.w - S(24), max(1, body.bottom - wipe_top - S(10))))
        wipe.fill(C_PLATE)
        screen.blit(wipe, (body.x + S(12), wipe_top))

    def fill_a(rect, rgb, alpha):
        surf = pygame.Surface((rect.w, rect.h), pygame.SRCALPHA)
        surf.fill((*rgb, alpha))
        screen.blit(surf, rect.topleft)

    def text(font, txt, col, x, y):
        surf = font.render(txt, True, col)
        screen.blit(surf, (x, y))
        return surf

    def text_vc(font, txt, col, x, rect):
        surf = font.render(txt, True, col)
        screen.blit(surf, (x, rect.y + (rect.h - surf.get_height()) // 2))
        return surf

    def button(rect, label, kind="neutral", enabled=True):
        hov = enabled and rect.collidepoint(mx, my)
        if not enabled:
            fill, border, fg = (16, 22, 34), (40, 50, 70), (70, 82, 100)
        elif kind == "primary":
            fill = C_ACT_HOV if hov else C_ACT_BG
            border, fg = C_ACT_BORDER, C_VALUE
        elif kind == "danger":
            fill = (86, 28, 34) if hov else (58, 20, 26)
            border, fg = (180, 70, 70), (255, 205, 205)
        else:
            fill = C_HOV if hov else C_BG
            border, fg = C_STEEL, C_VALUE
        pygame.draw.rect(screen, fill, rect)
        pygame.draw.rect(screen, border, rect, 1)
        lbl = IF(10, bold=True).render(label, True, fg)
        screen.blit(lbl, (rect.x + (rect.w - lbl.get_width()) // 2,
                          rect.y + (rect.h - lbl.get_height()) // 2))

    def progress_bar(rect, frac, col, label=None):
        frac = max(0.0, min(1.0, frac))
        pygame.draw.rect(screen, C_BG, rect)
        if frac > 0:
            fw = max(1, int(rect.w * frac))
            pygame.draw.rect(screen, col, (rect.x, rect.y, fw, rect.h))
            # Brighter leading edge so motion is visible between updates.
            if fw > 2:
                lead = tuple(min(255, c + 60) for c in col)
                pygame.draw.rect(
                    screen, lead, (rect.x + fw - 2, rect.y, 2, rect.h))
        pygame.draw.rect(screen, C_STEEL, rect, 1)
        if label:
            f = IF(10, bold=True)
            shadow = f.render(label, True, (0, 0, 0))
            lbl = f.render(label, True, (245, 250, 255))
            lx = rect.x + (rect.w - lbl.get_width()) // 2
            ly = rect.y + (rect.h - lbl.get_height()) // 2
            screen.blit(shadow, (lx + 1, ly + 1))
            screen.blit(lbl, (lx, ly))

    x0 = body.x + S(12)
    W = body.w - S(24)
    y = tab_bottom + S(20)
    bottom = body.bottom - S(10)

    # ── Summary strip ────────────────────────────────────────────────────
    n_paused = sum(1 for p in projects if p.get("paused"))
    n_active = len(projects) - n_paused
    sum_rect = pygame.Rect(x0, y, W, S(54))
    fill_a(sum_rect, C_CANVAS, 235)
    pygame.draw.rect(screen, C_STEEL, sum_rect, 1)
    text(IF(11, bold=True), "CONSTRUCTION QUEUE", C_TEXT,
         sum_rect.x + S(10), sum_rect.y + S(6))
    if projects:
        status = f"{n_active} building"
        if n_paused:
            status += f"  ·  {n_paused} paused"
        status_col = C_OK if n_active else C_AMBER
    else:
        status, status_col = "Idle", C_DIM
    sf = F(11, bold=True)
    ssurf = sf.render(status, True, status_col)
    screen.blit(ssurf, (sum_rect.right - ssurf.get_width() - S(10),
                        sum_rect.y + S(6)))
    if projects:
        etas = [_eta_seconds(p.get("project_id"), _num(p.get("progress")), now)
                for p in projects if not p.get("paused")]
        known = [e for e in etas if e is not None]
        if known and len(known) == len(etas):
            eta_txt = f"All done in about {_fmt_duration(max(known))}"
        elif known:
            eta_txt = f"Longest so far: {_fmt_duration(max(known))} (estimating others)"
        elif etas:
            eta_txt = "Estimating time remaining…"
        else:
            eta_txt = "Everything is paused"
        overall = sum(max(0.0, min(1.0, _num(p.get("progress"))))
                      for p in projects) / len(projects)
        etasurf = F(10).render(eta_txt, True, C_DIM)
        screen.blit(etasurf, (sum_rect.x + S(10) + IF(11, bold=True).size(
            "CONSTRUCTION QUEUE")[0] + S(14), sum_rect.y + S(8)))
        progress_bar(pygame.Rect(sum_rect.x + S(10), sum_rect.y + S(28),
                                 sum_rect.w - S(20), S(16)),
                     overall, C_OK if n_active else C_AMBER,
                     f"Overall  {overall * 100:.1f}%")
    else:
        progress_bar(pygame.Rect(sum_rect.x + S(10), sum_rect.y + S(28),
                                 sum_rect.w - S(20), S(16)),
                     0.0, C_OK, "No active jobs - pick a blueprint below")
    y = sum_rect.bottom + S(8)

    # ── Active projects ──────────────────────────────────────────────────
    text(IF(10, bold=True), "ACTIVE PROJECTS", C_TEXT, x0, y)
    y += IF(10, bold=True).get_height() + S(4)

    CARD_H = S(50)
    SB_W = S(8)
    vis_cards = max(1, min(len(projects), MAX_VISIBLE_CARDS))
    proj_area = pygame.Rect(x0, y, W, CARD_H * vis_cards)
    fill_a(proj_area, C_CANVAS, 235)
    pygame.draw.rect(screen, C_STEEL, proj_area, 1)

    proj_total = len(projects)
    proj_max = max(0, proj_total - vis_cards)
    host._ps_cb_proj_scroll = max(0, min(host._ps_cb_proj_scroll, proj_max))
    proj_overflow = proj_total > vis_cards
    card_w = proj_area.w - (SB_W + S(4) if proj_overflow else 0)
    host._ps_cb_proj_panel_rect = pygame.Rect(proj_area)

    new_proj_rects = []
    hover_proj = None
    _hits["proj"] = []
    if not projects:
        msg = F(11).render(
            "Nothing under construction. Press BUILD on a blueprint below.",
            True, C_DIM)
        screen.blit(msg, (proj_area.x + (proj_area.w - msg.get_width()) // 2,
                          proj_area.y + (proj_area.h - msg.get_height()) // 2))
    else:
        start = host._ps_cb_proj_scroll
        for i, proj in enumerate(projects[start:start + vis_cards]):
            pid = proj.get("project_id")
            paused = bool(proj.get("paused"))
            progress = max(0.0, min(1.0, _num(proj.get("progress"))))
            qty = int(_num(proj.get("qty"), 1) or 1)
            card = pygame.Rect(proj_area.x, proj_area.y + i * CARD_H,
                               card_w, CARD_H)
            selected = pid is not None and host._ps_cb_selected_proj == pid
            hovered = card.collidepoint(mx, my)
            if selected:
                fill_a(card, C_SEL, 225)
            elif hovered:
                fill_a(card, C_HOV, 190)
            elif i % 2:
                fill_a(card, C_ROW_ALT, 200)
            if i > 0:
                pygame.draw.line(screen, C_GRID, (card.x, card.y),
                                 (card.right, card.y))
            bar_col = C_AMBER if paused else C_OK
            pygame.draw.rect(screen, bar_col,
                             (card.x, card.y + 1, S(3), card.h - 1))

            # Buttons (top-right)
            bh = S(20)
            can_r = pygame.Rect(card.right - S(8) - S(62), card.y + S(5),
                                S(62), bh)
            pau_r = pygame.Rect(can_r.x - S(6) - S(70), card.y + S(5),
                                S(70), bh)
            if pid is not None:
                button(pau_r, "RESUME" if paused else "PAUSE")
                button(can_r, "CANCEL", "danger")
                _hits["proj"].append((pid, pau_r, can_r, proj))

            # Status / ETA, right-aligned left of the buttons
            if paused:
                st_txt, st_col = "PAUSED", C_AMBER
            else:
                eta = _eta_seconds(pid, progress, now)
                if progress >= 1.0:
                    st_txt, st_col = "Finishing…", C_OK
                elif eta is None:
                    st_txt, st_col = "ETA estimating…", C_DIM
                else:
                    st_txt, st_col = f"ETA {_fmt_duration(eta)}", C_TEXT
            stf = F(11, bold=True)
            sts = stf.render(st_txt, True, st_col)
            sts_x = pau_r.x - S(10) - sts.get_width()
            screen.blit(sts, (sts_x, card.y + S(5)
                              + (bh - sts.get_height()) // 2))

            # Name
            name = str(proj.get("name", "Unknown"))
            if qty > 1:
                name += f"  x{qty}"
            nf = IF(12, bold=True)
            name = _fit_text(nf, name, sts_x - S(10) - (card.x + S(12)))
            text(nf, name, C_AMBER if paused else C_VALUE, card.x + S(12),
                 card.y + S(5) + (bh - nf.get_height()) // 2)

            # Progress bar
            bar = pygame.Rect(card.x + S(12), card.y + S(29),
                              card.w - S(24), S(14))
            progress_bar(bar, progress, bar_col, f"{progress * 100:.1f}%")

            if hovered and not (pau_r.collidepoint(mx, my)
                                or can_r.collidepoint(mx, my)):
                hover_proj = (proj, card)
            new_proj_rects.append((pid, card, proj))
    host._ps_cb_proj_rects = new_proj_rects

    _publish_scrollbar(
        host, pygame, screen, "proj", proj_overflow, proj_area,
        host._ps_cb_proj_scroll, proj_max, proj_total, vis_cards, CARD_H,
        SB_W, S, C_BG, C_ACCENT, C_STEEL, C_VALUE, mx, my)

    # Original Pause/Cancel footer buttons are replaced by inline ones.
    host._ps_cb_pause_rect = None
    host._ps_cb_cancel_rect = None

    y = proj_area.bottom + S(10)
    pygame.draw.line(screen, C_STEEL, (x0, y), (x0 + W, y))
    y += S(8)

    # ── Blueprints header ────────────────────────────────────────────────
    hdr_f = IF(10, bold=True)
    query = host._ps_cb_search.strip().lower()
    tech_values = sorted({str(bp.get("tech", "")) for bp in blueprints},
                         key=_tech_sort_key)
    if _tech_filter[0] is not None and _tech_filter[0] not in tech_values:
        _tech_filter[0] = None
    filtered = [
        bp for bp in blueprints
        if (not query or query in str(
            bp.get("output_name") or bp.get("display_name", "")).lower())
        and (_tech_filter[0] is None
             or str(bp.get("tech", "")) == _tech_filter[0])
    ]
    hdr_txt = "AVAILABLE BLUEPRINTS"
    hs = text(hdr_f, hdr_txt, C_TEXT, x0, y)
    count_txt = (f"{len(filtered)} of {len(blueprints)}"
                 if len(filtered) != len(blueprints) else f"{len(blueprints)}")
    text(F(10), count_txt, C_DIM, x0 + hs.get_width() + S(10), y + S(1))
    y += hs.get_height() + S(4)

    # ── Search box + tech chips ──────────────────────────────────────────
    ROW_H = S(22)
    srch_w = min(int(W * 0.42), S(260))
    srch = pygame.Rect(x0, y, srch_w, ROW_H)
    host._ps_cb_search_rect = srch
    focus = host._ps_cb_search_focus
    fill_a(srch, C_HOV if focus else C_CANVAS, 235)
    pygame.draw.rect(screen, C_ACCENT if focus else (
        C_ACCENT if srch.collidepoint(mx, my) else C_STEEL), srch, 1)
    if host._ps_cb_search:
        s_txt, s_col = host._ps_cb_search, C_VALUE
    elif focus:
        s_txt, s_col = "", C_VALUE
    else:
        s_txt, s_col = "Search blueprints...", C_DIM
    if focus and (pygame.time.get_ticks() // 500) % 2 == 0:
        try:
            s_txt = rm.text_with_caret(
                s_txt, host._text_cursors.get("ps_cb_search"))
        except Exception:
            s_txt += "|"
    clear_w = S(18) if host._ps_cb_search else 0
    sfont = F(11)
    s_txt = _fit_text(sfont, s_txt, srch.w - S(16) - clear_w)
    text_vc(sfont, s_txt, s_col, srch.x + S(8), srch)
    if host._ps_cb_search:
        clr = pygame.Rect(srch.right - S(18), srch.y + S(2), S(16), srch.h - S(4))
        clr_hov = clr.collidepoint(mx, my)
        lbl = IF(11, bold=True).render("x", True, C_VALUE if clr_hov else C_DIM)
        screen.blit(lbl, (clr.x + (clr.w - lbl.get_width()) // 2,
                          clr.y + (clr.h - lbl.get_height()) // 2))
        _hits["clear"] = clr
    else:
        _hits["clear"] = None

    _hits["chips"] = []
    if len(tech_values) > 1:
        cx = srch.right + S(10)
        chip_f = IF(10, bold=True)
        for value in [None] + tech_values:
            label = "ALL" if value is None else _tech_label(value)
            cw = chip_f.size(label)[0] + S(16)
            if cx + cw > x0 + W:
                break
            chip = pygame.Rect(cx, y, cw, ROW_H)
            active = _tech_filter[0] == value
            hov = chip.collidepoint(mx, my)
            pygame.draw.rect(screen, C_SEL if active else (C_HOV if hov else C_BG),
                             chip)
            pygame.draw.rect(screen, C_ACCENT if active else C_STEEL, chip, 1)
            ls = chip_f.render(label, True, C_VALUE if active else C_TEXT)
            screen.blit(ls, (chip.x + (chip.w - ls.get_width()) // 2,
                             chip.y + (chip.h - ls.get_height()) // 2))
            _hits["chips"].append((chip, value))
            cx += cw + S(4)
    y += ROW_H + S(6)

    # ── Blueprint list ───────────────────────────────────────────────────
    hint_h = S(16)
    list_rect = pygame.Rect(x0, y, W, max(S(40), bottom - hint_h - y))
    fill_a(list_rect, C_CANVAS, 235)
    pygame.draw.rect(screen, C_STEEL, list_rect, 1)

    BP_ROW_H = S(38)
    bp_vis = max(1, list_rect.h // BP_ROW_H)
    bp_total = len(filtered)
    bp_max = max(0, bp_total - bp_vis)
    host._ps_cb_bp_scroll = max(0, min(host._ps_cb_bp_scroll, bp_max))
    bp_overflow = bp_total > bp_vis
    row_w = list_rect.w - (SB_W + S(4) if bp_overflow else 0)
    host._ps_cb_bp_panel_rect = pygame.Rect(list_rect)

    new_bp_rects = []
    _hits["build"] = []
    _hits["amount"] = []
    if not filtered:
        if not blueprints:
            msg_txt = "No blueprints equipped"
        elif query:
            msg_txt = f'No blueprints match "{host._ps_cb_search.strip()}"'
        else:
            msg_txt = "No blueprints in this tech tier"
        ms = F(11).render(msg_txt, True, C_DIM)
        screen.blit(ms, (list_rect.x + (list_rect.w - ms.get_width()) // 2,
                         list_rect.y + (list_rect.h - ms.get_height()) // 2))
    else:
        start = host._ps_cb_bp_scroll
        for i, bp in enumerate(filtered[start:start + bp_vis]):
            abs_idx = start + i
            row = pygame.Rect(list_rect.x, list_rect.y + i * BP_ROW_H,
                              row_w, BP_ROW_H)
            hovered = row.collidepoint(mx, my)
            popup = host._ps_cb_bp_popup
            open_here = popup is not None and popup.get("idx") == abs_idx
            if hovered or open_here:
                fill_a(row, C_HOV, 190)
            elif i % 2:
                fill_a(row, C_ROW_ALT, 200)
            if i > 0:
                pygame.draw.line(screen, C_GRID, (row.x, row.y),
                                 (row.right, row.y))

            # Tech badge (only when the server supplies a tier)
            has_tech = bool(str(bp.get("tech", "")).strip())
            badge = pygame.Rect(row.x + S(8), row.y + (row.h - S(22)) // 2,
                                S(34), S(22))
            if has_tech:
                pygame.draw.rect(screen, C_BG, badge)
                pygame.draw.rect(screen, C_STEEL, badge, 1)
                bl = IF(10, bold=True).render(
                    _tech_label(bp.get("tech", "")), True, C_ACCENT)
                screen.blit(bl, (badge.x + (badge.w - bl.get_width()) // 2,
                                 badge.y + (badge.h - bl.get_height()) // 2))

            uses = bp.get("uses_remaining", bp.get("uses", None))
            unlimited = uses is None or uses == -1
            exhausted = (not unlimited) and isinstance(uses, (int, float)) and uses <= 0
            multi = unlimited or (isinstance(uses, (int, float)) and uses > 1)
            max_uses = 9999 if unlimited else int(uses or 0)

            # Right-hand action cluster
            bh = S(24)
            by = row.y + (row.h - bh) // 2
            build_r = pygame.Rect(row.right - S(8) - S(62), by, S(62), bh)
            amt_r = pygame.Rect(build_r.x - S(6) - S(44), by, S(44), bh)
            button(build_r, "BUILD", "primary", enabled=not exhausted)
            _hits["build"].append((build_r, bp, exhausted))
            cluster_left = build_r.x
            if multi:
                button(amt_r, "x N", "neutral")
                _hits["amount"].append((amt_r, bp, max_uses))
                cluster_left = amt_r.x

            # Uses
            if unlimited:
                u_txt, u_col = "Unlimited", C_DIM
            else:
                u_txt = f"{int(uses)} use{'s' if int(uses) != 1 else ''} left"
                u_col = (255, 160, 60) if uses <= 3 else C_TEXT
            uf = F(10)
            us = uf.render(u_txt, True, u_col)
            u_x = cluster_left - S(10) - us.get_width()
            screen.blit(us, (u_x, row.y + (row.h - us.get_height()) // 2))

            # Name + cost summary
            text_x = (badge.right + S(10)) if has_tech else (row.x + S(12))
            is_child = bool(bp.get("child", False))
            if is_child:
                text_x += S(10)
            name = str(bp.get("output_name") or bp.get("display_name", "Unknown"))
            if is_child:
                name = "> " + name
            avail = u_x - S(10) - text_x
            nf = IF(12, bold=True)
            text(nf, _fit_text(nf, name, avail),
                 C_DIM if exhausted else C_VALUE, text_x, row.y + S(4))
            cf = F(10)
            text(cf, _fit_text(cf, _cost_summary(rm, bp), avail), C_DIM,
                 text_x, row.y + S(4) + nf.get_height() + S(1))

            new_bp_rects.append((abs_idx, row, bp))
    host._ps_cb_bp_rects = new_bp_rects

    _publish_scrollbar(
        host, pygame, screen, "bp", bp_overflow, list_rect,
        host._ps_cb_bp_scroll, bp_max, bp_total, bp_vis, BP_ROW_H,
        SB_W, S, C_BG, C_ACCENT, C_STEEL, C_VALUE, mx, my)

    hint_f = F(10)
    hint = hint_f.render(_fit_text(
        hint_f, "Click a blueprint: full costs  |  Right-click: more build "
        "options  |  Hover a job: requirements", W), True, C_DIM)
    screen.blit(hint, (x0, list_rect.bottom + S(3)))

    # ── Blueprint popup ──────────────────────────────────────────────────
    popup = host._ps_cb_bp_popup
    if popup is None and hover_proj is not None:
        hp, hcard = hover_proj
        reqs = _requirements_for(hp, blueprints)
        qty = int(_num(hp.get("qty"), 1) or 1)
        notes = [("Per unit" + (f"  (x{qty} queued)" if qty > 1 else ""),
                  C_DIM)]
        if reqs is None:
            reqs = {}
            notes.append(("Requirements unavailable for this job.", C_AMBER))
        _draw_popup(host, pygame, screen, rm,
                    {"bp": reqs, "y": hcard.y,
                     "title": str(hp.get("name", "Project")),
                     "notes": notes,
                     "labels": ("Already paid (initial)",
                                "Still drawn while building (periodic)")},
                    body, win_w, S, F, IF, C_PLATE, C_HEADER, C_STEEL,
                    C_ACCENT, C_VALUE, C_TEXT, C_DIM)
    if popup is not None:
        _draw_popup(host, pygame, screen, rm, popup, body, win_w, S, F, IF,
                    C_PLATE, C_HEADER, C_STEEL, C_ACCENT, C_VALUE, C_TEXT,
                    C_DIM)

    # ── Cancel-confirm modal ─────────────────────────────────────────────
    confirm = host._ps_cb_cancel_confirm
    if confirm is not None:
        _draw_cancel_confirm(host, pygame, screen, confirm, S, F, IF,
                             C_BG, C_STEEL, C_VALUE, C_WARN, mx, my)


def _publish_scrollbar(host, pygame, screen, which, overflow, area, scroll,
                       max_scroll, total, visible, row_h, sb_w, S, c_track,
                       c_accent, c_steel, c_value, mx, my):
    """Publish geometry under the attribute names the game's drag code reads."""
    prefix = f"_ps_cb_{which}_sb_"
    if not overflow:
        setattr(host, prefix + "track_rect", None)
        setattr(host, prefix + "thumb_rect", None)
        setattr(host, prefix + "thumb_travel", 0)
        setattr(host, prefix + "max_scroll", 0)
        setattr(host, prefix + "dragging", False)
        return
    track = pygame.Rect(area.right - sb_w - S(2), area.y, sb_w, area.h)
    pygame.draw.rect(screen, c_track, track, border_radius=2)
    thumb_h = max(S(18), int(area.h * visible * row_h / max(1, total * row_h)))
    travel = max(1, area.h - thumb_h)
    thumb_y = area.y + (int((scroll / max_scroll) * travel)
                        if max_scroll > 0 else 0)
    thumb = pygame.Rect(track.x, thumb_y, sb_w, thumb_h)
    hot = getattr(host, prefix + "dragging", False) or thumb.collidepoint(mx, my)
    pygame.draw.rect(screen, c_accent if hot else c_steel, thumb, border_radius=2)
    pygame.draw.rect(screen, c_value if hot else c_accent, thumb, 1,
                     border_radius=2)
    setattr(host, prefix + "track_rect", track)
    setattr(host, prefix + "thumb_rect", thumb)
    setattr(host, prefix + "thumb_travel", travel)
    setattr(host, prefix + "max_scroll", max_scroll)


def _draw_popup(host, pygame, screen, rm, popup, body, win_w, S, F, IF,
                c_plate, c_header, c_steel, c_accent, c_value, c_text, c_dim):
    bp = popup.get("bp") or {}
    initial, periodic = _cost_groups(bp)
    title_f, body_f = F(12, bold=True), F(11)
    lines = [(str(popup.get("title") or bp.get("display_name", "?")),
              c_value, title_f)]
    for note, note_col in popup.get("notes") or ():
        lines.append((note, note_col, body_f))
    labels = popup.get("labels") or (
        getattr(rm, "_INITIAL_COST_LABEL", "Initial Cost"),
        getattr(rm, "_PERIODIC_COST_LABEL", "Periodic Cost"))
    sec_col = getattr(rm, "_COST_SECTION_COLOR", (100, 150, 200))
    cost_col = (180, 220, 255)
    if bp.get("manhours", 0):
        lines.append((f"Manhours: {_fmt_value(rm, bp['manhours'])}", cost_col, body_f))
    for label, group in ((labels[0], initial), (labels[1], periodic)):
        if not group:
            continue
        lines.append((label.upper(), sec_col, body_f))
        for name, value in group:
            lines.append((f"  {name}: {_fmt_value(rm, value)}", cost_col, body_f))
    desc = str(bp.get("description", "") or "").strip()
    if desc:
        cur = ""
        for word in desc.split():
            if len(cur) + len(word) + 1 > 34:
                lines.append((cur.strip(), (140, 170, 210), body_f))
                cur = word + " "
            else:
                cur += word + " "
        if cur.strip():
            lines.append((cur.strip(), (140, 170, 210), body_f))

    pad = S(8)
    pw = max(f.size(t)[0] for t, _, f in lines) + pad * 2 + S(6)
    ph = sum(f.get_linesize() + S(2) for _, _, f in lines) + pad * 2
    px = body.right + S(6)
    if px + pw > win_w:
        px = max(0, body.x - pw - S(6))
    py = max(body.y, min(body.bottom - ph, popup.get("y", body.y + S(80))))
    bg = pygame.Surface((pw, ph), pygame.SRCALPHA)
    bg.fill((*c_plate, 248))
    screen.blit(bg, (px, py))
    pygame.draw.rect(screen, c_steel, (px, py, pw, ph), 1)
    pygame.draw.rect(screen, (*c_header,), (px + 1, py + 1, pw - 2, title_f.get_linesize() + pad))
    pygame.draw.line(screen, c_accent, (px + 1, py + title_f.get_linesize() + pad),
                     (px + pw - 2, py + title_f.get_linesize() + pad))
    cy = py + pad
    for txt, col, font in lines:
        screen.blit(font.render(txt, True, col), (px + pad, cy))
        cy += font.get_linesize() + S(2)


def _draw_cancel_confirm(host, pygame, screen, confirm, S, F, IF, c_btn,
                         c_steel, c_value, c_warn, mx, my):
    W, H = S(420), S(170)
    sw, sh = screen.get_size()
    panel = pygame.Rect((sw - W) // 2, (sh - H) // 2, W, H)
    try:
        plate = host._hud_plate_layers(W, H, S(34), chamfer_max=S(12))
        screen.blit(plate["plate"], panel.topleft)
    except Exception:
        bg = pygame.Surface((W, H), pygame.SRCALPHA)
        bg.fill((8, 14, 26, 250))
        screen.blit(bg, panel.topleft)
        pygame.draw.rect(screen, c_steel, panel, 1)
    pygame.draw.line(screen, c_warn, panel.topleft,
                     (panel.right - S(12), panel.y), 2)

    qty = int(_num(confirm.get("qty"), 1) or 1)
    name = str(confirm.get("name", "this project"))
    if qty > 1:
        name += f" (x{qty})"
    ty = panel.y + S(14)
    for txt, font, col in (
            ("CANCEL CONSTRUCTION?", IF(12, bold=True), c_warn),
            (name, IF(11), c_value),
            ("Progress is lost. Materials and credits already",
             F(10), (170, 150, 155)),
            ("spent are NOT refunded; item ingredients are returned.",
             F(10), (170, 150, 155))):
        surf = font.render(txt, True, col)
        screen.blit(surf, (panel.x + (W - surf.get_width()) // 2, ty))
        ty += surf.get_height() + S(3)

    bh, bw, gap = S(26), S(110), S(12)
    by = panel.bottom - bh - S(14)
    no_r = pygame.Rect(panel.x + (W - bw * 2 - gap) // 2, by, bw, bh)
    yes_r = pygame.Rect(no_r.right + gap, by, bw, bh)
    for rect, label, fill, border, fg in (
            (no_r, "KEEP BUILDING", c_btn, c_steel, c_value),
            (yes_r, "CANCEL JOB", (64, 22, 28), c_warn, (255, 205, 205))):
        if rect.collidepoint(mx, my):
            fill = tuple(min(255, c + 22) for c in fill)
        pygame.draw.rect(screen, fill, rect)
        pygame.draw.rect(screen, border, rect, 1)
        ls = IF(10, bold=True).render(label, True, fg)
        screen.blit(ls, (rect.x + (rect.w - ls.get_width()) // 2,
                         rect.y + (rect.h - ls.get_height()) // 2))
    confirm["panel_rect"] = panel
    confirm["yes_rect"] = yes_r
    confirm["no_rect"] = no_r


# ── Patch + input ────────────────────────────────────────────────────────

def _make_draw_station_overlay(pygame, original):
    def patched(self, ctx):
        if _st["failed"]:
            return original(self, ctx)
        ov = self._station_overlay
        if not (ov is not None and ov.get("kind") == "player_station"
                and self._ps_tab == CB_TAB):
            _clear_hits()
            _was_active[0] = False
            return original(self, ctx)

        # Hide the game's own modal/popup while it draws the (soon replaced)
        # body, so they are not painted underneath our version.
        saved_confirm = self._ps_cb_cancel_confirm
        saved_popup = self._ps_cb_bp_popup
        self._ps_cb_cancel_confirm = None
        self._ps_cb_bp_popup = None
        try:
            result = original(self, ctx)
        finally:
            self._ps_cb_cancel_confirm = saved_confirm
            self._ps_cb_bp_popup = saved_popup

        if self._ps_tab != CB_TAB:       # original may have bounced the tab
            _clear_hits()
            return result
        try:
            _draw_construction(self, pygame, ctx.screen)
        except Exception:
            _st["failed"] = True
            _clear_hits()
            logger = _st["logger"]
            if logger is not None:
                logger.exception(
                    "construction overlay draw failed; falling back to vanilla")
        return result

    return patched


def _on_event(host, pygame, event, screen):
    if _st["failed"] or event.type != pygame.MOUSEBUTTONDOWN or event.button != 1:
        return None
    if (getattr(host, "_disconnected", False)
            or getattr(host, "_esc_menu_open", False)
            or getattr(host, "_options_open", False)):
        return None
    ov = host._station_overlay
    if not (ov is not None and ov.get("kind") == "player_station"
            and host._ps_tab == CB_TAB):
        return None
    # Modals own the click: let the game handle them.
    if (host._ps_cb_cancel_confirm is not None
            or host._ps_cb_ctx_menu is not None
            or getattr(host, "_ps_transfer_dialog", None) is not None):
        return None
    body = ov.get("body_rect")
    if body is None or not body.collidepoint(event.pos):
        return None

    pos = event.pos
    sid = (ov.get("data") or {}).get("attached_station_id")
    if sid is None:
        return None

    for pid, pau_r, can_r, proj in _hits["proj"]:
        if pau_r.collidepoint(pos):
            host._send_json("PS_CB_PAUSE_PROJECT", {
                "station_id": sid, "project_id": pid,
                "paused": not bool(proj.get("paused")),
            })
            _last_poll[0] = time.monotonic() - POLL_S + 1.5
            return True
        if can_r.collidepoint(pos):
            host._ps_cb_selected_proj = pid
            host._ps_cb_search_focus = False
            host._ps_cb_cancel_confirm = {
                "station_id": sid, "project_id": pid,
                "name": proj.get("name", "this project"),
                "qty": int(_num(proj.get("qty"), 1) or 1),
            }
            return True

    for rect, bp, exhausted in _hits["build"]:
        if rect.collidepoint(pos):
            if not exhausted:
                host._make_cb_build(bp.get("id", ""), sid)()
                _last_poll[0] = time.monotonic() - POLL_S + 1.5
            return True

    for rect, bp, max_uses in _hits["amount"]:
        if rect.collidepoint(pos):
            host._make_cb_build_amount(
                bp.get("id", ""), sid, bp.get("display_name", "?"), max_uses)()
            return True

    for rect, value in _hits["chips"]:
        if rect.collidepoint(pos):
            _tech_filter[0] = value
            host._ps_cb_bp_scroll = 0
            return True

    clr = _hits["clear"]
    if clr is not None and clr.collidepoint(pos):
        host._ps_cb_search = ""
        host._ps_cb_bp_scroll = 0
        cursors = getattr(host, "_text_cursors", None)
        if isinstance(cursors, dict):
            cursors.pop("ps_cb_search", None)
        host._ps_cb_search_focus = True
        return True
    return None


def _on_startup(host, pygame, screen):
    cls = type(host)
    original = cls._draw_station_overlay
    _st["cls"] = cls
    _st["orig"] = original
    _st["rm"] = sys.modules[original.__module__]
    cls._draw_station_overlay = _make_draw_station_overlay(pygame, original)


def _on_shutdown(*_args, **_kwargs):
    if _st["cls"] is not None and _st["orig"] is not None:
        _st["cls"]._draw_station_overlay = _st["orig"]


def apply(api):
    _st["logger"] = api.logger
    api.on("client.startup", _on_startup)
    api.on("client.event", _on_event)
    api.on("loader.shutdown", _on_shutdown)
    api.logger.info("station-construction-overhaul ready, waiting for client.startup")
