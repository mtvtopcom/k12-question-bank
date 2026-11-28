#!/usr/bin/env python3
"""K12 题库工厂桌面管理台（macOS / tkinter）。

双击 .app 启动，或 python3 desktop/src/app.py。
仅依赖 Python 标准库 + 系统 Tcl/Tk。
"""
from __future__ import annotations

import copy
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import ttk, messagebox, filedialog, scrolledtext, simpledialog

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402
import prompt as prompt_mod  # noqa: E402
import api_bank  # noqa: E402
import bank_import  # noqa: E402
import api_client  # noqa: E402
import api_config  # noqa: E402
import api_task  # noqa: E402

APP_TITLE = "K12 题库工厂"
VERSION = "1.13.1"

# 难度 1-4 的文字描述（界面统一显示，不再写「难度1」）
DIFFICULTY_LABEL = {1: "容易", 2: "中等", 3: "较难", 4: "难"}
DIFFICULTY_CODE = {v: k for k, v in DIFFICULTY_LABEL.items()}

# 模块二（选择知识点生成内容）的筛选值映射（界面显示 → 数据层代码）
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
        # 单选项必须绑定到同一个变量：否则 Tk 会给所有下拉共用全局变量
        # selectedButton，导致勾选停在上一次点过的项上、与实际筛选值不一致。
        for v in self._values:
            self.menu.add_radiobutton(label=v, variable=self.var, value=v)
        if self._values and self.var.get() not in self._values:
            self.var.set(self._values[0])

    def set_values(self, values, keep=True):
        """更新可选项；keep=False 或当前值不在新列表时回退到第一项。"""
        self._values = [str(v) for v in values]
        before = self.var.get()
        self._rebuild_menu()
        if not keep and self._values:
            self.var.set(self._values[0])
        # 值变了的话 trace 已经回调过 command，这里只在值没变时补一次
        if self._ready and self._command and self.var.get() == before:
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


def _short_name(name: str, limit: int = 16) -> str:
    """任务名太长时截断，给列表的来源列用。"""
    text = str(name or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


class HelpDot(tk.Label):
    """小问号：鼠标停留给一句白话，点一下弹窗讲清楚这项是干什么的。"""

    def __init__(self, master, title: str, brief: str, detail: str, **kw):
        super().__init__(master, text="?", width=2, anchor="center",
                         font=("", 11, "bold"), foreground="#2F6FEB",
                         background=LIGHT_BG, cursor="pointinghand", **kw)
        self._title, self._brief, self._detail = title, brief, detail
        self._tip = None
        self._timer = None
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    # ---- 悬停 ----
    def _on_enter(self, _evt=None):
        self.configure(foreground="#0B4F8A")
        self._timer = self.after(350, self._show_tip)

    def _on_leave(self, _evt=None):
        self.configure(foreground="#2F6FEB")
        if self._timer is not None:
            try:
                self.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None
        self._hide_tip()

    def _show_tip(self):
        self._timer = None
        if self._tip is not None or not self.winfo_ismapped():
            return
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        tip.configure(background="#2B2B30")
        tk.Label(tip, text=self._brief, justify="left", background="#2B2B30",
                 foreground="#FFFFFF", font=("", 11), wraplength=340,
                 padx=10, pady=7).pack()
        tip.wm_geometry(f"+{self.winfo_rootx() + 18}+{self.winfo_rooty() + 20}")
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        self._tip = tip

    def _hide_tip(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None

    # ---- 点击 ----
    def _on_click(self, _evt=None):
        self._hide_tip()
        messagebox.showinfo(self._title, self._detail,
                            parent=self.winfo_toplevel())


# 全局固定浅色配色（不做昼夜模式，始终使用白天的默认色）
# ECECEC 与 macOS 浅色窗口背景（windowBackgroundColor）一致：ttk 控件的底色由
# 系统绘制、改不了，只有取同一个值才不会在窗口里看到一块块色差。
LIGHT_BG = "#ECECEC"
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


def force_light_theme() -> None:
    """把整个 App 固定为浅色（Aqua）外观，彻底不跟随 macOS 深色模式。

    Tk 自带的 tk::unsupported::MacWindowStyle 只能逐窗口设置外观，系统原生
    的提示框、下拉菜单、标题栏仍会跟着系统变黑。这里直接调 AppKit 把
    NSApp 的 appearance 设成 Aqua，一次覆盖全部原生绘制。
    """
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import ctypes.util

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "libobjc")
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        proto0 = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        proto1 = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_void_p)
        send0 = proto0(("objc_msgSend", objc))
        send1 = proto1(("objc_msgSend", objc))

        def cls(name: str):
            return objc.objc_getClass(name.encode())

        def sel(name: str):
            return objc.sel_registerName(name.encode())

        app = send0(cls("NSApplication"), sel("sharedApplication"))
        if not app:
            return
        idea = send1(cls("NSString"), sel("stringWithUTF8String:"),
                     b"NSAppearanceNameAqua")
        aqua = send1(cls("NSAppearance"), sel("appearanceNamed:"), idea)
        if aqua:
            send1(app, sel("setAppearance:"), aqua)
    except Exception:
        pass


def ask_yes_no_cancel(parent, title: str, message: str):
    """三选一确认弹框：「是」/「否」/「取消」。

    返回 "yes" / "no"；点「取消」、按 Esc 或直接关窗口返回 None（什么都不做）。
    """
    result = {"value": None}

    win = tk.Toplevel(parent)
    win.title(title)
    win.resizable(False, False)
    win.transient(parent)
    use_light_appearance(win)

    body = ttk.Frame(win, padding=(20, 16))
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=message, justify="left",
              wraplength=340).pack(anchor="w")

    bar = ttk.Frame(win, padding=(20, 4, 20, 14))
    bar.pack(fill="x")
    ttk.Button(bar, text="取消", command=lambda: _close_dialog(win, result, None)).pack(
        side="right", padx=(8, 0))
    ttk.Button(bar, text="否", command=lambda: _close_dialog(win, result, "no")).pack(
        side="right", padx=(8, 0))
    ttk.Button(bar, text="是", command=lambda: _close_dialog(win, result, "yes")).pack(
        side="right", padx=(8, 0))

    win.protocol("WM_DELETE_WINDOW", lambda: _close_dialog(win, result, None))
    win.bind("<Escape>", lambda _e: _close_dialog(win, result, None))

    win.update_idletasks()
    win.grab_set()
    # 居中到父窗口
    try:
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")
    except Exception:
        pass
    win.wait_window()
    return result["value"]


