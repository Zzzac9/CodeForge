"""多步 agent 运行时使用的轻量工作记忆。

session history 负责保存完整事件流；这个模块只保存更小的一层工作集：
当前任务摘要、最近接触的文件、文件短摘要，以及少量跨轮笔记。
这样下一轮 prompt 还能接上上一轮，但不会被整段历史塞满。
"""

import hashlib
from datetime import datetime
import re
from pathlib import Path

from .workspace import clip, now

# 工作记忆容量上限：最近接触文件、短期笔记、文件摘要都只保留少量关键项
WORKING_FILE_LIMIT = 8
EPISODIC_NOTE_LIMIT = 12
FILE_SUMMARY_LIMIT = 6

# durable memory 的默认主题配置，用来把长期信息按类型分门别类落盘
DURABLE_TOPIC_DEFAULTS = {
    "project-conventions": {
        "title": "Project Conventions",
        "summary": "Stable repository conventions.",
        "tags": ["convention"],
    },
    "key-decisions": {
        "title": "Key Decisions",
        "summary": "Long-lived decisions and rationale anchors.",
        "tags": ["decision"],
    },
    "dependency-facts": {
        "title": "Dependency Facts",
        "summary": "Stable dependency and environment facts.",
        "tags": ["dependency"],
    },
    "user-preferences": {
        "title": "User Preferences",
        "summary": "Stable user preferences.",
        "tags": ["preference"],
    },
}


def default_memory_state():
    # 用一个小而结构化的状态，而不是一大段自由文本摘要。
    return {
        "working": {
            "task_summary": "",
            "recent_files": [],
        },
        "episodic_notes": [],
        "file_summaries": {},
        "task": "",
        "files": [],
        "notes": [],
        "next_note_index": 0,
    }
19

