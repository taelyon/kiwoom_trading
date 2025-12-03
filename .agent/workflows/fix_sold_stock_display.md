---
description: Fix for sold stocks reappearing in UI
---

# Fix: Sold Stocks Reappearing in UI

## Problem
Stocks that were fully sold were reappearing in the "Holdings" list box and "Investment Status" table. This was caused by:
1.  `KiwoomTrader` failing to remove the stock from `boughtBox` due to strict string matching (e.g., whitespace issues).
2.  `AccountManager` re-adding the stock during periodic REST API balance checks because the API data update was slightly delayed, showing the stock as still held.

## Solution

### 1. Robust Removal from `boughtBox`
In `KiwoomTrader.place_sell_order`, the removal logic was updated to check if the stock code is *contained* in the list item text, rather than an exact match.

```python
if code in self.parent.boughtBox.item(i).text():
    self.parent.boughtBox.takeItem(i)
```

### 2. `sold_blacklist` Mechanism
A `sold_blacklist` dictionary was added to `KiwoomTrader` to track stocks that have been fully sold within the last 10 seconds.

```python
self.sold_blacklist[code] = datetime.now()
```

### 3. Preventing Re-addition in `AccountManager`
In `AccountManager._initialize_balance_data_from_rest_api`, a check was added to skip processing any stock that is currently in the `sold_blacklist`.

```python
if hasattr(parent, 'trader') and parent.trader and parent.trader.is_recently_sold(stock_code):
    self.logger.debug(f"🚫 {stock_code} 최근 매도된 종목이므로 REST API 잔고 반영 건너뜀")
    continue
```

### 4. `pending_sell_orders` Cleanup
The `pending_sell_orders` set is now properly cleared upon successful order placement to prevent state lock-ups where a stock is permanently marked as "selling".

## Verification
- Perform a full sell of a stock.
- Verify that the stock is immediately removed from the "Holdings" list.
- Verify that the stock does not reappear in the "Holdings" list or "Investment Status" table during subsequent balance updates.
