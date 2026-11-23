#!/usr/bin/env python3
"""K12 知识点地图桌面管理台（macOS / tkinter）。

双击 .app 启动，或 python3 desktop/src/app.py。
仅依赖 Python 标准库 + 系统 Tcl/Tk。
"""
from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402

APP_TITLE = "K12 知识点地图 · 管理台"
VERSION = "1.2"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1360x860")
        self.root.minsize(1080, 700)

        self.tax = store.load_taxonomy()
        self.current_volume: str = ""          # 选中的册 volume_id
        self.current_rows = []                 # 当前列表 [(kp, vol)]
        self.selected_id: str = ""             # 选中的知识点 id

        self._build_style()
        self._build_menu()
        self._build_layout()
        self._refresh_tree()
        self._update_status()

    # ---------------- 样式 ----------------
    def _build_style(self):
        st = ttk.Style()
        if "aqua" in st.theme_names():
            st.theme_use("aqua")
        st.configure("Treeview", rowheight=25)
        st.configure("TButton", padding=5)
        st.configure("Toolbar.TButton", padding=6)
        st.configure("Title.TLabel", font=("", 15, "bold"))
        st.configure("Sub.TLabel", foreground="#555555")
        st.configure("Head.TLabel", font=("", 11, "bold"))

    def _build_menu(self):
        menubar = tk.Menu(self.root)

        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="导出当前结果 (CSV)", command=lambda: self.export("csv"))
        m_file.add_command(label="导出当前结果 (JSON)", command=lambda: self.export("json"))
        m_file.add_separator()
        m_file.add_command(label="打开数据目录", command=self.open_data_dir)
        m_file.add_command(label="打开变更日志", command=self.open_changelog)
        m_file.add_separator()
        m_file.add_command(label="退出", command=self.root.quit)
        menubar.add_cascade(label="文件", menu=m_file)

        m_edit = tk.Menu(menubar, tearoff=0)
        m_edit.add_command(label="新增知识点", command=self.add_dialog)
        m_edit.add_command(label="编辑所选", command=self.edit_dialog)
        m_edit.add_command(label="删除所选", command=self.delete_selected)
        m_edit.add_separator()
        m_edit.add_command(label="撤销上次变更", command=self.undo)
        menubar.add_cascade(label="编辑", menu=m_edit)

        m_tools = tk.Menu(menubar, tearoff=0)
        m_tools.add_command(label="生成题目（所选知识点）", command=self.generate_questions)
        m_tools.add_command(label="批量生成（当前列表）", command=self.generate_batch_questions)
        m_tools.add_separator()
        m_tools.add_command(label="运行全部校验", command=self.run_checks)
        m_tools.add_command(label="刷新数据", command=self.reload_all)
        menubar.add_cascade(label="工具", menu=m_tools)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="使用说明", command=self.help_dialog)
        m_help.add_command(label=f"关于（v{VERSION}）", command=self.about_dialog)
        menubar.add_cascade(label="帮助", menu=m_help)

        self.root.config(menu=menubar)

    # ---------------- 布局 ----------------
    def _build_layout(self):
        # 顶部标题
        head = ttk.Frame(self.root, padding=(14, 10))
        head.pack(fill="x")
        ttk.Label(head, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        self.lbl_stat = ttk.Label(head, text="", style="Sub.TLabel")
        self.lbl_stat.pack(side="left", padx=16)

        # 主体：左右分栏
        main = ttk.PanedWindow(self.root, orient="horizontal")
        main.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        # 左：册树
        left = ttk.Frame(main, width=270)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        ttk.Label(left, text="知识地图", style="Head.TLabel").grid(row=0, column=0, sticky="w", pady=(4, 6))
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        main.add(left, weight=0)

        # 右：筛选 + 列表 + 详情
        right = ttk.Frame(main)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=3)
        right.rowconfigure(4, weight=2)

        # 筛选栏
        flt = ttk.Frame(right)
        flt.grid(row=0, column=0, sticky="ew", pady=(4, 6))
        ttk.Label(flt, text="搜索").pack(side="left")
        self.var_q = tk.StringVar()
        e = ttk.Entry(flt, textvariable=self.var_q, width=26)
        e.pack(side="left", padx=(4, 10))
        e.bind("<Return>", lambda _: self.refresh_list())
        ttk.Button(flt, text="查找", command=self.refresh_list).pack(side="left", padx=(0, 12))

        ttk.Label(flt, text="难度").pack(side="left")
        self.var_diff = tk.StringVar(value="")
        cb = ttk.Combobox(flt, textvariable=self.var_diff, width=7,
                          values=["", "1", "2", "3", "4"], state="readonly")
        cb.pack(side="left", padx=(4, 10))
        cb.bind("<<ComboboxSelected>>", lambda _: self.refresh_list())

        ttk.Label(flt, text="认知").pack(side="left")
        self.var_bloom = tk.StringVar(value="")
        cb2 = ttk.Combobox(flt, textvariable=self.var_bloom, width=7,
                           values=[""] + self.tax["bloom"], state="readonly")
        cb2.pack(side="left", padx=(4, 10))
        cb2.bind("<<ComboboxSelected>>", lambda _: self.refresh_list())

        ttk.Label(flt, text="引擎").pack(side="left")
        self.var_engine = tk.StringVar(value="")
        cb3 = ttk.Combobox(flt, textvariable=self.var_engine, width=9,
                           values=["", "template", "llm"], state="readonly")
        cb3.pack(side="left", padx=(4, 10))
        cb3.bind("<<ComboboxSelected>>", lambda _: self.refresh_list())
        ttk.Button(flt, text="清除筛选", command=self.clear_filters).pack(side="left")

        # 列表
        self.lbl_list = ttk.Label(right, text="知识点", style="Head.TLabel")
        self.lbl_list.grid(row=1, column=0, sticky="w")
        cols = ("id", "name", "domain", "difficulty", "bloom", "engine_hint")
        self.list = ttk.Treeview(right, columns=cols, show="headings", selectmode="browse")
        heads = {"id": "ID", "name": "知识点", "domain": "课标领域",
                 "difficulty": "难度", "bloom": "认知层级", "engine_hint": "引擎"}
        widths = {"id": 190, "name": 300, "domain": 150,
                  "difficulty": 55, "bloom": 80, "engine_hint": 80}
        for c in cols:
            self.list.heading(c, text=heads[c], command=lambda cc=c: self.sort_by(cc))
            self.list.column(c, width=widths[c], anchor="w")
        self.list.grid(row=2, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(right, orient="vertical", command=self.list.yview)
        lsb.grid(row=2, column=1, sticky="ns")
        self.list.configure(yscrollcommand=lsb.set)
        self.list.bind("<<TreeviewSelect>>", self.on_list_select)
        self.list.bind("<Double-1>", lambda _: self.edit_dialog())

        # 详情
        self.lbl_detail = ttk.Label(right, text="详情（选中知识点）", style="Head.TLabel")
        self.lbl_detail.grid(row=3, column=0, sticky="w", pady=(8, 4))
        self.detail = scrolledtext.ScrolledText(right, wrap="word", height=10,
                                                font=("", 12), background="#FBFBFB")
        self.detail.grid(row=4, column=0, sticky="nsew", columnspan=2)
        self.detail.configure(state="disabled")
        main.add(right, weight=1)

        # 底部工具栏
        bar = ttk.Frame(self.root, padding=(10, 8))
        bar.pack(fill="x")
        for text, cmd in [
            ("新增知识点", self.add_dialog),
            ("编辑所选", self.edit_dialog),
            ("删除所选", self.delete_selected),
            ("撤销", self.undo),
            ("生成题目", self.generate_questions),
            ("批量生成", self.generate_batch_questions),
            ("运行校验", self.run_checks),
            ("导出 CSV", lambda: self.export("csv")),
            ("刷新", self.reload_all),
        ]:
            ttk.Button(bar, text=text, command=cmd, style="Toolbar.TButton").pack(side="left", padx=(0, 8))
        self.lbl_msg = ttk.Label(bar, text="", foreground="#0F6E56")
        self.lbl_msg.pack(side="right")

    # ---------------- 树 ----------------
    def _refresh_tree(self, keep: str = ""):
        self.tree.delete(*self.tree.get_children())
        vols = store.load_all_volumes()
        stages: dict = {}
        for v in vols:
            s = stages.setdefault(v["stage"], {})
            sub = s.setdefault(v["subject"], {})
            sub.setdefault(v["grade"], []).append(v)

        for stage in ("kg", "primary", "junior"):
            if stage not in stages:
                continue
            sname = self.tax["stage_names"].get(stage, stage)
            sid = f"stage::{stage}"
            self.tree.insert("", "end", sid, text=f"{sname}（{sum(len(x) for g in stages[stage].values() for x in g.values())} 点）", open=True)
            for subject in ("chinese", "math", "english"):
                if subject not in stages[stage]:
                    continue
                suname = self.tax["subject_names"].get(subject, subject)
                uid = f"subject::{stage}::{subject}"
                self.tree.insert(sid, "end", uid, text=suname, open=True)
                for grade in sorted(stages[stage][subject],
                                    key=lambda g: store.GRADE_ORDER.get(g, 9)):
                    glabel = store._grade_label(grade)
                    gid = f"grade::{stage}::{subject}::{grade}"
                    self.tree.insert(uid, "end", gid, text=glabel, open=True)
                    for v in stages[stage][subject][grade]:
                        vid = f"vol::{v['volume_id']}"
                        self.tree.insert(gid, "end", vid,
                                         text=f"{self.tax['term_names'].get(v['term'], v['term'])} · {len(v['knowledge_points'])} 点")
        if keep:
            try:
                self.tree.selection_set(keep)
                self.tree.see(keep)
            except tk.TclError:
                pass

    def on_tree_select(self, _=None):
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        if not item.startswith("vol::"):
            return
        self.current_volume = item.split("::", 1)[1]
        self.selected_id = ""
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.configure(state="disabled")
        self.refresh_list()

    # ---------------- 列表 ----------------
    def refresh_list(self):
        rows = store.query(
            q=self.var_q.get(),
            stage="", subject="",
            domain="",
            difficulty=int(self.var_diff.get()) if self.var_diff.get() else None,
            bloom=self.var_bloom.get(),
            engine=self.var_engine.get(),
        )
        if self.current_volume:
            rows = [(kp, v) for kp, v in rows if v["volume_id"] == self.current_volume]
        self.current_rows = rows
        self.list.delete(*self.list.get_children())
        for kp, v in rows:
            self.list.insert("", "end", iid=kp["id"], values=(
                kp["id"], kp["name"], kp.get("domain", ""),
                kp.get("difficulty", ""), kp.get("bloom", ""),
                kp.get("engine_hint", ""),
            ))
        vol = next((v for v in store.load_all_volumes()
                    if v["volume_id"] == self.current_volume), None)
        scope = vol["title"] if vol else "全部册"
        self.lbl_list.configure(text=f"知识点 · {scope}（{len(rows)} 条）")

    def sort_by(self, col: str):
        rows = sorted(self.current_rows, key=lambda pair: str(pair[0].get(col, "")))
        self.current_rows = rows
        self.list.delete(*self.list.get_children())
        for kp, v in rows:
            self.list.insert("", "end", iid=kp["id"], values=(
                kp["id"], kp["name"], kp.get("domain", ""),
                kp.get("difficulty", ""), kp.get("bloom", ""),
                kp.get("engine_hint", ""),
            ))

    def on_list_select(self, _=None):
        sel = self.list.selection()
        if not sel:
            return
        self.selected_id = sel[0]
        found = store.find_point(self.selected_id)
        if not found:
            return
        kp, v = found
        self._show_detail(kp, v)

    def _show_detail(self, kp, v):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", f"{kp['name']}\n", ("title",))
        self.detail.insert("end", f"ID: {kp['id']}    册: {v['title']}    序号: {kp['index']}\n\n")
        fields = [
            ("课标领域 domain", kp.get("domain")),
            ("主题 theme", kp.get("theme")),
            ("单元 unit", kp.get("unit")),
            ("学习目标 objective", kp.get("objective")),
            ("难度 difficulty", f"{kp.get('difficulty')} · {self.tax['difficulty'].get(str(kp.get('difficulty')), '')}"),
            ("认知层级 bloom", kp.get("bloom")),
            ("前置关系 prerequisites", "、".join(kp.get("prerequisites", [])) or "无"),
            ("核心概念 concept", kp.get("concept")),
            ("易错点 common_mistakes", "\n    · ".join(kp.get("common_mistakes", []))),
            ("建议题型 suggested_types", "、".join(kp.get("suggested_types", []))),
            ("年级范围 grade_range", "、".join(kp.get("grade_range", []))),
            ("权威来源 source", kp.get("source")),
            ("引擎提示 engine_hint", kp.get("engine_hint")),
        ]
        for label, value in fields:
            self.detail.insert("end", f"{label}\n", ("label",))
            self.detail.insert("end", f"    {value or '—'}\n\n")
        self.detail.tag_configure("title", font=("", 14, "bold"))
        self.detail.tag_configure("label", font=("", 11, "bold"), foreground="#185FA5")
        self.detail.configure(state="disabled")

    def clear_filters(self):
        self.var_q.set("")
        self.var_diff.set("")
        self.var_bloom.set("")
        self.var_engine.set("")
        self.refresh_list()

    # ---------------- 编辑弹窗 ----------------
    def _editor(self, title: str, values: dict, on_save):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("760x760")
        win.transient(self.root)
        win.grab_set()

        canvas = tk.Canvas(win)
        sb = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        form = ttk.Frame(canvas, padding=14)
        canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        entries = {}
        labels = {
            "domain": "课标领域 *", "theme": "主题 *", "unit": "单元 *",
            "name": "知识点名称 *", "objective": "学习目标 *",
            "difficulty": "难度（1-4）*", "bloom": "认知层级 *",
            "prerequisites": "前置关系（ID，逗号分隔）",
            "concept": "核心概念 *", "common_mistakes": "易错点（逗号分隔）",
            "suggested_types": "建议题型（逗号分隔）",
            "grade_range": "年级范围（逗号分隔）",
            "source": "权威来源 *", "engine_hint": "引擎（template / llm）",
        }
        r = 0
        for key in store.EDITABLE:
            ttk.Label(form, text=labels[key], style="Head.TLabel").grid(row=r, column=0, sticky="nw", pady=(8, 2))
            r += 1
            val = values.get(key, "")
            if isinstance(val, list):
                val = ", ".join(str(x) for x in val)
            if key == "difficulty":
                w = ttk.Combobox(form, values=["1", "2", "3", "4"], width=60)
                w.set(str(val))
            elif key == "bloom":
                w = ttk.Combobox(form, values=self.tax["bloom"], width=60)
                w.set(str(val))
            elif key == "engine_hint":
                w = ttk.Combobox(form, values=["template", "llm"], width=60)
                w.set(str(val))
            elif key in ("objective", "concept"):
                w = tk.Text(form, width=68, height=5, wrap="word")
                w.insert("1.0", str(val))
                w._is_text = True
            else:
                w = ttk.Entry(form, width=68)
                w.insert(0, str(val))
            w.grid(row=r, column=0, sticky="ew", pady=(0, 4))
            entries[key] = w
            r += 1

        def collect():
            out = {}
            for k, w in entries.items():
                if getattr(w, "_is_text", False):
                    out[k] = w.get("1.0", "end").strip()
                else:
                    out[k] = w.get().strip()
            return out

        def save():
            try:
                on_save(collect())
            except store.StoreError as e:
                messagebox.showerror("保存失败", str(e), parent=win)
                return
            except Exception as e:
                messagebox.showerror("保存失败", str(e), parent=win)
                return
            win.destroy()
            self.reload_all()

        btn = ttk.Frame(form, padding=(0, 14))
        btn.grid(row=r, column=0, sticky="ew")
        ttk.Button(btn, text="保存", command=save).pack(side="right", padx=(8, 0))
        ttk.Button(btn, text="取消", command=win.destroy).pack(side="right")

    def edit_dialog(self):
        if not self.selected_id:
            messagebox.showinfo("提示", "请先在列表中选择一个知识点")
            return
        found = store.find_point(self.selected_id)
        if not found:
            return
        kp, _ = found
        self._editor(f"编辑知识点 · {self.selected_id}", kp,
                     lambda vals: self._do_update(self.selected_id, vals))

    def _do_update(self, tid, vals):
        store.update_point(tid, vals)
        self.toast(f"已更新 {tid}")
        self.selected_id = tid

    def add_dialog(self):
        if not self.current_volume:
            messagebox.showinfo("提示", "请先在左侧选择一个册，再新增知识点")
            return
        tpl = {
            "domain": "", "theme": "", "unit": "", "name": "",
            "objective": "", "difficulty": 2, "bloom": "理解",
            "prerequisites": [], "concept": "", "common_mistakes": [],
            "suggested_types": [], "grade_range": [], "source": "",
            "engine_hint": "template",
        }
        self._editor(f"新增知识点 · {self.current_volume}", tpl, self._do_add)

    def _do_add(self, vals):
        tid = store.add_point(self.current_volume, vals)
        self.selected_id = tid
        self.toast(f"已新增 {tid}")

    def delete_selected(self):
        if not self.selected_id:
            messagebox.showinfo("提示", "请先在列表中选择一个知识点")
            return
        kp, v = store.find_point(self.selected_id)
        if not messagebox.askyesno("确认删除",
                                   f"确定删除？\n\n{kp['name']}\n{self.selected_id}\n\n（可用「撤销」恢复）"):
            return
        try:
            store.delete_point(self.selected_id)
        except store.StoreError as e:
            messagebox.showerror("删除失败", str(e))
            return
        self.selected_id = ""
        self.toast(f"已删除 {kp['name']}")
        self.reload_all()

    def undo(self):
        try:
            msg = store.undo()
        except store.StoreError as e:
            messagebox.showinfo("无法撤销", str(e))
            return
        self.toast(msg)
        self.reload_all()

    # ---------------- 工具 ----------------
    def generate_questions(self):
        if not self.selected_id:
            messagebox.showinfo("提示", "请先在列表中选择一个知识点")
            return
        found = store.find_point(self.selected_id)
        if not found:
            return
        kp, _ = found
        QuestionDialog(self.root, [kp], title=f"题目生成 · {kp['name']}")

    def generate_batch_questions(self):
        rows = self.current_rows
        if not rows:
            messagebox.showinfo("提示", "当前列表没有知识点，请先选择册或设置筛选")
            return
        if len(rows) > 150 and not messagebox.askyesno(
                "批量生成",
                f"当前列表有 {len(rows)} 个知识点，批量生成需要一点时间。继续？"):
            return
        QuestionDialog(self.root, [kp for kp, _ in rows],
                       title=f"批量题目生成 · {len(rows)} 个知识点")

    def run_checks(self):
        win = tk.Toplevel(self.root)
        win.title("校验结果")
        win.geometry("900x680")
        txt = scrolledtext.ScrolledText(win, wrap="none", font=("Menlo", 11))
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        txt.insert("end", "正在运行校验，请稍候…\n")
        txt.configure(state="disabled")
        win.update()

        def work():
            out = store.run_checks()
            self.root.after(0, lambda: self._show_checks(txt, out))
        import threading
        threading.Thread(target=work, daemon=True).start()

    def _show_checks(self, txt, out):
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        txt.insert("end", out)
        txt.configure(state="disabled")

    def export(self, fmt: str):
        rows = self.current_rows
        if not rows:
            messagebox.showinfo("提示", "当前没有可导出的知识点")
            return
        ext = "csv" if fmt == "csv" else "json"
        path = filedialog.asksaveasfilename(
            title=f"导出 {ext.upper()}", defaultextension=f".{ext}",
            filetypes=[(f"{ext.upper()} 文件", f"*.{ext}"), ("所有文件", "*.*")])
        if not path:
            return
        content = store.export_csv(rows) if fmt == "csv" else store.export_json(rows)
        with open(path, "w", encoding="utf-8-sig" if fmt == "csv" else "utf-8") as f:
            f.write(content)
        self.toast(f"已导出 {len(rows)} 条 → {os.path.basename(path)}")

    def open_data_dir(self):
        subprocess_open(store.VOL_ROOT)

    def open_changelog(self):
        path = store.CHANGELOG
        if not os.path.exists(path):
            messagebox.showinfo("提示", "还没有变更记录")
            return
        subprocess_open(path)

    def reload_all(self):
        self.tax = store.load_taxonomy()
        self._refresh_tree(keep=f"vol::{self.current_volume}" if self.current_volume else "")
        self.refresh_list()
        if self.selected_id:
            found = store.find_point(self.selected_id)
            if found:
                self._show_detail(*found)
        self._update_status()

    def _update_status(self):
        s = store.stats()
        parts = [f"{store.STAGE_NAMES[k]} {v.get('chinese',0)+v.get('math',0)+v.get('english',0)}"
                 for k, v in s["by_stage"].items()]
        self.lbl_stat.configure(text=f"{s['volumes']} 册 · {s['points']} 知识点 · " + " / ".join(parts))

    def toast(self, msg: str):
        self.lbl_msg.configure(text=msg)
        self.root.after(5000, lambda: self.lbl_msg.configure(text=""))

    def help_dialog(self):
        messagebox.showinfo("使用说明", """1. 左侧树按 学段 → 科目 → 年级 → 册 展开，点册查看知识点。
2. 顶部可按关键词、难度、认知层级、引擎筛选；点列标题排序。
3. 双击列表项或点「编辑所选」修改知识点。
4. 编辑直接写入 knowledge-map/volumes/ 下的 JSON（下游出题用的数据）。
   每次写操作都会自动备份到 desktop/backups/，并记入 changelog.jsonl。
5. 「撤销」恢复最近一次变更所在册的完整快照。
6. 「运行校验」调用结构校验 + 字段校验 + 课标交叉校验三套脚本。
7. 「生成题目」用内容感知引擎为所选知识点现场出题；「批量生成」对当前列表
   统一出题并全局去重。可设置每点题数与随机种子，双击题目看完整选项和解析，
   支持导出 JSONL / CSV。生成在后台线程进行，不卡界面。
8. 注意：volumes/ 是 data/*.py 生成的产物。若要永久固化修改，
   请把改动同步回 knowledge-map/data/ 下对应的源文件，再运行 build_volumes.py。""")

    def about_dialog(self):
        messagebox.showinfo("关于", f"{APP_TITLE}\nv{VERSION}\n\n63 册 / 1225 知识点\n内容感知型出题引擎 · Python 标准库 + Tcl/Tk")


class QuestionDialog:
    """题目生成窗口：设置参数 → 后台生成（不卡界面）→ 列表 / 详情 / 导出。"""

    def __init__(self, parent: tk.Tk, topics: list, title: str):
        self.topics = list(topics)          # [{知识点 dict}]
        self.questions: list = []           # [题目 dict]
        self.busy = False
        self.win = tk.Toplevel(parent)
        self.win.title(title)
        self.win.geometry("1000x680")
        self.win.minsize(820, 520)
        self.win.transient(parent)

        # 参数栏
        bar = ttk.Frame(self.win, padding=(12, 10))
        bar.pack(fill="x")
        ttk.Label(bar, text="每点题数").pack(side="left")
        self.var_n = tk.StringVar(value="12" if len(self.topics) == 1 else "6")
        ttk.Combobox(bar, textvariable=self.var_n, width=6,
                     values=["4", "6", "8", "12", "20", "30"],
                     state="readonly").pack(side="left", padx=(4, 14))
        ttk.Label(bar, text="随机种子").pack(side="left")
        self.var_seed = tk.StringVar(value="20261120")
        ttk.Entry(bar, textvariable=self.var_seed, width=12).pack(side="left", padx=(4, 14))
        self.btn_gen = ttk.Button(bar, text="生成题目", command=self.generate)
        self.btn_gen.pack(side="left")
        self.lbl_status = ttk.Label(bar, text="就绪", foreground="#0F6E56")
        self.lbl_status.pack(side="left", padx=14)

        self.pb = ttk.Progressbar(self.win, mode="determinate", maximum=len(self.topics))
        self.pb.pack(fill="x", padx=12)

        # 题目列表
        frame = ttk.Frame(self.win, padding=(12, 8))
        frame.pack(fill="both", expand=True)
        cols = ("no", "kind", "stem", "answer", "difficulty", "source")
        heads = {"no": "#", "kind": "题型", "stem": "题干", "answer": "答案",
                 "difficulty": "难度", "source": "来源"}
        widths = {"no": 42, "kind": 92, "stem": 540, "answer": 52,
                  "difficulty": 52, "source": 110}
        self.list = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self.list.heading(c, text=heads[c])
            self.list.column(c, width=widths[c], anchor="w")
        self.list.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.list.yview)
        sb.pack(side="right", fill="y")
        self.list.configure(yscrollcommand=sb.set)
        self.list.bind("<Double-1>", lambda _: self.show_detail())

        # 底部操作
        bottom = ttk.Frame(self.win, padding=(12, 10))
        bottom.pack(fill="x")
        for text, cmd in [
            ("查看完整题目", self.show_detail),
            ("导出 JSONL", lambda: self.export("jsonl")),
            ("导出 CSV", lambda: self.export("csv")),
        ]:
            ttk.Button(bottom, text=text, command=cmd).pack(side="left", padx=(0, 8))
        ttk.Button(bottom, text="关闭", command=self.win.destroy).pack(side="right")

    def _after(self, fn):
        """窗口可能已关闭，安全投递到主线程。"""
        try:
            self.win.after(0, fn)
        except tk.TclError:
            pass

    def generate(self):
        if self.busy:
            return
        try:
            n = int(self.var_n.get())
            seed = int(self.var_seed.get())
        except ValueError:
            messagebox.showerror("参数错误", "每点题数和随机种子必须是整数", parent=self.win)
            return
        self.busy = True
        self.btn_gen.configure(state="disabled")
        self.lbl_status.configure(text="生成中…")
        self.list.delete(*self.list.get_children())
        self.questions = []
        self.pb.configure(value=0, maximum=len(self.topics))

        def work():
            try:
                if len(self.topics) == 1:
                    qs = store.generate_for_point(self.topics[0]["id"], n, seed)
                    self._after(lambda: self.pb.configure(value=1))
                else:
                    def progress(i, total, acc):
                        self._after(lambda i=i, total=total, acc=acc: (
                            self.pb.configure(value=i),
                            self.lbl_status.configure(
                                text=f"生成中 {i}/{total} · 已入库 {acc} 题")))
                    ids = [kp["id"] for kp in self.topics]
                    qs = store.generate_batch(ids, n, seed, progress=progress)
                self._after(lambda: self._done(qs))
            except Exception as exc:  # noqa: BLE001 - 界面需展示任何失败
                self._after(lambda: self._error(str(exc)))

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _done(self, qs):
        self.questions = qs
        self.busy = False
        self.btn_gen.configure(state="normal")
        for i, q in enumerate(qs, 1):
            self.list.insert("", "end", iid=q["id"], values=(
                i, store.question_kind(q), q["stem"], q["answer"],
                q["difficulty"], q["source"]))
        self.lbl_status.configure(
            text=f"完成 · 生成 {len(qs)} 题" if qs else "完成 · 0 题（该知识点内容不足以命题）")

    def _error(self, msg):
        self.busy = False
        self.btn_gen.configure(state="normal")
        self.lbl_status.configure(text="生成失败")
        messagebox.showerror("生成失败", msg, parent=self.win)

    def show_detail(self):
        sel = self.list.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一道题目", parent=self.win)
            return
        q = next((x for x in self.questions if x["id"] == sel[0]), None)
        if not q:
            return
        win = tk.Toplevel(self.win)
        win.title("题目详情")
        win.geometry("760x560")
        win.transient(self.win)
        txt = scrolledtext.ScrolledText(win, wrap="word", font=("", 13), padx=14, pady=12)
        txt.pack(fill="both", expand=True)
        txt.insert("end", f"{store.question_kind(q)} · 难度 {q['difficulty']} · {q['bloom']}\n", ("meta",))
        txt.insert("end", f"知识点：{q['topic']}  ·  {q['grade']} {q['subject']}\n\n", ("meta",))
        txt.insert("end", q["stem"] + "\n\n", ("stem",))
        for i, opt in enumerate(q["options"]):
            letter = "ABCD"[i]
            correct = letter == q["answer"]
            txt.insert("end",
                       f"（{'答案' if correct else '选项'}） {letter}. {opt}\n",
                       ("correct" if correct else "opt",))
        txt.insert("end", "\n解析\n", ("label",))
        txt.insert("end", q["explanation"] + "\n\n", ("expl",))
        txt.insert("end", f"题目 ID：{q['id']}\n引擎：{q['source']} {q['generator_version']}", ("meta",))
        txt.tag_configure("meta", foreground="#666666", font=("", 11))
        txt.tag_configure("stem", font=("", 14, "bold"))
        txt.tag_configure("label", font=("", 12, "bold"), foreground="#185FA5")
        txt.tag_configure("correct", foreground="#0F6E56", font=("", 13, "bold"))
        txt.tag_configure("opt", font=("", 13))
        txt.configure(state="disabled")

    def export(self, fmt: str):
        if not self.questions:
            messagebox.showinfo("提示", "还没有题目可导出", parent=self.win)
            return
        path = filedialog.asksaveasfilename(
            parent=self.win, title=f"导出题目 {fmt.upper()}",
            defaultextension=f".{fmt}",
            filetypes=[(f"{fmt.upper()} 文件", f"*.{fmt}"), ("所有文件", "*.*")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(store.export_questions(self.questions, fmt))
        messagebox.showinfo("导出完成",
                            f"已导出 {len(self.questions)} 题 → {os.path.basename(path)}",
                            parent=self.win)


def subprocess_open(path: str):
    """用系统默认方式打开文件/目录（Mac: open）。"""
    import subprocess
    try:
        subprocess.run(["open", path], check=False)
    except Exception:
        pass


def main():
    root = tk.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
