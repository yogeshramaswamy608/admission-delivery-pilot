"""Recover one missing admission notice from a private Fast Healthcare Interoperability Resources (FHIR) server."""

from copy import deepcopy
from dataclasses import dataclass
import json
from math import isfinite
import re
from urllib.parse import unquote, urlencode, urljoin, urlsplit
from uuid import uuid4


FHIR_ID = re.compile(r"[A-Za-z0-9.\-]{1,64}\Z")
EVENT_NUMBER = re.compile(r"[1-9][0-9]{0,18}\Z")
TOPIC = "http://hl7.org/fhir/us/ecr/SubscriptionTopic/encounter-start"


class RecoveryError(ValueError):
    """A source response or recovery state cannot support a safe delivery."""


def require(condition, message):
    if not condition:
        raise RecoveryError(message)


def valid_id(value):
    return isinstance(value, str) and bool(FHIR_ID.fullmatch(value)) and value not in {".", ".."}


def document(raw, expected_type):
    try:
        result = json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise RecoveryError("Response did not contain valid FHIR JSON.") from error
    require(isinstance(result, dict) and result.get("resourceType") == expected_type,
            f"Expected a FHIR {expected_type} resource.")
    return result


def children(value, field):
    result = value.get(field, [])
    require(isinstance(result, list) and all(isinstance(item, dict) for item in result),
            f"Expected objects in FHIR {field}.")
    return result


def named(values, name):
    matches = [item for item in values if item.get("name") == name]
    require(len(matches) == 1, f"Expected exactly one {name} parameter.")
    return matches[0]


def bundle_entries(bundle, expected_type):
    require(bundle.get("type") == expected_type, f"Expected a FHIR {expected_type} Bundle.")
    return children(bundle, "entry")


def status_parameters(entries):
    status = entries[0].get("resource") if entries else None
    require(isinstance(status, dict) and status.get("resourceType") == "Parameters",
            "Expected status Parameters as the first notification entry.")
    return children(status, "parameter")


def event_number(event):
    value = named(children(event, "part"), "event-number").get("valueString")
    require(isinstance(value, str) and EVENT_NUMBER.fullmatch(value), "Expected a positive event-number string.")
    number = int(value)
    require(number <= 2**63 - 1, "Event number exceeds the supported range.")
    return number


def reference_value(parameter):
    reference = parameter.get("valueReference")
    require(isinstance(reference, dict) and isinstance(reference.get("reference"), str)
            and bool(reference["reference"]), "Expected a literal FHIR resource reference.")
    return reference["reference"]


def reference_id(reference, resource_type):
    require(isinstance(reference, str), f"Expected a {resource_type} reference.")
    try:
        parts = urlsplit(reference)
    except ValueError as error:
        raise RecoveryError(f"Invalid {resource_type} reference.") from error
    require(parts.scheme in {"", "http", "https"} and (not parts.scheme or parts.hostname)
            and not (parts.query or parts.fragment or parts.username or parts.password),
            f"Invalid {resource_type} reference.")
    segments = parts.path.rstrip("/").split("/")
    if len(segments) >= 4 and segments[-2] == "_history":
        segments = segments[:-2]
    require(len(segments) >= 2 and segments[-2] == resource_type and valid_id(segments[-1]),
            f"Expected a reference to a {resource_type} instance.")
    return segments[-1]


def is_full_encounter(resource):
    if not isinstance(resource, dict) or resource.get("resourceType") != "Encounter":
        return False
    meta = resource.get("meta", {})
    require(isinstance(meta, dict), "Expected an Encounter metadata object.")
    return (isinstance(resource.get("status"), str) and bool(resource["status"])
            and isinstance(resource.get("class"), dict) and bool(resource["class"])
            and not any(tag.get("code") == "SUBSETTED" for tag in children(meta, "tag")))


def check_encounter(resource, patient_id):
    require(is_full_encounter(resource) and valid_id(resource.get("id")), "Expected a full Encounter with a valid id.")
    subject = resource.get("subject")
    require(isinstance(subject, dict) and reference_id(subject.get("reference"), "Patient") == patient_id,
            "Encounter subject differs from the patient being recovered.")
    return resource


