"""Asset library: client.assets and client.assets.characters.

Every request here goes to a scripted transport; nothing reaches the network. Each behaviour has a
counter-proof next to it, because the failure modes of a list filter or an id check are silent: a
dropped ``favorite=False`` returns the unfiltered library, and an empty id lists the collection
instead of reading one asset.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import tempfile
import unittest
import urllib.parse

# Load straight from the source tree, exactly as test_client.py does, so the tests run before
# `pip install -e .`.
ROOT = pathlib.Path(__file__).parents[1]
MODULE_PATH = ROOT / "src" / "spicyapi" / "_client.py"
SPEC = importlib.util.spec_from_file_location("spicyapi_assets_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
client_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client_module)

API = client_module.API_BASE_URL
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def ok(data: object, headers: dict[str, str] | None = None):
    body = {"code": 200, "msg": "success", "data": data, "request_id": "req_asset"}
    return 200, headers or {}, json.dumps(body).encode()


def failure(status: int, headers: dict[str, str] | None = None, **extra: object):
    body = {"code": status, "msg": "refused by the asset library", "request_id": "req_fail"}
    body.update(extra)
    return status, headers or {}, json.dumps(body).encode()


class Transport:
    """Hands out scripted responses in order and records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        if body is not None and self._is_json(headers):
            body = json.loads(body)
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    @staticmethod
    def _is_json(headers) -> bool:
        return headers.get("Content-Type") == "application/json"


def make_client(transport, *, max_retries=0, sleeps=None):
    return client_module.SpicyClient(
        "sk_assets_secret",
        transport=transport,
        max_retries=max_retries,
        sleep=(sleeps.append if sleeps is not None else (lambda seconds: None)),
        random_value=lambda: 0.5,
    )


def query(call) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlparse(call["url"]).query)


def path(call) -> str:
    return urllib.parse.urlparse(call["url"]).path


def asset(asset_id: str = "ast_hero", **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "id": asset_id,
        "uri": f"spicy://a/{asset_id}",
        "name": "Hero portrait",
        "kind": "image",
        "contentType": "image/png",
        "bytes": 1843201,
        "width": 1536,
        "height": 1024,
        "source": "task_output",
        "sourceId": "job_1",
        "createdAt": "2026-10-11T08:20:11Z",
        "updatedAt": "2026-10-11T08:20:11Z",
        "previewUrl": "https://preview.example/private",
        "favorite": False,
        "model": "bytedance/seedream-5.0-pro/text-to-image",
        "modelName": "Seedream 5.0 Pro",
    }
    record.update(overrides)
    return record


def character(character_id: str = "ety_mia", **overrides: object) -> dict[str, object]:
    images = [asset("ast_front"), asset("ast_side")]
    record: dict[str, object] = {
        "id": character_id,
        "kind": "character",
        "name": "Mia",
        "note": "",
        "coverAssetId": "ast_front",
        "cover": images[0],
        "assets": images,
        "count": len(images),
        "createdAt": "2026-10-12T08:00:00Z",
        "updatedAt": "2026-10-12T08:00:00Z",
    }
    record.update(overrides)
    return record


def contract_enum(anchor: str, field: str) -> set[str]:
    """Enum values of ``field`` in the section after ``anchor`` of the pinned contract."""
    text = (ROOT / "contracts" / "openapi.yaml").read_text(encoding="utf-8")
    start = text.index(anchor)
    field_at = text.index(field, start)
    match = re.search(r"enum:\s*\[([^\]]+)\]", text[field_at : field_at + 600])
    assert match, f"no enum for {field!r} after {anchor!r}"
    return {value.strip().strip("'\"") for value in match.group(1).split(",")}


