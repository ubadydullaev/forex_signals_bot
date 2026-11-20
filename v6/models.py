
from dataclasses import dataclass
from typing import Optional, Literal


SignalSide = Literal["BUY", "SELL", "HOLD"]


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class MarketData:
    symbol: str
    timeframe: str
    candles: list[Candle]


@dataclass
class Signal:
    symbol: str
    timeframe: str
    side: SignalSide
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    risk_reward: Optional[float] = None
    confidence: float = 0.0
    reason: str = ""


@dataclass
class AnalysisResult:
    symbol: str
    timeframe: str
    signal: Signal
    trend: str = "UNKNOWN"
    strength: float = 0.0
    reasons: tuple[str, ...] = ()
  