class SourceReader:
    """Read from a single configured source tenant using a journaling GET callback.

    The callback takes an absolute URL and returns (status, raw bytes). It must
    persist each request and raw response, including non-200s, and disable redirects.
    """

    def __init__(self, source_base, subscription_id, patient_id, read_source):
        parsed = urlsplit(source_base)
        require(parsed.scheme in {"http", "https"} and parsed.hostname and not (
            parsed.username or parsed.password or parsed.fragment or parsed.query), "Invalid source base URL.")
        require(valid_id(subscription_id) and valid_id(patient_id), "Invalid subscription or patient ID.")
        self.base = source_base.rstrip("/") + "/"
        self.origin = parsed.scheme, parsed.hostname, parsed.port
        self.tenant_path = parsed.path.rstrip("/") + "/"
        self.subscription_id = subscription_id
        self.patient_id = patient_id
        self.read_source = read_source

    def _source_url(self, link, relative_to):
        require(isinstance(link, str) and bool(link) and not any(ord(char) <= 32 for char in link),
                "Invalid source reference URL.")
        try:
            target = urlsplit(urljoin(relative_to, link))
            origin = target.scheme, target.hostname, target.port
        except ValueError as error:
            raise RecoveryError("Invalid source reference URL.") from error
        path = unquote(target.path)
        require(origin == self.origin and path.startswith(self.tenant_path)
                and not (target.username or target.password or target.fragment or "\\" in path or "%" in path)
                and all(segment not in {"..", "."} for segment in path.split("/")),
                "Source reference escapes the configured FHIR tenant.")
        return target.geturl()

    def _read(self, url, resource_type):
        status, body = self.read_source(url)
        require(status == 200, f"Source returned HTTP {status} for {resource_type}.")
        return document(body, resource_type)

    def _focus_encounter(self, focus, entries):
        embedded = [entry for entry in entries if entry.get("fullUrl") == focus]
        if not embedded and not urlsplit(focus).scheme:
            absolute = self._source_url(focus, self.base)
            embedded = [entry for entry in entries if entry.get("fullUrl") == absolute]
        require(len(embedded) <= 1, "Multiple entries resolve the replay event focus.")
        resource = embedded[0].get("resource") if embedded else None
        if is_full_encounter(resource):
            if not focus.startswith("urn:"):
                require(resource.get("id") == reference_id(focus, "Encounter"),
                        "Embedded Encounter does not match its focus ID.")
            return check_encounter(resource, self.patient_id)
        encounter_id = reference_id(focus, "Encounter")
        resolved = self._read(self._source_url(focus, self.base), "Encounter")
        require(resolved.get("id") == encounter_id, "Resolved Encounter does not match its focus ID.")
        return check_encounter(resolved, self.patient_id)

    def replay(self, missing_event):
        """Return (Encounter, optional event timestamp) for the exact replay event."""
        require(type(missing_event) is int and 0 < missing_event <= 2**63 - 1, "Invalid replay event number.")
        query = urlencode({"eventsSinceNumber": missing_event, "eventsUntilNumber": missing_event,
                           "content": "full-resource"})
        relative_url = f"Subscription/{self.subscription_id}/$events?{query}"
        response = self._read(self._source_url(relative_url, self.base), "Bundle")
        entries = bundle_entries(response, "history")
        parameters = status_parameters(entries)
        subscription = reference_value(named(parameters, "subscription"))
        require(reference_id(subscription, "Subscription") == self.subscription_id,
                "Replay response belongs to a different subscription.")
        matching = [item for item in parameters if item.get("name") == "notification-event"
                    and event_number(item) == missing_event]
        require(len(matching) == 1, "The response did not identify exactly one requested event.")
        parts = children(matching[0], "part")
        focus = reference_value(named(parts, "focus"))
        timestamps = [part.get("valueInstant") for part in parts if part.get("name") == "timestamp"]
        require(len(timestamps) <= 1 and all(isinstance(value, str) and value for value in timestamps),
                "Invalid replay event timestamp.")
        return self._focus_encounter(focus, entries[1:]), timestamps[0] if timestamps else None

    def search(self):
        """Return all matched patient Encounters keyed by resource ID across search pages."""
        query = urlencode({"patient": self.patient_id})
        url = self._source_url(f"Encounter?{query}", self.base)
        results, visited = {}, set()
        while url:
            require(url not in visited, "Encounter search returned a repeated next link.")
            visited.add(url)
            page = self._read(url, "Bundle")
            for entry in bundle_entries(page, "searchset"):
                search = entry.get("search", {})
                require(isinstance(search, dict), "Invalid Bundle search metadata.")
                if search.get("mode") in {"include", "outcome"}:
                    continue
                encounter = check_encounter(entry.get("resource"), self.patient_id)
                previous = results.setdefault(encounter["id"], encounter)
                require(previous == encounter, "Encounter search returned conflicting versions of one match.")
            next_links = [link.get("url") for link in children(page, "link") if link.get("relation") == "next"]
            require(len(next_links) <= 1, "Encounter search returned multiple next links.")
            url = self._source_url(next_links[0], url) if next_links else None
        return results


@dataclass(frozen=True)
class Gap:
    event_number: int
    detected_at: float
    eligible_at: float


@dataclass(frozen=True)
class RecoveryResult:
    mode: str
    event_number: int
    admission_id: str
    request_id: str
    receiver_status: int


@dataclass(frozen=True)
class Forwarded:
    arrival: float
    admission_id: str
    bundle: dict


