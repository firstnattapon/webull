# Live API testing (Webull UAT / prod)

This bot talks to Webull through the official `webull-openapi-python-sdk`.
`scripts/live_smoke_test.py` runs the **production** code paths
(`config.py` + `broker.py` + the SDK) against a live endpoint and reports
PASS/FAIL. It is the fastest way to confirm a set of credentials is wired up
correctly before deploying.

## What it checks

1. **Authenticated read** — `get_position_and_price(SYMBOL)`. Fetching your
   position and the last price exercises credentials, HMAC signing, region,
   and endpoint resolution end-to-end. If this passes, auth works.
2. **Order preview** (opt-in, `--preview`) — previews a 1-share BUY. Never
   executes.
3. **Real order** (opt-in, `--place`) — submits a real 1-share market BUY.
   **UAT only.** Use with care.

## Run it

```bash
export WEBULL_APP_KEY=...        # app key
export WEBULL_APP_SECRET=...     # app secret
export WEBULL_ACCOUNT_ID=...     # account id
export WEBULL_ENV=uat            # uat (default) or prod
export SYMBOL=AAPL               # symbol to probe

python scripts/live_smoke_test.py            # read-only
python scripts/live_smoke_test.py --preview  # + preview a BUY (no execution)
```

Exit code is `0` when every check passes.

## Network egress requirement

The live hop needs outbound HTTPS to the Webull host:

| WEBULL_ENV | Host |
|------------|------|
| `uat`      | `th-api.uat.webullbroker.com` |
| `prod`     | `api.webull.co.th` |

Restricted execution environments (including Claude Code on the web with a
locked-down network policy) block **all** arbitrary outbound HTTPS — the SDK's
very first call (`/openapi/config`) then fails with a proxy `403 Forbidden`
before any credential is checked. That is an environment policy block, **not**
a code or credential error. Run the smoke test from a machine or environment
whose network policy allows the Webull host above.

## Offline correctness

Even where the live hop is blocked, the request the bot *builds* can be
verified with the real SDK by intercepting the transport layer. This confirms
the order goes to the v2 endpoint `/openapi/trade/stock/order/place`, carries
the `category: US_STOCK` header (the v2 SDK hardcodes `STOCK`), and sends
`instrument_type: EQUITY` / `market: US` in the body — matching the
`webull-openapi-thai-lab` reference.
