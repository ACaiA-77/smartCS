"""Author source-first challenge candidates; this script never invokes retrieval."""

import json
from collections import Counter, defaultdict
from pathlib import Path


INDEX = Path("artifacts/rag_round3/production_indexes")
OUT = Path("artifacts/rag_cross_domain_sparse_challenge_candidates_20260923")
A = "apple_support"
E = "agent_engineering"
ACCOUNT = "apple_account_registration_guide.md"
FAQ = "apple_product_faq.md"
POLICY = "apple_refund_subscription_policy.md"
MAC = "generated/apple_mac_support.md"
RETURNS = "generated/apple_returns_refund_cn.md"
SALES = "generated/apple_sales_policies_cn.md"
RETAIL = "generated/apple_retail_sales_policies_cn.md"
REFUND = "generated/apple_request_refund.md"
SUBSCRIPTION = "generated/apple_cancel_subscription.md"
CARE = "generated/apple_cancel_applecare.md"
ARCH = "Getnotes/架构师 Agent 落地实践：从技术方案设计到复杂系统理解-2026年09月09日-来自【得到大脑】.md"
HOOK = "Getnotes/Agent-Hook深度教程-事件-匹配-处理器-阻止机制一次讲透.md"

# Each reference is (source, chunk_index, grade, fact_group, literal source anchor, judgment).
# Questions were composed from these frozen sections in source order, before any A/B retrieval.
CASES = [
    (A, "semantic", "从 Apple 中国官网退多件商品时，应怎样打包，装箱单怎么处理？", "多件商品通常放在一个包装箱，并附上装箱单。", ["same_source_competition"],
     [(POLICY, 1, 2, "answer", "一个包装箱", "同时说明合箱和装箱单要求")]),
    (A, "semantic", "Apple 中国官网发来发货详情后，过了多久仍未交付时，买家可选择取消订单？", "发货详情通知之后 30 日内仍未交付，可以选择取消订单。", ["cross_domain_decoy", "supporting_evidence", "same_source_competition", "multi_fact"],
     [(SALES, 8, 2, "answer", "30 日内未能向您交付", "直接说明取消条件和 30 日窗口"), (RETURNS, 4, 2, "answer", "30 日内未能向您交付", "同一交付规则的重复来源"), (SALES, 7, 1, "dispatch_notice", "发货详情", "说明发货详情通知何时发送，未单独给出超期取消规则"), (RETURNS, 3, 1, "dispatch_notice", "发货详情", "同一发货通知事实的重复来源")]),
    (A, "semantic", "用 macOS 恢复重新安装系统时，个人数据一定会被移除吗？", "文档说明使用 macOS 恢复重新安装不会移除个人数据。", ["cross_domain_decoy"],
     [(FAQ, 2, 2, "answer", "不会移除个人数据", "直接回答数据是否移除"), (MAC, 2, 2, "answer", "不会移除你的个人数据", "同一事实的另一来源表达")]),
    (A, "semantic", "对 App Store 的项目申请退款后，何时能知道进度？获批后款项会马上到账吗？", "通常等 24 至 48 小时获知最新信息；获批后退回付款方式仍可能另需时间。", ["multi_component_answer", "same_source_competition"],
     [(POLICY, 5, 2, "answer", "24 到 48 小时", "同时说明更新等待和到账另需时间"), (REFUND, 2, 2, "answer", "需要再等待一些时间", "同一完整事实的官方摘录")]),
    (A, "lexical", "Apple 中国官网标价出错时，通知买家后提供哪两种订单处理选择？", "可按正确价格继续交易，或无费用地取消订单。", ["cross_domain_decoy", "same_source_competition"],
     [(SALES, 6, 2, "answer", "以正确价格继续交易", "直接给出两种价格错误处理选择"), (RETURNS, 2, 2, "answer", "以正确价格继续交易", "同一条款的重复来源")]),
    (A, "lexical", "Apple 免费或打折的试用订阅如果不想续订，至少要在结束前多久取消？", "至少提前 24 小时。", ["same_source_competition"],
     [(SUBSCRIPTION, 17, 2, "answer", "至少 24 小时", "直接给出试用取消提前量")]),
    (A, "lexical", "Apple 中国大陆零售店从何时不再兑换购物卡，现有卡的余额应如何处理？", "自 2013 年 7 月 1 日起不再兑换；现有持卡人可到中国大陆 Apple Store 零售店申请退还余额。", ["multi_component_answer"],
     [(RETAIL, 0, 2, "answer", "2013 年7 月1 日", "同段给出停止兑换时间和余额处理办法")]),
    (A, "lexical", "取消 AppleCare 计划需联系人工协助时，要准备哪三项资料？", "AppleCare 协议编号、设备序列号和原始销售收据。", ["multi_component_answer", "same_source_competition"],
     [(POLICY, 8, 2, "answer", "原始销售收据", "完整列出人工协助所需三项资料"), (CARE, 3, 2, "answer", "原始销售收据", "同一清单的官方摘录")]),
    (A, "confusing", "分期付款的 Apple 中国官网订单退款后，每期还款额和手续费一定保持原样吗？", "不一定；Apple 处理退款后，发卡行可能重新统计每期金额和手续费。", ["same_source_competition"],
     [(POLICY, 2, 2, "answer", "发卡行可能重新统计每期还款金额及手续费", "直接说明分期退款后的金额变化可能")]),
    (A, "confusing", "在 Apple 中国官网下单并送货的商品，可以拿到任意 Apple 零售店退吗？", "不能；只有零售店取货订单可以到店退，且应退到取货的零售店。", ["cross_domain_decoy", "supporting_evidence", "same_source_competition", "multi_fact"],
     [(POLICY, 3, 2, "answer", "只有 Apple 零售店取货订单", "直接限定到店退货范围"), (RETURNS, 0, 2, "answer", "只有在Apple零售店取货的订单", "同一政策的原文复述"), (SALES, 1, 2, "answer", "只有在Apple零售店取货的订单", "同一政策条款的另一重复段落"), (POLICY, 4, 1, "retail_boundary", "仅可在原店进行退货", "补充零售店购买的不同退货边界，不能单独回答官网配送订单")]),
    (A, "confusing", "iPhone 电池因正常使用而损耗，属于 Apple 一年有限保修中的制造问题吗？", "不属于；该有限保修通常针对制造问题，不覆盖正常使用造成的电池损耗。", ["supporting_evidence", "same_source_competition", "multi_fact"],
     [(FAQ, 5, 2, "answer", "正常使用造成的电池损耗", "直接区分制造问题与正常损耗"), ("generated/apple_iphone_repair.md", 6, 2, "answer", "正常使用造成的电池损耗", "同一保修事实的官方摘录"), (SALES, 13, 1, "manufacturing_scope", "材料和工艺缺陷", "仅说明一年保修针对制造缺陷，未说明正常电池损耗")]),
    (A, "confusing", "订阅列表中找不到 AppleCare 计划，能据此断定保障已经取消了吗？", "不能；计划可能尚未关联 Apple 账户，需要先检查关联情况。", ["cross_domain_decoy", "same_source_competition"],
     [(POLICY, 8, 2, "answer", "尚未关联到 Apple 账户", "直接给出另一种解释"), (CARE, 1, 2, "answer", "可能没有关联到你的 Apple 账户", "同一事实的官方摘录")]),
    (E, "semantic", "一个候选服务仓库没有命中业务关键词，为什么仍不能直接判定它不需改动？", "通用规则引擎可能仍承载目标能力；需要追踪系统调用和真实代码，而非只凭关键词搜索。", ["supporting_evidence", "same_source_competition", "multi_fact"],
     [(ARCH, 32, 2, "answer", "通用规则引擎", "直接描述 grep 漏掉实际承载能力的案例"), (ARCH, 31, 1, "graph_context", "图谱才能帮助你判断", "补充单仓库 grep 不足以判断系统职责")]),
    (E, "semantic", "为什么不应让 PostToolUse 在每次改文件后都运行全套测试？", "高频 Hook 应执行轻量快速的局部检查，全套完成条件更适合任务结束时核对。", ["supporting_evidence", "same_source_competition", "multi_fact"],
     [(HOOK, 20, 2, "answer", "轻量、快速、局部检查", "直接说明高频局部检查与 Stop 最终验收"), (HOOK, 46, 2, "answer", "会显著拖慢 Agent", "同一高频 Hook 不宜全量测试的直接理由"), (HOOK, 19, 1, "event_examples", "修改代码后运行局部测试", "提供两个事件的适用场景，但未解释频率成本")]),
    (E, "semantic", "自动生成了服务知识更新候选后，为什么高风险内容仍要人工确认语义？", "工具只能发现变化和防止遗漏，API 契约、状态机等是否改变含义需由人判断。", ["same_source_competition"],
     [(ARCH, 5, 2, "answer", "高风险知识仍人工确认语义", "直接说明高风险语义确认边界"), (ARCH, 26, 2, "answer", "人工仍负责确认语义", "原文同一判断的完整展开")]),
    (E, "semantic", "设计多个 Hook 时，为什么不应把每条项目规则都做成 Hook，能用代码判断的规则又该用什么？", "只把不能靠模型自觉执行的少数检查自动化；普通规则留在项目说明，确定性程序可判断时无需额外模型。", ["supporting_evidence", "same_source_competition", "multi_fact"],
     [(HOOK, 46, 2, "answer", "所有规则都做成 Hook", "直接给出 Hook 数量、普通规则和确定性程序的取舍"), (HOOK, 1, 1, "mechanism_roles", "项目长期说明书", "仅补充说明书与 Hook 的角色边界")]),
    (E, "lexical", "架构设计里的“订单”一词可能分别指哪四类单据？", "交易订单、支付单、配送单和对账单。", ["cross_domain_decoy", "same_source_competition", "multi_component_answer"],
     [(ARCH, 1, 2, "answer", "交易/支付/配送/对账", "直接列出四类订单含义"), (ARCH, 15, 2, "answer", "交易订单、支付单、配送单或对账单", "原文同一歧义事实")]),
    (E, "lexical", "PermissionRequest 与 PreToolUse 是同一个 Hook 事件吗？前者发生在什么节点？", "不是；PermissionRequest 发生在 Agent 准备向用户请求权限时，PreToolUse 在工具执行前检查。", ["supporting_evidence", "same_source_competition", "multi_fact"],
     [(HOOK, 18, 2, "answer", "不完全是一回事", "直接区分 PermissionRequest 与 PreToolUse"), (HOOK, 4, 1, "pretool_timing", "工具执行之前", "只支持 PreToolUse 的时点，未解释权限请求")]),
    (E, "lexical", "为什么应在 PreToolUse 做危险工具操作拦截，而不是等 PostToolUse？", "PreToolUse 在工具执行前可阻止副作用；PostToolUse 时工具已经执行，只能反馈和修正。", ["supporting_evidence", "same_source_competition", "multi_fact"],
     [(HOOK, 8, 2, "answer", "无法撤销", "直接比较两个事件的阻止效果"), (HOOK, 23, 2, "answer", "不能阻止修改发生", "同一执行前拦截事实的文件保护案例"), (HOOK, 4, 1, "timing", "工具执行之后", "仅提供事件发生阶段")]),
    (E, "lexical", "技术方案设计 Agent Runtime 的五层分别是什么？", "业务理解、系统分析、架构推理、服务知识、事实验证。", ["multi_component_answer"],
     [(ARCH, 8, 2, "answer", "Agent Runtime 五层结构", "同段完整列出五层"), (ARCH, 35, 2, "answer", "自顶向下，当前 Agent Runtime", "原文逐层说明同一五层结构")]),
    (E, "confusing", "Hook 由运行环境自动触发，是否意味着脚本不会报错、超时，内部模型判断也一定确定？", "否；触发较确定，但脚本仍可能报错或超时，内部模型判断仍有不确定性。", ["same_source_competition"],
     [(HOOK, 0, 2, "answer", "脚本报错、超时", "直接说明确定性边界")]),
    (E, "confusing", "配置 Hook 后，能否把它当作唯一安全边界，取代 Permission 和 Sandbox？", "不能；Hook 适合动态检查，权限和沙箱才是实际访问与执行边界。", ["same_source_competition"],
     [(HOOK, 1, 2, "answer", "不应该完全替代权限和沙箱", "直接区分 Hook 与权限沙箱"), (HOOK, 43, 2, "answer", "真正不可突破的权限边界", "同一安全边界事实的案例说明")]),
    (E, "confusing", "service-knowledge 必须与代码放在同一个仓库，Agent 才能使用吗？", "不必；可由 agent runtime、Harness 或 Skill 关联。", ["same_source_competition"],
     [(ARCH, 6, 2, "answer", "不一定要放同一 repo", "直接说明不要求同仓库"), (ARCH, 27, 2, "answer", "完全可以和代码分开", "原文对同仓库问题的同一回答")]),
    (E, "confusing", "方案覆盖需求后，Agent 可以自己决定跨团队承诺和高风险变更授权吗？", "不能；此类业务和高风险决策需人工介入，Agent 应登记问题而非猜测。", ["same_source_competition"],
     [(ARCH, 9, 2, "answer", "跨团队承诺", "直接列出人工介入点")]),
]