class CreateAssetTests(unittest.TestCase):
    def test_create_posts_the_source_and_generates_an_idempotency_key(self):
        transport = Transport([ok(asset()), ok(asset())])
        assets = make_client(transport).assets

        saved = assets.create(source="task_output", task_id="job_1", index=0)
        assets.create(source="task_output", task_id="job_1", index=0)

        self.assertEqual(saved["uri"], "spicy://a/ast_hero")
        first, second = transport.calls
        self.assertEqual(first["method"], "POST")
        self.assertEqual(first["url"], f"{API}/assets")
        self.assertEqual(first["body"], {"source": "task_output", "taskId": "job_1", "index": 0})
        self.assertRegex(first["headers"]["Idempotency-Key"], UUID)
        self.assertRegex(second["headers"]["Idempotency-Key"], UUID)
        # Counter-proof: a generated key is per call, never a constant that would replay the first
        # save instead of making a second one.
        self.assertNotEqual(
            first["headers"]["Idempotency-Key"], second["headers"]["Idempotency-Key"]
        )

    def test_each_source_sends_only_its_own_fields(self):
        transport = Transport([ok(asset()), ok(asset()), ok(asset())])
        assets = make_client(transport).assets

        assets.create(source="task_output", task_id=" job_1 ", key="out/0.png", name="Hero")
        assets.create(source="upload", file_id="spicy://f/fil_1")
        assets.create(source="url", url="https://cdn.example/still.png", name="Still")

        self.assertEqual(
            [call["body"] for call in transport.calls],
            [
                {"source": "task_output", "taskId": "job_1", "key": "out/0.png", "name": "Hero"},
                {"source": "upload", "fileId": "spicy://f/fil_1"},
                {"source": "url", "url": "https://cdn.example/still.png", "name": "Still"},
            ],
        )

    def test_a_caller_key_is_sent_unchanged_and_a_blank_one_sends_nothing(self):
        transport = Transport([ok(asset())])
        assets = make_client(transport).assets

        assets.create(source="upload", file_id="fil_1", idempotency_key="save-still-1")
        self.assertEqual(transport.calls[0]["headers"]["Idempotency-Key"], "save-still-1")

        with self.assertRaisesRegex(ValueError, "idempotency_key is required"):
            assets.create(source="upload", file_id="fil_1", idempotency_key="   ")
        self.assertEqual(len(transport.calls), 1, "a blank key must fail before any request")

    def test_a_transport_retry_replays_the_same_idempotency_key(self):
        transport = Transport([
            failure(503, code=503),
            TimeoutError("socket timed out"),
            ok(asset()),
        ])
        make_client(transport, max_retries=2).assets.create(source="upload", file_id="fil_1")

        keys = [call["headers"]["Idempotency-Key"] for call in transport.calls]
        self.assertEqual(len(keys), 3)
        self.assertRegex(keys[0], UUID)
        # A retry under a new key could save the file twice.
        self.assertEqual(len(set(keys)), 1)

    def test_a_malformed_save_is_refused_before_any_request(self):
        transport = Transport([])
        assets = make_client(transport).assets
        cases = [
            ({"source": "file"}, "source must be one of"),
            ({"source": "task_output"}, "task_id is required"),
            ({"source": "task_output", "task_id": "  "}, "task_id is required"),
            ({"source": "upload"}, "file_id is required"),
            ({"source": "url"}, "url is required"),
            ({"source": "url", "url": "  "}, "url must not be blank"),
            ({"source": "upload", "file_id": "fil_1", "url": "https://x.example/a.png"},
             "url does not apply"),
            ({"source": "url", "url": "https://x.example/a.png", "task_id": "job_1"},
             "task_id does not apply"),
            ({"source": "task_output", "task_id": "job_1", "index": -1}, "non-negative"),
            ({"source": "task_output", "task_id": "job_1", "index": True}, "non-negative"),
            ({"source": "task_output", "task_id": "job_1", "key": " "}, "key must not be blank"),
            ({"source": "upload", "file_id": 42}, "file_id must be a string"),
        ]
        for arguments, message in cases:
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, message):
                assets.create(**arguments)
        self.assertEqual(transport.calls, [])


