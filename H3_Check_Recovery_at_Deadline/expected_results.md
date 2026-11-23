# H3 confirmed deadline and scoring criteria

Yogesh accepted H2, authorized H3 and confirmed these criteria before testing on Nov 19, 2026. H3 asks whether one complete admission record is present at the deadline and whether it stays correct when the withheld original arrives.

## Clock and delivery

Use a controlled test clock. Start at zero when the receiver acknowledges notice 4. Allow one second for notice 3 to arrive out of order. Recovery may start at +1 second. Fix the deadline at +5 seconds from zero in every case, independent of when recovery completes. These are pilot settings. Record actual monotonic elapsed time and UTC timestamps separately so the distinction from real service latency is visible.

Take one receiver snapshot at the +5-second clock tick. Hold the clock and later writes while capturing and saving the raw response. A snapshot obtained after the cutoff, an unreadable response, or a receiver write whose ordering across the cutoff cannot be established makes that deadline result **unverified**. Never move the cutoff to wait for recovery or infer the earlier state from a later lookup.

Release the original third notice once at +6 seconds, after saving the deadline evidence. Use its original payload and a fresh request ID. Save the delivery response and take a separate final receiver snapshot.

## What counts as a pass

Use a fresh receiver for each case. Before delivery, require empty responses (HTTP 204) for admissions 1 through 4. Deliver notices 1, 2 and 4 once each and confirm each was accepted and stored exactly once. Keep the withheld original unavailable to recovery. Use the H2 replay and patient search methods.

| Case | Admission 3 at +5 seconds | After the late original |
| --- | --- | --- |
| Recovery off | Zero rows | One complete original |
| Replay event 3 | One complete recovered row | Still one complete recovered row |
| Search patient admissions | One complete recovered row | Still one complete recovered row |

Count every returned row for patient `drill-patient-1` and Encounter `drill-adm-3` before checking its contents. For a one-row result, require the row's patient, admission ID, Encounter type and `encounter-start` trigger to match. Decode `notificationData` and compare its entire Encounter, including the patient reference and admission identifier `https://example.org/admission-identifiers` / `TEST-ADMISSION-03`. Allow equivalent serialization, such as JSON key order or equivalent narrative markup.

Score recovered rows against the saved source Encounter, including the exact version and timestamp from that run. Verify the source's admission content against the original fixture, accounting only for the source-added version and timestamp. In the control, score the final row against the original. An unexpected count, incomplete Encounter or incorrect field on a readable, correctly timed snapshot fails. Save the late delivery response and inspect the final row regardless of that response. In each recovery case, verify the same stored row ID, recovery request ID and complete Encounter survived late delivery.

Retain the initialization checks, delivery and recovery exchanges, clock log, deadline and final snapshots, reference records, field differences and verdict for each case. The pinned Inferno Subscriptions Test Kit 0.13.0 excludes missed-notification recovery from its scope. For H3 technical completion, document how the executed deadline and late-delivery assertions extend that coverage. Yogesh decides acceptance. [Inferno 0.13.0 scope](https://github.com/inferno-framework/subscriptions-test-kit/blob/v0.13.0/README.md#L45-L46)
