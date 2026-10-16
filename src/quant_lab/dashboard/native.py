"""Local desktop display for the existing paper bot. No order submission API."""

import json
import logging
import os
import queue
import threading
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk
from zoneinfo import ZoneInfo

from quant_lab.dashboard.data import (
    NZ,
    ORDER_STATUS,
    STATUS,
    UTC,
    account_snapshot,
    local_status,
    process_alive,
    read_json,
    session_completed,
    tail_events,
    value,
)
from quant_lab.strategies.intraday import NY, ROOT, fetch_bars, session_time, timeout_request

BG, PANEL, EDGE = "#0b1220", "#121e30", "#24334b"
TEXT, MUTED, GREEN, RED, BLUE = "#edf4ff", "#90a4c0", "#55d6ab", "#fa8295", "#80b8ff"


class Dashboard:
    def __init__(self, window, root=ROOT, offline=False):
        self.window, self.root, self.offline = window, root, offline
        self.q, self.busy, self.snapshot = queue.Queue(), False, None
        self.closed = False
        window.title("SPY 模拟交易 · 控制台")
        width, height = (
            min(1320, window.winfo_screenwidth() - 70),
            min(880, window.winfo_screenheight() - 70),
        )
        window.geometry(f"{width}x{height}+25+20")
        window.minsize(min(width, 1050), min(height, 700))
        window.configure(bg=BG)
        window.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(window)
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            rowheight=27,
            borderwidth=0,
            font=("Microsoft YaHei UI", 10),
        )
        style.configure(
            "Treeview.Heading",
            background="#1b2b42",
            foreground=MUTED,
            relief="flat",
            font=("Microsoft YaHei UI", 10),
        )
        style.map("Treeview", background=[("selected", "#254566")])
        header = tk.Frame(window, bg="#101d30", padx=24, pady=15)
        header.pack(fill="x")
        self.label(header, "SPY  /  日内模拟交易", 19, bold=True).pack(side="left")
        self.label(header, "  PAPER ONLY  ·  模拟资金  ", 10, color=GREEN).pack(side="right")
        body = tk.Frame(window, bg=BG, padx=24, pady=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=3)
        body.rowconfigure(3, weight=2)
        headline = tk.Frame(body, bg=BG)
        headline.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self.status = self.label(headline, "正在读取运行状态…", 17, bold=True)
        self.status.pack(side="left")
        self.countdown = self.label(headline, "—", 12, color=BLUE)
        self.countdown.pack(side="right")
        cards = tk.Frame(body, bg=BG)
        cards.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        self.metrics = {}
        for col, (key, title) in enumerate(
            [
                ("equity", "账户净值  USD"),
                ("pnl", "今日账户盈亏  USD"),
                ("position", "SPY 持仓"),
                ("cash", "可用现金  USD"),
            ]
        ):
            cards.columnconfigure(col, weight=1, uniform="cards")
            card = tk.Frame(
                cards, bg=PANEL, padx=17, pady=12, highlightthickness=1, highlightbackground=EDGE
            )
            card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 10, 0))
            self.label(card, title, 10, color=MUTED).pack(anchor="w")
            big = self.label(card, "—", 24, bold=True)
            big.pack(anchor="w", pady=(5, 2))
            sub = self.label(card, "等待账户数据", 9, color=MUTED)
            sub.pack(anchor="w")
            self.metrics[key] = (big, sub)
        center = tk.Frame(body, bg=BG)
        center.grid(row=2, column=0, sticky="nsew", pady=(0, 14))
        center.columnconfigure(0, weight=1)
        center.columnconfigure(1, weight=0, minsize=260)
        center.rowconfigure(0, weight=1)
        chartbox = tk.Frame(
            center, bg=PANEL, padx=14, pady=10, highlightbackground=EDGE, highlightthickness=1
        )
        chartbox.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        charttop = tk.Frame(chartbox, bg=PANEL)
        charttop.pack(fill="x")
        self.label(charttop, "SPY · 五分钟行情", 12, bold=True).pack(side="left")
        self.chart_caption = self.label(charttop, "正在读取 IEX 行情", 9, color=MUTED)
        self.chart_caption.pack(side="right")
        self.chart = tk.Canvas(chartbox, bg=PANEL, highlightthickness=0, height=220)
        self.chart.pack(fill="both", expand=True, pady=(8, 0))
        self.chart.bind("<Configure>", lambda event: self.draw_chart())
        rules = tk.Frame(
            center, bg=PANEL, padx=17, pady=14, highlightbackground=EDGE, highlightthickness=1
        )
        rules.grid(row=0, column=1, sticky="nsew")
        self.label(rules, "本场策略与风控", 12, bold=True).pack(anchor="w")
        for title, desc in [
            ("开盘区间突破", "前 15 分钟建区间 · 5 分钟确认"),
            ("最多 5% 仓位", "多空共用 · 每日最多 3 次入场提示"),
            ("止损约 1% · 止盈约 2%", "买单附带券商端保护单"),
            ("单日亏损阈值 20%", "触发后停止入场并尝试退出"),
            ("收盘前 10 分钟退出", "仅授权本场，结束后不会续开"),
        ]:
            self.label(rules, title, 10, color=TEXT).pack(anchor="w", pady=(12, 1))
            self.label(rules, desc, 8, color=MUTED).pack(anchor="w")
        self.stop_button = tk.Button(
            rules,
            text="请求停止并平仓",
            command=self.request_stop,
            bg="#51303c",
            fg="#ffd9df",
            activebackground="#694050",
            activeforeground=TEXT,
            font=("Microsoft YaHei UI", 10),
            relief="flat",
            padx=12,
            pady=8,
            cursor="hand2",
        )
        self.stop_button.pack(fill="x", pady=(16, 0), side="bottom")
        ordersbox = tk.Frame(
            body, bg=PANEL, padx=14, pady=10, highlightbackground=EDGE, highlightthickness=1
        )
        ordersbox.grid(row=3, column=0, sticky="nsew", pady=(0, 12))
        self.label(ordersbox, "最近 SPY 订单  ·  含保护单及此前订单", 12, bold=True).pack(
            anchor="w", pady=(0, 8)
        )
        wrap = tk.Frame(ordersbox, bg=PANEL)
        wrap.pack(fill="both", expand=True)
        cols = ("time", "side", "qty", "filled", "price", "type", "status")
        self.table = ttk.Treeview(wrap, columns=cols, show="headings", height=5)
        for key, title, width in zip(
            cols,
            ["美东提交时间", "方向", "数量", "已成交", "成交均价", "订单类型", "状态"],
            [180, 70, 80, 80, 110, 100, 160],
        ):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=60, anchor="center", stretch=True)
        scroll = ttk.Scrollbar(wrap, command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.table.pack(fill="both", expand=True)
        self.table.tag_configure("buy", foreground=GREEN)
        self.table.tag_configure("sell", foreground=BLUE)
        footer = tk.Frame(body, bg=BG)
        footer.grid(row=4, column=0, sticky="ew")
        self.health = self.label(footer, "账户数据每 30 秒刷新 · 运行心跳每秒检查", 9, color=MUTED)
        self.health.pack(side="left")
        for text, command in [("立即刷新", self.refresh), ("查看日志", self.open_log)]:
            tk.Button(
                footer,
                text=text,
                command=command,
                bg="#20314a",
                fg=TEXT,
                relief="flat",
                padx=12,
                pady=6,
                cursor="hand2",
                font=("Microsoft YaHei UI", 9),
            ).pack(side="right", padx=(8, 0))
        self.label(
            body,
            "关闭此窗口不会停止后台交易。行情为 IEX 单一交易所数据；退出及止损成交不作保证。",
            8,
            color=MUTED,
        ).grid(row=5, column=0, sticky="w", pady=(8, 0))
        if not offline:
            window.after(100, self.refresh)
            window.after(500, self.pulse)
            window.after(30000, self.periodic)

    def label(self, parent, text, size, color=TEXT, bold=False):
        return tk.Label(
            parent,
            text=text,
            fg=color,
            bg=parent.cget("bg"),
            font=("Microsoft YaHei UI", size, "bold" if bold else "normal"),
        )

    def refresh(self):
        if self.busy or self.offline or self.closed:
            return
        self.busy = True
        self.health.configure(text="正在更新模拟账户与行情…", fg=BLUE)

        def work():
            try:
                self.q.put(("data", account_snapshot(self.root)))
            except Exception as exc:
                self.q.put(("error", type(exc).__name__))

        threading.Thread(target=work, daemon=True).start()

    def periodic(self):
        if self.closed:
            return
        self.refresh()
        self.window.after(30000, self.periodic)

    def pulse(self):
        if self.closed:
            return
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self.busy = False
                if kind == "data":
                    self.render(payload)
                else:
                    self.health.configure(
                        text=f"账户刷新失败（{payload}）；保留上次数据，勿视为最新", fg=RED
                    )
        except queue.Empty:
            pass
        state = local_status(self.root)
        last = state["last"]
        label = STATUS.get(last.get("status"), last.get("status", "尚无运行记录"))
        completed = session_completed(state, self.snapshot)
        color = GREEN if state["healthy"] else MUTED if completed else RED
        prefix = (
            "● 本场已结束，下场需重新授权启动"
            if completed
            else "● 后台运行中"
            if state["healthy"]
            else ("● 心跳过期" if state["alive"] else "● 后台未运行")
        )
        self.status.configure(text=f"{prefix}  ·  {label}", fg=color)
        if state["stopped"]:
            self.stop_button.configure(text="已请求停止，等待确认", state="disabled")
        now = datetime.now(UTC)
        target = (self.snapshot or {}).get("next_open") or last.get("next_open_nz")
        if self.snapshot and self.snapshot.get("market_open"):
            target = self.snapshot.get("close")
            prefix = "距收盘"
        else:
            prefix = "距开盘"
        try:
            delta = max(0, int((datetime.fromisoformat(target) - now).total_seconds()))
            h, remain = divmod(delta, 3600)
            m, s = divmod(remain, 60)
            self.countdown.configure(
                text=f"{prefix}  {h:02}:{m:02}:{s:02}  ·  奥克兰 {now.astimezone(NZ):%H:%M:%S}"
            )
        except (TypeError, ValueError):
            self.countdown.configure(text=f"奥克兰 {now.astimezone(NZ):%m-%d %H:%M:%S}")
        self.window.after(1000, self.pulse)

    def render(self, data):
        self.snapshot = data
        self.metrics["equity"][0].configure(text=f"${data['equity']:,.2f}")
        self.metrics["equity"][1].configure(text=f"本场基准 ${data['baseline']:,.2f}")
        self.metrics["pnl"][0].configure(
            text=f"{data['pnl']:+,.2f}", fg=GREEN if data["pnl"] >= 0 else RED
        )
        self.metrics["pnl"][1].configure(
            text=f"{data['daily_pct']:+.2f}% · 含持仓浮动盈亏"
            if data["daily_pct"] is not None
            else "基准不可用"
        )
        self.metrics["position"][0].configure(text=f"{data['qty']:g} 股")
        if data["qty"] < 0:
            self.metrics["position"][0].configure(text=f"空头 {abs(data['qty']):g} 股")
        self.metrics["position"][1].configure(
            text=f"市值 ${data['position_value']:,.2f} · 浮盈亏 {data['position_pnl']:+,.2f}"
        )
        self.metrics["cash"][0].configure(text=f"${data['cash']:,.2f}")
        self.metrics["cash"][1].configure(text="仅模拟账户 · 不使用真实资金")
        self.table.delete(*self.table.get_children())
        for row in data["rows"]:
            price = f"${float(row['price']):,.2f}" if row["price"] is not None else "—"
            self.table.insert(
                "",
                "end",
                values=(
                    row["time"],
                    row["side"],
                    row["qty"],
                    row["filled"],
                    price,
                    row["type"],
                    row["status"],
                ),
                tags=("buy" if row["side"] == "买入" else "sell",),
            )
        if not data["rows"]:
            self.table.insert("", "end", values=("暂无 SPY 订单", "", "", "", "", "", ""))
        stamp = datetime.fromisoformat(data["fetched"]).astimezone(NZ)
        self.health.configure(
            text=f"账户更新于 {stamp:%H:%M:%S} · 自动刷新 30 秒"
            + (" · 行情暂不可用" if data.get("chart_error") else ""),
            fg=MUTED,
        )
        self.chart_caption.configure(text=f"{data.get('chart_date', '—')} 美东 · IEX · 已完成 K 线")
        self.draw_chart()

    def draw_chart(self):
        canvas = self.chart
        canvas.delete("all")
        w, h = canvas.winfo_width(), canvas.winfo_height()
        bars = (self.snapshot or {}).get("bars", [])
        if not bars or w < 150 or h < 100:
            canvas.create_text(
                max(w / 2, 80),
                max(h / 2, 60),
                text="等待可用的五分钟行情",
                fill=MUTED,
                font=("Microsoft YaHei UI", 11),
            )
            return
        left, right, top, bottom = 12, w - 70, 12, h - 25
        low, high = min(b["low"] for b in bars), max(b["high"] for b in bars)
        pad = max((high - low) * 0.12, 0.1)
        low, high = low - pad, high + pad

        def y(price):
            return bottom - (price - low) / (high - low) * (bottom - top)

        times = [datetime.fromisoformat(b["time"]) for b in bars]
        span = max(300, (times[-1] - times[0]).total_seconds())

        def x(stamp):
            return left + 6 + (stamp - times[0]).total_seconds() / span * max(1, right - left - 12)

        for i in range(5):
            price = low + (high - low) * i / 4
            yy = y(price)
            canvas.create_line(left, yy, right, yy, fill=EDGE, dash=(2, 5))
            canvas.create_text(
                right + 8, yy, text=f"{price:.2f}", anchor="w", fill=MUTED, font=("Segoe UI", 9)
            )
        bw = max(2, min(10, (right - left) / max(len(bars), 1) * 0.6))
        for b, stamp in zip(bars, times):
            xx, color = x(stamp), GREEN if b["close"] >= b["open"] else RED
            canvas.create_line(xx, y(b["high"]), xx, y(b["low"]), fill=color)
            a, z = sorted([y(b["open"]), y(b["close"])])
            canvas.create_rectangle(
                xx - bw / 2, a, xx + bw / 2, max(a + 1, z), fill=color, outline=color
            )
        for idx in sorted(set([0, len(bars) // 2, len(bars) - 1])):
            canvas.create_text(
                x(times[idx]),
                h - 9,
                text=times[idx].strftime("%H:%M"),
                fill=MUTED,
                font=("Segoe UI", 9),
            )
        opening = [b for b, t in zip(bars, times) if t.hour == 9 and t.minute in (30, 35, 40)]
        if len(opening) == 3:
            for price, label in [
                (max(b["high"] for b in opening), "开盘区间上沿"),
                (min(b["low"] for b in opening), "开盘区间下沿"),
            ]:
                canvas.create_line(left, y(price), right, y(price), fill=BLUE, dash=(6, 4))
                canvas.create_text(
                    left + 4,
                    y(price) - 8,
                    text=label,
                    fill=BLUE,
                    anchor="w",
                    font=("Microsoft YaHei UI", 8),
                )

    def request_stop(self):
        if messagebox.askyesno(
            "停止模拟交易",
            "请求后台停止新入场，并在正常交易时段撤销 SPY 订单、尝试平仓？\n\n需要联网及券商确认；这不是立即成交保证。",
            parent=self.window,
        ):
            try:
                (self.root / "STOP_PAPER").write_text("STOP\n", encoding="utf-8")
                self.stop_button.configure(text="已请求停止，等待确认", state="disabled")
            except OSError:
                messagebox.showerror(
                    "未能发出请求", "无法写入停止标志；请检查本地目录权限。", parent=self.window
                )

    def open_log(self):
        path = self.root / "logs" / "paper_execution.jsonl"
        if path.exists():
            os.startfile(str(path))

    def close(self):
        self.closed = True
        self.window.destroy()


def main():
    logging.getLogger("alpaca").setLevel(logging.CRITICAL)
    window = tk.Tk()
    Dashboard(window)
    window.mainloop()


if __name__ == "__main__":
    main()
