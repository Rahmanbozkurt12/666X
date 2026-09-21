# Binance Market Maker Bot (CCXT Edition)

A sophisticated cryptocurrency market making bot for Binance spot trading, implementing the Avellaneda-Stoikov model with comprehensive risk management and real-time P&L tracking.

## Features

### Core Trading Features
- **Avellaneda-Stoikov Market Making**: Advanced algorithmic market making with optimal bid/ask pricing
- **Real-time Order Book Monitoring**: WebSocket-based order book updates for minimal latency
- **Dynamic Spread Calculation**: Volatility and inventory-aware spread adjustments
- **Inventory Management**: Automatic position skewing to manage inventory risk
- **Multi-regime Support**: Adapts strategy between trending and ranging markets

### Risk Management
- **Real-time P&L Tracking**: Separate realized and unrealized P&L calculations
- **Position Limits**: Maximum inventory ratio and drawdown protection
- **Kill Switch**: Automatic trading halt on risk limit breach
- **Balance Validation**: Ensures sufficient funds before order placement
- **Fee-aware Pricing**: Incorporates maker fees into quote calculations

### Technical Features
- **External Configuration**: JSON-based configuration system
- **CCXT Integration**: Professional-grade exchange connectivity
- **WebSocket Support**: Real-time market data and order updates
- **Robust Error Handling**: Graceful degradation to REST API fallback
- **Comprehensive Logging**: Detailed logging for monitoring and debugging

## Requirements

```bash
pip install ccxt
pip install ccxt[pro]  # Optional: for WebSocket support
```

### Dependencies
- Python 3.8+
- ccxt >= 3.0.0
- ccxt.pro (optional, for WebSocket features)

## Quick Start

### 1. Setup Configuration

Run the bot once to generate the default configuration file:

```bash
python binance_market_maker_bot_3.py
```

This creates `market_maker_config.json` with default settings.

### 2. Configure API Keys

Edit `market_maker_config.json` and add your Binance API credentials:

```json
{
    "exchange": {
        "api_key": "your_binance_api_key_here",
        "api_secret": "your_binance_api_secret_here",
        "testnet": true,
        "symbol": "BTC/USDT",
        "base_asset": "BTC",
        "quote_asset": "USDT"
    },
    "trading": {
        "total_capital": 329.0,
        "max_inventory_ratio": 0.2,
        "max_drawdown_ratio": 0.03,
        "base_spread_ticks": 3.0,
        "volatility_multiplier": 4.0,
        "inventory_skew_strength": 3.0,
        "min_order_lifetime": 10.0,
        "min_base_balance": 0.001,
        "min_quote_balance": 5.0,
        "allow_short_selling": false
    },
    "risk": {
        "risk_aversion": 0.1,
        "market_impact": 0.01,
        "time_horizon": 1.0,
        "use_avellaneda": true
    }
}
```

### 3. Get API Keys

