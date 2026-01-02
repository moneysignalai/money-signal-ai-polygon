import time

from bots import options_common


def test_iter_option_contracts_uses_last_trade_p_and_s(monkeypatch):
    contract = "O:TEST240101C00100000"
    trade_ts = int(time.time() * 1_000_000_000)

    chain_response = {
        "results": [
            {
                "ticker": contract,
                "last_trade": {},
                "last_quote": {},
            }
        ],
        "underlying": {"last": {"price": 25.0}},
    }

    monkeypatch.setattr(
        options_common,
        "get_option_chain_cached",
        lambda symbol, ttl_seconds=60: chain_response,
    )
    monkeypatch.setattr(
        options_common,
        "get_last_option_trades_cached",
        lambda full_symbol: {"results": {"p": 1.23, "s": 45, "t": trade_ts}},
    )

    contracts = options_common.iter_option_contracts("TEST")

    assert len(contracts) == 1
    parsed = contracts[0]
    assert parsed.premium == 1.23
    assert parsed.size == 45
    assert parsed.notional == 1.23 * 45 * options_common.OPTION_MULTIPLIER
