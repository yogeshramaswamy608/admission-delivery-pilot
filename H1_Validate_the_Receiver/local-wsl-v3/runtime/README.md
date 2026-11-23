# Start the H1 receiver

Use the kit's main command for a complete H1 run. To run just the receiver setup from the extracted kit root, run:

```sh
python3 runtime/start.py --output evidence/deployment
```

This command needs a local Docker engine for Linux/amd64 with Docker Compose v2 and Python 3.10 or later. The full kit requires Python 3.11 or later. The engine must support a private bridge and a local RAM-backed volume. Setup downloads three public images by digest. The database starts empty, with generated credentials available to each service in memory. The receiver listens on its own loopback interface. The application and database share only the new private network. Both have zero host-published ports. A separate run gets a fresh project and database.

| Component | Release |
| --- | --- |
| Receiver | eCRNow v4.0.4, source commit `89da654471b651dd70592f371e577fffa3c5f04e` |
| Receiver image | `drajerhealth/ecr-now@sha256:28a195c6138316988689adf21afcc2a239bf1f95dc9bc0b52e87fb113adc2976` |
| Database image | PostgreSQL 15.20, Debian Bookworm, `postgres@sha256:e924003122c1459b25ebe2af0f5b8becc8b50ccee1354b304e5ba7ec1e0bb42c` |
| Read-only readiness and test client image | Python 3.11, Debian Bookworm slim, `python@sha256:e8c69d86735f6df3efa06b324694fe51f0422785f83a3bd463052636c2421944` |

The launcher takes the database scripts directly from the receiver image. It checks each script against the tagged release and follows the publisher's fresh-install sequence. It checks the actual five-column uniqueness constraint before starting the receiver. The database contains no configured hospital or public health destinations. The receiver uses its published authentication default inside this private test. Neither notifications nor credentials are sent to a public endpoint.

The launcher waits for eCRNow's application-ready event and startup delay. The Python test container shares the receiver's network namespace and queries an unused synthetic identifier over its loopback interface. A successful `build.json` records the empty HTTP 204 response, the receiver's internal address, actual image and container identities, network, database version and constraint. H1 still needs profile validation and the independent receipt, content and duplicate tests. A blocked attempt retains its stage, reason and available container diagnostics. A rerun must use a different evidence directory.

The public evidence files are `progress.jsonl`, `compose.json`, `migration.json`, `state.json` and, on readiness success, `build.json`. The launcher never writes database credentials into the evidence directory. Retain the generated evidence before ending a temporary machine. On a persistent test machine, stop the two project containers after the test using the exact project name recorded in `state.json`. This stops the services without deleting the database.

## Publisher references

- [Pinned build and packaged resource locations](https://github.com/drajer-health/eCRNow/blob/89da654471b651dd70592f371e577fffa3c5f04e/Dockerfile)
- [Pinned receiver and database sample configuration](https://github.com/drajer-health/eCRNow/blob/89da654471b651dd70592f371e577fffa3c5f04e/docker-compose.yml)
- [Release fresh-install sequence](https://github.com/drajer-health/eCRNow/blob/89da654471b651dd70592f371e577fffa3c5f04e/documents/Database_Migration_Guide_v4.xx.md)
- [eCRNow application defaults](https://github.com/drajer-health/eCRNow/blob/89da654471b651dd70592f371e577fffa3c5f04e/src/main/resources/application.properties)
- [Release notification constraint](https://github.com/drajer-health/eCRNow/blob/89da654471b651dd70592f371e577fffa3c5f04e/src/main/resources/db-scripts/4.xx_db_migration_scripts/psql_db_scripts/schema_alterations_and_constraints/notification_context.sql)
- [Release startup event](https://github.com/drajer-health/eCRNow/blob/89da654471b651dd70592f371e577fffa3c5f04e/src/main/java/com/drajer/ecrapp/EcrApp.java)
- [Receiver image manifest](https://hub.docker.com/layers/drajerhealth/ecr-now/ecr-now-4.0.4-lts/images/sha256-28a195c6138316988689adf21afcc2a239bf1f95dc9bc0b52e87fb113adc2976)
- [PostgreSQL official image release metadata](https://github.com/docker-library/repo-info/blob/master/repos/postgres/remote/15.20-bookworm.md)
- [Python official image release metadata](https://github.com/docker-library/repo-info/blob/master/repos/python/remote/3.11-slim-bookworm.md)
- [Docker maintainer explanation of port publishing on internal-only networks](https://github.com/moby/moby/discussions/53256)