class ListAssetsTests(unittest.TestCase):
    def test_list_encodes_every_filter_and_joins_sources_with_commas(self):
        transport = Transport([ok({"items": [asset()], "nextCursor": "c2"}), ok({"items": []})])
        assets = make_client(transport).assets

        page = assets.list(
            kind="image", source=["task_output", "url"], q="hero portrait", cursor="c1", limit=20
        )
        assets.list()

        self.assertEqual(page["nextCursor"], "c2")
        first = transport.calls[0]
        self.assertEqual(path(first), "/api/v1/assets")
        self.assertEqual(
            query(first),
            {
                "kind": ["image"],
                "source": ["task_output,url"],
                "q": ["hero portrait"],
                "cursor": ["c1"],
                "limit": ["20"],
            },
        )
        # No filters means no query string at all.
        self.assertEqual(transport.calls[1]["url"], f"{API}/assets")

    def test_favorite_false_is_sent_and_none_is_omitted(self):
        transport = Transport([ok({"items": []}), ok({"items": []}), ok({"items": []})])
        assets = make_client(transport).assets

        assets.list(favorite=True)
        assets.list(favorite=False)
        assets.list(favorite=None)

        self.assertEqual(query(transport.calls[0])["favorite"], ["true"])
        # Counter-proof: False is a filter of its own ("not favorites"). Dropping it as falsy would
        # quietly return favorites and non-favorites alike.
        self.assertEqual(query(transport.calls[1])["favorite"], ["false"])
        self.assertNotIn("favorite", query(transport.calls[2]))

        with self.assertRaisesRegex(ValueError, "favorite"):
            assets.list(favorite="false")
        self.assertEqual(len(transport.calls), 3)

    def test_folder_character_and_sort_filters_are_sent(self):
        transport = Transport([ok({"items": [asset(favorite=True)]}), ok({"items": []})])
        assets = make_client(transport).assets

        assets.list(
            favorite=True, collection_id=" col_moodboard ", entity_id="ety_mia", sort="name",
            q="hero",
        )
        assets.list(sort="size", source=[])

        self.assertEqual(
            query(transport.calls[0]),
            {
                "q": ["hero"],
                "favorite": ["true"],
                "collectionId": ["col_moodboard"],
                "entityId": ["ety_mia"],
                "sort": ["name"],
            },
        )
        second = query(transport.calls[1])
        self.assertEqual(second, {"sort": ["size"]}, "an empty source list filters nothing")

    def test_an_invalid_sort_sends_nothing(self):
        transport = Transport([])
        assets = make_client(transport).assets

        for sort in ("newest", "Name", ""):
            with self.subTest(sort=sort), self.assertRaisesRegex(ValueError, "sort must be one of"):
                assets.list(sort=sort)
        # The iterator checks its arguments when it is created, not on the first next().
        with self.assertRaisesRegex(ValueError, "sort must be one of"):
            assets.iter_all(sort="largest")
        self.assertEqual(transport.calls, [])

    def test_empty_and_malformed_filters_send_nothing(self):
        transport = Transport([])
        assets = make_client(transport).assets
        cases = [
            ({"collection_id": "  "}, "collection_id is required"),
            ({"entity_id": ""}, "entity_id is required"),
            ({"q": ""}, "q must not be an empty string"),
            ({"kind": ""}, "kind must not be an empty string"),
            ({"source": ""}, "source must not be an empty string"),
            ({"source": ["upload", ""]}, "source must not be an empty string"),
            ({"cursor": ""}, "cursor must not be an empty string"),
            ({"limit": 0}, "limit must be a positive integer"),
            ({"limit": True}, "limit must be a positive integer"),
        ]
        for arguments, message in cases:
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, message):
                assets.list(**arguments)
        self.assertEqual(transport.calls, [])

    def test_an_unknown_folder_404_is_raised_as_is_and_not_retried(self):
        transport = Transport([failure(404, code=404, msg="collection not found")])
        with self.assertRaises(client_module.SpicyApiError) as raised:
            make_client(transport, max_retries=2).assets.list(collection_id="col_elsewhere")
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(raised.exception.code, 404)
        self.assertEqual(str(raised.exception), "collection not found")
        self.assertEqual(len(transport.calls), 1, "a 404 is not retried")

    def test_a_cursor_from_another_sort_fails_with_its_400_and_is_not_retried(self):
        transport = Transport([failure(400, code=400, msg="cursor is invalid")])
        sleeps: list[float] = []
        with self.assertRaises(client_module.SpicyApiError) as raised:
            make_client(transport, max_retries=3, sleeps=sleeps).assets.list(
                sort="oldest", cursor="name-c2"
            )
        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(raised.exception.request_id, "req_fail")
        self.assertEqual(str(raised.exception), "cursor is invalid")
        # The identical request gets the identical answer, so resending only burns the rate limit.
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(query(transport.calls[0]), {"sort": ["oldest"], "cursor": ["name-c2"]})


