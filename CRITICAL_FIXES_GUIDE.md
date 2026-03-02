# AlgoForge - Critical Bug Fixes Guide

## URGENT FIXES (Do These First)

---

## FIX #1: Expose Credentials in config.py ⚠️ SECURITY CRITICAL

### Current Problem
```python
# config.py - EXPOSED! Anyone can see your API keys
DHAN_CLIENT_ID    = "16065293"
DHAN_ACCESS_TOKEN = "eyJ0eXAi..."  # JWT token visible
```

### Solution: Use Environment Variables

**Step 1:** Create `.env` file
```bash
# .env (ADD TO .gitignore!)
DHAN_CLIENT_ID=16065293
DHAN_ACCESS_TOKEN=eyJ0eXAi...
DEBUG=false
```

**Step 2:** Update config.py
```python
import os
from dotenv import load_dotenv

load_dotenv()

DHAN_CLIENT_ID    = os.getenv('DHAN_CLIENT_ID', 'YOUR_CLIENT_ID_HERE')
DHAN_ACCESS_TOKEN = os.getenv('DHAN_ACCESS_TOKEN', 'YOUR_TOKEN_HERE')
```

**Step 3:** Install python-dotenv
```bash
pip install python-dotenv
```

---

## FIX #2: Condition Counter Corruption ❌ LOGIC BREAK

### Current Problem
```javascript
let conditionCounters = { entry: 0, exit: 0 };

function addConditionRow(type) {
  const rowId = conditionCounters[type]++;  // Increments globally
  // ...
}

function removeConditionRow(type, id) {
  const r = document.getElementById(`${type}-row-${id}`);
  if(r) r.remove();
  // ❌ Counter NOT decreased!
}
```

**Scenario:**
1. User adds 3 entry conditions → IDs: 0, 1, 2 (counter = 3)
2. User deletes all 3 rows
3. User adds 1 more condition → Gets ID 3 (not 0!)
4. UI looks like 4th condition, but it's actually 1st

### Solution
```javascript
function clearAllConditions(type) {
  const container = document.getElementById(`${type}-conditions-container`);
  container.innerHTML = '';
  conditionCounters[type] = 0;  // ← RESET COUNTER
}

function removeConditionRow(type, id) {
  const r = document.getElementById(`${type}-row-${id}`);
  if(r) r.remove();
  
  // ← Count remaining and reset if none left
  const container = document.getElementById(`${type}-conditions-container`);
  if (container.children.length === 0) {
    conditionCounters[type] = 0;
  }
}

// Update the button that adds first condition
function addConditionRow(type) {
  const container = document.getElementById(`${type}-conditions-container`);
  
  // If no conditions exist, reset counter
  if (container.children.length === 0) {
    conditionCounters[type] = 0;
  }
  
  const rowId = conditionCounters[type]++;
  // ... rest of function
}
```

---

## FIX #3: Time_Of_Day & Day_Of_Week Conditions (Frontend-Backend Mismatch) ❌

### Current Problem
- Frontend has complete UI for time/day conditions
- Backend `engine/backtest.py` doesn't support them
- User creates condition → Silently ignored during backtest

### Backend Fix: Add to engine/backtest.py

