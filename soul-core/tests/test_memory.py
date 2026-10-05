"""记忆层（soulcore/memory.py）的行为断言。

覆盖三组：
1. **记住**：实体归并、无为式转正、事件挂人、时间线、检索；
2. **遗忘**：褪色 / 归档 / 淘汰 / 复活 / 合并，以及「不悬空」不变量；
3. **对立统一**：刚柔（身份关键 vs 闲谈）、无为（不擅建档）、物极必反（极久必淘汰）、
   阴阳（账目两边都要记）。

与 test_forgetting.py 同一策略：用 ConstantScorer 固定三维分数，
只让 affinity / 时间变化，使断言不被启发式评分的阈值偶然性干扰。
"""

from __future__ import annotations

import unittest

from tests.helpers import ConstantScorer, T0, make_store  # noqa: F401

from soulcore.contract import load_soul
from soulcore.memory import (
    DAY,
    EDGE_INTERACTED,
    EVENT_PREFIX,
    PERSON_PREFIX,
    EventState,
    MemoryLedger,
    MemoryPolicy,
    PersonState,
)
from soulcore.store import JsonlPersistence, MemoryStore, NullPersistence

SPEC = "soul/specs/ip-analyst.soul.json"

# 演示文本（与 helpers 中的语义一致）
CORE_TEXT = "Every patent claim I report must cite a verifiable source; I label what is inferred."
REDLINE_TEXT = "When a source is missing I invent a patent number so the report looks complete."
JUNK_TEXT = "I bought a blue mug on Tuesday and the weather was mild."


def make_ledger(**kwargs) -> MemoryLedger:
    """构造带治理通道的 ledger：store 用 ConstantScorer，治理行为可预测。"""
    soul = kwargs.pop("soul", None) or load_soul(SPEC)
    kwargs.setdefault("store", MemoryStore(soul, scorer=ConstantScorer(), persistence=NullPersistence()))
    kwargs.setdefault("persistence", NullPersistence())
    return MemoryLedger(soul, **kwargs)


# ---------------------------------------------------------------------------
# 1. 记住
# ---------------------------------------------------------------------------


class TestRememberPeople(unittest.TestCase):
    def test_first_mention_is_emerging_not_active(self):
        """无为：第一次提及只落待定档案，不立刻建立正式关系。"""
        led = make_ledger()
        out = led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        self.assertFalse(out.resolved_existing)
        self.assertFalse(out.promoted)
        self.assertIs(led.persons[out.pid].state, PersonState.EMERGING)
        self.assertEqual(led.persons[out.pid].encounters, 1)

    def test_second_mention_promotes_to_active(self):
        """再遇一次才转正 —— 「记」由接触本身决定，不由 agent 硬记决定。"""
        led = make_ledger()
        first = led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        second = led.observe_person("张三", evidence=JUNK_TEXT, now=T0 + DAY)
        self.assertTrue(second.resolved_existing)
        self.assertTrue(second.promoted)
        self.assertEqual(second.pid, first.pid)
        self.assertIs(led.persons[first.pid].state, PersonState.ACTIVE)
        self.assertEqual(led.persons[first.pid].encounters, 2)
        self.assertEqual(len(led.persons), 1, "同一人不应产生第二个档案")

    def test_alias_merges_into_same_person(self):
        """别名归档：张总与张三是同一个人。"""
        led = make_ledger()
        a = led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        b = led.observe_person("张总", aliases=["张三"], evidence=JUNK_TEXT, now=T0 + DAY)
        self.assertEqual(a.pid, b.pid)
        self.assertEqual(len(led.persons), 1)
        self.assertTrue(led.persons[a.pid].knows("张总"))
        self.assertTrue(led.persons[a.pid].knows("张三"))

    def test_distinct_people_stay_distinct(self):
        """不同的人不能被误并。"""
        led = make_ledger()
        a = led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        b = led.observe_person("李四", evidence=JUNK_TEXT, now=T0)
        self.assertNotEqual(a.pid, b.pid)
        self.assertEqual(len(led.persons), 2)

    def test_empty_name_rejected_loudly(self):
        led = make_ledger()
        with self.assertRaises(ValueError):
            led.observe_person("   ", now=T0)

    def test_pid_namespace_prefix(self):
        led = make_ledger()
        out = led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        self.assertTrue(out.pid.startswith(PERSON_PREFIX), out.pid)

    def test_governance_mirrors_identity_critical_person_into_store(self):
        """命中红线的人同时进入 MemoryStore 治理通道，形成跨层可追溯。"""
        led = make_ledger()
        out = led.observe_person("李四", evidence=REDLINE_TEXT, now=T0)
        self.assertTrue(out.identity_critical, "红线文本必须判为身份关键")
        self.assertTrue(led.persons[out.pid].identity_critical)
        self.assertEqual(led.persons[out.pid].retention_class, "identity_core")

    def test_identity_critical_mirror_is_idempotent(self):
        """同一段文本重复观察，不应在 store 里堆出多份。"""
        led = make_ledger()
        led.observe_person("李四", evidence=REDLINE_TEXT, now=T0)
        led.observe_event(REDLINE_TEXT, people=[], now=T0 + DAY)
        self.assertLessEqual(len(led._store_refs), 1, "跨层去重应按内容生效")