class IterateAssetsTests(unittest.TestCase):
    def test_iter_all_follows_next_cursor_and_repeats_filters_and_sort_on_every_page(self):
        transport = Transport([
            ok({"items": [asset("ast_1"), asset("ast_2")], "nextCursor": "c2"}),
            ok({"items": [asset("ast_3")], "nextCursor": "c3"}),
            ok({"items": [asset("ast_4")]}),
        ])
        ids = [
            item["id"]
            for item in make_client(transport).assets.iter_all(
                kind="video", sort="name", favorite=False, limit=2
            )
        ]

        self.assertEqual(ids, ["ast_1", "ast_2", "ast_3", "ast_4"])
        self.assertEqual(
            [query(call).get("cursor") for call in transport.calls], [None, ["c2"], ["c3"]]
        )
        for call in transport.calls:
            with self.subTest(url=call["url"]):
                sent = query(call)
                # A cursor only works with the filters and sort it was issued for.
                self.assertEqual(sent["sort"], ["name"])
                self.assertEqual(sent["kind"], ["video"])
                self.assertEqual(sent["favorite"], ["false"])
                self.assertEqual(sent["limit"], ["2"])

    def test_iter_all_starts_from_a_given_cursor(self):
        transport = Transport([ok({"items": [asset("ast_9")]})])
        ids = [item["id"] for item in make_client(transport).assets.iter_all(cursor="c5")]
        self.assertEqual(ids, ["ast_9"])
        self.assertEqual(query(transport.calls[0]), {"cursor": ["c5"]})

    def test_iter_all_is_lazy_and_stops_when_the_caller_stops(self):
        transport = Transport([
            ok({"items": [asset("ast_1"), asset("ast_2")], "nextCursor": "c2"}),
        ])
        iterator = make_client(transport).assets.iter_all()
        self.assertEqual(transport.calls, [], "nothing is fetched until iteration starts")
        for item in iterator:
            self.assertEqual(item["id"], "ast_1")
            break
        self.assertEqual(len(transport.calls), 1, "breaking out must not fetch the next page")

    def test_iter_all_refuses_a_cursor_that_repeats(self):
        transport = Transport([
            ok({"items": [asset("ast_1")], "nextCursor": "same"}),
            ok({"items": [asset("ast_2")], "nextCursor": "same"}),
        ])
        seen = []
        with self.assertRaisesRegex(client_module.SpicyApiError, "already returned"):
            for item in make_client(transport).assets.iter_all():
                seen.append(item["id"])
        self.assertEqual(seen, ["ast_1", "ast_2"])
        self.assertEqual(len(transport.calls), 2)

    def test_a_rejected_cursor_mid_walk_is_raised_and_not_retried(self):
        transport = Transport([
            ok({"items": [asset("ast_1")], "nextCursor": "c2"}),
            failure(400, code=400, msg="cursor is invalid"),
        ])
        with self.assertRaises(client_module.SpicyApiError) as raised:
            list(make_client(transport, max_retries=3).assets.iter_all(sort="size"))
        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(len(transport.calls), 2)


