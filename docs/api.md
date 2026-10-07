# Verified Lighter contracts

Inspection date: 2026-10-07. Installed `lighter-sdk==1.1.6`. Official SDK source inspected at `04bee9ba9811b87ef1cccc568e7f590b5fe649bb`. PyPI dependencies are frozen in `uv.lock` and hash-pinned in `requirements.lock`. The Linux x86_64 native signer loads successfully.

Authoritative references:

* [Official Python SDK](https://github.com/elliottech/lighter-python)
* [WebSocket reference](https://apidocs.lighter.xyz/docs/websocket-reference)
* [Signing transactions](https://apidocs.lighter.xyz/docs/trading)
* [Account data](https://apidocs.lighter.xyz/docs/account-data)
* [Account tiers](https://apidocs.lighter.xyz/docs/account-types)
* [Rate limits](https://apidocs.lighter.xyz/docs/rate-limits)
* [Volume quota](https://apidocs.lighter.xyz/docs/volume-quota-program)
* [Native fee/margin constants](https://github.com/elliottech/lighter-go/blob/main/types/txtypes/constants.go)

| Requirement | Exact adapter/API contract |
| --- | --- |
| Mainnet | HTTPS `https://mainnet.zklighter.elliot.ai`; chain ID 304 |
| Market data WS | `wss://mainnet.zklighter.elliot.ai/stream?readonly=true` |
| Authenticated WS | `wss://mainnet.zklighter.elliot.ai/stream` |
| BTC discovery | `OrderApi.order_book_details(filter="perp")`, exactly one active BTC perp |
| Depth | `order_book/{market_id}`, snapshot then absolute level-size deltas |
| Sequence | Current `begin_nonce` equals previous `nonce`; API offsets strictly increase but need not be consecutive |
| BBO/trades | `ticker/{market_id}`, `trade/{market_id}` |
| Authenticated BTC updates | `account_market/{market_id}/{account_index}`, auth token in subscribe message |
| Account/position | `AccountApi.account(by="index", value=str(account_index), active_only=False)` |
| Limits/fees | `AccountApi.account_limits(account_index=..., authorization=...)` |
| Active orders | `OrderApi.account_active_orders(authorization=..., account_index=..., market_id=...)` |
| Order reconciliation | `OrderApi.account_orders(authorization=..., client_order_indexes=str(id), account_index=...)` |
| Recent account fills | `OrderApi.trades(sort_by="timestamp", sort_dir="desc", limit=100, market_id=..., account_index=..., authorization=...)` |
| Signing | `SignerClient(url=..., account_index=..., api_private_keys={key_index: key}, chain_id=304, nonce_management_type=NONE)` |
| Registered key check | `SignerClient.check_client()` in a worker thread |
| Auth | `create_auth_token_with_expiry(api_key_index=...)`, fresh token on reconnect |
| Nonce | `TransactionApi.next_nonce(account_index=..., api_key_index=...)`, serialized local increments |
| Create | `sign_create_order(...)`, native size/price integers, unique client ID <2^48 |
| Market IOC | type 1, time-in-force 0, expiry 0; supplied price is an execution limit |
| Close | `reduce_only=True`, opposite side, actual remaining quantity |
| WS submission | `jsonapi/sendtx`, JSON data `{id, tx_type, tx_info}` using the official SDK example format |
| HTTP submission | `TransactionApi.send_tx(tx_type=..., tx_info=...)`; exactly one POST attempt |
| Cancel | `sign_cancel_order(market_index=..., order_index=..., nonce=..., api_key_index=...)` |
| Leverage | `sign_update_leverage(market, 10000 // leverage, margin_mode=0, nonce=..., api_key_index=...)`, authoritative IMF confirmation |
| Uncertain transaction | `TransactionApi.tx(by="hash", value=...)`; failed status 0 proves failure; accepted/pending does not prove fills |

Observed public BTC metadata: market ID **1**, size precision **5**, price precision **1**, minimum base size **0.00007 BTC**, minimum quote size **$10**, minimum native IMF **200** (current maximum leverage **50×**). These are observations, not application constants; the application discovers them at every start.

Current documented constraints:

* Depth is batched every 50 ms; BBO is triggered on nonce updates. No software timing claim can remove exchange batching.
* Standard/Plus taker delay is 300 ms; Premium taker delay is 140 ms. Premium fees and capacity vary with stakes/credits. The application never switches tiers automatically.
* Native fee ticks have denominator 1,000,000. Native IMF ticks have denominator 10,000. Account JSON IMF units must be confirmed for the actual account representation; leverage verification uses an explicit scale.
* Standard has 60 aggregate reads/transactions per rolling minute. Plus/Premium reads are weighted; current account/accountLimits weight is 300, active-order/order-lookup weight 100, account trades 200, nextNonce 6.
* Plus/Premium volume quota is shared across L1 subaccounts. Create orders consume quota; single-order cancels do not. The initial available counter must be supplied if absent from accountLimits, and subsequent send acknowledgements refresh it when present.
* An acknowledgement with code 200 establishes acceptance only. Terminal order status, authoritative position updates, and fills are separate evidence.
* Public `readonly=true` data access does not establish trading permission for the same region or authenticate an account.

Authenticated account payloads, WS transaction acknowledgements, leverage updates, and live fills remain unverified against a funded account. No exchange transaction was submitted during onboarding.