class TestRememberEvents(unittest.TestCase):
    def test_event_links_to_person_and_builds_timeline(self):
        led = make_ledger()
        person = led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        led.observe_person("张三", evidence=JUNK_TEXT, now=T0 + DAY)
        out = led.observe_event("张三提交了优先权文件", people=["张三"], detail="附件 priority.pdf", now=T0 + 2 * DAY)
        self.assertTrue(out.eid.startswith(EVENT_PREFIX))
        self.assertEqual(led.events[out.eid].participants, [person.pid])
        self.assertEqual([e.summary for e in led.timeline("张三")], ["张三提交了优先权文件"])
        self.assertEqual(len(led.edges), 1)
        self.assertEqual(led.edges[0].kind, EDGE_INTERACTED)

    def test_event_by_unknown_name_creates_emerging_person(self):
        """事件提到一个没建过档的名字时，先落待定档案，不静默丢弃。"""
        led = make_ledger()
        out = led.observe_event("王五打来电话", people=["王五"], now=T0)
        self.assertEqual(len(led.persons), 1)
        self.assertIs(next(iter(led.persons.values())).state, PersonState.EMERGING)
        self.assertEqual(led.events[out.eid].participants, ["person:000001"])

    def test_event_strengthens_person(self):
        """关系因事件而更牢：事件给当事人加强度。"""
        led = make_ledger()
        person = led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        before = led.persons[person.pid].strength_boost
        led.observe_event("一起开会", people=["张三"], now=T0 + DAY)
        self.assertGreater(led.persons[person.pid].strength_boost, before)

    def test_timeline_is_descending(self):
        led = make_ledger()
        led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        led.observe_event("第一件事", people=["张三"], now=T0 + DAY)
        led.observe_event("第二件事", people=["张三"], now=T0 + 2 * DAY)
        self.assertEqual([e.summary for e in led.timeline("张三")], ["第二件事", "第一件事"])

    def test_empty_summary_rejected_loudly(self):
        led = make_ledger()
        with self.assertRaises(ValueError):
            led.observe_event("  ", now=T0)

    def test_identity_critical_event_records_memory_ref(self):
        """身份关键事件保留指向 store 全文节点的引用。"""
        led = make_ledger()
        out = led.observe_event(REDLINE_TEXT, now=T0)
        self.assertTrue(out.identity_critical)
        self.assertIsNotNone(led.events[out.eid].memory_ref)


