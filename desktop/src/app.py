#!/usr/bin/env python3
"""K12 题库工厂桌面管理台（macOS / tkinter）。

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
import prompt as prompt_mod  # noqa: E402

APP_TITLE = "K12 题库工厂"
VERSION = "1.3.5"

# 难度 1-4 的文字描述（界面统一显示，不再写「难度1」）
DIFFICULTY_LABEL = {1: "容易", 2: "中等", 3: "较难", 4: "难"}
DIFFICULTY_CODE = {v: k for k, v in DIFFICULTY_LABEL.items()}

# AI 提示词模块的筛选值映射（界面显示 → 数据层代码）
# 「全部」为默认值，映射到空串表示不筛选
AI_STAGE = {"全部": "", "幼儿园": "kg", "小学": "primary", "初中": "junior"}
AI_SUBJECT = {"全部": "", "语文": "chinese", "数学": "math", "英语": "english"}
AI_GRADE = {"全部": "", "小班": "k1", "中班": "k2", "大班": "k3",
            "一年级": "g1", "二年级": "g2", "三年级": "g3",
            "四年级": "g4", "五年级": "g5", "六年级": "g6",
            "七年级": "g7", "八年级": "g8", "九年级": "g9"}
# 学段 → 可选年级（学段选定后年级下拉只显示对应范围）
STAGE_GRADE_LABELS = {
    "": [k for k in AI_GRADE if k != "全部"],
    "kg": ["小班", "中班", "大班"],
    "primary": ["一年级", "二年级", "三年级", "四年级", "五年级", "六年级"],
    "junior": ["七年级", "八年级", "九年级"],
}
# 每知识点题数：自定义 + 常用档位（含大批量 50 / 100）
QUESTION_COUNT_OPTIONS = ["自定义", "1", "3", "5", "10", "15", "20", "50", "100"]
# 大模型输出格式（界面显示 → 提示词引擎代码）
# Excel 用制表符表格：模型无法直接产出二进制 .xlsx，复制到 Excel 另存即可
AI_FORMAT = {"JSON": "json", "TXT": "txt", "Excel": "xlsx", "CSV": "csv"}
AI_PRESETS = {
    "自定义": None,
    "基础为主": {1: 50, 2: 30, 3: 15, 4: 5},
    "均衡": {1: 25, 2: 25, 3: 25, 4: 25},
    "进阶": {1: 10, 2: 30, 3: 40, 4: 20},
    "挑战": {1: 5, 2: 15, 3: 30, 4: 50},
}


class ClickCombo(tk.Menubutton):
    """单击展开、单击选项即完成选择的下拉菜单。

    macOS 的 ttk.Combobox（aqua 主题）常需多次点击才能选中，这里改用原生
    Menubutton + Menu：点一下展开，再点一下选项即写入变量并触发 command。
    对外提供与 Combobox 相近的 get()/set()/set_values() 接口。
    """

    def __init__(self, master, variable, values, width=10, command=None, **kw):
        self._values = [str(v) for v in values]
        self._command = command
        self._ready = False
        super().__init__(master, textvariable=variable, indicatoron=True,
                         relief="raised", width=width, anchor="w",
                         background=LIGHT_PANEL, foreground=LIGHT_TEXT,
                         activebackground="#EAF1FE", activeforeground=LIGHT_TEXT,
                         highlightthickness=0, borderwidth=1, **kw)
        self.var = variable
        self.menu = tk.Menu(self, tearoff=0, font=("", 12))
        self.configure(menu=self.menu)
        self._rebuild_menu()
        self.var.trace_add("write", self._on_var_write)
        self._ready = True

    def _rebuild_menu(self):
        self.menu.delete(0, "end")
        for v in self._values:
            self.menu.add_radiobutton(label=v, command=lambda value=v: self.var.set(value))
        if self._values and self.var.get() not in self._values:
            self.var.set(self._values[0])

    def set_values(self, values, keep=True):
        """更新可选项；keep=False 或当前值不在新列表时回退到第一项。"""
        self._values = [str(v) for v in values]
        self._rebuild_menu()
        if not keep and self._values:
            self.var.set(self._values[0])
        if self._ready and self._command:
            self._command()

    def _on_var_write(self, *_):
        if self._ready and self._command:
            self._command()

    def get(self):
        return self.var.get()

    def set(self, value):
        value = str(value)
        if value in self._values:
            self.var.set(value)


def _diff_label(value) -> str:
    """难度数字 → 容易/中等/较难/难；已是文字则原样返回。"""
    text = str(value)
    try:
        return DIFFICULTY_LABEL.get(int(text), text)
    except (TypeError, ValueError):
        return text


# 全局固定浅色配色（不做昼夜模式，始终使用白天的默认色）
LIGHT_BG = "#F5F5F7"
LIGHT_PANEL = "#FFFFFF"
LIGHT_TEXT = "#1D1D1F"


def use_light_appearance(win) -> None:
    """把窗口固定为浅色外观，避免跟随 macOS 深色模式变黑。

    macOS 要求窗口先完成布局才能读写外观，因此先 update_idletasks，
    失败时再延迟重试几次。
    """
    def apply(attempt: int = 0) -> None:
        try:
            win.update_idletasks()
            win.tk.call("tk::unsupported::MacWindowStyle", "appearance",
                        win._w, "aqua")
        except Exception:
            if attempt < 4:
                try:
                    win.after(50, lambda: apply(attempt + 1))
                except Exception:
                    pass

    apply()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1360x860")
        self.root.minsize(1080, 700)
        self.root.configure(background=LIGHT_BG)
        use_light_appearance(self.root)          # 固定浅色，不跟随系统深色模式

        self.tax = store.load_taxonomy()
        self.current_volume: str = ""          # 选中的册 volume_id
        self.current_rows = []                 # 当前列表 [(kp, vol)]
        self.selected_id: str = ""             # 选中的知识点 id
        # AI 提示词模块独立状态（与知识点模块的选择互不干扰）
        self.ai_rows = []                      # 当前筛选结果 [(kp, vol)]
        self.ai_selected: set = set()          # 勾选的知识点 id
        self.ai_vars_ratio = {}                # 难度 1-4 占比 StringVar
        self.ai_var_n_custom = tk.StringVar(value="")  # 「自定义」题数
        self.ai_cmb_grade = None               # 年级下拉（学段联动用）
        self.ai_ent_n = None                   # 自定义题数输入框
        self.ai_anchor = ""                    # Shift 连选的起始行
        self.ai_sort_sel_desc = None           # 「选」列排序方向：None/True/False
        self.logo_img = None                   # 防止 PhotoImage 被回收

        self._build_style()
        self._build_menu()
        self._build_layout()
        self._refresh_tree()
        self.refresh_list()               # 未选册时列出全部册的知识点
        self._update_status()

    # ---------------- 样式 ----------------
    def _build_style(self):
        st = ttk.Style()
        if "aqua" in st.theme_names():
            st.theme_use("aqua")
        st.configure("Treeview", rowheight=25)
        # AI 选点列表：行高与字号、勾选框比例协调
        st.configure("AI.Treeview", rowheight=28, font=("", 12),
                     background=LIGHT_PANEL, fieldbackground=LIGHT_PANEL,
                     foreground=LIGHT_TEXT)
        st.configure("AI.Treeview.Heading", font=("", 11, "bold"))
        st.configure("TButton", padding=5)
        st.configure("Toolbar.TButton", padding=6)
        st.configure("Title.TLabel", font=("", 15, "bold"))
        st.configure("Sub.TLabel", foreground="#555555")
        st.configure("Head.TLabel", font=("", 11, "bold"))
        # 固定浅色配色：所有面板、列表、标签统一白天色
        st.configure("TFrame", background=LIGHT_BG)
        st.configure("TLabel", background=LIGHT_BG, foreground=LIGHT_TEXT)
        st.configure("TCheckbutton", background=LIGHT_BG, foreground=LIGHT_TEXT)
        st.configure("TRadiobutton", background=LIGHT_BG, foreground=LIGHT_TEXT)
        st.configure("TNotebook", background=LIGHT_BG)
        st.configure("Treeview", background=LIGHT_PANEL,
                     fieldbackground=LIGHT_PANEL, foreground=LIGHT_TEXT)
        st.configure("Treeview.Heading", background="#F2F2F4", foreground=LIGHT_TEXT)

    def _logo_image(self, target=52):
        """加载项目 logo 并按整数倍缩小；Tk 不支持 PNG 时返回 None。"""
        candidates = [
            os.path.join(store.PROJECT_ROOT, "assets", "logo.png"),
            os.path.join(store.PROJECT_ROOT, "黑底K12黄色勾选清单图标.png"),
        ]
        for path in candidates:
            if not os.path.exists(path):
                continue
            try:
                img = tk.PhotoImage(file=path)
                factor = max(1, min(img.width(), img.height()) // target)
                return img.subsample(factor)
            except (tk.TclError, ValueError):
                continue
        return None

    def _make_check_images(self, size=16):
        """像素画生成勾选框：选中=蓝底白勾，未选=白底灰框（尺寸与行高协调）。"""
        def blank(fill, border):
            img = tk.PhotoImage(width=size, height=size)
            img.put(fill, to=(0, 0, size, size))
            edge = max(1, round(size / 8))
            for i in range(size):
                img.put(border, to=(i, 0, i + 1, edge))
                img.put(border, to=(i, size - edge, i + 1, size))
                img.put(border, to=(0, i, edge, i + 1))
                img.put(border, to=(size - edge, i, size, i + 1))
            return img

        off = blank("#FFFFFF", "#8A8A8A")
        on = blank("#2F6FEB", "#2F6FEB")

        def segment(x0, y0, x1, y1, color):
            x0, y0, x1, y1 = round(x0), round(y0), round(x1), round(y1)
            steps = max(abs(x1 - x0), abs(y1 - y0))
            for s in range(steps + 1):
                x = round(x0 + (x1 - x0) * s / steps)
                y = round(y0 + (y1 - y0) * s / steps)
                on.put(color, to=(x - 1, y - 1, x + 1, y + 1))

        # 勾形按尺寸等比缩放：短边 → 转折点 → 长边
        u = size / 16.0
        segment(4.2 * u, 8.2 * u, 6.8 * u, 10.9 * u, "#FFFFFF")
        segment(6.8 * u, 10.9 * u, 11.8 * u, 5.2 * u, "#FFFFFF")
        return off, on

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
        m_tools.add_command(label="AI 提示词 · 当前知识点", command=self.goto_ai_single)
        m_tools.add_command(label="AI 提示词 · 当前列表", command=self.goto_ai_batch)
        m_tools.add_separator()
        m_local = tk.Menu(m_tools, tearoff=0)
        m_local.add_command(label="本地模板出题（所选知识点）", command=self.generate_questions)
        m_local.add_command(label="本地模板批量出题（当前列表）", command=self.generate_batch_questions)
        m_tools.add_cascade(label="本地模板引擎（旧）", menu=m_local)
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
        # 顶部标题（左侧展示项目 Logo）
        head = ttk.Frame(self.root, padding=(14, 10))
        head.pack(fill="x")
        self.logo_img = self._logo_image(48)
        if self.logo_img is not None:
            ttk.Label(head, image=self.logo_img).pack(side="left", padx=(0, 12))
        ttk.Label(head, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        self.lbl_stat = ttk.Label(head, text="", style="Sub.TLabel")
        self.lbl_stat.pack(side="left", padx=16)

        # 两大模块
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        self.nb = nb

        # ===== 模块一：知识点管理 =====
        tab_km = ttk.Frame(nb, padding=6)
        nb.add(tab_km, text="知识点管理")

        # 主体：左右分栏
        main = ttk.PanedWindow(tab_km, orient="horizontal")
        main.pack(fill="both", expand=True)

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
        self.var_diff = tk.StringVar(value="全部")
        cb = ClickCombo(flt, self.var_diff,
                        ["全部"] + [DIFFICULTY_LABEL[d] for d in (1, 2, 3, 4)],
                        width=7, command=self.refresh_list)
        cb.pack(side="left", padx=(4, 10))

        ttk.Label(flt, text="认知").pack(side="left")
        self.var_bloom = tk.StringVar(value="全部")
        cb2 = ClickCombo(flt, self.var_bloom, ["全部"] + self.tax["bloom"],
                         width=7, command=self.refresh_list)
        cb2.pack(side="left", padx=(4, 10))
        ttk.Button(flt, text="清除筛选", command=self.clear_filters).pack(side="left")

        # 列表：去掉引擎列，新增学科、年级列（均取自所属册）
        self.lbl_list = ttk.Label(right, text="知识点", style="Head.TLabel")
        self.lbl_list.grid(row=1, column=0, sticky="w")
        cols = ("id", "name", "subject", "grade", "domain", "difficulty", "bloom")
        self.list = ttk.Treeview(right, columns=cols, show="headings", selectmode="browse")
        heads = {"id": "ID", "name": "知识点", "subject": "学科", "grade": "年级",
                 "domain": "课标领域", "difficulty": "难度", "bloom": "认知层级"}
        widths = {"id": 190, "name": 280, "subject": 60, "grade": 80,
                  "domain": 150, "difficulty": 55, "bloom": 80}
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
                                                font=("", 12), background=LIGHT_PANEL,
                                                foreground=LIGHT_TEXT, insertbackground=LIGHT_TEXT)
        self.detail.grid(row=4, column=0, sticky="nsew", columnspan=2)
        self.detail.configure(state="disabled")
        main.add(right, weight=1)

        # 模块一底部工具栏
        bar = ttk.Frame(tab_km, padding=(4, 8))
        bar.pack(fill="x", side="bottom")
        for text, cmd in [
            ("新增知识点", self.add_dialog),
            ("编辑所选", self.edit_dialog),
            ("删除所选", self.delete_selected),
            ("撤销", self.undo),
            ("恢复原始配置", self.restore_original_config),
            ("运行校验", self.run_checks),
            ("导出 CSV", lambda: self.export("csv")),
            ("刷新", self.reload_all),
        ]:
            ttk.Button(bar, text=text, command=cmd, style="Toolbar.TButton").pack(side="left", padx=(0, 8))
        self.lbl_msg = ttk.Label(bar, text="", foreground="#0F6E56")
        self.lbl_msg.pack(side="right")

        # ===== 模块二：AI 提示词 =====
        tab_ai = ttk.Frame(nb, padding=6)
        nb.add(tab_ai, text="AI 提示词")
        self._build_ai_tab(tab_ai)
        self.ai_update_alloc()
        self.ai_refresh()

    # ---------------- AI 提示词模块 ----------------
    def _build_ai_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=3)
        parent.rowconfigure(6, weight=2)

        # 筛选栏：学段 / 科目 / 年级默认「全部」，学段选定后年级联动收窄
        flt = ttk.Frame(parent)
        flt.grid(row=0, column=0, sticky="ew", pady=(2, 8))
        ttk.Label(flt, text="学段").pack(side="left")
        self.ai_var_stage = tk.StringVar(value="全部")
        ClickCombo(flt, self.ai_var_stage, list(AI_STAGE), width=9,
                   command=self.ai_on_stage_change).pack(side="left", padx=(4, 10))
        ttk.Label(flt, text="科目").pack(side="left")
        self.ai_var_subject = tk.StringVar(value="全部")
        ClickCombo(flt, self.ai_var_subject, list(AI_SUBJECT), width=8,
                   command=self.ai_refresh).pack(side="left", padx=(4, 10))
        ttk.Label(flt, text="年级").pack(side="left")
        self.ai_var_grade = tk.StringVar(value="全部")
        self.ai_cmb_grade = ClickCombo(flt, self.ai_var_grade,
                                       ["全部"] + STAGE_GRADE_LABELS[""], width=11,
                                       command=self.ai_refresh)
        self.ai_cmb_grade.pack(side="left", padx=(4, 10))
        ttk.Label(flt, text="搜索").pack(side="left")
        self.ai_var_q = tk.StringVar()
        eq = ttk.Entry(flt, textvariable=self.ai_var_q, width=22)
        eq.pack(side="left", padx=(4, 6))
        eq.bind("<Return>", lambda _: self.ai_refresh())
        ttk.Button(flt, text="筛选", command=self.ai_refresh).pack(side="left", padx=(0, 6))
        ttk.Button(flt, text="重置", command=self.ai_reset_filters).pack(side="left")

        # 列表标题 + 选择操作
        lbar = ttk.Frame(parent)
        lbar.grid(row=1, column=0, sticky="ew")
        self.ai_lbl_list = ttk.Label(lbar, text="知识点", style="Head.TLabel")
        self.ai_lbl_list.pack(side="left")
        ttk.Button(lbar, text="全选当前", command=self.ai_select_all).pack(side="right", padx=(4, 0))
        ttk.Button(lbar, text="反选", command=self.ai_invert).pack(side="right", padx=(4, 0))
        ttk.Button(lbar, text="清空选择", command=self.ai_clear_sel).pack(side="right", padx=(4, 0))

        # 勾选列表：首列用自绘勾选框（Treeview 只有树列支持图片）
        self._chk_off, self._chk_on = self._make_check_images()
        cols = ("id", "name", "subject", "grade", "domain", "difficulty", "bloom")
        self.ai_list = ttk.Treeview(parent, columns=cols, show="tree headings",
                                    selectmode="none", takefocus=False,
                                    style="AI.Treeview")
        heads = {"id": "ID", "name": "知识点", "subject": "学科",
                 "grade": "年级", "domain": "课标领域", "difficulty": "难度",
                 "bloom": "认知层级"}
        widths = {"id": 165, "name": 235, "subject": 55, "grade": 72,
                  "domain": 130, "difficulty": 60, "bloom": 78}
        self.ai_list.heading("#0", text="选")
        self.ai_list.column("#0", width=48, minwidth=48, stretch=False, anchor="center")
        for c in cols:
            self.ai_list.heading(c, text=heads[c])
            self.ai_list.column(c, width=widths[c],
                                anchor="center" if c in ("subject", "difficulty", "bloom") else "w")
        # 已选行整行浅蓝，未选恢复白底
        self.ai_list.tag_configure("sel", background="#C9DEFF", foreground="#0B3D6B")
        self.ai_list.grid(row=2, column=0, sticky="nsew")
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.ai_list.yview)
        sb.grid(row=2, column=1, sticky="ns")
        self.ai_list.configure(yscrollcommand=sb.set)
        self.ai_list.bind("<Button-1>", self.ai_on_click)

        # 参数行一：题数（支持自定义）+ 难度预设
        p1 = ttk.Frame(parent)
        p1.grid(row=3, column=0, sticky="ew", pady=(8, 4))
        ttk.Label(p1, text="每知识点题数").pack(side="left")
        nf = ttk.Frame(p1)
        nf.pack(side="left", padx=(4, 14))
        self.ai_var_n = tk.StringVar(value="5")
        ClickCombo(nf, self.ai_var_n, QUESTION_COUNT_OPTIONS, width=9,
                   command=self.ai_on_n_change).pack(side="left")
        self.ai_ent_n = ttk.Entry(nf, textvariable=self.ai_var_n_custom, width=6)
        self.ai_ent_n.bind("<KeyRelease>", lambda _: self.ai_update_alloc())
        ttk.Label(p1, text="难度预设").pack(side="left")
        self.ai_var_preset = tk.StringVar(value="基础为主")
        ClickCombo(p1, self.ai_var_preset, list(AI_PRESETS), width=11,
                   command=self.ai_apply_preset).pack(side="left", padx=(4, 14))
        ttk.Label(p1, text="输出格式").pack(side="left")
        self.ai_var_format = tk.StringVar(value="JSON")
        ClickCombo(p1, self.ai_var_format, list(AI_FORMAT), width=7).pack(
            side="left", padx=(4, 14))
        self.ai_lbl_alloc = ttk.Label(p1, text="", foreground="#0F6E56")
        self.ai_lbl_alloc.pack(side="left")

        # 参数行二：四级占比（容易 / 中等 / 较难 / 难）
        p2 = ttk.Frame(parent)
        p2.grid(row=4, column=0, sticky="ew", pady=(0, 6))
        for d in (1, 2, 3, 4):
            ttk.Label(p2, text=f"{DIFFICULTY_LABEL[d]}%").pack(side="left")
            var = tk.StringVar(value=str(AI_PRESETS["基础为主"][d]))
            ent = ttk.Entry(p2, textvariable=var, width=5)
            ent.pack(side="left", padx=(3, 10))
            ent.bind("<KeyRelease>", lambda _: self.ai_update_alloc())
            self.ai_vars_ratio[d] = var
        ttk.Button(p2, text="刷新分配", command=self.ai_update_alloc).pack(side="left", padx=(0, 10))
        self.ai_lbl_ratio_sum = ttk.Label(p2, text="")
        self.ai_lbl_ratio_sum.pack(side="left")

        # 操作栏
        ops = ttk.Frame(parent)
        ops.grid(row=5, column=0, sticky="ew", pady=(2, 6))
        ttk.Button(ops, text="生成提示词", command=self.ai_generate,
                   style="Toolbar.TButton").pack(side="left", padx=(0, 8))
        ttk.Button(ops, text="复制全部", command=self.ai_copy).pack(side="left", padx=(0, 8))
        ttk.Button(ops, text="导出 .txt", command=lambda: self.ai_export("txt")).pack(side="left", padx=(0, 8))
        ttk.Button(ops, text="导出 .md", command=lambda: self.ai_export("md")).pack(side="left", padx=(0, 8))
        self.ai_lbl_status = ttk.Label(ops, text="", foreground="#0F6E56")
        self.ai_lbl_status.pack(side="left", padx=12)

        # 提示词输出（可编辑微调）
        self.ai_text = scrolledtext.ScrolledText(parent, wrap="word", font=("", 12),
                                                 background=LIGHT_PANEL, foreground=LIGHT_TEXT,
                                                 insertbackground=LIGHT_TEXT, padx=10, pady=8)
        self.ai_text.grid(row=6, column=0, sticky="nsew", columnspan=2)

    # ---- AI 模块：筛选与勾选 ----
    def ai_on_stage_change(self):
        """学段变化 → 年级下拉只显示对应学段；失效年级回到「全部」。"""
        stage = AI_STAGE.get(self.ai_var_stage.get(), "")
        labels = STAGE_GRADE_LABELS.get(stage, STAGE_GRADE_LABELS[""])
        self.ai_cmb_grade.set_values(["全部"] + labels, keep=False)
        self.ai_refresh()

    def ai_refresh(self):
        rows = store.query(
            q=self.ai_var_q.get(),
            stage=AI_STAGE.get(self.ai_var_stage.get(), ""),
            subject=AI_SUBJECT.get(self.ai_var_subject.get(), ""),
            grade=AI_GRADE.get(self.ai_var_grade.get(), ""),
        )
        self.ai_rows = rows
        self.ai_anchor = ""
        self.ai_sort_sel_desc = None
        self.ai_list.heading("#0", text="选")
        self._ai_fill_rows()

    def _ai_fill_rows(self):
        """按当前 ai_rows 重建列表，并恢复每行的勾选图标与蓝色底色。"""
        self.ai_list.delete(*self.ai_list.get_children())
        for kp, v in self.ai_rows:
            selected = kp["id"] in self.ai_selected
            self.ai_list.insert(
                "", "end", iid=kp["id"],
                image=self._chk_on if selected else self._chk_off,
                values=(
                    kp["id"], kp["name"],
                    store.SUBJECT_NAMES.get(v.get("subject", ""), v.get("subject", "")),
                    store._grade_label(v["grade"]),
                    kp.get("domain", ""),
                    _diff_label(kp.get("difficulty", "")),
                    kp.get("bloom", ""),
                ), tags=("sel",) if selected else ())
        self._ai_count()

    def ai_reset_filters(self):
        self.ai_var_stage.set("全部")
        self.ai_var_subject.set("全部")
        self.ai_var_grade.set("全部")
        self.ai_cmb_grade.set_values(["全部"] + STAGE_GRADE_LABELS[""], keep=False)
        self.ai_var_q.set("")
        self.ai_refresh()

    def _ai_count(self):
        self.ai_lbl_list.configure(
            text=(f"知识点 · 已勾选 {len(self.ai_selected)} / {len(self.ai_rows)}"
                  "（点行勾选 · Shift 连选 · 点「选」表头排序）"))

    def _ai_apply_row_states(self, rows=None):
        """按 ai_selected 同步某批行的勾选图标与整行蓝色底色。"""
        for iid in (rows if rows is not None else self.ai_list.get_children()):
            selected = iid in self.ai_selected
            self.ai_list.item(
                iid, image=self._chk_on if selected else self._chk_off,
                tags=("sel",) if selected else ())

    def ai_on_click(self, event):
        region = self.ai_list.identify("region", event.x, event.y)
        # 点「选」列表头 → 按选中 / 未选中排序
        if region == "heading":
            if self.ai_list.identify_column(event.x) == "#0":
                self.ai_sort_by_selected()
                return "break"
            return
        if region not in ("cell", "tree"):
            return
        iid = self.ai_list.identify_row(event.y)
        if not iid:
            return
        children = list(self.ai_list.get_children())
        if iid not in children:
            return
        shift = bool(event.state & 0x0001)   # Shift 键
        if shift and self.ai_anchor in children:
            # Shift 连选：锚点行到当前行之间全部选中
            start, end = sorted((children.index(self.ai_anchor), children.index(iid)))
            span = children[start:end + 1]
            self.ai_selected.update(span)
            self._ai_apply_row_states(span)
        else:
            # 普通点击：整行切换选中 / 取消
            if iid in self.ai_selected:
                self.ai_selected.discard(iid)
            else:
                self.ai_selected.add(iid)
            self._ai_apply_row_states([iid])
            self.ai_anchor = iid
        self._ai_count()
        return "break"

    def _ai_repaint(self):
        self._ai_apply_row_states()
        self._ai_count()

    def ai_sort_by_selected(self):
        """点「选」表头：已选在前 ↔ 未选在前，来回切换。"""
        if self.ai_sort_sel_desc is None:
            self.ai_sort_sel_desc = True
        else:
            self.ai_sort_sel_desc = not self.ai_sort_sel_desc
        sel_first = self.ai_sort_sel_desc
        self.ai_rows.sort(
            key=lambda pair: 0 if (pair[0]["id"] in self.ai_selected) == sel_first else 1)
        self.ai_anchor = ""
        self._ai_fill_rows()
        self.ai_list.heading("#0", text="选 ↓" if sel_first else "选 ↑")

    def ai_select_all(self):
        self.ai_selected.update(kp["id"] for kp, _ in self.ai_rows)
        self._ai_repaint()

    def ai_clear_sel(self):
        self.ai_selected.clear()
        self._ai_repaint()

    def ai_invert(self):
        ids = {kp["id"] for kp, _ in self.ai_rows}
        self.ai_selected = ids - self.ai_selected
        self._ai_repaint()

    # ---- AI 模块：参数 ----
    def _ai_mix(self):
        try:
            mix = {d: max(0, int(self.ai_vars_ratio[d].get() or 0)) for d in (1, 2, 3, 4)}
        except (ValueError, KeyError):
            return None
        return mix if sum(mix.values()) > 0 else None

    def ai_on_n_change(self):
        """题数选「自定义」才显示输入框；其余档位直接隐藏。"""
        if self.ai_var_n.get() == "自定义":
            self.ai_ent_n.pack(side="left", padx=(4, 2))
        else:
            self.ai_ent_n.pack_forget()
        self.ai_update_alloc()

    def _ai_current_n(self) -> int:
        """当前有效题数；自定义为空或非法时回退 5。"""
        raw = self.ai_var_n_custom.get().strip() if self.ai_var_n.get() == "自定义" \
            else self.ai_var_n.get()
        try:
            n = int(raw)
            return n if n > 0 else 5
        except (ValueError, TypeError):
            return 5

    def ai_apply_preset(self):
        mix = AI_PRESETS.get(self.ai_var_preset.get())
        if mix:
            for d in (1, 2, 3, 4):
                self.ai_vars_ratio[d].set(str(mix[d]))
        self.ai_update_alloc()

    def ai_update_alloc(self):
        n = self._ai_current_n()
        mix = self._ai_mix()
        alloc = prompt_mod.allocate_difficulty(n, mix)
        self.ai_lbl_alloc.configure(
            text=f"按 {n} 题分配：" + " / ".join(
                f"{DIFFICULTY_LABEL[d]} {alloc[d]}题" for d in (1, 2, 3, 4)))
        total = sum(mix.values()) if mix else 0
        self.ai_lbl_ratio_sum.configure(
            text=f"占比合计 {total}%" + ("" if total == 100 else "（非 100% 将按比例归一）"))

    # ---- AI 模块：生成 / 复制 / 导出 ----
    def ai_generate(self):
        ids = [kp["id"] for kp, _ in self.ai_rows if kp["id"] in self.ai_selected]
        if not ids:
            messagebox.showinfo("提示", "请先勾选至少一个知识点（点击列表中的任意一行）")
            return
        n = self._ai_current_n()
        mix = self._ai_mix()
        fmt = AI_FORMAT.get(self.ai_var_format.get(), "json")
        try:
            if len(ids) == 1:
                text = store.build_point_prompt(ids[0], n, difficulty_mix=mix,
                                                output_format=fmt)
                scope = "单个知识点"
            else:
                text = store.build_batch_prompt(ids, n, difficulty_mix=mix,
                                                output_format=fmt)
                scope = f"{len(ids)} 个知识点"
        except store.StoreError as e:
            messagebox.showerror("生成失败", str(e))
            return
        self.ai_text.delete("1.0", "end")
        self.ai_text.insert("1.0", text)
        self.ai_lbl_status.configure(
            text=f"已生成 {len(text)} 字 · {scope} · 每点 {n} 题 · {self.ai_var_format.get()}")

    def ai_copy(self):
        text = self.ai_text.get("1.0", "end").strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        self.ai_lbl_status.configure(text="已复制到剪贴板 ✓")

    def ai_export(self, fmt: str):
        text = self.ai_text.get("1.0", "end").strip()
        if not text:
            return
        ext = "md" if fmt == "md" else "txt"
        path = filedialog.asksaveasfilename(
            parent=self.root, title=f"导出提示词 .{ext}", defaultextension=f".{ext}",
            filetypes=[(f"{ext.upper()} 文件", f"*.{ext}"), ("所有文件", "*.*")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.ai_lbl_status.configure(text=f"已导出 → {os.path.basename(path)}")

    # ---- 菜单快捷入口：切到 AI 模块并预选 ----
    def goto_ai_single(self):
        if not self.selected_id:
            messagebox.showinfo("提示", "请先在「知识点管理」选择一个知识点")
            return
        self.nb.select(1)
        self.ai_reset_filters()
        self.ai_selected = {self.selected_id}
        self._ai_repaint()

    def goto_ai_batch(self):
        ids = [kp["id"] for kp, _ in self.current_rows]
        if not ids:
            messagebox.showinfo("提示", "当前列表没有知识点")
            return
        self.nb.select(1)
        self.ai_reset_filters()
        self.ai_selected = set(ids)
        self._ai_repaint()

    # ---------------- 树 ----------------
    def _tree_open_state(self) -> dict:
        """记录当前展开状态，刷新后原样保留（首次启动为空 = 全部收起）。"""
        state = {}

        def walk(parent):
            for iid in self.tree.get_children(parent):
                state[iid] = bool(self.tree.item(iid, "open"))
                walk(iid)

        walk("")
        return state

    def _refresh_tree(self, keep: str = ""):
        opened = self._tree_open_state()
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
            n_vol = sum(len(x) for g in stages[stage].values() for x in g.values())
            n_pt = sum(len(v["knowledge_points"])
                       for g in stages[stage].values() for x in g.values() for v in x)
            self.tree.insert("", "end", sid, text=f"{sname}（{n_vol} 册 / {n_pt} 点）",
                             open=opened.get(sid, False))
            for subject in ("chinese", "math", "english"):
                if subject not in stages[stage]:
                    continue
                suname = self.tax["subject_names"].get(subject, subject)
                uid = f"subject::{stage}::{subject}"
                self.tree.insert(sid, "end", uid, text=suname, open=opened.get(uid, False))
                for grade in sorted(stages[stage][subject],
                                    key=lambda g: store.GRADE_ORDER.get(g, 9)):
                    glabel = store._grade_label(grade)
                    gid = f"grade::{stage}::{subject}::{grade}"
                    self.tree.insert(uid, "end", gid, text=glabel, open=opened.get(gid, False))
                    for v in stages[stage][subject][grade]:
                        vid = f"vol::{v['volume_id']}"
                        self.tree.insert(gid, "end", vid,
                                         text=f"{self.tax['term_names'].get(v['term'], v['term'])} · {len(v['knowledge_points'])} 点")
        if keep:
            try:
                # 只展开到当前选中的册，其余层级保持用户原来的展开/收起状态
                parent = self.tree.parent(keep)
                while parent:
                    self.tree.item(parent, open=True)
                    parent = self.tree.parent(parent)
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
            difficulty=DIFFICULTY_CODE.get(self.var_diff.get()),
            bloom=self.var_bloom.get() if self.var_bloom.get() != "全部" else "",
        )
        if self.current_volume:
            rows = [(kp, v) for kp, v in rows if v["volume_id"] == self.current_volume]
        self.current_rows = rows
        self._fill_knowledge_list(rows)
        vol = next((v for v in store.load_all_volumes()
                    if v["volume_id"] == self.current_volume), None)
        scope = vol["title"] if vol else "全部册"
        self.lbl_list.configure(text=f"知识点 · {scope}（{len(rows)} 条）")

    def _fill_knowledge_list(self, rows):
        """统一渲染模块一列表（难度显示文字；学科、年级取自所属册）。"""
        self.list.delete(*self.list.get_children())
        for kp, v in rows:
            self.list.insert("", "end", iid=kp["id"], values=(
                kp["id"], kp["name"],
                self.tax["subject_names"].get(v["subject"], v["subject"]),
                store._grade_label(v["grade"]),
                kp.get("domain", ""),
                _diff_label(kp.get("difficulty", "")), kp.get("bloom", ""),
            ))

    def sort_by(self, col: str):
        def key(pair):
            kp, v = pair
            if col == "subject":
                return self.tax["subject_names"].get(v["subject"], v["subject"])
            if col == "grade":
                return (store.GRADE_ORDER.get(v["grade"], 9), v["term"])
            if col == "difficulty":
                return int(kp.get("difficulty") or 0)
            return str(kp.get(col, ""))

        self.current_rows = sorted(self.current_rows, key=key)
        self._fill_knowledge_list(self.current_rows)

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
        """详情：首行为标题 + 元信息，其余字段统一「字段名: 值」单行紧凑展示。"""
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
            ("前置关系 prerequisites", "、".join(kp.get("prerequisites", []))),
            ("核心概念 concept", kp.get("concept")),
            ("易错点 common_mistakes", list(kp.get("common_mistakes", []))),
            ("建议题型 suggested_types", "、".join(kp.get("suggested_types", []))),
            ("年级范围 grade_range", "、".join(kp.get("grade_range", []))),
            ("权威来源 source", kp.get("source")),
        ]
        for label, value in fields:
            self.detail.insert("end", f"{label}: ", ("label",))
            if isinstance(value, list):
                if value:
                    self.detail.insert("end", f"{value[0]}\n")
                    for item in value[1:]:
                        self.detail.insert("end", f"    · {item}\n")
                else:
                    self.detail.insert("end", "—\n")
            else:
                self.detail.insert("end", f"{value or '—'}\n")
        self.detail.tag_configure("title", font=("", 14, "bold"))
        self.detail.tag_configure("label", font=("", 11, "bold"), foreground="#185FA5")
        self.detail.configure(state="disabled")

    def clear_filters(self):
        self.var_q.set("")
        self.var_diff.set("全部")
        self.var_bloom.set("全部")
        self.refresh_list()

    # ---------------- 编辑弹窗 ----------------
    def _editor(self, title: str, values: dict, on_save):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("760x760")
        win.transient(self.root)
        win.grab_set()
        use_light_appearance(win)

        canvas = tk.Canvas(win, background=LIGHT_PANEL, highlightthickness=0)
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
            "difficulty": "难度（容易/中等/较难/难）*", "bloom": "认知层级 *",
            "prerequisites": "前置关系（ID，逗号分隔）",
            "concept": "核心概念 *", "common_mistakes": "易错点（逗号分隔）",
            "suggested_types": "建议题型（逗号分隔）",
            "grade_range": "年级范围（逗号分隔）",
            "source": "权威来源 *",
        }
        r = 0
        for key in store.EDITABLE:
            ttk.Label(form, text=labels[key], style="Head.TLabel").grid(row=r, column=0, sticky="nw", pady=(8, 2))
            r += 1
            val = values.get(key, "")
            if isinstance(val, list):
                val = ", ".join(str(x) for x in val)
            if key == "difficulty":
                v = tk.StringVar(value=_diff_label(val))
                w = ClickCombo(form, v, [DIFFICULTY_LABEL[d] for d in (1, 2, 3, 4)],
                               width=60)
            elif key == "bloom":
                v = tk.StringVar(value=str(val))
                w = ClickCombo(form, v, self.tax["bloom"], width=60)
            elif key in ("objective", "concept"):
                w = tk.Text(form, width=68, height=5, wrap="word",
                            background=LIGHT_PANEL, foreground=LIGHT_TEXT,
                            insertbackground=LIGHT_TEXT, relief="flat",
                            borderwidth=0, highlightthickness=1,
                            highlightbackground="#C8C8CC", highlightcolor="#2F6FEB")
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
                if k == "difficulty":
                    # 界面显示「容易/中等/较难/难」，存回数据时换回 1-4
                    out[k] = str(DIFFICULTY_CODE.get(out[k], out[k]))
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

    def restore_original_config(self):
        """整库恢复为软件安装时的原始配置（63 册 / 1225 个知识点）。"""
        info = store.original_info()
        if not info:
            messagebox.showerror(
                "无法恢复",
                "未找到随软件附带的原始配置快照：\n"
                f"{store.ORIGINAL_ARCHIVE}\n\n"
                "请重新安装或从项目仓库补齐 desktop/original/ 后再试。")
            return
        cur = store.stats()
        if not messagebox.askyesno(
                "恢复原始配置",
                "将把整个知识点库恢复为软件安装时的原始配置：\n\n"
                f"    原始：{info.get('volumes', '?')} 册 · {info.get('points', '?')} 个知识点\n"
                f"    当前：{cur['volumes']} 册 · {cur['points']} 个知识点\n\n"
                "所有新增、编辑、删除都会被覆盖。\n"
                "恢复前会自动整库备份，仍可用「撤销」回到恢复前的状态。\n\n确定继续？",
                icon="warning", default="no", parent=self.root):
            return
        try:
            result = store.restore_original()
        except store.StoreError as e:
            messagebox.showerror("恢复失败", str(e), parent=self.root)
            return
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("恢复失败", f"写入数据时出错：{e}", parent=self.root)
            return

        self.current_volume = ""
        self.selected_id = ""
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.configure(state="disabled")
        self.reload_all()
        msg = f"已恢复原始配置：{result['volumes']} 册 / {result['points']} 个知识点"
        if result["removed"]:
            msg += f"，清理额外册 {len(result['removed'])} 个"
        self.toast(msg)

    # ---------------- 工具 ----------------
    def generate_prompt(self):
        if not self.selected_id:
            messagebox.showinfo("提示", "请先在列表中选择一个知识点")
            return
        # 用局部变量固定当前选中的知识点：PromptDialog 回调 builder(n, fmt)，
        # 这里必须把 topic_id 绑定进去，否则题数会被误当成 topic_id（"知识点不存在: 5"）。
        topic_id = self.selected_id
        found = store.find_point(topic_id)
        if not found:
            return
        kp, _ = found
        PromptDialog(self.root,
                     lambda n, fmt: store.build_point_prompt(topic_id, n, output_format=fmt),
                     title=f"出题提示词 · {kp['name']}", default_n=5)

    def generate_batch_prompt(self):
        rows = self.current_rows
        if not rows:
            messagebox.showinfo("提示", "当前列表没有知识点，请先选择册或设置筛选")
            return
        ids = [kp["id"] for kp, _ in rows]
        PromptDialog(self.root,
                     lambda n, fmt: store.build_batch_prompt(ids, n, output_format=fmt),
                     title=f"批量出题提示词 · {len(ids)} 个知识点", default_n=3)

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
        use_light_appearance(win)
        txt = scrolledtext.ScrolledText(win, wrap="none", font=("Menlo", 11),
                                        background=LIGHT_PANEL, foreground=LIGHT_TEXT)
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
        self.ai_refresh()                 # AI 模块同步最新数据（筛选与勾选保留）
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
        messagebox.showinfo("使用说明", """【模块一 · 知识点管理】
