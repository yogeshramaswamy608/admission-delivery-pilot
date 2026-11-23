# H1 | Validate the receiver

## What the release team needs to know

The hospital has already admitted the patient. A notice tells the receiving software about that admission. A missed admission notice means the software missed that message. All records in this test are fictional.

H1 establishes a baseline. Can eCRNow accept four ordinary admission notices, store the complete third admission, and keep exactly one copy when that same notice arrives again? The later recovery test needs this baseline to interpret a missing, late, or duplicate record.

## Inputs

- Four original files in `fixtures/` represent four inpatient admissions for fictional patient `drill-patient-1`. Use the files byte-for-byte, in order. Resend exactly the third file.
- Each file is a Fast Healthcare Interoperability Resources (FHIR) Release 4 history Bundle on the same subscription. Entry one describes the active subscription event, its sequence number and its focus. Entry two contains the corresponding Encounter, including the patient reference.
- Test with eCRNow v4.0.4 at source revision `89da654471b651dd70592f371e577fffa3c5f04e`. The supplied setup selects the publisher's pinned image and a fresh PostgreSQL database. Save the actual deployed versions.
- The official format check uses the Health Level Seven (HL7) Java validator 7.0.5, FHIR 4.0.1 and the R4-specific Subscriptions Backport 1.1.0 package. Preserve each report and its link to the original file.
- Comparison versions in the project record are Inferno Subscriptions Test Kit 0.13.0 and fhir-candle 2026.708.1348. H1 sends the fixed notices directly to eCRNow. A comparison-kit run and a live source-server run are separate observations.

## What earns a technical pass

| Step | Action | Evidence required |
|---|---|---|
| 1. Validate each message | Check all four original files against the official R4 notification profile. | Four individual passing reports, matching input hashes, exact package and validator versions, and a documented resolution for any diagnostic requiring review. |
| 2. Establish a clean start | Look up all four patient/admission pairs at the actual receiver. | Four HTTP 204 responses before the first delivery. A pre-existing record stops the test. |
| 3. Deliver each notice | Send admissions 1, 2, 3 and 4 to `POST /api/receive-notification`, with a fresh `X-Request-ID` each time. | Four HTTP 200 responses, with request identifiers, timestamps and the exact sent bytes. |
| 4. Inspect admission three | Read `GET /api/notificationContexts?patientId=drill-patient-1&notificationResourceId=drill-adm-3`. | HTTP 200 with exactly one row. Check the patient, admission ID, Encounter type, encounter-start trigger and every field of the stored Encounter against input three. |
| 5. Redeliver admission three | Send the identical bytes from step 3 with a fresh transport request identifier. | HTTP 422 with a response identifying the duplicate constraint. A generic 422 is insufficient. |
| 6. Count and compare again | Repeat the admission-three lookup. | HTTP 200 with exactly one row and the same complete matching Encounter. A second row, altered record or missing field stops the test. |

The test sequence produces 11 receiver exchanges: 4 initial reads + 4 deliveries + 1 read + 1 redelivery + 1 final read. Any startup probes are recorded separately. HTTP means Hypertext Transfer Protocol.

## Run the fallback

Extract this ZIP into a new directory on a private Linux test computer. It needs Python 3.11 or later, Java 17 or later and local Docker Engine with Compose v2 capable of running Linux/amd64 containers. The computer must permit public software downloads, private container networking and local storage for the database. No hospital account or real patient data is required.

From the extracted directory, preview the test, then run it:

```sh
python3 start_h1.py --dry-run
python3 start_h1.py
```

The first command previews the sequence. The second verifies the kit, stages the public validation software, starts a fresh private receiver and database, runs validation, and attempts the live sequence only after validation passes. The setup and its official dependencies require disk space beyond the small kit. The first download includes the receiver image. Actual download and startup times depend on the test computer.

This fallback's live startup remains unverified. An operator can use the exact text in `H1_RUN_REQUEST.txt` alongside this kit. Execution requires a computer capable of running the commands.

The setup downloads public dependencies first. It then runs the four official format checks with the validation container's network disabled. The receiver and database share an internal test network. The validator permits fictional example.org addresses on the four original, hash-checked inputs. It loads the published notification code list from this kit alongside the Release 4 guide. All admission deliveries target the local test receiver.

## What to return

Return the generated `H1-evidence-*.zip` from the attempt, including when it stops. The command prints its path. Keep the original output directory on the test computer. Each attempt uses a fresh directory so a rerun preserves earlier observations.

The reviewer needs the run summary, setup observations, exact version and build record, four validation reports, hashes, and every raw receiver request and response reached. The two admission-three reads and the rejection body establish the duplicate result. If execution stops, return the last successful step, failing command and original error, with the reports available up to that point.

If the validator flags a condition it could not check, retain the report and resolve the condition before continuing. Never substitute invented output, a mock receiver or an assumed pass for an observed result. Keep passwords, keys, private database data, host settings and personal records out of the evidence.

### If the official reports require review

The first run always starts without a review. An error or fatal issue stops the test. For a warning or flagged informational message, find `issues-requiring-review.json` inside the attempt. It lists the exact finding, input file and a key tied to the saved report. Investigate what the validator could not check. An unresolved item keeps H1 open.

After each finding is substantiated, a reviewer can supply a separate JSON object keyed by the exact keys in that list. Each entry requires `conformance_checked` set to `true` and a written `reason` identifying the checked evidence. Preserve the original reports. From this same extracted kit, run the following with the paths to that earlier attempt directory and the completed review file supplied as shell variables.

```sh
python3 start_h1.py --review-attempt "$H1_ATTEMPT_DIR" --warning-review "$H1_REVIEW_FILE"
```

The command creates a separate attempt and a fresh receiver. The kit rechecks the original input and report hashes, software identity, reports and recorded decisions before sending any notices. The technical result goes to Yogesh for acceptance.

The setup leaves its receiver and database available for inspection. After preserving the evidence, the operator can stop that attempt's services using the exact project name in its deployment record. See `runtime/README.md`.

## Decision and timing

H1 started Nov 18, 2026 at 7:21:12am Pacific. The target is Nov 19, 2026. The current managed computer cannot run the live test. The expected results above define success on a capable test computer.

Yogesh reviews the evidence and decides acceptance. Recovery of a deliberately withheld notice, the deadline for that recovery, and any independent team's release decision belong to later work.

## Sources

- [eCRNow release 4.0.4](https://github.com/drajer-health/eCRNow/releases/tag/v4.0.4)
- [HL7 R4 notification profile, version 1.1.0](https://hl7.org/fhir/uv/subscriptions-backport/STU1.1/StructureDefinition-backport-subscription-notification-r4.html)
- [R4-specific guide package](https://hl7.org/fhir/uv/subscriptions-backport/STU1.1/downloads.html)
- [HL7 validator 7.0.5](https://github.com/hapifhir/org.hl7.fhir.core/releases/tag/7.0.5)
- [Published notification type CodeSystem](https://hl7.org/fhir/R4B/codesystem-subscription-notification-type.canonical.json) and [ValueSet](https://hl7.org/fhir/R4B/valueset-subscription-notification-type.canonical.json) from Release 4B, version 4.3.0. The Release 4 Backport guide binds the notification type to this ValueSet. The notice itself is validated as Release 4, version 4.0.1.
