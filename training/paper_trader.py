"""Paper trading: dual fake wallets, mid-price fills, optional SQLite audit (brief §7)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from loguru import logger

import config
from data.data_store import get_connection
from monitoring import trade_logger

AssetClass = Literal["stock", "crypto"]


@dataclass
class Position:
    asset_class: AssetClass
    symbol: str
    quantity: float
    avg_price: float


@dataclass
class FillResult:
    ok: bool
    side: Literal["buy", "sell"]
    asset_class: AssetClass
    symbol: str
    quantity: float
    price: float
    notional: float
    message: str
    broker_order_id: str | None = None


def _position_key(asset_class: AssetClass, symbol: str) -> tuple[AssetClass, str]:
    from utils.symbols import position_key_symbol

    return asset_class, position_key_symbol(asset_class, symbol)


class PaperTrader:
    """
    Simulated broker: separate USD pools for stocks vs crypto, positions marked at avg entry.
    Fills always at the provided mid price (no slippage). Optionally persists trades + portfolio_state.
    """

    def __init__(
        self,
        stocks_cash: float | None = None,
        crypto_cash: float | None = None,
        *,
        persist_sqlite: bool = True,
        db_path: Path | str | None = None,
        mode: str | None = None,
        telegram_on_fills: bool = True,
    ) -> None:
        self.cash_stocks = float(
            stocks_cash if stocks_cash is not None else config.PAPER_STOCKS_STARTING_CASH
        )
        self.cash_crypto = float(
            crypto_cash if crypto_cash is not None else config.PAPER_CRYPTO_STARTING_CASH
        )
        self._positions: dict[tuple[AssetClass, str], Position] = {}
        self._current_prices: dict[str, float] = {}
        if persist_sqlite:
            self._db_path: Path | str | None = Path(db_path) if db_path is not None else config.DB_PATH
        else:
            self._db_path = None
        self.mode = (mode or config.MODE or "paper").strip().lower()
        if self.mode not in ("paper", "live"):
            self.mode = "paper"
        self._telegram_on_fills = bool(telegram_on_fills)

    def set_telegram_on_fills(self, value: bool) -> None:
        """When False, filled orders skip Telegram (e.g. worker sends custom Sprint 9 messages)."""
        self._telegram_on_fills = bool(value)

    @property
    def persistence_path(self) -> Path | None:
        """SQLite file used for audit rows, or None if persistence is disabled."""
        return Path(self._db_path) if self._db_path is not None else None

    def _cash_for(self, asset_class: AssetClass) -> float:
        return self.cash_stocks if asset_class == "stock" else self.cash_crypto

    def _set_cash(self, asset_class: AssetClass, value: float) -> None:
        if asset_class == "stock":
            self.cash_stocks = value
        else:
            self.cash_crypto = value

    def position(self, asset_class: AssetClass, symbol: str) -> Position | None:
        return self._positions.get(_position_key(asset_class, symbol))

    @property
    def positions(self) -> dict[tuple[AssetClass, str], Position]:
        return self._positions

    def mark_to_market(self, prices: dict[str, float]) -> None:
        out: dict[str, float] = {}
        for sym, px in (prices or {}).items():
            try:
                pxf = float(px)
            except (TypeError, ValueError):
                continue
            if pxf > 0:
                out[str(sym).strip()] = pxf
        self._current_prices = out

    def positions_market_value(self) -> tuple[float, float]:
        """Signed book contribution per sleeve (``qty * avg``; shorts are negative)."""
        s_val = 0.0
        c_val = 0.0
        for pos in self._positions.values():
            v = pos.quantity * pos.avg_price
            if pos.asset_class == "stock":
                s_val += v
            else:
                c_val += v
        return s_val, c_val

    def positions_gross_notional(self) -> tuple[float, float]:
        """Gross notional at avg entry (``abs(qty) * avg``) for deployed / cap checks."""
        s_val = 0.0
        c_val = 0.0
        for pos in self._positions.values():
            v = abs(pos.quantity) * pos.avg_price
            if pos.asset_class == "stock":
                s_val += v
            else:
                c_val += v
        return s_val, c_val

    def equity_stocks(self) -> float:
        mv, _ = self.positions_market_value()
        return self.cash_stocks + mv

    def equity_crypto(self) -> float:
        _, mv = self.positions_market_value()
        return self.cash_crypto + mv

    def equity_total(self) -> float:
        cash = self.cash_stocks + self.cash_crypto
        market_value = 0.0
        for pos in self._positions.values():
            px = self._current_prices.get(pos.symbol, pos.avg_price)
            market_value += pos.quantity * px
        return cash + market_value

    def deployed_pct(self) -> float:
        eq = self.equity_total()
        if eq <= 0.0:
            return 0.0
        s_mv, c_mv = self.positions_gross_notional()
        deployed = s_mv + c_mv
        return (deployed / eq) * 100.0

    def _persist(self, fn: Any) -> None:
        if self._db_path is None:
            return
        try:
            with get_connection(self._db_path) as conn:
                fn(conn)
        except Exception:
            logger.exception("PaperTrader SQLite persistence failed")

    def _log_trade_row(
        self,
        *,
        asset_class: AssetClass,
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: float,
        price: float,
        notional: float,
        status: str,
        broker_order_id: str | None,
        reason_code: str | None,
        meta: dict[str, Any] | None,
    ) -> None:
        def _w(conn: Any) -> None:
            trade_logger.log_trade(
                conn,
                mode=self.mode,
                asset_class=asset_class,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                notional=notional,
                status=status,
                broker_order_id=broker_order_id,
                reason_code=reason_code,
                meta=meta,
            )
            s_mv, c_mv = self.positions_market_value()
            g_s, g_c = self.positions_gross_notional()
            eq_s = self.cash_stocks + s_mv
            eq_c = self.cash_crypto + c_mv
            eq_t = eq_s + eq_c
            deployed = g_s + g_c
            dep_pct = (deployed / eq_t * 100.0) if eq_t > 0.0 else 0.0
            trade_logger.log_portfolio_snapshot(
                conn,
                mode=self.mode,
                cash_stocks=self.cash_stocks,
                cash_crypto=self.cash_crypto,
                equity_stocks=eq_s,
                equity_crypto=eq_c,
                equity_total=eq_t,
                deployed_pct=dep_pct,
                kill_switch_active=False,
                meta={"source": "paper_trader"},
            )
            if self._telegram_on_fills:
                self._telegram_after_trade(
                    asset_class=asset_class,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    status=status,
                    reason_code=reason_code,
                )

        self._persist(_w)
        try:
            from risk import drawdown_guard

            drawdown_guard.notify_kill_switch_if_tripped(self.equity_total())
        except Exception:
            logger.exception("Kill-switch notify after trade failed")

    def _telegram_after_trade(
        self,
        *,
        asset_class: AssetClass,
        symbol: str,
        side: Literal["buy", "sell"],
        quantity: float,
        price: float,
        status: str,
        reason_code: str | None,
    ) -> None:
        from monitoring import alerts

        if not alerts.telegram_alerts_configured():
            return
        rc_u = (reason_code or "").upper()
        stop_hit = "STOP_LOSS" in rc_u or rc_u == "STOPLOSS"
        if status == "filled":
            alerts.notify_trade_executed(
                side=side,
                asset_class=asset_class,
                symbol=symbol,
                quantity=quantity,
                price=price,
                status=status,
                reason_code=reason_code,
            )
            if stop_hit:
                alerts.notify_stop_loss_hit(
                    symbol,
                    asset_class,
                    detail=f"{side.upper()} {quantity} @ {price}",
                )

    def market_buy(
        self,
        asset_class: AssetClass,
        symbol: str,
        quantity: float,
        mid_price: float,
        *,
        reason_code: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> FillResult:
        if quantity <= 0 or mid_price <= 0:
            return FillResult(
                False, "buy", asset_class, symbol, 0.0, mid_price, 0.0, "quantity and mid_price must be positive"
            )
        key = _position_key(asset_class, symbol)
        existing = self._positions.get(key)
        cash = self._cash_for(asset_class)

        # Cover short (full or partial); remainder opens/adds long when flat after cover.
        if existing is not None and existing.quantity < -1e-12:
            abs_s = abs(existing.quantity)
            cover = min(quantity, abs_s)
            rem = quantity - cover
            need = cover * mid_price + (rem * mid_price if rem > 1e-12 else 0.0)
            if cash + 1e-9 < need:
                msg = f"insufficient {asset_class} cash: need {need:.2f}, have {cash:.2f}"
                self._log_trade_row(
                    asset_class=asset_class,
                    symbol=symbol.strip(),
                    side="buy",
                    quantity=quantity,
                    price=mid_price,
                    notional=need,
                    status="rejected",
                    broker_order_id=None,
                    reason_code="INSUFFICIENT_CASH",
                    meta={"message": msg},
                )
                return FillResult(False, "buy", asset_class, symbol, 0.0, mid_price, 0.0, msg)

            new_q = existing.quantity + cover
            cover_notional = cover * mid_price
            self._set_cash(asset_class, cash - cover_notional)
            if abs(new_q) < 1e-12:
                self._positions.pop(key, None)
            else:
                self._positions[key] = Position(
                    asset_class, symbol.strip(), new_q, existing.avg_price
                )
            oid1 = f"paper-{uuid.uuid4().hex[:16]}"
            self._log_trade_row(
                asset_class=asset_class,
                symbol=symbol.strip(),
                side="buy",
                quantity=cover,
                price=mid_price,
                notional=cover_notional,
                status="filled",
                broker_order_id=oid1,
                reason_code=reason_code,
                meta=meta,
            )
            if rem <= 1e-12:
                return FillResult(True, "buy", asset_class, symbol, quantity, mid_price, need, "filled", oid1)

            cash2 = self._cash_for(asset_class)
            rem_notional = rem * mid_price
            ex2 = self._positions.get(key)
            if ex2 is None:
                self._positions[key] = Position(asset_class, symbol.strip(), rem, mid_price)
            else:
                qn = ex2.quantity + rem
                avgn = (ex2.quantity * ex2.avg_price + rem * mid_price) / qn
                self._positions[key] = Position(asset_class, symbol.strip(), qn, avgn)
            self._set_cash(asset_class, cash2 - rem_notional)
            oid2 = f"paper-{uuid.uuid4().hex[:16]}"
            self._log_trade_row(
                asset_class=asset_class,
                symbol=symbol.strip(),
                side="buy",
                quantity=rem,
                price=mid_price,
                notional=rem_notional,
                status="filled",
                broker_order_id=oid2,
                reason_code=reason_code,
                meta=meta,
            )
            return FillResult(True, "buy", asset_class, symbol, quantity, mid_price, need, "filled", oid1)

        notional = quantity * mid_price
        if cash + 1e-9 < notional:
            msg = f"insufficient {asset_class} cash: need {notional:.2f}, have {cash:.2f}"
            self._log_trade_row(
                asset_class=asset_class,
                symbol=symbol.strip(),
                side="buy",
                quantity=quantity,
                price=mid_price,
                notional=notional,
                status="rejected",
                broker_order_id=None,
                reason_code="INSUFFICIENT_CASH",
                meta={"message": msg},
            )
            return FillResult(False, "buy", asset_class, symbol, 0.0, mid_price, 0.0, msg)

        if existing is None or abs(existing.quantity) < 1e-12:
            self._positions[key] = Position(asset_class, symbol.strip(), quantity, mid_price)
        else:
            q = existing.quantity + quantity
            avg = (existing.quantity * existing.avg_price + quantity * mid_price) / q
            self._positions[key] = Position(asset_class, symbol.strip(), q, avg)

        self._set_cash(asset_class, cash - notional)
        oid = f"paper-{uuid.uuid4().hex[:16]}"
        self._log_trade_row(
            asset_class=asset_class,
            symbol=symbol.strip(),
            side="buy",
            quantity=quantity,
            price=mid_price,
            notional=notional,
            status="filled",
            broker_order_id=oid,
            reason_code=reason_code,
            meta=meta,
        )
        return FillResult(True, "buy", asset_class, symbol, quantity, mid_price, notional, "filled", oid)

    def market_sell(
        self,
        asset_class: AssetClass,
        symbol: str,
        quantity: float,
        mid_price: float,
        *,
        reason_code: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> FillResult:
        if quantity <= 0 or mid_price <= 0:
            return FillResult(
                False, "sell", asset_class, symbol, 0.0, mid_price, 0.0, "quantity and mid_price must be positive"
            )
        key = _position_key(asset_class, symbol)
        existing = self._positions.get(key)
        notional = quantity * mid_price
        cash = self._cash_for(asset_class)

        if existing is not None and existing.quantity > 1e-12:
            if existing.quantity + 1e-12 < quantity:
                msg = "insufficient position"
                self._log_trade_row(
                    asset_class=asset_class,
                    symbol=symbol.strip(),
                    side="sell",
                    quantity=quantity,
                    price=mid_price,
                    notional=notional,
                    status="rejected",
                    broker_order_id=None,
                    reason_code="INSUFFICIENT_POSITION",
                    meta={"message": msg},
                )
                return FillResult(False, "sell", asset_class, symbol, 0.0, mid_price, 0.0, msg)
            new_q = existing.quantity - quantity
            if new_q <= 1e-12:
                self._positions.pop(key, None)
            else:
                self._positions[key] = Position(asset_class, symbol.strip(), new_q, existing.avg_price)
            self._set_cash(asset_class, cash + notional)
            oid = f"paper-{uuid.uuid4().hex[:16]}"
            self._log_trade_row(
                asset_class=asset_class,
                symbol=symbol.strip(),
                side="sell",
                quantity=quantity,
                price=mid_price,
                notional=notional,
                status="filled",
                broker_order_id=oid,
                reason_code=reason_code,
                meta=meta,
            )
            return FillResult(True, "sell", asset_class, symbol, quantity, mid_price, notional, "filled", oid)

        if existing is not None and existing.quantity < -1e-12:
            new_q = existing.quantity - quantity
            abs_old = abs(existing.quantity)
            abs_new = abs(new_q)
            new_avg = (abs_old * existing.avg_price + quantity * mid_price) / abs_new
            self._positions[key] = Position(asset_class, symbol.strip(), new_q, new_avg)
            self._set_cash(asset_class, cash + notional)
            oid = f"paper-{uuid.uuid4().hex[:16]}"
            self._log_trade_row(
                asset_class=asset_class,
                symbol=symbol.strip(),
                side="sell",
                quantity=quantity,
                price=mid_price,
                notional=notional,
                status="filled",
                broker_order_id=oid,
                reason_code=reason_code,
                meta=meta,
            )
            return FillResult(True, "sell", asset_class, symbol, quantity, mid_price, notional, "filled", oid)

        if asset_class == "stock":
            self._positions[key] = Position(asset_class, symbol.strip(), -quantity, mid_price)
            self._set_cash(asset_class, cash + notional)
            oid = f"paper-{uuid.uuid4().hex[:16]}"
            self._log_trade_row(
                asset_class=asset_class,
                symbol=symbol.strip(),
                side="sell",
                quantity=quantity,
                price=mid_price,
                notional=notional,
                status="filled",
                broker_order_id=oid,
                reason_code=reason_code,
                meta=meta,
            )
            return FillResult(True, "sell", asset_class, symbol, quantity, mid_price, notional, "filled", oid)

        msg = "insufficient position"
        self._log_trade_row(
            asset_class=asset_class,
            symbol=symbol.strip(),
            side="sell",
            quantity=quantity,
            price=mid_price,
            notional=notional,
            status="rejected",
            broker_order_id=None,
            reason_code="INSUFFICIENT_POSITION",
            meta={"message": msg},
        )
        return FillResult(False, "sell", asset_class, symbol, 0.0, mid_price, 0.0, msg)

    def log_signal_row(
        self,
        *,
        symbol: str,
        signal_name: str,
        raw_value: float | None,
        direction: int,
        weight: float | None = None,
        combined_score: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if self._db_path is None:
            return

        def _w(conn: Any) -> None:
            trade_logger.log_signal(
                conn,
                mode=self.mode,
                symbol=symbol,
                signal_name=signal_name,
                raw_value=raw_value,
                direction=direction,
                weight=weight,
                combined_score=combined_score,
                meta=meta,
            )

        self._persist(_w)


def create_paper_trader(
    *,
    persist_sqlite: bool = True,
    db_path: Path | str | None = None,
    telegram_on_fills: bool = True,
) -> PaperTrader:
    """Factory: by default persists to config.DB_PATH; set persist_sqlite=False for in-memory tests."""
    return PaperTrader(
        persist_sqlite=persist_sqlite,
        db_path=db_path,
        telegram_on_fills=telegram_on_fills,
    )
