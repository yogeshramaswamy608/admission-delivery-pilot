# Admission delivery pilot v1.0.2

**Project owner:** Yogesh Ramaswamy. Cite this package as *Admission delivery pilot*, v1.0.2, and include the ZIP digest with your results.

**Implementation:** Built with an AI coding assistant under the owner's direction. [DECISIONS.md](DECISIONS.md) separates the owner's decisions from the assistant's work.

**Version:** v1.0.2 changes only documentation and version strings from v1.0.1, which added the license files. v1.0.1 (SHA-256 `ebba39b1aaf3e395e6c4d3f37a70d25d3ad1489f2628ecea084824e289946fd8`) passed a clean 45-trial live run on Nov 19, 2026 (matrix `20261120T011620Z-93884736`). v1.0.0 (SHA-256 `487524f7b2858c7131db1facb8750d8638fcffacf0aa98f2551147f6845babdd`) was the internally accepted baseline, and its H4 and H5 results were verified on v1.0.0. A result for v1.0.2 comes only from a run of v1.0.2, and the release notes give each published run beside the ZIP digest.

**Historical H1 kit:** `H1_Validate_the_Receiver/local-wsl-v3/START_HERE.md` and `H1_RUN_REQUEST.txt` are the H1 kit files as accepted on Nov 19, 2026. Their status lines predate the H1 run. They stay unchanged because the H2 comparison checks the kit's manifest hash. This README governs.

Run this package to measure what an eCRNow receiver stores when a notice for one fictional hospital admission is missing, late, out of order or repeated. The run compares recovery off, replay of a numbered event and a fresh search of the patient's Encounters. It includes four fixed notices, starts its own source and creates a fresh receiver, database and recovery state for each of 45 trials.

## Start from the ZIP

Use a writable directory on a private Ubuntu 26.04 x86-64 test machine with Python 3.11 or newer. The live run needs Bash, curl, tar, dpkg-deb, the standard checksum utilities, iproute2 and a running local Linux/amd64 Docker Engine with Compose v2 or later (the `docker compose` plugin). The account needs Docker access, either directly or through the machine's usual interactive sudo prompt. The engine must support internal bridges and a RAM-backed volume. Each trial asks Docker Hub for the three pinned images, so one matrix makes more than 100 registry requests. An unauthenticated client can hit Docker Hub's pull rate limit, which the run reports as UNVERIFIED trials with a failed image stage. Run `docker login` before the live run. The source and callback use local loopback ports. Allow disk space for the images, evidence and retained Docker resources for 45 trials.

On a cold machine the source setup downloads fhir-candle 2026.708.1348 from NuGet, ASP.NET Core 8.0.34 from Microsoft and Ubuntu's amd64 ICU 78.2-2ubuntu1 package. The receiver setup pulls digest-pinned eCRNow v4.0.4, PostgreSQL 15.20 and a Python test-client image from Docker Hub. The source data is included in the NuGet package. The runtime preparation script contains the download addresses and expected checksums. [Source runtime downloads](H2_Compare_Recovery/source-server/prepare_runtime.sh) · [Receiver and database configuration](H1_Validate_the_Receiver/local-wsl-v3/runtime/compose.json)

1. Download the two release assets, `admission-delivery-pilot-v1.0.2.zip` and `admission-delivery-pilot-v1.0.2.zip.sha256`. GitHub's automatic "Source code" archives are different files and do not match the digest. Put both assets in an empty directory and open a terminal there. Check the digest with `sha256sum -c admission-delivery-pilot-v1.0.2.zip.sha256`. It must print `OK`.
2. Extract it with `python3 -m zipfile -e admission-delivery-pilot-v1.0.2.zip .`.
3. Enter the extracted directory with `cd admission-delivery-pilot-v1.0.2`.
4. Check the distribution and fixed inputs with `python3 -B run.py --verify`. The optional `python3 -B run.py --test` runs offline controls, including all 15 case/mode combinations in memory. Both checks work without Docker. A successful check exits zero.
5. Run `python3 -B run.py`. Keep the terminal and machine awake until the launcher exits. It checks the package, starts a fresh test source, runs the matrix and audits the raw evidence. The accepted H4 matrix took 20 minutes 45 seconds on its original host. Allow additional time for a cold download and setup.

## What each trial does

The fhir-candle source serves Fast Healthcare Interoperability Resources (FHIR) Release 4. Its in-memory tenant contains patient `drill-patient-1` and Encounters `drill-adm-1` through `drill-adm-4`. Two live checks must succeed before receiver trials start. The replay request is `Subscription/encounter-start/$events?eventsSinceNumber=3&eventsUntilNumber=3&content=full-resource`. The patient search is `Encounter?patient=drill-patient-1`. Replay must return event 3 with its admission. Search must return all four admissions including the same complete third Encounter. Both retain the source version and timestamp.

The sender delivers the fixed original notices directly to a clean receiver and waits for each acknowledgment. Acknowledgment of notice 4 starts the clock. Recovery becomes eligible at +1 second. With recovery off the gap stays open. Replay reads event 3 again. Search reads the patient's current Encounters again and selects the one missing from the receiver's observed notices. Enabled recovery forwards one source-derived notice for an open gap. Only the sender and the scoring tools can use the withheld original as a fixture.