1. 左侧知识地图按 学段 → 科目 → 年级 → 册 组织，默认全部收起，
   点箭头逐层展开，点册查看该册知识点。
2. 顶部可按关键词、难度、认知层级筛选；点列标题排序。列表含学科、年级列。
3. 双击列表项或点「新增/编辑/删除」管理知识点，改动写入 volumes/ 下的 JSON，
   自动备份到 desktop/backups/ 并记入 changelog.jsonl；「撤销」恢复上次快照。
4. 「恢复原始配置」把整库还原为软件安装时的 63 册 / 1225 个知识点：
   恢复前自动整库备份，误操作可再点「撤销」回到恢复前状态。
5. 「运行校验」调用结构校验 + 字段校验 + 课标交叉校验三套脚本。

【模块二 · AI 提示词】（主推）
6. 筛选栏的学段 / 科目 / 年级默认均为「全部」；选了学段后年级只显示对应年级
   （如选小学只列 1—6 年级，不再显示 7/8/9 年级）。点一下下拉菜单展开，
   再点一下选项即完成选择。列表含学科、认知层级列。
7. 直接点任意一行即可勾选 / 取消（选中的知识点整行变蓝），按住 Shift 再点另一行
   可连选两行之间的全部数据；点「选」列表头可按已选 / 未选排序，再点切换方向。
   「全选当前」=全选本页结果，「清空选择」重来。
