import asyncio
import json
import logging
import math
import os
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

import ccxt
try:
    import ccxt.pro as ccxtpro
except ImportError:  # ccxt[pro] yoksa REST fallback
    ccxtpro = None  # type: ignore


def decimal_precision_decimal(value):
    d = Decimal(str(value)).normalize()
    return -d.as_tuple().exponent


def get_client_id():
    header = "x-GRBCT6GB"
    guid = uuid.uuid4().hex  # 32-character hex string
    combined = header + guid
    return combined[:32]  # Extract only the first 32 characters

# ============================================================================
# EXTERNAL CONFIG FILE HANDLER
# ============================================================================

# Varsayılan: bakiyeyi 8'e böl, 8 likit USDT pair'de al/sat
DEFAULT_SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "BNB/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "DOGE/USDT",
    "ADA/USDT",
    "AVAX/USDT",
]
DEFAULT_BALANCE_SLOTS = 8


class ConfigFile:
    """Handler for external configuration file"""
    
    @staticmethod
    def create_default_config(filename: str = "market_maker_config.json"):
        """Create a default configuration file"""
        default_config = {
            "exchange": {
                "api_key": "your_binance_api_key_here",
                "api_secret": "your_binance_api_secret_here",
                "testnet": False,  # GERÇEK Binance spot
                "symbol": "BTC/USDT",
                "symbols": DEFAULT_SYMBOLS,
                "base_asset": "BTC",
                "quote_asset": "USDT"
            },
            "trading": {
                "total_capital": 0.0,
                "balance_slots": DEFAULT_BALANCE_SLOTS,
                "split_live_balance": True,
                "max_inventory_ratio": 0.9,
                "max_drawdown_ratio": 0.05,
                "base_spread_ticks": 3.0,
                "volatility_multiplier": 4.0,
                "inventory_skew_strength": 3.0,
                "min_order_lifetime": 10.0,
                "min_base_balance": 0.0001,
                "min_quote_balance": 5.0,
                "allow_short_selling": False
            },
            "risk": {
                "risk_aversion": 0.1,
                "market_impact": 0.01,
                "time_horizon": 1.0,
                "use_avellaneda": True
            }
        }
        
        with open(filename, 'w') as f:
            json.dump(default_config, f, indent=4)
        
        return filename
    
    @staticmethod
    def load_config(filename: str = "market_maker_config.json"):
        """Load configuration from file"""
        if not os.path.exists(filename):
            print(f"Config file {filename} not found. Creating default config...")
            ConfigFile.create_default_config(filename)
            print(f"Please edit {filename} with your API keys and settings.")
            return None
        
        try:
            with open(filename, 'r') as f:
                config_data = json.load(f)
            return config_data
        except Exception as e:
            print(f"Error loading config file: {e}")
            return None

# ============================================================================
# CONFIGURATION SYSTEM
# ============================================================================

@dataclass
class Config:
    # API Credentials - Now loaded from external file
    API_KEY: str = ""
    API_SECRET: str = ""
    USE_TESTNET: bool = False  # varsayılan: GERÇEK Binance
    
    # Exchange Settings
    SYMBOL: str = "BTC/USDT"
    SYMBOLS: List[str] = None  # type: ignore
    BASE_ASSET: str = "BTC"
    QUOTE_ASSET: str = "USDT"
    TICK_SIZE: float = 0.01
    MIN_NOTIONAL: float = 5.0
    MIN_QTY: float = 0.00001
    LOT_SIZE: float = 0.00001
    
    # Capital & Risk Management
    TOTAL_CAPITAL: float = 0.0  # 0 = canlı bakiyeden hesapla
    BALANCE_SLOTS: int = DEFAULT_BALANCE_SLOTS
    SPLIT_LIVE_BALANCE: bool = True
    MAX_INVENTORY_RATIO: float = 0.9
    MAX_DRAWDOWN_RATIO: float = 0.05
    LIQUIDATION_BUFFER: float = 0.2
    
    # Strategy Parameters
    BASE_SPREAD_TICKS: float = 3.0
    VOLATILITY_MULTIPLIER: float = 4.0
    INVENTORY_SKEW_STRENGTH: float = 3.0
    IMBALANCE_SKEW_STRENGTH: float = 1.0
    
    # Avellaneda-Stoikov Parameters
    RISK_AVERSION: float = 0.1
    MARKET_IMPACT: float = 0.01
    TIME_HORIZON: float = 1.0
    USE_AVELLANEDA: bool = True
    
    # Order Management
    MIN_ORDER_LIFETIME: float = 10.0
    MAX_ORDER_REPLACE_FREQ: float = 10.0
    QUEUE_AHEAD: bool = False
    
    # Position Management
    MIN_BASE_BALANCE: float = 0.0001
    MIN_QUOTE_BALANCE: float = 5.0
    ALLOW_SHORT_SELLING: bool = False
    
    # WebSocket & API
    WS_RECONNECT_DELAY: float = 5.0
    API_TIMEOUT: float = 10.0
    RATE_LIMIT_BUFFER: float = 0.1
    
    # Performance Monitoring
    PRICE_HISTORY_SIZE: int = 1000
    TRADE_HISTORY_SIZE: int = 100
    VOLATILITY_WINDOW: float = 60.0
    
    # Fee tracking (will be populated from API)
    MAKER_FEE_RATE: float = 0.001
    TAKER_FEE_RATE: float = 0.001
    FEE_CURRENCY: str = ""

    def __post_init__(self):
        if self.SYMBOLS is None:
            self.SYMBOLS = list(DEFAULT_SYMBOLS)

    @classmethod
    def from_file(cls, filename: str = "market_maker_config.json"):
        """Create Config from external file"""
        config_data = ConfigFile.load_config(filename)
        if not config_data:
            return None
        
        config = cls()
        
        # Load exchange settings
        exchange = config_data.get("exchange", {})
        config.API_KEY = (
            exchange.get("api_key", "")
            or os.getenv("BINANCE_API_KEY", "")
        ).strip()
        config.API_SECRET = (
            exchange.get("api_secret", "")
            or os.getenv("BINANCE_API_SECRET", "")
        ).strip()
        config.USE_TESTNET = exchange.get("testnet", False)
        config.SYMBOL = exchange.get("symbol", "BTC/USDT")
        symbols = exchange.get("symbols")
        if isinstance(symbols, list) and symbols:
            config.SYMBOLS = [str(s).strip() for s in symbols if str(s).strip()]
        else:
            config.SYMBOLS = [config.SYMBOL]
        config.BASE_ASSET = exchange.get("base_asset", "BTC")
        config.QUOTE_ASSET = exchange.get("quote_asset", "USDT")
        
        # Load trading settings
        trading = config_data.get("trading", {})
        config.TOTAL_CAPITAL = float(trading.get("total_capital", 0.0) or 0.0)
        config.BALANCE_SLOTS = int(trading.get("balance_slots", DEFAULT_BALANCE_SLOTS) or DEFAULT_BALANCE_SLOTS)
        config.SPLIT_LIVE_BALANCE = bool(trading.get("split_live_balance", True))
        config.MAX_INVENTORY_RATIO = trading.get("max_inventory_ratio", 0.9)
        config.MAX_DRAWDOWN_RATIO = trading.get("max_drawdown_ratio", 0.05)
        config.BASE_SPREAD_TICKS = trading.get("base_spread_ticks", 3.0)
        config.VOLATILITY_MULTIPLIER = trading.get("volatility_multiplier", 4.0)
        config.INVENTORY_SKEW_STRENGTH = trading.get("inventory_skew_strength", 3.0)
        config.MIN_ORDER_LIFETIME = trading.get("min_order_lifetime", 10.0)
        config.MAX_ORDER_REPLACE_FREQ = trading.get("max_order_replace_freq", 10.0)
        config.MIN_BASE_BALANCE = trading.get("min_base_balance", 0.0001)
        config.MIN_QUOTE_BALANCE = trading.get("min_quote_balance", 5.0)
        config.ALLOW_SHORT_SELLING = trading.get("allow_short_selling", False)
        
        # Load risk settings
        risk = config_data.get("risk", {})
        config.RISK_AVERSION = risk.get("risk_aversion", 0.1)
        config.MARKET_IMPACT = risk.get("market_impact", 0.01)
        config.TIME_HORIZON = risk.get("time_horizon", 1.0)
        config.USE_AVELLANEDA = risk.get("use_avellaneda", True)
        
        return config

    def for_symbol(self, symbol: str, slot_capital: float) -> "Config":
        """Clone config for one slot/symbol with allocated USDT."""
        base = symbol.split("/")[0] if "/" in symbol else symbol.replace("USDT", "")
        quote = symbol.split("/")[1] if "/" in symbol else self.QUOTE_ASSET
        return Config(
            API_KEY=self.API_KEY,
            API_SECRET=self.API_SECRET,
            USE_TESTNET=self.USE_TESTNET,
            SYMBOL=symbol,
            SYMBOLS=[symbol],
            BASE_ASSET=base,
            QUOTE_ASSET=quote,
            TICK_SIZE=self.TICK_SIZE,
            MIN_NOTIONAL=self.MIN_NOTIONAL,
            MIN_QTY=self.MIN_QTY,
            LOT_SIZE=self.LOT_SIZE,
            TOTAL_CAPITAL=float(slot_capital),
            BALANCE_SLOTS=1,
            SPLIT_LIVE_BALANCE=False,
            MAX_INVENTORY_RATIO=self.MAX_INVENTORY_RATIO,
            MAX_DRAWDOWN_RATIO=self.MAX_DRAWDOWN_RATIO,
            LIQUIDATION_BUFFER=self.LIQUIDATION_BUFFER,
            BASE_SPREAD_TICKS=self.BASE_SPREAD_TICKS,
            VOLATILITY_MULTIPLIER=self.VOLATILITY_MULTIPLIER,
            INVENTORY_SKEW_STRENGTH=self.INVENTORY_SKEW_STRENGTH,
            IMBALANCE_SKEW_STRENGTH=self.IMBALANCE_SKEW_STRENGTH,
            RISK_AVERSION=self.RISK_AVERSION,
            MARKET_IMPACT=self.MARKET_IMPACT,
            TIME_HORIZON=self.TIME_HORIZON,
            USE_AVELLANEDA=self.USE_AVELLANEDA,
            MIN_ORDER_LIFETIME=self.MIN_ORDER_LIFETIME,
            MAX_ORDER_REPLACE_FREQ=self.MAX_ORDER_REPLACE_FREQ,
            QUEUE_AHEAD=self.QUEUE_AHEAD,
            MIN_BASE_BALANCE=self.MIN_BASE_BALANCE,
            MIN_QUOTE_BALANCE=self.MIN_QUOTE_BALANCE,
            ALLOW_SHORT_SELLING=self.ALLOW_SHORT_SELLING,
            WS_RECONNECT_DELAY=self.WS_RECONNECT_DELAY,
            API_TIMEOUT=self.API_TIMEOUT,
            RATE_LIMIT_BUFFER=self.RATE_LIMIT_BUFFER,
            PRICE_HISTORY_SIZE=self.PRICE_HISTORY_SIZE,
            TRADE_HISTORY_SIZE=self.TRADE_HISTORY_SIZE,
            VOLATILITY_WINDOW=self.VOLATILITY_WINDOW,
            MAKER_FEE_RATE=self.MAKER_FEE_RATE,
            TAKER_FEE_RATE=self.TAKER_FEE_RATE,
            FEE_CURRENCY=self.FEE_CURRENCY,
        )
    
    @property
    def max_inventory_usd(self) -> float:
        return self.TOTAL_CAPITAL * self.MAX_INVENTORY_RATIO
    
    @property
    def max_drawdown_usd(self) -> float:
        return self.TOTAL_CAPITAL * self.MAX_DRAWDOWN_RATIO

# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class MarketState:
    symbol: str
    bid: float = 0.0
    ask: float = 0.0
    mid_price: float = 0.0
    microprice: float = 0.0
    volatility: float = 0.0
    imbalance: float = 0.0
    regime: str = "range"
    last_update: float = 0.0
    
    @property
    def spread(self) -> float:
        return self.ask - self.bid if self.bid > 0 and self.ask > 0 else 0.0

@dataclass
class Quotes:
    bid_price: float
    ask_price: float
    bid_size: float
    ask_size: float
    timestamp: float
    can_place_bid: bool = True
    can_place_ask: bool = True
    
    def __post_init__(self):
        if self.timestamp == 0:
            self.timestamp = time.time()

@dataclass
class Position:
    symbol: str
    quantity: float = 0.0
    avg_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    
    @property
    def notional_value(self) -> float:
        return abs(self.quantity) * self.avg_price
    
    def update_unrealized_pnl(self, current_price: float):
        if self.quantity != 0:
            self.unrealized_pnl = self.quantity * (current_price - self.avg_price)

@dataclass
class Balance:
    """Balance tracking structure"""
    base_free: float = 0.0
    base_total: float = 0.0
    quote_free: float = 0.0
    quote_total: float = 0.0
    last_update: float = 0.0

@dataclass
class Trade:
    """Trade record for P&L tracking"""
    id: str
    symbol: str
    side: str
    amount: float
    price: float
    timestamp: float
    fee: float = 0.0
    fee_currency: str = ""

class EMA:
    def __init__(self, alpha: float):
        self.alpha = alpha
        self.value: Optional[float] = None
    
    def update(self, new_value: float) -> float:
        if self.value is None:
            self.value = new_value
        else:
            self.value = self.alpha * new_value + (1 - self.alpha) * self.value
        return self.value

# ============================================================================
# CCXT BINANCE CLIENT - FIXED VERSION
# ============================================================================

class BinanceCCXTClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        
        # Initialize CCXT exchange (sandbox=False → gerçek Binance spot)
        exchange_class = ccxt.binance
        self.exchange = exchange_class({
            'apiKey': api_key,
            'secret': api_secret,
            'sandbox': testnet,
            'enableRateLimit': True,
            'rateLimit': 100,
            'options': {
                'defaultType': 'spot',
            }
        })
        
        # Initialize CCXT Pro for WebSocket
        self.exchange_ws = None
        if ccxtpro is not None:
            try:
                self.exchange_ws = ccxtpro.binance({
                    'apiKey': api_key,
                    'secret': api_secret,
                    'sandbox': testnet,
                    'enableRateLimit': True,
                    'options': {
                        'defaultType': 'spot',
                    }
                })
            except Exception as e:
                logging.warning(f"CCXT Pro not available: {e}. WebSocket features will be limited.")
                self.exchange_ws = None
        else:
            logging.warning("ccxt.pro yok — REST ile devam (pip install 'ccxt[pro]' opsiyonel)")
        
        self.logger = logging.getLogger(__name__)
        self.markets_loaded = False
        
    async def initialize(self):
        """Initialize exchange connection and load markets"""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.exchange.load_markets)
            self.markets_loaded = True
            await loop.run_in_executor(None, self.exchange.fetch_status)
            
            self.logger.info(f"Connected to {'Testnet' if self.testnet else 'Live'} Binance")
            self.logger.info(f"Loaded {len(self.exchange.markets)} markets")
            return True
        except Exception as e:
            self.logger.error(f"Failed to initialize exchange: {e}")
            return False
    
    async def get_account_balance(self):
        """Get account balance"""
        try:
            loop = asyncio.get_event_loop()
            balance = await loop.run_in_executor(None, self.exchange.fetch_balance)
            return balance
        except Exception as e:
            self.logger.error(f"Failed to fetch balance: {e}")
            return None
    
    async def get_order_book(self, symbol: str, limit: int = 20):
        """Get order book"""
        try:
            loop = asyncio.get_event_loop()
            order_book = await loop.run_in_executor(None, self.exchange.fetch_order_book, symbol, limit)
            return order_book
        except Exception as e:
            self.logger.error(f"Failed to fetch order book for {symbol}: {e}")
            return None
    
    async def place_order(self, symbol: str, side: str, amount: float, price: float = None, order_type: str = 'limit'):
        """Place an order with proper validation - FIXED for zero amounts"""
        try:
            # Ensure inputs are floats
            amount = float(amount)
            if price is not None:
                price = float(price)
            
            # Check for zero or invalid amounts BEFORE validation
            if amount <= 0:
                self.logger.error(f"Cannot place order: amount is {amount}")
                return None
            
            if order_type == 'limit' and (price is None or price <= 0):
                self.logger.error(f"Cannot place limit order: invalid price {price}")
                return None
            
            # Get market info for validation
            market = self.get_market_info(symbol)
            if not market:
                raise ValueError(f"Market info not available for {symbol}")
            
            # Log original values
            self.logger.debug(f"Original order: {side} {amount:.8f} @ {price:.4f}")
            
            # Validate order parameters
            amount = self._validate_amount(amount, market)
            if price:
                price = self._validate_price(price, market)
            
            # Check AGAIN after validation for zero amounts
            if amount <= 0:
                self.logger.error(f"Original order: {side} {amount:.8f} @ {price:.4f}")
                self.logger.error(f"Invalid amount after validation: {amount}")
                return None
            
            if price and price <= 0:
                self.logger.error(f"Original order: {side} {amount:.8f} @ {price:.4f}")
                self.logger.error(f"Invalid price after validation: {price}")
                return None
            
            # Check minimum notional
            if price and amount:
                notional = float(amount) * float(price)
                min_notional = float(market.get('limits', {}).get('cost', {}).get('min', 5.0))
                
                if notional < min_notional:
                    self.logger.warning(f"Order notional ${notional:.2f} below minimum ${min_notional:.2f}")
                    # Adjust amount to meet minimum notional
                    new_amount = (min_notional * 1.1) / price
                    amount = self._validate_amount(new_amount, market)
                    notional = amount * price
                    self.logger.info(f"Adjusted order size to {amount:.8f} (notional: ${notional:.2f})")
            
            # Final validation
            if amount <= 0:
                self.logger.error(f"Final amount is invalid: {amount}")
                return None
            
            self.logger.debug(f"Final order: {side} {amount:.8f} @ {price:.4f}")
            
            params = {
                    'newClientOrderId': get_client_id()
                }
            

            loop = asyncio.get_event_loop()
            order = await loop.run_in_executor(
                None, 
                self.exchange.create_order,
                symbol, order_type, side, float(amount), float(price) if price else None, params
            )
            self.logger.info(f"Order placed: {order['id']} - {side} {amount:.8f} {symbol} @ {price:.4f}")
            return order
        except Exception as e:
            self.logger.error(f"Failed to place order: {e}")
            self.logger.debug(f"Order details - Symbol: {symbol}, Side: {side}, Amount: {amount}, Price: {price}, Type: {order_type}")
            return None
    

    def _validate_amount(self, amount: float, market: dict) -> float:
        """Validate and adjust order amount - FIXED to handle zero properly"""
        try:
            amount = float(amount)
            
            # If amount is zero or negative, return 0 immediately
            if amount <= 0:
                return 0.0
            
            limits = market.get('limits', {}).get('amount', {})
            min_amount = float(limits.get('min', 0.00001))
            max_amount = float(limits.get('max', float('inf')))
            
            # Get step size (lot size)
            precision = market.get('precision', {})
            if 'amount' in precision:
                if isinstance(precision['amount'], int):
                    amount_precision = precision['amount']
                    step_size = 10 ** (-amount_precision)
                else:
                    step_size = float(precision['amount'])
                    amount_precision = decimal_precision_decimal(step_size)
            else:
                amount_precision = 8
                step_size = 0.00000001
            
            # Round to step size
            if step_size > 0:
                amount = round(amount / step_size) * step_size
            
            # Round to precision
            amount = round(amount, amount_precision)
            
            # Ensure minimum
            if amount < min_amount:
                amount = min_amount
                if step_size > 0:
                    amount = round(amount / step_size) * step_size
                amount = round(amount, amount_precision)
            
            # Ensure maximum  
            if amount > max_amount:
                amount = max_amount
                
            return float(amount)
        except Exception as e:
            self.logger.error(f"Error validating amount {amount}: {e}")
            return 0.0
    
    def _validate_price(self, price: float, market: dict) -> float:
        """Validate and adjust order price"""
        try:
            price = float(price)
            
            limits = market.get('limits', {}).get('price', {})
            min_price = float(limits.get('min', 0.01))
            max_price = float(limits.get('max', float('inf')))
            
            # Get tick size
            precision = market.get('precision', {})
            if 'price' in precision:
                if isinstance(precision['price'], int):
                    price_precision = precision['price']
                    tick_size = 10 ** (-price_precision)
                else:
                    tick_size = float(precision['price'])
                    price_precision = decimal_precision_decimal(tick_size)
            else:
                price_precision = 2
                tick_size = 0.01
            
            # Round to tick size
            if tick_size > 0:
                price = round(price / tick_size) * tick_size
            
            # Round to precision
            price = round(price, price_precision)
            
            # Ensure minimum
            if price < min_price:
                price = min_price
                
            # Ensure maximum
            if price > max_price:
                price = max_price
                
            return float(price)
        except Exception as e:
            self.logger.error(f"Error validating price {price}: {e}")
            return float(price)
    
    async def cancel_order(self, order_id: str, symbol: str):
        """Cancel an order"""
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self.exchange.cancel_order, order_id, symbol)
            self.logger.info(f"Order cancelled: {order_id}")
            return result
        except Exception as e:
            self.logger.error(f"Failed to cancel order {order_id}: {e}")
            return None
    
    async def get_open_orders(self, symbol: str = None):
        """Get open orders"""
        try:
            loop = asyncio.get_event_loop()
            orders = await loop.run_in_executor(None, self.exchange.fetch_open_orders, symbol)
            return orders
        except Exception as e:
            self.logger.error(f"Failed to fetch open orders: {e}")
            return []
    
    async def get_my_trades(self, symbol: str, limit: int = 100):
        """Get recent trades"""
        try:
            loop = asyncio.get_event_loop()
            trades = await loop.run_in_executor(None, self.exchange.fetch_my_trades, symbol, None, limit)
            return trades
        except Exception as e:
            self.logger.error(f"Failed to fetch trades for {symbol}: {e}")
            return []
    
    async def get_ticker(self, symbol: str):
        """Get ticker data"""
        try:
            loop = asyncio.get_event_loop()
            ticker = await loop.run_in_executor(None, self.exchange.fetch_ticker, symbol)
            return ticker
        except Exception as e:
            self.logger.error(f"Failed to fetch ticker for {symbol}: {e}")
            return None
    
    async def watch_order_book(self, symbol: str, limit: int = 20):
        """Watch order book via WebSocket (CCXT Pro)"""
        if not self.exchange_ws:
            return None
        try:
            order_book = await self.exchange_ws.watch_order_book(symbol, limit)
            return order_book
        except Exception as e:
            self.logger.error(f"Failed to watch order book: {e}")
            return None
    
    def get_market_info(self, symbol: str):
        """Get market information"""
        try:
            if not self.markets_loaded:
                self.logger.warning("Markets not loaded yet")
                return None
                
            if symbol in self.exchange.markets:
                market = self.exchange.markets[symbol]
                return market
            else:
                self.logger.error(f"Symbol {symbol} not found in markets")
                available_symbols = [s for s in self.exchange.markets.keys() if symbol.replace('/', '') in s]
                if available_symbols:
                    self.logger.info(f"Similar symbols found: {available_symbols[:5]}")
                return None
        except Exception as e:
            self.logger.error(f"Failed to get market info for {symbol}: {e}")
            return None
    
    async def close(self):
        """Close exchange connections"""
        try:
            if self.exchange_ws:
                await self.exchange_ws.close()
            if hasattr(self.exchange, 'close') and asyncio.iscoroutinefunction(self.exchange.close):
                await self.exchange.close()
            elif hasattr(self.exchange, 'close'):
                self.exchange.close()
            self.logger.info("Exchange connections closed")
        except Exception as e:
            self.logger.error(f"Error closing exchange connections: {e}")

    async def get_trading_fees(self, symbol: str = None):
        """Fetch trading fees from exchange"""
        try:
            loop = asyncio.get_event_loop()
            fees = await loop.run_in_executor(None, self.exchange.fetch_trading_fees)
            
            if symbol and symbol in fees:
                return fees[symbol]
            elif self.exchange.markets and symbol in self.exchange.markets:
                market = self.exchange.markets[symbol]
                return {
                    'maker': market.get('maker', 0.001),
                    'taker': market.get('taker', 0.001),
                    'percentage': True,
                    'tierBased': True
                }
            else:
                # Default fees if we can't get specific ones
                return {
                    'maker': 0.001,
                    'taker': 0.001,
                    'percentage': True,
                    'tierBased': True
                }
        except Exception as e:
            self.logger.error(f"Failed to fetch trading fees: {e}")
            # Return default fees
            return {
                'maker': 0.001,
                'taker': 0.001,
                'percentage': True,
                'tierBased': True
            }

# ============================================================================
# WEBSOCKET MANAGER FOR CCXT PRO
# ============================================================================

class WebSocketManager:
    def __init__(self, client: BinanceCCXTClient, symbol: str):
        self.client = client
        self.symbol = symbol
        self.running = False
        self.callbacks = {
            'order_book': [],
            'trades': [],
            'balance': []
        }
        
    def add_callback(self, event_type: str, callback):
        """Add callback for WebSocket events"""
        if event_type in self.callbacks:
            self.callbacks[event_type].append(callback)
            
    
    async def start(self):
        """Start WebSocket connections"""
        if not self.client.exchange_ws:
            logging.warning("CCXT Pro not available, using REST API polling")
            return False
        
        self.running = True
        
        tasks = [
            asyncio.create_task(self._watch_order_book()),
            asyncio.create_task(self._watch_balance()),
        ]
        
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logging.error(f"WebSocket error: {e}")
        finally:
            self.running = False
    
    async def _watch_order_book(self):
        """Watch order book updates"""
        while self.running:
            try:
                order_book = await self.client.watch_order_book(self.symbol)
                if order_book:
                    for callback in self.callbacks['order_book']:
                        try:
                            await callback(order_book)
                        except Exception as e:
                            logging.error(f"Order book callback error: {e}")
            except Exception as e:
                logging.error(f"Order book watch error: {e}")
                await asyncio.sleep(5)
    
    async def _watch_balance(self):
        """Watch balance updates"""
        while self.running:
            try:
                if hasattr(self.client.exchange_ws, 'watch_balance'):
                    balance = await self.client.exchange_ws.watch_balance()
                    if balance:
                        for callback in self.callbacks['balance']:
                            try:
                                await callback(balance)
                            except Exception as e:
                                logging.error(f"Balance callback error: {e}")
            except Exception as e:
                logging.error(f"Balance watch error: {e}")
                await asyncio.sleep(10)

# ============================================================================
# MARKET MAKER BOT - FIXED WITH REALIZED P&L TRACKING
# ============================================================================

