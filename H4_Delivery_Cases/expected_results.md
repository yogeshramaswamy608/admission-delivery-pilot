# H4 delivery cases and expected results

Yogesh accepted H3 and authorized H4 on Nov 19, 2026. This pre-run plan checks whether admission 3 is lost, arrives late or is duplicated under five delivery schedules. The project closure target is Dec 15, 2026. All results below are predictions.

## Clock and repetitions

Use the [confirmed H3 clock and scoring rules](../H3_Check_Recovery_at_Deadline/expected_results.md). The receiver's acknowledgment of notice 4 is time zero. Send the numbered originals in the order shown, waiting for each acknowledgment before sending the next. A gap still open at +1 second becomes eligible for recovery. Freeze the controlled clock and hold later writes while saving the +5-second deadline snapshot. At +6 seconds perform any scheduled delivery and take a separate final snapshot after its response. At +6 in the other cases, take that final snapshot with no new delivery.

ASSUMED for H4. Run each case three times with recovery off, three times with event replay enabled and three times with patient search enabled. Each repetition starts with a fresh receiver, database and recovery state. This makes 45 trials. Three repetitions check repeatability under these pilot conditions. Schedule the reordered original at +0.25 seconds and the intentional duplicate at +2 seconds.

## Expected receiver state

The table shows the expected number of complete records for admission 3 at +5 seconds and at final readback. In every cell, admissions 1, 2 and 4 must each have exactly one complete original record at both reads. No other admission is expected for the fictional test patient.

| Case | Original notices in delivery order | Recovery off, deadline → final | Replay, deadline → final | Search, deadline → final |
| --- | --- | --- | --- | --- |
| Normal | 1, 2, 3, 4 | 1 → 1 | 1 → 1 | 1 → 1 |
| Missing | 1, 2, 4. Never send original 3. | 0 → 0 | 1 → 1 | 1 → 1 |
| Late | 1, 2, 4. Send original 3 at +6, after saving the deadline snapshot. | 0 → 1 | 1 → 1 | 1 → 1 |
| Reordered | 1, 2, 4. Send original 3 at +0.25 and require acknowledgment before +1. | 1 → 1 | 1 → 1 | 1 → 1 |
| Duplicate | 1, 2, 3, 4. At +2 resend the exact original 3 with a fresh request ID. | 1 → 1 | 1 → 1 | 1 → 1 |

## What determines the verdict

Start with four empty admission lookups (HTTP 204) in the pinned eCRNow v4.0.4 and PostgreSQL 15.20 deployment. Recheck the pinned source's event 3 replay and patient search before comparing modes. At each snapshot, read the full test patient's records at one held clock tick, count all rows by admission ID and compare every stored Encounter field with its reference.

In normal, reordered and duplicate delivery, all four stored records must be originals. Enabled recovery must produce zero gap-recovery requests and zero recovery deliveries. In missing and late delivery with recovery enabled, expect one recovery of admission 3 after the grace period and before the deadline. Compare the saved record with the actual source Encounter, including its version and timestamp. Require the same row ID, successful delivery request ID and full Encounter at final readback. The sender's original admission 3 payload stays outside the recovery code.

In the late case with recovery off, the first delivery of admission 3 at +6 should return HTTP 200. With replay or search, the late original should return duplicate-specific HTTP 422. The intentional duplicate at +2 should return the same duplicate response in all three modes. Record the exact response, confirm that it names the expected admission uniqueness constraint and key, stop retries for that duplicate and verify that the first stored row and its successful request ID survive. An unrelated 422 is an error.

Score a readable, correctly timed mismatch as **fail**. Mark an unreadable snapshot, missed delivery schedule or write whose order across the deadline is uncertain as **unverified**. Preserve the original observation and record any rerun separately. For every trial retain timestamped inputs, acknowledgments, source requests, recovery deliveries, deadline and final snapshots, differences and verdict. Report all 45 outcomes against this table.
