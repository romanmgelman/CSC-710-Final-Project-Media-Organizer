import os
import json
import shutil
import threading
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# these are optional — if they're missing we just fall back to mtime
try:
    from exif import Image as ExifImage
    HAS_EXIF = True
except ImportError:
    HAS_EXIF = False

try:
    from hachoir.parser import createParser
    from hachoir.metadata import extractMetadata
    import hachoir.core.config as hachoir_config
    hachoir_config.quiet = True  # hachoir is noisy by default, tell it to chill
    HAS_HACHOIR = True
except ImportError:
    HAS_HACHOIR = False


MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# anything outside this list gets ignored during the scan
MEDIA_EXTS = {
    # photos
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".tif",
    ".webp", ".bmp", ".gif", ".raw", ".cr2", ".cr3", ".nef",
    ".arw", ".dng", ".orf", ".rw2", ".raf",
    # videos
    ".mov", ".mp4", ".m4v", ".avi", ".mkv", ".wmv", ".flv", ".3gp",
    ".mts", ".m2ts",
}


# -------------------------------------------------------------
# pulling dates out of files
# -------------------------------------------------------------

def _parse_exif_datetime(s):
    # EXIF mostly uses 'YYYY:MM:DD HH:MM:SS' but some cameras get creative
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
    # best source for photos — gets the actual shutter time
    if not HAS_EXIF:
        return None
    try:
        with open(path, "rb") as f:
            img = ExifImage(f)
        if not img.has_exif:
            return None
        # try these in order, datetime_original is the one we really want
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
    # catches videos and anything exif can't handle
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
    # last resort — filesystem time. not reliable but better than nothing
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except Exception:
        return None


def get_file_date(path):
    # try each source in order, return whatever works first
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


# -------------------------------------------------------------
# figuring out where everything should go
# -------------------------------------------------------------

def build_plan(source, dest, recursive, rename, progress_cb=None):
    """
    Walks the source, figures out a destination path for each file,
    and returns a list of dicts describing what would happen.
    Nothing gets moved here — this is just the plan.
    """
    plan = []
    used_dsts = set()

    # gather candidate files
    if recursive:
        candidates = [p for p in source.rglob("*") if p.is_file()]
    else:
        candidates = [p for p in source.iterdir() if p.is_file()]
    candidates = [p for p in candidates if p.suffix.lower() in MEDIA_EXTS]

    total = len(candidates)
    for i, src in enumerate(candidates):
        if progress_cb:
            progress_cb(i, total, src.name)

        dt, tag = get_file_date(src)
        if not dt:
            # couldn't get a date anywhere, park it in the skipped bucket
            plan.append({
                "src": src, "dst": None, "year": None, "month": None,
                "date": None, "source_tag": "none", "skipped": True,
                "reason": "no date found",
            })
            continue

        folder = dest / f"{dt.year:04d}" / MONTH_NAMES[dt.month - 1]

        if rename:
            base = f"{dt.month:02d}_{dt.day:02d}_{dt.year:04d}"
            ext = src.suffix.lower()
            candidate = folder / f"{base}{ext}"
            # if something else already claimed this name, tack on the time
            if candidate in used_dsts or candidate.exists():
                stamp = dt.strftime("%H%M%S")
                candidate = folder / f"{base}_{stamp}{ext}"
                # if we *still* collide, start counting
                n = 1
                while candidate in used_dsts or candidate.exists():
                    candidate = folder / f"{base}_{stamp}_{n}{ext}"
                    n += 1
            dst = candidate
        else:
            # keep the original name, just bump it if there's a clash
            candidate = folder / src.name
            n = 1
            stem, ext = src.stem, src.suffix
            while candidate in used_dsts or candidate.exists():
                candidate = folder / f"{stem}_{n}{ext}"
                n += 1
            dst = candidate

        used_dsts.add(dst)
        plan.append({
            "src": src, "dst": dst,
            "year": f"{dt.year:04d}", "month": MONTH_NAMES[dt.month - 1],
            "date": dt, "source_tag": tag, "skipped": False, "reason": "",
        })

    if progress_cb:
        progress_cb(total, total, "")
    return plan


# -------------------------------------------------------------
# actually doing the thing
# -------------------------------------------------------------

def execute_plan(plan, operation, progress_cb=None):
    # operation is 'copy' or 'move'
    ok, err = 0, 0
    errors = []
    todo = [p for p in plan if not p["skipped"]]
    total = len(todo)

    for i, item in enumerate(todo):
        if progress_cb:
            progress_cb(i, total, item["src"].name)
        try:
            item["dst"].parent.mkdir(parents=True, exist_ok=True)
            if operation == "copy":
                shutil.copy2(item["src"], item["dst"])  # copy2 keeps mtime
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
# GUI
# -------------------------------------------------------------

class MoApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("mo — media organizer")
        self.geometry("900x650")
        self.minsize(700, 500)

        self.plan = []
        self.worker_queue = Queue()  # background threads talk to the UI through this

        self._build_ui()
        self._poll_queue()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # source + destination pickers up top
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Source:").grid(row=0, column=0, sticky="w")
        self.source_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.source_var).grid(
            row=0, column=1, sticky="ew", padx=4)
        ttk.Button(top, text="Browse…", command=self.pick_source).grid(
            row=0, column=2)

        ttk.Label(top, text="Destination:").grid(row=1, column=0, sticky="w")
        self.dest_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.dest_var).grid(
            row=1, column=1, sticky="ew", padx=4)
        ttk.Button(top, text="Browse…", command=self.pick_dest).grid(
            row=1, column=2)

        top.columnconfigure(1, weight=1)

        # options row
        opts = ttk.LabelFrame(self, text="Options")
        opts.pack(fill="x", **pad)

        self.operation_var = tk.StringVar(value="copy")
        ttk.Radiobutton(opts, text="Copy (keep originals)",
                        variable=self.operation_var, value="copy").pack(
            side="left", padx=8, pady=4)
        ttk.Radiobutton(opts, text="Move (cut & paste)",
                        variable=self.operation_var, value="move").pack(
            side="left", padx=8, pady=4)

        ttk.Separator(opts, orient="vertical").pack(
            side="left", fill="y", padx=8)

        self.recursive_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Include subfolders",
                        variable=self.recursive_var).pack(
            side="left", padx=8, pady=4)

        self.rename_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Rename files (MM_DD_YYYY.ext)",
                        variable=self.rename_var).pack(
            side="left", padx=8, pady=4)

        # the two big buttons
        actions = ttk.Frame(self)
        actions.pack(fill="x", **pad)
        self.preview_btn = ttk.Button(actions, text="Preview",
                                      command=self.run_preview)
        self.preview_btn.pack(side="left")
        self.apply_btn = ttk.Button(actions, text="Apply",
                                    command=self.run_apply, state="disabled")
        self.apply_btn.pack(side="left", padx=8)
        self.undo_btn = ttk.Button(actions, text="Undo Last Operation",
                           command=self.undo_last_operation)
        self.undo_btn.pack(side="left", padx=8)

        # preview tree — groups files under year / month
        tree_frame = ttk.LabelFrame(self, text="Preview")
        tree_frame.pack(fill="both", expand=True, **pad)

        cols = ("new_name", "date", "source")
        self.tree = ttk.Treeview(tree_frame, columns=cols)
        self.tree.heading("#0", text="Folder / File")
        self.tree.heading("new_name", text="New name")
        self.tree.heading("date", text="Date")
        self.tree.heading("source", text="Metadata")
        self.tree.column("#0", width=300, anchor="w")
        self.tree.column("new_name", width=200, anchor="w")
        self.tree.column("date", width=140, anchor="w")
        self.tree.column("source", width=80, anchor="center")

        yscroll = ttk.Scrollbar(tree_frame, orient="vertical",
                                command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

        # status bar at the bottom
        status = ttk.Frame(self)
        status.pack(fill="x", **pad)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(status, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(status, mode="determinate", length=200)
        self.progress.pack(side="right")

    # folder pickers
    def pick_source(self):
        path = filedialog.askdirectory(title="Select source folder")
        if path:
            self.source_var.set(path)

    def pick_dest(self):
        path = filedialog.askdirectory(title="Select destination folder")
        if path:
            self.dest_var.set(path)

    def run_preview(self):
        src = self.source_var.get().strip()
        dst = self.dest_var.get().strip()
        if not src or not Path(src).is_dir():
            messagebox.showerror("mo", "Pick a valid source folder.")
            return
        if not dst:
            messagebox.showerror("mo", "Pick a destination folder.")
            return

        self.preview_btn.config(state="disabled")
        self.apply_btn.config(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.status_var.set("Scanning…")
        self.progress["value"] = 0

        src_p, dst_p = Path(src), Path(dst)
        recursive = self.recursive_var.get()
        rename = self.rename_var.get()

        # scanning can take a while (hachoir is slow on videos),
        # so shove it onto a background thread and keep the UI alive
        def work():
            def cb(i, total, name):
                self.worker_queue.put(("progress", i, total, f"Scanning: {name}"))
            try:
                plan = build_plan(src_p, dst_p, recursive, rename, cb)
                self.worker_queue.put(("preview_done", plan))
            except Exception as e:
                self.worker_queue.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _populate_tree(self, plan):
        # build the year > month > file hierarchy, with a skipped bucket at the bottom
        self.tree.delete(*self.tree.get_children())
        by_folder = {}
        skipped = []
        for item in plan:
            if item["skipped"]:
                skipped.append(item)
                continue
            key = (item["year"], item["month"])
            by_folder.setdefault(key, []).append(item)

        # years ascending, months in calendar order (not alphabetical)
        for (year, month) in sorted(by_folder.keys(),
                                    key=lambda k: (k[0], MONTH_NAMES.index(k[1]))):
            year_id = f"year::{year}"
            if not self.tree.exists(year_id):
                self.tree.insert("", "end", iid=year_id, text=year,
                                 values=("", "", ""), open=True)
            month_id = f"month::{year}::{month}"
            items = by_folder[(year, month)]
            self.tree.insert(year_id, "end", iid=month_id,
                             text=f"{month}  ({len(items)} files)",
                             values=("", "", ""), open=False)
            for item in items:
                date_str = item["date"].strftime("%Y-%m-%d %H:%M:%S")
                self.tree.insert(
                    month_id, "end",
                    text=item["src"].name,
                    values=(item["dst"].name, date_str, item["source_tag"]),
                )

        if skipped:
            skip_id = "skipped"
            self.tree.insert("", "end", iid=skip_id,
                             text=f"⚠ Skipped  ({len(skipped)} files)",
                             values=("", "", ""), open=False)
            for item in skipped:
                self.tree.insert(skip_id, "end", text=item["src"].name,
                                 values=("—", "—", item["reason"]))

    def run_apply(self):
        if not self.plan:
            return
        todo = [p for p in self.plan if not p["skipped"]]
        if not todo:
            messagebox.showinfo("mo", "Nothing to do.")
            return

        op = self.operation_var.get()
        msg = (f"{op.capitalize()} {len(todo)} file(s) into:\n"
               f"{self.dest_var.get()}\n\nProceed?")
        if not messagebox.askyesno("Confirm", msg):
            return

        self.preview_btn.config(state="disabled")
        self.apply_btn.config(state="disabled")
        self.status_var.set("Applying…")
        self.progress["value"] = 0

        plan = self.plan
        dest_folder = Path(self.dest_var.get())
        def work():
            def cb(i, total, name):
                self.worker_queue.put(("progress", i, total, f"{op.capitalize()}ing: {name}"))
            try:
                ok, err, errors = execute_plan(plan, op, cb)
                
                undo_log = []
                for item in plan:
                    if not item["skipped"]:
                        undo_log.append({
                            "operation": op,
                            "src": str(item["src"]),
                            "dst": str(item["dst"])
                        })

                undo_path = dest_folder / "mo_undo_log.json"
                with open(undo_path, "w", encoding="utf-8") as f:
                    json.dump(undo_log, f, indent=2)
                
                self.worker_queue.put(("apply_done", ok, err, errors))
            except Exception as e:
                self.worker_queue.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()
        
    # undo function
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

            undone = 0
            errors = []

            for item in reversed(undo_log):
                src = Path(item["src"])
                dst = Path(item["dst"])
                operation = item["operation"]

                try:
                    if operation == "move":
                        if dst.exists():
                            src.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(dst), str(src))
                            undone += 1

                    elif operation == "copy":
                        if dst.exists():
                            dst.unlink()
                            undone += 1

                except Exception as e:
                    errors.append(f"{dst.name}: {e}")

            undo_path.unlink()

            if errors:
                messagebox.showwarning(
                    "Undo finished with errors",
                    f"Undone: {undone}\n\nErrors:\n" + "\n".join(errors[:10])
                )
            else:
                messagebox.showinfo("Undo Complete", f"Undone: {undone} file(s).")

        except Exception as e:
            messagebox.showerror("mo", f"Undo failed:\n{e}")

    # poll the queue so background threads can update the UI safely.
    # tkinter really doesn't like being touched from other threads.
    def _poll_queue(self):
        try:
            while True:
                msg = self.worker_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, i, total, text = msg
                    if total:
                        self.progress["maximum"] = total
                        self.progress["value"] = i
                    self.status_var.set(text or "…")
                elif kind == "preview_done":
                    self.plan = msg[1]
                    self._populate_tree(self.plan)
                    total = len(self.plan)
                    skipped = sum(1 for p in self.plan if p["skipped"])
                    self.status_var.set(
                        f"Preview ready: {total - skipped} to sort, "
                        f"{skipped} skipped.")
                    self.preview_btn.config(state="normal")
                    if total - skipped > 0:
                        self.apply_btn.config(state="normal")
                elif kind == "apply_done":
                    _, ok, err, errors = msg
                    self.status_var.set(f"Done. {ok} succeeded, {err} failed.")
                    self.preview_btn.config(state="normal")
                    self.apply_btn.config(state="disabled")
                    self.plan = []
                    if err:
                        # cap the error list so we don't flood the dialog
                        messagebox.showwarning(
                            "Finished with errors",
                            f"{ok} ok, {err} failed.\n\n" +
                            "\n".join(errors[:10]) +
                            ("\n…" if len(errors) > 10 else ""))
                    else:
                        messagebox.showinfo("Done", f"Processed {ok} file(s).")
                elif kind == "error":
                    self.status_var.set("Error.")
                    self.preview_btn.config(state="normal")
                    messagebox.showerror("mo", msg[1])
        except Empty:
            pass
        self.after(100, self._poll_queue)


if __name__ == "__main__":
    if not HAS_EXIF and not HAS_HACHOIR:
        print("heads up: neither `exif` nor `hachoir` is installed — "
              "we'll have to fall back to file mtime for everything.")
        print("install them with: pip install exif hachoir")
    MoApp().mainloop()