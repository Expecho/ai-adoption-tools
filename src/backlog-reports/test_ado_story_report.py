import datetime as dt
import unittest

import ado_story_report as a
import story_config as sc

UTC = dt.timezone.utc


def upd(rev, when, new_state=None):
    f = {"System.ChangedDate": {"newValue": when}}
    if new_state:
        f["System.State"] = {"newValue": new_state}
    return {"rev": rev, "fields": f}


CATS = {"New": "Proposed", "Approved": "Proposed", "Committed": "InProgress", "Testable": "InProgress", "Done": "Completed"}


def wi(created="2026-01-01T00:00:00Z", relations=None):
    return {"id": 7, "fields": {"System.CreatedDate": created, "System.Title": "t", "System.Tags": "a; b"},
            "relations": relations or []}


class TimelineTests(unittest.TestCase):
    def build(self, updates, created="2026-01-01T00:00:00Z"):
        events = a.state_timeline(created, updates)
        return a.build_story(wi(created), events, "https://dev.azure.com/o", CATS)

    def test_time_per_state_and_phases(self):
        s = self.build([upd(1, "2026-01-01T00:00:00Z", "New"), upd(2, "2026-01-03T00:00:00Z", "Approved"),
                        upd(3, "2026-01-04T00:00:00Z", "Committed"), upd(4, "2026-01-06T12:00:00Z", "Testable"),
                        upd(5, "2026-01-07T12:00:00Z", "Done")])
        self.assertEqual(s.days_in_state, {"New": 2.0, "Approved": 1.0, "Committed": 2.5, "Testable": 1.0})
        self.assertEqual(s.phase_days, {"created": 3.0, "committed": 3.5})
        self.assertEqual(s.lead_days, 6.5)
        self.assertEqual(s.done[:10], "2026-01-07")
        self.assertFalse(s.reopened)
        self.assertEqual(s.tags, ["a", "b"])

    def test_non_state_updates_are_ignored_and_reopen_adds_up(self):
        s = self.build([upd(1, "2026-01-01T00:00:00Z", "New"), upd(2, "2026-01-02T00:00:00Z"),
                        upd(3, "2026-01-02T00:00:00Z", "Committed"), upd(4, "2026-01-03T00:00:00Z", "Done"),
                        upd(5, "2026-01-04T00:00:00Z", "Committed"), upd(6, "2026-01-06T00:00:00Z", "Done")])
        self.assertEqual(s.days_in_state["Committed"], 3.0)
        self.assertTrue(s.reopened)
        self.assertEqual(s.done[:10], "2026-01-06")

    def test_not_currently_done_is_skipped(self):
        self.assertIsNone(self.build([upd(1, "2026-01-01T00:00:00Z", "New"), upd(2, "2026-01-02T00:00:00Z", "Committed")]))

    def test_parse_ts_handles_fractions(self):
        self.assertEqual(a.parse_ts("2026-10-01T07:51:41.5Z"), dt.datetime(2026, 10, 1, 7, 51, 41, 500000, tzinfo=UTC))
        self.assertEqual(a.parse_ts("2026-10-01T07:51:41.1234567+00:00").microsecond, 123456)


def link(guid, n, created):
    return {"url": f"vstfs:///GitHub/PullRequest/{guid}%2f{n}", "created": created}


def pr(repo, n, created, ai):
    return {"url": f"https://github.com/o/{repo}/pull/{n}", "repo": f"o/{repo}", "number": n,
            "created_at": created, "ai": ai, "source": "github"}


class LinkTests(unittest.TestCase):
    def setUp(self):
        prs = [pr("A", 5, "2026-02-01T10:00:00Z", False), pr("B", 5, "2026-03-01T10:00:00Z", True),
               pr("B", 9, "2026-03-05T10:00:00Z", True), pr("C", 9, "2026-04-05T10:00:00Z", False)]
        self.prs = {a.norm_url(p["url"]): p for p in prs}

    def story(self, *links):
        s = a.Story(id=1, title="t", url="u", created="2026-01-01T00:00:00+00:00", done="2026-05-01T00:00:00+00:00")
        s.pr_links = list(links)
        return s

    def test_number_plus_creation_time_picks_the_repo(self):
        s = self.story(link("a1", 5, "2026-03-01T10:00:03Z"))
        a.resolve_links([s], self.prs)
        self.assertEqual(s.pr_matched, ["https://github.com/o/B/pull/5"])

    def test_guid_learned_from_one_link_resolves_another(self):
        s1 = self.story(link("a1", 5, "2026-03-01T10:00:03Z"))
        s2 = self.story(link("a1", 9, None))   # no date: ambiguous by number, resolved through the guid
        a.resolve_links([s1, s2], self.prs)
        self.assertEqual(s2.pr_matched, ["https://github.com/o/B/pull/9"])

    def test_ambiguous_and_unknown_links_stay_unmatched(self):
        s = self.story(link("b2", 9, None), link("c3", 77, "2026-03-01T10:00:00Z"))
        a.resolve_links([s], self.prs)
        self.assertEqual(s.pr_matched, [])

    def test_plain_urls_match_directly(self):
        s = self.story({"url": "https://github.com/o/A/pull/5/", "created": None})
        a.resolve_links([s], self.prs)
        self.assertEqual(len(s.pr_matched), 1)

    def test_pr_links_extracts_only_pr_relations(self):
        rels = [{"rel": "Hyperlink", "url": "https://gitlab.com/g/p/-/merge_requests/3", "attributes": {}},
                {"rel": "ArtifactLink", "url": "vstfs:///GitHub/Commit/x%2fabc", "attributes": {"name": "GitHub Commit"}},
                {"rel": "ArtifactLink", "url": "vstfs:///GitHub/PullRequest/x%2f4",
                 "attributes": {"name": "GitHub Pull Request", "resourceCreatedDate": "2026-01-01T00:00:00Z"}}]
        self.assertEqual([l["url"] for l in a.pr_links(rels)],
                         ["https://gitlab.com/g/p/-/merge_requests/3", "vstfs:///GitHub/PullRequest/x%2f4"])