```python
# In engine/backtest.py, find the condition evaluation function
# Add these helper functions:

def check_time_condition(df, left, operator, right_value):
    """Check Time_Of_Day condition"""
    if not hasattr(df.index, 'time'):
        return pd.Series([False] * len(df))
    
    from datetime import datetime, time as dt_time
    
    # Parse right_value (e.g., "11:00")
    target_time = datetime.strptime(right_value, "%H:%M").time()
    
    times = df.index.time
    
    if operator == "is_above":
        return pd.Series(times >= target_time)
    elif operator == "is_below":
        return pd.Series(times < target_time)
    elif operator == ">=":
        return pd.Series(times >= target_time)
    elif operator == "<=":
        return pd.Series(times <= target_time)
    else:
        return pd.Series([False] * len(df))

def check_day_condition(df, operator, right_days):
    """Check Day_Of_Week condition"""
    if not hasattr(df.index, 'dayofweek'):
        return pd.Series([False] * len(df))
    
    day_map = {
        'Monday': 0, 'Tuesday': 1, 'Wednesday': 2,
        'Thursday': 3, 'Friday': 4, 'Saturday': 5, 'Sunday': 6
    }
    
    target_days = [day_map.get(d, -1) for d in right_days]
    current_days = df.index.dayofweek
    
    if operator == "contains":
        return pd.Series([d in target_days for d in current_days])
    elif operator == "not_contains":
        return pd.Series([d not in target_days for d in current_days])
    else:
        return pd.Series([False] * len(df))

# Then in your condition evaluation loop, add:
if condition['left'] == 'Time_Of_Day':
    mask = check_time_condition(df, ..., condition['operator'], condition['right_time'])
elif condition['left'] == 'Day_Of_Week':
    mask = check_day_condition(df, condition['operator'], condition['right_days'])
```

---

## FIX #4: Global State Variables → Use Closures ⚠️ RACE CONDITION

### Current Problem
```javascript
// ❌ BAD: Global variables
let movingStrategyId = null;
let paperState = { running: false, ... };
let lastBacktestData = null;

// If two users access same page, they interfere!
```

### Solution: Use Module Pattern
```javascript
// ✅ GOOD: Encapsulated state
const StrategyManager = (() => {
  let movingStrategyId = null;
  let cache = [];
  
  return {
    setMovingId: (id) => {
      movingStrategyId = id;
    },
    getMovingId: () => {
      return movingStrategyId;
    },
    setCache: (strategies) => {
      cache = [...strategies];  // Immutable copy
    },
    getCache: () => {
      return [...cache];
    }
  };
})();

const PaperTrader = (() => {
  let state = {
    running: false,
    tick: 0,
    pnl: 0,
    activeLegs: []
  };
  
  return {
    start: (payload) => {
      if (state.running) return; // Prevent double-start
      state.running = true;
      // ...
    },
    stop: () => {
      state.running = false;
    },
    getState: () => {
      return { ...state };  // Return copy, not reference
    }
  };
})();

// Usage:
StrategyManager.setMovingId(42);
const id = StrategyManager.getMovingId();

PaperTrader.start(payload);
const { pnl } = PaperTrader.getState();
```

---

## FIX #5: Form Validation Before API Call ❌

### Current Problem
```javascript
// ❌ BAD: No validation
async function runBacktest() {
  const payload = buildPayload();
  const res = await fetch('/api/backtest', {
    method: 'POST',
    body: JSON.stringify(payload)
  });
  // If payload is malformed, backend might 500
}
```

### Solution: Validate First
```javascript
// ✅ GOOD: Comprehensive validation
class PayloadValidator {
  static validate(payload) {
    const errors = [];
    
    // Required fields
    if (!payload.instrument) errors.push('Instrument is required');
    if (!payload.entry_conditions?.length) errors.push('Add at least one entry condition');
    if (!payload.from_date) errors.push('From date is required');
    if (!payload.to_date) errors.push('To date is required');
    
    // Date validation
    const from = new Date(payload.from_date);
    const to = new Date(payload.to_date);
    if (from >= to) errors.push('From date must be before To date');
    if (to > new Date()) errors.push('To date cannot be in future');
    
    // Number validation
    if (payload.stoploss_pct <= 0) errors.push('SL % must be > 0');
    if (payload.stoploss_pct > 100) errors.push('SL % cannot exceed 100');
    
    // Leg validation
    if (payload.legs?.length === 0) errors.push('Add at least one leg');
    
    payload.legs?.forEach((leg, i) => {
      if (!leg.transaction_type) errors.push(`Leg ${i+1}: Missing transaction type`);
      if (!leg.option_type) errors.push(`Leg ${i+1}: Missing option type`);
      if (leg.lots <= 0) errors.push(`Leg ${i+1}: Lots must be > 0`);
    });
    
    if (errors.length > 0) {
      throw new Error('Validation failed:\n' + errors.join('\n'));
    }
    
    return true;
  }
}

// Usage:
async function runBacktest() {
  const payload = buildPayload();
  
  try {
    PayloadValidator.validate(payload);
  } catch (err) {
    toast(err.message, 'danger');
    return;
  }
  
  try {
    const res = await fetch('/api/backtest', {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    
    if (!res.ok) {
      throw new Error(`API returned ${res.status}: ${res.statusText}`);
    }
    
    const data = await res.json();
    if (data.status !== 'success') {
      throw new Error(data.message || 'Backtest failed');
    }
    
    renderResults(data, payload);
    toast('✅ Backtest complete!', 'success');
  } catch (err) {
    toast(`❌ Error: ${err.message}`, 'danger');
    console.error(err);
  }
}
```

