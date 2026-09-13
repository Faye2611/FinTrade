import streamlit as st
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
import sqlite3
import plotly.graph_objects as go
import threading
import time
import os

# ====================== DATABASE SETUP ======================
DB_PATH = 'fintrade.db'

def get_db_connection():
    """Returns a short-lived connection configured with WAL mode & busy timeout."""
    conn = sqlite3.connect(DB_PATH, timeout=60, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout = 60000;")
    except sqlite3.OperationalError:
        pass
    return conn

@st.cache_resource
def init_db():
    """Initializes and migrates database schema safely."""
    with get_db_connection() as conn:
        c = conn.cursor()
        
        # Portfolio table
        c.execute('''CREATE TABLE IF NOT EXISTS portfolio
                     (symbol TEXT PRIMARY KEY, quantity REAL, avg_price REAL)''')

        # Trade History table
        c.execute('''CREATE TABLE IF NOT EXISTS trade_history
                     (timestamp TEXT, symbol TEXT, action TEXT, order_type TEXT,
                      quantity REAL, price REAL, total REAL, execution_status TEXT)''')

        # Migrate trade_history schema if created under older versions
        c.execute("PRAGMA table_info(trade_history)")
        columns = [col[1] for col in c.fetchall()]
        if 'order_type' not in columns:
            c.execute("ALTER TABLE trade_history ADD COLUMN order_type TEXT DEFAULT 'Market'")
        if 'execution_status' not in columns:
            c.execute("ALTER TABLE trade_history ADD COLUMN execution_status TEXT DEFAULT 'EXECUTED'")

        # Pending Orders table
        c.execute('''CREATE TABLE IF NOT EXISTS pending_orders
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, symbol TEXT, 
                      action TEXT, order_type TEXT, quantity REAL, target_price REAL, 
                      parent_trade_price REAL)''')

        # Cash table
        c.execute('''CREATE TABLE IF NOT EXISTS cash (id INTEGER PRIMARY KEY, amount REAL)''')
        c.execute("INSERT OR IGNORE INTO cash (id, amount) VALUES (1, 100000.0)")
        
        conn.commit()
    return True

# Initialize database once
init_db()

# ====================== CONSTANTS & CONFIG ======================
COMMISSION_FLAT = 4.95        # Flat fee per transaction
SPREAD_BPS = 0.0010           # 10 basis points (0.10%) spread impact

# ====================== HELPER FUNCTIONS ======================
def get_stock_data(symbol):
    """Fetches real-time price info and market schedule metadata."""
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="1d")
        if not data.empty:
            current_price = round(data['Close'].iloc[-1], 4)
            info = ticker.info
            is_market_open = info.get('marketState', 'REGULAR') == 'REGULAR'
            return current_price, is_market_open
        return None, False
    except Exception:
        return None, False

def get_current_price(symbol):
    price, _ = get_stock_data(symbol)
    return price

def get_cash():
    """Reads cash balance using a short-lived connection."""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT amount FROM cash WHERE id=1")
        result = c.fetchone()
        return result[0] if result else 100000.0

def update_cash(new_amount):
    """Updates cash balance safely inside a transaction context."""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE cash SET amount = ? WHERE id=1", (new_amount,))
        conn.commit()

def get_portfolio():
    """Fetches portfolio dataframe safely."""
    with get_db_connection() as conn:
        return pd.read_sql_query("SELECT * FROM portfolio", conn)

def get_trade_history():
    """Fetches trade history dataframe safely."""
    with get_db_connection() as conn:
        return pd.read_sql_query("SELECT * FROM trade_history ORDER BY timestamp DESC", conn)

def get_pending_orders():
    """Fetches active pending orders safely."""
    with get_db_connection() as conn:
        return pd.read_sql_query("SELECT * FROM pending_orders ORDER BY timestamp DESC", conn)

