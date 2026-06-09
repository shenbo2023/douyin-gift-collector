#!/usr/bin/env python3
"""抖音礼物采集 - 图形界面（exe 入口）"""

import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

import collect_gifts


class GiftCollectorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("抖音礼物采集")
        self.root.geometry("520x280")
        self.root.resizable(False, False)

        self._worker: Optional[threading.Thread] = None
        self._running = False
        self._log_file = ""

        collect_gifts.load_config()
        hint = collect_gifts.sid_guard_expiry_hint()

        pad = {"padx": 12, "pady": 6}
        frm = ttk.Frame(root, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="直播间房间 ID", font=("", 11, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(
            frm,
            text="输入纯数字 room_id，或 live.douyin.com/后面的短号",
            foreground="#666",
        ).grid(row=1, column=0, columnspan=3, sticky="w")

        self.room_var = tk.StringVar()
        entry = ttk.Entry(frm, textvariable=self.room_var, width=42, font=("", 12))
        entry.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 8))
        entry.focus_set()
        entry.bind("<Return>", lambda _e: self.start())

        btn_frm = ttk.Frame(frm)
        btn_frm.grid(row=3, column=0, columnspan=3, sticky="w", pady=4)
        self.start_btn = ttk.Button(btn_frm, text="开始采集", command=self.start, width=12)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(btn_frm, text="停止", command=self.stop, width=10, state="disabled")
        self.stop_btn.pack(side="left", padx=(0, 8))
        ttk.Button(btn_frm, text="打开配置", command=self.open_config, width=10).pack(side="left")

        self.status_var = tk.StringVar(value="状态：就绪")
        ttk.Label(frm, textvariable=self.status_var, foreground="#0066cc").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(12, 2)
        )

        self.log_var = tk.StringVar(value="礼物日志：未开始")
        ttk.Label(frm, textvariable=self.log_var, wraplength=460, justify="left").grid(
            row=5, column=0, columnspan=3, sticky="w"
        )

        cfg_path = os.path.join(collect_gifts._APP, "config.json")
        ttk.Label(
            frm,
            text=f"配置文件：{cfg_path}",
            foreground="#888",
            wraplength=460,
            justify="left",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))

        if hint:
            ttk.Label(frm, text=f"⚠ {hint}", foreground="#cc6600", wraplength=460).grid(
                row=7, column=0, columnspan=3, sticky="w", pady=(6, 0)
            )

        frm.columnconfigure(0, weight=1)

    def _normalize_room_id(self, raw: str) -> str:
        text = raw.strip()
        if not text:
            raise ValueError("请输入房间 ID")
        m = re.search(r"live\.douyin\.com/(\d+)", text)
        if m:
            return m.group(1)
        if re.fullmatch(r"\d+", text):
            return text
        raise ValueError("房间 ID 格式不正确，请输入纯数字")

    def start(self) -> None:
        if self._running:
            return
        try:
            room_id = self._normalize_room_id(self.room_var.get())
        except ValueError as e:
            messagebox.showwarning("提示", str(e))
            return

        self._running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set(f"状态：连接中… room_id={room_id}")

        def on_started(_room: str, log_file: str) -> None:
            self._log_file = log_file
            self.root.after(0, lambda: self.status_var.set(f"状态：采集中 room_id={_room}"))
            self.root.after(0, lambda: self.log_var.set(f"礼物日志：{log_file}"))

        def worker() -> None:
            try:
                collect_gifts.run(
                    room_id,
                    show_join=False,
                    gifts_only=True,
                    on_started=on_started,
                )
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("运行失败", str(e)))
            finally:
                self.root.after(0, self._on_stopped)

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        if not self._running:
            return
        self.status_var.set("状态：正在停止…")
        collect_gifts.stop_collector()

    def _on_stopped(self) -> None:
        self._running = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set("状态：已停止")
        if self._log_file:
            self.log_var.set(f"礼物日志：{self._log_file}")

    def open_config(self) -> None:
        path = os.path.join(collect_gifts._APP, "config.json")
        if not os.path.isfile(path):
            collect_gifts.load_config()
        folder = os.path.dirname(path)
        if sys.platform == "win32":
            os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", folder], check=False)
        else:
            subprocess.run(["xdg-open", folder], check=False)


def main_gui() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "aqua")
    except tk.TclError:
        pass
    GiftCollectorApp(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (collect_gifts.stop_collector(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main_gui()