def _close_dialog(win, result, value):
    result["value"] = value
    win.grab_release()
    win.destroy()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1360x860")
        self.root.minsize(1080, 700)
        self.root.configure(background=LIGHT_BG)
        force_light_theme()                      # 整个 App 固定白天外观
        use_light_appearance(self.root)          # 固定浅色，不跟随系统深色模式

        self.tax = store.load_taxonomy()
        self.current_volume: str = ""          # 选中的册 volume_id
        self.current_rows = []                 # 当前列表 [(kp, vol)]
        self.selected_id: str = ""             # 选中的知识点 id
        # 模块二状态：勾选、题数、难度也供模块三（大模型 API 生成）复用
        self.ai_rows = []                      # 当前筛选结果 [(kp, vol)]
        self.ai_selected: set = set()          # 勾选的知识点 id
        self.ai_vars_ratio = {}                # 难度 1-4 占比 StringVar
        self.ai_var_n_custom = tk.StringVar(value="")  # 「自定义」题数
        self.ai_cmb_grade = None               # 年级下拉（学段联动用）
        self.ai_ent_n = None                   # 自定义题数输入框
        self.ai_anchor = ""                    # Shift 连选的起始行
        self.ai_sort_sel_desc = None           # 「选」列排序方向：None/True/False
        self.logo_img = None                   # 防止 PhotoImage 被回收

        # 模块三状态：只保留接口配置与任务进度，勾选/题量/难度复用模块二
        self.api_cfg = api_config.load()       # 接口配置（含 API Key，仅本机）
        self.api_runner = None                 # 正在运行的任务线程
        self.api_task = None                   # 当前任务 dict
        self.api_events = queue.Queue()        # 后台线程 → 界面线程的事件队列
        self.api_task_map = {}                 # 任务下拉：显示文字 → task_id
        self._api_task_loading = False
        self._api_buttons = {}
        self._api_probe_pending = False
        self._api_pause_pending = False        # 已点暂停/停止，等线程收尾
        self._closing = False
        self._api_active_provider = None
        self._api_initializing = False
        self._api_poll_id = None
        self._api_hint_id = None

        # Tk 回调里的异常默认只打到 stdout（打包后看不到），
        # 统一收集到 crash.log，避免界面无声卡住还查不到原因
        self.root.report_callback_exception = self._on_tk_error

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
        st.configure("Hint.TLabel", foreground="#6B6B70", font=("", 10))
        st.configure("SourceSub.TLabel", foreground="#3F4A5A", font=("", 11))
        st.configure("Status.TLabel", foreground="#2F3B4A", font=("", 11))
        st.configure("TLabelframe", background=LIGHT_BG, bordercolor="#D6D6DA",
                     relief="solid", borderwidth=1)
        st.configure("TLabelframe.Label", background=LIGHT_BG, foreground="#4A4A4F",
                     font=("", 11, "bold"))
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
        m_file.add_command(label="导出题库 (Excel)", command=lambda: self.bank_export("xlsx"))
        m_file.add_command(label="导出题库 (CSV)", command=lambda: self.bank_export("csv"))
        m_file.add_separator()
        m_file.add_command(label="打开数据目录", command=self.open_data_dir)
        m_file.add_command(label="打开 API 数据目录", command=lambda: subprocess_open(api_config.data_dir()))
        m_file.add_command(label="打开任务 JSON 目录",
                           command=lambda: subprocess_open(api_config.exports_dir()))
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
        m_tools.add_command(label="大模型 API 生成", command=lambda: self.nb.select(2))
        m_tools.add_command(label="API 任务题库", command=lambda: self.nb.select(3))
        m_tools.add_command(label="全部题库", command=lambda: self.nb.select(4))
        m_tools.add_separator()
        m_tools.add_command(label="生成内容 · 当前知识点", command=self.goto_ai_single)
        m_tools.add_command(label="生成内容 · 当前列表", command=self.goto_ai_batch)
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

        # 五个模块（题库拆成两套，各自一级标签页）
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

        # ===== 模块二：选择知识点生成内容 =====
        tab_ai = ttk.Frame(nb, padding=6)
        nb.add(tab_ai, text="选择知识点生成内容")
        self.tab_ai = tab_ai
        self._build_ai_tab(tab_ai)
        self.ai_update_alloc()
        self.ai_refresh()

        # ===== 模块三：大模型 API 生成 =====
        tab_api = ttk.Frame(nb, padding=6)
        nb.add(tab_api, text="大模型API生成")
        self.tab_api = tab_api
        self._build_api_tab(tab_api)

        # ===== 模块四：API 任务题库 =====
        tab_bank_api = ttk.Frame(nb, padding=6)
        nb.add(tab_bank_api, text="API 任务题库")
        self.tab_bank_api = tab_bank_api
        self.bank_api_page = BankPage(self, tab_bank_api, api_bank.BANK_API)

        # ===== 模块五：全部题库 =====
        tab_bank_all = ttk.Frame(nb, padding=6)
        nb.add(tab_bank_all, text="全部题库")
        self.tab_bank_all = tab_bank_all
        self.bank_all_page = BankPage(self, tab_bank_all, api_bank.BANK_ALL)

        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._api_poll_id = self.root.after(200, self._poll_api_events)
        self._api_hint_id = self.root.after(600, self._api_restore_hint)

    # ---------------- 模块二：选择知识点生成内容 ----------------
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
        ttk.Button(ops, text="根据选择生成大模型提示词", command=self.ai_generate,
                   style="Toolbar.TButton").pack(side="left", padx=(0, 8))
        ttk.Button(ops, text="复制全部", command=self.ai_copy).pack(side="left", padx=(0, 8))
        ttk.Button(ops, text="导出 .txt", command=lambda: self.ai_export("txt")).pack(side="left", padx=(0, 8))
        ttk.Button(ops, text="导出 .md", command=lambda: self.ai_export("md")).pack(side="left", padx=(0, 8))
        ttk.Button(ops, text="本地API生成", command=self.api_from_prompt,
                   style="Toolbar.TButton").pack(side="left", padx=(0, 8))
        self.ai_lbl_status = ttk.Label(ops, text="", foreground="#0F6E56")
        self.ai_lbl_status.pack(side="left", padx=12)

        # 提示词输出（可编辑微调）
        self.ai_text = scrolledtext.ScrolledText(parent, wrap="word", font=("", 12),
                                                 background=LIGHT_PANEL, foreground=LIGHT_TEXT,
                                                 insertbackground=LIGHT_TEXT, padx=10, pady=8)
        self.ai_text.grid(row=6, column=0, sticky="nsew", columnspan=2)

        # 题数与难度和 API 页共用一份（存在 config.json 的 generation 节）
        self._ai_load_generation()
        self.ai_on_n_change()

    def _ai_load_generation(self):
        """恢复上次的题数与难度配比（API 页生成时用的就是这一份）。"""
        gen = self.api_cfg.get("generation") or {}
        saved_n = str(gen.get("n") or "5")
        custom = str(gen.get("n_custom") or "").strip()
        if saved_n not in QUESTION_COUNT_OPTIONS:
            if saved_n.isdigit():
                custom = custom or saved_n
            saved_n = "自定义"
        if custom:
            self.ai_var_n_custom.set(custom)
        self.ai_var_n.set(saved_n)

        preset = str(gen.get("preset") or "基础为主")
        if preset not in AI_PRESETS:
            preset = "基础为主"
        self.ai_var_preset.set(preset)
        ratios = gen.get("ratios") or {}
        base = AI_PRESETS[preset]
        for d in (1, 2, 3, 4):
            self.ai_vars_ratio[d].set(str(ratios.get(str(d), base[d])))

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
        # 结果集变了：勾选只保留仍在结果里的。
        # 早先这里是纯累积，切到「语文」还会带着上一次「数学」的 144 个，
        # 标题就成了「已勾选 300 / 156」，API 页也会按 300 个去生成。
        visible = {kp["id"] for kp, _ in rows}
        dropped = len(self.ai_selected - visible)
        if dropped:
            self.ai_selected &= visible
            if hasattr(self, "ai_lbl_status"):
                self.ai_lbl_status.configure(
                    text=f"筛选结果变了，原先勾选的 {dropped} 个不在当前列表里，已自动取消",
                    foreground="#8A5B00")
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
        # 分子只数当前列表里的：万一某处漏了收窄，也不会出现「300 / 156」
        cur = sum(1 for kp, _ in self.ai_rows if kp["id"] in self.ai_selected)
        self.ai_lbl_list.configure(
            text=(f"知识点 · 已勾选 {cur} / {len(self.ai_rows)}"
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
            text=f"已生成 {len(text)} 字 · {scope} · 每点 {n} 题 · {self.ai_var_format.get()}",
            foreground="#0F6E56")

    def ai_copy(self):
        text = self.ai_text.get("1.0", "end").strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update()
        self.ai_lbl_status.configure(text="已复制到剪贴板 ✓", foreground="#0F6E56")

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
        self.ai_lbl_status.configure(text=f"已导出 → {os.path.basename(path)}",
                                     foreground="#0F6E56")

    # ---- 菜单快捷入口：切到 AI 模块并预选 ----
    def goto_ai_single(self):
        if not self.selected_id:
            messagebox.showinfo("提示", "请先在「知识点管理」选择一个知识点")
            return
        self.nb.select(1)
        self.ai_reset_filters()
        self.ai_selected = {self.selected_id}
        self._ai_repaint()
        self.ai_lbl_status.configure(text="")          # 清掉重置筛选时的旧提示

    def goto_ai_batch(self):
        ids = [kp["id"] for kp, _ in self.current_rows]
        if not ids:
            messagebox.showinfo("提示", "当前列表没有知识点")
            return
        self.nb.select(1)
        self.ai_reset_filters()
        self.ai_selected = set(ids)
        self._ai_repaint()
        self.ai_lbl_status.configure(text="")          # 清掉重置筛选时的旧提示

    # ================= 模块三：大模型 API 生成 =================
    def api_goto_prompt(self):
        """API 页的「去选择知识点」：切回模块二。"""
        self.nb.select(self.tab_ai)

    def api_from_prompt(self):
        """模块二底部的「本地API生成」：带着当前勾选与题量跳到 API 页。"""
        self.nb.select(self.tab_api)
        self._api_refresh_source()
        self._api_set_buttons(
            running=self.api_runner is not None and self.api_runner.is_alive())
        if not self.ai_selected:
            self.ai_lbl_status.configure(text="先勾选知识点，再点「本地API生成」", foreground="#B3261E")
        else:
            self.ai_lbl_status.configure(
                text=(f"已带入 {len(self.ai_selected)} 个知识点 · 每点 "
                      f"{self._ai_current_n()} 题，到「大模型API生成」配好模型即可开始"),
                foreground="#0F6E56")

    def _api_selected_pairs(self):
        """按册顺序取出模块二勾选的知识点（勾选已随筛选收窄，与列表一致）。"""
        ids = self.ai_selected
        if not ids:
            return []
        pairs = []
        for vol in store.load_all_volumes():
            for kp in vol.get("knowledge_points") or []:
                if kp.get("id") in ids:
                    pairs.append((kp, vol))
        return pairs

    def _api_source_detail(self, pairs, n):
        """把已选知识点按「年级 + 学科」汇总成一行明细，方便核对生成来源。"""
        if not pairs:
            return ""
        stage_order = {"kg": 0, "primary": 1, "junior": 2}
        subject_order = {"chinese": 0, "math": 1, "english": 2}
        buckets = {}
        for _kp, vol in pairs:
            key = (vol.get("stage") or "", vol.get("grade") or "",
                   vol.get("subject") or "")
            buckets[key] = buckets.get(key, 0) + 1
        items = sorted(buckets.items(),
                       key=lambda kv: (stage_order.get(kv[0][0], 9),
                                       store.GRADE_ORDER.get(kv[0][1], 9),
                                       subject_order.get(kv[0][2], 9),
                                       kv[0][0], kv[0][1], kv[0][2]))
        parts = [f"{store._grade_label(grade)}{store.SUBJECT_NAMES.get(subject, subject)} "
                 f"{cnt} 点 / {cnt * n} 题"
                 for (_stage, grade, subject), cnt in items]
        return "明细：" + "　·　".join(parts)

    def _api_refresh_source(self):
        """刷新 API 页顶部的「生成内容」摘要。"""
        if not hasattr(self, "api_lbl_source"):
            return
        pairs = self._api_selected_pairs()
        n = self._ai_current_n()
        if pairs:
            self.api_lbl_source.configure(
                text=f"已选 {len(pairs)} 个知识点 · 每点 {n} 题",
                foreground="#0B4F8A")
            self.api_lbl_source_total.configure(
                text=f"　预计 {len(pairs) * n} 题", foreground="#C0392B")
        else:
            self.api_lbl_source.configure(
                text="还没有勾选知识点 —— 请到「选择知识点生成内容」页选择要生成的内容",
                foreground="#B3261E")
            self.api_lbl_source_total.configure(text="")
        if hasattr(self, "api_lbl_breakdown"):
            self.api_lbl_breakdown.configure(
                text=self._api_source_detail(pairs, n))
        mix = self._ai_mix() or dict(AI_PRESETS["基础为主"])
        alloc = prompt_mod.allocate_difficulty(n, mix)
        self.api_lbl_source2.configure(
            text=("难度配比 " + " / ".join(f"{DIFFICULTY_LABEL[d]} {mix.get(d, 0)}%"
                                          for d in (1, 2, 3, 4))
                  + "　每点实际分配 " + " / ".join(f"{DIFFICULTY_LABEL[d]} {alloc[d]} 题"
                                                  for d in (1, 2, 3, 4))))

    def _build_api_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(5, weight=1)

        cfg = ttk.LabelFrame(parent, text="大模型接口（Key 只保存在本机，不会上传）",
                             padding=(10, 6))
        cfg.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        cfg.columnconfigure(0, weight=1)

        self.api_var_provider = tk.StringVar(
            value=api_config.PROVIDER_LABELS.get(self.api_cfg.get("provider", "openai"),
                                                 "OpenAI 兼容"))
        top = ttk.Frame(cfg)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="协议").pack(side="left")
        ClickCombo(top, self.api_var_provider,
                   list(api_config.PROVIDER_LABELS.values()), width=15,
                   command=self.api_on_provider_change).pack(
            side="left", padx=(4, 18))
        ttk.Label(top, text="预设").pack(side="left")
        self.api_var_preset = tk.StringVar(value="自定义")
        self.api_cmb_preset = ClickCombo(top, self.api_var_preset, ["自定义"],
                                         width=20, command=self.api_on_preset_change)
        self.api_cmb_preset.pack(side="left", padx=(4, 18))
        ttk.Label(top, text="模型").pack(side="left")
        self.api_var_model = tk.StringVar()
        ttk.Entry(top, textvariable=self.api_var_model, width=28).pack(
            side="left", padx=(4, 0))

        base_row = ttk.Frame(cfg)
        base_row.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(base_row, text="Base URL").pack(side="left")
        self.api_var_base = tk.StringVar()
        ttk.Entry(base_row, textvariable=self.api_var_base).pack(
            side="left", fill="x", expand=True, padx=(4, 0))

        row = ttk.Frame(cfg)
        row.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(row, text="API Key").pack(side="left")
        self.api_var_key = tk.StringVar()
        self.api_ent_key = ttk.Entry(row, textvariable=self.api_var_key, width=34,
                                     show="•")
        self.api_ent_key.pack(side="left", padx=(4, 4))
        self.api_var_show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="显示", variable=self.api_var_show_key,
                        command=self.api_toggle_key).pack(side="left", padx=(0, 14))
        ttk.Button(row, text="测试连接", command=self.api_test).pack(
            side="left", padx=(0, 6))
        ttk.Button(row, text="保存配置", command=self.api_save_config_clicked).pack(
            side="left")
        self.api_lbl_probe = ttk.Label(row, text="", style="Hint.TLabel",
                                       wraplength=560, justify="left")
        self.api_lbl_probe.pack(side="left", padx=(10, 0))

        # 高级设置：平时收起，点开才出现 6 个技术参数，每项旁边有 ? 讲人话
        adv_head = ttk.Frame(cfg)
        adv_head.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.api_btn_adv = ttk.Button(adv_head, text="▶ 高级设置（一般不用改）",
                                      command=self.api_toggle_advanced)
        self.api_btn_adv.pack(side="left")
        self.api_lbl_adv = ttk.Label(adv_head, text="", style="Hint.TLabel")
        self.api_lbl_adv.pack(side="left", padx=(10, 0))

        self.api_adv = ttk.Frame(cfg)
        self.api_adv.grid(row=4, column=0, sticky="ew", pady=(8, 0))

        self.api_var_temp = tk.StringVar()
        self.api_var_max_tokens = tk.StringVar()
        self.api_var_timeout = tk.StringVar()
        self.api_var_interval = tk.StringVar()
        self.api_var_batch = tk.StringVar()
        self.api_var_concurrency = tk.StringVar()
        self.api_var_json = tk.BooleanVar(value=True)
        self.api_var_nothink = tk.BooleanVar(value=True)

        row1 = ttk.Frame(self.api_adv)
        row1.pack(fill="x")
        row2 = ttk.Frame(self.api_adv)
        row2.pack(fill="x", pady=(6, 0))
        row3 = ttk.Frame(self.api_adv)
        row3.pack(fill="x", pady=(6, 0))

        def adv_item(label, var, width, title, brief, detail, row):
            cell = ttk.Frame(row)
            cell.pack(side="left", padx=(0, 16))
            ttk.Label(cell, text=label).pack(side="left")
            ttk.Entry(cell, textvariable=var, width=width).pack(
                side="left", padx=(4, 2))
            HelpDot(cell, title, brief, detail).pack(side="left")

        adv_item(
            "灵活度", self.api_var_temp, 5, "灵活度（温度）",
            "回答的随机程度：越低越稳，越高越发散。0.6~1.0 最适合出题。",
            "灵活度（专业叫法：温度 / temperature）\n\n"
            "它决定模型回答有多「放得开」：\n"
            "· 0        几乎每次都给最标准的答案，题目容易重复\n"
            "· 0.7~1.0  有变化但不跑偏（推荐 0.8，默认值）\n"
            "· 1.5 以上 很发散，可能出怪题、跑题\n\n"
            "出题建议 0.6 ~ 1.0。范围 0 ~ 2，不知道就保持 0.8。", row1)
        adv_item(
            "输出上限", self.api_var_max_tokens, 6, "输出上限（max_tokens）",
            "模型一次最多能写多少内容。太小会被截断，默认 8192 够出 6 题。",
            "输出上限（专业叫法：max_tokens）\n\n"
            "模型一次回答最多能写多少内容。这里按 token 算，1 个汉字大约 1~2 个 token。\n"
            "· 太小（如 2000）：一批 10 题会被截断，拿不到完整结果\n"
            "· 推荐 8000 ~ 16000：够一次写 6~10 道带解析的题\n"
            "· 每次题数很多时（如 50 ~ 100 题）：建议 32000 以上，否则容易写一半断掉\n"
            "· 可填 64 ~ 1000000；填得大不会多花钱，费用只按实际写出来的内容算\n"
            "  （填得超过服务商上限时，接口会直接报错提示，不会悄悄截断）\n\n"
            "如果日志出现「输出被截断」，就把它调大，或把「每次题数」调小。", row1)
        adv_item(
            "等待上限(秒)", self.api_var_timeout, 5, "等待上限（超时）",
            "等多久还没回应就判定超时并自动重试。默认 90 秒。",
            "等待上限（超时时间，单位秒）\n\n"
            "一次请求最多等多久。超过这个时间没收到回应，就当作超时，自动重试。\n"
            "· 网络正常：默认 90 秒够用\n"
            "· 输出上限很大或模型很慢：可调到 180 ~ 300 秒\n\n"
            "说明：软件会按「输出上限」自动放宽超时（最多 240 秒），\n"
            "所以这里填小了也不会把长回答掐断，日志里会写明实际等了多久。", row1)
        adv_item(
            "请求间隔(秒)", self.api_var_interval, 5, "请求间隔",
            "两次请求之间歇多久，防止被服务商限流。默认 0.8 秒。",
            "请求间隔（单位秒）\n\n"
            "每生成完一批，等这么久再发下一次请求。\n"
            "· 默认 0.8 秒，基本不会被限流\n"
            "· 遇到「请求太频繁 / 429」：调到 2 ~ 3 秒\n"
            "· 想跑得更快：调到 0.2 ~ 0.3 秒，但更容易被限流", row2)
        adv_item(
            "每次题数", self.api_var_batch, 4, "每次题数（batch）",
            "一次请求让模型出几题。越小越稳，越大越快。默认 6。",
            "每次题数（一次请求生成几道题，可填 1 ~ 100）\n\n"
            "软件会把每个知识点的题数拆成几批去请求，填多少就每批请求多少：\n"
            "· 6 ~ 10 题：速度和稳定性比较平衡（推荐）\n"
            "· 1 ~ 3 题：最稳，不容易被截断或漏题，但请求次数多\n"
            "· 20 ~ 100 题：一次要更多，速度快，但一次写得长、更容易被截断；\n"
            "  被截断时软件会自动折半重试（日志里会写明），不用你手动改\n\n"
            "提示：题数填得大，「输出上限」也要跟着给够（如 100 题给 32000 以上），\n"
            "等待上限也会自动放宽，避免长回答被提前掐断。", row2)

        adv_item(
            "同时生成", self.api_var_concurrency, 4, "同时生成（并发数）",
            "同时向接口发几个请求。越大越快，单 Key 建议 4~8，默认 4。",
            "同时生成（并发数 / concurrency）\n\n"
            "一次同时处理几个知识点。这是提速最直接的一项：\n"
            "· 1 路：和以前一样，一次只发一个请求，最慢\n"
            "· 4 路（默认）：速度约 4 倍，绝大多数服务商都不会限流\n"
            "· 8 ~ 16 路：更快，但要看你的 Key 配额；被限流时软件会自动降速，\n"
            "  日志会出现「接口限流：整体降速」，冷却后自动恢复\n\n"
            "填 1 ~ 16。只有一个 Key 时建议 4 ~ 8；多个 Key 可以开到 16。\n"
            "第一次用可以先跑 20 题看看日志有没有限流，再往上加。", row2)

        json_cell = ttk.Frame(row2)
        json_cell.pack(side="left")
        ttk.Checkbutton(json_cell, text="固定格式（JSON）",
                        variable=self.api_var_json).pack(side="left")
        HelpDot(
            json_cell, "固定格式（JSON）",
            "让模型按固定格式回答。出题必须打开，否则软件读不懂。",
            "固定格式（JSON 模式）\n\n"
            "打开后，会要求模型用 JSON 这种「程序能读懂」的格式回答。\n"
            "· 打开（推荐）：软件能直接解析出题干、选项、答案、解析\n"
            "· 关闭：回答变成一段普通文字，软件多半读不懂，会报「解析失败」\n\n"
            "除非你在调试接口，否则请保持打开。"
        ).pack(side="left", padx=(2, 0))

        think_cell = ttk.Frame(row3)
        think_cell.pack(side="left")
        ttk.Checkbutton(think_cell, text="关闭深度思考（更快）",
                        variable=self.api_var_nothink,
                        command=self._api_refresh_think_hint).pack(side="left")
        HelpDot(
            think_cell, "关闭深度思考",
            "让模型跳过「先想一大段再作答」，直接出题。速度快 2~3 倍，花费也少。",
            "关闭深度思考（thinking）\n\n"
            "推理型模型（DeepSeek、豆包、GLM、Kimi 等）回答前会先在「心里」写\n"
            "一大段推理过程，再给出最终答案。\n\n"
            "对「解数学难题、写复杂代码」这类任务，这段思考很有用；\n"
            "但你的题目要求和输出格式已经写得很清楚了，模型不需要反复推敲，\n"
            "这段思考基本是白烧的 token。\n\n"
            "打开本项后：\n"
            "· 单道题消耗从 1500 token 降到 300 左右\n"
            "· 同样并发下，出题速度快 2 ~ 3 倍\n"
            "· 理科压轴题的正确率可能略有下降\n\n"
            "建议：\n"
            "· 语文、英语、常识、基础题 —— 直接打开\n"
            "· 数学、物理难题 —— 先跑 20 题，抽检正确率再决定\n"
            "· 服务商不支持这个参数时，软件会自动改回默认方式，\n"
            "  日志里会写「该模型不支持关闭深度思考」，不影响出题"
        ).pack(side="left", padx=(2, 0))
        self.api_lbl_think = tk.Label(think_cell, text="", font=("", 10), anchor="w")
        self.api_lbl_think.pack(side="left", padx=(8, 0))

        for _var in (self.api_var_temp, self.api_var_max_tokens,
                     self.api_var_timeout, self.api_var_interval,
                     self.api_var_batch, self.api_var_concurrency, self.api_var_json,
                     self.api_var_nothink):
            _var.trace_add("write", lambda *_: self._api_refresh_adv_summary())
        self.api_var_nothink.trace_add(
            "write", lambda *_: self._api_refresh_think_hint())
        self._api_refresh_think_hint()
        self.api_adv.grid_remove()

        # 生成来源：勾选、题数、难度都在模块二设置，这里只读不重复设置
        src = ttk.LabelFrame(parent, text="生成内容（来自「选择知识点生成内容」页）",
                             padding=(10, 6))
        src.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        src.columnconfigure(0, weight=1)
        band = tk.Frame(src, background="#EDF4FE", highlightthickness=1,
                        highlightbackground="#C4D9F6")
        band.grid(row=0, column=0, sticky="ew")
        self.api_lbl_source = tk.Label(
            band, text="", background="#EDF4FE", foreground="#0B4F8A",
            font=("", 15, "bold"), anchor="w", padx=12, pady=7)
        self.api_lbl_source.pack(side="left")
        self.api_lbl_source_total = tk.Label(
            band, text="", background="#EDF4FE", foreground="#C0392B",
            font=("", 16, "bold"), anchor="w", pady=7)
        self.api_lbl_source_total.pack(side="left")
        ttk.Button(src, text="去选择知识点", command=self.api_goto_prompt).grid(
            row=0, column=1, sticky="e", padx=(8, 0))
        # 明细：按「年级 + 学科」拆开显示点了多少条、预计多少题
        self.api_lbl_breakdown = ttk.Label(
            src, text="", style="SourceSub.TLabel", justify="left", anchor="w")
        self.api_lbl_breakdown.grid(row=1, column=0, columnspan=2, sticky="ew",
                                    pady=(5, 0))
        src.bind("<Configure>", lambda e: self.api_lbl_breakdown.configure(
            wraplength=max(360, e.width - 24)))
        self.api_lbl_source2 = ttk.Label(src, text="", style="SourceSub.TLabel")
        self.api_lbl_source2.grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))

        # 任务控制：生成按钮 + 任务操作（第一行），任务下拉与状态各占满宽（第二行）
        ops = ttk.Frame(parent)
        ops.grid(row=2, column=0, sticky="ew", pady=(2, 2))
        ops.columnconfigure(1, weight=1)
        act = ttk.Frame(ops)
        act.grid(row=0, column=0, sticky="w")
        for text, cmd, key in [
            ("开始生成", self.api_start, "start"),
            ("暂停", self.api_pause, "pause"),
            ("继续", self.api_resume, "resume"),
            ("停止", self.api_stop, "stop"),
        ]:
            btn = ttk.Button(act, text=text, command=cmd, style="Toolbar.TButton")
            btn.pack(side="left", padx=(0, 8))
            self._api_buttons[key] = btn

        task_ops = ttk.Frame(ops)
        task_ops.grid(row=0, column=2, sticky="e")
        ttk.Button(task_ops, text="重命名", command=self.api_rename_task).pack(
            side="left", padx=(0, 6))
        ttk.Button(task_ops, text="删除任务", command=self.api_delete_task).pack(
            side="left", padx=(0, 6))
        ttk.Button(task_ops, text="打开生成文件夹",
                   command=lambda: subprocess_open(api_config.exports_dir())).pack(
            side="left")

        task_row = ttk.Frame(parent)
        task_row.grid(row=3, column=0, sticky="ew", pady=(0, 2))
        task_row.columnconfigure(1, weight=1)
        ttk.Label(task_row, text="当前任务").grid(row=0, column=0, sticky="w")
        self.api_var_task = tk.StringVar(value="（暂无任务）")
        self.api_cmb_task = ClickCombo(task_row, self.api_var_task, ["（暂无任务）"],
                                       width=24, command=self.api_on_task_pick)
        self.api_cmb_task.grid(row=0, column=1, sticky="ew", padx=(6, 10))
        self.api_lbl_task = ttk.Label(task_row, text="", style="Status.TLabel",
                                      anchor="e")
        self.api_lbl_task.grid(row=0, column=2, sticky="e")

        # 进度
        prog = ttk.Frame(parent)
        prog.grid(row=4, column=0, sticky="ew", pady=(0, 0))
        self.api_pb = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.api_pb.pack(side="left", fill="x", expand=True)
        self.api_lbl_progress = ttk.Label(prog, text="0 / 0 题", width=20, anchor="e")
        self.api_lbl_progress.pack(side="left", padx=(10, 0))
        self.api_lbl_usage = ttk.Label(prog, text="", style="Hint.TLabel")
        self.api_lbl_usage.pack(side="left", padx=(12, 0))

        # 进度明细 + 运行日志
        pane = ttk.PanedWindow(parent, orient="horizontal")
        pane.grid(row=5, column=0, sticky="nsew", pady=(6, 0))
        left = ttk.Frame(pane)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        it_head = ttk.Frame(left)
        it_head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Label(it_head, text="进度明细", style="Head.TLabel").pack(side="left")
        ttk.Button(it_head, text="清空明细", command=self.api_clear_items).pack(
            side="right")
        ttk.Button(it_head, text="删除选中", command=self.api_delete_items).pack(
            side="right", padx=(0, 4))
        cols2 = ("topic", "progress", "status")
        self.api_item_tree = ttk.Treeview(left, columns=cols2, show="headings",
                                          selectmode="extended", height=6)
        for c, title, w in (("topic", "知识点", 260), ("progress", "进度", 80),
                            ("status", "状态", 80)):
            self.api_item_tree.heading(c, text=title)
            self.api_item_tree.column(c, width=w,
                                      anchor="w" if c == "topic" else "center")
        self.api_item_tree.tag_configure("done", foreground="#0F6E56")
        self.api_item_tree.tag_configure("error", foreground="#B3261E")
        self.api_item_tree.tag_configure("running", foreground="#185FA5")
        self.api_item_tree.tag_configure("paused", foreground="#8A6D00")
        self.api_item_tree.grid(row=1, column=0, sticky="nsew")
        isb = ttk.Scrollbar(left, orient="vertical", command=self.api_item_tree.yview)
        isb.grid(row=1, column=1, sticky="ns")
        self.api_item_tree.configure(yscrollcommand=isb.set)
        self.api_item_tree.bind("<Double-1>", self.api_jump_item)
        pane.add(left, weight=3)

        right = ttk.Frame(pane)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        log_head = ttk.Frame(right)
        log_head.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(log_head, text="运行日志", style="Head.TLabel").pack(side="left")
        ttk.Button(log_head, text="清空日志", command=self.api_clear_log).pack(
            side="right")
        self.api_log = scrolledtext.ScrolledText(right, height=6, wrap="none",
                                                 font=("Menlo", 10),
                                                 background=LIGHT_PANEL,
                                                 foreground=LIGHT_TEXT,
                                                 insertbackground=LIGHT_TEXT)
        self.api_log.grid(row=1, column=0, sticky="nsew")
        self.api_log.configure(state="disabled")
        pane.add(right, weight=2)

        self._api_initializing = True
        self.api_on_provider_change()
        self._api_initializing = False
        self._api_refresh_adv_summary()
        # 上次退出时线程被直接掐断，任务文件会停在 running；软件刚启动不可能在跑
        fixed = api_task.normalize_interrupted()
        api_task.normalize_names()
        self.api_refresh_task_choices()
        self._api_set_buttons(running=False)
        self._api_refresh_source()
        if fixed:
            self._api_log(f"上次退出时有 {fixed} 个任务停在「生成中」，已改为「已暂停」；"
                          "选中后点「继续」接着生成")

    # ---- API 模块：接口配置 ----
    def _api_stash_profile(self):
        """切换协议前把当前表单值暂存到内存配置，避免来回切换丢失输入。"""
        if self._api_initializing or not self._api_active_provider:
            return
        provider = self._api_active_provider
        if provider not in api_config.PROVIDER_LABELS:
            return
        def num(var, cast, default):
            try:
                return cast(var.get())
            except (TypeError, ValueError):
                return default
        base = self.api_var_base.get().strip()
        model = self.api_var_model.get().strip()
        preset = self.api_var_preset.get()
        values = api_config.preset_values(provider, preset)
        if values and (values[0] != base or values[1] != model):
            preset = "自定义"
        self.api_cfg.setdefault("profiles", {})[provider] = {
            "preset": preset,
            "base_url": base,
            "model": model,
            "api_key": self.api_var_key.get().strip(),
            "temperature": num(self.api_var_temp, float, 0.8),
            "max_tokens": num(self.api_var_max_tokens, int, 8192),
            "timeout": num(self.api_var_timeout, int, 90),
            "interval": num(self.api_var_interval, float, 0.8),
            "batch": num(self.api_var_batch, int, 6),
            "concurrency": num(self.api_var_concurrency, int, 4),
            "json_mode": bool(self.api_var_json.get()),
            "no_thinking": bool(self.api_var_nothink.get()),
        }

    def api_on_provider_change(self):
        if getattr(self, "_api_loading_profile", False):
            return
        provider = api_config.LABEL_TO_PROVIDER.get(self.api_var_provider.get(), "openai")
        if (not self._api_initializing and self._api_active_provider
                and provider != self._api_active_provider):
            self._api_stash_profile()
        prof = api_config.profile(self.api_cfg, provider)
        self._api_loading_profile = True
        try:
            names = api_config.preset_names(provider)
            self.api_cmb_preset.set_values(names or ["自定义"], keep=False)
            preset = str(prof.get("preset") or "")
            if preset not in names:
                preset = names[-1] if names else "自定义"
            values = api_config.preset_values(provider, preset)
            if values and (values[0] != str(prof.get("base_url") or "")
                           or values[1] != str(prof.get("model") or "")):
                preset = "自定义"
            self.api_var_preset.set(preset)
            self._api_fill_profile(prof)
        finally:
            self._api_loading_profile = False
        self._api_active_provider = provider

    def api_on_preset_change(self):
        if getattr(self, "_api_loading_profile", False):
            return
        provider = api_config.LABEL_TO_PROVIDER.get(self.api_var_provider.get(), "openai")
        values = api_config.preset_values(provider, self.api_var_preset.get())
        if not values:
            return
        base, model = values
        if base:
            self.api_var_base.set(base)
        if model:
            self.api_var_model.set(model)

    def _api_fill_profile(self, prof):
        self.api_var_base.set(str(prof.get("base_url") or ""))
        self.api_var_model.set(str(prof.get("model") or ""))
        self.api_var_key.set(str(prof.get("api_key") or ""))
        self.api_var_temp.set(str(prof.get("temperature", 0.8)))
        self.api_var_max_tokens.set(str(prof.get("max_tokens", 8192)))
        self.api_var_timeout.set(str(prof.get("timeout", 90)))
        self.api_var_interval.set(str(prof.get("interval", 0.8)))
        self.api_var_batch.set(str(prof.get("batch", 6)))
        self.api_var_concurrency.set(str(prof.get("concurrency", 4)))
        self.api_var_json.set(bool(prof.get("json_mode", True)))
        self.api_var_nothink.set(bool(prof.get("no_thinking", True)))
        self._api_refresh_think_hint()

    def api_toggle_key(self):
        self.api_ent_key.configure(show="" if self.api_var_show_key.get() else "•")

    def api_toggle_advanced(self):
        """高级设置默认收起，需要时再展开。"""
        open_ = not getattr(self, "_api_adv_open", False)
        self._api_adv_open = open_
        self.api_btn_adv.configure(
            text=("▼ 高级设置（一般不用改）" if open_
                  else "▶ 高级设置（一般不用改）"))
        if open_:
            self.api_adv.grid()
            self.api_lbl_adv.configure(text="点每项旁边的 ? 看白话说明")
        else:
            self.api_adv.grid_remove()
            self._api_refresh_adv_summary()

    def _api_refresh_adv_summary(self):
        """收起状态下，用一句话显示当前的高级参数。"""
        if getattr(self, "_api_adv_open", False):
            return
        if not hasattr(self, "api_lbl_adv"):
            return

        def val(var, default):
            try:
                text = str(var.get()).strip()
            except Exception:  # noqa: BLE001
                return default
            return text or default

        self.api_lbl_adv.configure(
            text=(f"当前：灵活度 {val(self.api_var_temp, '0.8')} · "
                  f"输出上限 {val(self.api_var_max_tokens, '8192')} · "
                  f"等待 {val(self.api_var_timeout, '90')} 秒 · "
                  f"间隔 {val(self.api_var_interval, '0.8')} 秒 · "
                  f"每次 {val(self.api_var_batch, '6')} 题 · "
                  f"并行 {val(self.api_var_concurrency, '4')} 路 · "
                  f"固定格式{'开' if self.api_var_json.get() else '关'} · "
                  f"深度思考{'关' if self.api_var_nothink.get() else '开'}"))

    def _api_refresh_think_hint(self):
        """勾选框旁边给一句结果提示，别让用户猜开关生效后能得到什么。"""
        if not hasattr(self, "api_lbl_think"):
            return
        if self.api_var_nothink.get():
            self.api_lbl_think.configure(
                text="比默认快 2~3 倍，单题花费约降七成", foreground="#0F6E56")
        else:
            self.api_lbl_think.configure(
                text="模型会先思考再作答，较慢但更稳", foreground="#8A6D3B")

    def api_collect_profile(self) -> dict:
        provider = api_config.LABEL_TO_PROVIDER.get(self.api_var_provider.get(), "openai")

        def number(var, label, cast, low, high):
            """超出范围直接提示，不再悄悄改成边界值。

            以前是静默裁剪：填 100 实际按 10 保存，界面上却还显示 100，
            看起来像「设置了没生效」。现在会明确告诉你范围是多少。
            """
            raw = var.get().strip()
            try:
                value = cast(raw)
            except (TypeError, ValueError):
                raise ValueError(f"「{label}」需要填写数字，当前是：{raw or '空'}")
            if not low <= value <= high:
                raise ValueError(f"「{label}」可填 {low:g} ~ {high:g}，当前是 {raw}")
            return value

        base = self.api_var_base.get().strip()
        model = self.api_var_model.get().strip()
        key = self.api_var_key.get().strip()
        if not base:
            raise ValueError("请填写 Base URL（可在「预设」里选一家自动填入）")
        if not model:
            raise ValueError("请填写模型名（例如 deepseek-chat）")
        if not key and ("localhost" in base or "127.0.0.1" in base):
            key = "local"          # 本地 Ollama / LM Studio 通常不校验 Key
        if not key:
            raise ValueError("请填写 API Key")
        return {
            "provider": provider,
            "preset": self.api_var_preset.get(),
            "base_url": base,
            "model": model,
            "api_key": key,
            "temperature": number(self.api_var_temp, "灵活度", float, 0.0, 2.0),
            "max_tokens": number(self.api_var_max_tokens, "输出上限", int, 64, 1000000),
            "timeout": number(self.api_var_timeout, "等待上限（秒）", int, 10, 900),
            "interval": number(self.api_var_interval, "请求间隔（秒）", float, 0.0, 120.0),
            "batch": number(self.api_var_batch, "每次题数", int, 1, 100),
            "concurrency": number(self.api_var_concurrency, "同时生成", int, 1, 16),
            "json_mode": bool(self.api_var_json.get()),
            "no_thinking": bool(self.api_var_nothink.get()),
        }

    def api_save_config(self, silent: bool = False) -> bool:
        try:
            prof = self.api_collect_profile()
        except ValueError as exc:
            if not silent:
                messagebox.showwarning("配置不完整", str(exc))
            return False
        provider = prof.pop("provider")
        cfg = copy.deepcopy(self.api_cfg)
        cfg["provider"] = provider
        cfg.setdefault("profiles", {})[provider] = prof
        # 题数与难度跟着模块二走，保存接口配置时一并落盘
        cfg["generation"] = {
            "n": self.ai_var_n.get(),
            "n_custom": self.ai_var_n_custom.get().strip(),
            "preset": self.ai_var_preset.get(),
            "ratios": {str(d): self.ai_vars_ratio[d].get() for d in (1, 2, 3, 4)},
        }
        try:
            api_config.save(cfg)
        except OSError as exc:
            if not silent:
                messagebox.showerror("保存失败", f"无法写入配置文件：{exc}")
            return False
        self.api_cfg = cfg
        if not silent:
            self.api_lbl_probe.configure(text="已保存到本机 ✓", foreground="#0F6E56")
        return True

    def api_save_config_clicked(self):
        if self.api_save_config():
            self.toast("接口配置已保存（仅本机，权限 600）")

    def api_test(self):
        try:
            prof = self.api_collect_profile()
        except ValueError as exc:
            messagebox.showwarning("配置不完整", str(exc))
            return
        if self._api_probe_pending:
            return
        self._api_probe_pending = True
        self.api_lbl_probe.configure(text="正在测试连接…", foreground="#185FA5")

        def work():
            result = api_client.test_connection(prof)
            self.api_events.put({"type": "test", "result": result})

        threading.Thread(target=work, daemon=True).start()

    def _api_ssl_help(self, hint: str = ""):
        """证书校验失败时给出可照做的步骤（本机 Python 缺 CA 是最常见原因）。"""
        script, version = "", ""
        m = re.search(r"/Python\.framework/Versions/([\d.]+)/", sys.executable or "")
        if m:
            version = m.group(1)
            script = f"/Applications/Python {version}/Install Certificates.command"
        steps = ["本机 Python 的根证书库是空的（用 python.org 安装包后没运行过证书脚本），",
                 "所以不是网络问题，也不是 API Key 填错。软件已自动改用 macOS 系统证书库重试。",
                 "", "如果仍然失败，按任意一条处理："]
        idx = 1
        if script:
            steps.append(f"{idx}) 在终端执行：open \"{script}\"")
            idx += 1
        steps.append(f"{idx}) 或在终端执行：\"{sys.executable}\" -m pip install --upgrade certifi")
        idx += 1
        steps.append(f"{idx}) 公司网络有代理替换证书时，把代理根证书装进「钥匙串访问」并设为「始终信任」")
        if hint:
            steps += ["", f"技术细节：{hint}"]
        messagebox.showwarning("HTTPS 证书校验失败", "\n".join(steps))

    # ---- API 模块：任务控制 ----
    def _api_set_buttons(self, running: bool):
        states = {"start": not running, "pause": running,
                  "resume": not running, "stop": running}
        for key, enabled in states.items():
            btn = self._api_buttons.get(key)
            if btn is not None:
                btn.configure(state="normal" if enabled else "disabled")

    def _api_log(self, text: str):
        try:
            self.api_log.configure(state="normal")
            self.api_log.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
            lines = int(self.api_log.index("end-1c").split(".")[0])
            if lines > 800:
                self.api_log.delete("1.0", f"{lines - 800}.0")
            self.api_log.see("end")
            self.api_log.configure(state="disabled")
        except tk.TclError:
            pass

    def api_clear_log(self):
        """清空运行日志（不影响任务进度与题库）。"""
        try:
            self.api_log.configure(state="normal")
            self.api_log.delete("1.0", "end")
            self.api_log.configure(state="disabled")
        except tk.TclError:
            pass

    def api_refresh_task_choices(self, select_id: str = ""):
        labels = []
        self.api_task_map = {}
        for s in api_task.list_tasks():
            name = s.get("name") or s["id"]
            label = (f"{name} · "
                     f"{api_task.STATUS_LABELS.get(s['status'], s['status'])}")
            base = label
            i = 2
            while label in self.api_task_map:
                label = f"{base} ({i})"
                i += 1
            labels.append(label)
            self.api_task_map[label] = s["id"]
        if not labels:
            labels = ["（暂无任务）"]
        self._api_task_loading = True
        try:
            self.api_cmb_task.set_values(labels, keep=False)
            target = next((l for l, tid in self.api_task_map.items() if tid == select_id), "")
            if target:
                self.api_var_task.set(target)
        finally:
            self._api_task_loading = False

    def api_on_task_pick(self):
        if self._api_task_loading:
            return
        if self.api_runner is not None and self.api_runner.is_alive():
            messagebox.showinfo("任务进行中", "请先「暂停」或「停止」当前任务，再切换查看其他任务。")
            target = ""
            if self.api_task:
                target = next((l for l, tid in self.api_task_map.items()
                               if tid == self.api_task.get("id")), "")
            self._api_task_loading = True
            try:
                if target:
                    self.api_var_task.set(target)
            finally:
                self._api_task_loading = False
            return
        tid = self.api_task_map.get(self.api_var_task.get())
        if not tid:
            return
        task = api_task.load_task(tid)
        if not task:
            messagebox.showwarning("任务不存在", "任务文件已被删除或损坏。")
            return
        self.api_task = task
        self._api_render_task(task)
        usage = task.get("usage") or {}
        self.api_lbl_usage.configure(
            text=f"累计请求 {usage.get('requests', 0)} 次 · "
                 f"输入 {usage.get('prompt_tokens', 0)} / 输出 {usage.get('completion_tokens', 0)} tokens")

    def _api_render_task(self, task):
        self.api_item_tree.delete(*self.api_item_tree.get_children())
        # 线程已经不在跑（或已点暂停/停止）时，残留的 running 一律显示为已暂停
        live = (self._api_running() and not self._api_pause_pending
                and task.get("status") == "running")
        for idx, item in enumerate(task.get("items") or []):
            status = item.get("status", "pending")
            if status == "running" and not live:
                status = "paused"
            self.api_item_tree.insert(
                "", "end", iid=str(idx),
                values=(item.get("topic_name", ""),
                        f"{item.get('done', 0)}/{item.get('need', 0)}",
                        api_task.STATUS_LABELS.get(status, status)),
                tags=(status,))
        totals = task.get("totals") or {}
        self._api_update_progress(totals.get("done", 0), totals.get("need", 0))
        status = task.get("status", "")
        if status == "running" and not live:
            status = "paused"
        self.api_lbl_task.configure(text=self._api_status_text(task, status, live))

    def _api_status_text(self, task, status, live=False) -> str:
        """任务行右侧提示：只写「现在该做什么」，任务名与状态在左边下拉里。"""
        if getattr(self, "_api_pause_pending", False):
            return "暂停中 · 正在收尾…"
        totals = task.get("totals") or {}
        done = int(totals.get("done", 0))
        need = int(totals.get("need", 0))
        remain = max(0, need - done)
        if task.get("error"):
            return str(task["error"])
        if status == "running":
            return "生成中 · 正在等模型返回…" if live else "已中断，点「继续」接着跑"
        if status in ("paused", "stopped", "partial"):
            if remain:
                return f"还有 {remain} 题未生成 · 点「继续」接着跑"
            return f"{done} 题已全部生成"
        if status == "done":
            return f"{done} 题已全部入库"
        if status == "empty":
            return "明细已清空，可删除该任务"
        if status == "error":
            return "已中止，请查看运行日志"
        return "点「开始生成」开跑"

    def _api_update_progress(self, done: int, need: int):
        need = int(need)
        done = int(done)
        if need <= 0:
            self.api_pb.configure(maximum=1, value=0)
            self.api_lbl_progress.configure(text="0 题")
            return
        self.api_pb.configure(maximum=need, value=min(done, need))
        text = f"{done} / {need} 题 · {round(done * 100 / need)}%"
        started = getattr(self, "_api_run_started", 0.0)
        base = getattr(self, "_api_run_base", 0)
        elapsed = time.time() - started if started else 0
        gained = done - base
        if started and elapsed > 20 and gained > 0:
            per_min = gained / (elapsed / 60.0)
            text += f" · {per_min:.1f} 题/分"
            left = need - done
            if per_min > 0 and left > 0:
                minutes = left / per_min
                text += (" · 约剩 " + (f"{minutes / 60:.1f} 小时" if minutes >= 90
                                       else f"{minutes:.0f} 分钟"))
        self.api_lbl_progress.configure(text=text)

    # ---- API 模块：任务与明细的删除 / 清空 / 改名 ----
    def _api_running(self) -> bool:
        return self.api_runner is not None and self.api_runner.is_alive()

    def _api_refuse_running(self, action: str) -> bool:
        if not self._api_running():
            return False
        messagebox.showinfo(
            "任务进行中",
            f"请先「暂停」或「停止」当前任务，再{action}。\n"
            "暂停后等任务状态变成「已暂停」就可以操作。")
        return True

    def _api_current_task(self):
        task = self.api_task
        if task is None:
            tid = self.api_task_map.get(self.api_var_task.get())
            if tid:
                task = api_task.load_task(tid)
                if task:
                    self.api_task = task
        return task

    def _api_reset_view(self):
        self.api_item_tree.delete(*self.api_item_tree.get_children())
        self.api_pb.configure(maximum=1, value=0)
        self.api_lbl_progress.configure(text="0 题")
        self.api_lbl_usage.configure(text="")
        self.api_lbl_task.configure(text="")

    def api_delete_items(self):
        task = self._api_current_task()
        if task is None:
            messagebox.showinfo("没有任务", "先在「任务」下拉里选一个任务，再删除明细。")
            return
        if self._api_refuse_running("删除进度明细"):
            return
        sel = list(self.api_item_tree.selection())
        if not sel:
            messagebox.showinfo("提示", "先在进度明细里选中要删除的知识点"
                                       "（可按住 Command 多选）。")
            return
        names = [str(self.api_item_tree.item(i, "values")[0]) for i in sel]
        preview = "、".join(names[:3]) + ("…" if len(names) > 3 else "")
        if not messagebox.askyesno(
                "删除知识点",
                f"从任务「{task.get('name') or task['id']}」里移除 {len(sel)} 个知识点？\n"
                f"{preview}\n\n"
                "已生成的题目仍留在题库；这个任务以后不再生成这些知识点。"):
            return
        removed = api_task.remove_items(task, sel)
        self._api_render_task(task)
        self.api_refresh_task_choices(select_id=task["id"])
        self._api_log(f"已移除 {removed} 个知识点，任务还剩 "
                      f"{len(task.get('items') or [])} 个")

    def api_clear_items(self):
        task = self._api_current_task()
        if task is None:
            messagebox.showinfo("没有任务", "先在「任务」下拉里选一个任务，再清空明细。")
            return
        if self._api_refuse_running("清空进度明细"):
            return
        items = task.get("items") or []
        if not items:
            messagebox.showinfo("提示", "这个任务已经没有知识点了。")
            return
        if not messagebox.askyesno(
                "清空明细",
                f"移除任务「{task.get('name') or task['id']}」里的全部 "
                f"{len(items)} 个知识点？\n\n"
                "已生成的题目仍留在题库，任务本身保留（可在「任务」下拉里删除它）。"):
            return
        removed = api_task.remove_items(task, range(len(items)))
        self._api_render_task(task)
        self.api_refresh_task_choices(select_id=task["id"])
        self._api_log(f"已清空明细：移除 {removed} 个知识点，任务本身保留")

    def api_rename_task(self):
        task = self._api_current_task()
        if task is None:
            messagebox.showinfo("没有任务", "先在「任务」下拉里选一个任务，再改名。")
            return
        if self._api_refuse_running("重命名任务"):
            return
        name = simpledialog.askstring(
            "重命名任务", "任务名（同时用作任务 JSON 的文件名）：",
            initialvalue=str(task.get("name") or ""), parent=self.root)
        if name is None:
            return
        if not name.strip():
            messagebox.showwarning("任务名不能为空", "请输入一个任务名。")
            return
        old = task.get("name") or task["id"]
        new = api_task.rename_task(task, name)
        self._api_render_task(task)
        self.api_refresh_task_choices(select_id=task["id"])
        self._api_log(f"任务已重命名：{old} → {new}")
        if new != " ".join(name.split())[:40].strip():
            self._api_log("  与已有任务重名，已自动加序号区分")
        self._api_log(f"  JSON 存档：{api_task.archive_path(task)}")

    def api_delete_task(self):
        task = self._api_current_task()
        if task is None:
            messagebox.showinfo("没有任务", "当前没有可删除的任务。")
            return
        if self._api_refuse_running("删除任务"):
            return
        totals = task.get("totals") or {}
        done = int(totals.get("done") or 0)
        name = task.get("name") or task["id"]
        if not messagebox.askyesno(
                "删除任务",
                f"删除任务「{name}」？\n\n"
                "· 任务进度文件会删除\n"
                "· 该任务的 JSON 存档会删除\n"
                f"· 题库里已生成的 {done} 道题保留\n\n"
                "删除后不能撤销。"):
            return
        api_task.delete_task(task["id"])
        api_task.remove_archive(task)
        self.api_task = None
        self._api_reset_view()
        self._api_log(f"已删除任务：{name}（题库保留 {done} 题）")
        self.api_refresh_task_choices()
        if self.api_task_map:
            self.api_on_task_pick()          # 自动切到剩下的最新任务
        else:
            self.api_lbl_task.configure(text="已删除，当前没有任务")

    def _api_spawn(self, task, profile) -> bool:
        if self.api_runner is not None and self.api_runner.is_alive():
            messagebox.showinfo("任务进行中", "已有任务在运行，请先暂停或停止。")
            return False
        api_task.save_task(task)
        self.api_task = task
        self._api_render_task(task)
        self._api_log(f"—— 任务 {task['id']} · {task.get('name', '')} ——")
        self._api_log(f"   {len(task['items'])} 个知识点 / {task['totals']['need']} 题 / "
                      f"JSON 存档 {os.path.basename(api_task.archive_path(task))}")
        self.api_runner = api_task.Runner(task, profile, self.api_events.put)
        self._api_run_started = time.time()
        self._api_run_base = int((task.get("totals") or {}).get("done", 0))
        self.api_runner.start()
        self._api_pause_pending = False
        self._api_set_buttons(running=True)
        self.api_lbl_task.configure(text="生成中…")
        return True

    def api_start(self):
        if self.api_runner is not None and self.api_runner.is_alive():
            messagebox.showinfo("任务进行中", "已有任务在运行，请先暂停或停止。")
            return
        # 勾选与题量来自模块二；这里只是把它们变成任务
        pairs = self._api_selected_pairs()
        if not pairs:
            if messagebox.askyesno(
                    "还没有选择知识点",
                    "生成内容在「选择知识点生成内容」页选择。现在去选？"):
                self.api_goto_prompt()
            return
        if self.api_task and self.api_task.get("totals", {}).get("done", 0) < \
                self.api_task.get("totals", {}).get("need", 0) and \
                self.api_task.get("status") != "error":
            t = self.api_task["totals"]
            ans = ask_yes_no_cancel(
                self.root, "发现未完成任务",
                f"当前任务还有 {t['need'] - t['done']} 题未完成。\n\n"
                "点「是」继续原任务；点「否」用当前选择新建任务（原任务保留）；"
                "点「取消」关闭本弹框，什么都不做。")
            if ans is None:          # 取消：不做任何操作
                return
            if ans == "yes":         # 是：继续原任务
                self.api_resume()
                return
        try:
            profile = self.api_collect_profile()
        except ValueError as exc:
            messagebox.showwarning("接口配置不完整", str(exc))
            return
        n = self._ai_current_n()
        mix = self._ai_mix() or dict(AI_PRESETS["基础为主"])
        self.api_save_config(silent=True)
        task = api_task.new_task(pairs, n, mix, profile)
        self._api_log(f"新建任务「{task['name']}」：{len(pairs)} 个知识点 × {n} 题，"
                      f"难度 " + "/".join(f"{DIFFICULTY_LABEL[d]}{mix.get(d, 0)}%"
                                          for d in (1, 2, 3, 4)))
        if self._api_spawn(task, profile):
            self.api_refresh_task_choices(select_id=task["id"])

    def api_pause(self):
        if self.api_runner is None or not self.api_runner.is_alive():
            return
        if self._api_pause_pending:
            return
        self._api_pause_pending = True
        self.api_runner.pause()
        self._api_set_buttons(running=False)
        # 状态列立刻改成「暂停中」，不用等线程收尾那一两百毫秒
        for iid in self.api_item_tree.get_children():
            vals = self.api_item_tree.item(iid, "values")
            if len(vals) >= 3 and str(vals[2]) == api_task.STATUS_LABELS["running"]:
                self.api_item_tree.item(iid, values=(vals[0], vals[1], "暂停中"),
                                        tags=("paused",))
        self._api_log("已暂停：当前请求已放弃，进度保存在断点处。")
        self.api_lbl_task.configure(text="暂停中…")

    def api_stop(self):
        if self.api_runner is None or not self.api_runner.is_alive():
            return
        if self._api_pause_pending:
            return
        if not messagebox.askyesno("停止任务", "停止后已生成的题目会保留，可稍后「继续」。确定停止？"):
            return
        self._api_pause_pending = True
        self.api_runner.stop()
        self._api_set_buttons(running=False)
        self._api_log("已停止：当前请求已放弃，已生成的题目都保留。")
        self.api_lbl_task.configure(text="正在停止…")

    def api_resume(self):
        if self.api_runner is not None and self.api_runner.is_alive():
            self.api_lbl_task.configure(text="上一个请求正在收尾，稍等再点…")
            return
        task = self.api_task
        if task is None:
            tid = self.api_task_map.get(self.api_var_task.get())
            task = api_task.load_task(tid) if tid else None
        if not task:
            messagebox.showinfo("没有可继续的任务", "请先开始一个新任务，或在「任务」下拉里选择历史任务。")
            return
        if not task.get("items"):
            messagebox.showinfo(
                "任务已清空",
                "这个任务的知识点已被清空，无法继续生成。\n"
                "可以点「删除任务」清掉它，或到「选择知识点生成内容」重新选点开始新任务。")
            return
        totals = task.get("totals") or {}
        if totals.get("done", 0) >= totals.get("need", 0):
            messagebox.showinfo("任务已完成", "这个任务已经生成完了，可以新建任务或去「API 任务题库」导出。")
            return
        try:
            profile = self.api_collect_profile()
        except ValueError as exc:
            messagebox.showwarning("接口配置不完整", str(exc))
            return
        api_task.resume_task(task)
        self.api_save_config(silent=True)
        if self._api_spawn(task, profile):
            self.api_refresh_task_choices(select_id=task["id"])

    def api_jump_item(self, _event=None):
        sel = self.api_item_tree.selection()
        if not sel or not self.api_task:
            return
        try:
            item = self.api_task["items"][int(sel[0])]
        except (ValueError, IndexError):
            return
        self._select_knowledge(item.get("topic_id", ""))

    def _select_knowledge(self, topic_id: str):
        found = store.find_point(topic_id)
        if not found:
            messagebox.showinfo("找不到知识点", "该知识点可能已被删除。")
            return
        kp, vol = found
        self.current_volume = vol["volume_id"]
        self._refresh_tree(keep=f"vol::{vol['volume_id']}")
        self.refresh_list()
        self.nb.select(0)
        if self.list.exists(topic_id):
            self.list.selection_set(topic_id)
            self.list.see(topic_id)
            self.on_list_select()

    # ---- API 模块：事件循环 ----
    def _poll_api_events(self):
        """把后台事件搬到界面。任何单个事件出错都不能让整个泵停掉。"""
        if self._closing:
            return
        try:
            while True:
                try:
                    ev = self.api_events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._api_handle_event(ev)
                except tk.TclError:
                    raise
                except Exception as exc:  # noqa: BLE001 —— 一个事件画不出来，别拖死后面所有事件
                    self._crash_note(f"处理事件 {ev.get('type')}", exc)
            self._poll_watchdog()
        except tk.TclError:
            return
        finally:
            self._reschedule_poll()

    def _reschedule_poll(self):
        if self._closing:
            return
        try:
            self._api_poll_id = self.root.after(150, self._poll_api_events)
        except tk.TclError:
            pass

    def _poll_watchdog(self):
        """线程已经结束但收尾事件丢了，别把四个按钮一直锁死。"""
        runner = self.api_runner
        if runner is None or runner.is_alive() or self._closing:
            return
        start_btn = self._api_buttons.get("start")
        if start_btn is None or str(start_btn.cget("state")) != "disabled":
            return
        self._api_set_buttons(running=False)
        self._api_pause_pending = False
        self._api_log("任务线程已结束，按钮已复位；进度未更新的任务可点「继续」接着生成")

    def _crash_note(self, where: str, exc: BaseException):
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        path = ""
        try:
            path = os.path.join(api_config.data_dir(), "crash.log")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where}\n{text}")
        except OSError:
            path = ""
        try:
            if hasattr(self, "api_log"):
                self._api_log(f"内部错误（{where}）：{exc}")
                if path:
                    self._api_log(f"  详情已写入 {path}")
        except Exception:  # noqa: BLE001
            pass

    def _on_tk_error(self, exc, val, tb):
        self._crash_note("界面回调", val if val is not None else exc)

    def _api_handle_event(self, ev):
        kind = ev.get("type")
        if kind == "log":
            self._api_log(str(ev.get("text", "")))
        elif kind == "item":
            idx = ev.get("index")
            iid = str(idx)
            if self.api_item_tree.exists(iid):
                status = ev.get("status", "pending")
                if status == "running" and self._api_pause_pending:
                    status = "paused"      # 已点暂停，晚到的进度事件别再显示生成中
                self.api_item_tree.item(
                    iid, values=(self.api_item_tree.item(iid, "values")[0],
                                 f"{ev.get('done', 0)}/{ev.get('need', 0)}",
                                 api_task.STATUS_LABELS.get(status, status)),
                    tags=(status,))
        elif kind == "progress":
            self._api_update_progress(ev.get("done", 0), ev.get("need", 0))
        elif kind == "usage":
            usage = ev.get("usage") or {}
            self.api_lbl_usage.configure(
                text=f"请求 {usage.get('requests', 0)} 次 · "
                     f"输入 {usage.get('prompt_tokens', 0)} / "
                     f"输出 {usage.get('completion_tokens', 0)} tokens")
        elif kind == "beat":
            if self.api_runner is not None and self.api_runner.is_alive() \
                    and not self._api_pause_pending:
                elapsed = int(ev.get("elapsed", 0))
                limit = int(ev.get("timeout", 90) or 90)
                self.api_lbl_task.configure(
                    text=f"生成中 · 已等待 {elapsed} 秒（单次超时 {limit} 秒）")
        elif kind == "status":
            status = ev.get("status", "")
            if status == "running" and self._api_pause_pending:
                self.api_lbl_task.configure(text="暂停中…")
                return
            self.api_lbl_task.configure(
                text=api_task.STATUS_LABELS.get(status, status))
        elif kind == "finished":
            self._api_on_finished(ev)
        elif kind == "test":
            self._api_probe_pending = False
            result = ev.get("result") or {}
            ok = bool(result.get("ok"))
            self.api_lbl_probe.configure(
                text=str(result.get("message", "")),
                foreground="#0F6E56" if ok else "#B3261E")
            if ok:
                self.api_save_config(silent=True)
                note = str(result.get("ssl") or "")
                if note and "Python 自带" not in note:
                    self._api_log(f"证书：{note}")
            self._api_log(("连接测试成功：" if ok else "连接测试失败：")
                          + str(result.get("message", "")))
            hint = str(result.get("hint") or "")
            if hint and not ok:
                self._api_log(f"处理建议：{hint}")
            if not ok and result.get("kind") == "ssl":
                self._api_ssl_help(hint)

    def _api_on_finished(self, ev):
        tid = str(ev.get("task_id") or "")
        current = str((self.api_task or {}).get("id") or "")
        if tid and current and tid != current:
            return  # 旧任务晚到的收尾事件，别动当前任务的界面
        self._api_pause_pending = False
        self._api_set_buttons(running=False)
        status = ev.get("status", "")
        done, need = ev.get("done", 0), ev.get("need", 0)
        if self.api_task:
            self._api_render_task(self.api_task)      # 先刷明细，最后再写状态文字
            self.api_refresh_task_choices(select_id=self.api_task.get("id", ""))
        self._api_update_progress(done, need)
        label = api_task.STATUS_LABELS.get(status, status)
        remain = max(0, need - done)
        if status in ("paused", "stopped"):
            text = (f"还有 {remain} 题未生成 · 点「继续」接着跑" if remain
                    else f"{done} 题已全部生成")
        elif status == "done":
            text = f"{done} 题已全部入库"
        elif status == "partial":
            text = f"部分完成 · 还有 {remain} 题，点「继续」重试"
        else:
            text = f"{label} · {done}/{need} 题"
        self.api_lbl_task.configure(text=text)
        self._update_status()
        if status == "error":
            messagebox.showerror("生成中止", ev.get("error") or "任务出错，请查看运行日志。")
        elif status == "done":
            self.toast(f"全部完成：共 {done} 题，已存入题库")
        elif status == "partial":
            self.toast(f"部分完成：{done}/{need} 题，有 {ev.get('failed', 0)} 个知识点失败，可点「继续」重试")
        bank_active = self.nb.index(self.nb.select()) == 3
        if bank_active:
            self.bank_api_page.refresh()

    def _api_restore_hint(self):
        if self.api_runner is not None and self.api_runner.is_alive():
            return
        try:
            tasks = api_task.list_tasks()
        except Exception:  # noqa: BLE001
            return
        unfinished = [t for t in tasks
                      if t["status"] in ("paused", "stopped", "partial", "running")
                      and t["done"] < t["total"]]
        if not unfinished:
            return
        s = unfinished[0]
        task = api_task.load_task(s["id"])
        if not task:
            return
        self.api_task = task
        self._api_render_task(task)
        self.api_refresh_task_choices(select_id=task["id"])
        self._api_log(f"发现未完成任务：{s.get('name') or s['id']} · "
                      f"{s['done']}/{s['total']} 题。点「继续」接着生成。")

    # ================= 题库对外入口 =================
    def _active_bank_page(self):
        """文件菜单的导出跟随当前标签页；不在题库页时默认全部题库。"""
        try:
            index = self.nb.index(self.nb.select())
            api_index = self.nb.index(self.tab_bank_api)
        except (tk.TclError, AttributeError):
            return self.bank_all_page
        return self.bank_api_page if index == api_index else self.bank_all_page

    def bank_export(self, fmt: str):
        self._active_bank_page().export(fmt)

    def _on_tab_changed(self, _event=None):
        try:
            index = self.nb.index(self.nb.select())
        except tk.TclError:
            return
        if index == 3:
            self.bank_api_page.refresh_tasks()
        elif index == 4:
            self.bank_all_page.refresh()
        elif index == 2:
            self._api_refresh_source()      # 生成内容摘要跟着模块二的勾选走

    def _on_close(self):
        self._closing = True
        for after_id in (self._api_poll_id, self._api_hint_id):
            if after_id:
                try:
                    self.root.after_cancel(after_id)
                except tk.TclError:
                    pass
        if self.api_runner is not None and self.api_runner.is_alive():
            if not messagebox.askyesno(
                    "任务进行中",
                    "生成任务还在运行。退出前会暂停并保存进度，下次可继续。\n确定退出？"):
                return
            self.api_runner.pause()
            self.api_runner.join(5)
        self.api_save_config(silent=True)
        try:
            self.root.destroy()
        except tk.TclError:
            pass

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
        self.ai_refresh()                 # 勾选与筛选保留
        self._api_refresh_source()        # API 页的「生成内容」摘要跟着更新
        self._update_status()

    def _update_status(self):
        s = store.stats()
        parts = [f"{store.STAGE_NAMES[k]} {v.get('chinese',0)+v.get('math',0)+v.get('english',0)}"
                 for k, v in s["by_stage"].items()]
        try:
            api_total = api_bank.stats(api_bank.BANK_API)["total"]
            all_total = api_bank.stats(api_bank.BANK_ALL)["total"]
        except Exception:  # noqa: BLE001
            api_total = all_total = 0
        self.lbl_stat.configure(
            text=f"{s['volumes']} 册 · {s['points']} 知识点 · "
                 f"题库 {api_total}（API）+ {all_total}（全部） · "
                 + " / ".join(parts))

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