---

## FIX #6: Broker Toggle is Dummy - Add Real Validation ⚠️

### Current Problem
```javascript
// ❌ 100% DUMMY - no backend validation
function toggleBroker() {
  isBrokerConnected = !isBrokerConnected;
  document.getElementById('broker-dot').style.backgroundColor = 
    isBrokerConnected ? '#22c55e' : 'grey';
}
```

### Solution: Add Real Connection Check
```python
# In app.py, add endpoint
@app.post("/api/broker/check")
async def check_broker():
    """Verify broker connection is valid"""
    try:
        if not dhan._is_configured():
            return {"status": "not_configured"}
        
        # Try to get positions - this will fail if credentials are bad
        positions = dhan.get_positions()
        return {
            "status": "connected",
            "broker": "Dhan",
            "message": "Connection successful"
        }
    except Exception as e:
        return {
            "status": "error",
            "broker": "Dhan",
            "message": str(e)
        }

@app.post("/api/broker/connect")
async def connect_broker():
    """Establish broker connection (validate credentials)"""
    try:
        # Re-initialize Dhan client with config
        dhan_test = DhanClient()
        
        # Verify by fetching health info
        result = dhan_test.get_funds()
        if result and "status" in result:
            return {"status": "connected", "message": "Broker connected successfully"}
        else:
            return {"status": "error", "message": "Invalid response from broker"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
```

```javascript
// In strategy.html
let isBrokerConnected = false;
let brokerCheckInterval = null;

async function toggleBroker() {
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = '⏳ Checking...';
  
  try {
    const res = await fetch('/api/broker/connect',{ method: 'POST' });
    const data = await res.json();
    
    if (data.status === 'connected') {
      isBrokerConnected = true;
      document.getElementById('broker-dot').style.backgroundColor = '#22c55e';
      document.getElementById('broker-text').textContent = 'Live Broker: Connected';
      toast('✅ Broker Connected!', 'success');
      
      // Optional: Start periodic health check
      startBrokerHealthCheck();
    } else {
      isBrokerConnected = false;
      document.getElementById('broker-dot').style.backgroundColor = 'red';
      document.getElementById('broker-text').textContent = 'Live Broker: Connection Failed';
      toast('❌ ' + (data.message || 'Connection failed'), 'danger');
    }
  } catch (err) {
    isBrokerConnected = false;
    document.getElementById('broker-dot').style.backgroundColor = 'red';
    toast('❌ Network error: ' + err.message, 'danger');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Toggle Connection';
  }
}

function startBrokerHealthCheck() {
  if (brokerCheckInterval) return; // Already running
  
  brokerCheckInterval = setInterval(async () => {
    try {
      const res = await fetch('/api/broker/check', { method: 'POST' });
      const data = await res.json();
      
      if (data.status !== 'connected') {
        // Connection lost
        isBrokerConnected = false;
        document.getElementById('broker-dot').style.backgroundColor = 'orange';
        toast('⚠️ Broker connection lost', 'warn');
        stopBrokerHealthCheck();
      }
    } catch (err) {
      // Assume connection lost
      isBrokerConnected = false;
      document.getElementById('broker-dot').style.backgroundColor = 'grey';
      stopBrokerHealthCheck();
    }
  }, 30000); // Check every 30 seconds
}

function stopBrokerHealthCheck() {
  if (brokerCheckInterval) {
    clearInterval(brokerCheckInterval);
    brokerCheckInterval = null;
  }
}
```

