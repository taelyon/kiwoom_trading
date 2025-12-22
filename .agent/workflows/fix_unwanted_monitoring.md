---
description: Fix for unwanted monitoring of removed stocks (Zombie Data)
---

# Fix Unwanted Monitoring of Removed Stocks

This workflow addresses the issue where a stock continues to be monitored or re-appears in the monitoring list after being explicitly removed. This typically happens due to "zombie" data tasks completing after the removal or incomplete cleanup of internal queues.

## Problem Description
- A stock is removed from `MonitoringManager` and `ChartDataCache.cache`.
- However, an asynchronous data collection task for that stock might still be running or queued (`ChartDataCache.api_request_queue`).
- When this task completes, `_on_chart_data_ready` receives the data.
- Previously, `_on_chart_data_ready` would see the stock is missing from the cache and re-initialize it, effectively "resurrecting" the stock.
- Also, `remove_monitoring_stock` only removed the stock from the cache but left it in queues and pending lists.

## Solution Implemented

### 1. Robust Cleanup in `ChartDataCache`
- **`remove_monitoring_stock`**: Now calls `self.remove_stock(code)` internally. This ensures a unified and complete cleanup process.
- **`remove_stock`**: Updated to explicitly remove the stock from `self.pending_stocks` in addition to `self.cache`, `self.api_request_queue`, and `self.active_chart_tasks`.

### 2. Zombie Data Guard in `_on_chart_data_ready`
- Added a guard clause at the beginning of `_on_chart_data_ready`.
- **Logic**: If a stock is NOT in `self.cache` AND NOT in `self.pending_stocks`, it is considered "removed" or "unwanted".
- The function now logs a warning ("🚫 {code}: 제거된 종목의 데이터 수신됨") and returns immediately, discarding the data and preventing re-initialization.

## Verification
To verify the fix:
1.  Start the application and add a stock to monitoring (e.g., via condition search or manual add).
2.  Wait for it to be active (chart data collected).
3.  Remove the stock from the monitoring list (or trigger a condition exit).
4.  Observe the logs. You should see:
    - `MonitoringManager`: ✅ 모니터링 종목 제거
    - `ChartDataCache`: 🗑️ ChartDataCache: {code} 캐시 데이터 제거됨
    - `ChartDataCache`: 🗑️ ChartDataCache: {code} 대기 목록(pending_stocks)에서 제거됨
    - If a late task arrives: `🚫 {code}: 제거된 종목의 데이터 수신됨 - 캐시 저장 및 UI 추가 건너뜁니다.`
5.  Ensure the stock does not reappear in the `AutoTrader` monitoring logs.
