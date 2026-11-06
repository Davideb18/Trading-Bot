# P0.7: deployment admission and API isolation

P0.6 remains unchanged and denies all new entries, DCA and replacement orders.
This patch does not authorize trading, alter journal execution or change the
independent supervisor's emergency authority.

## Required deployment inputs

No API password, JWT key, websocket token or username has a production fallback.
Set explicit `FREQTRADE_USERNAME`, `FREQTRADE_PASSWORD`, `FREQTRADE_JWT_TOKEN`,
`FREQTRADE_WS_TOKEN`, `PROP_BYBIT_API_KEY` and `PROP_BYBIT_API_SECRET` outside the
repository. The five secrets must be distinct, at least 24 characters, have at
least eight distinct characters and contain no common placeholder markers.
These are rejection heuristics, not an entropy proof or remote credential test.
Use generated credentials and exchange-issued keys; never pad an issued key.
Keys rejected by this policy require review, not modification to make them pass.
Optional Gemini credentials do not grant trading authority.

Both containers use the same explicit exchange and API credentials. API key
fingerprints, account, environment, challenge, phase and profile must match the
existing journal and state/baseline files. CCXT credential/endpoint overrides and
verbose response logging are rejected. Local checks cannot prove that a remote
key is valid or belongs to the claimed UID; existing P0.5 direct verification
remains required. Invalid remote authentication remains fail-closed, not silently
replaced with another account. No remote authentication was attempted here.

Set `PROP_EMERGENCY_AUTHORITY=dedicated-account-single-host` only when the account
is dedicated and every writer shares the same host/directory. This is an operator
attestation, not distributed enforcement. `PROP_LOAD_ENV_FILE=1` is rejected;
the supervisor never silently reads an arbitrary project `.env`.

## Storage and startup

Set `PROP_DATA_DIR` to an existing absolute account-specific host directory and
`PROP_UID`/`PROP_GID` to its non-root Linux owner. Both containers bind that same
directory; Compose must not create a missing directory. Existing initialized
journal, risk state and baseline are required. No new history, account binding
or midnight equity is manufactured by this patch.

Preflight runs before the container launches Freqtrade or imports supervisor
workers. Core initialization repeats validation before constructing the exchange.
Every restart revalidates. Restart policy is `no`, avoiding loops on invalid
credentials; restart orchestration must deliberately repeat the same checks.

Protected directories require owner-only permissions (normally 0700); protected
files require owner-only read/write permissions (normally 0600). Checked paths
include config, risk state/baseline, journal files, existing trade database and
logs. Symlink paths and foreign ownership are rejected. New files inherit umask
0077. Permissions are inspected, never repaired automatically. Windows native
operation is refused because POSIX bits cannot establish Windows ACL security.
Docker Desktop bind mounts may fail these checks: use a Linux storage environment
whose ownership and permissions are actually enforced. No fallback weakens them.

The existing orders.lock and supervisor.lock remain the concurrency authority.
A second core/supervisor sharing the directory is refused. Copies of the journal
on separate volumes/hosts cannot be detected or coordinated by these locks. Do
not deploy the same account with such copies. This patch does not introduce a
second execution lock or distributed coordinator.

## Network and secrets

The standalone JSON binds API to 127.0.0.1. Compose overrides the container bind
to 0.0.0.0 on a dedicated bridge and publishes only `127.0.0.1:8088:8088` on the
host. The supervisor calls `http://freqtrade_prop:8088/api/v1` through Docker DNS;
it publishes no ports. The bridge permits outbound connectivity needed by the
existing services; it is intentionally not an `internal: true` network. No
service, image build, pull or exchange connection was started during verification.

The `PROP_PRIVATE_DOCKER_NETWORK` marker permits this container bind; it cannot
inspect or guarantee host firewall rules, externally edited Compose files or
administrator-created port forwarding. Localhost and Docker administrators remain
trusted. HTTP on the private bridge is not transport encryption. Do not extend it
to an untrusted multi-host network without a separate secured transport design.

Known configured secrets and their URL encodings are redacted at Python logging
record creation. Exception details are suppressed to avoid signed request leaks.
Container preflight failure emits only a fixed message. Never run unredacted
`docker compose config` or publish environment/inspect output: Docker environment
variables remain visible to host/container administrators. Direct stdout writes,
crash dumps and external tooling are outside Python logging redaction. Host secret
file ACLs and Compose's own env-file loading remain operator responsibilities;
this patch does not inspect, modify or rotate a user's `.env` or credentials.

Previously committed defaults must be considered public; any real reuse requires
operator rotation. No credentials were rotated automatically.

## Verification and reuse

Offline tests cover missing/placeholder secrets, identity and environment mismatch,
storage rejection, permission metadata, restart validation, existing ownership
locks, log redaction, network/volume configuration and pre-exchange validation.
P0.1-P0.6 suites are rerun unchanged. Docker Compose validation uses only temporary
process environment values and `config --quiet --env-file NUL`; no secrets are
printed and no user's `.env` is loaded. Full ORM runtime and Linux/container
connectivity are not claimed by these static checks.

The security module contains no One-Step risk percentages. Reuse for Two-Step
requires its validated profile and deployment wiring; no Two-Step branch was
changed. This is not challenge readiness.