# Updated calculate_portfolio_value function
def calculate_portfolio_value():
    portfolio = get_portfolio()
    total_value = 0
    details = []
    
    if portfolio.empty:
        return total_value, details

    symbols_list = portfolio['symbol'].tolist()
    
    # Download data
    try:
        # Ticker-grouped data
        batch_data = yf.download(symbols_list, period="1d", progress=False)
    except Exception:
        batch_data = None

    for _, row in portfolio.iterrows():
        sym = row['symbol']
        price = None
        
        if batch_data is not None and not batch_data.empty:
            try:
                # Handle yfinance multi-index columns robustly
                if 'Close' in batch_data.columns:
                    close_df = batch_data['Close']
                    if isinstance(close_df, pd.DataFrame) and sym in close_df.columns:
                        price = close_df[sym].dropna().iloc[-1]
                    elif isinstance(close_df, pd.Series):
                        price = close_df.dropna().iloc[-1]
                
                if price is None:
                    price = get_current_price(sym)
            except Exception:
                price = get_current_price(sym)
        else:
            price = get_current_price(sym)
            
        if price is None or pd.isna(price):
            price = row['avg_price']
            
        price = round(float(price), 4)
        value = row['quantity'] * price
        total_value += value
        
        details.append({
            'symbol': sym,
            'quantity': row['quantity'],
            'avg_price': row['avg_price'],
            'current_price': price,
            'value': round(value, 2),
            'unrealized_pnl': round(value - (row['quantity'] * row['avg_price']), 2)
        })
        
    return total_value, details

def get_historical_analysis(symbol, timeframe_months=12):
    try:
        end_date = datetime.today()
        start_date = end_date - timedelta(days=timeframe_months * 30 + 300)

        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start_date, end=end_date)

        if df.empty:
            return None

        # ====================== MOVING AVERAGES ======================
        df['SMA_50'] = df['Close'].rolling(window=50).mean()
        df['SMA_200'] = df['Close'].rolling(window=200).mean()

        # ====================== RSI (14 DAYS) ======================
        delta = df['Close'].diff()

        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()

        rs = avg_gain / avg_loss
        df['RSI_14'] = 100 - (100 / (1 + rs))

        # ====================== 3-MONTH MOMENTUM ======================
        df['Momentum_3M'] = df['Close'].pct_change(periods=63) * 100

        cutoff_date = datetime.today() - timedelta(days=timeframe_months * 30)
        df = df.loc[
            df.index >= pd.to_datetime(cutoff_date).tz_localize(df.index.tz)
        ]

        return df

    except Exception:
        return None

def generate_decision_signal(hist_data):
    """Generates a simple explainable technical trading signal."""

    if hist_data is None or hist_data.empty:
        return None

    latest = hist_data.iloc[-1]

    price = latest['Close']
    sma50 = latest['SMA_50']
    sma200 = latest['SMA_200']
    rsi = latest['RSI_14']
    momentum = latest['Momentum_3M']

    score = 0
    reasons = []

    # ====================== PRICE VS SMA50 ======================
    if price > sma50:
        score += 1
        reasons.append(("🟢", "Price is above the 50-day SMA", "Bullish"))
    else:
        score -= 1
        reasons.append(("🔴", "Price is below the 50-day SMA", "Bearish"))

    # ====================== SMA50 VS SMA200 ======================
    if sma50 > sma200:
        score += 1
        reasons.append(("🟢", "50-day SMA is above the 200-day SMA", "Bullish"))
    else:
        score -= 1
        reasons.append(("🔴", "50-day SMA is below the 200-day SMA", "Bearish"))

    # ====================== RSI ======================
    if rsi < 30:
        score += 1
        reasons.append(("🟢", f"RSI is {rsi:.1f} — potentially oversold", "Bullish"))
    elif rsi > 70:
        score -= 1
        reasons.append(("🔴", f"RSI is {rsi:.1f} — potentially overbought", "Bearish"))
    else:
        reasons.append(("🟡", f"RSI is {rsi:.1f} — neutral range", "Neutral"))

    # ====================== MOMENTUM ======================
    if momentum > 5:
        score += 1
        reasons.append(("🟢", f"3-month momentum is +{momentum:.1f}%", "Bullish"))
    elif momentum < -5:
        score -= 1
        reasons.append(("🔴", f"3-month momentum is {momentum:.1f}%", "Bearish"))
    else:
        reasons.append(("🟡", f"3-month momentum is {momentum:.1f}%", "Neutral"))

    # ====================== FINAL SIGNAL ======================
    if score >= 2:
        signal = "BUY"
        explanation = "The technical indicators are generally positive."
    elif score <= -2:
        signal = "SELL"
        explanation = "The technical indicators are generally negative."
    else:
        signal = "HOLD"
        explanation = "The technical indicators are mixed, so there is no strong directional signal."

    return {
        "signal": signal,
        "score": score,
        "price": price,
        "rsi": rsi,
        "momentum": momentum,
        "reasons": reasons,
        "explanation": explanation
    }