【模块二 · 选择知识点生成内容】
6. 这一页负责「生成什么」：勾选知识点、定题数与难度。筛选栏的学段 / 科目 / 年级
   默认「全部」，选了学段后年级只显示对应年级。
7. 直接点任意一行即可勾选 / 取消（选中的知识点整行变蓝），按住 Shift 再点另一行
   可连选两行之间的全部数据。
8. 「每知识点题数」可选 1/3/5/10/15/20/50/100，或选「自定义」；难度用
   容易/中等/较难/难 四档，预设默认「基础为主」（50/30/15/5），可改四个百分比。
9. 「输出格式」可选 JSON / TXT / Excel / CSV，「根据选择生成大模型提示词」后
   可复制到 DeepSeek / 元宝 / ChatGPT 手动使用。
10. 提示词含完整知识点档案、年级红线、难度分布与来源字段，便于溯源。
    点「本地API生成」会带着当前勾选与题数跳到模块三。

【模块三 · 大模型API生成】
11. 这一页只负责「怎么生成」：配置接口与看进度。协议选 OpenAI 兼容 /
    Anthropic Claude / Google Gemini，在「预设」里选一家会一键填好 Base URL
    和模型，再粘贴你自己的 API Key。点「测试连接」确认可用；
    配置自动保存在本机（文件权限 600，不会上传）。
    中间的「高级设置」平时是收起的：灵活度、输出上限、等待上限、请求间隔、
    每次题数、同时生成、固定格式（JSON）、关闭深度思考 都在里面，每项旁边有个
    蓝色 ?，鼠标停一下看一句话，点一下看完整白话说明。不懂就保持默认。