**For Testing (Recommended):**
- Visit [Binance Testnet](https://testnet.binance.vision/)
- Create account and generate API keys
- Set `"testnet": true` in config

**For Live Trading:**
- Visit [Binance API Management](https://www.binance.com/en/my/settings/api-management)
- Create API keys with spot trading permissions
- Set `"testnet": false` in config

### 4. Run the Bot

```bash
python binance_market_maker_bot_3.py
```

## Configuration Guide

### Exchange Settings

| Parameter | Description | Default |
|-----------|-------------|---------|
| `api_key` | Binance API key | Required |
| `api_secret` | Binance API secret | Required |
| `testnet` | Use testnet (true) or live trading (false) | `true` |
| `symbol` | Trading pair | `"BTC/USDT"` |
| `base_asset` | Base asset symbol | `"BTC"` |
| `quote_asset` | Quote asset symbol | `"USDT"` |

### Trading Parameters

| Parameter | Description | Default | Range |
|-----------|-------------|---------|-------|
| `total_capital` | Total trading capital in USD | `1000.0` | > 0 |
| `max_inventory_ratio` | Max position as % of capital | `0.2` | 0.05-0.5 |
| `max_drawdown_ratio` | Max loss as % of capital | `0.03` | 0.01-0.1 |
| `base_spread_ticks` | Minimum spread in ticks | `3.0` | 1-10 |
| `volatility_multiplier` | Volatility impact on spread | `4.0` | 1-10 |
| `inventory_skew_strength` | Position skewing strength | `3.0` | 0-10 |
| `min_order_lifetime` | Minimum order duration (seconds) | `10.0` | 5-60 |
| `min_base_balance` | Minimum base asset balance | `0.001` | > 0 |
| `min_quote_balance` | Minimum quote asset balance | `5.0` | > 0 |
| `allow_short_selling` | Allow negative positions | `false` | boolean |

### Avellaneda-Stoikov Parameters

| Parameter | Description | Default | Range |
|-----------|-------------|---------|-------|
| `risk_aversion` | Risk aversion coefficient (γ) | `0.1` | 0.01-1.0 |
| `market_impact` | Market impact coefficient (k) | `0.01` | 0.001-0.1 |
| `time_horizon` | Time horizon (T) | `1.0` | 0.1-10.0 |
| `use_avellaneda` | Use Avellaneda model vs simple volatility | `true` | boolean |

## Strategy Overview

### Avellaneda-Stoikov Model

The bot implements the Avellaneda-Stoikov market making model, which calculates optimal bid/ask prices based on:

1. **Reservation Price**: Optimal mid-price considering inventory position
2. **Optimal Spread**: Based on volatility, risk aversion, and time horizon
3. **Inventory Skew**: Adjusts quotes to manage inventory risk
4. **Market Imbalance**: Responds to order book imbalance

### Risk Management

The bot includes multiple layers of risk protection:

- **Inventory Limits**: Maximum position size as percentage of capital
- **Drawdown Protection**: Automatic kill switch on maximum loss
- **Balance Monitoring**: Prevents orders exceeding available balance
- **Real-time P&L**: Continuous monitoring of realized and unrealized P&L

## Monitoring and Metrics

The bot displays real-time metrics including:

```
================================================================================
Binance Market Maker Bot (CCXT) - 14:23:15
================================================================================
Symbol:   BTC/USDT | Regime:  range | Spread: 2.45
Bid:  91234.56 | Mid:  91236.78 | Ask:  91239.01
Position:   0.001234 BTC | Notional: $112.50
Balance:   0.012345 BTC | $  1,234.56 USDT
Realized P&L: $    12.34 | Unrealized P&L: $    -5.67
Total P&L: $     6.67 (+2.03%) | Max DD: $     8.90
Volatility:   1.25% | Imbalance:  +0.12 | Open Orders: 2
Current Quotes:
  Bid:  91234.56 @ 0.001100 ✅
  Ask:  91239.01 @ 0.001050 ✅
Trades: 42 | Volume: 0.045600 | VWAP Bid: 91145.23 | VWAP Ask: 91256.78
```

### Key Metrics

- **Realized P&L**: Profit/loss from completed trades
- **Unrealized P&L**: Mark-to-market P&L of current position
- **Fill Ratio**: Balance between bid and ask fills
- **Volume**: Total traded volume
- **VWAP**: Volume-weighted average prices

## Safety Features

### Testnet First
Always test on Binance Testnet before live trading:
```json
{
    "exchange": {
        "testnet": true
    }
}
```

### Position Limits
Configure maximum inventory exposure:
```json
{
    "trading": {
        "max_inventory_ratio": 0.2,  // 20% of capital
        "max_drawdown_ratio": 0.03   // 3% maximum loss
    }
}
```

### Kill Switch
The bot automatically stops trading if:
- Maximum drawdown is exceeded
- Critical errors occur
- Position limits are breached

## Advanced Configuration

### Strategy Selection

Choose between Avellaneda-Stoikov and simple volatility-based strategies:

```json
{
    "risk": {
        "use_avellaneda": true,      // Advanced model
        "risk_aversion": 0.1,        // Lower = more aggressive
        "market_impact": 0.01,       // Market impact parameter
        "time_horizon": 1.0          // Strategy time horizon
    }
}
```

### Market Regime Detection

The bot automatically detects market conditions:
- **Range**: Sideways market, wider spreads
- **Trend**: Directional market, tighter spreads with trend skew

## Troubleshooting

### Common Issues

**Orders not placing:**
- Check minimum balance requirements
- Verify tick size and lot size compliance
- Ensure sufficient balance for fees

**WebSocket connection issues:**
- Bot automatically falls back to REST API
- Install `ccxt[pro]` for WebSocket support
- Check network connectivity

**P&L calculation discrepancies:**
- Bot tracks only trades made during current session
- Historical trades don't affect realized P&L calculation
- Position is sourced from exchange balance

### Debug Mode

Enable detailed logging by modifying the logging level:

```python
logging.basicConfig(level=logging.DEBUG)
```

## Performance Optimization

### For High-Frequency Trading

1. Install CCXT Pro for WebSocket support
2. Reduce `min_order_lifetime` to 5-10 seconds
3. Increase `max_order_replace_freq` to 5-15 seconds
4. Use smaller `base_spread_ticks` (1-2 ticks)

### For Conservative Trading

1. Increase `max_drawdown_ratio` protection
2. Lower `max_inventory_ratio`
3. Increase `base_spread_ticks` for wider spreads
4. Set higher `min_quote_balance` buffers

## API Rate Limits

The bot respects Binance API rate limits:
- Uses CCXT built-in rate limiting
- Implements exponential backoff on errors
- Falls back to REST when WebSocket unavailable

## Risk Disclaimer

**Important Warning**: This software is for educational and research purposes. Cryptocurrency trading involves substantial risk of loss. 

- **Test thoroughly** on testnet before any live trading
- **Start with small amounts** you can afford to lose completely  
- **Monitor continuously** during operation
- **Understand the strategy** before deploying capital
- **Market making can lose money** in volatile or trending markets

The authors are not responsible for any financial losses incurred through use of this software.

## License

MIT License - See LICENSE file for details.

## Contributing

Contributions welcome! Please:
1. Test changes thoroughly on testnet
2. Include unit tests for new features
3. Update documentation for configuration changes
4. Follow existing code style and error handling patterns

## Support

For issues and questions:
- Review the troubleshooting section above
- Check Binance API documentation for exchange-specific issues
- Ensure your API keys have correct permissions
- Verify sufficient balance and proper configuration

**Note**: This bot requires active monitoring and is not designed for unsupervised operation.
