"""Prompt 组装与上下文预算控制。

这个模块负责决定：每一轮到底把多少 prefix、memory、相关笔记、历史
以及当前用户请求送进模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass


# 整个 prompt 的默认总字符预算
DEFAULT_TOTAL_BUDGET = 12000

# 各个 section 的初始预算；后续如果超预算，会在这些基础上继续压缩
DEFAULT_SECTION_BUDGETS = {
    "prefix": 3600,
    "memory": 1600,
    "relevant_memory": 1200,
    "history": 5200,
}

# 各个 section 的最低保留长度，避免压缩时把关键上下文直接砍没
DEFAULT_SECTION_FLOORS = {
    "prefix": 1200,
    "memory": 400,
    "relevant_memory": 300,
    "history": 1500,
}

# 当 prompt 超预算时，会优先压缩这些 section。
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix")

# prompt 最终组装时的 section 顺序
SECTION_ORDER = ("prefix", "memory", "relevant_memory", "history", "current_request")

# 当前用户请求单独定义成常量，方便统一引用
CURRENT_REQUEST_SECTION = "current_request"

# 每轮最多召回 3 条相关记忆
RELEVANT_MEMORY_LIMIT = 3


def _tail_clip(text, limit):
    # 把文本裁剪到指定长度；这里保留前半段内容，并用 ... 标记截断
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


@dataclass
class SectionRender:
    # raw 是原始文本，budget 是预算，rendered 是裁剪后的最终文本
    raw: str
    budget: int
    rendered: str
    details: dict | None = None

    @property
    def raw_chars(self):
        # 原始 section 长度，用于 metadata/report 统计
        return len(self.raw)

    @property
    def rendered_chars(self):
        # 实际进入 prompt 的 section 长度
        return len(self.rendered)


class ContextManager:
    def __init__(
        self,
        agent,
        total_budget=DEFAULT_TOTAL_BUDGET,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
    ):
        # 保存 agent 引用，后面 build prompt 时要从 agent 里拿 prefix、memory、history 等状态
        self.agent = agent

        # prompt 总预算，超过这个长度就会触发上下文压缩
        self.total_budget = int(total_budget)

        # 复制一份默认 section 预算，避免直接修改全局 DEFAULT_SECTION_BUDGETS
        self.section_budgets = dict(DEFAULT_SECTION_BUDGETS)

        # 如果外部传了自定义 section_budgets，就覆盖默认预算
        if section_budgets:
            self.section_budgets.update({str(key): int(value) for key, value in section_budgets.items()})

        # 记录外部指定的 section 最低保留长度；没有传就用空 dict
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}

        # 计算每个 section 的压缩下限，防止某一块被裁到太短
        self.section_floors = self._compute_section_floors()

        # prompt 超预算时的压缩顺序；默认先压 relevant_memory，再压 history、memory、prefix
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)

    def build(self, user_message):
        """按预算组装一轮完整 prompt。

        为什么存在：
        仅靠用户这一轮输入，模型并不知道当前仓库状态、会话里已经读过什么、
        哪些旧信息还值得继续参考。这个函数负责把“稳定基线 + 工作记忆 +
        相关笔记 + 历史 + 当前请求”拼成真正发给模型的 prompt。

        输入 / 输出：
        - 输入：`user_message`，也就是用户当前这一轮的新请求。
        - 输出：`(prompt, metadata)`。
          `prompt` 是最终发送给模型的文本；
          `metadata` 记录了每个 section 的原始长度、裁剪后的长度、是否触发了
          预算收缩等信息，后续会进入 trace/report，便于解释这轮 prompt
          是怎么被拼出来的。

        在 agent 链路里的位置：
        它位于 `Pico.ask()` 的每轮模型调用之前，是“真正发请求给模型”
        的最后一道组装工序。`WorkspaceContext` 提供稳定前缀，`LayeredMemory`
        提供工作记忆，这个函数则把它们和当前请求合成一份可控大小的 prompt。
        """
        # 统一转成字符串，避免上层传入非字符串对象导致拼 prompt 出问题
        user_message = str(user_message)

        # 每次 build 前重新计算 floor，确保外部修改预算后下限也能同步更新
        self.section_floors = self._compute_section_floors()

        # 默认启用三项能力：普通 memory、相关记忆召回、上下文压缩
        memory_enabled = True
        relevant_memory_enabled = True
        context_reduction_enabled = True

        # 如果 agent 支持 feature flag，就按 agent 当前配置决定哪些上下文能力开启
        if hasattr(self.agent, "feature_enabled"):
            memory_enabled = self.agent.feature_enabled("memory")
            relevant_memory_enabled = self.agent.feature_enabled("relevant_memory")
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")

        # 先准备几个基础 section 的原始文本；history 这里先留空，后面渲染时再从 session 里取
        section_texts = {
            "prefix": str(getattr(self.agent, "prefix", "")),
            "memory": "Memory:\n- disabled" if not memory_enabled else str(self.agent.memory_text()),
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }

        # 如果当前 session 有 checkpoint 信息，就拼到 prefix 后面，帮助模型知道恢复现场
        checkpoint_text = ""
        if hasattr(self.agent, "render_checkpoint_text"):
            checkpoint_text = str(self.agent.render_checkpoint_text() or "").strip()
        if checkpoint_text:
            section_texts["prefix"] = section_texts["prefix"] + "\n\n" + checkpoint_text

        # 根据当前用户请求，从 memory 中召回最相关的少量笔记
        selected_notes = []
        if memory_enabled and relevant_memory_enabled and hasattr(self.agent, "memory") and hasattr(self.agent.memory, "retrieval_candidates"):
            selected_notes = self.agent.memory.retrieval_candidates(user_message, limit=RELEVANT_MEMORY_LIMIT)

        # 如果关闭上下文压缩，就完整渲染各 section，不走预算收缩逻辑
        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(section_texts, selected_notes=selected_notes)
            prompt = self._assemble_prompt(rendered)

            # 即使不压缩，也生成 metadata，方便 trace/report 统一记录 prompt 构成
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                selected_notes=selected_notes,
                user_message=user_message,
                section_texts=section_texts,
            )
            return prompt, metadata

        # 从默认 section 预算复制一份本轮可修改预算，后续压缩只改这个局部 budgets
        budgets = dict(self.section_budgets)

        # 按当前预算先渲染一次 prompt，看看是否已经满足总预算
        rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes)
        prompt = self._assemble_prompt(rendered)

        # 记录每次压缩发生在哪个 section、压缩前后长度是多少
        reduction_log = []

        # 如果 prompt 超预算，就按固定顺序不断压缩。
        # 这里的顺序体现了平台偏好：
        # 先牺牲 relevant_memory，再牺牲 history，然后才动 memory 和 prefix。
        # 最新用户请求永远不裁剪，因为那是本轮最重要的输入。
        while len(prompt) > self.total_budget:
            # overflow 表示当前 prompt 比总预算多出来多少字符
            overflow = len(prompt) - self.total_budget
            reduced = False

            # 按 reduction_order 找一个还能继续压缩的 section
            for section in self.reduction_order:
                # floor 是这个 section 的最低保留长度，不能压到比它更低
                floor = int(self.section_floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))

                # 如果当前 section 已经压到下限，就跳过它
                if current_budget <= floor:
                    continue

                # 尝试从当前 section 的预算里扣掉 overflow，但不能低于 floor
                new_budget = max(floor, current_budget - overflow)

                # 如果新预算没有变小，说明这次无法靠这个 section 继续压缩
                if new_budget >= current_budget:
                    continue

                # 记录这次压缩动作，后面 metadata/report 可以解释 prompt 是怎么变短的
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current_budget,
                        "after_chars": new_budget,
                        "overflow_chars": overflow,
                    }
                )

                # 更新该 section 的预算，然后重新渲染 prompt
                budgets[section] = new_budget
                rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes)
                prompt = self._assemble_prompt(rendered)

                # 本轮 while 已经成功压缩一次，跳出 for，重新检查总长度
                reduced = True
                break

            # 所有 section 都压不动时退出，避免死循环
            if not reduced:
                break

        # 生成最终 metadata：包含 section 长度、预算、召回笔记、压缩记录等
        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            selected_notes=selected_notes,
            user_message=user_message,
            section_texts=section_texts,
        )

        # 返回最终 prompt 和可审计的构建信息
        return prompt, metadata
    
    def _render_sections_without_reduction(self, section_texts, selected_notes=None):
        # 这个分支用于关闭 context_reduction 的情况：不按预算裁剪，尽量完整渲染所有 section
        selected_notes = selected_notes or []

        # relevant_memory 单独渲染成固定标题 + 若干条笔记，方便最终 prompt 结构稳定
        relevant_lines = ["Relevant memory:"]
        if selected_notes:
            relevant_lines.extend(f"- {note['text']}" for note in selected_notes)
        else:
            relevant_lines.append("- none")
        relevant_raw = "\n".join(relevant_lines)

        # history 从 agent.session["history"] 里取；这里先转 list，避免后续操作影响原始对象
        history = list(getattr(self.agent, "session", {}).get("history", []))
        history_raw = self._raw_history_text(history)

        # 返回每个 section 的 SectionRender；因为不裁剪，所以 raw 和 rendered 基本相同
        return {
            "prefix": SectionRender(raw=section_texts["prefix"], budget=len(section_texts["prefix"]), rendered=section_texts["prefix"], details={}),
            "memory": SectionRender(raw=section_texts["memory"], budget=len(section_texts["memory"]), rendered=section_texts["memory"], details={}),
            "relevant_memory": SectionRender(
                raw=relevant_raw,
                budget=len(relevant_raw),
                rendered=relevant_raw,
                details={
                    # selected_notes 是召回到的原始笔记文本
                    "selected_notes": [note["text"] for note in selected_notes],
                    # rendered_notes 是实际进入 prompt 的笔记；不裁剪时两者相同
                    "rendered_notes": [note["text"] for note in selected_notes],
                    "selected_count": len(selected_notes),
                    "rendered_count": len(selected_notes),
                    # 不做预算分配，所以 note_budget 记为 0
                    "note_budget": 0,
                },
            ),
            "history": SectionRender(raw=history_raw, budget=len(history_raw), rendered=history_raw, details={"rendered_entries": []}),
            CURRENT_REQUEST_SECTION: SectionRender(
                # 当前请求永远完整保留，不参与压缩
                raw=section_texts[CURRENT_REQUEST_SECTION],
                budget=0,
                rendered=section_texts[CURRENT_REQUEST_SECTION],
                details={},
            ),
        }

    def _compute_section_floors(self):
        # 给每个 section 计算最低保留预算：默认是该 section 初始预算的 1/4，但至少保留 20 字符
        floors = {
            section: max(20, int(budget) // 4)
            for section, budget in self.section_budgets.items()
        }

        # 外部传入的 floor 配置优先级更高，可以覆盖默认计算结果
        floors.update(self._section_floor_overrides)
        return floors

    def _render_sections(self, section_texts, budgets, selected_notes=None):
        # 按 SECTION_ORDER 统一渲染各个 section，保证最终 prompt 的结构顺序稳定
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)

            if section == CURRENT_REQUEST_SECTION:
                # 当前用户请求是本轮最重要输入，不做裁剪，也不需要预算
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "relevant_memory":
                # relevant_memory 有自己的渲染逻辑：多条 note 要共享预算，不能被一条长 note 吃满
                rendered[section] = self._render_relevant_memory(selected_notes or [], int(budget or 0))
            elif section == "history":
                # history 也有专门压缩逻辑：优先保留最近消息，并折叠旧的重复读取
                rendered[section] = self._render_history_section(int(budget or 0))
            else:
                # prefix 和 memory 这类普通文本 section，直接按预算做尾部裁剪
                raw = section_texts[section]
                rendered_text = _tail_clip(raw, int(budget)) if budget is not None else raw
                rendered[section] = SectionRender(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})

        return rendered

    def _render_relevant_memory(self, selected_notes, budget):
        # 把召回笔记渲染成 prompt 里的 Relevant memory section
        header = "Relevant memory:"

        # 只取非空 text，避免空笔记污染 prompt
        note_texts = [str(note.get("text", "")) for note in selected_notes if str(note.get("text", "")).strip()]

        # raw 代表未裁剪的完整 relevant_memory 文本
        raw_lines = [header] + [f"- {text}" for text in note_texts]
        raw = "\n".join(raw_lines) if note_texts else "\n".join([header, "- none"])

        if not note_texts:
            # 没召回到笔记时，固定渲染 "- none"，保持 section 结构稳定
            rendered = raw
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "selected_notes": [],
                    "rendered_notes": [],
                    "selected_count": 0,
                    "rendered_count": 0,
                    "note_budget": 0,
                },
            )

        # 计算每条 note 平均能分到多少预算
        per_note_budget = self._per_note_budget(budget, len(note_texts), header)
        rendered_notes = []

        while True:
            # 让每条 note 平分这一段的预算，避免一条超长笔记把其他笔记都挤掉。
            rendered_notes = [_tail_clip(text, per_note_budget) for text in note_texts]
            rendered = "\n".join([header] + [f"- {text}" for text in rendered_notes])

            # 如果已经放得下，或者每条 note 已经压到极限，就停止继续压缩
            if len(rendered) <= budget or per_note_budget <= 1:
                break

            # 还超预算就继续把每条 note 的预算往下减
            per_note_budget -= 1

        if len(rendered) > budget and budget > 0:
            # 极端情况下，按 note 平分还是放不下，就退化成对整个 raw 直接裁剪
            rendered = _tail_clip(raw, budget)
            rendered_notes = [rendered]

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                # selected_notes 是召回到的完整 note，rendered_notes 是实际进入 prompt 的 note
                "selected_notes": note_texts,
                "rendered_notes": rendered_notes,
                "selected_count": len(note_texts),
                "rendered_count": len(rendered_notes),
                "note_budget": per_note_budget,
            },
        )

    def _per_note_budget(self, budget, note_count, header):
        # 没有 note 时，不需要分配预算
        if note_count <= 0:
            return 0

        # overhead 粗略估算标题、换行、"- " 这些结构字符占用
        overhead = len(header) + 3 * note_count

        # 真正能分给 note 正文的预算
        usable = max(0, budget - overhead)

        # 每条 note 至少给 1 个字符，避免预算太小时出现 0
        return max(1, usable // note_count)

    def _render_history_section(self, budget):
        # 从 session 里拿完整历史，然后渲染成 Transcript section
        history = list(getattr(self.agent, "session", {}).get("history", []))
        raw = self._raw_history_text(history)

        if not history:
            # 没有历史时也固定输出 Transcript，保证 prompt 结构一致
            rendered = "Transcript:\n- empty"
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "rendered_entries": [],
                    "older_entries_count": 0,
                    "collapsed_duplicate_reads": 0,
                    "reused_file_summary_count": 0,
                    "summarized_tool_count": 0,
                },
            )

        # 优先保留最近的历史，因为下一步决策通常最依赖刚刚发生的工具结果。
        recent_window = 6
        recent_start = max(0, len(history) - recent_window)

        # 先把历史压成 entry 列表；里面可能已经折叠了旧 read_file、复用了文件摘要等
        history_entries, history_details = self._compressed_history_entries(history, recent_start)

        rendered_entries = []

        # 倒着放 entry：从最近的开始尝试塞进 prompt，保证越近的上下文越容易保留下来
        for entry in reversed(history_entries):
            recent = bool(entry.get("recent", False))
            candidate_lines = list(entry.get("lines", []))

            # 尝试把当前 entry 放到已有 rendered_entries 前面
            candidate_entries = candidate_lines + rendered_entries
            candidate_rendered = "\n".join(["Transcript:", *candidate_entries])

            if len(candidate_rendered) <= budget:
                # 放得下就直接接受
                rendered_entries = candidate_entries
                continue

            if recent:
                # 最近历史更重要，即使放不下，也尝试进一步裁剪后保留
                available = budget - len("Transcript:")
                if rendered_entries:
                    # 已经保留的内容也要占预算，这里扣掉它们的大致长度
                    available -= sum(len(line) + 1 for line in rendered_entries)

                # 至少给最近 entry 留 20 字符，避免被压成完全没信息
                available = max(20, available - 1)
                candidate_lines = [_tail_clip(line, available) for line in candidate_lines]
                candidate_entries = candidate_lines + rendered_entries
                candidate_rendered = "\n".join(["Transcript:", *candidate_entries])

                if len(candidate_rendered) <= budget:
                    rendered_entries = candidate_entries
            else:
                # 较旧历史优先级低，放不下时只保留极短提示
                smaller_lines = [_tail_clip(line, 20) for line in candidate_lines]
                smaller_entries = smaller_lines + rendered_entries
                smaller_rendered = "\n".join(["Transcript:", *smaller_entries])

                if len(smaller_rendered) <= budget:
                    rendered_entries = smaller_entries

        rendered = "\n".join(["Transcript:", *rendered_entries])

        if len(rendered) > budget and budget > 0:
            # 最后的兜底：如果组合后仍超预算，就直接裁剪 raw history
            rendered = _tail_clip(raw, budget)

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "recent_window": recent_window,
                "recent_start": recent_start,
                "rendered_entries": rendered_entries,
                **history_details,
            },
        )

    def _compressed_history_entries(self, history, recent_start):
        # 把完整 history 压缩成一组可渲染的 entries。
        # recent_start 之后的历史算“最近历史”，会尽量保留完整内容；
        # recent_start 之前的旧历史会被摘要、折叠，避免 transcript 越积越长。
        entries = []
        seen_older_reads = set()

        # details 用来记录压缩过程中发生了什么，最后会进入 metadata/report
        details = {
            "older_entries_count": 0,
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
        }

        for index, item in enumerate(history):
            # 判断这条历史是不是最近窗口内的内容
            recent = index >= recent_start

            if recent:
                # 最近历史最重要，通常包含刚刚的工具结果或用户追问，所以给较大的行裁剪上限
                line_limit = 900
                entries.append(
                    {
                        "recent": True,
                        "lines": self._render_history_item(item, line_limit),
                    }
                )
                continue

            if item["role"] == "tool" and item["name"] == "read_file":
                # 旧的 read_file 很容易重复读同一个文件，所以这里做去重折叠
                path = str(item["args"].get("path", "")).strip()
                if path in seen_older_reads:
                    details["collapsed_duplicate_reads"] += 1
                    continue
                seen_older_reads.add(path)

                # 如果 memory 里已经有这个文件的摘要，就用摘要替代原始 read_file 输出
                summary = self._reusable_file_summary(path)
                if summary:
                    entries.append({"recent": False, "lines": [f"{path} -> {summary}"]})
                    details["older_entries_count"] += 1
                    details["reused_file_summary_count"] += 1
                    continue

            if item["role"] == "tool":
                # 对旧工具结果做短摘要，避免把很长的 stdout / 文件内容再次塞进 prompt
                summary_line = self._summarize_old_tool_item(item)
                entries.append({"recent": False, "lines": [summary_line]})
                details["older_entries_count"] += 1
                details["summarized_tool_count"] += 1
                continue

            # 旧的普通 user/assistant 消息只保留短版本
            entries.append({"recent": False, "lines": self._render_history_item(item, 60)})

        return entries, details


    def _reusable_file_summary(self, path):
        # 尝试从 memory.file_summaries 里拿某个文件的短摘要
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "to_dict"):
            return ""

        # to_dict 会返回规范化后的 memory 快照
        snapshot = memory.to_dict()
        summary = snapshot.get("file_summaries", {}).get(str(path), {})
        if not summary:
            return ""

        # 这里只返回摘要正文；freshness 校验在 memory 层已经处理过
        return str(summary.get("summary", "")).strip()


    def _summarize_old_tool_item(self, item):
        if item["name"] == "run_shell":
            # run_shell 的输出通常包含 exit_code/stdout/stderr，可能很长，所以单独压成一行
            command = str(item["args"].get("command", "")).strip() or "shell"
            lines = [line.strip() for line in str(item.get("content", "")).splitlines() if line.strip()]
            summary = " | ".join(lines[:3]) if lines else "(empty)"
            return f"{command} -> {summary}"

        # 其他旧工具直接复用通用渲染逻辑，并取第一行作为摘要
        return self._render_history_item(item, 60)[0]


    def _raw_history_text(self, history):
        # 渲染未压缩的完整 transcript，主要用于统计 raw_chars 或兜底裁剪
        if not history:
            return "Transcript:\n- empty"

        lines = []
        for item in history:
            if item["role"] == "tool":
                # 工具历史包含工具名、参数和完整工具结果
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(str(item["content"]))
            else:
                # 普通消息只记录角色和内容
                lines.append(f"[{item['role']}] {item['content']}")

        return "\n".join(["Transcript:", *lines])


    def _render_history_item(self, item, line_limit):
        if item["role"] == "tool":
            # 工具消息拆成两行：第一行说明调了什么工具，第二行放裁剪后的结果
            prefix = f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}"
            content = _tail_clip(item["content"], max(20, line_limit))
            return [prefix, content]

        # user/assistant 消息渲染成单行，并按 line_limit 裁剪
        return [f"[{item['role']}] {_tail_clip(item['content'], line_limit)}"]


    def _assemble_prompt(self, rendered):
        # 顺序是刻意设计的：稳定规则放前面，最新请求放最后。
        # prefix：规则、工具说明、工作区基线
        # memory：当前任务摘要、最近文件、文件摘要
        # relevant_memory：和本轮请求相关的少量记忆
        # history：会话历史
        # current_request：本轮用户请求，放最后保证模型最容易关注
        return "\n\n".join(
            [
                rendered["prefix"].rendered,
                rendered["memory"].rendered,
                rendered["relevant_memory"].rendered,
                rendered["history"].rendered,
                rendered[CURRENT_REQUEST_SECTION].rendered,
            ]
        ).strip()


    def _metadata(self, prompt, rendered, budgets, reduction_log, selected_notes, user_message, section_texts):
        # section_metadata 记录每个 section 的原始长度、预算长度和最终渲染长度
        # 这些信息后续可以用来解释：prompt 哪一块太长、哪一块被压缩了
        section_metadata = {}
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "budget_chars": int(budgets.get(section, 0)),
                "rendered_chars": rendered[section].rendered_chars,
            }

        # current_request 不参与预算压缩，所以 budget_chars 记为 None
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_chars": None,
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
        }

        return {
            # prompt 总长度和总预算，用来判断最终是否仍然超预算
            "prompt_chars": len(prompt),
            "prompt_budget_chars": self.total_budget,
            "prompt_over_budget": len(prompt) > self.total_budget,

            # 记录 prompt section 顺序和本轮实际使用的预算
            "section_order": list(SECTION_ORDER),
            "section_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,

            # 记录预算压缩过程：压了哪些 section、从多少压到多少
            "budget_reductions": reduction_log,
            "reduction_order": list(self.reduction_order),

            # relevant_memory 相关统计：召回了几条、来自哪里、是否 durable、最终渲染了几条
            "relevant_memory": {
                "limit": RELEVANT_MEMORY_LIMIT,
                "selected_count": len(selected_notes),
                "selected_notes": [note["text"] for note in selected_notes],
                "selected_sources": [str(note.get("source", "")).strip() for note in selected_notes],
                "selected_kinds": [str(note.get("kind", "episodic")).strip() or "episodic" for note in selected_notes],
                "selected_durable_count": sum(
                    1 for note in selected_notes if (str(note.get("kind", "episodic")).strip() or "episodic") == "durable"
                ),
                "raw_chars": rendered["relevant_memory"].raw_chars,
                "rendered_chars": rendered["relevant_memory"].rendered_chars,
                "rendered_notes": list(rendered["relevant_memory"].details.get("rendered_notes", [])),
                "rendered_count": int(rendered["relevant_memory"].details.get("rendered_count", 0)),
            },

            # history 相关统计：原始长度、渲染长度、旧历史折叠和摘要情况
            "history": {
                "raw_chars": rendered["history"].raw_chars,
                "rendered_chars": rendered["history"].rendered_chars,
                "older_entries_count": int(rendered["history"].details.get("older_entries_count", 0)),
                "collapsed_duplicate_reads": int(rendered["history"].details.get("collapsed_duplicate_reads", 0)),
                "reused_file_summary_count": int(rendered["history"].details.get("reused_file_summary_count", 0)),
                "summarized_tool_count": int(rendered["history"].details.get("summarized_tool_count", 0)),
            },

            # current_request 单独记录，方便验证最新用户请求没有被裁剪丢失
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "rendered_chars": len(user_message),
                "section_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            },
        }