"""Check the recovery contract without network services or an expected case fixture."""

import json
import unittest
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from recovery import Gap, RecoveryError, RecoveryLayer, SourceReader, TOPIC


BASE = "http://127.0.0.1:8080/fhir/r4/"
PATIENT = "contract-patient"
SUBSCRIPTION = "encounter-start"
TEMPLATE_BASE = "https://example.org/fhir"
WHEN = "2026-01-01T11:35:00Z"


def contents(value):
    return json.dumps(value).encode()


def encounter(identifier, patient=PATIENT):
    return {"resourceType": "Encounter", "id": identifier, "status": "in-progress",
            "class": {"code": "IMP"}, "subject": {"reference": f"Patient/{patient}"},
            "period": {"start": WHEN}, "meta": {"versionId": "7"},
            "identifier": [{"system": "https://example.org/admission", "value": identifier}]}


def event(number, focus):
    return {"name": "notification-event", "part": [
        {"name": "event-number", "valueString": str(number)},
        {"name": "timestamp", "valueInstant": WHEN},
        {"name": "focus", "valueReference": {"reference": focus}},
    ]}


def envelope(events, entries, kind="query-event", base=BASE.rstrip("/")):
    status = {"resourceType": "Parameters", "parameter": [
        {"name": "subscription", "valueReference": {"reference": f"{base}/Subscription/{SUBSCRIPTION}"}},
        {"name": "topic", "valueCanonical": TOPIC},
        {"name": "status", "valueCode": "active"},
        {"name": "type", "valueCode": kind},
        {"name": "events-since-subscription-start", "valueString": "93"},
        *events,
    ]}
    return {"resourceType": "Bundle", "type": "history", "id": "prior-delivery", "timestamp": WHEN,
            "meta": {"profile": ["example-profile"]},
            "entry": [{"resource": status, "request": {"method": "GET"},
                       "response": {"status": "200"}}, *entries]}


def original(number, identifier=None):
    identifier = identifier or f"persisted-{number}"
    focus = f"{TEMPLATE_BASE}/Encounter/{identifier}"
    entry = {"fullUrl": focus, "resource": encounter(identifier), "request": {"method": "POST", "url": "Encounter"},
             "response": {"status": "201"}}
    return contents(envelope([event(number, focus)], [entry], "event-notification", TEMPLATE_BASE))


def search_page(resources, next_url=None):
    return {"resourceType": "Bundle", "type": "searchset", "entry": [{"resource": value} for value in resources],
            "link": [{"relation": "next", "url": next_url}] if next_url else []}


class SourceContract(unittest.TestCase):
    def test_replay_selects_exact_event_and_embedded_focus(self):
        wanted = encounter("actual-source-replay")
        other = encounter("unrelated")
        focus = "urn:uuid:1c180675-9047-44cf-a487-c25388c7fd4f"
        resource = envelope([event(2, "Encounter/unrelated"), event(3, focus)],
                            [{"fullUrl": focus, "resource": wanted}, {"resource": other}])
        def read(url):
            parts = urlsplit(url)
            self.assertEqual(parts.path, "/fhir/r4/Subscription/encounter-start/$events")
            self.assertEqual(parse_qs(parts.query), {"eventsSinceNumber": ["3"], "eventsUntilNumber": ["3"],
                                                     "content": ["full-resource"]})
            return 200, contents(resource)
        self.assertEqual(SourceReader(BASE, SUBSCRIPTION, PATIENT, read).replay(3), (wanted, WHEN))

    def test_replay_resolves_id_only_focus_from_configured_tenant(self):
        wanted = encounter("actual-source-replay")
        focus = f"Encounter/{wanted['id']}"
        lookup = BASE + focus
        calls = []
        def read(url):
            calls.append(url)
            if url == lookup:
                return 200, contents(wanted)
            return 200, contents(envelope([event(3, focus)], [{"fullUrl": lookup, "resource": {
                "resourceType": "Encounter", "id": wanted["id"], "meta": {"tag": [{"code": "SUBSETTED"}]}}}]))
        self.assertEqual(SourceReader(BASE, SUBSCRIPTION, PATIENT, read).replay(3), (wanted, WHEN))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], lookup)

    def test_replay_blocks_external_lookup_before_requesting_it(self):
        calls = []
        def read(url):
            calls.append(url)
            return 200, contents(envelope([event(3, "https://external.invalid/fhir/Encounter/focus")], []))
        with self.assertRaisesRegex(RecoveryError, "escapes"):
            SourceReader(BASE, SUBSCRIPTION, PATIENT, read).replay(3)
        self.assertEqual(len(calls), 1)

    def test_replay_requires_exact_event_and_valid_positive_number(self):
        def read(url):
            return 200, contents(envelope([event(2, "Encounter/focus")], []))
        reader = SourceReader(BASE, SUBSCRIPTION, PATIENT, read)
        with self.assertRaisesRegex(RecoveryError, "exactly one requested"):
            reader.replay(3)
        for invalid in [0, -1, 2**63, True]:
            with self.subTest(invalid=invalid), self.assertRaisesRegex(RecoveryError, "Invalid replay"):
                reader.replay(invalid)

    def test_search_reads_all_pages_and_deduplicates_exact_records(self):
        first, second = encounter("first"), encounter("second")
        initial = BASE + f"Encounter?patient={PATIENT}"
        next_page = BASE + "Encounter?cursor=page2"
        replies = {initial: search_page([first], "?cursor=page2"), next_page: search_page([first, second])}
        reader = SourceReader(BASE, SUBSCRIPTION, PATIENT, lambda url: (200, contents(replies[url])))
        self.assertEqual(reader.search(), {"first": first, "second": second})

    def test_search_rejects_cross_tenant_links_loops_and_wrong_subject(self):
        for next_url, message in [
            ("/fhir/other/Encounter", "escapes"), (f"?patient={PATIENT}", "repeated next"),
            ("Encounter/%2e%2e/Encounter", "escapes"),
        ]:
            with self.subTest(next_url=next_url), self.assertRaisesRegex(RecoveryError, message):
                SourceReader(BASE, SUBSCRIPTION, PATIENT,
                             lambda _: (200, contents(search_page([], next_url)))).search()
        with self.assertRaisesRegex(RecoveryError, "subject differs"):
            SourceReader(BASE, SUBSCRIPTION, PATIENT,
                         lambda _: (200, contents(search_page([encounter("wrong", "other-patient")])))).search()