8. 「每知识点题数」可选 1/3/5/10/15/20/50/100，或选「自定义」填任意正整数。
   难度用「容易/中等/较难/难」四档：预设默认「基础为主」（50/30/15/5），
   也可选均衡/进阶/挑战或直接改四个百分比，实时显示按题数换算的各档题数。
   「输出格式」可选 JSON / TXT / Excel / CSV：JSON 便于入库，TXT 便于阅读，
   Excel 输出制表符表格（复制到 Excel 另存 .xlsx），CSV 可直接导入表格。
9. 点「生成提示词」：勾 1 个出单点提示词，勾多个出批量提示词。
   提示词含完整知识点档案、年级红线（禁止超出该知识点所属年级水平）、
   难度分布、题目来源知识点字段（topic_id/topic_name），以及所选输出格式的
   字段说明与示例。
10. 生成后可在文本框直接微调，「复制全部」粘贴到 DeepSeek / 元宝 / ChatGPT，
   或导出 .txt / .md。

【其它】
11. 工具菜单的「本地模板引擎（旧）」保留旧的内容感知直接出题，仅作对比。
12. 注意：volumes/ 是 data/*.py 生成的产物。「恢复原始配置」只还原运行数据；
    若要永久固化修改，请把改动同步回 knowledge-map/data/ 下的源文件，
    再运行 build_volumes.py。""")

    def about_dialog(self):
        s = store.stats()
        info = store.original_info() or {}
        original = (f"出厂配置：{info.get('volumes')} 册 / {info.get('points')} 知识点\n"
                    if info else "")
        messagebox.showinfo(
            "关于",
            f"{APP_TITLE}\nv{VERSION}\n\n"
            f"当前：{s['volumes']} 册 / {s['points']} 知识点\n"
            f"{original}"
            "内容感知型出题引擎 · Python 标准库 + Tcl/Tk")


class PromptDialog:
    """大模型出题提示词窗口：生成 → 预览 → 复制 / 导出。"""

    def __init__(self, parent: tk.Tk, builder, title: str, default_n: int = 5):
        self.builder = builder                  # fn(n, fmt) -> prompt 文本
        self.win = tk.Toplevel(parent)
        self.win.title(title)
        self.win.geometry("900x760")
        self.win.minsize(700, 500)
        self.win.transient(parent)
        use_light_appearance(self.win)

        bar = ttk.Frame(self.win, padding=(12, 10))
        bar.pack(fill="x")
        ttk.Label(bar, text="每题数").pack(side="left")
        self.var_n = tk.StringVar(value=str(default_n))
        ClickCombo(bar, self.var_n, QUESTION_COUNT_OPTIONS[1:], width=7,
                   command=self.refresh).pack(side="left", padx=(4, 12))
        ttk.Label(bar, text="输出格式").pack(side="left")
        self.var_format = tk.StringVar(value="JSON")
        ClickCombo(bar, self.var_format, list(AI_FORMAT), width=7,
                   command=self.refresh).pack(side="left", padx=(4, 12))
        ttk.Button(bar, text="重新生成", command=self.refresh).pack(side="left")
        self.lbl = ttk.Label(bar, text="", foreground="#0F6E56")
        self.lbl.pack(side="left", padx=12)

        ttk.Label(self.win, text="提示词（可直接编辑微调，复制后粘贴到大模型）",
                  style="Head.TLabel").pack(anchor="w", padx=14, pady=(2, 4))
        self.txt = scrolledtext.ScrolledText(self.win, wrap="word", font=("", 12),
                                             background=LIGHT_PANEL, foreground=LIGHT_TEXT,
                                             insertbackground=LIGHT_TEXT, padx=10, pady=8)
        self.txt.pack(fill="both", expand=True, padx=12)

        bottom = ttk.Frame(self.win, padding=(12, 10))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="复制全部", command=self.copy).pack(side="left", padx=(0, 8))
        ttk.Button(bottom, text="导出 .txt", command=lambda: self.export("txt")).pack(side="left", padx=(0, 8))
        ttk.Button(bottom, text="导出 .md", command=lambda: self.export("md")).pack(side="left", padx=(0, 8))
        ttk.Button(bottom, text="关闭", command=self.win.destroy).pack(side="right")
        self.refresh()

    def refresh(self):
        try:
            n = int(self.var_n.get())
            text = self.builder(n, AI_FORMAT.get(self.var_format.get(), "json"))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("生成失败", str(exc), parent=self.win)
            return
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", text)
        self.lbl.configure(text=f"已生成 · {len(text)} 字")

    def copy(self):
        text = self.txt.get("1.0", "end").strip()
        if not text:
            return
        self.win.clipboard_clear()
        self.win.clipboard_append(text)
        self.win.update()
        self.lbl.configure(text="已复制到剪贴板 ✓")

    def export(self, fmt: str):
        text = self.txt.get("1.0", "end").strip()
        if not text:
            return
        ext = "md" if fmt == "md" else "txt"
        path = filedialog.asksaveasfilename(
            parent=self.win, title=f"导出提示词 .{ext}", defaultextension=f".{ext}",
            filetypes=[(f"{ext.upper()} 文件", f"*.{ext}"), ("所有文件", "*.*")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.lbl.configure(text=f"已导出 → {os.path.basename(path)}")


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
        use_light_appearance(self.win)

        # 参数栏
        bar = ttk.Frame(self.win, padding=(12, 10))
        bar.pack(fill="x")
        ttk.Label(bar, text="每点题数").pack(side="left")
        self.var_n = tk.StringVar(value="12" if len(self.topics) == 1 else "6")
        ClickCombo(bar, self.var_n, ["4", "6", "8", "12", "20", "30"],
                   width=7).pack(side="left", padx=(4, 14))
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
        use_light_appearance(win)
        txt = scrolledtext.ScrolledText(win, wrap="word", font=("", 13), padx=14, pady=12,
                                        background=LIGHT_PANEL, foreground=LIGHT_TEXT)
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