class AssetItemTests(unittest.TestCase):
    def test_get_update_and_delete_address_one_asset_by_id_or_reference(self):
        transport = Transport([
            ok(asset()),
            ok(asset(name="Hero v2")),
            ok({"id": "ast_hero", "deleted": True}),
        ])
        assets = make_client(transport).assets

        assets.get("ast_hero")
        renamed = assets.update("spicy://a/ast_hero", name="Hero v2")
        deleted = assets.delete("  ast_hero  ")

        self.assertEqual(renamed["name"], "Hero v2")
        self.assertEqual(deleted, {"id": "ast_hero", "deleted": True})
        self.assertEqual(
            [(call["method"], call["url"]) for call in transport.calls],
            [
                ("GET", f"{API}/assets/ast_hero"),
                ("PATCH", f"{API}/assets/ast_hero"),
                ("DELETE", f"{API}/assets/ast_hero"),
            ],
        )
        self.assertEqual(transport.calls[1]["body"], {"name": "Hero v2"})
        # Rename and delete are safe to repeat, so they carry no Idempotency-Key.
        self.assertNotIn("Idempotency-Key", transport.calls[1]["headers"])
        self.assertNotIn("Idempotency-Key", transport.calls[2]["headers"])
        self.assertIsNone(transport.calls[2]["body"])

    def test_update_sends_only_the_fields_given(self):
        transport = Transport([
            ok(asset(favorite=True)),
            ok(asset(favorite=False, name="Hero v3")),
        ])
        assets = make_client(transport).assets

        starred = assets.update("spicy://a/ast_hero", favorite=True)
        assets.update("ast_hero", name="Hero v3", favorite=False)

        self.assertTrue(starred["favorite"])
        # A favorite toggle must leave the name alone, so no name key is sent at all.
        self.assertEqual(transport.calls[0]["body"], {"favorite": True})
        # Counter-proof: False is a value to send, not an absent field.
        self.assertEqual(transport.calls[1]["body"], {"name": "Hero v3", "favorite": False})

    def test_an_update_with_nothing_to_change_sends_nothing(self):
        transport = Transport([])
        assets = make_client(transport).assets
        with self.assertRaisesRegex(ValueError, "needs a name, a favorite flag, or both"):
            assets.update("ast_hero")
        with self.assertRaisesRegex(ValueError, "favorite must be True or False"):
            assets.update("ast_hero", favorite=1)
        self.assertEqual(transport.calls, [])

    def test_ids_are_path_encoded_and_empty_references_send_nothing(self):
        transport = Transport([ok(asset())])
        assets = make_client(transport).assets

        assets.get("ast_a/../b")
        self.assertEqual(transport.calls[0]["url"], f"{API}/assets/ast_a%2F..%2Fb")

        for bad in ("", "   ", "spicy://a/", "spicy://a/   ", "ast_a\nHost: evil", None):
            with self.subTest(asset_id=bad), self.assertRaises(ValueError):
                assets.get(bad)
            with self.subTest(asset_id=bad), self.assertRaises(ValueError):
                assets.delete(bad)
            with self.subTest(asset_id=bad), self.assertRaises(ValueError):
                assets.update(bad, name="x")
        self.assertEqual(len(transport.calls), 1)

    def test_an_unknown_asset_404_is_raised_as_is_and_not_retried(self):
        transport = Transport([failure(404, code=404, msg="asset not found")])
        with self.assertRaises(client_module.SpicyApiError) as raised:
            make_client(transport, max_retries=2).assets.get("ast_gone")
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(raised.exception.code, 404)
        self.assertIsNone(raised.exception.reason)
        self.assertEqual(len(transport.calls), 1)

    def test_usage_reads_the_plan_and_scope(self):
        plan = {
            "id": "plus", "name": "Plus", "minTopupUsd": "10", "maxBytes": 524288000,
            "maxCount": 200, "maxFileBytes": 104857600,
        }
        transport = Transport([
            ok({
                "tier": plan,
                "usage": {"count": 3, "bytes": 4096},
                "full": False,
                "nextTier": None,
                "tiers": [plan],
                "dailyCreateLimit": 200,
                "createdToday": 1,
                "lifetimeTopupUsd": "25",
                "scope": {
                    "workspaceId": "ws_me", "personal": True, "role": "owner",
                    "canWrite": True, "canTopUp": True,
                },
            }),
            ok({
                "tier": plan,
                "usage": {"count": 3, "bytes": 4096},
                "full": False,
                "nextTier": {**plan, "id": "pro", "name": "Pro", "minTopupUsd": "100"},
                "tiers": [plan],
                "dailyCreateLimit": 200,
                "createdToday": 0,
                "scope": {
                    "workspaceId": "ws_team", "personal": False, "role": "developer",
                    "canWrite": True, "canTopUp": False,
                },
            }),
        ])
        assets = make_client(transport).assets

        personal = assets.usage()
        team = assets.usage()

        self.assertEqual(transport.calls[0]["method"], "GET")
        self.assertEqual(transport.calls[0]["url"], f"{API}/assets/usage")
        self.assertEqual(personal["tier"]["name"], "Plus")
        self.assertEqual(personal["lifetimeTopupUsd"], "25")
        self.assertTrue(personal["scope"]["personal"])
        # A team key without top-up rights gets neither amount; the record is passed through as
        # received, so the absence shows up as a missing key rather than an invented value.
        self.assertFalse(team["scope"]["canTopUp"])
        self.assertNotIn("lifetimeTopupUsd", team)
        self.assertNotIn("topupToNextTierUsd", team)