The test holds the controlled clock at +5 seconds while it saves the full patient snapshot and holds later writes. At +6 it sends any scheduled late original and waits for its response before taking the final snapshot. The saved completion time for either read can be later than its target. A write with uncertain ordering across a snapshot makes that observation unverified.

## Expected results

Each cell gives the complete admission 3 record count at +5 and at final readback. Run three repetitions of every cell. Admissions 1, 2 and 4 must each appear exactly once in both snapshots. Each original must match its fixture. Each recovered Encounter must match the live source including version and timestamp. An accepted first record must keep its row identifier and successful request identifier.

| Delivery case | Original delivery schedule | Recovery off | Event replay | Patient search |
| --- | --- | --- | --- | --- |
| Normal | 1, 2, 3, 4 | 1 → 1 | 1 → 1 | 1 → 1 |
| Missing | 1, 2, 4 only | 0 → 0 | 1 → 1 | 1 → 1 |
| Late | 1, 2, 4, then 3 at +6 after saving the deadline read | 0 → 1 | 1 → 1 | 1 → 1 |
| Reordered | 1, 2, 4, then 3 at +0.25, acknowledged before +1 | 1 → 1 | 1 → 1 | 1 → 1 |
| Duplicate | 1, 2, 3, 4, then identical 3 at +2 with a fresh request identifier | 1 → 1 | 1 → 1 | 1 → 1 |

Expect zero recovery queries and deliveries for normal, reordered and duplicate schedules. For missing and late schedules, expect one query and one successful recovery delivery in each enabled trial. In late/off the original should return HTTP 200. In late/replay, late/search and every duplicate trial, the repeated delivery should return HTTP 422 identifying `uc_notification_context_v2` and the admission 3 key. Check that the sender stops retrying that delivery and the first accepted row survives.

## Read and retain the evidence

Start with `run-history/<unique-launch>/launch.json`. It records the package manifest digest and the paths to this invocation's source and matrix as the stages are reached. Retain that launch directory, including `source-setup.log` and `run.log` when present. The source evidence is under `H2_Compare_Recovery/source-server/runs/`. Keep the complete matching run under `H4_Delivery_Cases/runs/`, its sibling `H4-<run-id>.zip` and `H4-<run-id>-audit.json`, the source run and the original ZIP digest together. A blocked, failed or unverified live run exits nonzero. Preserve its evidence alongside any fresh attempt.

Open the audit JSON. A complete passing matrix has `counts.PASS` equal to 45, 45 entries in `trials`, agreement between `runner` and `independent` in each entry, and empty `global_findings`, `flagged` and `not_run` lists. In this file, `independent` means a second program re-read the raw evidence. The following are selected fields from the accepted H4 audit, shown as a reading example. [Packaged sample summary](samples/summary.json)

| Trial | Runner / audit | Deadline rows for admission 3 | Final rows for admission 3 | Scheduled delivery HTTP |
| --- | --- | ---: | ---: | --- |
| `missing-off-r1` | PASS / PASS | 0 | 0 | No scheduled delivery |
| `late-replay-r1` | PASS / PASS | 1 | 1 | 422 |
| `reordered-search-r1` | PASS / PASS | 1 | 1 | 200 |

To re-score a run from its evidence without Docker or network access, use the package's second program. In a directory that holds the extracted package and the run's trial archive `H4-<run-id>.zip`, extract the archive into a folder named after the run with `python3 -m zipfile -e H4-<run-id>.zip <run-id>`, then run `python3 -B admission-delivery-pilot-v1.0.2/H4_Delivery_Cases/audit_h4.py <run-id> --output rescore.json`. The program refuses an output path inside the run folder. It exits zero only when no trial is flagged and there is no global finding, and `rescore.json` has the same fields as the run's `H4-<run-id>-audit.json`, so the two can be compared directly.

For a flagged trial, open `<run-id>/trials/<case>/r<repetition>/<mode>/score.json` and `clock.jsonl`. The snapshot receipts name their exact raw files under `receiver/`. Review `delivery/` for the scheduled original and `recovery/` for any runtime source read and forwarded notice. The scorer may perform its own reference read under `scoring/`. A readable, correctly timed mismatch is a failure. Unreadable evidence or uncertain timing is unverified. A run that stops early must account for the trials it never reached.

Report the package version, ZIP digest, host software versions, run identifier, outcome counts, and specific failed, unverified or unrun trial IDs. The tested scope is one synthetic patient and four fixed admissions on pinned software. The measurements cover receiver storage at controlled deadlines. Clinical use, production latency and reliability, other products, and independent adoption require separate evidence. [Decision record and standards](DECISIONS.md)

## License

The project material Yogesh Ramaswamy can license is licensed under the Apache License, Version 2.0 ([LICENSE](LICENSE)). No upstream software is included. The run downloads each upstream component, and each keeps its own license, listed in [NOTICE](NOTICE). Citation details are in [CITATION.cff](CITATION.cff).
