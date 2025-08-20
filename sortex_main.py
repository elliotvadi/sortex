#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SortEX – sort images and videos by date (EXIF) into folders "YYYY-MM".
GUI with Tkinter to select source folder. Works on Windows/macOS/Linux.
Requires: Pillow (pip install pillow)
Optional: pillow-heif for HEIC/HEIF (pip install pillow-heif)

iCloud adjustments:
- Skips .aae sidecar files.
- Pairs .mov/.mp4 (Live Photos videos) with photo of the same filename stem and moves/copies them together
  according to the photo's date. If video lacks a pair: fallback to the file's mtime.
"""

import os
import sys
import shutil
from pathlib import Path
from datetime import datetime
import threading
import queue
from typing import Dict, List, Tuple, Optional

# --- Dependencies ---
try:
    from PIL import Image, ExifTags
except ImportError:
    print("This script requires Pillow. Install with: pip install pillow")
    sys.exit(1)

# HEIC support if available
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()  # register HEIF/HEIC loader in Pillow
    HEIC_SUPPORTED = True
except Exception:
    HEIC_SUPPORTED = False

# --- Tkinter GUI ---
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# EXIF tags we can use
_EXIF_DATETIME_CANDIDATES = [
    "DateTimeOriginal",  # most common
    "CreateDate",
    "DateTimeDigitized",
    "DateTime",          # fallback
]

# Folder name for files without date
UNKNOWN_DIR_NAME = "unknown-date"

# File extensions we handle (images + videos)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif", ".webp"}
VIDEO_EXTS = {".mov", ".mp4", ".avi", ".mpeg"}
IGNORE_EXTS = {".aae"}  # iCloud sidecar

def is_media_file(path: Path) -> bool:
    ext = path.suffix.lower()
    return ext in IMAGE_EXTS or ext in VIDEO_EXTS

def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS

def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS

def _exif_dict_from_image(img: Image.Image):
    exif = img.getexif()
    if not exif:
        return {}
    out = {}
    for tag_id, value in exif.items():
        tag = ExifTags.TAGS.get(tag_id, tag_id)
        out[str(tag)] = value
    return out

def parse_exif_datetime(value: str):
    """
    Try to parse common EXIF datetime formats and return a datetime object.
    """
    if not value:
        return None

    fmts = [
        "%Y:%m:%d %H:%M:%S",
        "%Y:%m:%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(str(value), fmt)
        except Exception:
            continue
    return None

def get_image_datetime(path: Path):
    """
    Try to get photo datetime from EXIF. Fallback to file's mtime if is EXIF missing.
    Returns (dt, source) where source is 'exif' or 'mtime' or None.
    """
    try:
        with Image.open(path) as img:
            exif = _exif_dict_from_image(img)
            for key in _EXIF_DATETIME_CANDIDATES:
                if key in exif:
                    dt = parse_exif_datetime(exif[key])
                    if dt:
                        return dt, "exif"
    except Exception:
        pass

    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts), "mtime"
    except Exception:
        return None, None

def build_target_dirname(dt: datetime):
    mm = f"{dt.month:02d}"
    yyyy = f"{dt.year:02d}"
    return f"{yyyy}-{mm}"

def ensure_unique_path(target: Path) -> Path:
    """
    If file exists – add a running suffix _1, _2, ...
    """
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    i = 1
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1

class ImageSorterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SortEX – Sort pictures via date (EXIF) | Copyright (c) 2025 Elliot Vadi")
        self.geometry("780x560")
        self.minsize(700, 500)

        self.source_dir = tk.StringVar(value="")
        self.copy_mode = tk.BooleanVar(value=False)  # if True: copy, otherwise move
        self.include_unknown = tk.BooleanVar(value=True)  # put files without date in "unknown-date"
        self.process_subdirs = tk.BooleanVar(value=True)  # include subfolders

        self.log_queue = queue.Queue()

        self.create_widgets()
        self.after(100, self._process_log_queue)

    def create_widgets(self):
        pad = {"padx": 10, "pady": 8}

        frm_src = ttk.LabelFrame(self, text="1) Select source folder")
        frm_src.pack(fill="x", **pad)

        row = ttk.Frame(frm_src)
        row.pack(fill="x", padx=10, pady=8)
        ttk.Label(row, text="Source:").pack(side="left")
        self.entry_src = ttk.Entry(row, textvariable=self.source_dir)
        self.entry_src.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(row, text="Browse...", command=self.browse_source).pack(side="left")

        frm_opts = ttk.LabelFrame(self, text="2) Options")
        frm_opts.pack(fill="x", **pad)

        opt_row = ttk.Frame(frm_opts)
        opt_row.pack(fill="x", padx=10, pady=8)

        ttk.Checkbutton(opt_row, text="Copy files instead of moving", variable=self.copy_mode).pack(side="left")
        ttk.Checkbutton(opt_row, text="Include subfolders", variable=self.process_subdirs).pack(side="left", padx=(16, 0))
        ttk.Checkbutton(opt_row, text=f"Place files without date in \"{UNKNOWN_DIR_NAME}\"", variable=self.include_unknown).pack(side="left", padx=(16, 0))

        frm_run = ttk.LabelFrame(self, text="3) Run")
        frm_run.pack(fill="x", **pad)

        run_row = ttk.Frame(frm_run)
        run_row.pack(fill="x", padx=10, pady=8)

        self.btn_dry = ttk.Button(run_row, text="Test run (no change)", command=self.run_dry)
        self.btn_dry.pack(side="left")

        self.btn_go = ttk.Button(run_row, text="Run!", command=self.run_sort)
        self.btn_go.pack(side="left", padx=(12, 0))

        self.progress = ttk.Progressbar(frm_run, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x", padx=10, pady=(0, 8))

        self.status = ttk.Label(frm_run, text="Ready.")
        self.status.pack(fill="x", padx=10, pady=(0, 10))

        frm_log = ttk.LabelFrame(self, text="Log")
        frm_log.pack(fill="both", expand=True, **pad)

        self.txt = tk.Text(frm_log, height=14, wrap="word", state="disabled")
        self.txt.pack(fill="both", expand=True, padx=10, pady=10)

    def browse_source(self):
        d = filedialog.askdirectory(title="Choose a source folder")
        if d:
            self.source_dir.set(d)

    def _collect_files(self, src: Path):
        if self.process_subdirs.get():
            return [p for p in src.rglob("*") if p.is_file() and is_media_file(p) and p.suffix.lower() not in IGNORE_EXTS]
        else:
            return [p for p in src.glob("*") if p.is_file() and is_media_file(p) and p.suffix.lower() not in IGNORE_EXTS]

    def _log(self, msg: str):
        self.log_queue.put(msg)

    def _process_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.txt.configure(state="normal")
                self.txt.insert("end", msg + "\n")
                self.txt.see("end")
                self.txt.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._process_log_queue)

    def run_dry(self):
        self._start_worker(dry_run=True)

    def run_sort(self):
        self._start_worker(dry_run=False)

    def _start_worker(self, dry_run: bool):
        src = Path(self.source_dir.get()).expanduser()
        if not src.exists() or not src.is_dir():
            messagebox.showerror("Error", "Choose a valid source folder.")
            return

        self.btn_go.configure(state="disabled")
        self.btn_dry.configure(state="disabled")
        self.status.configure(text="Working...")
        self.progress.configure(value=0)

        worker = threading.Thread(target=self._worker, args=(src, dry_run), daemon=True)
        worker.start()

    def _pair_live_photos(self, files: List[Path]) -> Tuple[Dict[Path, List[Path]], List[Path]]:
        """
        Create pairs for Live Photos:
        - If a video (.mov/.mp4) has the same stem as a photo in the same folder, pair them.
        - Returns:
            pairs: {photo_path: [associated videos]}
            singles: files not paired (including photos and videos).
        """
        by_dir: Dict[Path, Dict[str, List[Path]]] = {}
        for p in files:
            by_dir.setdefault(p.parent, {}).setdefault(p.stem, []).append(p)

        pairs: Dict[Path, List[Path]] = {}
        singles: List[Path] = []
        for parent, stems in by_dir.items():
            for stem, items in stems.items():
                images = [x for x in items if is_image_file(x)]
                videos = [x for x in items if is_video_file(x)]
                if images and videos:
                    master = sorted(images)[0]
                    pairs.setdefault(master, []).extend(sorted(videos))
                    for extra_img in images[1:]:
                        singles.append(extra_img)
                else:
                    singles.extend(items)
        singles = list(dict.fromkeys(singles))
        in_pairs = set()
        for master, vids in pairs.items():
            in_pairs.add(master)
            in_pairs.update(vids)
        singles = [p for p in singles if p not in in_pairs]
        return pairs, singles

    def _worker(self, src: Path, dry_run: bool):
        try:
            files = self._collect_files(src)
            total = len(files)
            if total == 0:
                self._log("Found no media.")
                self._done()
                return

            pairs, singles = self._pair_live_photos(files)
            total_ops = len(pairs) + len(singles)
            self.progress.configure(maximum=total_ops)
            n_done = 0

            def move_or_copy(path: Path, target_dir: Path) -> Path:
                if not dry_run:
                    target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / path.name
                final_target = ensure_unique_path(target)
                if dry_run:
                    return final_target
                if self.copy_mode.get():
                    shutil.copy2(path, final_target)
                else:
                    shutil.move(str(path), str(final_target))
                return final_target

            for master_img, videos in pairs.items():
                dt, source = get_image_datetime(master_img) if is_image_file(master_img) else (None, None)
                if not dt and not self.include_unknown.get():
                    self._log(f"Skipping (no date found): {master_img.relative_to(src)} (+ {len(videos)} video)")
                    n_done += 1
                    self.progress.configure(value=n_done)
                    continue
                dirname = build_target_dirname(dt) if dt else UNKNOWN_DIR_NAME
                target_dir = src / dirname

                final_master = move_or_copy(master_img, target_dir)
                src_label = f" ({source})" if source else ""
                action = "Would " + ("copy" if self.copy_mode.get() else "move") if dry_run else ("Copied" if self.copy_mode.get() else "Moved")
                self._log(f"{action}: {master_img.relative_to(src)}  ->  {dirname}/{final_master.name}{src_label}")

                for v in videos:
                    final_v = move_or_copy(v, target_dir)
                    self._log(f"{action}: {v.relative_to(src)}  ->  {dirname}/{final_v.name} (paired with photo)")

                n_done += 1
                self.progress.configure(value=n_done)

            for path in singles:
                rel = path.relative_to(src)
                try:
                    if is_image_file(path):
                        dt, source = get_image_datetime(path)
                    else:
                        try:
                            ts = path.stat().st_mtime
                            dt, source = datetime.fromtimestamp(ts), "mtime"
                        except Exception:
                            dt, source = None, None

                    if dt:
                        dirname = build_target_dirname(dt)
                    else:
                        if not self.include_unknown.get():
                            self._log(f"Skipping (no date found): {rel}")
                            n_done += 1
                            self.progress.configure(value=n_done)
                            continue
                        dirname = UNKNOWN_DIR_NAME

                    target_dir = src / dirname
                    final_target = move_or_copy(path, target_dir)

                    action = "Would " + ("copy" if self.copy_mode.get() else "move") if dry_run else ("Copied" if self.copy_mode.get() else "Moved")
                    src_label = f" ({source})" if source else ""
                    self._log(f"{action}: {rel}  ->  {dirname}/{final_target.name}{src_label}")

                except Exception as e:
                    self._log(f"Error with {rel}: {e!r}")

                n_done += 1
                self.progress.configure(value=n_done)

            self._log("Done!")
        finally:
            self._done()

    def _done(self):
        self.btn_go.configure(state="normal")
        self.btn_dry.configure(state="normal")
        self.status.configure(text="Ready.")

def main():
    app = ImageSorterApp()
    app.mainloop()

if __name__ == "__main__":
    main()
