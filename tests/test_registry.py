"""Registry integrity — `nova/registry.py` against the shipped catalogue.

Offline and database-free: these tests only read `nova/data/provider_registry.json`
through `nova.catalog`, so they fail loudly when a hand edit leaves the file
malformed (a wrong base_url, a duplicated model id, a keyless provider that
secretly wants a key) instead of that defect surfacing as a broken route later.

Run: python -m unittest tests.test_registry -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova import catalog, registry


class ValidateTests(unittest.TestCase):
    def test_the_shipped_registry_is_valid(self):
        self.assertEqual(registry.validate(), [])

    def test_keyless_providers_declare_no_credentials(self):
        keyless = [p for p in catalog.providers() if registry.is_keyless(p)]
        self.assertTrue(keyless, "expected at least one keyless provider")
        for provider in keyless:
            self.assertFalse(provider["requires_key"], provider["key"])
            self.assertIn(provider.get("auth_type"), ("none", None), provider["key"])

    def test_every_provider_declares_at_least_one_model(self):
        for provider in catalog.providers():
            self.assertGreaterEqual(len(provider.get("models") or []), 1, provider["key"])

    def test_every_keyless_provider_has_models(self):
        for provider in catalog.providers():
            if not registry.is_keyless(provider):
                continue
            self.assertGreaterEqual(len(provider.get("models") or []), 1, provider["key"])

    def test_free_tier_is_a_short_string_or_null(self):
        for provider in catalog.providers():
            free_tier = provider.get("free_tier")
            self.assertTrue(free_tier is None or isinstance(free_tier, str), provider["key"])

    def test_validate_reports_a_known_bad_document(self):
        # Guard against `validate()` being vacuously green: a deliberately broken
        # provider must produce a problem naming it.
        original = catalog._catalogue

        def broken():
            doc = original()
            return {**doc, "providers": [
                {
                    "key": "pollinations", "name": "P", "kind": "openai", "format": "openai",
                    "base_url": "https://text.pollinations.ai/chat/completions",
                    "auth_type": "apikey", "requires_key": False, "color": "#fff", "priority": 1,
                    "models": [
                        {"id": "dup", "name": "D", "ctx": 1, "max_out": 1, "caps": {"tools": True, "vision": False}},
                        {"id": "dup", "name": "D", "ctx": 1, "max_out": 1,
                         "caps": {"tools": True, "vision": False, "reasoning": False}},
                    ],
                }
            ]}

        catalog._catalogue = broken
        try:
            problems = registry.validate()
        finally:
            catalog._catalogue = original
        joined = "\n".join(problems)
        self.assertIn("base_url", joined)
        self.assertIn("duplicate model id", joined)
        self.assertIn("caps is missing 'reasoning'", joined)
        self.assertIn("requires_key is false", joined)


class SummaryTests(unittest.TestCase):
    def test_summary_counts_match_the_catalogue(self):
        source = catalog.source()
        summary = registry.summary()
        self.assertEqual(summary["providers"], source["providers"])
        self.assertEqual(summary["models"], source["models"])
        self.assertEqual(sum(summary["kinds"].values()), source["providers"])

    def test_summary_counts_keyless_and_free(self):
        summary = registry.summary()
        keyless = [p for p in catalog.providers() if p.get("requires_key") is False]
        self.assertEqual(summary["keyless_providers"], len(keyless))
        self.assertGreater(summary["keyless_providers"], 0)
        self.assertGreater(summary["free_models"], 0)
        self.assertLessEqual(summary["free_models"], summary["models"])

    def test_kinds_breakdown_covers_known_kinds(self):
        kinds = registry.summary()["kinds"]
        self.assertTrue(set(kinds) <= registry.KNOWN_KINDS, kinds)
        self.assertIn("openai", kinds)


class EngineModelsTests(unittest.TestCase):
    def test_engine_models_come_only_from_keyless_providers(self):
        keyless = {p["key"] for p in catalog.providers() if registry.is_keyless(p)}
        models = registry.registry_models_for_engine()
        self.assertTrue(models)
        for entry in models:
            self.assertIn(entry["provider"], keyless)
            self.assertIn("id", entry)
            self.assertIn("ctx", entry)

    def test_engine_models_are_unique_and_strip_the_free_suffix(self):
        models = registry.registry_models_for_engine()
        ids = [m["id"] for m in models]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse([i for i in ids if i.endswith(":free")])

    def test_free_suffixed_variants_collapse_onto_the_base_id(self):
        # Build the union from the keyless registry entries themselves so the
        # assertion holds even if the catalogue adds or drops a provider.
        expected = set()
        for provider in catalog.providers():
            if not registry.is_keyless(provider):
                continue
            for model in provider["models"]:
                expected.add(model["id"][: -len(":free")] if model["id"].endswith(":free") else model["id"])
        self.assertEqual({m["id"] for m in registry.registry_models_for_engine()}, expected)


class ExportsTests(unittest.TestCase):
    def test_public_surface_is_declared(self):
        for name in ("validate", "summary", "registry_models_for_engine"):
            self.assertIn(name, registry.__all__)
            self.assertTrue(callable(getattr(registry, name)))


if __name__ == "__main__":
    unittest.main()