---

## FIX #7: Better Error Handling on All API Calls ❌

### Current Problem
```javascript
// ❌ No error handling
async function saveStrategy() {
  const res = await fetch('/api/strategies', { method: 'POST', ... });
  if (res.ok) { toast('Saved!', 'success'); }
}
```

### Solution: Proper Error Handling
```javascript
// ✅ GOOD: Comprehensive error handling
class APIClient {
  static async post(endpoint, data) {
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
        timeout: 30000  // 30 second timeout
      });
      
      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(
          errorData.detail || 
          `Server error ${res.status}: ${res.statusText}`
        );
      }
      
      const body = await res.json();
      if (body.error) {
        throw new Error(body.error);
      }
      
      return body;
      
    } catch (err) {
      if (err instanceof TypeError) {
        throw new Error('Network error: ' + err.message);
      }
      throw err;
    }
  }
  
  static async get(endpoint) {
    return this.post(endpoint + '?_method=GET', {});
  }
}

// Usage:
async function saveStrategy() {
  const payload = buildPayload();
  
  try {
    PayloadValidator.validate(payload);
  } catch (err) {
    toast('Validation error: ' + err.message, 'danger');
    return;
  }
  
  try {
    const result = await APIClient.post('/api/strategies', payload);
    toast('✅ Strategy saved!', 'success');
    fetchStrategies();
  } catch (err) {
    toast('❌ Save failed: ' + err.message, 'danger');
    console.error('Save error:', err);
  }
}
```

---

## FIX #8: Lot Size Display Issue ⚠️

### Current Problem
```javascript
// Frontend sends lot_size: 0 (means "auto-detect")
function buildPayload() {
  return {
    lot_size: 0,  // ← Means "auto"
    // ...
  };
}

// Backend auto-detects but never tells frontend
// Frontend can't display actual value used
```

### Solution: Return Lot Size from Backtest
```python
# In app.py, add to backtest response
@app.post("/api/backtest")
async def api_run_backtest(payload: StrategyPayload):
    # ... existing code ...
    
    results = run_backtest(
        df_raw=df_raw,
        entry_conditions=...,
        exit_conditions=...,
        strategy_config=strategy_config,
    )
    
    # ← Add actual lot size to response
    if results.get("status") == "success":
        # Detect lot size used
        from_dt = datetime.strptime(payload.from_date, "%Y-%m-%d")
        lot_size = 75 if from_dt.year <= 2026 and from_dt.month < 1 else 65
        
        results["actual_lot_size"] = lot_size
        results["actual_quantity_per_lot"] = lot_size * (payload.lots or 1)
    
    return results
```

```javascript
// In frontend, display actual lot size
function renderResults(data, payload) {
  // ... existing code ...
  
  // Show actual lot size if available
  const lotSizeDisplay = document.querySelector('[data-actual-lot-size]');
  if (lotSizeDisplay && data.actual_lot_size) {
    lotSizeDisplay.textContent = `Lot Size: ${data.actual_lot_size} (${data.actual_quantity_per_lot} contracts)`;
  }
}
```

---

## FIX #9: Form State Persistence (Lost on Refresh) ⚠️

### Current Problem
- User builds complex strategy
- Page refreshes (accidental or network issue)
- All unsaved work is LOST