class MarketMakerBot:
    def __init__(self, config: Config, client: Optional[BinanceCCXTClient] = None, owns_client: bool = True):
        self.config = config
        self.owns_client = owns_client and client is None
        self.client = client or BinanceCCXTClient(config.API_KEY, config.API_SECRET, testnet=config.USE_TESTNET)
        self.ws_manager = None
        
        # Market State
        self.market_state = MarketState(config.SYMBOL)
        self.price_history = deque(maxlen=config.PRICE_HISTORY_SIZE)
        self.volatility_ema = EMA(alpha=2.0 / (config.VOLATILITY_WINDOW + 1))
        
        # Position & P&L
        self.position = Position(config.SYMBOL)
        self.balance = Balance()
        self.starting_balance = config.TOTAL_CAPITAL
        self.slot_capital = float(config.TOTAL_CAPITAL)  # bu pair için ayrılan USDT (bakiye/8)
        self.max_drawdown = 0.0
        self.kill_switch_active = False
        
        # FIXED: Trade tracking for realized P&L
        self.trade_history: List[Trade] = []
        self.last_trade_id: Optional[str] = None
        
        # Order Management
        self.current_quotes: Optional[Quotes] = None
        self.open_orders: Dict[str, Dict] = {}
        self.last_order_time = 0.0
        self.last_quote_time = 0.0
        
        # Market Info
        self.market_info = None
        self.tick_size = config.TICK_SIZE
        self.min_qty = config.MIN_QTY
        self.lot_size = config.LOT_SIZE
        self.min_notional = config.MIN_NOTIONAL
        
        # Metrics
        self.total_trades = 0
        self.bid_fills = 0
        self.ask_fills = 0
        self.total_volume = 0.0
        self.vwap_bid = 0.0
        self.vwap_ask = 0.0
        
        # Logging
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(f"{__name__}.{config.SYMBOL}")
    
    async def initialize_fees(self):
        """Initialize fee information from exchange"""
        try:
            fee_info = await self.client.get_trading_fees(self.config.SYMBOL)
            
            if fee_info:
                self.config.MAKER_FEE_RATE = fee_info.get('maker', 0.001)
                self.config.TAKER_FEE_RATE = fee_info.get('taker', 0.001)
                
                # Check if fees are percentage-based (they should be)
                if not fee_info.get('percentage', True):
                    self.logger.warning("Fees are not percentage-based, this might affect calculations")
                
                self.logger.info(f"Trading fees: Maker={self.config.MAKER_FEE_RATE*100:.3f}%, "
                            f"Taker={self.config.TAKER_FEE_RATE*100:.3f}%")
            else:
                self.logger.warning("Using default fee rates")
                
        except Exception as e:
            self.logger.error(f"Error initializing fees: {e}")
            # Use defaults
            self.config.MAKER_FEE_RATE = 0.001
            self.config.TAKER_FEE_RATE = 0.001

    async def refresh_fees_periodically(self, interval: int = 3600):
        """Periodically refresh fee information"""
        while True:
            await asyncio.sleep(interval)
            try:
                await self.initialize_fees()
                self.logger.info("Refreshed fee information")
            except Exception as e:
                self.logger.error(f"Error refreshing fees: {e}")

    async def start(self, initialize_client: bool = True):
        """Start the market maker bot"""
        self.logger.info(f"Starting MM {self.config.SYMBOL} | slot=${self.slot_capital:.2f}")
        
        if initialize_client:
            if not await self.client.initialize():
                self.logger.error("Failed to initialize CCXT client")
                return
        
        await self.load_market_info()
        await self.initialize_market_data()

        # Start periodic fee refresh
        asyncio.create_task(self.refresh_fees_periodically())

        await self.start_websockets()
        await self.trading_loop()
    
    async def load_market_info(self):
        """Load market information from exchange"""
        try:
            symbol_formats = [
                self.config.SYMBOL,
                f"{self.config.BASE_ASSET}/{self.config.QUOTE_ASSET}",
                self.config.SYMBOL.replace('USDT', '/USDT'),
            ]
            
            market_info = None
            actual_symbol = None
            
            for symbol_format in symbol_formats:
                market_info = self.client.get_market_info(symbol_format)
                if market_info:
                    actual_symbol = symbol_format
                    break
            
            if market_info:
                self.market_info = market_info
                
                limits = market_info.get('limits', {})
                precision = market_info.get('precision', {})
                
                # Price precision and tick size
                if 'price' in precision:
                    price_precision = precision['price']
                    if isinstance(price_precision, int):
                        self.tick_size = 10 ** (-price_precision)
                    else:
                        self.tick_size = float(price_precision)
                
                # Quantity precision and lot size  
                if 'amount' in precision:
                    amount_precision = precision['amount']
                    if isinstance(amount_precision, int):
                        self.lot_size = 10 ** (-amount_precision)
                    else:
                        self.lot_size = float(amount_precision)
                
                # Minimum quantity
                if 'amount' in limits and 'min' in limits['amount']:
                    self.min_qty = limits['amount']['min']
                
                # Minimum notional
                if 'cost' in limits and 'min' in limits['cost']:
                    self.min_notional = limits['cost']['min']
                
                if actual_symbol != self.config.SYMBOL:
                    self.logger.info(f"Using symbol format: {actual_symbol}")
                    self.config.SYMBOL = actual_symbol
                
                self.logger.info(f"Market info loaded: tick={self.tick_size}, lot={self.lot_size}, min_qty={self.min_qty}, min_notional=${self.min_notional}")
                
            else:
                self.logger.error(f"Could not load market info")
                self.logger.warning("Using default market parameters")
                
        except Exception as e:
            self.logger.error(f"Error loading market info: {e}")
    
    async def initialize_market_data(self):
        """Initialize market data and position"""
        await self.update_balance()
        
        order_book = await self.client.get_order_book(self.config.SYMBOL)
        if order_book:
            await self.update_market_state_from_ccxt(order_book)
        
        # Initialize fee information
        await self.initialize_fees()

        # FIXED: Only track trades made after bot starts
        await self.load_trade_history_and_calculate_pnl()
        
        # Set position from exchange balance, not from P&L calculation
        self.position.quantity = self.balance.base_total
        
        self.logger.info(f"Initialized - Position: {self.position.quantity} {self.config.BASE_ASSET}")
        self.logger.info(f"Balance - {self.config.BASE_ASSET}: {self.balance.base_free:.6f}, {self.config.QUOTE_ASSET}: {self.balance.quote_free:.2f}")
        self.logger.info(f"Realized P&L: ${self.position.realized_pnl:.2f}")
    
    async def load_trade_history_and_calculate_pnl(self):
        """Load trade history but don't calculate P&L from past trades"""
        try:
            # Just get the latest trade ID to know where to start tracking
            trades = await self.client.get_my_trades(self.config.SYMBOL, limit=1)
            if trades:
                self.last_trade_id = str(trades[-1]['id'])
                self.logger.info(f"Starting to track trades from ID: {self.last_trade_id}")
            else:
                self.last_trade_id = None
                self.logger.info("No existing trades found")
        except Exception as e:
            self.logger.error(f"Error loading initial trade ID: {e}")
            self.last_trade_id = None

    def calculate_realized_pnl(self):
        """Calculate realized P&L only from trades made during this session"""
        try:
            if not self.trade_history:
                return
            
            realized_pnl = 0.0
            
            for trade in self.trade_history:
                # Use actual fees from trade data, not estimated rates
                actual_fee = trade.fee
                
                if trade.side == 'buy':
                    # For buys, we're spending quote currency
                    cost = trade.amount * trade.price + actual_fee
                    realized_pnl -= cost
                else:  # sell
                    # For sells, we're receiving quote currency
                    revenue = trade.amount * trade.price - actual_fee
                    realized_pnl += revenue
            
            self.position.realized_pnl = realized_pnl
            self.logger.debug(f"P&L calculation complete: Realized=${realized_pnl:.2f}")
            
        except Exception as e:
            self.logger.error(f"Error calculating realized P&L: {e}")

    async def update_balance(self):
        """Update account balance tracking and position"""
        balance_data = await self.client.get_account_balance()
        if balance_data:
            base_balance = balance_data.get('total', {}).get(self.config.BASE_ASSET, 0.0)
            base_free = balance_data.get('free', {}).get(self.config.BASE_ASSET, 0.0)
            quote_balance = balance_data.get('total', {}).get(self.config.QUOTE_ASSET, 0.0)
            quote_free = balance_data.get('free', {}).get(self.config.QUOTE_ASSET, 0.0)
            
            # Update position from exchange
            self.position.quantity = float(base_balance)
            
            self.balance.base_total = float(base_balance)
            self.balance.base_free = float(base_free)
            self.balance.quote_total = float(quote_balance)
            self.balance.quote_free = float(quote_free)
            self.balance.last_update = time.time()
            
            self.logger.debug(f"Balance updated - {self.config.BASE_ASSET}: {self.balance.base_free:.6f} free, "
                            f"{self.config.QUOTE_ASSET}: {self.balance.quote_free:.2f} free")
        
    
    async def check_for_new_trades(self):
        """Check for new trades and update realized P&L - FIXED VERSION"""
        try:
            # Fetch recent trades
            new_trades = await self.client.get_my_trades(self.config.SYMBOL, limit=50)
            if not new_trades:
                return
            
            # Find trades newer than our last recorded trade
            new_trade_found = False
            for trade_data in new_trades:
                trade_id = str(trade_data['id'])
                
                # Skip if we've already processed this trade
                if any(t.id == trade_id for t in self.trade_history):
                    continue
                
                # If we have a last_trade_id, only process newer trades
                if self.last_trade_id and int(trade_id) <= int(self.last_trade_id):
                    continue
                    
                # New trade found
                new_trade_found = True
                trade = Trade(
                    id=trade_id,
                    symbol=trade_data['symbol'],
                    side=trade_data['side'],
                    amount=float(trade_data['amount']),
                    price=float(trade_data['price']),
                    timestamp=float(trade_data['timestamp']) / 1000,
                    fee=float(trade_data.get('fee', {}).get('cost', 0)),
                    fee_currency=trade_data.get('fee', {}).get('currency', '')
                )
                
                self.trade_history.append(trade)
                
                # Log the fill
                self.logger.info(f"New fill detected: {trade.side.upper()} {trade.amount:.6f} @ ${trade.price:.2f}")
                
                # Update fill counts
                if trade.side == 'buy':
                    self.bid_fills += 1
                else:
                    self.ask_fills += 1
                
                self.total_trades += 1
                self.total_volume += trade.amount
                
                # Update last trade ID
                self.last_trade_id = trade_id
            
            if new_trade_found:
                # Update position from exchange (not from P&L calculation)
                await self.update_balance()
                
                # Recalculate realized P&L only from new trades
                self.calculate_realized_pnl()
                self.logger.info(f"Updated realized P&L: ${self.position.realized_pnl:.2f}")
                
        except Exception as e:
            self.logger.error(f"Error checking for new trades: {e}")

    
    def can_place_buy_order(self, order_size: float, price: float) -> bool:
        """Check if we can place a buy order (slot bütçesi ile sınırlı)"""
        required_quote = order_size * price * 1.01
        slot_budget = max(self.slot_capital, self.config.TOTAL_CAPITAL)
        
        if required_quote > slot_budget * 1.05:
            return False
        
        if self.balance.quote_free < required_quote:
            return False
        
        if self.balance.quote_free < self.config.MIN_QUOTE_BALANCE:
            return False
        
        return True
    
    def can_place_sell_order(self, order_size: float) -> bool:
        """Check if we can place a sell order"""
        if self.balance.base_free < order_size:
            return False
        
        if self.balance.base_free < self.config.MIN_QTY:
            return False
        
        if not self.config.ALLOW_SHORT_SELLING and self.balance.base_free <= 0:
            return False
        
        return True
    
    async def start_websockets(self):
        """Start WebSocket connections"""
        self.ws_manager = WebSocketManager(self.client, self.config.SYMBOL)
        
        self.ws_manager.add_callback('order_book', self.on_order_book_update)
        self.ws_manager.add_callback('balance', self.on_balance_update)
        
        asyncio.create_task(self.ws_manager.start())
        self.logger.info("WebSocket connections started")
    
    async def on_order_book_update(self, order_book):
        """Handle order book updates from WebSocket"""
        await self.update_market_state_from_ccxt(order_book)
    
    async def on_balance_update(self, balance):
        """Handle balance updates from WebSocket (fill detection)"""
        try:
            # Check for position changes (fills)
            if self.config.BASE_ASSET in balance.get('total', {}):
                new_base_total = balance['total'][self.config.BASE_ASSET]
                
                if new_base_total != self.balance.base_total:
                    fill_qty = new_base_total - self.balance.base_total
                    self.logger.info(f"Fill detected via WebSocket: {fill_qty:+.8f} {self.config.BASE_ASSET}")
                    
                    # Update balance immediately
                    self.balance.base_total = new_base_total
                    self.balance.base_free = balance.get('free', {}).get(self.config.BASE_ASSET, 0.0)
                    
                    # Check for new trades and update P&L
                    await self.check_for_new_trades()
            
            # Update quote balance
            if self.config.QUOTE_ASSET in balance.get('total', {}):
                self.balance.quote_total = balance['total'][self.config.QUOTE_ASSET]
                self.balance.quote_free = balance.get('free', {}).get(self.config.QUOTE_ASSET, 0.0)
            
            self.balance.last_update = time.time()
            
        except Exception as e:
            self.logger.error(f"Error processing balance update: {e}")
    
    async def update_market_state_from_ccxt(self, order_book):
        """Update market state from CCXT order book format"""
        try:
            bids = order_book.get('bids', [])
            asks = order_book.get('asks', [])
            
            if not bids or not asks:
                return
            
            best_bid, bid_qty = bids[0][0], bids[0][1]
            best_ask, ask_qty = asks[0][0], asks[0][1]
            
            self.market_state.bid = best_bid
            self.market_state.ask = best_ask
            self.market_state.mid_price = (best_bid + best_ask) / 2
            self.market_state.last_update = time.time()
            
            # Calculate microprice
            total_qty = bid_qty + ask_qty
            if total_qty > 0:
                self.market_state.microprice = (best_bid * ask_qty + best_ask * bid_qty) / total_qty
            
            # Calculate imbalance
            self.market_state.imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty) if (bid_qty + ask_qty) > 0 else 0
            
            # Update price history and volatility
            self.price_history.append((time.time(), self.market_state.mid_price))
            self.update_volatility()
            
            # Update regime
            self.market_state.regime = self.detect_regime()
            
            # Update position P&L
            self.position.update_unrealized_pnl(self.market_state.mid_price)
            
            # Generate new quotes if needed
            await self.maybe_update_quotes()
            
        except Exception as e:
            self.logger.error(f"Error updating market state: {e}")
    
    def update_volatility(self):
        """Update volatility using EWMA of returns"""
        if len(self.price_history) < 2:
            return
        
        recent_prices = list(self.price_history)[-60:]
        returns = []
        
        for i in range(1, len(recent_prices)):
            prev_price = recent_prices[i-1][1]
            curr_price = recent_prices[i][1]
            if prev_price > 0:
                returns.append(math.log(curr_price / prev_price))
        
        if returns:
            volatility = math.sqrt(sum(r**2 for r in returns) / len(returns)) * math.sqrt(60 * 10)
            self.market_state.volatility = self.volatility_ema.update(volatility)
    
    def detect_regime(self) -> str:
        """Detect market regime (trend vs range)"""
        if len(self.price_history) < 20:
            return "range"
        
        recent_prices = [p[1] for p in list(self.price_history)[-20:]]
        
        x = list(range(len(recent_prices)))
        n = len(recent_prices)
        
        sum_x = sum(x)
        sum_y = sum(recent_prices)
        sum_xy = sum(x[i] * recent_prices[i] for i in range(n))
        sum_x2 = sum(xi**2 for xi in x)
        
        slope = (n * sum_xy - sum_x * sum_y) / (n * sum_x2 - sum_x**2)
        normalized_slope = abs(slope / (sum_y / n))
        
        return "trend" if normalized_slope > 0.001 else "range"
    
    async def maybe_update_quotes(self):
        """Update quotes if conditions are met"""
        now = time.time()
        
        if now - self.last_quote_time < self.config.MAX_ORDER_REPLACE_FREQ:
            return
        
        if self.kill_switch_active:
            await self.cancel_all_orders()
            return
        
        await self.update_balance()
        
        new_quotes = self.generate_quotes()
        if not new_quotes:
            return
        
        if self.should_replace_orders(new_quotes):
            await self.replace_orders(new_quotes)
            self.last_quote_time = now
    
    def generate_quotes(self) -> Optional[Quotes]:
        """Generate bid/ask quotes"""
        if self.market_state.mid_price <= 0:
            return None
        
        try:
            if self.config.USE_AVELLANEDA:
                quotes = self.generate_avellaneda_quotes()
            else:
                quotes = self.generate_volatility_quotes()
                
            if quotes:
                quotes.can_place_bid = self.can_place_buy_order(quotes.bid_size, quotes.bid_price)
                quotes.can_place_ask = self.can_place_sell_order(quotes.ask_size)
                
            return quotes
        except Exception as e:
            self.logger.error(f"Error generating quotes: {e}")
            return None
    

    # Additional Fix: More conservative volatility-based quotes as fallback
    def generate_volatility_quotes(self) -> Quotes:
        """Generate quotes using simple volatility-based spread - MADE MORE CONSERVATIVE"""
        mid_price = self.market_state.mid_price
        fee_adjustment = mid_price * self.config.MAKER_FEE_RATE * 4
        volatility = max(self.market_state.volatility, 0.001)
        
        # More conservative spread calculation
        base_spread = max(
            self.config.BASE_SPREAD_TICKS * self.tick_size,
            mid_price * 0.0005  # Minimum 0.05% spread
        )
        
        # Reduce volatility impact
        vol_spread = min(
            self.config.VOLATILITY_MULTIPLIER * volatility * mid_price * 0.1,  # REDUCED volatility impact
            mid_price * 0.01  # Cap at 1% 
        )
        
        half_spread = (base_spread + vol_spread) / 2 + fee_adjustment
        
        # Conservative inventory and imbalance skew
        inventory_skew = self.get_inventory_skew() * 0.5  # REDUCED impact
        imbalance_skew = self.get_imbalance_skew() * 0.5  # REDUCED impact
        
        # Calculate prices
        bid_price = mid_price - half_spread + inventory_skew - imbalance_skew
        ask_price = mid_price + half_spread + inventory_skew + imbalance_skew

        # ADJUST FOR FEES using dynamically fetched rates
        #bid_price = bid_price * (1 - self.config.MAKER_FEE_RATE)
        #ask_price = ask_price * (1 + self.config.MAKER_FEE_RATE)
        
        # Ensure reasonable bounds
        bid_price = max(bid_price, mid_price * 0.99)   # No more than 1% below mid
        ask_price = min(ask_price, mid_price * 1.01)   # No more than 1% above mid
        
        # Round to tick size
        bid_price = self.round_to_tick(bid_price)
        ask_price = self.round_to_tick(ask_price)
        
        # Final validation
        if ask_price <= bid_price:
            spread = max(self.tick_size * 2, mid_price * 0.0005)
            bid_price = mid_price - spread/2
            ask_price = mid_price + spread/2
            bid_price = self.round_to_tick(bid_price)
            ask_price = self.round_to_tick(ask_price)
        
        bid_size, ask_size = self.calculate_order_sizes()
        
        return Quotes(bid_price, ask_price, bid_size, ask_size, time.time())
    
    # Issue 2 Fix: Prices too far from market - problem in Avellaneda model
    # The reservation price calculation is going haywire

    def generate_avellaneda_quotes(self) -> Quotes:
        """Generate quotes using Avellaneda-Stoikov model - FIXED extreme pricing"""
        
        mid_price = self.market_state.mid_price
        
        fee_adjustment = mid_price * self.config.MAKER_FEE_RATE * 4

        volatility = max(self.market_state.volatility, 0.001)
        
        # Validate inputs
        if mid_price <= 0:
            self.logger.warning(f"Invalid mid_price: {mid_price}, falling back to volatility quotes")
            return self.generate_volatility_quotes()
        
        # Much more conservative Avellaneda-Stoikov parameters
        gamma = max(0.001, min(self.config.RISK_AVERSION, 0.05))    # REDUCED: Risk aversion capped at 1%
        k = max(0.0001, min(self.config.MARKET_IMPACT, 0.001))     # REDUCED: Market impact much smaller
        T = max(0.1, min(self.config.TIME_HORIZON, 0.5))           # REDUCED: Shorter time horizon
        
        # Current inventory position (normalized) - clamp to smaller range
        inventory_pos = self.get_inventory_position()
        q = max(-0.5, min(inventory_pos, 0.5))  # REDUCED: Limit inventory impact to ±50%
        
        # Much more conservative price adjustment
        volatility_squared = min(volatility ** 2, 0.0001)  # REDUCED: Cap vol² much lower
        price_adjustment = q * gamma * volatility_squared * T
        
        # CRITICAL: Limit price adjustment to tiny percentage
        max_adjustment = mid_price * 0.1  # REDUCED: Only 0.1% max adjustment instead of 10%
        price_adjustment = max(-max_adjustment, min(price_adjustment, max_adjustment))
        
        reservation_price = mid_price - price_adjustment
        
        # Validate reservation price is close to mid
        if abs(reservation_price - mid_price) > mid_price * 0.01:  # If more than 1% away
            self.logger.warning(f"Reservation price {reservation_price:.4f} too far from mid {mid_price:.4f}, using mid")
            reservation_price = mid_price
        
        # Much smaller base spread
        base_half_spread = max(
            self.config.BASE_SPREAD_TICKS * self.tick_size / 2,  # Minimum spread from config
            mid_price * 0.0001  # Or 0.01% of price, whichever is larger
        )
        
        # Simplified spread calculation - ignore complex impact adjustment for now
        half_spread = base_half_spread + fee_adjustment
        
        # Convert volatility component more conservatively
        if volatility > 0:
            vol_component = volatility * mid_price * 0.1  # REDUCED: Much smaller volatility impact
            half_spread = max(half_spread, vol_component)
        
        # Cap the spread at reasonable level
        max_half_spread = mid_price * 0.01  # Max 1% half spread
        half_spread = min(half_spread, max_half_spread)
        
        # Calculate bid and ask prices around reservation price
        bid_price = reservation_price - half_spread
        ask_price = reservation_price + half_spread

        # ADJUST FOR FEES using dynamically fetched rates
        #bid_price = bid_price * (1 - self.config.MAKER_FEE_RATE)
        #ask_price = ask_price * (1 + self.config.MAKER_FEE_RATE)
        
        # Apply small imbalance skew
        imbalance_skew = self.get_imbalance_skew()
        max_skew = half_spread * 0.2  # Limit skew to 20% of spread
        imbalance_skew = max(-max_skew, min(imbalance_skew, max_skew))
        
        bid_price -= imbalance_skew
        ask_price += imbalance_skew
        
        # Ensure prices are reasonable - within 2% of mid price
        min_bid = mid_price * 0.98
        max_ask = mid_price * 1.02
        
        if bid_price < min_bid:
            self.logger.warning(f"Bid too low: {bid_price:.4f}, adjusting to {min_bid:.4f}")
            bid_price = min_bid
        
        if ask_price > max_ask:
            self.logger.warning(f"Ask too high: {ask_price:.4f}, adjusting to {max_ask:.4f}")  
            ask_price = max_ask
        
        # Ensure bid < ask
        if bid_price >= ask_price:
            self.logger.warning("Bid >= Ask, adjusting spread")
            spread = max(self.tick_size * 2, mid_price * 0.0005)  # Minimum 2 ticks or 0.05%
            bid_price = mid_price - spread/2
            ask_price = mid_price + spread/2
        
        # Round to tick size
        bid_price = self.round_to_tick(bid_price)
        ask_price = self.round_to_tick(ask_price)
        
        # Final sanity check
        if bid_price <= 0 or ask_price <= 0 or ask_price <= bid_price:
            self.logger.error(f"Final price validation failed: bid={bid_price}, ask={ask_price}, falling back")
            return self.generate_volatility_quotes()
        
        # Final check - if prices are still too far from mid, use volatility method
        bid_diff_pct = abs(bid_price - mid_price) / mid_price
        ask_diff_pct = abs(ask_price - mid_price) / mid_price
        
        if bid_diff_pct > 0.02 or ask_diff_pct > 0.02:  # More than 2% away
            self.logger.warning(f"Avellaneda prices too far from mid (bid: {bid_diff_pct:.2%}, ask: {ask_diff_pct:.2%}), using volatility method")
            return self.generate_volatility_quotes()
        
        # Calculate order sizes
        bid_size, ask_size = self.calculate_order_sizes()
        
        self.logger.debug(f"Avellaneda quotes: bid={bid_price:.4f} ({bid_diff_pct:.3%} from mid), "
                        f"ask={ask_price:.4f} ({ask_diff_pct:.3%} from mid)")
        
        return Quotes(bid_price, ask_price, bid_size, ask_size, time.time())


    def get_inventory_position(self) -> float:
        """Get normalized inventory position (-1 to 1)"""
        max_inventory = self.config.max_inventory_usd / self.market_state.mid_price
        if max_inventory == 0:
            return 0.0
        return max(-1.0, min(1.0, self.position.quantity / max_inventory))
    
    def get_inventory_skew(self) -> float:
        """Calculate inventory-based price skew"""
        inventory_pos = self.get_inventory_position()
        return -inventory_pos * self.config.INVENTORY_SKEW_STRENGTH * self.tick_size
    
    def get_imbalance_skew(self) -> float:
        """Calculate order book imbalance-based price skew"""
        return self.market_state.imbalance * self.config.IMBALANCE_SKEW_STRENGTH * self.tick_size
    
    
    # FIXES FOR THE MARKET MAKER BOT

    # Issue 1 Fix: Order size calculation returning zero
    # The problem is in calculate_order_sizes() method - it's too restrictive with balance checks

    def calculate_order_sizes(self) -> Tuple[float, float]:
        """Her pair için slot sermayesi = toplam bakiye / 8 — o tutarla al/sat."""
        if self.market_state.mid_price <= 0:
            self.logger.warning("Invalid mid price for size calculation")
            return 0.0, 0.0
        
        try:
            mid_price = float(self.market_state.mid_price)
            min_notional = float(self.min_notional)
            min_qty = float(self.min_qty)
            
            # Slot = bu coin için ayrılan USDT (bakiye/8)
            slot_usdt = float(self.slot_capital or self.config.TOTAL_CAPITAL)
            if slot_usdt <= 0:
                slot_usdt = float(self.balance.quote_free) / max(1, self.config.BALANCE_SLOTS)
            
            # Emir notional ≈ slot'un ~90%'ı (min notional üstü)
            target_notional = max(min_notional * 1.05, slot_usdt * 0.90)
            base_size = target_notional / mid_price
            
            # Envanter skew (hafif)
            inventory_pos = self.get_inventory_position()
            bid_size = base_size * max(0.5, 1.0 + float(inventory_pos) * 0.15)
            ask_size = base_size * max(0.5, 1.0 - float(inventory_pos) * 0.15)
            
            # AL: slot + serbest USDT ile sınırla
            max_buy_notional = min(float(self.balance.quote_free) * 0.95, slot_usdt)
            max_buy_size = max_buy_notional / mid_price if mid_price > 0 else 0.0
            if max_buy_size >= min_qty and max_buy_notional >= min_notional:
                bid_size = min(bid_size, max_buy_size)
            else:
                bid_size = 0.0
            
            # SAT: eldeki base (slot kadar USDT değerine kadar)
            max_sell_notional = slot_usdt
            max_sell_by_budget = max_sell_notional / mid_price if mid_price > 0 else 0.0
            max_sell_size = min(float(self.balance.base_free) * 0.95, max_sell_by_budget)
            if max_sell_size >= min_qty and (max_sell_size * mid_price) >= min_notional:
                ask_size = min(ask_size, max_sell_size)
            elif max_sell_size > 0 and float(self.balance.base_free) >= min_qty:
                # Min notional altındaysa satılabilir kadar koy (bakiye varsa)
                ask_size = max_sell_size if (max_sell_size * mid_price) >= min_notional else 0.0
            else:
                ask_size = 0.0
            
            bid_size = self.round_to_lot_size(bid_size) if bid_size > 0 else 0.0
            ask_size = self.round_to_lot_size(ask_size) if ask_size > 0 else 0.0
            
            # Son min-notional kontrolü
            if bid_size > 0 and bid_size * mid_price < min_notional:
                bid_size = 0.0
            if ask_size > 0 and ask_size * mid_price < min_notional:
                ask_size = 0.0
            
            self.logger.debug(
                f"[{self.config.SYMBOL}] slot=${slot_usdt:.2f} "
                f"bid={bid_size:.8f} (${bid_size * mid_price:.2f}) "
                f"ask={ask_size:.8f} (${ask_size * mid_price:.2f})"
            )
            
            return max(0.0, float(bid_size)), max(0.0, float(ask_size))
            
        except Exception as e:
            self.logger.error(f"Error calculating order sizes: {e}")
            return 0.0, 0.0
    
    def should_replace_orders(self, new_quotes: Quotes) -> bool:
        """Check if orders should be replaced"""
        if not self.current_quotes:
            return True
        
        bid_diff = abs(new_quotes.bid_price - self.current_quotes.bid_price)
        ask_diff = abs(new_quotes.ask_price - self.current_quotes.ask_price)
        
        min_price_diff = self.tick_size * 2
        
        if bid_diff >= min_price_diff or ask_diff >= min_price_diff:
            return True
        
        bid_size_diff = abs(new_quotes.bid_size - self.current_quotes.bid_size)
        ask_size_diff = abs(new_quotes.ask_size - self.current_quotes.ask_size)
        
        min_size_diff = self.min_qty * 2
        
        if bid_size_diff >= min_size_diff or ask_size_diff >= min_size_diff:
            return True
        
        if (new_quotes.can_place_bid != self.current_quotes.can_place_bid or 
            new_quotes.can_place_ask != self.current_quotes.can_place_ask):
            return True
        
        return False
    
    async def replace_orders(self, new_quotes: Quotes):
        """Replace current orders with new quotes"""
        await self.cancel_all_orders()
        await asyncio.sleep(0.1)
        
        orders_placed = []
        
        # FIXED: Only place order if size > 0 and we can place it
        if new_quotes.can_place_bid and new_quotes.bid_size > 0:
            bid_order = await self.client.place_order(
                self.config.SYMBOL, "buy", new_quotes.bid_size, new_quotes.bid_price, "limit"
            )
            
            if bid_order and bid_order.get('id'):
                self.open_orders[bid_order['id']] = {
                    'side': 'buy',
                    'price': new_quotes.bid_price,
                    'size': new_quotes.bid_size,
                    'timestamp': time.time(),
                    'symbol': self.config.SYMBOL
                }
                orders_placed.append(f"Bid: {new_quotes.bid_price:.4f} @ {new_quotes.bid_size:.6f}")
        
        if new_quotes.can_place_ask and new_quotes.ask_size > 0:
            ask_order = await self.client.place_order(
                self.config.SYMBOL, "sell", new_quotes.ask_size, new_quotes.ask_price, "limit"
            )
            
            if ask_order and ask_order.get('id'):
                self.open_orders[ask_order['id']] = {
                    'side': 'sell',
                    'price': new_quotes.ask_price,
                    'size': new_quotes.ask_size,
                    'timestamp': time.time(),
                    'symbol': self.config.SYMBOL
                }
                orders_placed.append(f"Ask: {new_quotes.ask_price:.4f} @ {new_quotes.ask_size:.6f}")
        
        self.current_quotes = new_quotes
        
        if orders_placed:
            self.logger.info(f"Orders placed - {', '.join(orders_placed)}")
        else:
            self.logger.debug("No orders placed due to balance constraints or zero sizes")
    
    async def cancel_all_orders(self):
        """Cancel all open orders using CCXT"""
        open_orders = await self.client.get_open_orders(self.config.SYMBOL)
        
        for order in open_orders:
            try:
                await self.client.cancel_order(order['id'], self.config.SYMBOL)
                if order['id'] in self.open_orders:
                    del self.open_orders[order['id']]
            except Exception as e:
                self.logger.error(f"Failed to cancel order {order['id']}: {e}")
        
        self.open_orders.clear()
    
    def round_to_tick(self, price: float) -> float:
        """Round price to exchange tick size"""
        if self.tick_size <= 0:
            return price
        return round(price / self.tick_size) * self.tick_size
    
    def round_to_lot_size(self, quantity: float) -> float:
        """Round quantity to minimum quantity increment"""
        if self.lot_size <= 0:
            return quantity
        return round(quantity / self.lot_size) * self.lot_size
    
    async def trading_loop(self):
        """Main trading loop with CCXT fallback"""
        self.logger.info("Starting main trading loop")
        
        while True:
            try:
                if not self.check_risk_limits():
                    if self.kill_switch_active:
                        self.logger.error("Kill switch activated - stopping trading")
                        await self.cancel_all_orders()
                        break
                
                if not self.ws_manager or not self.ws_manager.running:
                    await self.update_market_data_rest()
                
                await self.update_balance()
                await self.check_order_status()
                await self.print_status()
                
                await asyncio.sleep(1.0)
                
            except Exception as e:
                self.logger.error(f"Error in trading loop: {e}")
                await asyncio.sleep(5.0)
    
    async def update_market_data_rest(self):
        """Update market data using REST API as fallback"""
        try:
            order_book = await self.client.get_order_book(self.config.SYMBOL)
            if order_book:
                await self.update_market_state_from_ccxt(order_book)
        except Exception as e:
            self.logger.error(f"Error updating market data via REST: {e}")
    
    async def check_order_status(self):
        """Check status of our open orders"""
        try:
            open_orders = await self.client.get_open_orders(self.config.SYMBOL)
            open_order_ids = {order['id'] for order in open_orders}
            
            for order_id in list(self.open_orders.keys()):
                if order_id not in open_order_ids:
                    self.logger.info(f"Order {order_id} no longer open (likely filled)")
                    del self.open_orders[order_id]
                    self.last_quote_time = time.time() - self.config.MAX_ORDER_REPLACE_FREQ  ##trigger new quote

            for order in open_orders:
                if order['id'] not in self.open_orders:
                    self.open_orders[order['id']] = {
                        'side': order['side'],
                        'price': order['price'],
                        'size': order['amount'],
                        'timestamp': order['timestamp'] / 1000 if order['timestamp'] else time.time(),
                        'symbol': order['symbol']
                    }
        except Exception as e:
            self.logger.error(f"Error checking order status: {e}")
    
    def check_risk_limits(self) -> bool:
        """Check if risk limits are breached"""
        inventory_usd = abs(self.position.quantity) * self.market_state.mid_price
        if inventory_usd > self.config.max_inventory_usd:
            self.logger.warning(f"Inventory limit breached: ${inventory_usd:.2f}")
            return False
        
        # FIXED: Use realized P&L in drawdown calculation
        current_balance = self.starting_balance + self.position.realized_pnl + self.position.unrealized_pnl
        drawdown = self.starting_balance - current_balance
        
        if drawdown > self.config.max_drawdown_usd:
            self.logger.error(f"Max drawdown breached: ${drawdown:.2f}")
            self.kill_switch_active = True
            return False
        
        self.max_drawdown = max(self.max_drawdown, drawdown)
        return True
    
    async def shutdown(self):
        """Gracefully shutdown the bot"""
        self.logger.info(f"Shutting down {self.config.SYMBOL}...")
        
        await self.cancel_all_orders()
        
        if self.ws_manager:
            self.ws_manager.running = False
        
        if self.owns_client:
            await self.client.close()
        
        self.logger.info(f"{self.config.SYMBOL} shutdown complete")
    
    async def print_status(self):
        """Print current status to console - ENHANCED WITH REALIZED P&L"""
        current_time = datetime.now().strftime("%H:%M:%S")
        
        # FIXED: Calculate total P&L including realized P&L
        total_pnl = self.position.realized_pnl + self.position.unrealized_pnl
        pnl_pct = (total_pnl / self.starting_balance) * 100 if self.starting_balance > 0 else 0
        
        print(f"\n{'='*80}")
        print(f"Binance Market Maker Bot (CCXT) - {current_time}")
        print(f"{'='*80}")
        print(f"Symbol: {self.config.SYMBOL:10} | Regime: {self.market_state.regime:6} | "
              f"Spread: {self.market_state.spread:.4f}")
        print(f"Bid: {self.market_state.bid:8.4f} | Mid: {self.market_state.mid_price:8.4f} | "
              f"Ask: {self.market_state.ask:8.4f}")
        print(f"Position: {self.position.quantity:10.6f} {self.config.BASE_ASSET} | "
              f"Notional: ${self.position.notional_value:.2f}")
        
        print(f"Balance: {self.balance.base_free:8.6f} {self.config.BASE_ASSET} | "
              f"${self.balance.quote_free:8.2f} {self.config.QUOTE_ASSET}")
        
        # FIXED: Show realized P&L prominently
        print(f"Realized P&L: ${self.position.realized_pnl:8.2f} | "
              f"Unrealized P&L: ${self.position.unrealized_pnl:8.2f}")
        print(f"Total P&L: ${total_pnl:8.2f} ({pnl_pct:+6.2f}%) | "
              f"Max DD: ${self.max_drawdown:8.2f}")
        print(f"Volatility: {self.market_state.volatility*100:6.2f}% | "
              f"Imbalance: {self.market_state.imbalance:+6.2f} | "
              f"Open Orders: {len(self.open_orders)}")
        
        if self.current_quotes:
            print(f"Current Quotes:")
            bid_status = "✅" if self.current_quotes.can_place_bid and self.current_quotes.bid_size > 0 else "❌"
            ask_status = "✅" if self.current_quotes.can_place_ask and self.current_quotes.ask_size > 0 else "❌"
            print(f"  Bid: {self.current_quotes.bid_price:8.4f} @ {self.current_quotes.bid_size:8.6f} {bid_status}")
            print(f"  Ask: {self.current_quotes.ask_price:8.4f} @ {self.current_quotes.ask_size:8.6f} {ask_status}")
        
        print(f"Trades: {self.total_trades} | Volume: {self.total_volume:.6f} | "
              f"VWAP Bid: {self.vwap_bid:.4f} | VWAP Ask: {self.vwap_ask:.4f}")
        
        market_status = "Loaded" if self.market_info else "Not loaded"
        print(f"Market Info: {market_status} | Tick: {self.tick_size} | Min Notional: ${self.min_notional}")
        
        ws_status = "Connected" if self.ws_manager and self.ws_manager.running else "Disconnected (REST fallback)"
        print(f"WebSocket: {ws_status}")
        
        can_buy = self.can_place_buy_order(self.min_qty, self.market_state.mid_price)
        can_sell = self.can_place_sell_order(self.min_qty)
        print(f"Can Place: Buy {'✅' if can_buy else '❌'} | Sell {'✅' if can_sell else '❌'}")
        
        if self.kill_switch_active:
            print("🛑 KILL SWITCH ACTIVE - TRADING STOPPED")