def build() -> None:
    chunks = {}
    for domain in (A, E):
        path = INDEX / domain / "chunks.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            key = (domain, row["source"], row["chunk_index"])
            assert key not in chunks, key
            chunks[key] = row
    counts = Counter((domain, kind) for domain, kind, *_ in CASES)
    assert len(CASES) == 24
    assert counts == {(domain, kind): 4 for domain in (A, E) for kind in ("semantic", "lexical", "confusing")}
    queries, qrels, groups, sheet = [], [], [], ["# Cross-domain sparse challenge candidate review", "", "Engineering/model-selection challenge; not unseen Holdout. Source-first, no retrieval rankings were used.", ""]
    serial = Counter()
    for domain, kind, question, answer, tags, refs in CASES:
        serial[domain] += 1
        query_id = f"challenge_{'apple' if domain == A else 'agent'}_{serial[domain]:03d}"
        assert tags and all(tag in {"cross_domain_decoy", "same_source_competition", "supporting_evidence", "multi_fact", "multi_component_answer"} for tag in tags)
        queries.append({"query_id": query_id, "query": question, "domain": domain, "kind": kind, "mechanism_tags": tags, "status": "proposed"})
        sheet.extend([f"## {query_id} · {kind}", "", f"问题：{question}", "", f"答案要点：{answer}", "", f"机制标签：{', '.join(tags)}", "", "| Grade | Fact group | Source chunk | Judgment |", "|---:|---|---|---|"])
        grouped = defaultdict(list)
        for source, index, grade, fact_group, anchor, judgment in refs:
            chunk = chunks[domain, source, index]
            assert anchor in chunk["content"], (query_id, source, index, anchor)
            assert grade in (1, 2)
            qrels.append({"query_id": query_id, "chunk_id": chunk["chunk_id"], "domain": domain, "relevance": grade, "rationale": judgment, "source": source, "heading_path": chunk["heading_path"], "judgment_source": "source_first_candidate"})
            grouped[fact_group].append((chunk, grade, judgment))
            sheet.append(f"| {grade} | {fact_group} | `{source}` #{index} `{chunk['chunk_id']}` | {judgment} |")
        for fact_group, members in grouped.items():
            grades = {grade for _, grade, _ in members}
            assert len(grades) == 1, (query_id, fact_group)
            canonical = members[0][0]["chunk_id"]
            groups.append({"query_id": query_id, "group_id": f"{query_id}:{canonical}", "canonical_chunk_id": canonical, "relevance": members[0][1], "member_chunk_ids": [chunk["chunk_id"] for chunk, _, _ in members], "members": [{"chunk_id": chunk["chunk_id"], "relevance": grade, "role": "canonical" if i == 0 else "equivalent_alias"} for i, (chunk, grade, _) in enumerate(members)], "rationale": "; ".join(dict.fromkeys(judgment for _, _, judgment in members))})
        sheet.append("")
    assert len({row["query"] for row in queries}) == 24
    assert len({(row["query_id"], row["chunk_id"]) for row in qrels}) == len(qrels)
    OUT.mkdir(parents=True, exist_ok=True)
    for filename, rows in (("proposed_queries.jsonl", queries), ("proposed_qrels.jsonl", qrels), ("qrel_groups.jsonl", groups)):
        (OUT / filename).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    (OUT / "review_sheet.md").write_text("\n".join(sheet) + "\n", encoding="utf-8")
    print(f"candidate queries={len(queries)} qrels={len(qrels)} fact_groups={len(groups)}")


if __name__ == "__main__":
    build()