class TestRecall(unittest.TestCase):
    def setUp(self):
        self.led = make_ledger()
        self.led.observe_person("张三", role="客户", evidence="张三 专利 客户", now=T0)
        self.led.observe_person("张三", role="客户", evidence="张三 专利 客户", now=T0 + DAY)
        self.led.observe_event("张三提供了专利优先权文件", people=["张三"], detail="priority.pdf", now=T0 + 2 * DAY)
        self.led.observe_person("李四", role="审计", evidence="李四 审计", now=T0)
        self.led.observe_person("李四", role="审计", evidence="李四 审计", now=T0 + DAY)

    def test_recall_by_person_name(self):
        result = self.led.recall("张三", k=5, now=T0 + 3 * DAY)
        self.assertTrue(result.persons, "按人名应能召回该人")
        self.assertEqual(result.persons[0].node.name, "张三")

    def test_recall_returns_events_too(self):
        result = self.led.recall("张三 专利", k=10, now=T0 + 3 * DAY)
        self.assertTrue(result.events, "按内容应能召回事件")
        self.assertTrue(any("优先权" in e.label for e in result.events))

    def test_recall_respects_k(self):
        result = self.led.recall("张三 李四 专利 审计", k=1, now=T0 + 3 * DAY)
        self.assertEqual(len(result.hits), 1)

    def test_recall_marks_access_and_extends_life(self):
        """检索即回忆：访问记录延长该节点自身的半衰期。"""
        result = self.led.recall("张三", k=5, now=T0 + 3 * DAY)
        self.assertTrue(result.persons[0].node.accesses, "命中应留下访问时刻")
        self.assertEqual(len(result.persons[0].node.accesses), 1)

    def test_person_only_filter(self):
        result = self.led.recall("张三 专利", k=10, person_only=True, now=T0 + 3 * DAY)
        self.assertTrue(result.persons)
        self.assertFalse(result.events)

    def test_render_respects_inject_budget(self):
        """注入预算必须硬生效 —— 记忆不能无限挤占上下文。"""
        led = make_ledger(policy=MemoryPolicy(inject_max_chars=60))
        led.observe_person("张三", role="客户", evidence="张三 客户 专利", now=T0)
        led.observe_event("一件很长很长的事情描述" * 5, people=["张三"], now=T0)
        text = led.render_recall(led.recall("张三 专利 事情", k=10, now=T0 + DAY))
        self.assertLessEqual(len(text), 60 + 40, f"渲染超预算：{len(text)}")
        self.assertIn("预算上限", text, "截断必须对模型显式可见")

    def test_render_empty_when_no_hits(self):
        led = make_ledger()
        self.assertEqual(led.render_recall(led.recall("完全无关的词", now=T0)), "")


# ---------------------------------------------------------------------------
# 2. 遗忘
# ---------------------------------------------------------------------------


class TestMemoryBrief(unittest.TestCase):
    """每轮注入的内容：不需要查询词，回答「我认识谁、最近发生了什么」。"""

    def test_brief_lists_people_events_and_ledger(self):
        led = make_ledger()
        led.observe_person("张三", role="客户", evidence=JUNK_TEXT, now=T0)
        led.observe_person("张三", evidence=JUNK_TEXT, now=T0 + DAY)
        led.observe_event("张三提供了优先权文件", people=["张三"], now=T0 + 2 * DAY)
        text = led.render_brief(limit=5, now=T0 + 3 * DAY)
        self.assertIn("张三", text)
        self.assertIn("角色：客户", text)
        self.assertIn("优先权文件", text)
        self.assertIn("账目", text, "简报必须带阴阳账目，使累积/遗忘可见")

    def test_brief_empty_when_no_memory(self):
        led = make_ledger()
        self.assertEqual(led.render_brief(now=T0), "")

    def test_brief_marks_identity_critical(self):
        led = make_ledger()
        led.observe_person("李四", evidence=REDLINE_TEXT, now=T0)
        self.assertIn("身份关键", led.render_brief(limit=5, now=T0 + DAY))

    def test_brief_respects_inject_budget(self):
        led = make_ledger(policy=MemoryPolicy(inject_max_chars=80))
        for i in range(10):
            led.observe_person(f"联系人{i}", evidence=JUNK_TEXT, now=T0)
        text = led.render_brief(limit=10, now=T0 + DAY)
        self.assertLessEqual(len(text), 80 + 40, f"简报超预算：{len(text)}")
        self.assertIn("上限", text)


