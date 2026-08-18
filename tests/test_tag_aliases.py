from __future__ import annotations

import unittest

from scripts.profile_resolver import (
    find_stream,
    load_matrix,
    resolve_tag_alias,
    tag_aliases_for_stream,
)


class TagAliasContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.matrix = load_matrix()

    def test_default_alias_resolves_to_the_selected_toolchain(self) -> None:
        stream = resolve_tag_alias(self.matrix, "2025.1-rocky-10.2")

        self.assertEqual(stream["id"], "2025.1-rocky-10.2-20.5.0")
        self.assertEqual(
            tag_aliases_for_stream(self.matrix, stream),
            ["2025.1-rocky-10.2"],
        )

        ubuntu = resolve_tag_alias(self.matrix, "2025.1-ubuntu-24.04")
        self.assertEqual(ubuntu["id"], "2025.1-ubuntu-24.04-20.5.0")
        self.assertEqual(
            tag_aliases_for_stream(self.matrix, ubuntu),
            ["2025.1-ubuntu-24.04"],
        )

    def test_non_default_toolchain_does_not_update_the_alias(self) -> None:
        stream = find_stream(self.matrix, "2025.1-rocky-10.2-20.4.0")

        self.assertEqual(tag_aliases_for_stream(self.matrix, stream), [])

    def test_unknown_alias_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_tag_alias(self.matrix, "2025.1-rocky-10.2-20.6.0")


if __name__ == "__main__":
    unittest.main()