12. 生成内容不在本页设置：页面上方会显示当前已选多少个知识点、每点几题、
    「明细」一行按年级 + 学科拆开，写明每个年级学科选了多少条、预计多少题；
    再下面是难度怎么分配。点「去选择知识点」可回去修改。
13. 点「开始生成」后，软件会同时跑多个知识点（「同时生成」默认 4 路，可调到 16），
    进度条、明细表和日志实时刷新，同时统计请求次数与输入/输出 tokens。
    进度行会显示「X 题/分」和「约剩多少时间」；批量出题自动使用精简输出格式
    （只回题干/选项/答案/解析/难度，其余字段本地补），比完整版省一半 token。
    高级设置里的「关闭深度思考」默认是勾上的：让模型跳过作答前的推理过程，
    单题耗时与花费降到三分之一左右。服务商不支持这个参数时会自动改回默认方式，
    日志里写明「该模型不支持关闭深度思考」，不影响出题。若发现理科难题变简单，
    把这一项取消勾选即可。
14. 任务名按「年级 学科 · 知识点」自动生成（多个知识点显示「首个 等 N 点」），
    点「重命名」可以改；任务名同时用作该任务 JSON 存档的文件名。
15. 「暂停」「停止」立即生效（所有在途请求一起放弃，一秒内停下）；暂停时明细和
    任务状态都会显示「暂停中 / 已暂停」。已完成的题目都保留，直接关掉软件也不会丢进度，
    下次打开会显示「已暂停」，选中后点「继续」即从断点接着生成。