class DeliveryContract(unittest.TestCase):
    def layer(self, read, post, observed=(1, 2, 4)):
        layer = RecoveryLayer(BASE, SUBSCRIPTION, PATIENT, 5, read, post)
        for sequence in observed:
            layer.observe_forwarded(original(sequence), 10 + sequence)
        return layer

    def unexpected_transport(self, *_):
        self.fail("Transport must be untouched.")

    def test_boundary_repeated_notice_then_original_fills_gap(self):
        layer = self.layer(self.unexpected_transport, self.unexpected_transport)
        self.assertEqual(layer.gap(), Gap(3, 14, 19))
        self.assertIsNone(layer.recover("replay", 18.999))
        layer.observe_forwarded(original(2), 18.999)
        self.assertEqual(layer.gap(), Gap(3, 14, 19))
        layer.observe_forwarded(original(3), 19)
        self.assertIsNone(layer.gap())
        self.assertIsNone(layer.recover("search", 19))

    def test_wraps_unforwarded_search_resource_at_boundary_and_preserves_it_exactly(self):
        found = encounter("source-generated-id")
        read = lambda _: (200, contents(search_page([encounter("persisted-4"), found, encounter("persisted-2")])))
        posts = []
        def post(body, request_id):
            posts.append((json.loads(body), request_id))
            return 200, b"accepted"
        layer = self.layer(read, post)
        outcome = layer.recover("search", 19)
        self.assertEqual((outcome.admission_id, outcome.event_number, outcome.receiver_status), (found["id"], 3, 200))
        self.assertIsNone(layer.recover("search", 100))
        sent, request_id = posts[0]
        self.assertEqual(len(posts), 1)
        self.assertEqual(str(UUID(request_id)), request_id)
        status, full = sent["entry"]
        self.assertEqual(sent["meta"], {"profile": ["example-profile"]})
        self.assertEqual(full["resource"], found)
        self.assertEqual(full["fullUrl"], f"{TEMPLATE_BASE}/Encounter/{found['id']}")
        parameters = {parameter["name"]: parameter for parameter in status["resource"]["parameter"]}
        self.assertEqual(parameters["subscription"]["valueReference"]["reference"], f"{TEMPLATE_BASE}/Subscription/{SUBSCRIPTION}")
        self.assertEqual(parameters["events-since-subscription-start"]["valueString"], "3")
        self.assertEqual(parameters["notification-event"], event(3, full["fullUrl"]))

    def test_empty_or_ambiguous_search_never_posts(self):
        for found in [[], [encounter("one"), encounter("two")]]:
            with self.subTest(found=found):
                layer = self.layer(lambda _: (200, contents(search_page(found))), self.unexpected_transport)
                with self.assertRaisesRegex(RecoveryError, "exactly one unforwarded"):
                    layer.recover("search", 19)

    def test_uncertain_receiver_outcome_is_not_retried_in_the_same_trial(self):
        calls = []
        found = encounter("new-id")
        def post(body, request_id):
            calls.append((body, request_id))
            raise TimeoutError("Receiver response was lost.")
        layer = self.layer(lambda _: (200, contents(search_page([found]))), post)
        with self.assertRaises(TimeoutError):
            layer.recover("search", 19)
        self.assertIsNone(layer.recover("search", 30))
        self.assertEqual(len(calls), 1)

    def test_missing_sequence_and_maximum_event_number(self):
        layer = self.layer(self.unexpected_transport, self.unexpected_transport, observed=())
        self.assertIsNone(layer.gap())
        self.assertIsNone(layer.recover("search", 30))
        largest = 2**63 - 1
        layer.observe_forwarded(original(largest - 2), 10)
        layer.observe_forwarded(original(largest), 12)
        self.assertEqual(layer.gap(), Gap(largest - 1, 12, 17))
        with self.assertRaisesRegex(RecoveryError, "exceeds"):
            layer.observe_forwarded(original(largest + 1), 15)