### Solution: Auto-save to LocalStorage
```javascript
class DraftManager {
  static STORAGE_KEY = 'strategy_draft';
  
  static save(payload) {
    try {
      localStorage.setItem(
        this.STORAGE_KEY,
        JSON.stringify({
          payload,
          timestamp: new Date().toISOString()
        })
      );
      console.log('Draft auto-saved');
    } catch (err) {
      console.warn('Failed to save draft:', err);
    }
  }
  
  static load() {
    try {
      const data = localStorage.getItem(this.STORAGE_KEY);
      if (!data) return null;
      
      const draft = JSON.parse(data);
      const ageMs = new Date() - new Date(draft.timestamp);
      
      // Don't restore draft older than 7 days
      if (ageMs > 7 * 24 * 60 * 60 * 1000) {
        this.clear();
        return null;
      }
      
      return draft.payload;
    } catch (err) {
      console.warn('Failed to load draft:', err);
      return null;
    }
  }
  
  static clear() {
    localStorage.removeItem(this.STORAGE_KEY);
  }
}

// On page load:
document.addEventListener('DOMContentLoaded', () => {
  const draft = DraftManager.load();
  if (draft) {
    const restore = confirm(
      'Draft found from ' + new Date(draft.timestamp).toLocaleString() + 
      '. Restore it?'
    );
    if (restore) {
      restorePayloadToForm(draft);
      toast('✅ Draft restored', 'success');
    } else {
      DraftManager.clear();
    }
  }
});

// Auto-save every 30 seconds:
setInterval(() => {
  const payload = buildPayload();
  DraftManager.save(payload);
}, 30000);
```

---

## FIX #10: Deploy Modal State Instability ⚠️

### Current Problem
```javascript
// ❌ Dataset attribute approach is fragile
function setDeployType(type) {
  paperBtn.dataset.active = type === 'paper' ? '1' : '0';
  // If modal reopened, state might be wrong
}
```

### Solution: Use Proper State Object
```javascript
const DeployModal = (() => {
  let state = {
    deployType: 'paper',  // or 'auto'
    productType: 'MIS',   // or 'NRML'
    entryOrder: 'MARKET',
    exitOrder: 'MARKET',
    // ...all form fields
  };
  
  return {
    setState: (updates) => {
      state = { ...state, ...updates };
      renderModal();
    },
    
    getState: () => ({ ...state }),
    
    reset: () => {
      state = {
        deployType: 'paper',
        productType: 'MIS',
        // ... reset to defaults
      };
    },
    
    renderModal: () => {
      // Re-render UI based on state
      // This ensures state and UI always match
    },
    
    open: (runName) => {
      this.reset();
      // ... open implementation
    }
  };
})();

// Usage:
function openDeployModal() {
  DeployModal.open(document.getElementById('run-name-input').value);
}

function setDeployType(type) {
  DeployModal.setState({ deployType: type });
}

function deployStrategy() {
  const state = DeployModal.getState();
  // Now we have reliable state
  const isPaper = state.deployType === 'paper';
  // ...
}
```

---

## Summary of Fixes

| Fix # | Issue | Priority | Effort | Impact |
|-------|-------|----------|--------|--------|
| 1 | Expose credentials | 🔴 CRITICAL | 5min | Security |
| 2 | Condition counter | 🔴 CRITICAL | 15min | Data integrity |
| 3 | Time/Day backend | 🔴 CRITICAL | 1-2hr | Core feature |
| 4 | Global state | 🟠 HIGH | 1-2hr | Race conditions |
| 5 | Form validation | 🟠 HIGH | 1hr | User experience |
| 6 | Broker toggle | 🟠 HIGH | 30min | Functionality |
| 7 | Error handling | 🟠 HIGH | 1-2hr | Reliability |
| 8 | Lot size display | 🟡 MEDIUM | 30min | UX |
| 9 | State persistence | 🟡 MEDIUM | 1hr | UX |
| 10 | Modal state | 🟡 MEDIUM | 30min | Stability |

**Total effort:** ~8-10 hours for all critical+high priority fixes