16. 进度明细里选中知识点行，点「删除选中」可把它从本次任务移除（按住 Command 可多选）；
    「清空明细」移除当前任务的全部知识点。两者都只改任务，不动已经生成的题目。
    任务下拉旁的「删除任务」会删掉任务进度和它的 JSON 存档，题库里的题目保留。
17. 每个任务在数据目录 exports/ 下有一份独立 JSON（文件名 = 任务名），运行中会
    自动更新；点「打开生成文件夹」直接打开这个目录；「API 任务题库」页的「按任务导出」
    可以把筛选结果按任务拆成多个文件。
18. 「清空日志」只清空右侧的日志显示，不影响任务进度、题库和已生成的题目。
19. 网络错误与限流会自动处理：遇到 429 会让所有并发一起降速、冷却后自动恢复；
    其它错误按退避重试，重试过程写进日志（第几次、多久后重试）。
    等待模型响应时状态栏显示「已等待 N 秒」，不会有长时间没反应的错觉；
    输出上限或每批题数较大时，超时会自动放宽并在日志说明。
20. Key 无效、模型不存在、证书校验失败会立即中止全部并发并给出处理步骤；
    单题解析失败只影响该批，不会拖垮整个任务。返回的题目会自动校验选项、答案、
    解析与难度，重复题干按 id 去重，不会重复入库。
