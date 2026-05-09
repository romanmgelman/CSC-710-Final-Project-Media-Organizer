import os
import json
import math
import shutil
import threading
import hashlib
import platform
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import customtkinter as ctk

ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")

import geo

try:
    from exif import Image as ExifImage
    HAS_EXIF = True
except ImportError:
    HAS_EXIF = False

try:
    from hachoir.parser import createParser
    from hachoir.metadata import extractMetadata
    import hachoir.core.config as hachoir_config
    hachoir_config.quiet = True
    HAS_HACHOIR = True
except ImportError:
    HAS_HACHOIR = False

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif",
    ".webp", ".bmp", ".gif", ".raw", ".cr2", ".cr3", ".nef",
    ".arw", ".dng", ".orf", ".rw2", ".raf",
    ".mov", ".mp4", ".m4v", ".avi", ".mkv", ".wmv", ".flv", ".3gp",
    ".mts", ".m2ts",
}

SORT_BY_DATE          = "date"
SORT_BY_LOCATION      = "location"
SORT_BY_DATE_LOCATION = "date_location"

SORT_MODE_LABELS = {
    SORT_BY_DATE:          "Date (Year / Month)",
    SORT_BY_LOCATION:      "Location (Country)",
    SORT_BY_DATE_LOCATION: "Date + Location (Year / Month / Country)",
}
SORT_LABEL_TO_MODE = {v: k for k, v in SORT_MODE_LABELS.items()}

UNKNOWN_LOCATION = "Unknown"

# Treeview colors — CTk handles its own widgets; ttk.Treeview needs manual theming.
_TREE = {
    "Light": {
        "bg":      "#FFFFFF",
        "fg":      "#09090B",
        "sel_bg":  "#6366F1",
        "sel_fg":  "#FFFFFF",
        "hd_bg":   "#F8F8F9",
        "hd_fg":   "#71717A",
        "skip_fg": "#D97706",
    },
    "Dark": {
        "bg":      "#111111",
        "fg":      "#F4F4F5",
        "sel_bg":  "#6366F1",
        "sel_fg":  "#FFFFFF",
        "hd_bg":   "#161616",
        "hd_fg":   "#52525B",
        "skip_fg": "#F59E0B",
    },
}


# -------------------------------------------------------------
# date extraction
# -------------------------------------------------------------