# ====================== EXECUTION MECHANICS ENGINE ======================
def process_instant_trade(symbol, action, order_type, quantity, raw_price):
    """Applies slippage/spread & fees, then updates positions immediately."""
    cash = get_cash()
    
    if action == "Buy":
        execution_price = round(raw_price * (1 + SPREAD_BPS), 4)
    else:
        execution_price = round(raw_price * (1 - SPREAD_BPS), 4)
        
    total_cost = round(quantity * execution_price, 2)
    net_transaction_value = (total_cost + COMMISSION_FLAT) if action == "Buy" else (total_cost - COMMISSION_FLAT)
    
    portfolio_df = get_portfolio()
    existing_holding = portfolio_df[portfolio_df['symbol'] == symbol]
    current_owned = existing_holding['quantity'].values[0] if not existing_holding.empty else 0.0
    current_avg_price = existing_holding['avg_price'].values[0] if not existing_holding.empty else 0.0
    
    with get_db_connection() as conn:
        c = conn.cursor()
        if action == "Buy":
            if net_transaction_value > cash:
                return False, "Not enough cash to cover trade and commissions!"
            new_qty = current_owned + quantity
            new_avg_price = ((current_avg_price * current_owned) + (execution_price * quantity)) / new_qty
            
            c.execute("""INSERT INTO portfolio (symbol, quantity, avg_price) VALUES (?, ?, ?)
                         ON CONFLICT(symbol) DO UPDATE SET quantity = ?, avg_price = ?""", 
                      (symbol, new_qty, new_avg_price, new_qty, new_avg_price))
            
            c.execute("UPDATE cash SET amount = ? WHERE id=1", (cash - net_transaction_value,))
            
        elif action == "Sell":
            if quantity > current_owned:
                return False, f"Insufficient shares! You only hold {current_owned} shares."
            new_qty = current_owned - quantity
            if new_qty == 0:
                c.execute("DELETE FROM portfolio WHERE symbol = ?", (symbol,))
            else:
                c.execute("UPDATE portfolio SET quantity = ? WHERE symbol = ?", (new_qty, symbol))
            
            c.execute("UPDATE cash SET amount = ? WHERE id=1", (cash + net_transaction_value,))

        c.execute("""
            INSERT INTO trade_history (timestamp, symbol, action, order_type, quantity, price, total, execution_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().strftime("%Y-%m-%d %H:%M"), 
            symbol, 
            action, 
            order_type, 
            quantity, 
            execution_price, 
            total_cost, 
            'EXECUTED'
        ))
        conn.commit()

    return True, f"Executed {action} of {quantity} {symbol} at ${execution_price:,.2f} (Fee: ${COMMISSION_FLAT})"

# ====================== THREAD-SAFE BACKGROUND ENGINE ======================

def check_and_trigger_pending_orders_thread():
    """Continuously polls live market metrics in an independent background thread."""
    while True:
        try:
            pending_orders = []
            with get_db_connection() as conn:
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                c.execute("SELECT * FROM pending_orders")
                pending_orders = [dict(row) for row in c.fetchall()]
            
            if pending_orders:
                for order in pending_orders:
                    sym = order['symbol']
                    curr_price, _ = get_stock_data(sym)
                    
                    if not curr_price:
                        continue
                        
                    triggered = False
                    o_type = order['order_type']
                    target = order['target_price']
                    action = order['action']
                    qty = order['quantity']
                    
                    if o_type == "Limit":
                        if action == "Buy" and curr_price <= target: triggered = True
                        elif action == "Sell" and curr_price >= target: triggered = True
                    elif o_type == "Stop-Loss":
                        if action == "Sell" and curr_price <= target: triggered = True
                    elif o_type == "Take-Profit":
                        if action == "Sell" and curr_price >= target: triggered = True
                            
                    if triggered:
                        execute_trade_in_thread(sym, action, o_type, qty, curr_price, order['id'])
            
        except Exception as e:
            print(f"Background Engine Error: {e}")
            
        time.sleep(10)  # Polling interval increased to 10s to lower database access frequency

def execute_trade_in_thread(symbol, action, order_type, quantity, raw_price, order_id):
    """Thread-safe version of position updates and log handling."""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT amount FROM cash WHERE id=1")
        cash = c.fetchone()[0]
        
        execution_price = round(raw_price * (1 + SPREAD_BPS), 4) if action == "Buy" else round(raw_price * (1 - SPREAD_BPS), 4)
        total_cost = round(quantity * execution_price, 2)
        net_val = (total_cost + COMMISSION_FLAT) if action == "Buy" else (total_cost - COMMISSION_FLAT)
        
        c.execute("SELECT quantity, avg_price FROM portfolio WHERE symbol = ?", (symbol,))
        holding = c.fetchone()
        current_owned, current_avg_price = (holding[0], holding[1]) if holding else (0.0, 0.0)
        
        if action == "Buy" and net_val <= cash:
            new_qty = current_owned + quantity
            new_avg_price = ((current_avg_price * current_owned) + (execution_price * quantity)) / new_qty
            c.execute("""INSERT INTO portfolio (symbol, quantity, avg_price) VALUES (?, ?, ?)
                          ON CONFLICT(symbol) DO UPDATE SET quantity = ?, avg_price = ?""", 
                       (symbol, new_qty, new_avg_price, new_qty, new_avg_price))
            c.execute("UPDATE cash SET amount = ? WHERE id=1", (cash - net_val,))
        elif action == "Sell" and quantity <= current_owned:
            new_qty = current_owned - quantity
            if new_qty == 0:
                c.execute("DELETE FROM portfolio WHERE symbol = ?", (symbol,))
            else:
                c.execute("UPDATE portfolio SET quantity = ? WHERE symbol = ?", (new_qty, symbol))
            c.execute("UPDATE cash SET amount = ? WHERE id=1", (cash + net_val,))
            
        c.execute("""
            INSERT INTO trade_history (timestamp, symbol, action, order_type, quantity, price, total, execution_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().strftime("%Y-%m-%d %H:%M"), 
            symbol, 
            action, 
            order_type, 
            quantity, 
            execution_price, 
            total_cost, 
            'EXECUTED'
        ))
        
        c.execute("DELETE FROM pending_orders WHERE id = ?", (order_id,))
        conn.commit()