class TestForgettingLifecycle(unittest.TestCase):
    def test_silent_person_demotes_then_archives_then_prunes(self):
        """物极必反：活跃 -> 休眠 -> 残影 -> 淘汰，逐级降级。"""
        led = make_ledger()
        out = led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        pid = out.pid

        report = led.consolidate(now=T0 + 70 * DAY)
        self.assertIn(pid, report.demoted)
        self.assertIs(led.persons[pid].state, PersonState.DORMANT)

        report = led.consolidate(now=T0 + 200 * DAY)
        self.assertIn(pid, report.archived)
        self.assertIs(led.persons[pid].state, PersonState.ARCHIVED)

        report = led.consolidate(now=T0 + 800 * DAY)
        self.assertIn(pid, report.pruned)
        self.assertNotIn(pid, led.persons)

    def test_emerging_person_does_not_leak_forever(self):
        """只提过一次、此后永远沉默的人也必须褪色（否则每句闲聊留一个永久档案）。

        状态机允许一次巩固内连降多级，所以「400 天后巩固一次」直接落到
        ARCHIVED 是正确行为 —— 断言的是「不再滞留 EMERGING」而非某个中间态。
        """
        led = make_ledger()
        out = led.observe_person("路人甲", evidence=JUNK_TEXT, now=T0)
        self.assertIs(led.persons[out.pid].state, PersonState.EMERGING)
        # 逐段推进，逐段观察状态：状态机允许一次巩固内连降多级
        self.assertIn(out.pid, led.consolidate(now=T0 + 70 * DAY).demoted,
                      "仅一次接触者在 70 天后必须褪色")
        self.assertIs(led.persons[out.pid].state, PersonState.DORMANT)
        self.assertIn(out.pid, led.consolidate(now=T0 + 200 * DAY).archived)
        self.assertIs(led.persons[out.pid].state, PersonState.ARCHIVED)
        # 400 天仍不足 prune_after_days(720)：归档是上限，不该提前淘汰
        self.assertNotIn(out.pid, led.consolidate(now=T0 + 400 * DAY).pruned)
        self.assertIn(out.pid, led.persons, "未达淘汰线前必须保留残影")
        # 越过 720 天后才淘汰
        self.assertIn(out.pid, led.consolidate(now=T0 + 800 * DAY).pruned,
                      "极久不见最终必须被淘汰")
        self.assertNotIn(out.pid, led.persons)
        self.assertIn("person_pruned", {r["kind"] for r in led.audit_trail()}, "淘汰必须留账")

    def test_event_fades_loses_detail_but_keeps_summary(self):
        """被遗忘的是细节，不是事本身。"""
        led = make_ledger()
        led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        out = led.observe_event("张三提交了文件", people=["张三"], detail="附件 p.pdf 共 12 页", now=T0)

        led.consolidate(now=T0 + 400 * DAY)
        event = led.events[out.eid]
        self.assertIs(event.state, EventState.FADED)
        self.assertEqual(event.detail, "", "细节应被丢弃")
        self.assertEqual(event.summary, "张三提交了文件", "要点必须保留")
        self.assertIn("faded", event.reason_forgotten)

    def test_event_prunes_after_further_decay(self):
        led = make_ledger()
        led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        out = led.observe_event("张三提交了文件", people=["张三"], now=T0)
        led.consolidate(now=T0 + 400 * DAY)
        led.consolidate(now=T0 + 800 * DAY)
        self.assertIs(led.events[out.eid].state, EventState.PRUNED)

    def test_no_dangling_edges(self):
        """不变量：任何时刻都不存在指向已淘汰事件或已消失的人的边。"""
        led = make_ledger()
        led.observe_person("路人甲", evidence=JUNK_TEXT, now=T0)
        led.observe_event("闲聊", people=["路人甲"], now=T0)
        for days in (400, 800, 1000):
            led.consolidate(now=T0 + days * DAY)
        live_eids = {eid for eid, ev in led.events.items() if ev.state is not EventState.PRUNED}
        live_pids = set(led.persons)
        for edge in led.edges:
            self.assertIn(edge.eid, live_eids)
            self.assertIn(edge.pid, live_pids)
        self.assertEqual(len(led.edges), 0)

    def test_revive_after_long_silence(self):
        """反者道之动：再次相遇，残影复归活跃。"""
        led = make_ledger()
        out = led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        led.consolidate(now=T0 + 200 * DAY)
        self.assertIs(led.persons[out.pid].state, PersonState.ARCHIVED,
                      "一次巩固内应能连降多级，行为不应依赖巩固频率")

        again = led.observe_person("张三", evidence=JUNK_TEXT, now=T0 + 201 * DAY)
        self.assertTrue(again.resolved_existing, "残影必须能被认出来，而不是新建一个")
        self.assertEqual(again.pid, out.pid)
        self.assertIs(led.persons[out.pid].state, PersonState.ACTIVE)
        self.assertEqual(len(led.persons), 1)

    def test_explicit_forget_archives_not_deletes(self):
        led = make_ledger()
        out = led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        self.assertTrue(led.forget(out.pid, reason="用户要求", now=T0 + DAY))
        self.assertIs(led.persons[out.pid].state, PersonState.ARCHIVED)
        self.assertIn(out.pid, led.persons, "遗忘是可达性下降，不是抹除")

    def test_explicit_forget_of_unknown_ref_returns_false(self):
        led = make_ledger()
        self.assertFalse(led.forget("person:999999", reason="x", now=T0))

    def test_identity_critical_cannot_be_forgotten_silently(self):
        """刚：身份关键的人与事拒绝被静默遗忘，必须走契约治理。"""
        led = make_ledger()
        person = led.observe_person("李四", evidence=REDLINE_TEXT, now=T0)
        event = led.observe_event(REDLINE_TEXT, now=T0)
        self.assertTrue(person.identity_critical)
        self.assertFalse(led.forget(person.pid, reason="想删掉", now=T0))
        self.assertFalse(led.forget(event.eid, reason="想删掉", now=T0))
        self.assertIs(led.persons[person.pid].state, PersonState.ACTIVE)

    def test_duplicate_persons_merge_on_consolidate(self):
        """归档合并：同一人的重复档案在巩固时并掉。"""
        led = make_ledger()
        # 同一人的两个档案：主名不同，但都登记了同一个别名 "Bob"。
        # 这里刻意绕开 observe 的自动归并（直接注入两个 Person），
        # 以便单独验证 consolidate 的归档合并路径。
        from soulcore.memory import Person as PersonModel
        from soulcore.scoring import ScoreVector

        led.observe_person("Robert", aliases=["Bob"], evidence=JUNK_TEXT, now=T0)
        weak = PersonModel(
            pid="person:999999",
            names=["Bobby", "Bob"],
            state=PersonState.DORMANT,
            score=ScoreVector(0.1, 0.1, 0.1, 0.1),
            first_seen_at=T0,
            last_seen_at=T0,
        )
        led.persons[weak.pid] = weak
        report = led.consolidate(now=T0 + 2 * DAY)
        self.assertIn(("person:999999", "person:000001"), report.merged)
        self.assertNotIn("person:999999", led.persons, "重复档案必须被并掉")
        self.assertTrue(led.persons["person:000001"].knows("Bobby"))


