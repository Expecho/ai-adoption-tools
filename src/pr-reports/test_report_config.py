import json
import os
import unittest

import report_config as rc

HERE = os.path.dirname(os.path.abspath(__file__))


class GlobTests(unittest.TestCase):
    def m(self, pattern, name):
        return bool(rc.compile_glob(pattern).fullmatch(name))

    def test_star_stays_in_segment(self):
        self.assertTrue(self.m("app-*", "app-web"))
        self.assertFalse(self.m("app-*", "app-web/sub"))
        self.assertTrue(self.m("my-group/*", "my-group/x"))
        self.assertFalse(self.m("my-group/*", "my-group/x/y"))

    def test_double_star_crosses_segments(self):
        self.assertTrue(self.m("my-group/**", "my-group/x/y"))

    def test_question_mark_and_case(self):
        self.assertTrue(self.m("v?", "V1"))
        self.assertFalse(self.m("v?", "v12"))

    def test_dots_are_literal(self):
        self.assertTrue(self.m("Ibis.*", "Ibis.Platform"))
        self.assertFalse(self.m("Ibis.*", "IbisXPlatform"))

    def test_whole_name_only(self):
        self.assertFalse(self.m("app", "my-app"))


class FilterTests(unittest.TestCase):
    def test_empty_include_means_all_and_exclude_wins(self):
        f = rc.RepoFilter(exclude=["*-archive"])
        self.assertTrue(f.allows("org/api"))
        self.assertFalse(f.allows("org/api-archive"))
        f = rc.RepoFilter(include=["api-*"], exclude=["api-old"])
        self.assertTrue(f.allows("org/api-new"))
        self.assertFalse(f.allows("org/api-old"))
        self.assertFalse(f.allows("org/web"))

    def test_repo_patterns_bare_vs_path(self):
        f = rc.RepoFilter(include=["web", "g/sub/*"])
        self.assertTrue(f.allows("anything/web"))       # bare name, any scope
        self.assertTrue(f.allows("g/sub/lib"))          # full path
        self.assertFalse(f.allows("other/sub/lib"))
        self.assertFalse(f.allows("g/web2"))

    def test_unused_patterns_reported(self):
        f = rc.RepoFilter(include=["a*", "zzz"])
        f.allows("org/abc")
        self.assertEqual(f.unused(), ["zzz"])

    def test_literals_and_wildcards(self):
        f = rc.Filter(include=["a", "b*"])
        self.assertEqual((f.literals(), f.wildcards()), (["a"], ["b*"]))


class ParseTests(unittest.TestCase):
    def bad(self, data, text):
        with self.assertRaises(rc.ConfigError) as cm:
            rc.parse(data)
        self.assertIn(text, str(cm.exception))

    def test_example_config_is_valid(self):
        with open(os.path.join(HERE, "sources.example.json"), encoding="utf-8") as f:
            cfg = rc.parse(json.load(f))
        self.assertEqual(cfg.platforms(), ["github", "gitlab"])
        self.assertEqual(cfg.since, "2026-01-01")

    def test_rejections(self):
        self.bad([], "JSON object")
        self.bad({}, "at least one")
        self.bad({"github": {"orgs": {"include": ["a"]}}, "bogus": 1}, "unknown key")
        self.bad({"github": {}}, "github.orgs: required")
        self.bad({"github": {"orgs": {}}}, "list at least one")
        self.bad({"github": {"orgs": {"include": []}}}, "list at least one")
        self.bad({"github": {"orgs": {"include": ["a"], "regex": []}}}, "unknown key")
        self.bad({"github": {"orgs": {"include": ["a", "A"]}}}, "duplicate")
        self.bad({"github": {"orgs": {"include": ["a b"]}}}, "whitespace")
        self.bad({"github": {"orgs": {"include": [""]}}}, "non-empty")
        self.bad({"github": {"orgs": {"include": "a"}}}, "array")
        self.bad({"github": {"orgs": {"include": ["a"]}}, "since": "yesterday"}, "since")
        self.bad({"github": {"orgs": {"include": ["a"]}}, "options": {"nope": 1}}, "unknown option")
        self.bad({"github": {"orgs": {"include": ["a"]}}, "options": {"workers": 0}}, ">= 1")
        self.bad({"github": {"orgs": {"include": ["a"]}}, "options": {"workers": True}}, "int")

    def test_since_all(self):
        self.assertEqual(rc.parse({"since": "all", "gitlab": {"groups": {"include": ["g"]}}}).since, "all")


class CliOverlayTests(unittest.TestCase):
    def cfg(self):
        return rc.parse({"github": {"orgs": {"include": ["o"]}, "repos": {"exclude": ["x"]}},
                         "gitlab": {"groups": {"include": ["g"]}}})

    def test_org_without_config_defaults_to_github(self):
        c = rc.apply_cli(None, None, "my-org", ["a", "b"])
        self.assertEqual(c.platforms(), ["github"])
        self.assertEqual(c.github.scopes.include, ["my-org"])
        self.assertEqual(c.github.repos.include, ["a", "b"])

    def test_source_limits_platform(self):
        c = rc.apply_cli(self.cfg(), "gitlab", None, None)
        self.assertEqual(c.platforms(), ["gitlab"])

    def test_org_with_source_replaces_scopes_keeps_repo_excludes(self):
        c = rc.apply_cli(self.cfg(), "github", "other", None)
        self.assertEqual((c.platforms(), c.github.scopes.include, c.github.repos.exclude),
                         (["github"], ["other"], ["x"]))

    def test_errors(self):
        with self.assertRaises(rc.ConfigError):
            rc.apply_cli(None, None, None, None)
        with self.assertRaises(rc.ConfigError):
            rc.apply_cli(rc.parse({"github": {"orgs": {"include": ["o"]}}}), "gitlab", None, None)


class SchemaTests(unittest.TestCase):
    def test_schema_accepts_example_when_jsonschema_available(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        with open(os.path.join(HERE, "sources.schema.json"), encoding="utf-8") as f:
            schema = json.load(f)
        with open(os.path.join(HERE, "sources.example.json"), encoding="utf-8") as f:
            jsonschema.validate(json.load(f), schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({"github": {"orgs": {"include": []}}}, schema)


if __name__ == "__main__":
    unittest.main()
