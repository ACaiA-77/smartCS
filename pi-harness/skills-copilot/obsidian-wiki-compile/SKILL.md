---
name: obsidian-wiki-compile
description: Obsidian 知识库编译与巡检。当需要整理 raw/ 原始资料、维护 Wiki 条目、检查断链与孤儿页面，或决定该查 Wiki/Ontology 还是 RAG 兜底时使用。
---

# Obsidian LLM Wiki 编译

## 先查，再写
1. 先用 `vault_search` 在 `wiki`、`notes`、`raw` 三个区域检索，确认是否已有相关条目。
2. 再用 `onto_query` 查相关实体（人物、项目、RepoIssue、MailThread、NotePage、ActionItem）及其别名，避免同一个人/项目被写成两条。
3. 只有 Wiki/Ontology 都没有命中时，才把 `knowledge_search`（RAG 兜底）作为补充证据。

## 编译原始资料
- 用 `wiki_compile_doc` 归档一个来源：`source_uri` 必须可追溯，`raw_content` 保存原文，`summary_markdown` 写你提炼的条目。
- 编译后 `wiki/index.md` 与 `wiki/log.md` 会自动更新；不要手工去改这两个文件。
- 相关的实体用 `related_entities` 一并传入，便于后续 `onto_query` 串联。

## notes/ 只提案，不直接写
- `notes/` 是用户的私有笔记区。任何修改都必须用 `vault_propose_edit` 生成提案（含 `target_rel_path`、`rationale`、`proposed_content`），由用户决定是否采纳。
- 绝不要用其他写入工具直接覆盖 `notes/` 下的文件。

## 收尾巡检
- 写完或整理完一批资料后运行 `wiki_lint`，检查断链 `[[missing]]`、孤儿页面与未进入索引的页面。
- 把 lint 结果如实汇报；需要修复时逐条处理，不要批量删除。