class RecoveryLayer:
    """Track forwarded originals and perform at most one recovery POST per observed gap.

    Use a new instance per clean receiver run and make calls in one ordered
    trial. The POST callback accepts the exact bytes and request ID and must
    journal before transmission, retain the response, and disable retries.
    """

    def __init__(self, source_base, subscription_id, patient_id, reorder_wait, read_source, post_receiver):
        require(isfinite(reorder_wait) and reorder_wait >= 0, "Invalid reordering interval.")
        self.source = SourceReader(source_base, subscription_id, patient_id, read_source)
        self.reorder_wait = reorder_wait
        self.post_receiver = post_receiver
        self._forwarded = {}
        self._attempted = set()
        self._subscription_reference = None

    def observe_forwarded(self, notice_bytes, arrival_time):
        """Record a first-delivery notice after the receiver accepts it."""
        require(isfinite(arrival_time), "Invalid arrival time.")
        bundle = document(notice_bytes, "Bundle")
        entries = bundle_entries(bundle, "history")
        require(len(entries) == 2, "Expected the H1 two-entry admission notice.")
        parameters = status_parameters(entries)
        require(named(parameters, "topic").get("valueCanonical") == TOPIC
                and named(parameters, "type").get("valueCode") == "event-notification",
                "Expected an H1 encounter-start event notification.")
        reference = reference_value(named(parameters, "subscription"))
        sub = urlsplit(reference)
        require(sub.scheme in {"http", "https"} and sub.hostname and not (
            sub.username or sub.password or sub.query or sub.fragment), "Expected an absolute H1 subscription URL.")
        require(reference_id(reference, "Subscription") == self.source.subscription_id
                and self._subscription_reference in {None, reference},
                "Forwarded notice identifies a different subscription.")
        event = named(parameters, "notification-event")
        number = event_number(event)
        focus = reference_value(named(children(event, "part"), "focus"))
        require(entries[1].get("fullUrl") == focus, "Forwarded notice focus does not select its Encounter.")
        resource = check_encounter(entries[1].get("resource"), self.source.patient_id)
        accepted = self._forwarded.setdefault(number, Forwarded(arrival_time, resource["id"], bundle))
        require(accepted.bundle == bundle, "Conflicting forwarded notices have the same event number.")
        self._subscription_reference = reference

    def gap(self):
        """Return the one bracketed missing event and the time it becomes eligible."""
        numbers = sorted(self._forwarded)
        gaps = [(left, right) for left, right in zip(numbers, numbers[1:]) if right - left > 1]
        if not gaps:
            return None
        require(len(gaps) == 1 and gaps[0][1] - gaps[0][0] == 2,
                "The forwarded sequence has more than one missing event.")
        left, right = gaps[0]
        detected_at = max(self._forwarded[left].arrival, self._forwarded[right].arrival)
        return Gap(left + 1, detected_at, detected_at + self.reorder_wait)

    def _notice(self, number, encounter, event_timestamp):
        bundle = deepcopy(self._forwarded[number - 1].bundle)
        entries = bundle["entry"]
        parameters = status_parameters(entries)
        base, separator, _ = self._subscription_reference.rpartition("/Subscription/")
        require(bool(separator), "The H1 subscription URL does not identify a FHIR base.")
        full_url = f"{base}/Encounter/{encounter['id']}"
        period = encounter.get("period", {})
        require(isinstance(period, dict), "Invalid recovered Encounter period.")
        timestamp = event_timestamp or period.get("start")
        require(isinstance(timestamp, str) and bool(timestamp), "Missing source timestamp for the recovered admission.")
        parameter_id = str(uuid4())
        bundle.update(id=str(uuid4()), timestamp=timestamp)
        entries[0].update(fullUrl=f"urn:uuid:{parameter_id}")
        entries[0]["resource"]["id"] = parameter_id
        named(parameters, "events-since-subscription-start")["valueString"] = str(number)
        event = children(named(parameters, "notification-event"), "part")
        named(event, "event-number")["valueString"] = str(number)
        named(event, "timestamp")["valueInstant"] = timestamp
        named(event, "focus")["valueReference"]["reference"] = full_url
        entries[1].update(fullUrl=full_url, resource=encounter)
        return (json.dumps(bundle, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    def recover(self, mode, now):
        """Return a delivery result when a single gap is eligible for recovery."""
        require(mode in {"replay", "search"}, "Expected replay or search recovery mode.")
        require(isfinite(now), "Invalid recovery clock value.")
        gap = self.gap()
        if gap is None or now < gap.eligible_at or gap.event_number in self._attempted:
            return None
        already_forwarded = {notice.admission_id for notice in self._forwarded.values()}
        timestamp = None
        if mode == "replay":
            encounter, timestamp = self.source.replay(gap.event_number)
            require(encounter["id"] not in already_forwarded, "Replayed Encounter was already forwarded.")
        else:
            matches = self.source.search()
            missing = matches.keys() - already_forwarded
            require(len(missing) == 1, "Encounter search did not establish exactly one unforwarded admission.")
            encounter = matches[missing.pop()]
        payload = self._notice(gap.event_number, encounter, timestamp)
        request_id = str(uuid4())
        self._attempted.add(gap.event_number)
        status, _ = self.post_receiver(payload, request_id)
        require(status == 200, f"Receiver returned HTTP {status} for the recovered notice.")
        return RecoveryResult(mode, gap.event_number, encounter["id"], request_id, status)
