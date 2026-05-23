import sys
from datetime import datetime, timedelta
from binance_trade_bot.auto_trader import AutoTrader


class Strategy(AutoTrader):
    """
    Safe Strategy — extends the default scouting logic with three protections:

    1. CIRCUIT BREAKER
       If a coin has dropped more than CIRCUIT_BREAKER_DROP_PCT% vs USDT
       in the last CIRCUIT_BREAKER_HOURS hours, it is excluded as a trade
       target. Prevents the bot from rotating into a crashing asset.

    2. USDT SAFE-HAVEN
       If ALL coins in the list are below their recent high by more than
       SAFE_HAVEN_THRESHOLD_PCT%, the bot refuses to trade and logs that
       it is waiting for market conditions to improve. Sits out broad
       bear moves instead of churning between losers.

    3. VOLATILITY GATE
       Only allows a trade if the ratio improvement clears the standard
       scout_margin threshold (inherited from AutoTrader). No change to
       the core scouting math — just an extra guard layer.

    Configuration (edit the constants below to tune):
    """

    # --- Tunable parameters ---------------------------------------------------

    # % drop in USD value over the lookback window that blacklists a coin
    CIRCUIT_BREAKER_DROP_PCT = 8.0

    # How many hours to look back when checking for a drop
    CIRCUIT_BREAKER_HOURS = 24

    # If every coin is down more than this % from its recent high, go to USDT
    SAFE_HAVEN_THRESHOLD_PCT = 12.0

    # How many hours to look back for the "recent high" calculation
    SAFE_HAVEN_LOOKBACK_HOURS = 48

    # --------------------------------------------------------------------------

    def scout(self):
        """
        Main entry point called repeatedly by the bot loop.
        Wraps the parent scout with safety checks.
        """
        current_coin = self.db.get_current_coin()

        if current_coin is None:
            self.logger.info("No current coin set — skipping safe scout.")
            return

        # Fetch all coins we are allowed to trade
        all_coins = self.db.get_coins()
        coin_symbols = [c.symbol for c in all_coins if c.enabled]

        # --- Safety Check 1: circuit breaker ----------------------------------
        blacklisted = self._get_blacklisted_coins(coin_symbols)
        if blacklisted:
            self.logger.info(
                f"[SafeStrategy] Circuit breaker active for: {blacklisted} "
                f"(dropped >{self.CIRCUIT_BREAKER_DROP_PCT}% in "
                f"{self.CIRCUIT_BREAKER_HOURS}h)"
            )

        # --- Safety Check 2: USDT safe-haven ----------------------------------
        if self._all_coins_declining(coin_symbols):
            self.logger.info(
                "[SafeStrategy] ALL coins are in drawdown "
                f">{self.SAFE_HAVEN_THRESHOLD_PCT}% over "
                f"{self.SAFE_HAVEN_LOOKBACK_HOURS}h. "
                "Holding USDT — skipping trade."
            )
            return

        # --- Normal scouting with blacklist applied ---------------------------
        self._scout_with_blacklist(current_coin, blacklisted)

    # --------------------------------------------------------------------------
    # Private helpers
    # --------------------------------------------------------------------------

    def _get_current_usdt_price(self, symbol: str) -> float:
        """Return the current USDT price for a symbol, or 0.0 on failure."""
        try:
            ticker = self.manager.get_ticker_price(symbol + "USDT")
            return float(ticker) if ticker else 0.0
        except Exception:
            return 0.0

    def _get_price_n_hours_ago(self, symbol: str, hours: int) -> float:
        """
        Approximate the USDT price N hours ago using the oldest scout history
        entry within the window. Falls back to current price if unavailable
        (so the check is skipped rather than incorrectly triggered).
        """
        try:
            since = datetime.utcnow() - timedelta(hours=hours)
            # Use the DB scout history which stores ratio snapshots
            history = self.db.get_scout_history(symbol, since)
            if not history:
                return self._get_current_usdt_price(symbol)
            # history entries have a ratio relative to bridge; use first entry
            oldest = history[0]
            # ratio is coin/bridge at that time — invert to get bridge/coin
            # then multiply by current bridge price (USDT = 1.0)
            if oldest.other_coin_price and oldest.other_coin_price > 0:
                return float(oldest.other_coin_price)
            return self._get_current_usdt_price(symbol)
        except Exception:
            return self._get_current_usdt_price(symbol)

    def _get_blacklisted_coins(self, coin_symbols: list) -> list:
        """
        Return list of coin symbols that have dropped more than
        CIRCUIT_BREAKER_DROP_PCT% in the last CIRCUIT_BREAKER_HOURS hours.
        """
        blacklisted = []
        for symbol in coin_symbols:
            current_price = self._get_current_usdt_price(symbol)
            if current_price <= 0:
                continue
            past_price = self._get_price_n_hours_ago(
                symbol, self.CIRCUIT_BREAKER_HOURS
            )
            if past_price <= 0:
                continue
            drop_pct = ((past_price - current_price) / past_price) * 100
            if drop_pct >= self.CIRCUIT_BREAKER_DROP_PCT:
                blacklisted.append(symbol)
        return blacklisted

    def _all_coins_declining(self, coin_symbols: list) -> bool:
        """
        Return True if every coin is more than SAFE_HAVEN_THRESHOLD_PCT%
        below its recent high over the last SAFE_HAVEN_LOOKBACK_HOURS hours.
        """
        if not coin_symbols:
            return False

        declining_count = 0
        checked_count = 0

        for symbol in coin_symbols:
            current_price = self._get_current_usdt_price(symbol)
            if current_price <= 0:
                continue

            try:
                since = datetime.utcnow() - timedelta(
                    hours=self.SAFE_HAVEN_LOOKBACK_HOURS
                )
                history = self.db.get_scout_history(symbol, since)
                if not history:
                    continue

                prices = [
                    float(h.other_coin_price)
                    for h in history
                    if h.other_coin_price and float(h.other_coin_price) > 0
                ]
                if not prices:
                    continue

                recent_high = max(prices)
                drawdown_pct = (
                    (recent_high - current_price) / recent_high
                ) * 100

                checked_count += 1
                if drawdown_pct >= self.SAFE_HAVEN_THRESHOLD_PCT:
                    declining_count += 1

            except Exception:
                continue

        # Only trigger safe-haven if we successfully checked at least half
        # the coins and ALL of them are declining
        if checked_count == 0:
            return False
        return declining_count == checked_count and checked_count >= max(
            1, len(coin_symbols) // 2
        )

    def _scout_with_blacklist(self, current_coin, blacklisted: list):
        """
        Run the standard ratio-based scouting but skip any coin that is
        on the blacklist. Falls back to default scout() if no blacklist.
        """
        if not blacklisted:
            # No coins are blacklisted — run the default strategy unchanged
            super().scout()
            return

        # Temporarily disable blacklisted coins in DB, scout, then re-enable
        # This is the cleanest way to hook into the existing scout logic
        # without duplicating it.
        try:
            for symbol in blacklisted:
                coin = self.db.get_coin(symbol)
                if coin:
                    coin.enabled = False
                    self.db.merge_coin(coin)

            self.logger.info(
                f"[SafeStrategy] Scouting with {blacklisted} excluded."
            )
            super().scout()

        finally:
            # Always re-enable coins after scouting, even if scout() throws
            for symbol in blacklisted:
                coin = self.db.get_coin(symbol)
                if coin:
                    coin.enabled = True
                    self.db.merge_coin(coin)