class ChartDataTests(unittest.TestCase):
    def story(self, done, ai, created=1.0, committed=2.0, lead=5.0):
        s = a.Story(id=1, title="t", url="u", created="2026-01-01T00:00:00+00:00", done=done)
        s.ai, s.phase_days, s.lead_days = ai, {"created": created, "committed": committed}, lead
        return s

    def test_months_are_continuous_and_share_ignores_no_data(self):
        d = a.chart_data([self.story("2026-01-05T00:00:00+00:00", "ai"), self.story("2026-01-06T00:00:00+00:00", "human"),
                          self.story("2026-01-07T00:00:00+00:00", "nodata"), self.story("2026-03-02T00:00:00+00:00", "nodata")])
        self.assertEqual([r["m"] for r in d["months"]], ["2026-01", "2026-02", "2026-03"])
        jan, feb, mar = d["months"]
        self.assertEqual((jan["ai"], jan["human"], jan["nodata"], jan["pct"]), (1, 1, 1, 50.0))
        self.assertEqual((feb["pct"], mar["pct"]), (None, None))   # nothing with PR data: a gap, not 0%
        self.assertEqual(d["phases"]["ai"], {"n": 1, "created": 1.0, "committed": 2.0, "lead": 5.0})

    def test_empty_group_has_no_medians(self):
        d = a.chart_data([self.story("2026-01-05T00:00:00+00:00", "ai")])
        self.assertEqual(d["phases"]["human"], {"n": 0, "created": None, "committed": None, "lead": None})


class RepoStatsTests(unittest.TestCase):
    def story(self, repos, ai_repos, lead):
        s = a.Story(id=1, title="t", url="u", created="2026-01-01T00:00:00+00:00", done="2026-02-01T00:00:00+00:00")
        s.repos, s.ai_repos, s.lead_days = repos, ai_repos, lead
        s.phase_days = {"created": 1.0, "committed": 2.0}
        return s

    def test_story_counts_in_every_repo_and_ai_is_per_repo(self):
        rows = {r["repo"]: r for r in a.repo_stats([
            self.story(["o/A"], ["o/A"], 10), self.story(["o/A"], [], 20), self.story(["o/A", "o/B"], ["o/B"], 30)])}
        self.assertEqual((rows["o/A"]["n"], rows["o/A"]["ai"], rows["o/A"]["human"]), (3, 1, 2))
        self.assertEqual((rows["o/B"]["n"], rows["o/B"]["ai"], rows["o/B"]["human"]), (1, 1, 0))
        self.assertEqual(rows["o/A"]["lead_ai"], 10)
        self.assertEqual(rows["o/A"]["lead_human"], 25)      # median of 20 and 30
        self.assertIsNone(rows["o/B"]["lead_human"])
        self.assertEqual(rows["o/B"]["pct"], 100.0)

    def test_sorted_by_story_count_and_stories_without_repo_ignored(self):
        rows = a.repo_stats([self.story([], [], 5), self.story(["o/B"], [], 1), self.story(["o/A"], [], 1), self.story(["o/A"], [], 1)])
        self.assertEqual([r["repo"] for r in rows], ["o/A", "o/B"])

    def test_labels_drop_shared_prefix_only(self):
        self.assertEqual(a.repo_labels(["o/A", "o/B"]), {"o/A": "A", "o/B": "B"})
        self.assertEqual(a.repo_labels(["o/A", "p/B"]), {"o/A": "o/A", "p/B": "p/B"})


def rel_child(i):
    return {"rel": "System.LinkTypes.Hierarchy-Forward", "url": f"https://dev.azure.com/o/p/_apis/wit/workItems/{i}"}


def rel_pr(guid, n):
    return {"rel": "ArtifactLink", "url": f"vstfs:///GitHub/PullRequest/{guid}%2f{n}",
            "attributes": {"name": "GitHub Pull Request", "resourceCreatedDate": "2026-01-01T00:00:00Z"}}


