"""RouterX routing brain — tag routing, policies, lockouts,
fallback chains, selection strategies, the model exposure lists, the catalogue
and the MCP tool search.

Run: python -m unittest discover -s tests -p test_routing.py

These are the decisions a request's fate rests on, so they are pinned as pure
functions with no network, no keys and no live providers: what a tagged request
is allowed to reach, which candidate wins under each strategy, why a candidate
was dropped, and that a blocked model is refused rather than silently served by
something else. The last class wires the same brain into the gateway over a
throwaway SQLite database to prove `route_request` really orders rows.
"""
import asyncio
import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nova import catalog, mcp_tools, router_settings
from nova import routing
from nova.models import Model, ModelRoute, Provider, ProviderKey, SystemConfig


def candidate(exposed_id, **overrides):
    """A candidate dict shaped the way `gateway._candidate` builds them."""
    base = {
        "exposed_id": exposed_id,
        "model_id": exposed_id.split("/")[-1],
        "provider_key": exposed_id.split("/")[0],
        "provider_name": exposed_id.split("/")[0],
        "provider_priority": 100,
        "stage": 1,
        "chain_depth": 1,
        "status": "healthy",
        "latency_ms": 500,
        "cost_per_1m": 0.0,
        "req_count": 0,
        "tags": [],
    }
    base.update(overrides)
    return base


class TagRoutingTests(unittest.TestCase):
    def test_normalize_accepts_lists_and_comma_strings(self):
        self.assertEqual(routing.normalize_tags(" Vision , fast ,, vision "), ["vision", "fast"])
        self.assertEqual(routing.normalize_tags(["A", "a", "b"]), ["a", "b"])
        self.assertEqual(routing.normalize_tags(None), [])
        self.assertEqual(routing.normalize_tags(42), [])

    def test_untagged_request_reaches_everyone_untagged_provider_reaches_nobody(self):
        self.assertTrue(routing.matches_tags([], []))
        self.assertTrue(routing.matches_tags(["fast"], []))
        self.assertFalse(routing.matches_tags([], ["fast"]))

    def test_any_and_all_match_modes(self):
        # "any" is satisfied by a single shared tag; "all" needs every one.
        self.assertTrue(routing.matches_tags(["fast", "eu"], ["fast"], "any"))
        self.assertTrue(routing.matches_tags(["fast", "eu"], ["fast", "us"], "any"))
        self.assertTrue(routing.matches_tags(["fast", "eu"], ["fast", "eu"], "all"))
        self.assertFalse(routing.matches_tags(["fast", "eu"], ["fast", "us"], "all"))

    def test_tags_come_off_request_metadata(self):
        tags, mode = routing.request_tags({"metadata": {"tags": ["Fast"], "tag_match_mode": "all"}})
        self.assertEqual(tags, ["fast"])
        self.assertEqual(mode, "all")
        self.assertEqual(routing.request_tags(None), ([], "any"))
        self.assertEqual(routing.request_tags({"metadata": "nope"}), ([], "any"))


class PolicyTests(unittest.TestCase):
    def test_access_policy_blocks_a_glob(self):
        policies = routing.load_policies([{
            "id": "no-preview", "name": "no previews", "type": "access", "enabled": True,
            "priority": 1, "conditions": {"model_pattern": "gpt-*"},
            "actions": {"block_model": ["gpt-4o", "gpt-4o-mini"]},
        }])
        blocked = routing.evaluate_policies(policies, "gpt-4o")
        self.assertFalse(blocked["allowed"])
        self.assertIn("no previews", blocked["reason"])
        self.assertTrue(routing.evaluate_policies(policies, "claude-sonnet-4-6")["allowed"])

    def test_disabled_policies_are_ignored(self):
        policies = routing.load_policies([{
            "id": "x", "name": "x", "type": "access", "enabled": False,
            "actions": {"block_model": ["*"]},
        }])
        self.assertTrue(routing.evaluate_policies(policies, "anything")["allowed"])

    def test_routing_and_budget_policies_accumulate(self):
        policies = routing.load_policies([
            {"id": "a", "name": "a", "type": "routing", "priority": 2,
             "actions": {"prefer_provider": ["groq", "openrouter"]}},
            {"id": "b", "name": "b", "type": "budget", "priority": 1, "actions": {"max_tokens": 4096}},
            {"id": "c", "name": "c", "type": "budget", "priority": 3, "actions": {"max_tokens": 1024}},
        ])
        verdict = routing.evaluate_policies(policies, "gpt-4o")
        self.assertTrue(verdict["allowed"])
        self.assertEqual(verdict["preferred_providers"], ["groq", "openrouter"])
        self.assertEqual(verdict["max_tokens"], 1024)  # tightest cap wins

    def test_unknown_policy_types_are_dropped_not_stored(self):
        self.assertEqual(routing.load_policies([{"type": "wat"}]), [])