def _parse_exif_datetime(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y:%m:%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def get_date_from_exif(path):
    if not HAS_EXIF:
        return None
    try:
        with open(path, "rb") as f:
            img = ExifImage(f)
        if not img.has_exif:
            return None
        for tag in ("datetime_original", "datetime_digitized", "datetime"):
            if tag in dir(img):
                try:
                    dt = _parse_exif_datetime(getattr(img, tag))
                    if dt:
                        return dt
                except Exception:
                    continue
    except Exception:
        pass
    return None


def get_date_from_hachoir(path):
    if not HAS_HACHOIR:
        return None
    try:
        parser = createParser(str(path))
        if not parser:
            return None
        with parser:
            metadata = extractMetadata(parser)
            if not metadata:
                return None
            for key in ("creation_date", "last_modification"):
                if metadata.has(key):
                    val = metadata.get(key)
                    if isinstance(val, datetime):
                        return val
    except Exception:
        pass
    return None


def get_date_from_mtime(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except Exception:
        return None


def get_file_date(path):
    dt = get_date_from_exif(path)
    if dt:
        return dt, "exif"
    dt = get_date_from_hachoir(path)
    if dt:
        return dt, "hachoir"
    dt = get_date_from_mtime(path)
    if dt:
        return dt, "mtime"
    return None, "none"


def hash_file(path, chunk_size=1024 * 1024):
    sha = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                sha.update(chunk)
        return sha.hexdigest()
    except Exception:
        return None


# -------------------------------------------------------------
# destination layout
# -------------------------------------------------------------

def _safe_folder_name(name):
    bad = '<>:"/\\|?*'
    cleaned = "".join(c for c in name if c not in bad).strip()
    return cleaned or UNKNOWN_LOCATION


def _compute_dest_folder(dest, dt, country, sort_mode):
    country_folder = _safe_folder_name(country) if country else UNKNOWN_LOCATION
    if sort_mode == SORT_BY_DATE:
        return dest / f"{dt.year:04d}" / MONTH_NAMES[dt.month - 1]
    if sort_mode == SORT_BY_LOCATION:
        return dest / country_folder
    return dest / f"{dt.year:04d}" / MONTH_NAMES[dt.month - 1] / country_folder


def build_plan(source, dest, recursive, rename, sort_mode, progress_cb=None):
    plan = []
    used_dsts = set()
    seen_hashes = {}

    if recursive:
        candidates = [p for p in source.rglob("*") if p.is_file()]
    else:
        candidates = [p for p in source.iterdir() if p.is_file()]
    candidates = [p for p in candidates if p.suffix.lower() in MEDIA_EXTS]

    total = len(candidates)
    for i, src in enumerate(candidates):
        if progress_cb:
            progress_cb(i, total, src.name)

        file_hash = hash_file(src)
        duplicate = False

        if file_hash:
            if file_hash in seen_hashes:
                duplicate = True
            else:
                seen_hashes[file_hash] = src

        dt, date_tag = get_file_date(src)
        coords, gps_tag = geo.get_file_gps(src)
        if coords:
            lat, lng = coords
            country, city = geo.get_location_name(lat, lng)
        else:
            lat = lng = country = city = None

        skip_reason = None
        if duplicate:
            skip_reason = "duplicate"
        elif sort_mode == SORT_BY_DATE and not dt:
            skip_reason = "no date"
        elif sort_mode == SORT_BY_DATE_LOCATION and not dt:
            skip_reason = "no date"

        if skip_reason:
            plan.append({
                "src": src, "dst": None,
                "year": None, "month": None, "date": None, "source_tag": date_tag,
                "lat": lat, "lng": lng, "country": country, "city": city,
                "gps_tag": gps_tag, "skipped": True, "reason": skip_reason,
            })
            continue

        folder = _compute_dest_folder(dest, dt, country, sort_mode)

        if rename:
            if dt:
                base = f"{dt.month:02d}_{dt.day:02d}_{dt.year:04d}"
                ext = src.suffix.lower()
                candidate = folder / f"{base}{ext}"
                if candidate in used_dsts or candidate.exists():
                    stamp = dt.strftime("%H%M%S")
                    candidate = folder / f"{base}_{stamp}{ext}"
                n = 1
                while candidate in used_dsts or candidate.exists():
                    candidate = folder / f"{base}_{stamp}_{n}{ext}"
                    n += 1
                dst = candidate
            else:
                candidate = folder / src.name
                n, stem, ext = 1, src.stem, src.suffix
                while candidate in used_dsts or candidate.exists():
                    candidate = folder / f"{stem}_{n}{ext}"
                    n += 1
                dst = candidate
        else:
            candidate = folder / src.name
            n, stem, ext = 1, src.stem, src.suffix
            while candidate in used_dsts or candidate.exists():
                candidate = folder / f"{stem}_{n}{ext}"
                n += 1
            dst = candidate

        used_dsts.add(dst)
        plan.append({
            "src": src, "dst": dst,
            "year": f"{dt.year:04d}" if dt else None,
            "month": MONTH_NAMES[dt.month - 1] if dt else None,
            "date": dt, "source_tag": date_tag,
            "lat": lat, "lng": lng,
            "country": country if country else UNKNOWN_LOCATION,
            "city": city, "gps_tag": gps_tag,
            "skipped": False, "reason": "",
        })

    if progress_cb:
        progress_cb(total, total, "")
    return plan


def execute_plan(plan, operation, progress_cb=None):
    ok, err, errors = 0, 0, []
    todo  = [p for p in plan if not p["skipped"]]
    total = len(todo)

    for i, item in enumerate(todo):
        if progress_cb:
            progress_cb(i, total, item["src"].name)
        try:
            item["dst"].parent.mkdir(parents=True, exist_ok=True)
            if operation == "copy":
                shutil.copy2(item["src"], item["dst"])
            else:
                shutil.move(str(item["src"]), str(item["dst"]))
            ok += 1
        except Exception as e:
            err += 1
            errors.append(f"{item['src'].name}: {e}")

    if progress_cb:
        progress_cb(total, total, "")
    return ok, err, errors


# -------------------------------------------------------------
# Rounded scrollbar (canvas-based, no arrows)
# -------------------------------------------------------------

class RoundedScrollbar(tk.Canvas):
    """Thin, pill-shaped scrollbar drawn on a Canvas — no arrows, rounded thumb."""

    def __init__(self, parent, orient="vertical", command=None,
                 thumb_color="#3F3F46", hover_color="#71717A",
                 bg_color="#111111", radius=4, thickness=8, **kw):
        if orient == "vertical":
            kw["width"] = thickness
        else:
            kw["height"] = thickness
        super().__init__(parent, bd=0, highlightthickness=0, bg=bg_color, **kw)
        self._command     = command
        self._orient      = orient
        self._radius      = radius
        self._thumb_color = thumb_color
        self._hover_color = hover_color
        self._hovering    = False
        self._first       = 0.0
        self._last        = 1.0
        self._drag_pos    = None
        self._drag_first  = None

        self.bind("<Configure>",       lambda _: self._redraw())
        self.bind("<ButtonPress-1>",   self._on_press)
        self.bind("<B1-Motion>",       self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", lambda _: self._set_hover(True))
        self.bind("<Leave>", lambda _: self._set_hover(False))

    # Public API — same signature as ttk.Scrollbar
    def set(self, first, last):
        self._first = float(first)
        self._last  = float(last)
        self._redraw()

    def update_colors(self, thumb, hover, bg):
        self._thumb_color = thumb
        self._hover_color = hover
        self.configure(bg=bg)
        self._redraw()

    # ── Drawing ───────────────────────────────────────────────────────

    def _redraw(self):
        self.delete("thumb")
        W, H = self.winfo_width(), self.winfo_height()
        if W < 2 or H < 2:
            return
        if self._orient == "vertical":
            x0, x1 = 1, W - 1
            y0 = max(0,  H * self._first)
            y1 = min(H,  H * self._last)
        else:
            y0, y1 = 1, H - 1
            x0 = max(0,  W * self._first)
            x1 = min(W,  W * self._last)
        if (y1 - y0) < 2 or (x1 - x0) < 2:
            return
        r    = min(self._radius, int((x1 - x0) / 2), int((y1 - y0) / 2))
        pts  = self._pill_pts(x0, y0, x1, y1, r)
        fill = self._hover_color if self._hovering else self._thumb_color
        self.create_polygon(pts, fill=fill, outline="", tags="thumb", smooth=False)

    @staticmethod
    def _pill_pts(x0, y0, x1, y1, r, steps=12):
        pts = []
        for (cx, cy, a0) in [(x1-r, y0+r, -90), (x1-r, y1-r, 0),
                              (x0+r, y1-r,  90), (x0+r, y0+r, 180)]:
            for i in range(steps + 1):
                a = math.radians(a0 + 90 * i / steps)
                pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        return pts

    # ── Events ────────────────────────────────────────────────────────

    def _set_hover(self, on):
        self._hovering = on
        self._redraw()

    def _thumb_range(self):
        W, H = self.winfo_width(), self.winfo_height()
        if self._orient == "vertical":
            return H * self._first, H * self._last
        return W * self._first, W * self._last

    def _on_press(self, event):
        pos = event.y if self._orient == "vertical" else event.x
        a, b = self._thumb_range()
        if a <= pos <= b:
            self._drag_pos   = pos
            self._drag_first = self._first
        elif pos < a:
            self._command("scroll", -1, "pages")
        else:
            self._command("scroll",  1, "pages")

    def _on_motion(self, event):
        if self._drag_pos is None:
            return
        pos   = event.y if self._orient == "vertical" else event.x
        W, H  = self.winfo_width(), self.winfo_height()
        total = H if self._orient == "vertical" else W
        delta = (pos - self._drag_pos) / total
        self._command("moveto", self._drag_first + delta)

    def _on_release(self, _):
        self._drag_pos = self._drag_first = None


# -------------------------------------------------------------
# GUI
# -------------------------------------------------------------

class MoApp(ctk.CTk):
    # (light, dark) color pairs for CTk widgets
    _WIN    = ("#FAFAFA",  "#0D0D0D")
    _SIDE   = ("#F4F4F5",  "#111111")
    _SURF   = ("#FFFFFF",  "#161616")
    _RULE   = ("#E4E4E7",  "#1E1E1E")
    _BDR    = ("#D4D4D8",  "#2A2A2A")
    _TXT1   = ("#09090B",  "#F4F4F5")
    _TXT2   = ("#71717A",  "#A1A1AA")
    _TXT3   = ("#A1A1AA",  "#3F3F46")
    _ENTY   = ("#FFFFFF",  "#0D0D0D")
    _EBDR   = ("#D4D4D8",  "#2A2A2A")
    _ACC    = ("#6366F1",  "#818CF8")
    _ACCHOV = ("#4F46E5",  "#6366F1")

    def __init__(self):
        super().__init__()
        self.title("mo")
        self.geometry("1120x760")
        self.minsize(920, 600)

        self.plan = []
        self.worker_queue = Queue()

        self._setup_tree_style()
        self._build_ui()
        self._poll_queue()

    # ── Fonts ─────────────────────────────────────────────────────────

    @staticmethod
    def _font(size=14, weight="normal"):
        return ctk.CTkFont(size=size, weight=weight)

    @staticmethod
    def _mono(size=13):
        s   = platform.system()
        fam = {"Darwin": "Menlo", "Windows": "Consolas"}.get(s, "DejaVu Sans Mono")
        return ctk.CTkFont(family=fam, size=size)

    # ── Treeview theming ──────────────────────────────────────────────

    def _setup_tree_style(self):
        mode = ctk.get_appearance_mode()
        c    = _TREE.get(mode, _TREE["Light"])
        self._skip_fg = c["skip_fg"]
        self._tree_bg = c["bg"]

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("Mo.Treeview",
            background=c["bg"],
            foreground=c["fg"],
            fieldbackground=c["bg"],
            rowheight=32,
            borderwidth=0,
            relief="flat",
        )
        style.configure("Mo.Treeview.Heading",
            background=c["hd_bg"],
            foreground=c["hd_fg"],
            borderwidth=0,
            relief="flat",
            padding=(10, 8),
        )
        style.map("Mo.Treeview",
            background=[("selected", c["sel_bg"])],
            foreground=[("selected", c["sel_fg"])],
        )
        style.layout("Mo.Treeview", [
            ("Mo.Treeview.treearea", {"sticky": "nsew"})
        ])

        # ── Scrollbars ────────────────────────────────────────────────
        # Remove arrows so only the thumb floats on the track.
        style.layout("Mo.Vertical.TScrollbar", [
            ("Vertical.TScrollbar.trough", {
                "sticky": "nsew",
                "children": [("Vertical.TScrollbar.thumb",
                               {"expand": "1", "sticky": "nsew"})],
            })
        ])
        style.layout("Mo.Horizontal.TScrollbar", [
            ("Horizontal.TScrollbar.trough", {
                "sticky": "nsew",
                "children": [("Horizontal.TScrollbar.thumb",
                               {"expand": "1", "sticky": "nsew"})],
            })
        ])

        if mode == "Dark":
            sb_thumb  = "#3F3F46"   # zinc-600
            sb_active = "#71717A"   # zinc-500  — brightens on hover
            sb_trough = c["bg"]     # invisible track, thumb just floats
        else:
            sb_thumb  = "#C4C4C8"
            sb_active = "#8E8E93"
            sb_trough = c["bg"]

        for s in ("Mo.Vertical.TScrollbar", "Mo.Horizontal.TScrollbar"):
            style.configure(s,
                background=sb_thumb,
                troughcolor=sb_trough,
                bordercolor=sb_trough,
                darkcolor=sb_thumb,
                lightcolor=sb_thumb,
                gripcount=0,
                relief="flat",
                borderwidth=0,
                width=7,
            )
            style.map(s,
                background=[
                    ("active",  sb_active),
                    ("pressed", sb_active),
                    ("!active", sb_thumb),
                ],
                darkcolor=[("active", sb_active), ("!active", sb_thumb)],
                lightcolor=[("active", sb_active), ("!active", sb_thumb)],
            )

    # ── UI structure ──────────────────────────────────────────────────

    def _build_ui(self):
        self.configure(fg_color=self._WIN)
        # col 0 = sidebar, col 1 = 1 px separator, col 2 = main content
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=0, minsize=1)
        self.grid_columnconfigure(2, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)

        self._build_sidebar()

        sep = ctk.CTkFrame(self, width=1, corner_radius=0, fg_color=self._RULE)
        sep.grid(row=0, column=1, sticky="nsew")

        self._build_main()
        self._build_statusbar()

    # ── Sidebar ───────────────────────────────────────────────────────

    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=290, corner_radius=0, fg_color=self._SIDE)
        sb.grid(row=0, column=0, sticky="nsew")
        sb.pack_propagate(False)

        H = dict(padx=20)

        # ── App mark + theme toggle ───────────────────────────────────
        mark = ctk.CTkFrame(sb, fg_color="transparent")
        mark.pack(fill="x", padx=20, pady=(18, 14))

        ctk.CTkLabel(mark, text="mo",
                     font=self._font(20, "bold"),
                     text_color=self._TXT1).pack(side="left")
        ctk.CTkLabel(mark, text="  media organizer",
                     font=self._font(12),
                     text_color=self._TXT3).pack(side="left", pady=(4, 0))

        self._theme_btn = ctk.CTkButton(
            mark, text="☀", width=28, height=28, corner_radius=6,
            fg_color="transparent", hover_color=self._RULE,
            text_color=self._TXT2, font=self._font(14),
            command=self._toggle_theme,
        )
        self._theme_btn.pack(side="right")

        self._hrule(sb, top=0)

        # ── Folders ───────────────────────────────────────────────────
        self._slabel(sb, "SOURCE FOLDER", top=14)
        r1 = ctk.CTkFrame(sb, fg_color="transparent")
        r1.pack(fill="x", **H, pady=(5, 0))
        r1.grid_columnconfigure(0, weight=1)

        self.source_var = tk.StringVar()
        ctk.CTkEntry(
            r1, textvariable=self.source_var,
            placeholder_text="Choose folder…",
            font=self._mono(12), height=32, corner_radius=7,
            fg_color=self._ENTY, border_color=self._EBDR, border_width=1,
        ).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            r1, text="…", width=32, height=32, corner_radius=7,
            fg_color=self._SURF, hover_color=self._RULE,
            text_color=self._TXT2, border_width=1, border_color=self._EBDR,
            command=self.pick_source,
        ).grid(row=0, column=1, padx=(5, 0))

        self._slabel(sb, "DESTINATION", top=12)
        r2 = ctk.CTkFrame(sb, fg_color="transparent")
        r2.pack(fill="x", **H, pady=(5, 0))
        r2.grid_columnconfigure(0, weight=1)

        self.dest_var = tk.StringVar()
        ctk.CTkEntry(
            r2, textvariable=self.dest_var,
            placeholder_text="Choose folder…",
            font=self._mono(12), height=32, corner_radius=7,
            fg_color=self._ENTY, border_color=self._EBDR, border_width=1,
        ).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            r2, text="…", width=32, height=32, corner_radius=7,
            fg_color=self._SURF, hover_color=self._RULE,
            text_color=self._TXT2, border_width=1, border_color=self._EBDR,
            command=self.pick_dest,
        ).grid(row=0, column=1, padx=(5, 0))

        self._hrule(sb, top=14)

        # ── Operation ─────────────────────────────────────────────────
        self._slabel(sb, "OPERATION", top=14)
        self.operation_var = tk.StringVar(value="copy")
        self._op_seg = ctk.CTkSegmentedButton(
            sb, values=["Copy", "Move"],
            command=lambda v: self.operation_var.set(v.lower()),
            font=self._font(13), height=32,
            fg_color=self._SURF,
            selected_color=self._ACC, selected_hover_color=self._ACCHOV,
            unselected_color=self._SURF, unselected_hover_color=self._RULE,
        )
        self._op_seg.set("Copy")
        self._op_seg.pack(fill="x", **H, pady=(5, 0))

        # ── Options ───────────────────────────────────────────────────
        self._slabel(sb, "OPTIONS", top=14)
        self.recursive_var = tk.BooleanVar(value=True)
        self.rename_var    = tk.BooleanVar(value=False)
        sw = dict(font=self._font(13), onvalue=True, offvalue=False)
        ctk.CTkSwitch(sb, text="Include subfolders",
                      variable=self.recursive_var, **sw).pack(
            anchor="w", **H, pady=(7, 0))
        ctk.CTkSwitch(sb, text="Rename files",
                      variable=self.rename_var, **sw).pack(
            anchor="w", **H, pady=(7, 0))

        self._hrule(sb, top=14)

        # ── Organize by ───────────────────────────────────────────────
        self._slabel(sb, "ORGANIZE BY", top=14)
        self.sort_mode_var = tk.StringVar(value=SORT_BY_DATE)
        _map = {"Date": SORT_BY_DATE,
                "Location": SORT_BY_LOCATION,
                "Combined": SORT_BY_DATE_LOCATION}
        self._sort_seg = ctk.CTkSegmentedButton(
            sb, values=list(_map.keys()),
            command=lambda v: self.sort_mode_var.set(_map[v]),
            font=self._font(13), height=32,
            fg_color=self._SURF,
            selected_color=self._ACC, selected_hover_color=self._ACCHOV,
            unselected_color=self._SURF, unselected_hover_color=self._RULE,
        )
        self._sort_seg.set("Date")
        self._sort_seg.pack(fill="x", **H, pady=(5, 0))
        ctk.CTkLabel(sb, text="GPS required for location modes",
                     font=self._font(11), text_color=self._TXT3,
                     anchor="w").pack(anchor="w", **H, pady=(4, 0))

    # ── Main content ──────────────────────────────────────────────────

    def _build_main(self):
        main = ctk.CTkFrame(self, corner_radius=0, fg_color=self._WIN)
        main.grid(row=0, column=2, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        # Header: label (col 0) | badge (col 1, expands) | buttons (col 2)
        hdr = ctk.CTkFrame(main, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 10))
        hdr.grid_columnconfigure(1, weight=1)

        self._hdr_lbl = ctk.CTkLabel(hdr, text="PREVIEW",
                                      font=self._font(10, "bold"),
                                      text_color=self._TXT3, anchor="w")
        self._hdr_lbl.grid(row=0, column=0, sticky="w")

        self._badge_var = tk.StringVar(value="")
        self._badge_lbl = ctk.CTkLabel(
            hdr, textvariable=self._badge_var,
            font=self._font(11),
            text_color=self._TXT2,
            corner_radius=5,
            fg_color=self._SURF,
            padx=8,
        )
        self._badge_lbl.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self._badge_lbl.grid_remove()  # hidden until there's content

        # Action buttons (right-aligned in header)
        btns = ctk.CTkFrame(hdr, fg_color="transparent")
        btns.grid(row=0, column=2, sticky="e")

        _bkw = dict(height=30, corner_radius=7, font=self._font(12))

        self.preview_btn = ctk.CTkButton(
            btns, text="Preview", width=80,
            fg_color=self._ACC, hover_color=self._ACCHOV,
            text_color="#FFFFFF",
            command=self.run_preview, **_bkw,
        )
        self.preview_btn.pack(side="left", padx=(0, 6))

        self.apply_btn = ctk.CTkButton(
            btns, text="Apply", width=72,
            fg_color=self._SURF, hover_color=self._RULE,
            text_color=self._TXT3, border_width=1, border_color=self._EBDR,
            state="disabled",
            command=self.run_apply, **_bkw,
        )
        self.apply_btn.pack(side="left", padx=(0, 6))

        self.undo_btn = ctk.CTkButton(
            btns, text="Undo", width=68,
            fg_color=self._SURF, hover_color=self._RULE,
            text_color=self._TXT2, border_width=1, border_color=self._EBDR,
            command=self.undo_last_operation, **_bkw,
        )
        self.undo_btn.pack(side="left")

        # Tree container
        self._tree_card = ctk.CTkFrame(
            main, corner_radius=8,
            fg_color=("#FFFFFF", "#111111"),
            border_width=1, border_color=self._RULE,
        )
        self._tree_card.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 0))
        self._tree_card.grid_columnconfigure(0, weight=1)
        self._tree_card.grid_rowconfigure(0, weight=1)

        cols = ("new_name", "date", "location", "source")
        self.tree = ttk.Treeview(self._tree_card, columns=cols, style="Mo.Treeview")

        self.tree.heading("#0",       text="Folder / File", anchor="w")
        self.tree.heading("new_name", text="New Name",      anchor="w")
        self.tree.heading("date",     text="Date",          anchor="w")
        self.tree.heading("location", text="Location",      anchor="w")
        self.tree.heading("source",   text="Metadata",      anchor="center")

        self.tree.column("#0",        width=280, minwidth=160, anchor="w")
        self.tree.column("new_name",  width=200, minwidth=100, anchor="w")
        self.tree.column("date",      width=160, minwidth=100, anchor="w")
        self.tree.column("location",  width=160, minwidth=80,  anchor="w")
        self.tree.column("source",    width=80,  minwidth=60,  anchor="center")

        self.tree.tag_configure("skipped", foreground=self._skip_fg)

        mode = ctk.get_appearance_mode()
        if mode == "Dark":
            sb_thumb, sb_hover, sb_bg = "#3F3F46", "#71717A", "#111111"
        else:
            sb_thumb, sb_hover, sb_bg = "#C4C4C8", "#8E8E93", "#FFFFFF"

        self._ys = RoundedScrollbar(
            self._tree_card, orient="vertical", command=self.tree.yview,
            thumb_color=sb_thumb, hover_color=sb_hover, bg_color=sb_bg,
            radius=4, thickness=8,
        )
        self._xs = RoundedScrollbar(
            self._tree_card, orient="horizontal", command=self.tree.xview,
            thumb_color=sb_thumb, hover_color=sb_hover, bg_color=sb_bg,
            radius=4, thickness=8,
        )
        self.tree.configure(yscrollcommand=self._ys.set, xscrollcommand=self._xs.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        self._ys.grid(row=0, column=1, sticky="ns",  padx=(0, 2))
        self._xs.grid(row=1, column=0, sticky="ew",  pady=(0, 2))

    # ── Theme toggle ──────────────────────────────────────────────────

    def _toggle_theme(self):
        current  = ctk.get_appearance_mode()
        new_mode = "Light" if current == "Dark" else "Dark"
        ctk.set_appearance_mode(new_mode)
        self._setup_tree_style()
        self.tree.tag_configure("skipped", foreground=self._skip_fg)
        if new_mode == "Dark":
            sb_thumb, sb_hover, sb_bg = "#3F3F46", "#71717A", "#111111"
        else:
            sb_thumb, sb_hover, sb_bg = "#C4C4C8", "#8E8E93", "#FFFFFF"
        self._ys.update_colors(sb_thumb, sb_hover, sb_bg)
        self._xs.update_colors(sb_thumb, sb_hover, sb_bg)
        self._theme_btn.configure(text="☽" if new_mode == "Dark" else "☀")

    # ── Status bar ────────────────────────────────────────────────────

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, height=28, corner_radius=0,
                            fg_color=(self._RULE[0], "#1A1A1A"))
        bar.grid(row=1, column=0, columnspan=3, sticky="ew")
        bar.pack_propagate(False)

        self._dot = ctk.CTkLabel(bar, text="●", font=self._font(9),
                                  text_color=self._TXT3, width=20)
        self._dot.pack(side="left", padx=(14, 2))

        self.status_var = tk.StringVar(value="Ready")
        ctk.CTkLabel(bar, textvariable=self.status_var,
                     font=self._font(12), text_color=self._TXT2,
                     anchor="w").pack(side="left")

        self.progress = ctk.CTkProgressBar(bar, height=3, corner_radius=2,
                                            width=180,
                                            progress_color=self._ACC,
                                            mode="determinate")
        self.progress.set(0)
        self.progress.pack(side="right", padx=(0, 16))

    # ── Small UI helpers ──────────────────────────────────────────────

    def _hrule(self, parent, top=8, bottom=0):
        ctk.CTkFrame(parent, height=1, corner_radius=0,
                     fg_color=self._RULE).pack(fill="x", pady=(top, bottom))

    def _slabel(self, parent, text, top=12):
        """Sidebar section label — small, uppercase, muted."""
        ctk.CTkLabel(parent, text=text, font=self._font(10, "bold"),
                     text_color=self._TXT3, anchor="w").pack(
            anchor="w", padx=20, pady=(top, 0))

    def _set_dot(self, state):
        colors = {
            "idle":    self._TXT3,
            "working": "#F59E0B",
            "done":    "#10B981",
            "error":   "#EF4444",
        }
        self._dot.configure(text_color=colors.get(state, self._TXT3))

    def _enable_apply(self):
        self.apply_btn.configure(
            state="normal",
            fg_color=("#10B981", "#10B981"),
            hover_color=("#059669", "#059669"),
            text_color="#FFFFFF",
            border_width=0,
        )

    def _disable_apply(self):
        self.apply_btn.configure(
            state="disabled",
            fg_color=self._SURF,
            hover_color=self._RULE,
            text_color=self._TXT3,
            border_width=1,
        )

    def _start_progress(self, text):
        self.status_var.set(text)
        self._set_dot("working")
        self.progress.configure(mode="indeterminate")
        self.progress.start()

    def _finish_progress(self, text):
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(1.0)
        self.status_var.set(text)
        self._set_dot("done")

    def _update_badge(self, active, skipped):
        if active == 0 and skipped == 0:
            self._badge_var.set("")
            self._badge_lbl.grid_remove()
            return
        parts = []
        if active:
            parts.append(f"{active} to sort")
        if skipped:
            parts.append(f"{skipped} skipped")
        self._badge_var.set("  " + "  ·  ".join(parts))
        self._badge_lbl.grid()

    # ── Folder pickers ────────────────────────────────────────────────

    def pick_source(self):
        path = filedialog.askdirectory(title="Select source folder")
        if path:
            self.source_var.set(path)
            self._scan_source(path)

    def pick_dest(self):
        path = filedialog.askdirectory(title="Select destination folder")
        if path:
            self.dest_var.set(path)

    def _current_sort_mode(self):
        return self.sort_mode_var.get()

    # ── Source structure scan ─────────────────────────────────────────

    def _scan_source(self, path):
        """Fast filesystem-only scan shown immediately after folder selection."""
        self.plan = []
        self._disable_apply()
        self.tree.delete(*self.tree.get_children())
        self._badge_lbl.grid_remove()
        self._hdr_lbl.configure(text="SOURCE")
        self._start_progress("Reading source…")

        src = Path(path)

        def work():
            try:
                files = sorted(
                    (p for p in src.rglob("*")
                     if p.is_file() and p.suffix.lower() in MEDIA_EXTS),
                    key=lambda p: str(p).lower(),
                )
                self.worker_queue.put(("source_scanned", src, files))
            except Exception as e:
                self.worker_queue.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _populate_source_tree(self, src, files):
        """Render the source folder hierarchy in the preview tree."""
        PHOTO_EXTS = {
            ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif",
            ".webp", ".bmp", ".gif", ".raw", ".cr2", ".cr3", ".nef",
            ".arw", ".dng", ".orf", ".rw2", ".raf",
        }

        def ftype(p):
            return "Photo" if p.suffix.lower() in PHOTO_EXTS else "Video"

        def dir_iid(parts):
            return "sd::" + "/".join(parts)

        # Accumulate per-directory file counts (each folder includes all descendants).
        ph  = {}   # parts-tuple → photo count
        vid = {}   # parts-tuple → video count
        all_dirs: set = set()

        for f in files:
            rel_parts = f.relative_to(src).parts
            is_photo  = f.suffix.lower() in PHOTO_EXTS
            for depth in range(1, len(rel_parts)):
                key = rel_parts[:depth]
                all_dirs.add(key)
                if is_photo:
                    ph[key]  = ph.get(key, 0)  + 1
                else:
                    vid[key] = vid.get(key, 0) + 1

        # Insert directories, ensuring parents exist before children.
        inserted: set = set()

        def ensure_dir(parts):
            if parts in inserted:
                return
            if len(parts) > 1:
                ensure_dir(parts[:-1])
            parent_iid = dir_iid(parts[:-1]) if len(parts) > 1 else ""
            photos = ph.get(parts, 0)
            videos = vid.get(parts, 0)
            badges = []
            if photos:
                badges.append(f"{photos} photo{'s' if photos != 1 else ''}")
            if videos:
                badges.append(f"{videos} video{'s' if videos != 1 else ''}")
            self.tree.insert(parent_iid, "end",
                             iid=dir_iid(parts),
                             text=parts[-1],
                             values=("", "  ·  ".join(badges), "", ""),
                             open=len(parts) == 1)
            inserted.add(parts)

        for parts in sorted(all_dirs, key=lambda k: (len(k), k)):
            ensure_dir(parts)

        # Insert files under their parent directory.
        for f in files:
            rel_parts  = f.relative_to(src).parts
            parent_iid = dir_iid(rel_parts[:-1]) if len(rel_parts) > 1 else ""
            self.tree.insert(parent_iid, "end",
                             text=f.name,
                             values=(ftype(f), "", "", ""))

        # Update badge and status.
        total  = len(files)
        photos = sum(1 for f in files if f.suffix.lower() in PHOTO_EXTS)
        videos = total - photos
        badge_parts = []
        if photos:
            badge_parts.append(f"{photos} photo{'s' if photos != 1 else ''}")
        if videos:
            badge_parts.append(f"{videos} video{'s' if videos != 1 else ''}")

        if badge_parts:
            self._badge_var.set("  " + "  ·  ".join(badge_parts))
        else:
            self._badge_var.set("  no media files found")
        self._badge_lbl.grid()

    # ── Preview ───────────────────────────────────────────────────────

    def run_preview(self):
        src = self.source_var.get().strip()
        dst = self.dest_var.get().strip()
        if not src or not Path(src).is_dir():
            messagebox.showerror("mo", "Pick a valid source folder.")
            return
        if not dst:
            messagebox.showerror("mo", "Pick a destination folder.")
            return

        self.preview_btn.configure(state="disabled")
        self._disable_apply()
        self.tree.delete(*self.tree.get_children())
        self._update_badge(0, 0)
        self._hdr_lbl.configure(text="PREVIEW")
        self._start_progress("Scanning…")

        src_p, dst_p = Path(src), Path(dst)
        recursive    = self.recursive_var.get()
        rename       = self.rename_var.get()
        sort_mode    = self._current_sort_mode()

        def work():
            def cb(i, total, name):
                self.worker_queue.put(("progress", i, total, f"Scanning  {name}"))
            try:
                plan = build_plan(src_p, dst_p, recursive, rename, sort_mode, cb)
                self.worker_queue.put(("preview_done", plan, sort_mode))
            except Exception as e:
                self.worker_queue.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _populate_tree(self, plan, sort_mode):
        self.tree.delete(*self.tree.get_children())
        skipped = [p for p in plan if     p["skipped"]]
        active  = [p for p in plan if not p["skipped"]]

        if sort_mode == SORT_BY_DATE:
            self._populate_date_tree(active)
        elif sort_mode == SORT_BY_LOCATION:
            self._populate_location_tree(active)
        else:
            self._populate_date_location_tree(active)

        if skipped:
            sid = "skipped"
            self.tree.insert("", "end", iid=sid,
                             text=f"⚠  Skipped  ({len(skipped)})",
                             values=("", "", "", ""),
                             tags=("skipped",), open=False)
            for item in skipped:
                self.tree.insert(sid, "end",
                                 text=item["src"].name,
                                 values=("—", "—", "—", item["reason"]),
                                 tags=("skipped",))

    def _populate_date_tree(self, items):
        by_folder = {}
        for item in items:
            by_folder.setdefault((item["year"], item["month"]), []).append(item)

        for (year, month) in sorted(by_folder.keys(),
                                    key=lambda k: (k[0], MONTH_NAMES.index(k[1]))):
            year_id = f"year::{year}"
            if not self.tree.exists(year_id):
                self.tree.insert("", "end", iid=year_id, text=year,
                                 values=("", "", "", ""), open=True)
            mitems   = by_folder[(year, month)]
            month_id = f"month::{year}::{month}"
            self.tree.insert(year_id, "end", iid=month_id,
                             text=f"{month}  ·  {len(mitems)} files",
                             values=("", "", "", ""), open=False)
            for item in mitems:
                self._insert_file_row(month_id, item)

    def _populate_location_tree(self, items):
        by_country = {}
        for item in items:
            by_country.setdefault(item["country"] or UNKNOWN_LOCATION, []).append(item)

        for country in sorted(by_country,
                              key=lambda c: (c == UNKNOWN_LOCATION, c.lower())):
            cid    = f"country::{country}"
            citems = by_country[country]
            self.tree.insert("", "end", iid=cid,
                             text=f"{country}  ·  {len(citems)} files",
                             values=("", "", "", ""), open=True)
            for item in citems:
                self._insert_file_row(cid, item)

    def _populate_date_location_tree(self, items):
        tree_data = {}
        for item in items:
            tree_data \
                .setdefault(item["year"], {}) \
                .setdefault(item["month"], {}) \
                .setdefault(item["country"] or UNKNOWN_LOCATION, []) \
                .append(item)

        for year in sorted(tree_data):
            year_id = f"year::{year}"
            self.tree.insert("", "end", iid=year_id, text=year,
                             values=("", "", "", ""), open=True)
            for month in sorted(tree_data[year], key=MONTH_NAMES.index):
                countries   = tree_data[year][month]
                month_count = sum(len(v) for v in countries.values())
                month_id    = f"month::{year}::{month}"
                self.tree.insert(year_id, "end", iid=month_id,
                                 text=f"{month}  ·  {month_count} files",
                                 values=("", "", "", ""), open=False)
                for country in sorted(countries,
                                      key=lambda c: (c == UNKNOWN_LOCATION, c.lower())):
                    cid    = f"loc::{year}::{month}::{country}"
                    citems = countries[country]
                    self.tree.insert(month_id, "end", iid=cid,
                                     text=f"{country}  ·  {len(citems)}",
                                     values=("", "", "", ""), open=False)
                    for item in citems:
                        self._insert_file_row(cid, item)

    def _insert_file_row(self, parent_id, item):
        date_str = item["date"].strftime("%Y-%m-%d  %H:%M") if item["date"] else "—"
        if item["city"] and item["country"] and item["country"] != UNKNOWN_LOCATION:
            loc_str = f"{item['city']}, {item['country']}"
        elif item["country"] and item["country"] != UNKNOWN_LOCATION:
            loc_str = item["country"]
        else:
            loc_str = "—"
        self.tree.insert(parent_id, "end",
                         text=item["src"].name,
                         values=(item["dst"].name, date_str, loc_str,
                                 item["source_tag"]))

    # ── Apply ─────────────────────────────────────────────────────────

    def run_apply(self):
        if not self.plan:
            return
        todo = [p for p in self.plan if not p["skipped"]]
        if not todo:
            messagebox.showinfo("mo", "Nothing to do.")
            return
        op  = self.operation_var.get()
        msg = (f"{op.capitalize()} {len(todo)} file(s) into:\n"
               f"{self.dest_var.get()}\n\nProceed?")
        if not messagebox.askyesno("Confirm", msg):
            return

        self.preview_btn.configure(state="disabled")
        self._disable_apply()
        self._start_progress(f"{op.capitalize()}ing…")

        plan        = self.plan
        dest_folder = Path(self.dest_var.get())

        def work():
            def cb(i, total, name):
                self.worker_queue.put(
                    ("progress", i, total, f"{op.capitalize()}ing  {name}"))
            try:
                ok, err, errors = execute_plan(plan, op, cb)
                undo_log = [
                    {"operation": op, "src": str(it["src"]), "dst": str(it["dst"])}
                    for it in plan if not it["skipped"]
                ]
                with open(dest_folder / "mo_undo_log.json", "w", encoding="utf-8") as f:
                    json.dump(undo_log, f, indent=2)
                self.worker_queue.put(("apply_done", ok, err, errors))
            except Exception as e:
                self.worker_queue.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    # ── Undo ──────────────────────────────────────────────────────────

    def undo_last_operation(self):
        dest = self.dest_var.get().strip()
        if not dest:
            messagebox.showerror("mo", "Pick the destination folder first.")
            return
        undo_path = Path(dest) / "mo_undo_log.json"
        if not undo_path.exists():
            messagebox.showerror("mo", "No undo log found in the destination folder.")
            return
        if not messagebox.askyesno("Confirm Undo", "Undo the last operation?"):
            return

        try:
            with open(undo_path, "r", encoding="utf-8") as f:
                undo_log = json.load(f)

            undone, errors = 0, []
            for item in reversed(undo_log):
                src, dst, op = Path(item["src"]), Path(item["dst"]), item["operation"]
                try:
                    if op == "move" and dst.exists():
                        src.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(dst), str(src))
                        undone += 1
                    elif op == "copy" and dst.exists():
                        dst.unlink()
                        undone += 1
                except Exception as e:
                    errors.append(f"{dst.name}: {e}")

            undo_path.unlink()
            if errors:
                messagebox.showwarning(
                    "Undo finished with errors",
                    f"Undone: {undone}\n\nErrors:\n" + "\n".join(errors[:10]))
            else:
                messagebox.showinfo("Undo Complete", f"Undone: {undone} file(s).")
        except Exception as e:
            messagebox.showerror("mo", f"Undo failed:\n{e}")

    # ── Queue poll (thread → UI bridge) ──────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                msg  = self.worker_queue.get_nowait()
                kind = msg[0]

                if kind == "progress":
                    self.status_var.set(msg[3] or "…")

                elif kind == "source_scanned":
                    _, src, files = msg
                    self._populate_source_tree(src, files)
                    total = len(files)
                    self._finish_progress(
                        f"Found {total} media file{'s' if total != 1 else ''}  ·  "
                        "set options and click Preview"
                    )

                elif kind == "preview_done":
                    self.plan   = msg[1]
                    sort_mode   = msg[2]
                    self._populate_tree(self.plan, sort_mode)
                    total   = len(self.plan)
                    skipped = sum(1 for p in self.plan if p["skipped"])
                    active  = total - skipped
                    self._update_badge(active, skipped)
                    self._finish_progress("Ready")
                    self.preview_btn.configure(state="normal")
                    if active > 0:
                        self._enable_apply()

                elif kind == "apply_done":
                    _, ok, err, errors = msg
                    self._finish_progress(
                        f"Done  ·  {ok} processed" + (f"  ·  {err} failed" if err else ""))
                    self.preview_btn.configure(state="normal")
                    self.plan = []
                    self._update_badge(0, 0)
                    if err:
                        messagebox.showwarning(
                            "Finished with errors",
                            f"{ok} ok, {err} failed.\n\n" +
                            "\n".join(errors[:10]) +
                            ("\n…" if len(errors) > 10 else ""))
                    else:
                        messagebox.showinfo("Done", f"Processed {ok} file(s).")

                elif kind == "error":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress.set(0)
                    self.status_var.set("Error")
                    self._set_dot("error")
                    self.preview_btn.configure(state="normal")
                    messagebox.showerror("mo", msg[1])

        except Empty:
            pass
        self.after(100, self._poll_queue)


if __name__ == "__main__":
    if not HAS_EXIF and not HAS_HACHOIR:
        print("heads up: neither `exif` nor `hachoir` is installed — "
              "falling back to file mtime.")
        print("install: pip install exif hachoir")
    if not geo.HAS_GEOCODER:
        print("heads up: `reverse_geocoder` not installed — "
              "location sorting disabled.")
        print("install: pip install reverse_geocoder")
    MoApp().mainloop()
