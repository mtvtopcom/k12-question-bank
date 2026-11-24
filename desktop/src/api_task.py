#!/usr/bin/env python3
"""API 出题任务：进度持久化 + 后台单线程执行 + 断点续跑。

一个任务 = 若干知识点 × 每个知识点 N 题。任务文件随时落盘，因此：
- 暂停 / 停止 / 直接关掉软件，下次都能从已完成题数继续；
- 每个知识点单独记录 done / failed / error，失败不影响其他知识点；
- 已入库题目按 id 去重，重跑不会重复计费产出。
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import api_bank
import api_client
import api_config
import api_questions
import prompt as prompt_mod
import store

STATUS_LABELS = {
    "pending": "待开始",
    "running": "生成中",
    "paused": "已暂停",
    "stopped": "已停止",
    "partial": "部分完成",
    "done": "已完成",
    "error": "出错",
    "empty": "已清空",
}
SYSTEM_PROMPT = (
    "你是一位资深的中小学命题专家，熟悉中国教育部课程标准和《3-6岁儿童学习与发展指南》，"
    "擅长根据知识点命制高质量的选择题。严格按用户要求出题，"
    "只输出 JSON 数组，不要 Markdown 代码块，不要任何解释文字。"
)


# ---------------- 任务文件 ----------------
def _task_path(task_id: str) -> str:
    safe = os.path.basename(str(task_id)).replace(".json", "")
    return os.path.join(api_config.tasks_dir(), f"{safe}.json")


def _task_files() -> List[str]:
    return sorted(glob.glob(os.path.join(api_config.tasks_dir(), "*.json")))


def _read_raw(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) and isinstance(obj.get("items"), list) else None


def _as_n(value: Any) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def auto_name(items: Sequence[Dict[str, Any]], n: int,
              when: Any = None) -> str:
    """自动任务名：年级 + 学科 + 知识点，够认就行（题数在进度里看）。"""
    grades: List[str] = []
    stages: List[str] = []
    subjects: List[str] = []
    topics: List[str] = []
    for it in items or []:
        grade = str(it.get("grade_label") or "")
        if grade and grade not in grades:
            grades.append(grade)
        stage = str(it.get("stage") or "")
        if stage and stage not in stages:
            stages.append(stage)
        subject = store.SUBJECT_NAMES.get(it.get("subject", ""),
                                          str(it.get("subject") or ""))
        if subject and subject not in subjects:
            subjects.append(subject)
        topic = str(it.get("topic_name") or "").strip()
        if topic and topic not in topics:
            topics.append(topic)
    if len(grades) == 1:
        g = grades[0]
    elif not grades:
        g = ""
    elif len(stages) == 1:
        g = f"{grades[0]}~{grades[-1]}"
    else:
        g = f"{len(grades)}个年级"
    if len(subjects) <= 2:
        s = "/".join(subjects)
    else:
        s = f"{len(subjects)}科"
    head = " ".join(x for x in (g, s) if x)
    if not topics:
        tail = f"{len(items or [])}个知识点"
    elif len(topics) == 1:
        tail = topics[0]
    else:
        tail = f"{topics[0]} 等 {len(topics)} 点"
    return " · ".join(x for x in (head, tail) if x)


def _name_taken(name: str, exclude_id: str = "") -> bool:
    for path in _task_files():
        task = _read_raw(path)
        if not task or task.get("id") == exclude_id:
            continue
        if str(task.get("name") or "") == name:
            return True
    return False


def unique_name(name: str, exclude_id: str = "") -> str:
    """重名时自动加 (2)/(3)，避免任务下拉和导出文件名打架。"""
    base = " ".join(str(name or "").split())[:40].strip()
    if not base:
        base = "未命名任务"
    candidate = base
    i = 2
    while _name_taken(candidate, exclude_id):
        candidate = f"{base} ({i})"
        i += 1
    return candidate


def _task_name(task: Dict[str, Any]) -> str:
    name = str(task.get("name") or "").strip()
    if name:
        return name
    return auto_name(task.get("items") or [], _as_n(task.get("n_per_point")),
                     task.get("created_at"))


def name_of(task: Dict[str, Any]) -> str:
    """对外暴露任务名（没有名字的旧任务现算一个）。"""
    return _task_name(task or {})


# ---- 每个任务的独立 JSON 存档 ----
def archive_path(task: Dict[str, Any]) -> str:
    name = api_bank.safe_filename(_task_name(task),
                                  str(task.get("id") or "任务"))
    return os.path.join(api_config.exports_dir(), name + ".json")


def write_archive(task: Dict[str, Any],
                  records: Optional[Sequence[Dict[str, Any]]] = None) -> str:
    """把该任务已生成的题目写成 JSON（文件名 = 任务名），返回路径。"""
    if records is None:
        records = api_bank.rows_for_task(task.get("id", ""))
    path = archive_path(task)
    tmp = path + ".tmp"
    api_bank.export(tmp, "json", list(records))
    os.replace(tmp, path)          # 写临时文件再替换，避免中途失败留下半个 JSON
    return path


def remove_archive(task: Dict[str, Any]) -> None:
    path = archive_path(task)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---------------- 任务增删改 ----------------
def remove_items(task: Dict[str, Any], indexes: Sequence[Any]) -> int:
    """从任务里移除知识点行（已入库的题目不动）；全删完标为「已清空」。"""
    items = task.get("items") or []
    picked = set()
    for raw in indexes:
        try:
            idx = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(items):
            picked.add(idx)
    for idx in sorted(picked, reverse=True):
        items.pop(idx)
    recalc(task)
    if not items:
        task["status"] = "empty"
        task["error"] = ""
    elif (task.get("status") in ("paused", "stopped", "partial", "pending")
          and task["totals"]["done"] >= task["totals"]["need"]):
        task["status"] = "done"      # 删掉没生成的行后，剩下的其实已经齐了
    save_task(task)
    return len(picked)


def rename_task(task: Dict[str, Any], name: str) -> str:
    """改名并把已有 JSON 存档一起改名。"""
    old_path = archive_path(task)
    task["name"] = unique_name(name, task.get("id", ""))
    save_task(task)
    new_path = archive_path(task)
    if old_path != new_path and os.path.exists(old_path):
        try:
            os.replace(old_path, new_path)
        except OSError:
            try:
                os.remove(old_path)
            except OSError:
                pass
    return task["name"]


def delete_task(task_id: str) -> bool:
    """只删任务进度文件；题库里的题目与导出目录里的 JSON 由调用方决定。"""
    path = _task_path(task_id)
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except OSError:
        return False
    return False


def normalize_interrupted() -> int:
    """启动时把上次残留的 running 改成 paused（软件已退出，不可能还在跑）。"""
    fixed = 0
    for path in _task_files():
        task = _read_raw(path)
        if not task or task.get("status") != "running":
            continue
        task["status"] = "paused"
        for item in task.get("items") or []:
            if item.get("status") == "running":
                item["status"] = "paused"
        save_task(task)
        fixed += 1
    return fixed


_OLD_AUTO = re.compile(r"^\d{2}-\d{2} \d{2}:\d{2} · .* · \d+点×\d+题$")


def normalize_names() -> int:
    """把旧版自动名（时间 · 年级学科 · N点×M题）收短成「年级学科 · 知识点」。"""
    changed = 0
    for path in _task_files():
        task = _read_raw(path)
        if not task:
            continue
        name = str(task.get("name") or "")
        if name and not _OLD_AUTO.match(name):
            continue           # 用户自己起的名字不动
        new = auto_name(task.get("items") or [], _as_n(task.get("n_per_point")),
                        task.get("created_at"))
        if not new or new == name:
            continue
        rename_task(task, new)
        changed += 1
    return changed


def new_task(pairs: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]], n: int,
             ratios: Dict[Any, int], profile: Dict[str, Any],
             task_id: Optional[str] = None,
             name: Optional[str] = None) -> Dict[str, Any]:
    """pairs: [(知识点, 册元数据)]；n: 每个知识点题数。"""
    n = max(1, min(100, int(n or 1)))
    items = []
    for kp, vol in pairs:
        items.append({
            "topic_id": kp.get("id", ""),
            "topic_name": kp.get("name", ""),
            "volume_id": vol.get("volume_id", ""),
            "volume_title": vol.get("title", ""),
            "stage": vol.get("stage", ""),
            "subject": vol.get("subject", ""),
            "grade": vol.get("grade", ""),
            "grade_label": store._grade_label(vol.get("grade", ""))
                           + store.TERM_NAMES.get(vol.get("term", ""), ""),
            "need": n,
            "done": 0,
            "failed": 0,
            "status": "pending",
            "error": "",
        })
    now = dt.datetime.now().isoformat(timespec="seconds")
    task = {
        "schema": 1,
        "id": task_id or (dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                          + "-" + uuid.uuid4().hex[:4]),
        "name": "",
        "created_at": now,
        "updated_at": now,
        "status": "pending",
        "provider": profile.get("provider", "openai"),
        "provider_label": api_config.PROVIDER_LABELS.get(
            profile.get("provider", "openai"), profile.get("provider", "")),
        "model": profile.get("model", ""),
        "base_url": profile.get("base_url", ""),
        "n_per_point": n,
        "batch": int(profile.get("batch") or 6),
        "ratios": {str(k): int(v) for k, v in (ratios or {}).items()},
        "items": items,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "requests": 0},
        "error": "",
    }
    task["name"] = unique_name(name or auto_name(items, n, now))
    recalc(task)
    return task


def recalc(task: Dict[str, Any]) -> Dict[str, Any]:
    items = task.get("items") or []
    need = sum(int(it.get("need", 0)) for it in items)
    done = sum(min(int(it.get("done", 0)), int(it.get("need", 0))) for it in items)
    failed = sum(1 for it in items if it.get("status") == "error")
    topics_done = sum(1 for it in items
                      if int(it.get("done", 0)) >= int(it.get("need", 0)))
    task["totals"] = {"need": need, "done": done, "failed": failed,
                      "topics": len(items), "topics_done": topics_done}
    return task


def save_task(task: Dict[str, Any]) -> None:
    task["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    recalc(task)
    path = _task_path(task["id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    payload = json.dumps(task, ensure_ascii=False, indent=2) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_task(task_id: str) -> Optional[Dict[str, Any]]:
    path = _task_path(task_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            task = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(task, dict) or not isinstance(task.get("items"), list):
        return None
    if not task.get("name"):
        task["name"] = _task_name(task)          # 旧任务补一个自动名
    recalc(task)
    return task


def list_tasks(limit: int = 40) -> List[Dict[str, Any]]:
    """按更新时间倒序列出任务摘要。"""
    out = []
    for path in glob.glob(os.path.join(api_config.tasks_dir(), "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                task = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(task, dict) or not isinstance(task.get("items"), list):
            continue
        recalc(task)
        out.append({
            "id": task.get("id", ""),
            "name": task.get("name") or _task_name(task),
            "created_at": task.get("created_at", ""),
            "updated_at": task.get("updated_at", ""),
            "status": task.get("status", "pending"),
            "total": task["totals"]["need"],
            "done": task["totals"]["done"],
            "topics": task["totals"]["topics"],
            "topics_done": task["totals"]["topics_done"],
            "model": task.get("model", ""),
        })
    out.sort(key=lambda t: t.get("updated_at") or "", reverse=True)
    return out[:limit] if limit else out


def task_label(task: Dict[str, Any]) -> str:
    recalc(task)
    t = task["totals"]
    return (f"{_task_name(task)} · {t['done']}/{t['need']} 题 · "
            f"{STATUS_LABELS.get(task.get('status', ''), task.get('status', ''))}")


def resume_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """把任务恢复为可继续状态：崩溃遗留的 running 视为暂停，失败项重试。"""
    if task.get("status") == "running":
        task["status"] = "paused"
    for item in task.get("items") or []:
        if int(item.get("done", 0)) >= int(item.get("need", 0)):
            item["status"] = "done"
        else:
            item["status"] = "pending"
            item["failed"] = 0
            item["error"] = ""
    task["error"] = ""
    recalc(task)
    return task


# ---------------- 执行器 ----------------
class _Aborted(Exception):
    """暂停 / 停止时主动放弃当前在途请求（不等它返回）。"""


class Runner(threading.Thread):
    """后台单线程任务执行器；通过 emit 回调把事件交给界面线程。"""

    def __init__(self, task: Dict[str, Any], profile: Dict[str, Any],
                 emit: Callable[[Dict[str, Any]], None]):
        super().__init__(daemon=True, name=f"api-task-{task.get('id', '')}")
        self.task = task
        self.profile = dict(profile or {})
        self.emit_raw = emit
        self._stop_evt = threading.Event()
        self._pause_evt = threading.Event()
        self.client: Optional[api_client._BaseClient] = None
        self.index: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
        self.known_ids: set = set()
        self.topic_seen: Dict[str, set] = {}
        self.records: List[Dict[str, Any]] = []   # 本任务已生成的题目（写独立 JSON 用）

    # ---- 控制 ----
    def pause(self) -> None:
        self._pause_evt.set()

    def stop(self) -> None:
        self._stop_evt.set()

    @property
    def paused(self) -> bool:
        return self._pause_evt.is_set() and not self._stop_evt.is_set()

    def _emit(self, **payload: Any) -> None:
        try:
            self.emit_raw(payload)
        except Exception:  # noqa: BLE001
            pass

    def _log(self, text: str) -> None:
        self._emit(type="log", text=text)

    def _archive(self, announce: bool = False) -> None:
        """把本任务已生成的题目写成独立 JSON（文件名 = 任务名）。"""
        try:
            path = write_archive(self.task, self.records)
        except Exception as exc:  # noqa: BLE001 —— 存档失败不能影响出题
            self._log(f"  任务 JSON 存档失败：{type(exc).__name__}: {exc}")
            return
        if announce:
            self._log(f"本题任务的 JSON 存档：{path}")

    def _cancelled(self) -> bool:
        return self._stop_evt.is_set() or self._pause_evt.is_set()

    def _sleep(self, seconds: float) -> None:
        """退避等待，暂停/停止能立刻打断，不必等满。"""
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline:
            if self._cancelled():
                return
            time.sleep(min(0.2, max(0.0, deadline - time.time())))

    def _chat(self, system: str, user: str, json_mode: bool = True) -> Any:
        """发请求；把等待切成 0.25 秒一片，暂停/停止时立即放弃在途请求。

        等待期间每 5 秒发一次心跳，界面据此显示「已等待 N 秒」，
        避免长时间请求看起来像卡死。
        """
        box: Dict[str, Any] = {}

        def work():
            try:
                box["reply"] = self.client.chat(
                    system, user, json_mode=json_mode, cancel=self._cancelled)
            except BaseException as exc:  # noqa: BLE001 —— 原样抛回调用方
                box["error"] = exc

        th = threading.Thread(target=work, daemon=True, name="api-request")
        th.start()
        started = time.time()
        beat = 0
        while th.is_alive():
            if self._cancelled():
                raise _Aborted()
            th.join(0.25)
            elapsed = time.time() - started
            if th.is_alive() and int(elapsed) // 5 > beat:
                beat = int(elapsed) // 5
                self._emit(type="beat", elapsed=int(elapsed),
                           timeout=int(getattr(self.client, "timeout", 90) or 90))
        if "error" in box:
            err = box["error"]
            # 放弃请求时客户端会抛 cancelled，这种情况要算「被暂停」而不是失败
            if isinstance(err, api_client.LLMError) and err.kind == "cancelled":
                raise _Aborted()
            raise err
        return box.get("reply")

    def _check_timeout(self) -> None:
        """输出上限放得很大时，90 秒超时必然掐断长回答；本次运行先放宽并说明。"""
        client = self.client
        if client is None:
            return
        limit = int(getattr(client, "max_tokens", 8192) or 8192)
        # 按 200 tokens/秒的保守下限估算生成耗时，再留 60 秒给排队与传输
        need = int(min(240, max(90, 60 + limit / 200)))
        if client.timeout < need:
            old = int(client.timeout)
            client.timeout = need
            self._log(f"超时 {old} 秒对当前输出上限（{limit} tokens）偏短："
                      f"本次运行按 {need} 秒等待")
            self._log("  可在上方「超时(秒)」改成同样数值后点「保存配置」，下次就不用再调")

    # ---- 主循环 ----
    def run(self) -> None:  # noqa: C901
        task = self.task
        final_status = task.get("status", "pending")
        try:
            recalc(task)
            task["status"] = "running"
            task["error"] = ""
            save_task(task)
            self._emit(type="status", status="running")

            self._log(f"任务开始：{task['totals']['topics']} 个知识点 / "
                      f"共 {task['totals']['need']} 题")
            self.client = api_client.build_client(self.profile)
            self.client.log = self._log          # 重试过程写进界面日志
            label = api_config.PROVIDER_LABELS.get(self.profile.get("provider", ""), "")
            self._log(f"接口：{label} · {self.profile.get('model', '')}")
            self._check_timeout()
            if (task.get("model") and task.get("model") != self.profile.get("model")) or \
                    (task.get("provider") and task.get("provider") != self.profile.get("provider")):
                self._log(f"配置已变更：原 {task.get('provider_label')} / {task.get('model')} "
                          f"→ 现 {label} / {self.profile.get('model')}")
            task["provider"] = self.profile.get("provider", "openai")
            task["provider_label"] = label
            task["model"] = self.profile.get("model", "")
            task["base_url"] = self.profile.get("base_url", "")
            task["batch"] = int(self.profile.get("batch") or 6)

            self.index = self._build_index()
            self.known_ids = api_bank.existing_ids(force=True)
            self._log(f"题库现有 {len(self.known_ids)} 题，重复题目会自动跳过")
            save_task(task)
            self.records = api_bank.rows_for_task(task.get("id", ""))
            if self.records:
                self._log(f"本任务已有 {len(self.records)} 题，继续写入同一份任务 JSON")
            self._archive(announce=bool(self.records))

            for idx, item in enumerate(task["items"]):
                if self._cancelled():
                    break
                if int(item.get("done", 0)) >= int(item.get("need", 0)):
                    item["status"] = "done"
                    continue
                self._run_item(idx, item)
                recalc(task)
                save_task(task)

        except api_client.LLMError as exc:
            task["status"] = "error"
            task["error"] = exc.friendly()
            self._log(f"任务中止：{exc.friendly()}")
            self._emit(type="error", text=exc.friendly())
        except Exception as exc:  # noqa: BLE001
            task["status"] = "error"
            task["error"] = f"{type(exc).__name__}: {exc}"
            self._log(f"任务异常：{task['error']}")
            self._emit(type="error", text=task["error"])
        finally:
            recalc(task)
            totals = task["totals"]
            if task.get("status") == "error":
                final_status = "error"
            elif self._stop_evt.is_set():
                final_status = "stopped"
            elif self._pause_evt.is_set() and totals["done"] < totals["need"]:
                final_status = "paused"
            elif totals["done"] >= totals["need"]:
                final_status = "done"
            else:
                final_status = "partial"
            task["status"] = final_status
            save_task(task)
            if self.records:
                self._archive()
            label = STATUS_LABELS.get(final_status, final_status)
            if final_status in ("paused", "stopped"):
                msg = f"{label} · {totals['done']}/{totals['need']} 题（可点「继续」接着生成）"
            else:
                msg = f"任务结束：{label} · {totals['done']}/{totals['need']} 题"
            if totals["failed"]:
                msg += f" · {totals['failed']} 个知识点出错"
            self._log(msg)
            self._emit(type="status", status=final_status)
            self._emit(type="progress", done=totals["done"], need=totals["need"])
            self._emit(type="finished", status=final_status, task_id=task.get("id", ""),
                       done=totals["done"], need=totals["need"],
                       failed=totals["failed"], error=task.get("error", ""))

    def _build_index(self) -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
        index = {}
        for vol in store.load_all_volumes():
            for kp in vol.get("knowledge_points") or []:
                index[kp.get("id", "")] = (kp, vol)
        return index

    def _run_item(self, idx: int, item: Dict[str, Any]) -> None:
        task = self.task
        kp, vol = self.index.get(item["topic_id"], (None, None))
        if not kp:
            item["status"] = "error"
            item["error"] = "知识点已不存在（可能被删除）"
            self._log(f"[{idx + 1}/{len(task['items'])}] {item['topic_name']}：{item['error']}")
            self._emit(type="item", index=idx, done=item["done"], need=item["need"],
                       status="error", error=item["error"])
            return

        need = int(item["need"])
        batch = max(1, min(10, int(self.profile.get("batch") or 6)))
        task["batch"] = batch
        ratios = {int(k): int(v) for k, v in (task.get("ratios") or {}).items()}
        item["status"] = "running"
        item["error"] = ""
        self._emit(type="item", index=idx, done=item["done"], need=need, status="running")
        self._log(f"[{idx + 1}/{len(task['items'])}] {item['grade_label']} · "
                  f"{item['topic_name']}（{item['done']}/{need} 题）")

        empty_rounds = 0
        max_requests = need * 2 + 10
        requests = 0
        while int(item["done"]) < need:
            if self._cancelled():
                break
            if requests >= max_requests:
                item["error"] = "多次请求仍未获得足够合格题目"
                item["status"] = "error"
                break
            requests += 1
            remaining = need - int(item["done"])
            n_req = min(batch, remaining)
            try:
                text = prompt_mod.build_prompt(kp, vol, n_req,
                                               difficulty_mix=ratios,
                                               output_format="json")
                self._log(f"  请求 {n_req} 题（第 {requests} 次，"
                          f"剩余 {remaining}）…")
                reply = self._chat(SYSTEM_PROMPT, text, json_mode=True) if self.client \
                    else None
            except _Aborted:
                # 暂停/停止时主动放弃在途请求，不再等它跑完
                item["status"] = "paused" if self._pause_evt.is_set() else "pending"
                self._emit(type="item", index=idx, done=item["done"],
                           need=need, status=item["status"])
                self._log("  已中断当前请求，这一批没有计入进度，下次会重新请求")
                return
            except api_client.LLMError as exc:
                item["failed"] = int(item.get("failed", 0)) + 1
                if exc.kind in ("auth", "config", "not_found", "ssl"):
                    item["status"] = "error"
                    item["error"] = exc.friendly()
                    self._emit(type="item", index=idx, done=item["done"],
                               need=need, status="error", error=item["error"])
                    raise
                self._log(f"  请求失败：{exc.friendly()}")
                empty_rounds += 1
                if empty_rounds >= 3:
                    item["status"] = "error"
                    item["error"] = exc.friendly()
                    break
                self._sleep(min(20, 3 * empty_rounds))
                continue

            if reply is None:
                item["status"] = "error"
                item["error"] = "客户端未初始化"
                break
            usage = task.setdefault("usage", {"prompt_tokens": 0,
                                              "completion_tokens": 0, "requests": 0})
            usage["requests"] = int(usage.get("requests", 0)) + 1
            usage["prompt_tokens"] = int(usage.get("prompt_tokens", 0)) + reply.prompt_tokens
            usage["completion_tokens"] = int(usage.get("completion_tokens", 0)) + reply.completion_tokens
            self._emit(type="usage", usage=dict(usage))
            truncated = reply.finish_reason in ("length", "max_tokens", "MAX_TOKENS")
            try:
                items = api_questions.parse_questions(reply.text)
            except api_questions.ParseError as exc:
                head = (reply.text or "").strip()
                if truncated:
                    # 思考型模型常把额度全花在推理上，输出里连 JSON 的起始括号都没有；
                    # 或者缩到 1 题还被截断。这两种都是「额度不够」，缩批量没用。
                    if "[" not in head and "{" not in head:
                        reason = "模型把输出额度用在了思考过程上，没有产出题目 JSON"
                        self._log("  输出被思考过程占满：返回的是推理文字，没有题目 JSON")
                    elif n_req <= 1:
                        reason = "单道题就被长度上限截断，题目 JSON 不完整"
                        self._log("  单题输出仍被截断：题目 JSON 不完整")
                    else:
                        batch = max(1, n_req // 2)
                        self._log(f"  输出被长度上限截断：本次请求降为 {batch} 题重试")
                        continue
                    item["failed"] = int(item.get("failed", 0)) + 1
                    item["status"] = "error"
                    item["error"] = reason
                    self._log("  已中止任务（换下一题也是同样结果），按提示调整后再点「继续」")
                    raise api_client.LLMError(
                        reason, kind="config",
                        hint="把「最大输出」调到 16000 以上，或换用非思考模型，"
                             "然后点「继续」接着生成")
                item["failed"] = int(item.get("failed", 0)) + 1
                empty_rounds += 1
                self._log(f"  解析失败：{exc}")
                if empty_rounds >= 3:
                    item["status"] = "error"
                    item["error"] = f"模型输出无法解析：{exc}"
                    break
                self._sleep(2)
                continue

            records, errors = api_questions.normalize_batch(
                items, kp, vol,
                default_difficulty=int(kp.get("difficulty", 2) or 2),
                provider=self.profile.get("provider", ""),
                model=self.profile.get("model", ""),
                task_id=task.get("id", ""))
            remaining = need - int(item["done"])
            records = records[:remaining]
            fresh = []
            seen = self.topic_seen.setdefault(item["topic_id"], set())
            for rec in records:
                if rec["id"] in self.known_ids or rec["id"] in seen:
                    continue
                fresh.append(rec)
            dropped = len(records) - len(fresh)

            if not fresh:
                item["failed"] = int(item.get("failed", 0)) + 1
                empty_rounds += 1
                reason = "；".join(errors[:2]) or f"{dropped} 题重复"
                self._log(f"  没有新增题目（{reason}）")
                self._emit(type="item", index=idx, done=item["done"],
                           need=need, status="running")
                if empty_rounds >= 3:
                    item["status"] = "error"
                    item["error"] = f"连续 3 次没有获得合格题目（{reason}）"
                    break
                self._sleep(2)
                continue

            written = api_bank.append(fresh, known=self.known_ids)
            if not written:
                empty_rounds += 1
                self._log("  题目均已存在，跳过")
                if empty_rounds >= 3:
                    item["status"] = "error"
                    item["error"] = "题目重复，未能新增"
                    break
                continue

            empty_rounds = 0
            self.known_ids.update(r["id"] for r in fresh)
            seen.update(r["id"] for r in fresh)
            self.records.extend(fresh[:written])
            self._archive()
            item["done"] = int(item["done"]) + written
            item["status"] = "done" if item["done"] >= need else "running"
            if item["status"] == "done":
                item["error"] = ""
            recalc(task)
            save_task(task)
            note = f"，丢弃 {len(errors)} 题（{errors[0]}）" if errors else ""
            self._log(f"  入库 {written} 题，累计 {item['done']}/{need}{note}")
            self._emit(type="question", count=written)
            self._emit(type="item", index=idx, done=item["done"], need=need,
                       status=item["status"])
            self._emit(type="progress", done=task["totals"]["done"],
                       need=task["totals"]["need"])
            if item["done"] < need and not self._stop_evt.is_set() and not self._pause_evt.is_set():
                self._sleep(float(self.profile.get("interval") or 0))

        if int(item["done"]) >= need:
            item["status"] = "done"
            item["error"] = ""
        elif item["status"] == "running":
            item["status"] = "paused" if self._pause_evt.is_set() else "pending"
        self._emit(type="item", index=idx, done=item["done"], need=need,
                   status=item["status"], error=item.get("error", ""))
