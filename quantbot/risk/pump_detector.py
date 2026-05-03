"""Pump / dump / social-pump detection using rolling ``price_history`` (SQLite)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from data.data_store import get_connection
from monitoring import alerts


@dataclass
class PumpSignal:
    type: str  # PUMP | DUMP | SOCIAL_PUMP
    confidence: float
    emergency_buy: bool = False
    emergency_sell: bool = False
    pct_move: float = 0.0
    vol_multiple: float = 0.0
    n_cycles: int = 0
    mentions_change_pct: float = 0.0


class PumpDetector:
    """Thread-safe insert + prune + pump rules over last 20 samples per symbol."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def record_tick(self, symbol: str, price: float, volume: float | None) -> None:
        sym = symbol.strip()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        vol = float(volume) if volume is not None else None
        with self._lock:
            try:
                with get_connection() as conn:
                    conn.execute(
                        "INSERT INTO price_history (ts, symbol, price, volume) VALUES (?, ?, ?, ?)",
                        (ts, sym, float(price), vol),
                    )
                    keep = conn.execute(
                        "SELECT id FROM price_history WHERE symbol = ? ORDER BY id DESC LIMIT 30",
                        (sym,),
                    ).fetchall()
                    if keep:
                        ids = tuple(int(r[0]) for r in keep)
                        qmarks = ",".join("?" * len(ids))
                        conn.execute(
                            f"DELETE FROM price_history WHERE symbol = ? AND id NOT IN ({qmarks})",
                            (sym, *ids),
                        )
            except Exception as exc:
                logger.debug("[pump_detector] record_tick failed {}: {}", sym, exc)

    def _rows_for_symbol(self, symbol: str, limit: int = 20) -> list[tuple[float, float | None]]:
        sym = symbol.strip()
        with get_connection() as conn:
            cur = conn.execute(
                """
                SELECT price, volume FROM price_history
                WHERE symbol = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (sym, limit),
            )
            rows = [(float(r[0]), float(r[1]) if r[1] is not None else None) for r in cur.fetchall()]
        rows.reverse()
        return rows

    def _social_breakout(self, symbol: str) -> tuple[bool, float]:
        try:
            from social.reddit_scanner import get_cached_signals

            base = symbol.strip().upper().split("/")[0].replace("-", "")
            for m in get_cached_signals():
                if m.ticker.replace("-", "") == base and m.is_breakout:
                    return True, float(m.mentions_change_pct)
        except Exception:
            return False, 0.0
        return False, 0.0

    def check_for_pump(
        self,
        symbol: str,
        current_price: float,
        current_volume: float,
    ) -> PumpSignal | None:
        sym = symbol.strip()
        rows = self._rows_for_symbol(sym, 20)
        if len(rows) < 2:
            return None
        prices = [p for p, _ in rows]
        vols = [v for _, v in rows if v is not None and v > 0]
        cur_p = float(current_price)
        cur_v = float(current_volume) if current_volume and current_volume > 0 else 0.0
        min_p = min(prices)
        max_p = max(prices)
        avg_v = sum(vols) / len(vols) if vols else 0.0
        if avg_v <= 0.0:
            avg_v = 1e-9
        vol_mult = cur_v / avg_v if cur_v > 0 else 0.0

        n = len(prices)
        pct_from_low = (cur_p - min_p) / min_p if min_p > 0 else 0.0
        pct_from_high = (max_p - cur_p) / max_p if max_p > 0 else 0.0

        if pct_from_high > 0.15 and vol_mult > 3.0:
            sig = PumpSignal(
                type="DUMP",
                confidence=0.8,
                emergency_sell=True,
                pct_move=pct_from_high * 100.0,
                vol_multiple=vol_mult,
                n_cycles=n,
            )
            self._telegram_pump(sym, sig, mentions_change_pct=0.0)
            logger.info(
                "DUMP signal | {} | -{:.1f}% vs 20c high | vol {:.1f}x avg",
                sym,
                sig.pct_move,
                vol_mult,
            )
            return sig

        if pct_from_low > 0.15 and vol_mult > 3.0:
            sig = PumpSignal(
                type="PUMP",
                confidence=0.8,
                pct_move=pct_from_low * 100.0,
                vol_multiple=vol_mult,
                n_cycles=n,
            )
            self._telegram_pump(sym, sig, mentions_change_pct=0.0)
            logger.info(
                "PUMP signal | {} | +{:.1f}% vs 20c low | vol {:.1f}x avg",
                sym,
                sig.pct_move,
                vol_mult,
            )
            return sig

        brk, mchg = self._social_breakout(sym)
        if brk and len(prices) >= 4:
            p_old = prices[-4]
            if p_old > 0 and (cur_p - p_old) / p_old > 0.05:
                sig = PumpSignal(
                    type="SOCIAL_PUMP",
                    confidence=0.6,
                    emergency_buy=True,
                    pct_move=(cur_p - p_old) / p_old * 100.0,
                    vol_multiple=vol_mult,
                    n_cycles=min(3, n),
                    mentions_change_pct=mchg,
                )
                self._telegram_pump(sym, sig, mentions_change_pct=mchg)
                logger.info(
                    "SOCIAL_PUMP candidate | {} | +{:.1f}% / 3c | mentions_change {:.0f}%",
                    sym,
                    sig.pct_move,
                    mchg,
                )
                return sig

        return None

    def _telegram_pump(self, symbol: str, sig: PumpSignal, *, mentions_change_pct: float) -> None:
        if not alerts.telegram_alerts_configured():
            return
        try:
            if sig.type == "SOCIAL_PUMP":
                msg = (
                    f"PUMP DETECTED (social): {symbol} +{sig.pct_move:.1f}% in {sig.n_cycles} cycles | "
                    f"Volume {sig.vol_multiple:.1f}x avg | Reddit mentions +{mentions_change_pct:.0f}%"
                )
            else:
                msg = (
                    f"PUMP DETECTED: {symbol} {sig.type} | move {sig.pct_move:.1f}% in {sig.n_cycles} cycles | "
                    f"Volume {sig.vol_multiple:.1f}x avg"
                )
            alerts.send_telegram(msg)
        except Exception as exc:
            logger.debug("[pump_detector] telegram failed: {}", exc)