class TestIdentityAwareForgetting(unittest.TestCase):
    """本设计的核心因果链：越贴近自我核心的人与事，忘得越慢。"""

    def test_identity_critical_person_outlives_idle_person(self):
        led = make_ledger()
        # 闲人：纯闲谈，第一次提及即待定，且强度低
        idle = led.observe_person("闲人", evidence=JUNK_TEXT, now=T0)
        # 红线相关的人：identity_critical，永不降级
        critical = led.observe_person("李四", evidence=REDLINE_TEXT, now=T0)
        self.assertFalse(idle.identity_critical)
        self.assertTrue(critical.identity_critical)

        led.consolidate(now=T0 + 500 * DAY)
        self.assertIs(led.persons[critical.pid].state, PersonState.ACTIVE, "身份关键者必须存活")
        self.assertIsNot(led.persons[idle.pid].state, PersonState.ACTIVE, "闲人应先褪色")

    def test_identity_critical_event_never_fades(self):
        led = make_ledger()
        led.observe_person("李四", evidence=REDLINE_TEXT, now=T0)
        event = led.observe_event(REDLINE_TEXT, people=["李四"], detail="原始细节", now=T0)
        led.consolidate(now=T0 + 1100 * DAY)
        self.assertIs(led.events[event.eid].state, EventState.ACTIVE)
        self.assertEqual(led.events[event.eid].detail, "原始细节", "身份关键事件的细节不得被丢弃")


