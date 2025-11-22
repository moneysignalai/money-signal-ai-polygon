from bots.status_report import record_bot_stats

async def run_premarket() -> None:
    """
    Premarket gap / momentum bot.
    """
    _reset_if_new_day()

    if not POLYGON_KEY or not _client:
        print("[premarket] POLYGON_KEY or client missing; skipping.")
        return

    if not _in_premarket_window():
        print("[premarket] Outside premarket window; skipping.")
        return

    BOT_NAME = "premarket"
    start_ts = time.time()
    alerts_sent = 0
    matches = []

    universe = _get_universe()
    if not universe:
        print("[premarket] empty universe; skipping.")
        return

    trading_day = date.today()
    today_s = trading_day.isoformat()
    print(f"[premarket] scanning {len(universe)} symbols for premarket movers ({today_s})")

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already(sym):
            continue

        prev_bar, today_bar, days = _get_prev_and_today(sym, trading_day)
        if not prev_bar or not today_bar:
            continue

        prev_close = _safe_float(getattr(prev_bar, "close", getattr(prev_bar, "c", None)))
        if prev_close <= 0:
            continue

        todays_partial_vol = _safe_float(getattr(today_bar, "volume", getattr(today_bar, "v", None)))

        pre_low, pre_high, last_px, pre_vol = _get_premarket_window_aggs(sym, trading_day)
        if last_px <= 0 or pre_vol <= 0:
            continue

        if last_px < MIN_PREMARKET_PRICE:
            continue

        move_pct = (last_px - prev_close) / prev_close * 100.0
        abs_move = abs(move_pct)
        if abs_move < MIN_PREMARKET_MOVE_PCT:
            continue
        if MAX_PREMARKET_MOVE_PCT > 0.0 and abs_move > MAX_PREMARKET_MOVE_PCT:
            continue

        pre_dollar_vol = last_px * pre_vol
        if pre_dollar_vol < MIN_PREMARKET_DOLLAR_VOL:
            continue

        rvol = _compute_partial_rvol(sym, trading_day, today_bar, days)
        if rvol < max(MIN_PREMARKET_RVOL, MIN_RVOL_GLOBAL):
            continue

        if todays_partial_vol < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol_day_partial = last_px * todays_partial_vol
        grade = grade_equity_setup(abs_move, rvol, dollar_vol_day_partial)

        direction = "up" if move_pct > 0 else "down"
        emoji = "🚀" if move_pct > 0 else "⚠️"
        bias = (
            "Long premarket momentum / gap-and-go watch"
            if move_pct > 0
            else "Gap-down pressure; watch for flush or bounce"
        )

        body = (
            f"{emoji} Premarket move: {move_pct:.1f}% {direction} vs prior close\n"
            f"📈 Prev Close: ${prev_close:.2f} → Premarket Last: ${last_px:.2f}\n"
            f"📊 Premarket Range: ${pre_low:.2f} – ${pre_high:.2f}\n"
            f"📦 Premarket Vol: {pre_vol:,.0f} (≈ ${pre_dollar_vol:,.0f})\n"
            f"💰 Day Vol (partial): {todays_partial_vol:,.0f} (≈ ${dollar_vol_day_partial:,.0f})\n"
            f"📊 RVOL (partial): {rvol:.1f}x\n"
            f"🎯 Grade: {grade}\n"
            f"🧠 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark_alerted(sym)

        extra = (
            f"📣 PREMARKET — {sym}\n"
            f"🕒 {now_est()}\n"
            f"💰 ${last_px:.2f} · 📊 RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )

        send_alert("premarket", sym, last_px, rvol, extra=extra)

        matches.append(sym)
        alerts_sent += 1

    run_seconds = time.time() - start_ts

    try:
        record_bot_stats(
            BOT_NAME,
            scanned=len(universe),
            matched=len(matches),
            alerts=alerts_sent,
            runtime=run_seconds,
        )
    except Exception as e:
        print(f"[premarket] record_bot_stats error: {e}")

    print("[premarket] scan complete.")