# ============================================================================
# MAIN EXECUTION WITH CONFIG FILE SUPPORT
# ============================================================================

async def resolve_slot_capitals(client: BinanceCCXTClient, config: Config) -> Tuple[float, float, List[str]]:
    """Canlı USDT bakiyesini balance_slots'a böl; symbols listesini döndür."""
    symbols = list(config.SYMBOLS or [config.SYMBOL])
    slots = max(1, int(config.BALANCE_SLOTS or len(symbols) or DEFAULT_BALANCE_SLOTS))
    # Slot sayısı = min(8, symbols) — fazla coin varsa ilk N
    if len(symbols) > slots:
        symbols = symbols[:slots]
    elif len(symbols) < slots and len(symbols) == 1:
        # Tek symbol verilmişse DEFAULT_SYMBOLS ile doldur
        symbols = list(DEFAULT_SYMBOLS[:slots])

    quote = config.QUOTE_ASSET or "USDT"
    live_quote = 0.0
    bal = await client.get_account_balance()
    if bal:
        live_quote = float(bal.get("free", {}).get(quote, 0.0) or 0.0)

    if config.SPLIT_LIVE_BALANCE and live_quote > 0:
        total = live_quote
    elif config.TOTAL_CAPITAL and config.TOTAL_CAPITAL > 0:
        total = float(config.TOTAL_CAPITAL)
    else:
        total = live_quote

    slot = total / max(1, len(symbols))
    return total, slot, symbols


