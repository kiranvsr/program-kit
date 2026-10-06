import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import unittest

import miniyaml
from common import KitError


class MiniYamlTests(unittest.TestCase):
    def test_round_trip_nested_mapping_list_scalars(self):
        data = {"a": 1, "b": [1, "two", True, None, 2.5], "c": {"d": "x y", "e": []}, "q": "it's: here"}
        self.assertEqual(miniyaml.loads(miniyaml.dumps(data)), data)

    def test_comments_and_quotes(self):
        self.assertEqual(miniyaml.loads("# c\nk: 'v # not comment'\nn: \"1\"\n"), {"k": "v # not comment", "n": "1"})

    def test_rejects_anchors_tags_flow_and_tabs(self):
        for text in ("a: &x 1\n", "a: !!str 1\n", "a: [1, 2]\n", "a:\n\tb: 1\n"):
            with self.assertRaises(miniyaml.YAMLError):
                miniyaml.loads(text)

    def test_bad_indent_names_line(self):
        with self.assertRaisesRegex(miniyaml.YAMLError, "line 3"):
            miniyaml.loads("a:\n  b: 1\n c: 2\n")

    def test_yaml_error_is_a_kit_error(self):
        self.assertTrue(issubclass(miniyaml.YAMLError, KitError))

    def test_empty_and_comment_only_documents_are_none(self):
        for text in ("", "\n\n", "# only a comment\n"):
            self.assertIsNone(miniyaml.loads(text))

    def test_empty_containers_round_trip_at_every_level(self):
        for data in ({}, [], {"a": {}}, {"a": []}, [[]], [{}], [[], {}], {"a": [[], {}], "b": {"c": {}}}):
            self.assertEqual(miniyaml.loads(miniyaml.dumps(data)), data, data)

    def test_top_level_scalars_round_trip(self):
        for data in (None, True, 3, "text", "", 1.5):
            self.assertEqual(miniyaml.loads(miniyaml.dumps(data)), data)

    def test_strings_that_look_like_other_types_stay_strings(self):
        tricky = ["1", "007", "-3", "2.5", "1e5", "true", "false", "null", "~", "", " pad ", "a: b", "x #y",
                  "trailing:", "#lead", "- dash", "-", "[a]", "{a}", "&a", "*a", "!a", "|", ">", "%x", "@x", "`x",
                  "'q'", '"dq"', "it's", 'say "hi"', "two\nlines", "tab\there", "back\\slash", "uni\u00e9", "\x01"]
        data = {"k": tricky, "m": {t: t for t in tricky}}
        text = miniyaml.dumps(data)
        self.assertEqual(miniyaml.loads(text), data)

    def test_scalar_resolution(self):
        got = miniyaml.loads("a: true\nb: false\nc: null\nd: ~\ne: 12\nf: -4\ng: 3.25\nh: 1e3\ni: hello world\nj:\n")
        self.assertEqual(got, {"a": True, "b": False, "c": None, "d": None, "e": 12, "f": -4,
                               "g": 3.25, "h": 1000.0, "i": "hello world", "j": None})
        self.assertIs(type(got["e"]), int)
        self.assertIs(type(got["h"]), float)

    def test_quote_escapes(self):
        self.assertEqual(miniyaml.loads("a: 'it''s'\nb: \"say \\\"hi\\\"\\n\"\n"), {"a": "it's", "b": 'say "hi"\n'})

    def test_trailing_comments_after_values(self):
        self.assertEqual(miniyaml.loads("a: 1 # one\nb: 'x' # q\nc: # nothing\n  d: 2\n"),
                         {"a": 1, "b": "x", "c": {"d": 2}})

    def test_list_of_mappings_and_nested_lists(self):
        text = "items:\n  - name: a\n    n: 1\n  -   name: b\n      n: 2\n  - - x\n    - y\n  -\n    z: 1\n"
        self.assertEqual(miniyaml.loads(text), {"items": [{"name": "a", "n": 1}, {"name": "b", "n": 2},
                                                          ["x", "y"], {"z": 1}]})

    def test_dumps_shape_is_stable_and_ends_with_newline(self):
        text = miniyaml.dumps({"a": 1, "b": [{"c": 2, "d": [3]}, "x"], "e": {"f": None}})
        self.assertEqual(text, "a: 1\nb:\n  - c: 2\n    d:\n      - 3\n  - x\ne:\n  f: null\n")

    def test_errors_name_the_line(self):
        cases = {
            "a: 1\nb: &x 2\n": "line 2",
            "a: 1\n\nb: [1]\n": "line 3",
            "a: 1\na: 2\n": "line 2",
            "a: 'open\n": "line 1",
            "a: 1\nnot a mapping line\n": "line 2",
            "- a\nb: 1\n": "line 2",
            "a: b: c\n": "line 1",
            "a:\n  - 1\n  x: 2\n": "line 3",
            "a: 1\n    b: 2\n": "line 2",
            "a: |\n  text\n": "line 1",
            "a: \"bad \\q escape\"\n": "line 1",
            "a: 'x' junk\n": "line 1",
        }
        for text, expected in cases.items():
            with self.assertRaisesRegex(miniyaml.YAMLError, expected, msg=repr(text)):
                miniyaml.loads(text)

    def test_dumps_rejects_unsupported_data(self):
        for bad in ({1: "x"}, {"a": object()}, {"a": float("nan")}, {"a": float("inf")}, {"a": b"x"}):
            with self.assertRaises(miniyaml.YAMLError):
                miniyaml.dumps(bad)

    def test_tuples_dump_as_lists(self):
        self.assertEqual(miniyaml.loads(miniyaml.dumps({"a": (1, 2)})), {"a": [1, 2]})

    def test_crlf_input(self):
        self.assertEqual(miniyaml.loads("a: 1\r\nb:\r\n  - x\r\n"), {"a": 1, "b": ["x"]})


