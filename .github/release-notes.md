**Project owner:** Yogesh Ramaswamy. Built with an AI coding assistant under the owner's direction. DECISIONS.md in the package separates the owner's decisions from the assistant's work.

**What it does.** The package measures what an eCRNow receiver stores when a notice for one fictional hospital admission is missing, late, out of order or repeated. It compares recovery off, replay of a numbered event and a fresh search of the patient's Encounters. The five delivery cases are normal, missing, late (notice 3 at +6 seconds), reordered (notice 3 at +0.25 seconds, within the one-second grace period) and duplicate (notice 3 again at +2 seconds). Each of the 45 trials starts from a fresh receiver, database and recovery state.

**Download.** Use the two assets `admission-delivery-pilot-v1.0.2.zip` and `admission-delivery-pilot-v1.0.2.zip.sha256`. GitHub's automatic "Source code" archives are different files and do not match the digest. The ZIP's SHA-256 is `e58f8f7ef7f223d29a669302a8498c163492659d9cb1ba174acdba82a7c04b20`. README.md in the ZIP gives the checks and the run step by step.

**Run of this release.** On {RUN_DATE} (UTC), GitHub Actions run [{RUN_ID}]({RUN_URL}) downloaded the two assets above onto a fresh GitHub-hosted runner ({OS_NAME}, runner image {RUNNER_IMAGE}, {PYTHON_VERSION}, Docker Engine {DOCKER_ENGINE}, {COMPOSE_VERSION}), checked the digest, and ran the file check, the offline controls and the live matrix. Result: {PASS_COUNT} of 45 trials passed, {FLAGGED_COUNT} flagged, {NOT_RUN_COUNT} not run, {GLOBAL_COUNT} global findings. The package's second scoring program re-read the raw evidence and agreed with the runner on all {AGREE_COUNT} trials.

**Evidence of that run.** `{ARCHIVE_NAME}` is the trial archive (SHA-256 `{ARCHIVE_SHA256}`). `{AUDIT_NAME}` is the second program's audit file (SHA-256 `{AUDIT_SHA256}`). `{RECORD_NAME}` holds the host record, the launch log and the source checks (SHA-256 `{RECORD_SHA256}`). README.md, under "Read and retain the evidence", gives the command that re-scores the run offline from the trial archive. The evidence names the runner's own paths under /home/runner and the private addresses of the per-trial Docker networks the pilot creates.

**Versions.** v1.0.2 changes only documentation and version strings from v1.0.1. Neither v1.0.0 nor v1.0.1 was published. DECISIONS.md records each version and its runs.

**Scope.** One synthetic patient and four fixed admissions on pinned software. The measurements cover receiver storage at controlled deadlines. Clinical use, production latency and reliability, other products, and independent adoption require separate evidence.

**License.** Apache-2.0 for the project material Yogesh Ramaswamy can license. No upstream software is included. NOTICE lists each component the run downloads and its license.