async def run_multi_market_maker(config: Config):
    """8 (veya config.symbols) pair — bakiyeyi eşit böl, paralel al/sat."""
    client = BinanceCCXTClient(config.API_KEY, config.API_SECRET, testnet=config.USE_TESTNET)
    if not await client.initialize():
        print("❌ Exchange bağlantısı kurulamadı")
        return

    total, slot, symbols = await resolve_slot_capitals(client, config)
    if slot <= 0:
        print("❌ USDT bakiyesi / total_capital yok — slot sermayesi 0")
        await client.close()
        return

    print(f"💰 Toplam USDT: ${total:,.2f}")
    print(f"🧩 Slotlar: {len(symbols)} × ${slot:,.2f} (= bakiye/{len(symbols)})")
    print(f"📊 Pairler: {', '.join(symbols)}")

    bots: List[MarketMakerBot] = []
    for sym in symbols:
        # Slot min notional altındaysa yine de dene (emir aşamasında filtrelenir)
        cfg = config.for_symbol(sym, slot_capital=slot)
        bot = MarketMakerBot(cfg, client=client, owns_client=False)
        bots.append(bot)

    async def _run_one(bot: MarketMakerBot, delay: float):
        await asyncio.sleep(delay)
        await bot.start(initialize_client=False)

    try:
        tasks = [
            asyncio.create_task(_run_one(bot, i * 0.4))
            for i, bot in enumerate(bots)
        ]
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("\n🛑 Shutting down all slots...")
    except Exception as e:
        print(f"❌ Multi-MM error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        for bot in bots:
            try:
                await bot.shutdown()
            except Exception:
                pass
        await client.close()
        print("✅ Tüm slotlar kapatıldı")


async def main():
    """Main execution function with external config file support"""
    
    print("Binance Market Maker Bot (CCXT) — CANLI AL/SAT × 8 SLOT")
    print("="*65)
    print()
    print("🔧 MOD:")
    print("✅ Tüm USDT bakiyesi 8'e bölünür")
    print("✅ 8 likit pair'de paralel limit AL/SAT")
    print("✅ Realized P&L + drawdown kill-switch")
    print("✅ Env: BINANCE_API_KEY / BINANCE_API_SECRET")
    print()
    
    # Config: script dizinindeki dosya (cwd fark etmez)
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_maker_config.json")
    config = Config.from_file(config_path)
    
    if not config:
        print("❌ Failed to load configuration file")
        print("📝 Please check market_maker_config.json and set your API keys")
        return
    
    # Validate API keys
    if (
        not config.API_KEY
        or not config.API_SECRET
        or config.API_KEY in ("your_binance_api_key_here", "your_actual_api_key_here")
    ):
        print("❌ ERROR: Canlı Binance API key/secret gerekli")
        print("   → market_maker_config.json içine yaz VEYA")
        print("   → export BINANCE_API_KEY=... BINANCE_API_SECRET=...")
        print("🔗 https://www.binance.com → API Management (Spot Trade izinli)")
        return
    
    print(f"✅ Configuration loaded from market_maker_config.json")
    print(f"🚀 Starting bot with {'TESTNET' if config.USE_TESTNET else 'LIVE TRADING'}")
    print(f"📈 Max Inventory/slot: {config.MAX_INVENTORY_RATIO*100:.1f}%")
    print(f"⚠️  Max Drawdown/slot: {config.MAX_DRAWDOWN_RATIO*100:.1f}%")
    
    if not config.USE_TESTNET:
        print("\n" + "="*50)
        print("CANLI MOD — bakiyeyi 8'e böl, gerçek AL/SAT")
        print("Durdurmak: Ctrl+C (açık emirler iptal edilir)")
        print("="*50)
    
    try:
        await run_multi_market_maker(config)
    except KeyboardInterrupt:
        print("\n🛑 Durduruldu")
    except Exception as e:
        print(f"❌ Bot error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    # Installation check
    try:
        import ccxt
        print(f"✅ CCXT v{ccxt.__version__} detected")
        try:
            import ccxt.pro as ccxtpro
            print("✅ CCXT Pro detected - WebSocket support available")
        except ImportError:
            print("ℹ️  CCXT Pro not available - using REST API only")
            print("   Install with: pip install ccxt[pro]")
    except ImportError:
        print("❌ CCXT not found!")
        print("📦 Install CCXT with: pip install ccxt")
        print("📦 For WebSocket support: pip install ccxt[pro]")
        exit(1)
    
    # Check if config file exists, create if not (script dizininde)
    _config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_maker_config.json")
    if not os.path.exists(_config_path):
        print("📝 Creating default configuration file...")
        ConfigFile.create_default_config(_config_path)
        print(f"✅ Created {_config_path}")
        print()
        print("⚠️  IMPORTANT: market_maker_config.json düzenle:")
        print("   1. Gerçek Binance API key + secret (Spot Trade)")
        print("   2. testnet: false (canlı)")
        print("   3. symbols / balance_slots=8 (bakiye eşit bölünür)")
        print()
        print("📋 Örnek (CANLI × 8):")
        print("""
{
    "exchange": {
        "api_key": "...",
        "api_secret": "...",
        "testnet": false,
        "quote_asset": "USDT",
        "symbols": ["BTC/USDT","ETH/USDT","BNB/USDT","SOL/USDT","XRP/USDT","DOGE/USDT","ADA/USDT","AVAX/USDT"]
    },
    "trading": {
        "balance_slots": 8,
        "split_live_balance": true,
        "max_inventory_ratio": 0.9,
        "max_drawdown_ratio": 0.05
    }
}
        """)
        exit(0)
    
    # Run the bot (WindowsSelector sadece Windows'ta)
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass

    asyncio.run(main())