class SameIndentSequenceTests(unittest.TestCase):
    """A block sequence may sit at the same indent as its key (the default output of common YAML writers)."""

    def test_sequence_at_the_key_indent(self):
        self.assertEqual(miniyaml.loads("a:\n- 1\n- two\nb: x\n"), {"a": [1, "two"], "b": "x"})
        self.assertEqual(miniyaml.loads("a:\n- 1\n"), {"a": [1]})

    def test_nesting(self):
        text = ("items:\n- name: a\n  tags:\n  - x\n  - y\n  other: 1\n- name: b\n  tags: []\n"
                "deep:\n  inner:\n  - 1\n  - - 2\n    - 3\n  after: z\nlast:\n")
        self.assertEqual(miniyaml.loads(text), {
            "items": [{"name": "a", "tags": ["x", "y"], "other": 1}, {"name": "b", "tags": []}],
            "deep": {"inner": [1, [2, 3]], "after": "z"}, "last": None})
        self.assertEqual(miniyaml.loads("a:\n- b:\n  - c\n- d\n"), {"a": [{"b": ["c"]}, "d"]})

    def test_ambiguous_cases_keep_their_yaml_meaning(self):
        # A key inside a list item is indented past the dash, so a dash at the dash column is the next item.
        self.assertEqual(miniyaml.loads("- a:\n- b\n"), [{"a": None}, "b"])
        self.assertEqual(miniyaml.loads("k:\n- a:\n- b\n"), {"k": [{"a": None}, "b"]})
        # A key with nothing under it and a dash only deeper than the key is the old form.
        self.assertEqual(miniyaml.loads("a:\n  - 1\nb:\n  - 2\n"), {"a": [1], "b": [2]})
        # Null key followed by another key at the same indent stays null.
        self.assertEqual(miniyaml.loads("a:\nb:\n- 1\n"), {"a": None, "b": [1]})

    def test_nothing_else_is_newly_accepted(self):
        for text, line in (("a: 1\n- b\n", "line 2"),            # a dash after a scalar value
                           ("a:\n- 1\nx\n", "line 3"),           # a non-key line after the list
                           ("a:\n- 1\n  - 2\n", "line 3"),       # a deeper dash after a scalar item
                           ("a:\n- 1\n b: 2\n", "line 3"),       # off-column key
                           ("a:\n  - 1\n- 2\n", "line 3"),       # dash back at the key indent after a deeper list
                           ("- a\nb:\n- c\n", "line 2")):
            with self.assertRaisesRegex(miniyaml.YAMLError, line, msg=repr(text)):
                miniyaml.loads(text)

    def test_duplicate_key_after_a_same_indent_list_is_still_rejected(self):
        with self.assertRaisesRegex(miniyaml.YAMLError, "line 4"):
            miniyaml.loads("a:\n- 1\nb: 2\na: 3\n")


class ByteOrderMarkTests(unittest.TestCase):
    def test_a_leading_bom_is_ignored(self):
        self.assertEqual(miniyaml.loads("\ufeffa: 1\nb:\n  - x\n"), {"a": 1, "b": ["x"]})
        self.assertIsNone(miniyaml.loads("\ufeff# only a comment\n"))

    def test_a_bom_anywhere_else_is_not_stripped(self):
        self.assertEqual(miniyaml.loads("a: \ufeffx\n"), {"a": "\ufeffx"})


if __name__ == "__main__":
    unittest.main()