21. 生成结果直接写入「API 任务题库」，可以继续筛选、预览、删除与导出。

【模块四 · API 任务题库】
22. 一级标签页，专门管大模型生成的题。任务下拉选「全部任务」看全部，选某个任务
    只看该任务生成的题（任务名后带题数）；「删除本任务题目」只删题，任务与进度保留。
23. 筛选、预览、导出与「模块五」一致：搜索 / 学段 / 学科 / 年级 / 难度，
    点一行在下方看完整题目。

【模块五 · 全部题库】
24. 一级标签页，管导入的题和从 API 题库合并来的题。
25. 「导入题库…」支持 JSON / CSV / TSV / TXT / Excel(.xlsx/.xlsm)，可一次多选；
    旧版 .xls 若是 HTML/XML 表格也能直接读，二进制老格式会提示另存为 .xlsx。
    表头中英文都认（题干 / question、选项A / option_a…）；文件里没有学科年级时，
    按知识点 ID 或名称回查知识地图自动补齐，本软件自己导出的 JSON/TXT 也能直接导回。
26. 导入按「题干 + 选项」自动排重，重复题过滤；题干选项相同但答案不同的算冲突，
    同样过滤并报数。TXT 按空行分块：题干 / A. B. C. D. / 答案：X / 解析：…
27. 「从 API 题库合并…」会先列出「全部 API 任务」和每个具体任务（带题数），
    选哪个就只合哪个；两套库互相独立，合并只是复制，在一边删除不影响另一边。
28. 导出当前筛选结果：JSON / TXT / CSV / TSV / Excel(.xlsx)，文件名带库名与日期。
    导出的学科、年级、难度都写中文，再导入能原样读回；「按任务导出」为每个任务
    写一份「任务名.json」。不再提供 JSONL。
29. 「删除所选」「清空题库」前都会自动备份到数据目录 backups/，可手动找回。
30. 数据位置：菜单「文件 → 打开 API 数据目录」。config.json 是接口配置，
    questions.jsonl 是 API 任务题库，all_questions.jsonl 是全部题库，
    tasks/ 是任务进度，exports/ 是每个任务的 JSON 存档。

