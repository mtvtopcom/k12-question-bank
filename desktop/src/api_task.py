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
import threading
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


def new_task(pairs: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]], n: int,
             ratios: Dict[Any, int], profile: Dict[str, Any],
             task_id: Optional[str] = None) -> Dict[str, Any]:
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
    if not isinstance(task, dict) or not task.get("items"):
        return None
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
        if not isinstance(task, dict) or not task.get("items"):
            continue
        recalc(task)
        out.append({
            "id": task.get("id", ""),
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
    created = str(task.get("created_at", ""))[5:16].replace("T", " ")
    return (f"{created} · {t['done']}/{t['need']} 题 · "
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

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._stop_evt.wait(seconds)

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
            label = api_config.PROVIDER_LABELS.get(self.profile.get("provider", ""), "")
            self._log(f"接口：{label} · {self.profile.get('model', '')}")
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

            for idx, item in enumerate(task["items"]):
                if self._stop_evt.is_set() or self._pause_evt.is_set():
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
            msg = (f"任务结束：{STATUS_LABELS.get(final_status, final_status)} · "
                   f"{totals['done']}/{totals['need']} 题")
            if totals["failed"]:
                msg += f" · {totals['failed']} 个知识点出错"
            self._log(msg)
            self._emit(type="status", status=final_status)
            self._emit(type="progress", done=totals["done"], need=totals["need"])
            self._emit(type="finished", status=final_status,
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
            if self._stop_evt.is_set() or self._pause_evt.is_set():
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
                reply = self.client.chat(SYSTEM_PROMPT, text, json_mode=True) if self.client \
                    else None
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
            if reply.finish_reason in ("length", "max_tokens", "MAX_TOKENS"):
                self._log("  提示：输出达到长度上限，建议调大「最大输出 tokens」或减少每次请求题数")

            try:
                items = api_questions.parse_questions(reply.text)
            except api_questions.ParseError as exc:
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