class FallbackChainTests(unittest.TestCase):
    def setUp(self):
        self.chains = routing.load_chains({
            "gpt-4o": [
                {"id": "groq/llama", "priority": 10, "enabled": True},
                {"id": "off/model", "priority": 1, "enabled": False},
                {"id": "openrouter/llama", "priority": 5, "enabled": True},
            ]
        })

    def test_entries_sort_by_priority_and_skip_disabled(self):
        chain = routing.resolve_chain(self.chains, "gpt-4o")
        self.assertEqual([e["id"] for e in chain], ["openrouter/llama", "groq/llama"])

    def test_exclusions_remove_already_tried_entries(self):
        self.assertEqual(routing.next_fallback(self.chains, "gpt-4o", ["openrouter/llama"]), "groq/llama")
        self.assertIsNone(routing.next_fallback(self.chains, "gpt-4o", ["openrouter/llama", "groq/llama"]))

    def test_plain_string_chains_are_accepted(self):
        chains = routing.load_chains({"*": ["a/1", "b/2"]})
        self.assertEqual([e["id"] for e in chains["*"]], ["a/1", "b/2"])
        self.assertTrue(routing.has_fallback(chains, "*"))
        self.assertFalse(routing.has_fallback(chains, "nope"))


class LockoutTests(unittest.TestCase):
    def setUp(self):
        self.registry = routing.LockoutRegistry(threshold=2, base_ms=1000, max_ms=8000)

    def test_lockout_starts_after_the_threshold_and_backs_off(self):
        self.registry.record_failure("groq", "llama")
        self.assertFalse(self.registry.is_locked("groq", "llama"))
        self.assertGreater(self.registry.record_failure("groq", "llama"), 0)
        self.assertTrue(self.registry.is_locked("groq", "llama"))
        self.registry.record_failure("groq", "llama")
        remaining = self.registry.remaining_ms("groq", "llama")
        self.assertGreater(remaining, 1000)  # doubled past the base window

    def test_success_and_clear_reopen_the_pool(self):
        self.registry.record_failure("groq", "llama")
        self.registry.record_failure("groq", "llama")
        self.assertTrue(self.registry.is_locked("groq", "llama"))
        self.registry.record_success("groq", "llama")
        self.assertFalse(self.registry.is_locked("groq", "llama"))
        self.registry.record_failure("groq", "llama")
        self.registry.record_failure("groq", "llama")
        self.assertTrue(self.registry.clear("groq", "llama"))
        self.assertFalse(self.registry.is_locked("groq", "llama"))
        self.assertFalse(self.registry.clear("groq", "never-seen"))

    def test_state_survives_a_restart(self):
        self.registry.record_failure("groq", "llama")
        self.registry.record_failure("groq", "llama")
        restored = routing.LockoutRegistry(threshold=2, base_ms=1000, max_ms=8000)
        restored.load_state(self.registry.dump_state())
        self.assertTrue(restored.is_locked("groq", "llama"))
        self.assertEqual(restored.report()[0]["failure_count"], 2)

    def test_expired_entries_stop_counting_as_locked(self):
        self.registry.record_failure("groq", "llama")
        self.registry.record_failure("groq", "llama")
        self.assertFalse(self.registry.is_locked("groq", "llama", now_ms=10 ** 15))
        self.assertEqual(self.registry.report(now_ms=10 ** 15)[0]["remaining_ms"], 0)

    def test_client_lockout_escalates_then_resets(self):
        lockouts = routing.ClientLockouts(max_attempts=2, duration_ms=60_000, window_ms=60_000)
        self.assertFalse(lockouts.is_locked("1.2.3.4"))
        lockouts.record_failure("1.2.3.4")
        self.assertFalse(lockouts.is_locked("1.2.3.4"))
        self.assertTrue(lockouts.record_failure("1.2.3.4")["locked"])
        self.assertTrue(lockouts.is_locked("1.2.3.4"))
        lockouts.record_success("1.2.3.4")
        self.assertFalse(lockouts.is_locked("1.2.3.4"))


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.pool = [
            candidate("fast/one", latency_ms=200, cost_per_1m=4.0, stage=2, provider_priority=50),
            candidate("cheap/one", latency_ms=900, cost_per_1m=0.0, stage=1, provider_priority=10),
            candidate("steady/one", latency_ms=400, cost_per_1m=2.0, stage=1, provider_priority=10,
                      successes=9, failures=1),
        ]

    def test_priority_keeps_the_configured_stage_order(self):
        order = [c["exposed_id"] for c in routing.select(self.pool, "priority")]
        self.assertEqual(order[0], "cheap/one")
        self.assertLess(order.index("cheap/one"), order.index("steady/one"))
        self.assertEqual(order[-1], "fast/one")

    def test_cost_and_cost_optimized_prefer_the_cheapest(self):
        self.assertEqual(routing.select(self.pool, "cost")[0]["exposed_id"], "cheap/one")
        self.assertEqual(routing.select(self.pool, "cost-optimized")[0]["exposed_id"], "cheap/one")

    def test_latency_prefers_the_quickest(self):
        self.assertEqual(routing.select(self.pool, "latency")[0]["exposed_id"], "fast/one")

    def test_least_used_spreads_the_load(self):
        pool = [candidate("a/1", req_count=9), candidate("b/1", req_count=1)]
        self.assertEqual(routing.select(pool, "least-used")[0]["exposed_id"], "b/1")

    def test_round_robin_rotates_the_start(self):
        pool = [candidate("a/1"), candidate("b/1"), candidate("c/1")]
        first = routing.select(pool, "round-robin")[0]["exposed_id"]
        second = routing.select(pool, "round-robin")[0]["exposed_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(len(routing.select(pool, "round-robin")), 3)

    def test_weighted_and_random_cover_the_whole_pool(self):
        self.assertEqual(len(routing.select(self.pool, "weighted")), 3)
        self.assertEqual(len(routing.select(self.pool, "random")), 3)
        self.assertEqual(routing.select([], "priority"), [])

    def test_scoring_favours_health_and_reliability(self):
        healthy = candidate("good/1", status="healthy", successes=10, failures=0)
        dead = candidate("bad/1", status="dead", successes=0, failures=10)
        self.assertEqual(routing.select([dead, healthy], "rules")[0]["exposed_id"], "good/1")

    def test_lkgp_tries_the_last_known_good_first(self):
        order = routing.select(self.pool, "lkgp", last_known_good="fast/one")
        self.assertEqual(order[0]["exposed_id"], "fast/one")
        self.assertEqual(len(order), 3)

    def test_sla_keeps_compliant_candidates_ahead(self):
        pool = [candidate("slow/1", latency_ms=9000), candidate("quick/1", latency_ms=100)]
        order = routing.select(pool, "sla", sla={"target_p95_ms": 1000, "hard_constraints": True})
        self.assertEqual([c["exposed_id"] for c in order], ["quick/1"])

    def test_weights_are_normalized_and_never_negative(self):
        self.assertAlmostEqual(sum(routing.normalize_weights({"health": 3, "cost": 1}).values()), 1.0)
        self.assertEqual(routing.normalize_weights({"health": -5}), routing.DEFAULT_WEIGHTS)
        self.assertEqual(routing.normalize_weights({}), routing.DEFAULT_WEIGHTS)


class ExposureTests(unittest.TestCase):
    def test_empty_lists_expose_everything(self):
        self.assertTrue(catalog.is_exposure_allowed("gpt-4o", "openai"))

    def test_denylist_wins_over_allowlist(self):
        self.assertFalse(catalog.is_exposure_allowed(
            "gpt-4o", "openai", denylist=["gpt-4o"], allowlist=["gpt-*"]))

    def test_allowlist_narrows_to_matches_only(self):
        self.assertTrue(catalog.is_exposure_allowed("openai/gpt-4o", "openai", allowlist=["gpt-*"]))
        self.assertFalse(catalog.is_exposure_allowed("claude-opus-5", "anthropic", allowlist=["gpt-*"]))

    def test_globs_span_slashes(self):
        self.assertTrue(catalog.glob_match("anthropic/*", "anthropic/claude-opus-5"))
        self.assertTrue(catalog.glob_match("gpt-?5", "gpt-45"))
        self.assertFalse(catalog.glob_match("gpt-?5", "gpt-455"))


class PlanTests(unittest.TestCase):
    def test_locked_candidates_are_skipped_with_a_reason(self):
        lockouts = routing.LockoutRegistry(threshold=1, base_ms=60_000, max_ms=60_000)
        lockouts.record_failure("flaky", "llama")
        plan = routing.build_plan(
            [candidate("flaky/llama"), candidate("solid/llama")],
            requested="llama", lockouts=lockouts,
        )
        self.assertTrue(plan["allowed"])
        self.assertEqual([c["exposed_id"] for c in plan["candidates"]], ["solid/llama"])
        self.assertEqual(plan["excluded"], {"lockout": 1})
        self.assertIn("cooling down", plan["steps"][0]["reason"])

    def test_tags_narrow_the_pool(self):
        plan = routing.build_plan(
            [candidate("eu/one", tags=["eu"]), candidate("us/one", tags=["us"])],
            requested="m", request_tags_=["eu"],
        )
        self.assertEqual([c["exposed_id"] for c in plan["candidates"]], ["eu/one"])
        self.assertEqual(plan["excluded"], {"tags": 1})

    def test_blocked_model_is_refused_not_substituted(self):
        policies = routing.load_policies([{
            "id": "b", "name": "blocked", "type": "access",
            "actions": {"block_model": ["secret-*"]},
        }])
        plan = routing.build_plan([candidate("secret/x"), candidate("other/x")],
                                  requested="secret-x", policies=policies)
        self.assertFalse(plan["allowed"])
        self.assertEqual(plan["candidates"], [])

    def test_hidden_models_cannot_enter_a_fallback_chain(self):
        plan = routing.build_plan(
            [candidate("hidden/x"), candidate("shown/x")],
            requested="m", denylist=["hidden/*"],
        )
        self.assertEqual([c["exposed_id"] for c in plan["candidates"]], ["shown/x"])
        self.assertEqual(plan["excluded"], {"exposure": 1})


class CatalogTests(unittest.TestCase):
    """The catalogue is real data: assert its shape, then assert it answers.

    The counts are floors, not magic numbers: the registry is curated by hand,
    so the exact size moves, but shipping fewer providers/models than these
    floors means entries were dropped by accident. The model ids below are the
    real ids in `nova/data/provider_registry.json` (a provider the operator
    configures seeds its models as `<provider-key>/<model-id>`).
    """

    def test_catalogue_is_loaded_and_populated(self):
        source = catalog.source()
        self.assertIn("RouterX", source["source"])
        self.assertGreaterEqual(source["providers"], 15)
        self.assertGreaterEqual(source["models"], 60)

    def test_every_seeded_model_survives_a_retired_vendor_drop(self):
        # `claude-3-5-sonnet-20241022` is retired: it is not a catalogue id of
        # its own, so it is never advertised or dispatched as itself.
        listed = {m["id"] for p in catalog.providers() for m in p["models"]}
        self.assertNotIn("claude-3-5-sonnet-20241022", listed)
        resolved = catalog.resolve("claude-3-5-sonnet-20241022")
        self.assertNotEqual((resolved or {}).get("id"), "claude-3-5-sonnet-20241022")

    def test_resolution_handles_bare_prefixed_and_suffixed_ids(self):
        # `claude-opus-4-5` exists in the registry and *only* there, so exact,
        # prefixed and suffixed spellings must all land on the same entry.
        self.assertEqual((catalog.resolve("claude-opus-4-5") or {}).get("id"), "claude-opus-4-5")
        self.assertEqual((catalog.resolve("anthropic/claude-opus-4-5") or {}).get("id"), "claude-opus-4-5")
        self.assertEqual((catalog.resolve("claude-opus-4-5-free") or {}).get("id"), "claude-opus-4-5")
        self.assertIsNone(catalog.resolve(""))

    def test_resolution_works_for_a_model_from_each_of_several_providers(self):
        # The seeded form the gateway sees: <provider-key>/<model-id>.
        for exposed in (
            "anthropic/claude-opus-4-5",
            "openai/gpt-4o",
            "deepseek/deepseek-reasoner",
            "groq/llama-3.3-70b-versatile",
            "gemini/gemini-2.5-flash",
            "mistral/mistral-small-latest",
            "xai/grok-3",
            "cerebras/qwen-3-32b",
            "cohere/command-r-plus-08-2024",
        ):
            with self.subTest(exposed=exposed):
                provider_key, model_id = exposed.split("/", 1)
                entry = catalog.resolve(exposed)
                self.assertIsNotNone(entry, exposed)
                self.assertEqual(entry["id"], model_id)
                self.assertIn(provider_key, catalog.providers_for_model(model_id))

    def test_capabilities_and_context_come_from_the_registry(self):
        # `deepseek-reasoner` is declared with reasoning in the registry; the
        # capability answer must come from there, not from a per-provider guess.
        entry = catalog.resolve("deepseek-reasoner")
        self.assertIsNotNone(entry)
        self.assertTrue(entry["caps"]["reasoning"])
        self.assertTrue(catalog.capabilities("deepseek-reasoner")["reasoning"])
        self.assertFalse(catalog.capabilities("claude-haiku-4-5")["reasoning"])
        self.assertGreater(catalog.context_length("deepseek-reasoner"), 0)
        self.assertTrue(catalog.is_known("deepseek-reasoner"))

    def test_providers_and_search(self):
        self.assertEqual(catalog.provider("anthropic")["kind"], "anthropic")
        self.assertGreater(len(catalog.provider_models("anthropic")), 1)
        self.assertIn("anthropic", catalog.providers_for_model("claude-opus-4-5"))
        self.assertTrue(catalog.search("claude"))
        self.assertEqual(catalog.search(""), [])


class McpToolTests(unittest.TestCase):
    def test_search_prefers_name_matches_over_description_matches(self):
        tools = [
            {"name": "list_models", "description": "show the catalogue"},
            {"name": "ping", "description": "list_models health probe"},
        ]
        results = mcp_tools.search_tools(tools, "list_models")
        self.assertEqual(results[0]["name"], "list_models")

    def test_search_is_case_insensitive_and_bounded(self):
        tools = [{"name": f"tool_{i}", "description": "tool"} for i in range(50)]
        self.assertEqual(len(mcp_tools.search_tools(tools, "TOOL", limit=5)), 5)
        self.assertEqual(mcp_tools.search_tools(tools, "   "), [])
        self.assertEqual(mcp_tools.search_tools(tools, "zzz-nothing"), [])

    def test_regex_metacharacters_in_a_query_are_literal(self):
        tools = [{"name": "a.b", "description": "x"}, {"name": "axb", "description": "x"}]
        # A regex-built search would match both; the indexOf scanner matches one.
        self.assertEqual([t["name"] for t in mcp_tools.search_tools(tools, "a.b")], ["a.b"])

    def test_scopes_are_enforced_only_when_a_credential_declares_them(self):
        self.assertTrue(mcp_tools.has_scopes([], ["read:health"]))
        self.assertTrue(mcp_tools.has_scopes(["read:health"], ["read:health"]))
        self.assertFalse(mcp_tools.has_scopes(["read:health"], ["read:health", "write:combos"]))
        self.assertTrue(mcp_tools.has_scopes(None, []))

    def test_filter_by_scope_keeps_only_permitted_tools(self):
        tools = [{"name": "read", "scopes": ["read:health"]}, {"name": "write", "scopes": ["write:combos"]}]
        self.assertEqual([t["name"] for t in mcp_tools.filter_by_scope(tools, ["read:health"])], ["read"])

    def test_scope_and_skill_tables_are_loaded(self):
        self.assertIn("read:health", mcp_tools.scopes())
        skills = mcp_tools.skills()
        self.assertGreaterEqual(len(skills), 20)
        self.assertTrue(all(s["id"] for s in skills))
        self.assertEqual(mcp_tools.skill("providers-keys")["category"], "api")
        self.assertIsNone(mcp_tools.skill("nope"))
        self.assertIn("Providers", mcp_tools.skill_prompt("providers-keys"))
        self.assertTrue(mcp_tools.search_skills("provider"))


class GatewayRoutingTests(unittest.TestCase):
    """`route_request` has to order real rows, not just dicts."""

    def setUp(self):
        from nova import routing as routing_module
        from routers import gateway

        self.gateway = gateway
        self._original_lockouts = routing_module.LOCKOUTS
        self.registry = routing_module.LockoutRegistry(threshold=1, base_ms=60_000, max_ms=60_000)
        routing_module.LOCKOUTS = self.registry

        self.engine = create_engine("sqlite://", poolclass=StaticPool,
                                    connect_args={"check_same_thread": False})
        SystemConfig.__table__.create(self.engine)
        ModelRoute.__table__.create(self.engine)
        ProviderKey.__table__.create(self.engine)
        Provider.__table__.create(self.engine)
        Model.__table__.create(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()

        primary = Provider(key="primary", name="Primary", kind="openai",
                           baseUrl="https://primary.test", prefix="primary/", priority=10)
        backup = Provider(key="backup", name="Backup", kind="openai",
                          baseUrl="https://backup.test", prefix="backup/", priority=20)
        self.db.add_all([primary, backup])
        self.db.flush()
        self.db.add_all([
            Model(providerId=primary.id, modelId="fast", exposedId="primary/fast",
                  displayName="Fast", status="healthy", latencyMs=100),
            Model(providerId=backup.id, modelId="slow", exposedId="backup/slow",
                  displayName="Slow", status="unknown", latencyMs=900),
        ])
        self.db.add(ModelRoute(publicId="primary/fast", fallbacks='["backup/slow"]', auto=False))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.registry.reset()
        routing.LOCKOUTS = self._original_lockouts

    def test_direct_row_first_then_the_route_fallback(self):
        rows, plan = self.gateway.route_request(self.db, "primary/fast")
        self.assertEqual([r.exposedId for r in rows], ["primary/fast", "backup/slow"])
        self.assertEqual(plan["considered"], 2)
        self.assertEqual(plan["strategy"], "priority")

    def test_a_blocked_model_raises_before_any_upstream_is_touched(self):
        router_settings.save(self.db, policies=[{
            "id": "b", "name": "no fast", "type": "access",
            "actions": {"block_model": ["primary/*"]},
        }])
        with self.assertRaises(self.gateway.HTTPError) as ctx:
            self.gateway.route_request(self.db, "primary/fast")
        self.assertEqual(ctx.exception.status, 403)

    def test_a_declarative_chain_extends_the_model_route(self):
        router_settings.save(self.db, chains={"primary/fast": [{"id": "primary/slow", "priority": 0}]})
        self.db.add(Model(providerId=self.db.query(Provider).filter_by(key="primary").one().id,
                          modelId="slow", exposedId="primary/slow", displayName="Slow"))
        self.db.commit()
        rows, _plan = self.gateway.route_request(self.db, "primary/fast")
        self.assertEqual(rows[-1].exposedId, "primary/slow")

    def test_routing_summary_explains_the_decision(self):
        _rows, plan = self.gateway.route_request(self.db, "primary/fast")
        summary = self.gateway.routing_summary(plan)
        self.assertEqual(summary["ordered"], ["primary/fast", "backup/slow"])
        self.assertEqual(summary["considered"], 2)
        self.assertEqual(summary["excluded"], {})

    def test_locked_candidates_are_left_out_of_the_ordering(self):
        self.registry.record_failure("primary", "fast")
        rows, plan = self.gateway.route_request(self.db, "primary/fast")
        self.assertEqual([r.exposedId for r in rows], ["backup/slow"])
        self.assertEqual(plan["excluded"], {"lockout": 1})

    def test_settings_round_trip_through_system_config(self):
        router_settings.save(self.db, strategy="weighted", denylist=["backup/*"])
        reloaded = router_settings.load(self.db)
        self.assertEqual(reloaded.strategy, "weighted")
        self.assertEqual(reloaded.denylist, ["backup/*"])
        self.assertEqual(router_settings.load(self.db).weights, reloaded.weights)

    def test_an_unknown_strategy_falls_back_to_the_default(self):
        router_settings.save(self.db, strategy="telepathy")
        self.assertEqual(router_settings.load(self.db).strategy, router_settings.DEFAULT_STRATEGY)

    def _add_third_provider(self, kind="openai", status="healthy", enabled=True, base_url="https://third.test"):
        third = Provider(key="third", name="Third", kind=kind, baseUrl=base_url,
                         prefix="third/", priority=30, enabled=enabled)
        self.db.add(third)
        self.db.flush()
        self.db.add(Model(providerId=third.id, modelId="free", exposedId="third/free",
                          displayName="Free", status=status, latencyMs=50, isFree=True))
        self.db.commit()
        return third

    def test_auto_standins_extend_the_chain_when_auto_is_on(self):
        # The '*' default route turns auto on, so a failed explicit chain keeps
        # going onto the operator's other healthy providers instead of jumping
        # straight to the free engine.
        self._add_third_provider()
        route = self.db.query(ModelRoute).filter_by(publicId="primary/fast").one()
        route.auto = True
        self.db.commit()
        rows, plan = self.gateway.route_request(self.db, "primary/fast")
        self.assertEqual([r.exposedId for r in rows], ["primary/fast", "backup/slow", "third/free"])
        self.assertEqual(plan["considered"], 3)

    def test_auto_standins_skip_builtin_and_dead_providers(self):
        self._add_third_provider()
        builtin = Provider(key="engine", name="Engine", kind="builtin",
                           baseUrl="internal://engine", prefix="engine/", priority=1)
        gemini = Provider(key="gem", name="Gemini", kind="gemini",
                          baseUrl="https://gem.test", prefix="gem/", priority=2)
        dead = Provider(key="dead", name="Dead", kind="openai",
                        baseUrl="https://dead.test", prefix="dead/", priority=3)
        self.db.add_all([builtin, gemini, dead])
        self.db.flush()
        self.db.add_all([
            Model(providerId=builtin.id, modelId="mini", exposedId="engine/mini", status="healthy"),
            Model(providerId=gemini.id, modelId="pro", exposedId="gem/pro", status="healthy"),
            Model(providerId=dead.id, modelId="gone", exposedId="dead/gone", status="dead"),
        ])
        self.db.commit()
        route = self.db.query(ModelRoute).filter_by(publicId="primary/fast").one()
        route.auto = True
        self.db.commit()
        rows, _plan = self.gateway.route_request(self.db, "primary/fast")
        self.assertEqual([r.exposedId for r in rows], ["primary/fast", "backup/slow", "third/free"])

    def test_auto_standins_are_opt_out_per_route(self):
        # The explicit route in setUp sets auto=False: no stand-ins are added.
        self._add_third_provider()
        rows, _plan = self.gateway.route_request(self.db, "primary/fast")
        self.assertEqual([r.exposedId for r in rows], ["primary/fast", "backup/slow"])

    def test_auto_standins_cap_the_pool(self):
        for i in range(8):
            prov = Provider(key=f"p{i}", name=f"P{i}", kind="openai",
                            baseUrl=f"https://p{i}.test", prefix=f"p{i}/", priority=40 + i)
            self.db.add(prov)
            self.db.flush()
            self.db.add(Model(providerId=prov.id, modelId="m", exposedId=f"p{i}/m",
                              status="healthy", latencyMs=i))
        self.db.commit()
        route = self.db.query(ModelRoute).filter_by(publicId="primary/fast").one()
        route.auto = True
        self.db.commit()
        rows, _plan = self.gateway.route_request(self.db, "primary/fast")
        # 2 known rows + at most AUTO_STANDIN_LIMIT
        self.assertLessEqual(len(rows), 2 + self.gateway.AUTO_STANDIN_LIMIT)
        self.assertGreater(len(rows), 2)

    def test_persist_lockouts_is_throttled_and_never_raises(self):
        # Regression: a missing `global` made every successful request die with
        # UnboundLocalError the moment lockout state was flushed to the DB.
        self.registry.record_failure("primary", "fast")
        self.gateway.persist_lockouts(self.db, force=True)
        self.gateway.persist_lockouts(self.db)  # throttled path
        self.gateway.persist_lockouts(self.db, force=True)

    def test_a_failing_upstream_is_recorded_and_a_success_clears_it(self):
        from unittest.mock import patch

        import httpx

        from nova.models import ProviderKey

        row = self.db.query(Model).filter_by(exposedId="primary/fast").one()
        provider = self.db.query(Provider).filter_by(key="primary").one()
        self.db.add(ProviderKey(providerId=provider.id, label="k", apiKey="test-token"))
        self.db.commit()

        calls: list[str] = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(500, json={"error": "boom"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        # `attempt_upstream` opens its own short-lived session to pick a key;
        # point that at the test database so the key above is found.
        scoped = sessionmaker(bind=self.engine, expire_on_commit=False)
        with patch.object(self.gateway, "NONSTREAM_CLIENT", client), \
             patch("nova.database.SessionLocal", scoped):
            result = asyncio.run(
                self.gateway.attempt_upstream(row, [{"role": "user", "content": "hi"}], None))
        self.assertIsNone(result)  # a 500 is a miss, never a fabricated answer
        self.assertTrue(calls)

        self.gateway.note_attempt(row, False, "500 from upstream")
        self.assertTrue(self.registry.is_locked("primary", "fast"))
        self.gateway.note_attempt(row, True)
        self.assertFalse(self.registry.is_locked("primary", "fast"))


class StreamHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    """A heartbeat must never tear down the upstream read it is watching."""

    async def test_heartbeats_interleave_without_cancelling_the_read(self):
        from unittest.mock import patch

        from routers import gateway

        async def upstream():
            await asyncio.sleep(0.2)  # long prefill: silent past the heartbeat
            yield {"text": "he", "finish_reason": None, "tool_calls": None}
            yield {"text": "llo", "finish_reason": None, "tool_calls": None}

        with patch.object(gateway, "SSE_HEARTBEAT_S", 0.03):
            pieces = gateway._iterate_with_heartbeat(upstream(), b": hb\n\n")
            kinds = []
            first = None
            async for kind, value in pieces:
                kinds.append(kind)
                if kind == "hb":
                    continue
                if kind == "piece":
                    first = value
                break
            rest = []
            async for kind, value in pieces:
                if kind == "hb":
                    continue
                if kind == "eof":
                    break
                rest.append(value["text"])
        self.assertIn("hb", kinds, "a silent upstream must produce a heartbeat")
        self.assertEqual(first["text"], "he", "the real first chunk must still arrive")
        self.assertEqual(rest, ["llo"], "the rest of the stream must survive the heartbeat")

    async def test_a_finished_stream_reports_eof(self):
        from routers import gateway

        async def empty():
            return
            yield  # pragma: no cover

        async for kind, _value in gateway._iterate_with_heartbeat(empty(), b": hb\n\n"):
            self.assertEqual(kind, "eof")
            break
        else:  # pragma: no cover
            self.fail("expected an eof marker")


if __name__ == "__main__":
    unittest.main()