class CharacterTests(unittest.TestCase):
    def test_list_reads_one_page_and_get_one_character_with_its_images(self):
        transport = Transport([
            ok({"items": [character()], "nextCursor": "ec2"}),
            ok(character()),
            ok({"items": []}),
        ])
        characters = make_client(transport).assets.characters

        page = characters.list(asset_id="spicy://a/ast_front", limit=10, cursor="ec1")
        one = characters.get(" ety_mia ")
        characters.list()

        self.assertEqual(page["nextCursor"], "ec2")
        self.assertEqual(
            [image["uri"] for image in one["assets"]],
            ["spicy://a/ast_front", "spicy://a/ast_side"],
        )
        listed = transport.calls[0]
        self.assertEqual(path(listed), "/api/v1/assets/entities")
        # A spicy://a/ reference is unwrapped to the bare id.
        self.assertEqual(
            query(listed), {"assetId": ["ast_front"], "cursor": ["ec1"], "limit": ["10"]}
        )
        self.assertEqual(transport.calls[1]["method"], "GET")
        self.assertEqual(transport.calls[1]["url"], f"{API}/assets/entities/ety_mia")
        self.assertEqual(transport.calls[2]["url"], f"{API}/assets/entities")

    def test_characters_are_read_only(self):
        characters = make_client(Transport([])).assets.characters
        for verb in ("create", "update", "delete"):
            self.assertFalse(hasattr(characters, verb), f"characters.{verb} must not exist")

    def test_iter_all_follows_next_cursor_and_refuses_a_cursor_that_repeats(self):
        transport = Transport([
            ok({"items": [character("ety_1")], "nextCursor": "ec2"}),
            ok({"items": [character("ety_2")]}),
        ])
        ids = [
            item["id"] for item in make_client(transport).assets.characters.iter_all(limit=1)
        ]
        self.assertEqual(ids, ["ety_1", "ety_2"])
        self.assertEqual([query(call).get("cursor") for call in transport.calls], [None, ["ec2"]])
        self.assertTrue(all(query(call)["limit"] == ["1"] for call in transport.calls))

        looping = Transport([
            ok({"items": [character("ety_1")], "nextCursor": "same"}),
            ok({"items": [character("ety_2")], "nextCursor": "same"}),
        ])
        with self.assertRaisesRegex(
            client_module.SpicyApiError, "character listing returned a cursor"
        ):
            list(make_client(looping).assets.characters.iter_all())

    def test_an_unknown_character_404_is_raised_and_a_blank_id_sends_nothing(self):
        transport = Transport([failure(404, code=404, msg="character not found")])
        characters = make_client(transport, max_retries=2).assets.characters

        with self.assertRaises(client_module.SpicyApiError) as raised:
            characters.get("ety_gone")
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(raised.exception.code, 404)
        self.assertEqual(len(transport.calls), 1, "a 404 is not retried")
        self.assertEqual(transport.calls[0]["url"], f"{API}/assets/entities/ety_gone")

        for bad in ("", "   ", None):
            with self.subTest(character_id=bad), self.assertRaises(ValueError):
                characters.get(bad)
        with self.assertRaisesRegex(ValueError, "asset_id is required"):
            characters.list(asset_id="spicy://a/")
        with self.assertRaisesRegex(ValueError, "limit must be a positive integer"):
            characters.iter_all(limit=0)
        self.assertEqual(len(transport.calls), 1)