class DurableMemoryStore:
    def __init__(self, root):
        # root 是长期记忆目录，下面会放 MEMORY.md 和 topics/
        self.root = Path(root)
        self.index_path = self.root / "MEMORY.md"
        self.topics_dir = self.root / "topics"

    def topic_slugs(self):
        # 返回当前索引里所有 topic 的 slug
        return [topic["topic"] for topic in self.load_index()]

    def load_index(self):
        # 读取 MEMORY.md 索引；不存在就说明还没有长期记忆
        if not self.index_path.exists():
            return []
        lines = self.index_path.read_text(encoding="utf-8").splitlines()
        topics = []
        current = None

        for raw in lines:
            line = raw.strip()
            # 匹配形如：- [topic](topics/topic.md): Title
            match = re.match(r"- \[([^\]]+)\]\([^)]+\):\s*(.+)", line)
            if match:
                current = {
                    "topic": match.group(1).strip(),
                    "title": match.group(2).strip(),
                    "summary": "",
                    "tags": [],
                }
                topics.append(current)
                continue

            # 没有进入任何 topic 前，summary/tags 行都忽略
            if current is None:
                continue

            # 读取当前 topic 的 summary
            summary_match = re.match(r"- summary:\s*(.+)", line)
            if summary_match:
                current["summary"] = summary_match.group(1).strip()
                continue

            # 读取当前 topic 的 tags
            tags_match = re.match(r"- tags:\s*(.+)", line)
            if tags_match:
                current["tags"] = [tag.strip() for tag in tags_match.group(1).split(",") if tag.strip()]
        return topics

    def load_topic_notes(self, topic):
        # 读取某个 topic 对应的 notes 文件
        path = self.topics_dir / f"{topic}.md"
        if not path.exists():
            return []

        lines = path.read_text(encoding="utf-8").splitlines()
        notes = []
        capture = False
        updated_at = ""
        tags = []

        for raw in lines:
            line = raw.strip()
            if line.startswith("- tags:"):
                # topic 文件里的 tags 会作为该 topic 下每条 note 的标签
                tags = [tag.strip() for tag in line.split(":", 1)[1].split(",") if tag.strip()]
            elif line.startswith("- updated_at:"):
                updated_at = line.split(":", 1)[1].strip()
            elif line == "## Notes":
                # 从 ## Notes 后面开始真正收集笔记
                capture = True
            elif capture and line.startswith("- "):
                notes.append(
                    {
                        "text": line[2:].strip(),
                        "tags": tags,
                        "source": topic,
                        "created_at": updated_at or now(),
                        "kind": "durable",
                    }
                )
        return notes

    @staticmethod
    def _subject_key(text):
        # 从一句记忆里抽取“主语”，用于判断新旧记忆是不是在说同一件事
        text = str(text).strip()
        patterns = (
            r"^(.+?)\s+is\s+.+$",
            r"^(.+?)\s+are\s+.+$",
            r"^(.+?)\s+uses?\s+.+$",
            r"^(.+?)\s+should\s+.+$",
            r"^(.+?)是.+$",
            r"^(.+?)使用.+$",
        )
        for pattern in patterns:
            match = re.match(pattern, text, re.I)
            if match:
                subject = " ".join(_tokenize(match.group(1)))
                return subject or None
        return None

    def retrieval_candidates(self, query, limit=3):
        # 根据 query 从长期记忆里召回最相关的 notes
        query_tokens = _tokenize(query)
        ranked = []

        for topic in self.load_index():
            notes = self.load_topic_notes(topic["topic"])
            for note in notes:
                note_tags = {tag.lower() for tag in note.get("tags", [])}
                note_tokens = _tokenize(note.get("text", "")) | _tokenize(topic.get("title", "")) | note_tags

                # 排序依据：标签是否命中、关键词重叠数量、时间新旧
                exact_tag_match = int(bool(query_tokens & note_tags))
                keyword_overlap = len(query_tokens & note_tokens)
                if exact_tag_match == 0 and keyword_overlap == 0:
                    continue

                recency = _parse_timestamp(note.get("created_at"))
                ranked.append(((exact_tag_match, keyword_overlap, recency), note))

        ranked.sort(key=lambda item: item[0], reverse=True)
        return [note for _, note in ranked[:limit]]

    def _write_index(self, topics):
        # 重写 MEMORY.md 索引文件
        self.root.mkdir(parents=True, exist_ok=True)
        self.topics_dir.mkdir(parents=True, exist_ok=True)

        lines = ["# Durable Memory Index", ""]
        for topic in topics:
            lines.append(f"- [{topic['topic']}](topics/{topic['topic']}.md): {topic['title']}")
            lines.append(f"  - summary: {topic['summary']}")
            lines.append(f"  - tags: {', '.join(topic['tags'])}")

        self.index_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _write_topic(self, topic, notes):
        # 重写某个 topic 的 markdown 文件
        self.topics_dir.mkdir(parents=True, exist_ok=True)
        meta = DURABLE_TOPIC_DEFAULTS[topic]

        lines = [
            f"# {meta['title']}",
            "",
            f"- topic: {topic}",
            f"- summary: {meta['summary']}",
            f"- tags: {', '.join(meta['tags'])}",
            f"- updated_at: {now()}",
            "",
            "## Notes",
        ]
        for note in notes:
            lines.append(f"- {note}")

        (self.topics_dir / f"{topic}.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def promote(self, promotions):
        # 把本轮提取出来的长期记忆写入 durable memory
        if not promotions:
            return [], []

        # 先加载已有 topic 和已有 note
        topics = {topic["topic"]: topic for topic in self.load_index()}
        topic_notes = {slug: [note["text"] for note in self.load_topic_notes(slug)] for slug in topics}
        results = []
        superseded = []

        for topic, note_text in promotions:
            meta = DURABLE_TOPIC_DEFAULTS[topic]

            # 如果 topic 不存在，就按默认配置创建
            topics.setdefault(
                topic,
                {
                    "topic": topic,
                    "title": meta["title"],
                    "summary": meta["summary"],
                    "tags": list(meta["tags"]),
                },
            )

            existing = topic_notes.setdefault(topic, [])
            if note_text in existing:
                continue

            # 如果新旧 note 主语相同，就用新 note 替换旧 note
            new_subject = self._subject_key(note_text)
            replaced = False
            if new_subject:
                for index, old_text in enumerate(list(existing)):
                    if self._subject_key(old_text) == new_subject:
                        superseded.append(f"{topic}: {old_text} -> {note_text}")
                        existing[index] = note_text
                        replaced = True
                        break

            # 如果没有命中可替换项，就追加一条新 note
            if not replaced:
                existing.append(note_text)

            results.append(f"{topic}: {note_text}")

        # 最后统一重写索引和各 topic 文件
        self._write_index([topics[slug] for slug in sorted(topics)])
        for topic, notes in topic_notes.items():
            self._write_topic(topic, notes)

        return results, superseded


def _ensure_list(value):
    # 把输入统一规范成 list，方便后续处理 tags、notes 这类字段
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if value in (None, ""):
        return []
    return [value]

def _dedupe_preserve_order(items):
    # 去重但保留原始顺序，适合处理 recent_files、tags 这类列表
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def resolve_workspace_path(raw_path, workspace_root=None):
    # 把输入路径转成 Path；如果没有 workspace_root，就不做工作区边界校验
    path = Path(str(raw_path))
    if workspace_root is None:
        return path

    # 把相对路径解释为 workspace_root 下的路径
    root = Path(workspace_root).resolve()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve()

    try:
        # 确认最终路径仍然在工作区内，防止 ../ 逃逸到仓库外
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def canonicalize_path(raw_path, workspace_root=None):
    # 把路径规范成稳定的 posix 字符串，方便记忆里统一存储和比较
    resolved = resolve_workspace_path(raw_path, workspace_root)
    if resolved is None:
        return Path(str(raw_path)).as_posix()
    if workspace_root is None:
        return Path(str(raw_path)).as_posix()
    root = Path(workspace_root).resolve()
    return resolved.relative_to(root).as_posix()


def file_freshness(raw_path, workspace_root=None):
    # 用文件内容的 sha256 表示新鲜度；文件变化时 hash 也会变化
    resolved = resolve_workspace_path(raw_path, workspace_root)
    if resolved is None or not resolved.exists() or not resolved.is_file():
        return None
    return hashlib.sha256(resolved.read_bytes()).hexdigest()


def _tokenize(text):
    # 简单分词：提取英文、数字、下划线 token，并统一转小写
    return {token.lower() for token in re.findall(r"[A-Za-z0-9_]+", str(text))}


def _parse_timestamp(value):
    # 把 ISO 时间字符串转成时间戳；解析失败就当作最旧
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return 0.0


def _normalize_note(note, index):
    # 兼容旧格式：如果 note 是字符串，就包装成标准 episodic note dict
    if isinstance(note, str):
        text = clip(note.strip(), 500)
        return {
            "text": text,
            "tags": [],
            "source": "",
            "created_at": now(),
            "note_index": index,
            "kind": "episodic",
        }

    # 兼容异常格式：不是 dict 的内容也转成字符串保存，别让状态恢复直接崩
    if not isinstance(note, dict):
        text = clip(str(note).strip(), 500)
        return {
            "text": text,
            "tags": [],
            "source": "",
            "created_at": now(),
            "note_index": index,
            "kind": "episodic",
        }

    # 标准 dict note：逐个字段清洗、裁剪、补默认值
    text = clip(str(note.get("text", "")).strip(), 500)
    tags = [str(tag).strip() for tag in _ensure_list(note.get("tags", [])) if str(tag).strip()]
    source = str(note.get("source", "")).strip()
    created_at = str(note.get("created_at", "")).strip() or now()
    note_index = int(note.get("note_index", index))
    kind = str(note.get("kind", "episodic")).strip() or "episodic"
    return {
        "text": text,
        "tags": _dedupe_preserve_order(tags),
        "source": source,
        "created_at": created_at,
        "note_index": note_index,
        "kind": kind,
    }


def normalize_memory_state(state, workspace_root=None):
    # 没有状态就创建默认状态；有状态但不是 dict 就直接报错
    if state is None:
        state = default_memory_state()
    elif not isinstance(state, dict):
        raise TypeError("memory state must be a mapping")

    # 规范化层的作用，是把“磁盘里可能长得不太一样的旧状态”
    # 统一整理成当前 runtime 可直接使用的紧凑结构。
    working = state.get("working")
    if not isinstance(working, dict):
        working = {}
    working.setdefault("task_summary", "")
    working.setdefault("recent_files", [])
    working["task_summary"] = clip(str(working.get("task_summary", "")).strip(), 300)
    working["recent_files"] = _dedupe_preserve_order(
        [
            canonicalize_path(path, workspace_root)
            for path in _ensure_list(working.get("recent_files", []))
            if str(path).strip()
        ]
    )[-WORKING_FILE_LIMIT:]
    state["working"] = working

    # 兼容旧字段 task/files，把它们迁移到 working 结构里
    if not str(working["task_summary"]).strip() and state.get("task"):
        working["task_summary"] = clip(str(state.get("task", "")).strip(), 300)
    if not working["recent_files"] and state.get("files"):
        working["recent_files"] = _dedupe_preserve_order(
            [
                canonicalize_path(path, workspace_root)
                for path in _ensure_list(state.get("files", []))
                if str(path).strip()
            ]
        )[-WORKING_FILE_LIMIT:]

    # 规范化 episodic_notes；如果旧状态只有 notes，就从 notes 迁移
    episodic_notes = state.get("episodic_notes")
    if not isinstance(episodic_notes, list):
        episodic_notes = []

    if not episodic_notes and state.get("notes"):
        episodic_notes = [
            _normalize_note(note, index)
            for index, note in enumerate(_ensure_list(state.get("notes", [])))
            if str(note).strip()
        ]
    else:
        normalized_notes = []
        for index, note in enumerate(episodic_notes):
            if isinstance(note, str) and not str(note).strip():
                continue
            normalized_notes.append(_normalize_note(note, index))
        episodic_notes = normalized_notes

    # 只保留最近若干条短期笔记
    episodic_notes = episodic_notes[-EPISODIC_NOTE_LIMIT:]
    state["episodic_notes"] = episodic_notes

    # 规范化文件摘要，并保留 summary / created_at / freshness 三个核心字段
    file_summaries = state.get("file_summaries")
    if not isinstance(file_summaries, dict):
        file_summaries = {}
    normalized_file_summaries = {}
    for path, summary in file_summaries.items():
        path = canonicalize_path(path, workspace_root)
        if isinstance(summary, dict):
            text = clip(str(summary.get("summary", "")).strip(), 500)
            created_at = str(summary.get("created_at", "")).strip() or now()
            freshness = summary.get("freshness")
            freshness = None if freshness in (None, "") else str(freshness).strip() or None
        else:
            text = clip(str(summary).strip(), 500)
            created_at = now()
            freshness = None
        if not path or not text:
            continue
        normalized_file_summaries[path] = {
            "summary": text,
            "created_at": created_at,
            "freshness": freshness,
        }
    state["file_summaries"] = normalized_file_summaries

    # 维护下一条 note 的编号，避免新增笔记时 note_index 冲突
    next_note_index = state.get("next_note_index")
    if not isinstance(next_note_index, int) or next_note_index < 0:
        next_note_index = 0
    max_index = max([note["note_index"] for note in episodic_notes], default=-1)
    state["next_note_index"] = max(next_note_index, max_index + 1)

    # 回填旧字段，保持向后兼容
    state["task"] = working["task_summary"]
    state["files"] = list(working["recent_files"])
    state["notes"] = [note["text"] for note in episodic_notes]

    # 如果有 workspace_root，就顺手读取 durable memory 里已有的 topic 列表
    durable_root = Path(workspace_root) / ".codeforge" / "memory" if workspace_root is not None else None
    durable_store = DurableMemoryStore(durable_root) if durable_root is not None else None
    state["durable_topics"] = durable_store.topic_slugs() if durable_store is not None else []
    return state


def set_task_summary(state, summary, workspace_root=None):
    # 先规范化状态，再更新当前任务摘要
    state = normalize_memory_state(state, workspace_root)
    state["working"]["task_summary"] = clip(str(summary).strip(), 300)
    state["task"] = state["working"]["task_summary"]
    return state


def remember_file(state, path, workspace_root=None):
    # 先规范化 memory state，保证 working/recent_files 等字段都存在
    state = normalize_memory_state(state, workspace_root)

    # 把路径规范成工作区相对路径，方便后续去重和展示
    path = canonicalize_path(path, workspace_root).strip()
    if not path:
        return state

    # 如果这个文件之前已经出现过，先删掉旧位置，再追加到末尾表示“最近访问”
    files = [item for item in state["working"]["recent_files"] if item != path]
    files.append(path)

    # 只保留最近 WORKING_FILE_LIMIT 个文件
    state["working"]["recent_files"] = files[-WORKING_FILE_LIMIT:]

    # 同步旧字段 files，保持兼容
    state["files"] = list(state["working"]["recent_files"])
    return state


def append_note(state, text, tags=(), source="", created_at=None, workspace_root=None, kind="episodic"):
    # 先规范化状态，再追加短期笔记
    state = normalize_memory_state(state, workspace_root)
    text = clip(str(text).strip(), 500)
    if not text:
        return state

    # tags 统一转成去重后的字符串列表
    normalized_tags = _dedupe_preserve_order(
        [str(tag).strip() for tag in _ensure_list(tags) if str(tag).strip()]
    )

    # 构造标准 note 结构，note_index 用于排序和避免重复编号
    note = {
        "text": text,
        "tags": normalized_tags,
        "source": str(source).strip(),
        "created_at": str(created_at).strip() if created_at else now(),
        "note_index": int(state.get("next_note_index", 0)),
        "kind": str(kind).strip() or "episodic",
    }
    state["next_note_index"] = note["note_index"] + 1

    # 相同文本的旧 note 先移除，再把新 note 放到末尾
    notes = [item for item in state["episodic_notes"] if item["text"] != note["text"]]
    notes.append(note)

    # 只保留最近 EPISODIC_NOTE_LIMIT 条短期笔记
    state["episodic_notes"] = notes[-EPISODIC_NOTE_LIMIT:]

    # 同步旧字段 notes，保持兼容
    state["notes"] = [item["text"] for item in state["episodic_notes"]]
    return state


def set_file_summary(state, path, summary, workspace_root=None):
    # 写入某个文件的短摘要，并记录 freshness 用来后续判断是否过期
    state = normalize_memory_state(state, workspace_root)
    path = canonicalize_path(path, workspace_root).strip()
    summary = clip(str(summary).strip(), 500)
    if not path or not summary:
        return state

    state["file_summaries"][path] = {
        "summary": summary,
        "created_at": now(),
        "freshness": file_freshness(path, workspace_root),
    }
    return state


def invalidate_file_summary(state, path, workspace_root=None):
    # 主动删除某个文件摘要，通常用于文件被修改后让摘要失效
    state = normalize_memory_state(state, workspace_root)
    path = canonicalize_path(path, workspace_root).strip()
    if not path:
        return state
    state["file_summaries"].pop(path, None)
    return state


def invalidate_stale_file_summaries(state, workspace_root=None):
    # 检查所有文件摘要，如果文件内容 hash 变了，就删除旧摘要
    state = normalize_memory_state(state, workspace_root)
    invalidated = []

    for path, summary in list(state["file_summaries"].items()):
        current_freshness = file_freshness(path, workspace_root)
        if summary.get("freshness") == current_freshness:
            continue
        invalidated.append(path)
        state["file_summaries"].pop(path, None)

    return state, invalidated


def summarize_read_result(result, limit=180):
    # 我们不会把完整文件内容塞进记忆层，
    # 这里只保留足够提醒下一轮“刚刚读到了什么”的短摘要。
    lines = [line.strip() for line in str(result).splitlines() if line.strip()]
    if not lines:
        return "(empty)"

    # read_file 返回通常第一行是 "# 文件名"，摘要里可以跳过这行
    if lines[0].startswith("# "):
        lines = lines[1:]
    if not lines:
        return "(empty)"

    # 取前 3 行拼成一句短摘要，再做长度裁剪
    summary = " | ".join(lines[:3])
    return clip(summary, limit)


def retrieval_candidates(state, query, limit=3, workspace_root=None):
    # 从短期记忆和长期记忆里召回与 query 最相关的 notes
    state = normalize_memory_state(state, workspace_root)
    query_tokens = _tokenize(query)
    ranked = []

    for note in state["episodic_notes"]:
        # 召回逻辑故意保持简单透明：先看 tag 精确命中，
        # 再看关键词重叠，最后看新旧程度。这里不引入 embedding。
        note_tags = {tag.lower() for tag in note.get("tags", [])}
        note_tokens = _tokenize(note.get("text", "")) | _tokenize(note.get("source", "")) | note_tags
        exact_tag_match = int(bool(query_tokens & note_tags))
        keyword_overlap = len(query_tokens & note_tokens)
        if exact_tag_match == 0 and keyword_overlap == 0:
            continue

        recency = _parse_timestamp(note.get("created_at"))
        note_index = int(note.get("note_index", 0))
        ranked.append(((exact_tag_match, keyword_overlap, recency, note_index), note))

    # 如果有工作区，就把 durable memory 里的长期 note 也纳入召回
    if workspace_root is not None:
        durable_store = DurableMemoryStore(Path(workspace_root) / ".codeforge" / "memory")
        for note in durable_store.retrieval_candidates(query, limit=limit):
            note_tags = {tag.lower() for tag in note.get("tags", [])}
            note_tokens = _tokenize(note.get("text", "")) | _tokenize(note.get("source", "")) | note_tags
            exact_tag_match = int(bool(query_tokens & note_tags))
            keyword_overlap = len(query_tokens & note_tokens)
            recency = _parse_timestamp(note.get("created_at"))
            ranked.append(((exact_tag_match, keyword_overlap, recency, -1), note))

    # 分数越高越靠前，最后只返回前 limit 条
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [note for _, note in ranked[:limit]]


def retrieval_view(state, query, limit=3, workspace_root=None):
    # 把召回结果渲染成 prompt 里可直接使用的 Relevant memory 文本块
    candidates = retrieval_candidates(state, query, limit=limit, workspace_root=workspace_root)
    lines = ["Relevant memory:"]
    if not candidates:
        lines.append("- none")
        return "\n".join(lines)

    for note in candidates:
        lines.append(f"- {note['text']}")
    return "\n".join(lines)


def render_memory_text(state, workspace_root=None):
    # 把 memory state 先规范化，保证字段结构完整
    state = normalize_memory_state(state, workspace_root)

    # 这里渲染的是给模型看的紧凑“仪表盘”，不是完整回放。
    # 笔记正文默认不展开，只有在相关召回时才按需拿出来。
    lines = [
        "Memory:",
        f"- task: {state['working']['task_summary'] or '-'}",
        f"- recent_files: {', '.join(state['working']['recent_files']) or '-'}",
    ]

    # 只展示最近文件里的少量摘要，并且要求摘要 freshness 仍然匹配当前文件内容
    summaries = []
    for path in state["working"]["recent_files"][:FILE_SUMMARY_LIMIT]:
        summary = state["file_summaries"].get(path, {})
        current_freshness = file_freshness(path, workspace_root)
        if summary.get("summary", "") and summary.get("freshness") == current_freshness:
            summaries.append(f"- {path}: {summary['summary']}")

    # 有有效文件摘要就展开；没有就用 "-" 占位
    if summaries:
        lines.append("- file_summaries:")
        lines.extend(f"  {line}" for line in summaries)
    else:
        lines.append("- file_summaries: -")

    # episodic_notes 只展示数量，不直接把所有短期笔记塞进 prompt
    lines.append(f"- episodic_notes: {len(state['episodic_notes'])}")

    # durable_topics 展示长期记忆里已有的主题列表
    durable_topics = state.get("durable_topics", [])
    lines.append(f"- durable_topics: {', '.join(durable_topics) or '-'}")
    return "\n".join(lines)


def is_effectively_empty(state, workspace_root=None):
    # 判断 memory 是否基本为空，用于决定有没有必要渲染/保存记忆内容
    state = normalize_memory_state(state, workspace_root)
    return (
        not str(state["working"]["task_summary"]).strip()
        and not state["working"]["recent_files"]
        and not state["episodic_notes"]
        and not state["file_summaries"]
    )


class LayeredMemory:
    def __init__(self, state=None, workspace_root=None):
        # workspace_root 用来做路径规范化、freshness 校验和 durable memory 定位
        self.workspace_root = workspace_root
        self.state = normalize_memory_state(state, workspace_root)

        # 有工作区时才启用长期记忆存储；否则只使用内存里的短期状态
        self.durable_store = DurableMemoryStore(Path(workspace_root) / ".codeforge" / "memory") if workspace_root is not None else None

    def to_dict(self):
        # 导出前再规范化一次，保证写回 session 的结构是最新格式
        self.state = normalize_memory_state(self.state, self.workspace_root)
        return self.state

    def canonical_path(self, path):
        # 把路径转成工作区相对的稳定格式
        return canonicalize_path(path, self.workspace_root)

    def set_task_summary(self, summary):
        # 更新当前任务摘要
        self.state = set_task_summary(self.state, summary, self.workspace_root)
        return self

    def remember_file(self, path):
        # 记录最近接触过的文件
        self.state = remember_file(self.state, path, self.workspace_root)
        return self

    def append_note(self, text, tags=(), source="", created_at=None, kind="episodic"):
        # 追加一条短期笔记，通常来自工具结果或运行过程记录
        self.state = append_note(
            self.state,
            text,
            tags=tags,
            source=source,
            created_at=created_at,
            workspace_root=self.workspace_root,
            kind=kind,
        )
        return self

    def set_file_summary(self, path, summary):
        # 为某个文件保存短摘要和 freshness
        self.state = set_file_summary(self.state, path, summary, self.workspace_root)
        return self

    def invalidate_file_summary(self, path):
        # 主动让某个文件摘要失效
        self.state = invalidate_file_summary(self.state, path, self.workspace_root)
        return self

    def invalidate_stale_file_summaries(self):
        # 批量清理 freshness 已经过期的文件摘要，并返回被清理的路径
        self.state, invalidated = invalidate_stale_file_summaries(self.state, self.workspace_root)
        return invalidated

    def retrieval_candidates(self, query, limit=3):
        # 根据 query 召回相关记忆，返回结构化 note 列表
        return retrieval_candidates(self.state, query, limit=limit, workspace_root=self.workspace_root)

    def retrieval_view(self, query, limit=3):
        # 根据 query 召回相关记忆，并渲染成 prompt 文本块
        return retrieval_view(self.state, query, limit=limit, workspace_root=self.workspace_root)

    def render_memory_text(self):
        # 渲染给模型看的 memory 仪表盘
        return render_memory_text(self.state, self.workspace_root)

    def promote_durable(self, promotions):
        # 把候选长期记忆写入 durable memory；没有 durable store 就直接跳过
        if self.durable_store is None:
            return [], []

        # promote 前后都规范化一次，保证 durable_topics 等字段同步更新
        self.state = normalize_memory_state(self.state, self.workspace_root)
        promoted, superseded = self.durable_store.promote(promotions)
        self.state = normalize_memory_state(self.state, self.workspace_root)
        return promoted, superseded