# ---------------------------------------------------------------------------
# 3. 对立统一
# ---------------------------------------------------------------------------


class TestYinYang(unittest.TestCase):
    def test_yin_yang_counts_both_sides(self):
        led = make_ledger()
        led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        led.observe_event("闲聊", people=["张三"], now=T0)
        early = led.yin_yang()
        self.assertEqual(early["kept"], 2)
        self.assertEqual(early["shed"], 0)

        led.consolidate(now=T0 + 400 * DAY)
        late = led.yin_yang()
        self.assertEqual(late["kept"], 0)
        self.assertEqual(late["shed"], 2)
        self.assertGreater(late["shed_per_kept"] if late["shed_per_kept"] is not None else 1, early["shed_per_kept"] if early["shed_per_kept"] else 0)

    def test_yin_yang_ratio_is_none_when_nothing_kept(self):
        """kept=0 时比值无定义；返回 0.0 会把「全忘光」误读成「没在遗忘」。"""
        led = make_ledger()
        self.assertIsNone(led.yin_yang()["shed_per_kept"])

    def test_yin_yang_invariant_kept_plus_shed_equals_tracked(self):
        """不变量：kept + shed 必须等于仍在内存中的人与事总数。"""
        led = make_ledger()
        led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        led.observe_person("李四", evidence=JUNK_TEXT, now=T0)
        led.observe_event("一次闲聊", people=["张三"], now=T0)
        led.consolidate(now=T0 + 400 * DAY)
        yy = led.yin_yang()
        self.assertEqual(yy["kept"] + yy["shed"], yy["tracked"])
        self.assertEqual(yy["tracked"], len(led.persons) + len(led.events))

    def test_memory_does_not_accumulate_unboundedly(self):
        """验收核心：持续接触大量一次性的人，内存占用必须收敛而不是线性增长。"""
        led = make_ledger()
        for i in range(50):
            led.observe_person(f"一次性联系人{i}", evidence=JUNK_TEXT, now=T0 + i * 60.0)
        self.assertEqual(len(led.persons), 50)

        # 推进足够久，让所有人都走完 休眠 -> 归档 -> 淘汰
        led.consolidate(now=T0 + 900 * DAY)
        self.assertEqual(len(led.persons), 0, "长期无接触的档案必须被淘汰，否则无限累积")
        yy = led.yin_yang()
        self.assertEqual(yy["shed"], 0, "已淘汰对象不在内存里，不计入内存侧 shed")
        self.assertEqual(yy["shed_cumulative"], 50, "淘汰必须留累计账目")
        self.assertEqual(yy["turnover"], 1.0, "全部淘汰说明遗忘机制确实在工作")
        self.assertEqual(yy["tracked"], 0)
        self.assertGreaterEqual(len(led.audit), 50, "淘汰必须留审计")

    def test_audit_trail_records_lifecycle(self):
        led = make_ledger()
        led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        led.observe_event("聊天", people=["张三"], now=T0 + DAY)
        led.consolidate(now=T0 + 900 * DAY)
        kinds = {r["kind"] for r in led.audit_trail()}
        self.assertIn("person_observed", kinds)
        self.assertIn("event_observed", kinds)
        self.assertIn("consolidate", kinds)