class ReasonTests(unittest.TestCase):
    def test_the_exported_constants_match_the_pinned_contract(self):
        # The same comparison scripts/check_contract.py makes against the published contract, run
        # here against the pinned copy so it needs no network.
        self.assertEqual(
            set(client_module.ASSET_ERROR_REASONS),
            contract_enum("    ErrorEnvelope:", "                reason:"),
        )
        self.assertEqual(
            set(client_module.ASSET_SORTS),
            contract_enum("operationId: listAssets", "name: sort"),
        )
        self.assertEqual(
            set(client_module.ASSET_SOURCES),
            contract_enum("    AssetCreateRequest:", "source:"),
        )
        self.assertIn("asset_entity_name_taken", client_module.ASSET_ERROR_REASONS)
        self.assertEqual(len(client_module.ASSET_ERROR_REASONS), 8)

    def test_data_reason_becomes_the_error_reason_unknown_values_included(self):
        transport = Transport([
            failure(400, code=400, data={"reason": "asset_library_full"}),
            failure(400, code=400, data={"reason": "asset_reason_from_the_future"}),
            failure(400, code=400),
            failure(400, code=400, data={"reason": 42}),
            failure(400, code=400, data={"reason": ""}),
        ])
        assets = make_client(transport).assets
        reasons = []
        for _ in range(5):
            with self.assertRaises(client_module.SpicyApiError) as raised:
                assets.create(source="upload", file_id="fil_1")
            self.assertEqual(raised.exception.status, 400)
            self.assertEqual(raised.exception.code, 400)
            self.assertEqual(raised.exception.request_id, "req_fail")
            reasons.append(raised.exception.reason)

        self.assertEqual(
            reasons, ["asset_library_full", "asset_reason_from_the_future", None, None, None]
        )
        self.assertTrue(client_module.is_asset_error_reason("asset_library_full"))
        self.assertFalse(client_module.is_asset_error_reason("asset_reason_from_the_future"))
        self.assertFalse(client_module.is_asset_error_reason(None))

    def test_a_forbidden_library_is_raised_as_is_and_never_retried(self):
        transport = Transport([
            failure(403, code=403, data={"reason": "asset_workspace_forbidden"}),
        ])
        with self.assertRaises(client_module.SpicyApiError) as raised:
            make_client(transport, max_retries=2).assets.list()
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(raised.exception.reason, "asset_workspace_forbidden")
        self.assertEqual(str(raised.exception), "refused by the asset library")
        self.assertEqual(len(transport.calls), 1, "a 403 is a decision, not a transient failure")

    def test_a_taken_character_name_surfaces_its_reason(self):
        transport = Transport([
            failure(409, code=409, data={"reason": "asset_entity_name_taken"}),
        ])
        with self.assertRaises(client_module.SpicyApiError) as raised:
            make_client(transport, max_retries=2).assets.characters.list()
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.reason, "asset_entity_name_taken")
        self.assertEqual(len(transport.calls), 1)

    def test_the_daily_limit_is_handed_back_with_its_reason_instead_of_waited_out(self):
        transport = Transport([
            failure(429, {"Retry-After": "3600"}, code=429, data={"reason": "asset_daily_limit"}),
            ok(asset()),
        ])
        sleeps: list[float] = []
        with self.assertRaises(client_module.SpicyApiError) as raised:
            make_client(transport, max_retries=3, sleeps=sleeps).assets.create(
                source="upload", file_id="fil_1"
            )
        self.assertEqual(raised.exception.reason, "asset_daily_limit")
        self.assertEqual(raised.exception.retry_after_seconds, 3600.0)
        self.assertEqual(raised.exception.request_id, "req_fail")
        # Waiting until midnight UTC does not fit in a 30-second request budget. Sleeping out the
        # budget would end in a local timeout that hides all three fields above.
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sleeps, [])

    def test_a_short_retry_after_is_still_honoured(self):
        # Counter-proof for the test above: only a wait longer than the remaining budget skips the
        # retry. A rate limit that clears in a second is retried as before.
        transport = Transport([
            failure(429, {"Retry-After": "1"}, code=429),
            ok({"items": []}),
        ])
        sleeps: list[float] = []
        page = make_client(transport, max_retries=3, sleeps=sleeps).assets.list()
        self.assertEqual(page, {"items": []})
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(sleeps, [1.0])


UPLOAD_TICKET = {
    "fileId": "fil_9",
    "key": "spicy://f/fil_9",
    "uploadUrl": "https://storage.example/upload/fil_9?X-Amz-Signature=deadbeef",
    "method": "PUT",
    "headers": {"Content-Type": "image/png", "Content-Length": "4"},
    "expiresAt": "2026-10-12T00:10:00Z",
    "maxBytes": 10485760,
}
COMMITTED = {
    "fileId": "fil_9",
    "status": "ready",
    "bytes": 4,
    "contentType": "image/png",
    "sha256": "a" * 64,
    "uri": "spicy://f/fil_9",
    "expiresAt": "2026-10-13T00:00:00Z",
}


class UploadAndSaveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def write(self, name: str, data: bytes = b"\x89PNG") -> str:
        file_path = os.path.join(self.directory.name, name)
        with open(file_path, "wb") as handle:
            handle.write(data)
        return file_path

    def test_upload_file_uploads_commits_and_saves_under_the_file_name(self):
        file_path = self.write("hero portrait.png")
        transport = Transport([
            ok(UPLOAD_TICKET),
            (200, {}, b""),
            ok(COMMITTED),
            ok(asset("ast_up", source="upload", name="hero portrait.png")),
        ])
        saved = make_client(transport).assets.upload_file(file_path)

        self.assertEqual(saved["id"], "ast_up")
        self.assertEqual(
            [call["method"] for call in transport.calls], ["POST", "PUT", "POST", "POST"]
        )
        self.assertNotIn("Authorization", transport.calls[1]["headers"])
        save = transport.calls[3]
        self.assertEqual(save["url"], f"{API}/assets")
        self.assertEqual(
            save["body"], {"source": "upload", "fileId": "fil_9", "name": "hero portrait.png"}
        )
        self.assertRegex(save["headers"]["Idempotency-Key"], UUID)

    def test_an_explicit_name_wins_and_a_failed_upload_saves_nothing(self):
        file_path = self.write("clip.mp4", b"\x00\x00\x00\x18")
        ticket = {**UPLOAD_TICKET, "headers": {}, "maxBytes": 94371840}
        named = Transport([
            ok(ticket),
            (200, {}, b""),
            ok({**COMMITTED, "contentType": "video/mp4"}),
            ok(asset("ast_clip", kind="video", source="upload", name="Opening shot")),
        ])
        make_client(named).assets.upload_file(file_path, name="Opening shot")
        self.assertEqual(
            named.calls[3]["body"], {"source": "upload", "fileId": "fil_9", "name": "Opening shot"}
        )

        failing = Transport([ok(ticket), (403, {}, b"<Error>denied</Error>")])
        with self.assertRaises(client_module.SpicyUploadError):
            make_client(failing).assets.upload_file(file_path)
        self.assertEqual(len(failing.calls), 2, "nothing is saved when the bytes never arrived")

    def test_the_default_name_is_cut_to_80_characters_not_bytes(self):
        # Two-byte Cyrillic and Greek plus four-byte emoji: 85 characters, 172 bytes of UTF-8,
        # which stays inside the 255-byte file name limit of common file systems.
        emoji = "\N{GRINNING FACE}"
        long_name = ("\u0436\u03bb" * 39) + emoji * 3 + ".png"
        file_path = self.write(long_name)
        transport = Transport([ok(UPLOAD_TICKET), (200, {}, b""), ok(COMMITTED), ok(asset())])
        make_client(transport).assets.upload_file(file_path)

        sent = transport.calls[3]["body"]["name"]
        # Cut on characters, so the cut lands between two whole emoji rather than inside one.
        self.assertEqual(sent, ("\u0436\u03bb" * 39) + emoji * 2)
        self.assertEqual(len(sent), 80)

    def test_lora_weights_upload_as_octet_stream_up_to_two_gib(self):
        lora = "application/octet-stream"
        self.assertEqual(client_module.content_type_for_path("/tmp/style.safetensors"), lora)
        self.assertEqual(client_module.content_type_for_path("STYLE.SAFETENSORS"), lora)
        self.assertIn(lora, client_module.UPLOAD_CONTENT_TYPES)
        limit = 2 * 1024 * 1024 * 1024
        self.assertEqual(client_module.max_upload_bytes(lora), limit)
        # The other ceilings are untouched.
        self.assertEqual(client_module.max_upload_bytes("video/mp4"), 90 * 1024 * 1024)
        self.assertEqual(client_module.max_upload_bytes("image/png"), 10 * 1024 * 1024)

        transport = Transport([ok({**UPLOAD_TICKET, "maxBytes": limit})])
        client = make_client(transport)
        client.create_upload_url(content_type=lora, byte_count=limit)
        self.assertEqual(transport.calls[0]["body"], {"contentType": lora, "bytes": limit})
        with self.assertRaises(ValueError):
            client.create_upload_url(content_type=lora, byte_count=limit + 1)
        self.assertEqual(len(transport.calls), 1)

        # The pinned contract lists the type, so the local allow-list is not ahead of the server.
        self.assertIn(lora, contract_enum("    UploadURLRequest:", "contentType:"))


class NamespaceTests(unittest.TestCase):
    def test_each_client_gets_its_own_namespace_bound_to_it(self):
        first = make_client(Transport([ok({"items": []})]))
        second = make_client(Transport([]))
        self.assertIsInstance(first.assets, client_module.SpicyAssets)
        self.assertIsInstance(first.assets.characters, client_module.SpicyAssetCharacters)
        self.assertIsNot(first.assets, second.assets)
        first.assets.list()
        # The namespace sends through its own client's transport and credentials.
        self.assertEqual(
            first.transport.calls[0]["headers"]["Authorization"], "Bearer sk_assets_secret"
        )
        self.assertEqual(second.transport.calls, [])


if __name__ == "__main__":
    unittest.main()