class FakeAdo:
    """workitemsbatch stub: id -> work item."""
    def __init__(self, items):
        self.items, self.calls = items, []

    def request(self, path, body=None):
        self.calls.append(body["ids"])
        return {"value": [self.items[i] for i in body["ids"] if i in self.items]}


class DescendantTests(unittest.TestCase):
    def test_links_on_child_tasks_are_collected_with_their_origin(self):
        story = {"id": 1, "rev": 3, "fields": {"System.CreatedDate": "2026-01-01T00:00:00Z", "System.Title": "t"},
                 "relations": [rel_child(2), rel_child(3), rel_pr("a1", 10)]}
        items = {2: {"id": 2, "relations": [rel_pr("a1", 11), rel_child(4)]},
                 3: {"id": 3, "relations": []},
                 4: {"id": 4, "relations": [rel_pr("a1", 12), rel_pr("a1", 11)]}}   # grandchild; 11 repeats
        d = a.fetch_descendants(FakeAdo(items), [story])
        self.assertEqual(sorted(w["id"] for w in d[1]), [2, 3, 4])
        events = a.state_timeline("2026-01-01T00:00:00Z", [upd(1, "2026-01-01T00:00:00Z", "New"), upd(2, "2026-01-02T00:00:00Z", "Done")])
        st = a.build_story(story, events, "https://dev.azure.com/o", CATS, d[1])
        got = {(l["url"].rsplit("%2f", 1)[1], l["via"]) for l in st.pr_links}
        self.assertEqual(got, {("10", 1), ("11", 2), ("12", 4)})   # de-duplicated, keeps the first holder

    def test_depth_is_limited_and_unknown_children_are_skipped(self):
        story = {"id": 1, "relations": [rel_child(2), rel_child(99)]}
        items = {2: {"id": 2, "relations": [rel_child(3)]}, 3: {"id": 3, "relations": [rel_child(4)]}, 4: {"id": 4, "relations": []}}
        d = a.fetch_descendants(FakeAdo(items), [story], depth=2)
        self.assertEqual(sorted(w["id"] for w in d[1]), [2, 3])


class StoryConfigTests(unittest.TestCase):
    def wi(self, i, area, children=False):
        return {"id": i, "fields": {"System.AreaPath": area},
                "relations": [rel_child(99)] if children else []}

    def ids(self, items, data):
        return [w["id"] for w in sc.select(items, sc.parse(data))]

    def test_area_include_exclude_matches_area_and_below_case_insensitively(self):
        items = [self.wi(1, "Ibis\Infra"), self.wi(2, "Ibis\Infra\Ops"), self.wi(3, "Ibis\Cloud"), self.wi(4, "Ibis\InfraX")]
        self.assertEqual(self.ids(items, {"areaPaths": {"include": ["ibis\infra"]}}), [1, 2])
        self.assertEqual(self.ids(items, {"areaPaths": {"include": ["Ibis\*"], "exclude": ["Ibis\Infra\Ops", "Ibis\Cloud"]}}), [1, 4])
        self.assertEqual(self.ids(items, {"areaPaths": {"exclude": ["Ibis\Cloud"]}}), [1, 2, 4])

    def test_single_star_stays_within_one_level(self):
        items = [self.wi(1, "P\A\C"), self.wi(2, "P\A\B\C")]
        self.assertEqual(self.ids(items, {"areaPaths": {"include": ["P\*\C"]}}), [1])
        self.assertEqual(self.ids(items, {"areaPaths": {"include": ["P\**\C"]}}), [1, 2])

    def test_require_tasks(self):
        items = [self.wi(1, "A", children=True), self.wi(2, "A")]
        self.assertEqual(self.ids(items, {"requireTasks": True}), [1])
        self.assertEqual(self.ids(items, {}), [1, 2])

    def test_unused_patterns_are_reported(self):
        cfg = sc.parse({"areaPaths": {"include": ["A"], "exclude": ["Typo"]}})
        sc.select([self.wi(1, "A")], cfg)
        self.assertEqual(cfg.areas.unused(), ["Typo"])

    def test_validation(self):
        for bad in ({"since": "2026-01-01"}, {"createdSince": "01-01-2026"}, {"requireTasks": "yes"},
                    {"areaPaths": []}, {"areaPaths": {"include": "A"}}, {"areaPaths": {"nope": []}},
                    {"areaPaths": {"include": ["A", "a"]}}):
            with self.assertRaises(sc.ConfigError, msg=bad):
                sc.parse(bad)
        self.assertEqual(sc.parse({"createdSince": "2025-01-01"}).created_since, "2025-01-01")

    def test_describe(self):
        cfg = sc.parse({"areaPaths": {"exclude": ["X"]}, "createdSince": "2025-01-01", "requireTasks": True})
        self.assertEqual(cfg.describe(), "excluding areas X; created since 2025-01-01; with child tasks only")


class StatsTests(unittest.TestCase):
    def test_median_and_p90(self):
        self.assertEqual(a.med([1, 2, 10]), 2)
        self.assertIsNone(a.med([]))
        self.assertEqual(a.p90(list(range(11))), 9)


if __name__ == "__main__":
    unittest.main()