# ====================== START THE BACKGROUND ENGINE ======================
@st.cache_resource
def initialize_brokerage_engine():
    """Starts the engine thread precisely once when Streamlit initializes."""
    engine_thread = threading.Thread(target=check_and_trigger_pending_orders_thread, daemon=True)
    engine_thread.start()
    return f"Engine running on thread: {engine_thread.name}"

initialize_brokerage_engine()

# ====================== STREAMLIT APP ======================
st.set_page_config(page_title="FinTrade Paper Trader Pro", layout="wide")
st.title("FinTrade Paper Trader - Advanced Simulation Engine")

page = st.sidebar.selectbox("Go to", ["Dashboard", "Trade & Orders", "Portfolio Analysis", "History"])

# ====================== DASHBOARD ======================
if page == "Dashboard":
    st.header("Portfolio Overview")
    
    cash = get_cash()
    total_value, holdings = calculate_portfolio_value()
    grand_total = cash + total_value
    total_pnl = grand_total - 100000
    
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Cash Balance", f"${cash:,.2f}")
    col2.metric("Stocks Value", f"${total_value:,.2f}")
    col3.metric("Total Account Value", f"${grand_total:,.2f}")
    col4.metric("Overall Return", f"${total_pnl:,.2f}", delta=f"{(total_pnl/100000)*100:.2f}%")

    if holdings:
        st.subheader("Current Holdings")
        st.dataframe(pd.DataFrame(holdings), width='stretch')