【其它】
31. 工具菜单的「本地模板引擎（旧）」保留旧的内容感知直接出题，仅作对比。
32. volumes/ 是 data/*.py 生成的产物；要永久固化修改，请同步回源文件再运行
    build_volumes.py。""")

    def about_dialog(self):
        s = store.stats()
        info = store.original_info() or {}
        original = (f"出厂配置：{info.get('volumes')} 册 / {info.get('points')} 知识点\n"
                    if info else "")
        bank = api_bank.stats(api_bank.BANK_API)
        all_bank = api_bank.stats(api_bank.BANK_ALL)
        messagebox.showinfo(
            "关于",
            f"{APP_TITLE}\nv{VERSION}\n\n"
            f"当前：{s['volumes']} 册 / {s['points']} 知识点\n"
            f"{original}"
            f"API 任务题库：{bank['total']} 题（覆盖 {bank['topics']} 个知识点）\n"
            f"全部题库：{all_bank['total']} 题（覆盖 {all_bank['topics']} 个知识点）\n\n"
            "出题方式：生成提示词 / 大模型 API 生成（OpenAI 兼容 · Claude · Gemini）\n"
            "内容感知型模板引擎 · Python 标准库 + Tcl/Tk")


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


class BankPage:
    """一套题库界面：API 任务题库、全部题库各一个实例。

    两套库的筛选条件、列表、预览、工具栏完全独立，切标签页不会串状态；
    共用的只有数据层和主窗口底部的状态栏。
    """

    def __init__(self, app, parent, mode: str):
        self.app = app
        self.root = app.root
        self.mode = mode
        self.api_mode = mode == api_bank.BANK_API
        self.rows = []                  # 当前筛选结果（导出用完整结果）
        self.by_id = {}                 # 列表 iid → 记录
        self.refresh_job = None         # 80ms 合并刷新
        self.task_labels = {"全部任务": ""}
        self.syncing = False
        self.cols_api = ("subject", "grade", "topic", "stem", "answer",
                         "difficulty", "created")
        self.cols_all = ("subject", "grade", "topic", "stem", "answer",
                         "difficulty", "origin")
        self.cols = self.cols_api if self.api_mode else self.cols_all
        self._build(parent)

    # ---------------- 界面 ----------------
    def _build(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=3)
        parent.rowconfigure(4, weight=2)

        # 第一行：筛选
        flt = ttk.Frame(parent)
        flt.grid(row=0, column=0, sticky="ew", pady=(2, 6))
        ttk.Label(flt, text="搜索").pack(side="left")
        self.var_q = tk.StringVar()
        entry = ttk.Entry(flt, textvariable=self.var_q, width=22)
        entry.pack(side="left", padx=(4, 10))
        entry.bind("<Return>", lambda _: self.refresh())
        ttk.Label(flt, text="学段").pack(side="left")
        self.var_stage = tk.StringVar(value="全部")
        ClickCombo(flt, self.var_stage, list(AI_STAGE), width=8,
                   command=self.on_stage_change).pack(side="left", padx=(4, 8))
        ttk.Label(flt, text="学科").pack(side="left")
        self.var_subject = tk.StringVar(value="全部")
        ClickCombo(flt, self.var_subject, list(AI_SUBJECT), width=8,
                   command=self.refresh).pack(side="left", padx=(4, 8))
        ttk.Label(flt, text="年级").pack(side="left")
        self.var_grade = tk.StringVar(value="全部")
        self.cmb_grade = ClickCombo(
            flt, self.var_grade,
            ["全部"] + [k for k in AI_GRADE if k != "全部"], width=11,
            command=self.refresh)
        self.cmb_grade.pack(side="left", padx=(4, 8))
        ttk.Label(flt, text="难度").pack(side="left")
        self.var_diff = tk.StringVar(value="全部")
        ClickCombo(flt, self.var_diff,
                   ["全部"] + [DIFFICULTY_LABEL[d] for d in (1, 2, 3, 4)], width=7,
                   command=self.refresh).pack(side="left", padx=(4, 8))
        ttk.Button(flt, text="筛选", command=self.refresh).pack(side="left", padx=(0, 6))
        ttk.Button(flt, text="重置", command=self.reset_filters).pack(side="left")

        # 第二行：任务下拉（仅 API 库）/ 列表标题 / 统计
        bar = ttk.Frame(parent)
        bar.grid(row=1, column=0, sticky="ew")
        if self.api_mode:
            ttk.Label(bar, text="任务").pack(side="left")
            self.var_task = tk.StringVar(value="全部任务")
            self.cmb_task = ClickCombo(bar, self.var_task, ["全部任务"],
                                       width=28, command=self.refresh)
            self.cmb_task.pack(side="left", padx=(4, 12))
        self.lbl_head = ttk.Label(
            bar,
            text=("题目列表（按任务查看，最新在前）" if self.api_mode
                  else "全部题库（导入 + 合并，最新在前）"),
            style="Head.TLabel")
        self.lbl_head.pack(side="left")
        self.lbl_stats = ttk.Label(bar, text="", foreground="#0F6E56")
        self.lbl_stats.pack(side="right")

        # 第三行：题目列表
        cols = ("subject", "grade", "topic", "stem", "answer",
                "difficulty", "origin", "created")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings",
                                 displaycolumns=self.cols, selectmode="extended")
        heads = {"subject": "学科", "grade": "年级", "topic": "知识点",
                 "stem": "题干", "answer": "答案", "difficulty": "难度",
                 "origin": "来源", "created": "生成时间"}
        widths = {"subject": 52, "grade": 84, "topic": 140, "stem": 430,
                  "answer": 44, "difficulty": 52, "origin": 220, "created": 100}
        for c in cols:
            self.tree.heading(c, text=heads[c])
            self.tree.column(c, width=widths[c], stretch=False,
                             anchor="center" if c in ("subject", "answer",
                                                      "difficulty", "created")
                             else "w")
        self.tree.grid(row=2, column=0, sticky="nsew")
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        sb.grid(row=2, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Configure>", self.fit_columns)

        ttk.Label(parent, text="题目预览（双击列表项查看完整题目）",
                  style="Head.TLabel").grid(row=3, column=0, sticky="w", pady=(8, 3))
        self.preview = scrolledtext.ScrolledText(
            parent, wrap="word", height=8, font=("", 12), background=LIGHT_PANEL,
            foreground=LIGHT_TEXT, insertbackground=LIGHT_TEXT, padx=10, pady=8)
        self.preview.grid(row=4, column=0, sticky="nsew", columnspan=2)
        self.preview.configure(state="disabled")
        self.preview.tag_configure("title", font=("", 13, "bold"))
        self.preview.tag_configure("label", font=("", 11, "bold"),
                                   foreground="#185FA5")

        # 第四行：工具栏
        tools = ttk.Frame(parent, padding=(0, 8))
        tools.grid(row=5, column=0, columnspan=2, sticky="ew")
        if self.api_mode:
            self._export_button(tools).pack(side="left", padx=(0, 10))
            ttk.Button(tools, text="按任务导出",
                       command=self.export_by_task).pack(side="left", padx=(0, 10))
            ttk.Button(tools, text="合并到全部题库",
                       command=self.merge_current).pack(side="left", padx=(0, 10))
            ttk.Button(tools, text="删除本任务题目",
                       command=self.delete_task).pack(side="left", padx=(0, 6))
        else:
            ttk.Button(tools, text="导入题库…",
                       command=self.import_files).pack(side="left", padx=(0, 8))
            ttk.Button(tools, text="从 API 题库合并…",
                       command=self.merge_dialog).pack(side="left", padx=(0, 12))
            self._export_button(tools).pack(side="left", padx=(0, 10))
        ttk.Button(tools, text="删除所选",
                   command=self.delete_selected).pack(side="left", padx=(0, 6))
        ttk.Button(tools, text=("清空 API 题库" if self.api_mode else "清空全部题库"),
                   command=self.clear).pack(side="left")
        self.lbl_msg = ttk.Label(tools, text="", foreground="#0F6E56")
        self.lbl_msg.pack(side="right")
        ttk.Button(tools, text="刷新", command=self.refresh_tasks).pack(
            side="right", padx=(0, 6))
        ttk.Button(tools, text="数据目录",
                   command=lambda: subprocess_open(api_config.data_dir())).pack(
            side="right", padx=(0, 6))

        if self.api_mode:
            self.refresh_tasks()
        else:
            self.refresh()

    # ---------------- 数据刷新 ----------------
    def _task_names(self):
        try:
            tasks = api_task.list_tasks(limit=0)
        except Exception:  # noqa: BLE001
            tasks = []
        return {t["id"]: (t.get("name") or t["id"]) for t in tasks}

    def task_id(self) -> str:
        if not self.api_mode:
            return ""
        return self.task_labels.get(self.var_task.get(), "")

    def refresh(self):
        """筛选变化先排队 80ms，短时间内的多次改动合并成一次全库查询。"""
        if self.syncing:
            return
        if self.refresh_job is not None:
            try:
                self.root.after_cancel(self.refresh_job)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.refresh_job = self.root.after(80, self._do_refresh)
        except Exception:  # noqa: BLE001
            self._do_refresh()

    def _do_refresh(self):
        self.refresh_job = None
        if self.api_mode:
            self._sync_task_labels()
        self.rows = api_bank.query(
            q=self.var_q.get(),
            subject=AI_SUBJECT.get(self.var_subject.get(), ""),
            stage=AI_STAGE.get(self.var_stage.get(), ""),
            grade=AI_GRADE.get(self.var_grade.get(), ""),
            difficulty=DIFFICULTY_CODE.get(self.var_diff.get()),
            task_id=self.task_id(),
            bank=self.mode)
        self.by_id = {}
        names = self._task_names()
        shown = self.rows[:2000]                 # 列表只渲染最近 2000 行
        self.tree.delete(*self.tree.get_children())
        used_iid = set()
        for row_index, rec in enumerate(shown):
            created = str(rec.get("created_at", ""))[5:16].replace("T", " ")
            tid = str(rec.get("task_id") or "")
            if self.api_mode:
                origin = _short_name(names.get(tid, "未标记任务"))
            elif rec.get("origin") == "api":
                origin = "API · " + _short_name(names.get(
                    str(rec.get("origin_task_id") or tid), "任务"), 12)
            else:
                origin = "导入 · " + _short_name(
                    os.path.basename(str(rec.get("origin_file") or "文件")), 14)
            iid = str(rec.get("id") or f"row-{row_index}")
            base_iid, n = iid, 2
            while iid in used_iid:                # 极少数脏数据会撞 id
                iid = f"{base_iid}#{n}"
                n += 1
            used_iid.add(iid)
            self.by_id[iid] = rec
            self.tree.insert(
                "", "end", iid=iid,
                values=(store.SUBJECT_NAMES.get(rec.get("subject", ""),
                                                rec.get("subject", "")),
                        rec.get("grade_label", ""), rec.get("topic_name", ""),
                        rec.get("stem", "")[:80], rec.get("answer", ""),
                        _diff_label(rec.get("difficulty", "")), origin, created))

        s = api_bank.stats(self.mode)
        extra = f" · 仅显示最近 {len(shown)} 题" if len(self.rows) > len(shown) else ""
        if not self.api_mode and s["total"] == 0:
            self.lbl_stats.configure(
                text="全部题库还是空的 —— 点「导入题库…」或「从 API 题库合并…」开始")
        elif self.rows or s["total"]:
            self.lbl_stats.configure(
                text=f"共 {s['total']} 题 · {s['topics']} 知识点 · "
                     f"今日 +{self._today()} ｜ 当前 {len(self.rows)} 题{extra}")
        else:
            self.lbl_stats.configure(text="当前筛选下没有题目")
        if not self.rows:
            self._show_hint()

    def _sync_task_labels(self):
        """任务名后的题数会随生成变化，静默刷新选项（不动当前选择）。"""
        counts = api_bank.task_counts()
        mapping = {"全部任务": ""}
        labels = ["全部任务"]
        used = {"全部任务"}
        for tid, name in self._task_names().items():
            label = f"{_short_name(name, 16)} · {counts.get(tid, 0)} 题"
            while label in used:
                label += " "
            used.add(label)
            labels.append(label)
            mapping[label] = tid
        if labels == list(self.task_labels.keys()):
            return                               # 选项没变就不重建菜单
        current_id = self.task_id()
        self.task_labels = mapping
        keep = next((lbl for lbl, tid in mapping.items() if tid == current_id),
                    "全部任务")
        self.syncing = True
        try:
            self.cmb_task.set_values(labels, keep=True)
            self.var_task.set(keep)
        finally:
            self.syncing = False

    def refresh_tasks(self):
        """重建任务下拉（任务名 + 题数），尽量保留当前选择，然后刷新列表。"""
        if not self.api_mode:
            self.refresh()
            return
        current_id = self.task_id()
        counts = api_bank.task_counts()
        labels, mapping, used = ["全部任务"], {"全部任务": ""}, {"全部任务"}
        for tid, name in self._task_names().items():
            label = f"{_short_name(name, 16)} · {counts.get(tid, 0)} 题"
            while label in used:
                label += " "
            used.add(label)
            labels.append(label)
            mapping[label] = tid
        self.task_labels = mapping
        keep = next((lbl for lbl, tid in mapping.items() if tid == current_id),
                    "全部任务")
        self.syncing = True
        try:
            self.cmb_task.set_values(labels, keep=True)
            self.var_task.set(keep)
        finally:
            self.syncing = False
        self.refresh()

    def _today(self) -> int:
        key = "imported_at" if not self.api_mode else "created_at"
        today = time.strftime("%Y-%m-%d")
        return sum(1 for r in api_bank.load(self.mode)
                   if str(r.get(key, "")).startswith(today))

    def on_stage_change(self):
        """学段变化 → 年级下拉只显示对应学段；年级失效则回到「全部」。"""
        stage = AI_STAGE.get(self.var_stage.get(), "")
        self.cmb_grade.set_values(["全部"] + STAGE_GRADE_LABELS.get(stage, []),
                                  keep=False)
        self.refresh()

    def reset_filters(self):
        self.var_q.set("")
        self.var_stage.set("全部")
        self.var_subject.set("全部")
        self.cmb_grade.set_values(["全部"] + STAGE_GRADE_LABELS[""], keep=False)
        self.var_grade.set("全部")
        self.var_diff.set("全部")
        if self.api_mode:
            self.var_task.set("全部任务")
        self.refresh()

    def fit_columns(self, _event=None):
        """题干列吃掉剩余宽度：窗口再窄也不会把最后一列挤出屏幕。"""
        try:
            others = sum(int(self.tree.column(c, "width"))
                         for c in self.cols if c != "stem")
            avail = self.tree.winfo_width() - others - 2
            if avail < 160:
                return
            if abs(int(self.tree.column("stem", "width")) - avail) > 2:
                self.tree.column("stem", width=avail)
        except Exception:  # noqa: BLE001 —— 界面还没就绪时忽略
            pass

    # ---------------- 预览 ----------------
    def _show_hint(self):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        if self.api_mode:
            total = api_bank.stats(api_bank.BANK_API)["total"]
            self.preview.insert("end", "当前没有题目\n\n", ("title",))
            if total:
                self.preview.insert(
                    "end", "这个任务 / 当前筛选下没有题。换个任务，或清空搜索词、点「重置」；"
                           "如果任务还没跑，去「大模型API生成」点「继续」。")
            else:
                self.preview.insert(
                    "end", "API 任务题库还是空的 —— 先去「大模型API生成」跑一个任务，"
                           "生成结果会自动进入这里。")
        else:
            self.preview.insert("end", "「全部题库」还没有题目\n\n", ("title",))
            self.preview.insert(
                "end", "1. 点「导入题库…」把 JSON / TXT / CSV / TSV / Excel 题库"
                       "合并进来；\n"
                       "2. 或点「从 API 题库合并…」，选「全部任务」或某个任务导进来。\n\n"
                       "导入时会按题干 + 选项自动排重；文件里没有学科年级时，"
                       "会按知识点 ID 或名称回查知识地图补齐。")
        self.preview.configure(state="disabled")

    def on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        rec = self.by_id.get(sel[0])
        if not rec:
            return
        names = self._task_names()
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("end", f"{rec.get('topic_name', '')}\n", ("title",))
        self.preview.insert(
            "end", f"题目ID: {rec.get('id', '')}    知识点ID: {rec.get('topic_id', '')}    "
                   f"册: {rec.get('volume_title', '')}\n")
        tid = str(rec.get("task_id") or "")
        if rec.get("origin") == "api":
            src_tid = str(rec.get("origin_task_id") or tid)
            origin = "从 API 题库合并 · " + names.get(src_tid, f"{src_tid}（任务已删除）")
        elif rec.get("origin") == "import":
            origin = "导入 · " + str(rec.get("origin_file") or "未知文件")
        elif tid:
            origin = "API 生成 · " + names.get(tid, f"{tid}（任务已删除）")
        else:
            origin = "未标记来源"
        self.preview.insert("end", f"来源: {origin}\n")
        if rec.get("imported_at"):
            self.preview.insert("end", f"入库时间: {rec.get('imported_at', '')}\n")
        self.preview.insert("end", "\n")
        self.preview.insert("end", f"【题干】{rec.get('stem', '')}\n")
        for i, opt in enumerate(rec.get("options") or []):
            self.preview.insert("end", f"{'ABCD'[i]}. {opt}\n")
        self.preview.insert("end", f"\n【答案】{rec.get('answer', '')}. "
                                   f"{rec.get('answer_text', '')}\n")
        self.preview.insert("end", f"【解析】{rec.get('explanation', '')}\n\n")
        self.preview.insert(
            "end", f"学科: {store.SUBJECT_NAMES.get(rec.get('subject', ''), '')}    "
                   f"单元: {rec.get('chapter', '')}    "
                   f"难度: {_diff_label(rec.get('difficulty', ''))}    "
                   f"认知层级: {rec.get('bloom', '')}\n")
        self.preview.insert(
            "end", f"模型: {rec.get('model', '')}    生成时间: {rec.get('created_at', '')}")
        self.preview.configure(state="disabled")

    # ---------------- 导入 / 合并 ----------------
    def _busy(self, busy: bool, text: str = ""):
        self.root.configure(cursor="watch" if busy else "")
        if text:
            self.lbl_msg.configure(text=text)
        self.root.update_idletasks()

    def msg(self, text: str):
        self.lbl_msg.configure(text=text)

    def import_files(self):
        paths = filedialog.askopenfilenames(
            parent=self.root, title="导入题库到「全部题库」（可多选）",
            initialdir=os.path.expanduser("~/Desktop"),
            filetypes=[("题库文件", "*.json *.csv *.tsv *.txt *.xlsx *.xlsm *.xls"),
                       ("JSON", "*.json"),
                       ("表格", "*.csv *.tsv *.xlsx *.xlsm *.xls"),
                       ("文本", "*.txt"), ("所有文件", "*.*")])
        if not paths:
            return
        self._busy(True, "正在导入并排重…")
        try:
            result = bank_import.import_paths(list(paths))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导入失败", f"{type(exc).__name__}: {exc}")
            return
        finally:
            self._busy(False)
        self.refresh()
        self.app._update_status()
        self.msg(f"导入完成：+{result.added} 题，过滤重复 {result.duplicate} 题")
        messagebox.showinfo("导入结果", result.summary(f"导入 {result.files} 个文件"))

    def merge_dialog(self):
        """「从 API 题库合并…」：全部任务或某一个任务，二选一。"""
        total = api_bank.stats(api_bank.BANK_API)["total"]
        counts = api_bank.task_counts()
        if not total:
            messagebox.showinfo("没有可合并的题",
                                "API 任务题库还是空的，先去「大模型API生成」跑一个任务。")
            return
        names = self._task_names()
        entries = [("", "全部 API 任务", total)]
        for tid in sorted(counts, key=lambda k: names.get(k, k)):
            n = counts[tid]
            if n > 0:
                entries.append((tid, names.get(tid, tid), n))

        win = tk.Toplevel(self.root)
        win.title("从 API 题库合并")
        win.configure(background=LIGHT_BG)
        win.transient(self.root)
        win.resizable(False, False)
        use_light_appearance(win)               # 固定白天外观，不跟随系统深色模式
        ttk.Label(win, text="选择要合并的范围（合并后按题干 + 选项自动排重）：",
                  style="Head.TLabel").pack(anchor="w", padx=14, pady=(12, 6))
        box = tk.Frame(win, background=LIGHT_BG)
        box.pack(fill="both", expand=True, padx=14)
        lb = tk.Listbox(box, height=min(12, max(4, len(entries))), width=54,
                        font=("", 12), activestyle="none",
                        background=LIGHT_PANEL, foreground=LIGHT_TEXT,
                        disabledforeground="#9A9AA0",
                        selectbackground="#2F6FEB", selectforeground="#FFFFFF",
                        borderwidth=0, relief="flat",
                        highlightthickness=1, highlightbackground="#D6D6DA",
                        highlightcolor="#2F6FEB")
        for _tid, label, n in entries:
            lb.insert("end", f"{label} · {n} 题")
        lb.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box, orient="vertical", command=lb.yview)
        sb.pack(side="right", fill="y")
        lb.configure(yscrollcommand=sb.set)
        lb.selection_set(0)
        hint = ttk.Label(win, text="两套库互相独立：合并只是复制一份，之后在任一边删除都不影响另一边。",
                         style="Hint.TLabel")
        hint.pack(anchor="w", padx=14, pady=(6, 0))

        def choose(_event=None):
            sel = lb.curselection()
            if not sel:
                return
            task_id, label, n = entries[sel[0]]
            win.grab_release()
            win.destroy()
            self._merge(task_id, label, n)

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=14, pady=12)
        ttk.Button(btns, text="取消", command=lambda: (win.grab_release(),
                                                       win.destroy())).pack(side="right")
        ttk.Button(btns, text="合并", command=choose).pack(side="right", padx=(0, 8))
        lb.bind("<Double-1>", choose)
        win.bind("<Return>", choose)
        win.bind("<Escape>", lambda _e: (win.grab_release(), win.destroy()))
        win.update_idletasks()
        px, py = self.root.winfo_rootx(), self.root.winfo_rooty()
        pw, ph = self.root.winfo_width(), self.root.winfo_height()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")
        win.grab_set()
        lb.focus_set()
        win.wait_window()

    def merge_current(self):
        """API 页的「合并到全部题库」：当前任务，或全部任务。"""
        task_id = self.task_id()
        if task_id:
            total = len(api_bank.rows_for_task(task_id))
            label = "任务「" + self.var_task.get().split(" · ")[0] + "」"
        else:
            total = api_bank.stats(api_bank.BANK_API)["total"]
            label = "API 题库的全部题"
        if not total:
            messagebox.showinfo("没有可合并的题",
                                "API 任务题库还是空的，先去「大模型API生成」跑一个任务。")
            return
        if not messagebox.askyesno(
                "合并到全部题库",
                f"把{label}（{total} 题）合并进「全部题库」？\n\n"
                "题干和选项都相同的题会自动跳过，不会重复入库。\n"
                "两边互不影响：之后在任一库删除，都不会动另一个库。"):
            return
        self._merge(task_id, label, total)

    def _merge(self, task_id: str, label: str, total: int):
        self._busy(True, "正在合并并排重…")
        try:
            result = bank_import.merge_from_api(task_id)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("合并失败", f"{type(exc).__name__}: {exc}")
            return
        finally:
            self._busy(False)
        self.app.bank_all_page.refresh()
        self.app._update_status()
        self.msg(f"合并完成：+{result.added} 题，跳过重复 {result.duplicate} 题")
        messagebox.showinfo("合并结果", result.summary("合并到全部题库"))

    # ---------------- 导出 ----------------
    EXPORT_KINDS = {
        "json": ("JSON 文件", "*.json"),
        "txt": ("TXT 文本", "*.txt"),
        "csv": ("CSV 文件", "*.csv"),
        "tsv": ("TSV（Excel 可打开）", "*.tsv"),
        "xlsx": ("Excel 工作簿", "*.xlsx"),
    }

    def export(self, fmt: str):
        rows = self.rows
        if not rows:
            messagebox.showinfo("没有题目", "当前筛选结果为空。可先点「刷新」，或换个筛选条件。")
            return
        label, pattern = self.EXPORT_KINDS.get(fmt, ("所有文件", "*.*"))
        bank_name = "API题库" if self.api_mode else "全部题库"
        path = filedialog.asksaveasfilename(
            parent=self.root, title=f"导出{bank_name} {len(rows)} 题",
            initialdir=os.path.expanduser("~/Desktop"),
            initialfile=f"K12{bank_name}_{time.strftime('%Y%m%d')}.{fmt}",
            defaultextension=f".{fmt}",
            filetypes=[(label, pattern), ("所有文件", "*.*")])
        if not path:
            return
        try:
            n = api_bank.export(path, fmt, rows)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导出失败", f"{type(exc).__name__}: {exc}")
            return
        self.msg(f"已导出 {n} 题 → {os.path.basename(path)}")
        self.app.toast(f"{bank_name}已导出：{os.path.basename(path)}")

    def _export_button(self, parent):
        """一个「导出 ▾」按钮，点开选格式，比排五个按钮省地方。"""
        btn = ttk.Button(parent, text="导出 ▾")
        btn.configure(command=lambda: self._export_popup(btn))
        return btn

    def _export_popup(self, btn):
        menu = tk.Menu(self.root, tearoff=0)
        for label, fmt in (("JSON", "json"), ("TXT", "txt"), ("CSV", "csv"),
                           ("Excel (.xlsx)", "xlsx"), ("TSV", "tsv")):
            menu.add_command(label=label,
                             command=lambda f=fmt: self.export(f))
        try:
            menu.tk_popup(btn.winfo_rootx(),
                          btn.winfo_rooty() + btn.winfo_height())
        finally:
            menu.grab_release()
            menu.destroy()

    def export_by_task(self):
        """一个任务一个 JSON，文件名就是任务名（重名自动加序号）。"""
        rows = self.rows
        if not rows:
            messagebox.showinfo("没有题目", "当前筛选结果为空。可先点「刷新」，或先去「大模型API生成」产出题目。")
            return
        folder = filedialog.askdirectory(
            parent=self.root, title="选择导出文件夹（每个任务一个 JSON 文件）",
            initialdir=os.path.expanduser("~/Desktop"))
        if not folder:
            return
        names = self._task_names()
        groups = {}
        for rec in rows:
            groups.setdefault(str(rec.get("task_id") or ""), []).append(rec)
        used = set()
        files = total = failed = 0
        for tid, recs in groups.items():
            base = api_bank.safe_filename(names.get(tid) or "", "")
            if not base:
                base = "未标记任务" if not tid else f"任务-{tid[-6:]}"
            name, i = base, 2
            while name.casefold() in used:
                name = f"{base} ({i})"
                i += 1
            used.add(name.casefold())
            try:
                n = api_bank.export(os.path.join(folder, name + ".json"), "json", recs)
            except Exception:  # noqa: BLE001 —— 一个任务写失败不影响其他任务
                failed += 1
                continue
            files += 1
            total += n
        msg = f"已导出 {files} 个任务 / {total} 题"
        if failed:
            msg += f"（{failed} 个写失败）"
        self.msg(msg)
        self.app.toast(msg + f" → {folder}")

    # ---------------- 删除 / 清空 ----------------
    def delete_selected(self):
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先在列表里选择要删除的题目（可按住 Command 多选）。")
            return
        bank_name = api_bank.BANK_LABELS[self.mode]
        if not messagebox.askyesno(
                "删除题目",
                f"确定从「{bank_name}」删除选中的 {len(sel)} 道题？\n"
                "删除前会自动备份题库文件，另一个库不受影响。"):
            return
        ids = [str((self.by_id.get(i) or {}).get("id") or i) for i in sel]
        removed = api_bank.delete(ids, bank=self.mode)
        if self.api_mode:
            self.refresh_tasks()
        else:
            self.refresh()
        self.app._update_status()
        self.msg(f"已删除 {removed} 题")

    def delete_task(self):
        task_id = self.task_id()
        if not task_id:
            messagebox.showinfo("先选任务",
                                "当前是「全部任务」。请先在任务下拉里选一个具体任务，再点这个按钮。")
            return
        n = len(api_bank.rows_for_task(task_id))
        if not n:
            messagebox.showinfo("提示", "这个任务还没有生成任何题目。")
            return
        if not messagebox.askyesno(
                "删除本任务题目",
                f"从「API 任务题库」删除这个任务的全部 {n} 道题？\n\n"
                "任务本身与进度记录保留（可以继续生成）；「全部题库」里的题不受影响。\n"
                "删除前会自动备份。"):
            return
        removed = api_bank.delete_by_task(task_id)
        self.refresh_tasks()
        self.app._update_status()
        self.msg(f"已删除 {removed} 题")

    def clear(self):
        bank_name = api_bank.BANK_LABELS[self.mode]
        total = api_bank.stats(self.mode)["total"]
        if not total:
            messagebox.showinfo("提示", f"「{bank_name}」已经是空的。")
            return
        extra = ("\n「全部题库」里的题不受影响。" if self.api_mode
                 else "\nAPI 任务题库不受影响。")
        if not messagebox.askyesno(f"清空{bank_name}",
                                   f"将删除「{bank_name}」全部 {total} 道题，并先备份到数据目录。"
                                   f"{extra}\n确定继续？"):
            return
        removed = api_bank.clear(self.mode)
        if self.api_mode:
            self.refresh_tasks()
        else:
            self.refresh()
        self.app._update_status()
        self.msg(f"已清空 {removed} 题（已备份）")


def main():
    root = tk.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
