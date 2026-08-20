from __future__ import annotations

import unittest

from scripts.profile_resolver import load_matrix, resolve_tag_alias, tag_aliases_for_stream


class TagAliasContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix = load_matrix()

    def test_default_alias_resolves_to_the_selected_toolchain(self) -> None:
        for alias, expected_stream_id in self.matrix["tag_aliases"].items():
            with self.subTest(alias=alias):
                stream = resolve_tag_alias(self.matrix, alias)
                self.assertEqual(stream["id"], expected_stream_id)
                self.assertEqual(tag_aliases_for_stream(self.matrix, stream), [alias])

    def test_unknown_alias_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_tag_alias(self.matrix, "not-a-configured-alias")


if __name__ == "__main__":
    unittest.main()