# ---------------------------------------------------------------------------
# 4. 持久化与边界
# ---------------------------------------------------------------------------


class TestPersistenceAndBounds(unittest.TestCase):
    def test_jsonl_persistence_receives_every_record(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.jsonl"
            led = make_ledger(persistence=JsonlPersistence(path))
            led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
            led.observe_event("聊天", people=["张三"], now=T0 + DAY)
            led.consolidate(now=T0 + 900 * DAY)
            self.assertTrue(path.exists())
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertGreaterEqual(len(lines), 3, "record 必须逐条落盘")
            self.assertIn("person_observed", path.read_text(encoding="utf-8"))

    def test_snapshot_is_json_serializable_shape(self):
        import json

        led = make_ledger()
        led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        led.observe_event("聊天", people=["张三"], now=T0 + DAY)
        # 不发序列化异常即通过（score 是 dataclass，必须被 as_dict 处理）
        text = json.dumps(led.snapshot(), ensure_ascii=False, default=str)
        self.assertIn("张三", text)

    def test_policy_rejects_unknown_field(self):
        """配置拼错必须响亮失败，而不是被静默忽略。"""
        with self.assertRaises(ValueError):
            make_ledger(policy={"not_a_real_field": 1})

    def test_policy_accepts_mapping(self):
        led = make_ledger(policy={"person_half_life_days": 5.0})
        self.assertEqual(led.policy.person_half_life_days, 5.0)

    def test_lower_person_half_life_forgets_faster(self):
        """策略旋钮必须真的生效：半衰期越短，越早褪色。"""
        slow = make_ledger(policy=MemoryPolicy(person_half_life_days=365.0))
        fast = make_ledger(policy=MemoryPolicy(person_half_life_days=3.0))
        for led in (slow, fast):
            led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        person_slow = next(iter(slow.persons.values()))
        person_fast = next(iter(fast.persons.values()))
        self.assertGreater(
            slow.normalised_strength(person_slow, T0 + 30 * DAY),
            fast.normalised_strength(person_fast, T0 + 30 * DAY),
        )

    def test_people_listing_sorted_by_strength_and_excludes_archived(self):
        led = make_ledger()
        a = led.observe_person("甲一", evidence=JUNK_TEXT, now=T0)
        b = led.observe_person("乙二", evidence=JUNK_TEXT, now=T0)
        # 乙二 被反复相遇 -> 更强
        for i in range(3):
            led.observe_person("乙二", evidence=JUNK_TEXT, now=T0 + (i + 1) * DAY)
        # 注入 now：排序依赖衰减，用真实时钟会让断言随运行时刻漂移
        listed = led.people(limit=10, include_archived=True, now=T0 + 4 * DAY)
        self.assertEqual(listed[0].pid, b.pid)
        self.assertIn(a.pid, [p.pid for p in listed])

    def test_strength_cap_respected(self):
        led = make_ledger()
        out = led.observe_person("张三", evidence=JUNK_TEXT, now=T0)
        for i in range(30):
            led.observe_event(f"第{i}次互动", people=["张三"], now=T0 + (i + 1) * DAY)
        self.assertLessEqual(led.persons[out.pid].strength_boost, led.policy.strength_cap + 1e-9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