# ====================== TRADE & ORDERS PAGE ======================
elif page == "Trade & Orders":
    st.header("Order Execution Desks")
    
    trade_col, chart_col = st.columns([5, 7])
    
    with trade_col:
        st.subheader("Order Configuration")
        symbol = st.text_input("Stock Symbol", value="AAPL").upper().strip()
        
        if symbol:
            price, market_open = get_stock_data(symbol)
            
            if price:
                if market_open:
                    st.success(f"🟢 **{symbol}** Market is Open — Price: **${price}**")
                else:
                    st.warning(f"🔴 **{symbol}** Outside Regular Market Hours — Last Close: **${price}**")
                
                strict_market = st.checkbox("Enforce Market Hours Verification", value=False)
                
                action = st.radio("Action", ["Buy", "Sell"], horizontal=True)
                order_type = st.selectbox("Order Routing Type", ["Market", "Limit", "Stop-Loss", "Take-Profit"])
                quantity = st.number_input("Shares Quantity", min_value=1, value=10, step=1)
                
                target_price = price
                if order_type in ["Limit", "Stop-Loss", "Take-Profit"]:
                    target_price = st.number_input("Trigger Target Price ($)", min_value=0.01, value=price, format="%.4f")
                
                est_spread = price * SPREAD_BPS
                est_exec_price = (price + est_spread) if action == "Buy" else (price - est_spread)
                estimated_value = quantity * est_exec_price
                
                st.markdown(f"""
                *   **Simulated Bid/Ask Spread:** $\pm${SPREAD_BPS*100:.2f}% ($\sim${est_spread:.3f}/share)
                *   **Base Transaction Cost:** ${estimated_value:,.2f}
                *   **Contract Execution Fee:** ${COMMISSION_FLAT}
                *   **Total Expected Balance Impact:** **${(estimated_value + COMMISSION_FLAT) if action == "Buy" else (estimated_value - COMMISSION_FLAT):,.2f}**
                """)
                
                if st.button("Transmit Order to Exchange", type="primary", width='stretch'):
                    if strict_market and not market_open and order_type == "Market":
                        st.error("❌ Refused: Cannot route instant Market Orders outside standard asset trading hours.")
                    
                    elif order_type == "Market":
                        success, message = process_instant_trade(symbol, action, "Market", quantity, price)
                        if success:
                            st.success(message)
                            st.rerun()
                        else:
                            st.error(message)
                            
                    else:
                        with get_db_connection() as conn:
                            c = conn.cursor()
                            c.execute("""INSERT INTO pending_orders (timestamp, symbol, action, order_type, quantity, target_price, parent_trade_price)
                                         VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                      (datetime.now().strftime("%Y-%m-%d %H:%M"), symbol, action, order_type, quantity, target_price, price))
                            conn.commit()
                        st.info(f"💾 Pending {order_type} Order registered for {symbol} at target ${target_price:.2f}")
                        st.rerun()
            else:
                st.error("Invalid symbol or tracking engine cannot connect to Yahoo Finance data APIs.")

        st.markdown("---")
        st.subheader("⏳ Active Pending Orders")
        pending_df = get_pending_orders()
        if not pending_df.empty:
            st.dataframe(pending_df, width='stretch')
            if st.button("Purge All Open Orders"):
                with get_db_connection() as conn:
                    c = conn.cursor()
                    c.execute("DELETE FROM pending_orders")
                    conn.commit()
                st.rerun()
        else:
            st.caption("No dynamic target triggers active right now.")

    with chart_col:
        st.subheader("Technical Performance Canvas")
        if symbol:
            timeframe = st.selectbox("Historical View", ["3 Months", "6 Months", "1 Year", "2 Years"], index=2)
            tf_map = {"3 Months": 3, "6 Months": 6, "1 Year": 12, "2 Years": 24}
            
            show_sma50 = st.checkbox("Show 50-day SMA", value=True)
            show_sma200 = st.checkbox("Show 200-day SMA", value=True)
            
            with st.spinner("Fetching performance metrics..."):
                hist_data = get_historical_analysis(symbol, tf_map[timeframe])
                
            if hist_data is not None and not hist_data.empty:
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=hist_data.index, y=hist_data['Close'], mode='lines', name='Price', line=dict(color='#00b4d8', width=2)))
                if show_sma50:
                    fig.add_trace(go.Scatter(x=hist_data.index, y=hist_data['SMA_50'], mode='lines', name='50 SMA', line=dict(color='#f72585', width=1.5, dash='dash')))
                if show_sma200:
                    fig.add_trace(go.Scatter(x=hist_data.index, y=hist_data['SMA_200'], mode='lines', name='200 SMA', line=dict(color='#3a0ca3', width=1.5, dash='dot')))
                
                fig.update_layout(title=f"{symbol} Visual Analytics Matrix", xaxis_title="Date", yaxis_title="Price ($)", margin=dict(l=20, r=20, t=40, b=20), hovermode="x unified")
                st.plotly_chart(fig, width='stretch')

                # ====================== DECISION ASSISTANT ======================
                st.markdown("---")
                st.subheader("🤖 Technical Decision Assistant")

                signal_data = generate_decision_signal(hist_data)

                if signal_data:

                    signal = signal_data["signal"]

                    if signal == "BUY":
                        st.success(f"### 🟢 {symbol}: BUY SIGNAL")
                    elif signal == "SELL":
                        st.error(f"### 🔴 {symbol}: SELL SIGNAL")
                    else:
                        st.warning(f"### 🟡 {symbol}: HOLD")

                    col_a, col_b, col_c = st.columns(3)

                    col_a.metric(
                        "Technical Score",
                        f"{signal_data['score']}/4"
                    )

                    col_b.metric(
                        "RSI (14)",
                        f"{signal_data['rsi']:.1f}"
                    )

                    col_c.metric(
                        "3-Month Momentum",
                        f"{signal_data['momentum']:.2f}%"
                    )

                    st.write(f"**Assessment:** {signal_data['explanation']}")

                    st.write("**Indicator Breakdown:**")

                    for icon, reason, direction in signal_data["reasons"]:
                        st.write(f"{icon} {reason}")

# ====================== PORTFOLIO ANALYSIS ======================
elif page == "Portfolio Analysis":
    st.header("My Portfolio Performance")
    cash = get_cash()
    total_value, holdings = calculate_portfolio_value()
    st.write(f"**Cash Available:** ${cash:,.2f} | **Total Stocks Valuation:** ${total_value:,.2f}")
    
    if holdings:
        holdings_df = pd.DataFrame(holdings)
        st.dataframe(holdings_df, width='stretch')
        
        st.markdown("---")
        st.subheader("🔍 Analyze a Holding")
        selected_stock = st.selectbox("Select one of your stocks to view trend charts", holdings_df['symbol'].tolist())
        
        if selected_stock:
            hist_data = get_historical_analysis(selected_stock, 12)
            if hist_data is not None:
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=hist_data.index, y=hist_data['Close'], name='Price', line=dict(color='#00b4d8')))
                fig.add_trace(go.Scatter(x=hist_data.index, y=hist_data['SMA_50'], name='50 SMA', line=dict(color='#f72585', dash='dash')))
                fig.add_trace(go.Scatter(x=hist_data.index, y=hist_data['SMA_200'], name='200 SMA', line=dict(color='#3a0ca3', dash='dot')))
                
                fig.update_layout(title=f"1-Year Trend for {selected_stock}", xaxis_title="Date", yaxis_title="Price ($)", margin=dict(l=20, r=20, t=40, b=20))
                st.plotly_chart(fig, width='stretch')
    else:
        st.info("No holdings yet.")

# ====================== HISTORY ======================
elif page == "History":
    st.header("Trade Log Records")
    history = get_trade_history()
    if not history.empty:
        st.dataframe(history, width='stretch')
    else:
        st.info("No data entries written to standard history logs.")

st.caption("Engine Core v2.0 • Spread, Commissions and Pending Orders Enabled")

if st.sidebar.checkbox("Enable Auto-Refresh UI (10s)", value=True):
    time.sleep(10)
    st.rerun()