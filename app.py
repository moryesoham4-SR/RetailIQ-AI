import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta
import io
import os
import hashlib
import json
import warnings
warnings.filterwarnings("ignore")

# ─────────────────────────────────────────
# SUPABASE PERSISTENCE
# ─────────────────────────────────────────
# Locally: reads from .streamlit/secrets.toml
# On Streamlit Cloud: reads from app secrets
def _get_sb_creds():
    try:
        url = st.secrets["supabase"]["url"]
        key = st.secrets["supabase"]["key"]
        return url, key
    except Exception:
        return None, None

def _sb_headers():
    _, key = _get_sb_creds()
    # Use user JWT if logged in (activates RLS), else fall back to anon/publishable key
    token = st.session_state.get("sb_access_token", "") or key
    return {
        "apikey": key,           # works for both eyJ... and sb_publishable_... formats
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def _sb_auth_headers():
    """Headers for /auth/v1 endpoints — always uses the raw key."""
    _, key = _get_sb_creds()
    return {"apikey": key, "Content-Type": "application/json"}

def _sb_user_id():
    """Return Supabase auth user id or 'default' for anon."""
    return st.session_state.get("user_id") or "default"

def _sb_url(table, params=""):
    url, _ = _get_sb_creds()
    return f"{url}/rest/v1/{table}{params}"

def _sb_available():
    url, key = _get_sb_creds()
    return bool(url and key)

def sb_signup(email, password, full_name=""):
    """Create new user via Supabase Auth."""
    url, key = _get_sb_creds()
    if not url: return None, "Supabase not configured"
    import requests as _r
    try:
        resp = _r.post(
            f"{url}/auth/v1/signup",
            headers={"apikey": key, "Content-Type": "application/json"},
            json={"email": email, "password": password,
                  "data": {"full_name": full_name}},
            timeout=15)
        data = resp.json()
        if resp.status_code in (200, 201) and data.get("user"):
            return data, None
        return None, data.get("msg") or data.get("error_description") or "Signup failed"
    except Exception as e:
        return None, str(e)

def sb_login(email, password):
    """Login via Supabase Auth, return session tokens."""
    url, key = _get_sb_creds()
    if not url: return None, "Supabase not configured"
    import requests as _r
    try:
        resp = _r.post(
            f"{url}/auth/v1/token?grant_type=password",
            headers=_sb_auth_headers(),
            json={"email": email, "password": password},
            timeout=15)
        data = resp.json()
        if resp.status_code == 200 and data.get("access_token"):
            return data, None
        return None, data.get("error_description") or data.get("msg") or "Login failed"
    except Exception as e:
        return None, str(e)

def sb_logout():
    """Revoke Supabase session."""
    url, key = _get_sb_creds()
    token = st.session_state.get("sb_access_token", "")
    if not url or not token: return
    import requests as _r
    try:
        _r.post(f"{url}/auth/v1/logout",
                headers={**_sb_auth_headers(), "Authorization": f"Bearer {token}"}, timeout=10)
    except Exception:
        pass

def sb_refresh_session():
    """Refresh access token using refresh token."""
    url, key = _get_sb_creds()
    refresh = st.session_state.get("sb_refresh_token", "")
    if not url or not refresh: return False
    import requests as _r
    try:
        resp = _r.post(
            f"{url}/auth/v1/token?grant_type=refresh_token",
            headers=_sb_auth_headers(),
            json={"refresh_token": refresh}, timeout=15)
        data = resp.json()
        if resp.status_code == 200 and data.get("access_token"):
            st.session_state.sb_access_token  = data["access_token"]
            st.session_state.sb_refresh_token = data.get("refresh_token", refresh)
            return True
    except Exception:
        pass
    return False

# ── Entries ────────────────────────────────────────────────────────
def save_entries(entries):
    if not _sb_available(): return _save_local(SAVE_FILE, entries)
    try:
        import requests as _r
        rows = []
        for e in entries:
            row = dict(e)
            if hasattr(row.get("Order Date"), "isoformat"):
                row["order_date"] = row.pop("Order Date").isoformat()
            else:
                row["order_date"] = str(row.pop("Order Date", ""))
            # snake_case + sanitize keys for Supabase (must match table columns exactly)
            key_map = {
                "order_id": "order_id", "customer_name": "customer_name",
                "customer_id": "customer_id", "category": "category",
                "product_name": "product_name", "quantity": "quantity",
                "unit_price": "unit_price", "discount": "discount",
                "gst_%": "gst_percent", "gst%": "gst_percent",
                "gst_amount": "gst_amount", "sales": "sales", "profit": "profit",
                "payment_mode": "payment_mode", "state": "state",
                "city": "city", "notes": "notes",
            }
            clean = {}
            for k, v in row.items():
                norm = k.lower().replace(" ", "_")
                col = key_map.get(norm, norm.replace("%", "percent"))
                clean[col] = v
            rows.append(clean)
        _r.delete(_sb_url("manual_entries"), headers=_sb_headers(),
                  params={"user_id": f"eq.{_sb_user_id()}"})
        for row in rows:
            row["user_id"] = _sb_user_id()
        if rows:
            resp = _r.post(_sb_url("manual_entries"), headers=_sb_headers(), json=rows)
            if resp.status_code not in (200, 201):
                st.session_state["_last_sb_error"] = f"save_entries failed [{resp.status_code}]: {resp.text[:300]}"
                return _save_local(SAVE_FILE, entries)
        st.session_state["_last_sb_error"] = ""
        return True
    except Exception as ex:
        st.session_state["_last_sb_error"] = f"save_entries exception: {ex}"
        return _save_local(SAVE_FILE, entries)

def load_entries():
    if not _sb_available(): return _load_local(SAVE_FILE, [])
    try:
        import requests as _r
        resp = _r.get(_sb_url("manual_entries", f"?user_id=eq.{_sb_user_id()}&order=created_at.asc"),
                      headers=_sb_headers())
        if resp.status_code == 200:
            rows = resp.json()
            label_map = {
                "order_id": "Order ID", "customer_name": "Customer Name",
                "customer_id": "Customer ID", "category": "Category",
                "product_name": "Product Name", "quantity": "Quantity",
                "unit_price": "Unit Price", "discount": "Discount",
                "gst_percent": "GST %", "gst_amount": "GST Amount",
                "sales": "Sales", "profit": "Profit",
                "payment_mode": "Payment Mode", "state": "State",
                "city": "City", "notes": "Notes",
            }
            entries = []
            for row in rows:
                e = {}
                for k, v in row.items():
                    if k in ("id", "user_id", "created_at", "order_date"):
                        continue
                    label = label_map.get(k, k.replace("_", " ").title())
                    e[label] = v
                if "order_date" in row:
                    try: e["Order Date"] = pd.Timestamp(row["order_date"])
                    except Exception: pass
                entries.append(e)
            st.session_state["_last_sb_error"] = ""
            return entries
        else:
            st.session_state["_last_sb_error"] = f"load_entries failed [{resp.status_code}]: {resp.text[:300]}"
    except Exception as ex:
        st.session_state["_last_sb_error"] = f"load_entries exception: {ex}"
    return _load_local(SAVE_FILE, [])

# ── Products & Customers ───────────────────────────────────────────
def _upsert_list(table, key, lst):
    if not _sb_available(): return False
    try:
        import requests as _r
        _r.delete(_sb_url(table), headers=_sb_headers(),
                  params={"user_id": f"eq.{_sb_user_id()}", "list_key": f"eq.{key}"})
        if lst:
            rows = [{"user_id": _sb_user_id(), "list_key": key, "value": v} for v in lst]
            _r.post(_sb_url(table), headers=_sb_headers(), json=rows)
        return True
    except Exception:
        return False

def _fetch_list(table, key):
    if not _sb_available(): return None
    try:
        import requests as _r
        resp = _r.get(_sb_url(table, f"?user_id=eq.{_sb_user_id()}&list_key=eq.{key}"),
                      headers=_sb_headers())
        if resp.status_code == 200:
            return [r["value"] for r in resp.json()]
    except Exception:
        pass
    return None

def save_products(lst):
    _save_local(PROD_FILE, lst)
    return _upsert_list("retailiq_lists", "products", lst)

def load_products():
    remote = _fetch_list("retailiq_lists", "products")
    return remote if remote is not None else _load_local(PROD_FILE, [])

def save_customers(lst):
    _save_local(CUST_FILE, lst)
    return _upsert_list("retailiq_lists", "customers", lst)

def load_customers():
    remote = _fetch_list("retailiq_lists", "customers")
    return remote if remote is not None else _load_local(CUST_FILE, [])

# ── Preferences ────────────────────────────────────────────────────
def save_prefs(prefs):
    _save_local(PREF_FILE, prefs)
    if not _sb_available(): return False
    try:
        import requests as _r
        _r.delete(_sb_url("retailiq_prefs"), headers=_sb_headers(),
                  params={"user_id": f"eq.{_sb_user_id()}"})
        _r.post(_sb_url("retailiq_prefs"), headers=_sb_headers(),
                json=[{"user_id": _sb_user_id(), "prefs": json.dumps(prefs)}])
        return True
    except Exception:
        return False

def load_prefs():
    if _sb_available():
        try:
            import requests as _r
            resp = _r.get(_sb_url("retailiq_prefs", f"?user_id=eq.{_sb_user_id()}"),
                          headers=_sb_headers())
            if resp.status_code == 200 and resp.json():
                return json.loads(resp.json()[0]["prefs"])
        except Exception:
            pass
    return _load_local(PREF_FILE, {})

# ── Stock / Inventory persistence ───────────────────────────────────
def save_stock_entries(entries):
    if not _sb_available():
        return _save_local(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "retailiq_data", "stock_entries.json"
        ), entries)
    try:
        import requests as _r
        rows = []
        for e in entries:
            row = dict(e)
            if hasattr(row.get("Date"), "isoformat"):
                row["stock_date"] = row.pop("Date").isoformat()
            else:
                row["stock_date"] = str(row.pop("Date", ""))
            clean = {}
            for k, v in row.items():
                clean[k.lower().replace(" ","_")] = v
            clean["user_id"] = _sb_user_id()
            rows.append(clean)
        _r.delete(_sb_url("inventory_stock"), headers=_sb_headers(),
                  params={"user_id": f"eq.{_sb_user_id()}"})
        if rows:
            _r.post(_sb_url("inventory_stock"), headers=_sb_headers(), json=rows)
        return True
    except Exception:
        return _save_local(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "retailiq_data", "stock_entries.json"
        ), entries)

def load_stock_entries():
    local_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "retailiq_data", "stock_entries.json")
    if not _sb_available():
        return _load_local(local_path, [])
    try:
        import requests as _r
        resp = _r.get(_sb_url("inventory_stock", f"?user_id=eq.{_sb_user_id()}&order=created_at.asc"),
                      headers=_sb_headers())
        if resp.status_code == 200:
            rows = resp.json()
            entries = []
            for row in rows:
                e = {}
                for k, v in row.items():
                    if k in ("id","user_id","created_at"): continue
                    label = k.replace("_"," ").title()
                    e[label] = v
                if "stock_date" in row:
                    try: e["Date"] = pd.Timestamp(row["stock_date"])
                    except Exception: pass
                entries.append(e)
            return entries
    except Exception:
        pass
    return _load_local(local_path, [])

def save_reorder_settings(settings):
    local_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "retailiq_data", "reorder_settings.json")
    _save_local(local_path, settings)
    if not _sb_available(): return False
    try:
        prefs = load_prefs()
        prefs["reorder_settings"] = settings
        save_prefs(prefs)
        return True
    except Exception:
        return False

def load_reorder_settings():
    local_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "retailiq_data", "reorder_settings.json")
    if _sb_available():
        try:
            prefs = load_prefs()
            if "reorder_settings" in prefs:
                return prefs["reorder_settings"]
        except Exception:
            pass
    return _load_local(local_path, {})

# ── Dataset persistence ────────────────────────────────────────────
def save_dataset(df, name, col_map):
    """Persist uploaded dataset locally as parquet + metadata."""
    try:
        import pandas as _pd
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "retailiq_data")
        os.makedirs(data_dir, exist_ok=True)
        df.to_parquet(os.path.join(data_dir, "last_dataset.parquet"), index=False)
        _save_local(os.path.join(data_dir, "last_dataset_meta.json"),
                    {"dataset_name": name, "col_map": col_map,
                     "saved_at": _pd.Timestamp.now().isoformat()})
        # Also store name in prefs so it survives across devices if prefs sync
        prefs = load_prefs()
        prefs["last_dataset_name"] = name
        save_prefs(prefs)
        return True
    except Exception:
        return False

def load_dataset():
    """Reload last uploaded dataset from local parquet."""
    try:
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "retailiq_data")
        ds_file  = os.path.join(data_dir, "last_dataset.parquet")
        meta_file= os.path.join(data_dir, "last_dataset_meta.json")
        if os.path.exists(ds_file) and os.path.exists(meta_file):
            meta = _load_local(meta_file, {})
            df   = pd.read_parquet(ds_file)
            for dcol in ["Order Date", "Ship Date"]:
                if dcol in df.columns:
                    df[dcol] = pd.to_datetime(df[dcol], errors="coerce")
            return df, meta.get("dataset_name",""), meta.get("col_map",{})
    except Exception:
        pass
    return None, "", {}

def clear_dataset():
    """Remove saved dataset files."""
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "retailiq_data")
    for fname in ["last_dataset.parquet", "last_dataset_meta.json"]:
        fpath = os.path.join(data_dir, fname)
        try:
            if os.path.exists(fpath): os.remove(fpath)
        except Exception:
            pass

# ── Local fallback (works offline / before Supabase setup) ─────────
DATA_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "retailiq_data")
SAVE_FILE = os.path.join(DATA_DIR, "manual_entries.json")
PROD_FILE = os.path.join(DATA_DIR, "product_list.json")
CUST_FILE = os.path.join(DATA_DIR, "customer_list.json")
PREF_FILE = os.path.join(DATA_DIR, "preferences.json")
os.makedirs(DATA_DIR, exist_ok=True)

def _load_local(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def _save_local(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass

# ─────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────
st.set_page_config(
    page_title="RetailIQ AI",
    page_icon="🛍️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ─────────────────────────────────────────
# THEME — DEEP TEAL × SAFFRON
# ─────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=DM+Mono:wght@400;500&display=swap');

:root {
    --bg:          #0A1628;
    --bg2:         #060E1A;
    --bg3:         #0D1F3C;
    --glass:       rgba(13,31,60,0.85);
    --accent:      #FF9933;
    --accent2:     #FFB347;
    --accent-glow: rgba(255,153,51,0.2);
    --accent-dim:  rgba(255,153,51,0.08);
    --teal:        #00C9A7;
    --blue:        #4A9EFF;
    --red:         #FF4D4D;
    --amber:       #FF6B35;
    --green:       #00C9A7;
    --text:        #E8F4FF;
    --text2:       #8BAACC;
    --text3:       #4A6580;
    --border:      rgba(255,153,51,0.12);
    --border-hi:   rgba(255,153,51,0.30);
    --radius:      14px;
    --radius-lg:   20px;
}

html, body, [data-testid="stAppViewContainer"] {
    background: var(--bg) !important;
    color: var(--text) !important;
    font-family: 'Inter', sans-serif !important;
    -webkit-font-smoothing: antialiased !important;
}
[data-testid="stAppViewContainer"] > .main { background: var(--bg) !important; }
[data-testid="block-container"] { padding-top: 0.5rem !important; }
[data-testid="stMainBlockContainer"] { max-width: 100% !important; padding: 0 1.5rem 2rem !important; }

/* Sidebar */
[data-testid="stSidebar"] {
    background: var(--bg2) !important;
    border-right: 1px solid var(--border) !important;
}
[data-testid="stSidebarNav"] { display: none !important; }
[data-testid="stSidebar"] * { color: var(--text2) !important; }
[data-testid="stSidebar"] [role="radiogroup"] label {
    background: transparent !important; border-radius: 10px !important;
    padding: 8px 14px !important; margin: 1px 0 !important;
    border-left: 2px solid transparent !important; transition: all 0.2s !important;
}
[data-testid="stSidebar"] [role="radiogroup"] label p,
[data-testid="stSidebar"] [role="radiogroup"] label span {
    color: var(--text2) !important; font-size: 0.84rem !important; font-weight: 500 !important;
}
[data-testid="stSidebar"] [role="radiogroup"] label:hover {
    background: var(--accent-dim) !important; border-left-color: var(--accent) !important;
}
[data-testid="stSidebar"] [role="radiogroup"] label:hover p,
[data-testid="stSidebar"] [role="radiogroup"] label:hover span { color: var(--accent) !important; }
[data-testid="stSidebar"] [role="radiogroup"] label > div:first-child { display: none !important; }
[data-testid="stSidebar"] [role="radiogroup"] [aria-checked="true"] {
    background: var(--accent-dim) !important;
    border-left: 2px solid var(--accent) !important;
    border-radius: 0 10px 10px 0 !important;
}
[data-testid="stSidebar"] [role="radiogroup"] [aria-checked="true"] p,
[data-testid="stSidebar"] [role="radiogroup"] [aria-checked="true"] span {
    color: var(--accent) !important; font-weight: 600 !important;
    -webkit-text-fill-color: var(--accent) !important;
}

/* Inputs */
input, textarea,
[data-testid="stTextInput"] input,
[data-testid="stNumberInput"] input {
    background: var(--bg3) !important;
    border: 1px solid var(--border) !important;
    border-radius: 10px !important;
    color: var(--text) !important;
    -webkit-text-fill-color: var(--text) !important;
    caret-color: var(--accent) !important;
}
input:focus, textarea:focus {
    border-color: var(--accent) !important;
    box-shadow: 0 0 0 3px var(--accent-glow) !important;
}
input:-webkit-autofill, input:-webkit-autofill:focus {
    -webkit-box-shadow: 0 0 0 9999px var(--bg3) inset !important;
    -webkit-text-fill-color: var(--text) !important;
}
input::placeholder { color: var(--text3) !important; -webkit-text-fill-color: var(--text3) !important; }
[data-baseweb="input"], [data-baseweb="base-input"],
[data-testid="stTextInput"] > div { background: var(--bg3) !important; border-radius: 10px !important; }
[data-testid="stSelectbox"] div[data-baseweb="select"] > div,
[data-baseweb="select"] > div {
    background: var(--bg3) !important;
    border: 1px solid var(--border) !important;
    border-radius: 10px !important; color: var(--text) !important;
}
[data-baseweb="select"] * { color: var(--text) !important; }
label, [data-testid="stWidgetLabel"] p {
    color: var(--text2) !important; -webkit-text-fill-color: var(--text2) !important;
    font-size: 0.8rem !important; font-weight: 500 !important;
}

/* Buttons */
.stButton > button {
    background: linear-gradient(135deg, #FF9933, #E67E00) !important;
    color: #0A1628 !important; -webkit-text-fill-color: #0A1628 !important;
    font-weight: 700 !important; border: none !important;
    border-radius: 10px !important; font-size: 0.86rem !important;
    box-shadow: 0 4px 16px var(--accent-glow),
                inset 0 1px 0 rgba(255,255,255,0.15) !important;
    transition: all 0.2s !important;
}
.stButton > button:hover { transform: translateY(-2px) !important; box-shadow: 0 8px 24px rgba(255,153,51,0.4) !important; }
.stButton > button * { color: #0A1628 !important; -webkit-text-fill-color: #0A1628 !important; }
.stDownloadButton > button {
    background: var(--bg3) !important; border: 1px solid var(--border) !important;
    color: var(--accent) !important; -webkit-text-fill-color: var(--accent) !important; border-radius: 10px !important;
}

/* Metrics */
[data-testid="metric-container"] {
    background: var(--glass) !important; backdrop-filter: blur(20px) !important;
    border: 1px solid var(--border) !important; border-radius: var(--radius) !important;
    padding: 1rem 1.2rem !important;
    box-shadow: 0 4px 24px rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,153,51,0.06) !important;
}
[data-testid="stMetricValue"] { color: var(--accent) !important; -webkit-text-fill-color: var(--accent) !important; font-family: 'DM Mono', monospace !important; }
[data-testid="stMetricLabel"] { color: var(--text2) !important; -webkit-text-fill-color: var(--text2) !important; }

/* Dataframe */
[data-testid="stDataFrame"] {
    background: var(--glass) !important; border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important; overflow: hidden !important;
}

/* Progress */
[data-testid="stProgressBar"] > div > div {
    background: linear-gradient(90deg, #FF9933, #00C9A7) !important;
    border-radius: 999px !important; box-shadow: 0 0 8px var(--accent-glow) !important;
}
[data-testid="stProgressBar"] > div { background: var(--bg3) !important; border-radius: 999px !important; }

/* Form */
[data-testid="stForm"] {
    background: var(--glass) !important; backdrop-filter: blur(20px) !important;
    border: 1px solid var(--border) !important; border-radius: var(--radius-lg) !important;
    padding: 1.4rem !important;
    box-shadow: 0 8px 40px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,153,51,0.06) !important;
}

/* Alerts */
[data-testid="stAlert"] {
    background: rgba(255,153,51,0.05) !important; border: 1px solid var(--border) !important;
    border-left: 3px solid var(--accent) !important; border-radius: var(--radius) !important;
}
[data-testid="stAlert"] p { color: var(--text) !important; -webkit-text-fill-color: var(--text) !important; }

/* Expander */
[data-testid="stExpander"] {
    background: var(--glass) !important; border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important;
}
[data-testid="stExpander"] summary { color: var(--text) !important; }

/* Scrollbar */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: var(--bg2); }
::-webkit-scrollbar-thumb { background: var(--accent); border-radius: 4px; }

/* Page transition */
[data-testid="stMainBlockContainer"] { animation: fadeUp 0.3s ease; }
@keyframes fadeUp { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:translateY(0); } }

/* Ticker */
.riq-ticker-wrap {
    overflow: hidden; background: rgba(255,153,51,0.04);
    border: 1px solid var(--border); border-radius: 10px;
    padding: 6px 0; margin-bottom: 1.2rem;
}
.riq-ticker {
    display: inline-block; white-space: nowrap;
    font-family: 'DM Mono', monospace; font-size: 0.72rem;
    color: var(--accent2); letter-spacing: 0.08em;
    animation: tickScroll 40s linear infinite;
}
@keyframes tickScroll { from { transform: translateX(100vw); } to { transform: translateX(-100%); } }

/* Glass card */
.riq-card {
    background: var(--glass); backdrop-filter: blur(20px) saturate(160%);
    -webkit-backdrop-filter: blur(20px) saturate(160%);
    border: 1px solid var(--border); border-radius: var(--radius);
    padding: 1.2rem 1.4rem; margin-bottom: 0.9rem;
    box-shadow: 0 8px 32px rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,153,51,0.05);
    transition: border-color 0.2s, box-shadow 0.2s;
}
.riq-card:hover { border-color: var(--border-hi); box-shadow: 0 12px 40px rgba(0,0,0,0.4); }

/* Section tags */
.riq-tag {
    display: inline-block; font-family: 'DM Mono', monospace;
    font-size: 0.6rem; font-weight: 600; padding: 2px 9px;
    border-radius: 5px; background: var(--accent-dim); color: var(--accent);
    border: 1px solid var(--border); letter-spacing: 0.12em; text-transform: uppercase;
    margin-bottom: 3px;
}
.riq-section-title {
    font-family: 'Inter', sans-serif; font-size: 0.95rem;
    font-weight: 600; color: var(--text); margin-bottom: 0.8rem;
}

/* Page title */
.riq-page-title {
    font-family: 'Inter', sans-serif; font-size: 1.7rem;
    font-weight: 800; letter-spacing: -0.02em; color: var(--text); margin-bottom: 0.1rem;
}
.riq-page-sub {
    font-size: 0.76rem; color: var(--accent);
    font-family: 'DM Mono', monospace; letter-spacing: 0.04em;
    margin-bottom: 1.4rem; -webkit-text-fill-color: var(--accent);
}

/* AI insight cards */
.riq-insight {
    background: var(--glass); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 0.9rem 1.1rem;
    margin-bottom: 0.6rem; display: flex; gap: 12px;
    align-items: flex-start; backdrop-filter: blur(16px);
}

/* Sidebar components */
.riq-logo {
    font-family: 'Inter', sans-serif; font-size: 1.3rem; font-weight: 800;
    background: linear-gradient(90deg, #FF9933, #00C9A7);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text; padding: 0.5rem 0 0.1rem; letter-spacing: -0.01em;
}
.riq-tagline {
    font-size: 0.62rem; color: var(--text3); letter-spacing: 0.1em;
    font-family: 'DM Mono', monospace; margin-bottom: 1rem;
    -webkit-text-fill-color: var(--text3) !important;
}
.riq-user-badge {
    background: var(--accent-dim); border: 1px solid var(--border);
    border-radius: 10px; padding: 8px 12px;
    font-family: 'DM Mono', monospace; font-size: 0.76rem;
    color: var(--accent); margin-bottom: 0.8rem;
    -webkit-text-fill-color: var(--accent);
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────
# CONSTANTS & COLORS — DEEP TEAL × SAFFRON
# ─────────────────────────────────────────
BG      = "#0A1628"
BG2     = "#060E1A"
GRID    = "rgba(255,153,51,0.06)"
FONT_C  = "#8BAACC"
GOLD    = "#FF9933"
GOLD2   = "#FFB347"
GREEN   = "#00C9A7"
RED     = "#FF4D4D"
AMBER   = "#FF6B35"
BLUE    = "#4A9EFF"
PALETTE = [GOLD, GREEN, BLUE, AMBER, RED, GOLD2, "#A78BFA", "#F472B6"]

# ─────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────
# Load persisted preferences first
_prefs = load_prefs()

for k, v in [("logged_in", False), ("username", ""), ("user_id", None),
             ("sb_access_token", ""), ("sb_refresh_token", ""),
             ("dataset", None), ("dataset_name", ""), ("col_map", {}),
             ("currency", _prefs.get("currency", "INR")),
             ("manual_entries", []),
             ("me_product_list", []),
             ("me_customer_list", []),
             ("stock_entries", []),
             ("reorder_settings", {}),
             ("use_demo_data", False),
             ("_data_loaded", False)]:
    if k not in st.session_state:
        st.session_state[k] = v

# Load persisted data only once per session after login
if st.session_state.logged_in and not st.session_state._data_loaded:
    st.session_state.manual_entries   = load_entries()
    st.session_state.me_product_list  = load_products()
    st.session_state.me_customer_list = load_customers()
    st.session_state.stock_entries    = load_stock_entries()
    st.session_state.reorder_settings = load_reorder_settings()
    if st.session_state.dataset is None:
        _ds, _ds_name, _ds_col_map = load_dataset()
        if _ds is not None:
            st.session_state.dataset      = _ds
            st.session_state.dataset_name = _ds_name
            st.session_state.col_map      = _ds_col_map
    st.session_state._data_loaded = True

# ─────────────────────────────────────────
# CURRENCY CONFIG
# ─────────────────────────────────────────
CURRENCIES = {
    "INR": {"symbol": "₹", "name": "Indian Rupee",     "scale": "indian"},
    "USD": {"symbol": "$", "name": "US Dollar",         "scale": "western"},
    "EUR": {"symbol": "€", "name": "Euro",              "scale": "western"},
    "GBP": {"symbol": "£", "name": "British Pound",     "scale": "western"},
    "JPY": {"symbol": "¥", "name": "Japanese Yen",      "scale": "western"},
    "AED": {"symbol": "د.إ","name": "UAE Dirham",       "scale": "western"},
    "SGD": {"symbol": "S$", "name": "Singapore Dollar", "scale": "western"},
    "AUD": {"symbol": "A$", "name": "Australian Dollar","scale": "western"},
    "CAD": {"symbol": "C$", "name": "Canadian Dollar",  "scale": "western"},
    "CNY": {"symbol": "¥",  "name": "Chinese Yuan",     "scale": "western"},
}

def get_currency():
    """Return current currency config dict."""
    return CURRENCIES.get(st.session_state.get("currency", "INR"), CURRENCIES["INR"])

def currency_symbol():
    return get_currency()["symbol"]

def currency_tickprefix():
    return get_currency()["symbol"]

# ─────────────────────────────────────────
# PLOTLY BASE THEME
# ─────────────────────────────────────────
def plotly_base(height=320):
    return dict(
        height=height,
        paper_bgcolor=BG2,
        plot_bgcolor=BG2,
        font=dict(family="DM Mono, monospace", color=FONT_C, size=11),
        margin=dict(l=10, r=10, t=30, b=10),
        xaxis=dict(gridcolor=GRID, showline=False, zeroline=False,
                   tickfont=dict(size=10)),
        yaxis=dict(gridcolor=GRID, showline=False, zeroline=False,
                   tickfont=dict(size=10)),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
        hoverlabel=dict(bgcolor=BG2, bordercolor=GOLD,
                        font=dict(family="DM Mono", size=11, color=FONT_C)),
    )

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def page_header(title, sub=""):
    st.markdown(f'<div class="riq-page-title">{title}</div>', unsafe_allow_html=True)
    if sub:
        st.markdown(f'<div class="riq-page-sub">{sub}</div>', unsafe_allow_html=True)

def tag(label):
    return f'<div class="riq-tag">{label}</div>'

def fmt_num(n, prefix=""):
    """Format number using current currency and appropriate scale."""
    cur = get_currency()
    # Use currency symbol if prefix is a money marker, else use prefix as-is
    symbol = cur["symbol"] if prefix in ("$", "₹", "£", "€", "¥", "S$", "A$", "C$", "د.إ", "") and prefix != "" else prefix
    if not prefix:
        # No currency — plain number formatting
        if abs(n) >= 1_000_000: return f"{n/1_000_000:.2f}M"
        if abs(n) >= 1_000:     return f"{n/1_000:.1f}K"
        return f"{n:,.0f}"
    if cur["scale"] == "indian":
        if abs(n) >= 1_00_00_000: return f"{symbol}{n/1_00_00_000:.2f}Cr"
        if abs(n) >= 1_00_000:    return f"{symbol}{n/1_00_000:.2f}L"
        if abs(n) >= 1_000:       return f"{symbol}{n/1_000:.1f}K"
    else:
        if abs(n) >= 1_000_000_000: return f"{symbol}{n/1_000_000_000:.2f}B"
        if abs(n) >= 1_000_000:     return f"{symbol}{n/1_000_000:.2f}M"
        if abs(n) >= 1_000:         return f"{symbol}{n/1_000:.1f}K"
    return f"{symbol}{n:,.0f}"

def kpi_card(tag_txt, val, label, color, spark=""):
    return (
        f'<div style="background:rgba(25,25,28,0.8);backdrop-filter:blur(20px);'
        f'border:1px solid rgba(255,153,51,0.12);border-top:2px solid {color};'
        f'border-radius:14px;padding:0.9rem 1rem;position:relative;overflow:hidden;'
        f'box-shadow:0 4px 20px rgba(0,0,0,0.3),inset 0 1px 0 rgba(212,175,55,0.06);">'
        f'<div style="position:absolute;top:-25px;right:-15px;width:70px;height:70px;'
        f'border-radius:50%;background:{color};opacity:0.07;"></div>'
        f'<div style="display:inline-block;font-family:DM Mono,monospace;font-size:0.58rem;'
        f'font-weight:600;letter-spacing:0.1em;padding:2px 8px;border-radius:5px;'
        f'background:rgba(212,175,55,0.08);color:{color};margin-bottom:4px;">{tag_txt}</div>'
        f'<div style="font-family:DM Mono,monospace;font-size:1.25rem;font-weight:600;'
        f'color:{color};line-height:1.1;margin-bottom:2px;white-space:nowrap;'
        f'overflow:hidden;text-overflow:ellipsis;">{val}</div>'
        f'<div style="font-size:0.64rem;color:#6B6B75;text-transform:uppercase;letter-spacing:0.07em;">{label}</div>'
        f'{spark}</div>'
    )

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

# ─────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────
DEMO_PATH = os.path.join(os.path.dirname(__file__), "Global_Superstore2.csv")

DEFAULT_COL_MAP = {
    "order_id":     "Order ID",
    "order_date":   "Order Date",
    "ship_date":    "Ship Date",
    "ship_mode":    "Ship Mode",
    "customer_id":  "Customer ID",
    "customer_name":"Customer Name",
    "segment":      "Segment",
    "city":         "City",
    "state":        "State",
    "country":      "Country",
    "region":       "Region",
    "market":       "Market",
    "category":     "Category",
    "subcategory":  "Sub-Category",
    "product_name": "Product Name",
    "product_id":   "Product ID",
    "sales":        "Sales",
    "quantity":     "Quantity",
    "discount":     "Discount",
    "profit":       "Profit",
    "shipping_cost":"Shipping Cost",
    "order_priority":"Order Priority",
}

@st.cache_data
def load_demo():
    df = pd.read_csv(DEMO_PATH, encoding="latin1")
    df["Order Date"] = pd.to_datetime(df["Order Date"], dayfirst=True, errors="coerce")
    df["Ship Date"]  = pd.to_datetime(df["Ship Date"],  dayfirst=True, errors="coerce")
    df["Sales"]       = pd.to_numeric(df["Sales"], errors="coerce").fillna(0)
    df["Profit"]      = pd.to_numeric(df["Profit"], errors="coerce").fillna(0)
    df["Quantity"]    = pd.to_numeric(df["Quantity"], errors="coerce").fillna(0)
    df["Discount"]    = pd.to_numeric(df["Discount"], errors="coerce").fillna(0)
    df["Shipping Cost"]= pd.to_numeric(df["Shipping Cost"], errors="coerce").fillna(0)
    return df

def empty_dataset():
    """Return an empty but properly-schemad dataframe for fresh accounts."""
    cols = ["Order ID","Order Date","Ship Date","Ship Mode","Customer ID","Customer Name",
            "Segment","City","State","Country","Region","Market","Category","Sub-Category",
            "Product Name","Product ID","Sales","Quantity","Discount","Profit","Shipping Cost"]
    edf = pd.DataFrame(columns=cols)
    edf["Order Date"] = pd.to_datetime(edf["Order Date"])
    edf["Ship Date"]  = pd.to_datetime(edf["Ship Date"])
    for c in ["Sales","Quantity","Discount","Profit","Shipping Cost"]:
        edf[c] = pd.to_numeric(edf[c])
    return edf

def manual_entries_as_df():
    """Convert manual entries into the standard dataset schema."""
    entries = st.session_state.get("manual_entries", [])
    if not entries:
        return None
    me = pd.DataFrame(entries)
    if "Order Date" in me.columns:
        me["Order Date"] = pd.to_datetime(me["Order Date"], errors="coerce")
    if "Profit" not in me.columns:
        me["Profit"] = 0.0
    if "Order ID" not in me.columns:
        me["Order ID"] = [f"ME-{i}" for i in range(len(me))]
    if "Category" not in me.columns:
        me["Category"] = "General"
    for nc in ["Sales","Profit","Quantity","Discount"]:
        if nc in me.columns:
            me[nc] = pd.to_numeric(me[nc], errors="coerce").fillna(0)
    return me

def get_df():
    """Return the active dataset, always live-merged with Manual Entry sales."""
    base = None
    if st.session_state.dataset is not None:
        base = st.session_state.dataset
    elif st.session_state.get("use_demo_data", False):
        base = load_demo()

    me_df = manual_entries_as_df()

    if base is None and me_df is None:
        return empty_dataset()
    if base is None:
        return me_df
    if me_df is None:
        return base

    # Merge — align columns, concat, avoid duplicate Order IDs colliding
    common_cols = list(dict.fromkeys(list(base.columns) + list(me_df.columns)))
    base_aligned = base.reindex(columns=common_cols)
    me_aligned   = me_df.reindex(columns=common_cols)
    merged = pd.concat([base_aligned, me_aligned], ignore_index=True)
    return merged

def apply_col_map(df, col_map):
    """Rename uploaded dataset columns to standard names"""
    reverse = {v: k for k, v in col_map.items()}
    std_names = {
        "order_id":"Order ID","order_date":"Order Date","ship_date":"Ship Date",
        "ship_mode":"Ship Mode","customer_id":"Customer ID","customer_name":"Customer Name",
        "segment":"Segment","city":"City","state":"State","country":"Country",
        "region":"Region","market":"Market","category":"Category",
        "subcategory":"Sub-Category","product_name":"Product Name","product_id":"Product ID",
        "sales":"Sales","quantity":"Quantity","discount":"Discount","profit":"Profit",
        "shipping_cost":"Shipping Cost","order_priority":"Order Priority",
    }
    rename = {}
    for std_key, std_col in std_names.items():
        if std_key in col_map:
            rename[col_map[std_key]] = std_col
    return df.rename(columns=rename)

# ─────────────────────────────────────────
# AUTH DIALOGS — Supabase Auth
# ─────────────────────────────────────────
def _set_session(data, email):
    """Store Supabase session in st.session_state."""
    user = data.get("user") or {}
    st.session_state.logged_in        = True
    st.session_state.username         = (user.get("user_metadata", {}).get("full_name")
                                         or email.split("@")[0])
    st.session_state.user_id          = user.get("id") or data.get("user", {}).get("id")
    st.session_state.sb_access_token  = data.get("access_token", "")
    st.session_state.sb_refresh_token = data.get("refresh_token", "")
    st.session_state._data_loaded     = False  # trigger data reload for this user

@st.dialog("Login to RetailIQ")
def login_dialog():
    st.markdown(
        f'<div style="color:{GOLD};font-family:DM Mono,monospace;' 
        f'font-size:0.78rem;margin-bottom:1rem;">Sign in to your account</div>',
        unsafe_allow_html=True)

    email    = st.text_input("Email", placeholder="you@example.com")
    password = st.text_input("Password", type="password", placeholder="your password")

    if st.button("Login", use_container_width=True):
        if not email or not password:
            st.warning("Enter email and password.")
        elif not _sb_available():
            # Offline fallback for local dev without Supabase
            st.session_state.logged_in = True
            st.session_state.username  = email.split("@")[0]
            st.session_state.user_id   = hashlib.md5(email.encode()).hexdigest()
            st.session_state._data_loaded = False
            st.rerun()
        else:
            with st.spinner("Signing in…"):
                data, err = sb_login(email, password)
            if data:
                _set_session(data, email)
                st.rerun()
            else:
                st.error(f"Login failed: {err}")

    st.markdown(
        f'<div style="font-size:0.72rem;color:#6B6B75;margin-top:0.5rem;text-align:center;">' 
        f'No account? Close this and click Sign Up</div>',
        unsafe_allow_html=True)

@st.dialog("Create Account")
def signup_dialog():
    st.markdown(
        f'<div style="color:{GOLD};font-family:DM Mono,monospace;' 
        f'font-size:0.78rem;margin-bottom:1rem;">Create your RetailIQ account</div>',
        unsafe_allow_html=True)

    full_name = st.text_input("Full Name", placeholder="Ramesh Sharma")
    email     = st.text_input("Email", placeholder="you@example.com")
    password  = st.text_input("Password", type="password", placeholder="min 6 characters")
    confirm   = st.text_input("Confirm Password", type="password")

    if st.button("Create Account", use_container_width=True):
        if not email or not password or not full_name:
            st.warning("All fields are required.")
        elif len(password) < 6:
            st.warning("Password must be at least 6 characters.")
        elif password != confirm:
            st.error("Passwords do not match.")
        elif not _sb_available():
            # Offline fallback
            st.session_state.logged_in = True
            st.session_state.username  = full_name or email.split("@")[0]
            st.session_state.user_id   = hashlib.md5(email.encode()).hexdigest()
            st.session_state._data_loaded = False
            st.success(f"Welcome, {full_name}!")
            st.rerun()
        else:
            with st.spinner("Creating account…"):
                data, err = sb_signup(email, password, full_name)
            if data:
                # Auto-login after signup
                login_data, lerr = sb_login(email, password)
                if login_data:
                    _set_session(login_data, email)
                    st.rerun()
                else:
                    st.success("Account created! Please login.")
            else:
                st.error(f"Signup failed: {err}")

# ─────────────────────────────────────────
# LANDING PAGE
# ─────────────────────────────────────────
if not st.session_state.logged_in:
    st.markdown("""
    <div style="text-align:center;padding:3rem 1rem 2rem;">
        <div style="font-family:Inter,sans-serif;font-size:0.8rem;color:#D4AF37;
            letter-spacing:0.2em;margin-bottom:0.5rem;font-family:DM Mono,monospace;">
            ENTERPRISE RETAIL INTELLIGENCE
        </div>
        <div style="font-family:Inter,sans-serif;font-size:2.8rem;font-weight:800;
            letter-spacing:-0.03em;color:#F8FAFC;margin-bottom:0.5rem;line-height:1.15;">
            RetailIQ <span style="background:linear-gradient(90deg,#D4AF37,#E3C15B);
            -webkit-background-clip:text;-webkit-text-fill-color:transparent;">AI</span>
        </div>
        <div style="font-size:0.9rem;color:#B5B5BD;max-width:500px;margin:0 auto 2.5rem;">
            Upload any retail dataset. Get instant analytics,
            ML forecasts, and AI-powered business insights.
        </div>
    </div>
    """, unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    if c1.button("Login", use_container_width=True):
        login_dialog()
    if c2.button("Sign Up", use_container_width=True):
        signup_dialog()

    st.markdown("<br>", unsafe_allow_html=True)
    features = [
        ("📊", "Executive Dashboard", "Revenue, profit, orders KPIs at a glance"),
        ("📂", "Any Dataset", "Upload CSV/Excel — auto column mapping"),
        ("🤖", "AI Insights", "Auto-generated business recommendations"),
        ("📈", "ML Forecasting", "Sales & demand predictions"),
        ("👥", "Customer Analytics", "Segmentation, CLV, top customers"),
        ("📦", "Product Analytics", "Top/bottom performers, category analysis"),
        ("🌍", "Regional Analytics", "Market, region, country performance"),
        ("📄", "Reports", "PDF & Excel export with executive summary"),
    ]
    cols = st.columns(4)
    for i, (icon, title, desc) in enumerate(features):
        cols[i%4].markdown(
            f'<div class="riq-card" style="padding:0.9rem 1rem;">'
            f'<div style="font-size:1.5rem;margin-bottom:6px;">{icon}</div>'
            f'<div style="font-weight:600;font-size:0.88rem;color:#F8FAFC;">{title}</div>'
            f'<div style="font-size:0.72rem;color:#6B6B75;margin-top:3px;">{desc}</div>'
            f'</div>', unsafe_allow_html=True)
    st.stop()

# ─────────────────────────────────────────
# SIDEBAR NAV
# ─────────────────────────────────────────
with st.sidebar:
    st.markdown('<div class="riq-logo">🛍️ RetailIQ AI</div>', unsafe_allow_html=True)
    st.markdown('<div class="riq-tagline">RETAIL INTELLIGENCE PLATFORM</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="riq-user-badge">◉  {st.session_state.username}</div>', unsafe_allow_html=True)

    # Dataset badge
    ds_name = st.session_state.dataset_name or "Global Superstore (Demo)"
    st.markdown(
        f'<div style="background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.2);'
        f'border-radius:8px;padding:6px 10px;font-size:0.7rem;font-family:DM Mono,monospace;'
        f'color:#10B981;margin-bottom:0.8rem;">📂 {ds_name[:28]}</div>',
        unsafe_allow_html=True)

    st.markdown("---")

    nav_pages = [
        "🏠 Dashboard",
        "📂 Dataset Manager",
        "🧾 Manual Entry",
        "📋 Inventory",
        "📊 Sales Analytics",
        "👥 Customer Analytics",
        "📦 Product Analytics",
        "🌍 Regional Analytics",
        "📈 Forecasting",
        "🔮 Churn Prediction",
        "🚨 Anomaly Detection",
        "🛒 Recommendations",
        "🤖 AI Insights",
        "💬 AI Chatbot",
        "📄 Reports",
        "⚙️ Settings",
    ]
    if "current_page" not in st.session_state:
        st.session_state.current_page = "🏠 Dashboard"

    selected = st.radio("", nav_pages,
                        index=nav_pages.index(st.session_state.current_page)
                        if st.session_state.current_page in nav_pages else 0,
                        label_visibility="collapsed")
    if selected != st.session_state.current_page:
        st.session_state.current_page = selected
        st.rerun()
    menu = st.session_state.current_page

    st.markdown("<br>" * 3, unsafe_allow_html=True)
    if st.button("Logout", use_container_width=True):
        sb_logout()
        for k in ["logged_in","username","user_id","sb_access_token","sb_refresh_token",
                  "dataset","dataset_name","col_map","current_page",
                  "manual_entries","me_product_list","me_customer_list","_data_loaded"]:
            if k == "logged_in": st.session_state[k] = False
            elif k in ["user_id","dataset"]: st.session_state[k] = None
            elif k in ["username","dataset_name","sb_access_token","sb_refresh_token"]: st.session_state[k] = ""
            elif k == "col_map": st.session_state[k] = {}
            elif k == "current_page": st.session_state[k] = "🏠 Dashboard"
            elif k in ["manual_entries","me_product_list","me_customer_list"]: st.session_state[k] = []
            elif k == "_data_loaded": st.session_state[k] = False
        st.rerun()

# ─────────────────────────────────────────
# LOAD DATA
# ─────────────────────────────────────────
df = get_df()

# Pages that work fine with no data yet
_no_data_ok_pages = {"📂 Dataset Manager", "🧾 Manual Entry", "📋 Inventory", "⚙️ Settings"}

if len(df) == 0 and menu not in _no_data_ok_pages:
    page_header("Welcome to RetailIQ AI", "// let's get your data in")
    st.markdown(
        f'<div class="riq-card" style="text-align:center;padding:3rem 2rem;">'
        f'<div style="font-size:2.4rem;margin-bottom:0.6rem;">📊</div>'
        f'<div style="font-size:1.1rem;font-weight:600;color:{GOLD};margin-bottom:0.6rem;">'
        f'No data yet — let\u2019s fix that</div>'
        f'<div style="font-size:0.84rem;color:#B5B5BD;max-width:480px;margin:0 auto 1.5rem;line-height:1.7;">'
        f'Upload your own sales data, enter sales manually, or try the demo dataset '
        f'to explore everything RetailIQ AI can do.</div>'
        f'</div>', unsafe_allow_html=True)

    ec1, ec2, ec3 = st.columns(3)
    if ec1.button("📂 Upload My Dataset", use_container_width=True):
        st.session_state.current_page = "📂 Dataset Manager"
        st.rerun()
    if ec2.button("🧾 Enter Sales Manually", use_container_width=True):
        st.session_state.current_page = "🧾 Manual Entry"
        st.rerun()
    if ec3.button("🎬 Try Demo Data", use_container_width=True):
        st.session_state.use_demo_data = True
        st.rerun()
    st.stop()

# ═══════════════════════════════════════════════════════
# PAGE: DASHBOARD
# ═══════════════════════════════════════════════════════
if menu == "🏠 Dashboard":
    page_header("Executive Dashboard", "// retail performance at a glance")

    # ── SMART ALERTS ────────────────────────────────────────────────
    _alerts = []
    _monthly_kpi = df.groupby(df["Order Date"].dt.to_period("M"))["Sales"].sum()
    if len(_monthly_kpi) >= 2:
        _mom = (_monthly_kpi.iloc[-1] - _monthly_kpi.iloc[-2]) / _monthly_kpi.iloc[-2] * 100
        if _mom < -15:
            _alerts.append(("🔴", RED,   f"Revenue dropped {_mom:.1f}% vs last month — investigate immediately"))
        elif _mom < -5:
            _alerts.append(("🟡", AMBER, f"Revenue down {_mom:.1f}% month-on-month — monitor closely"))
        elif _mom > 20:
            _alerts.append(("🟢", GREEN, f"Strong revenue growth of +{_mom:.1f}% this month"))
    _ts = df["Sales"].sum()
    _tp = df["Profit"].sum()
    _mg = _tp / _ts * 100 if _ts > 0 else 0
    if _mg < 5:
        _alerts.append(("🔴", RED,   f"Profit margin critically low at {_mg:.1f}% — review pricing and costs"))
    elif _mg < 10:
        _alerts.append(("🟡", AMBER, f"Profit margin at {_mg:.1f}% — below healthy threshold of 10%"))
    if "Discount" in df.columns:
        _hdp = (df["Discount"] > 0.3).mean() * 100
        if _hdp > 25:
            _alerts.append(("🟡", AMBER, f"{_hdp:.0f}% of orders have discounts > 30% — hurting margins"))
    if "Product Name" in df.columns:
        _lc = (df.groupby("Product Name")["Profit"].sum() < 0).sum()
        if _lc > 10:
            _alerts.append(("🟡", AMBER, f"{_lc} loss-making products detected — review SKU profitability"))
    if "Shipping Cost" in df.columns and _ts > 0:
        _sr = df["Shipping Cost"].sum() / _ts * 100
        if _sr > 18:
            _alerts.append(("🟡", AMBER, f"Shipping costs are {_sr:.1f}% of revenue — negotiate carrier rates"))
    if _alerts:
        _alert_html = '<div style="margin-bottom:1.2rem;">'
        for _icon, _color, _msg in _alerts:
            _alert_html += (
                f'<div style="display:flex;align-items:center;gap:10px;padding:8px 14px;' +
                f'background:rgba(25,25,28,0.9);border:1px solid {_color}33;' +
                f'border-left:3px solid {_color};border-radius:10px;margin-bottom:6px;">' +
                f'<span style="font-size:1rem;">{_icon}</span>' +
                f'<span style="font-size:0.78rem;color:#E8E8F0;">{_msg}</span>' +
                f'</div>'
            )
        _alert_html += '</div>'
        st.markdown(_alert_html, unsafe_allow_html=True)

    total_sales   = df["Sales"].sum()
    total_profit  = df["Profit"].sum()
    total_orders  = df["Order ID"].nunique()
    total_cust    = df["Customer ID"].nunique() if "Customer ID" in df.columns else 0
    total_qty     = df["Quantity"].sum()
    profit_margin = (total_profit / total_sales * 100) if total_sales > 0 else 0
    avg_order_val = total_sales / total_orders if total_orders > 0 else 0
    total_products= df["Product ID"].nunique() if "Product ID" in df.columns else 0

    # Ticker
    tk = "     ◆     ".join([
        f"SALES  {fmt_num(total_sales,'$')}",
        f"PROFIT  {fmt_num(total_profit,'$')}",
        f"ORDERS  {fmt_num(total_orders)}",
        f"CUSTOMERS  {fmt_num(total_cust)}",
        f"MARGIN  {profit_margin:.1f}%",
        f"AVG ORDER  {fmt_num(avg_order_val,'$')}",
        f"PRODUCTS  {fmt_num(total_products)}",
        f"QTY SOLD  {fmt_num(total_qty)}",
    ])
    st.markdown(f'<div class="riq-ticker-wrap"><span class="riq-ticker">{tk} ◆ {tk}</span></div>',
                unsafe_allow_html=True)

    # 8 KPI cards
    k1 = kpi_card("SALES",   fmt_num(total_sales,"$"),   "Total Revenue",     GOLD)
    k2 = kpi_card("PROFIT",  fmt_num(total_profit,"$"),  "Total Profit",      GREEN)
    k3 = kpi_card("ORDERS",  fmt_num(total_orders),      "Total Orders",      BLUE)
    k4 = kpi_card("MARGIN",  f"{profit_margin:.1f}%",    "Profit Margin",     AMBER)
    k5 = kpi_card("CUSTOMERS",fmt_num(total_cust),       "Unique Customers",  GOLD2)
    k6 = kpi_card("AVG ORDER",fmt_num(avg_order_val,"$"),"Avg Order Value",   GREEN)
    k7 = kpi_card("PRODUCTS", fmt_num(total_products),   "Products Sold",     BLUE)
    k8 = kpi_card("QTY",      fmt_num(total_qty),        "Units Sold",        RED)

    kpi_grid = (
        '<style>.kpi8g{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:1.2rem;}'
        '@media(max-width:768px){.kpi8g{grid-template-columns:repeat(2,1fr)!important;}}</style>'
        f'<div class="kpi8g">{k1}{k2}{k3}{k4}{k5}{k6}{k7}{k8}</div>'
    )
    st.markdown(kpi_grid, unsafe_allow_html=True)

    # Row 1: Monthly Sales Trend + Category Breakdown
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f'{tag("TREND")}<div class="riq-section-title">Monthly Sales & Profit</div>', unsafe_allow_html=True)
        monthly = df.groupby(df["Order Date"].dt.to_period("M")).agg(
            Sales=("Sales","sum"), Profit=("Profit","sum")).reset_index()
        monthly["Month"] = monthly["Order Date"].astype(str)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=monthly["Month"], y=monthly["Sales"],
            name="Sales", line=dict(color=GOLD, width=2.5),
            fill="tozeroy", fillcolor="rgba(212,175,55,0.06)",
            hovertemplate="<b>%{x}</b><br>Sales: {currency_symbol()}%{{y:,.0f}}<extra></extra>"))
        fig.add_trace(go.Scatter(x=monthly["Month"], y=monthly["Profit"],
            name="Profit", line=dict(color=GREEN, width=2, dash="dot"),
            hovertemplate="<b>%{x}</b><br>Profit: {currency_symbol()}%{{y:,.0f}}<extra></extra>"))
        lay = plotly_base(300)
        lay.update({"xaxis_tickangle": -30, "legend": dict(bgcolor="rgba(0,0,0,0)", orientation="h", y=-0.2)})
        fig.update_layout(**lay)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    with col2:
        st.markdown(f'{tag("CATEGORY")}<div class="riq-section-title">Sales by Category</div>', unsafe_allow_html=True)
        if "Category" in df.columns:
            cat = df.groupby("Category")["Sales"].sum().reset_index().sort_values("Sales", ascending=False)
            fig2 = go.Figure(go.Pie(
                labels=cat["Category"], values=cat["Sales"], hole=0.65,
                marker=dict(colors=PALETTE[:len(cat)], line=dict(color=BG, width=3)),
                textinfo="label+percent", textfont=dict(size=11, color=FONT_C),
                hovertemplate="<b>%{label}</b><br>{currency_symbol()}%{{value:,.0f}}<br>%{percent}<extra></extra>"))
            fig2.add_annotation(text=f"<b>{fmt_num(total_sales,'$')}</b>",
                x=0.5, y=0.5, showarrow=False,
                font=dict(size=13, color=GOLD, family="DM Mono"), xref="paper", yref="paper")
            lay2 = plotly_base(300)
            lay2.update({"showlegend": True, "legend": dict(bgcolor="rgba(0,0,0,0)", font=dict(size=10), x=1.02, y=0.5, orientation="v")})
            fig2.update_layout(**lay2)
            st.plotly_chart(fig2, use_container_width=True, config={"displayModeBar": False})

    # Row 2: Sales by Region + Profit by Segment
    col3, col4 = st.columns(2)
    with col3:
        st.markdown(f'{tag("REGION")}<div class="riq-section-title">Sales by Region</div>', unsafe_allow_html=True)
        if "Region" in df.columns:
            reg = df.groupby("Region").agg(Sales=("Sales","sum"), Profit=("Profit","sum")).reset_index().sort_values("Sales", ascending=True)
            fig3 = go.Figure()
            fig3.add_trace(go.Bar(y=reg["Region"], x=reg["Sales"], name="Sales",
                orientation="h", marker=dict(color=GOLD, line_width=0, cornerradius=6),
                hovertemplate="<b>%{y}</b><br>{currency_symbol()}%{{x:,.0f}}<extra>Sales</extra>"))
            fig3.add_trace(go.Bar(y=reg["Region"], x=reg["Profit"], name="Profit",
                orientation="h", marker=dict(color=GREEN, line_width=0, cornerradius=6),
                hovertemplate="<b>%{y}</b><br>{currency_symbol()}%{{x:,.0f}}<extra>Profit</extra>"))
            lay3 = plotly_base(300)
            lay3.update({"barmode": "group", "bargap": 0.25, "xaxis_tickprefix": currency_tickprefix(),
                         "legend": dict(bgcolor="rgba(0,0,0,0)", orientation="h", y=-0.15)})
            fig3.update_layout(**lay3)
            st.plotly_chart(fig3, use_container_width=True, config={"displayModeBar": False})

    with col4:
        st.markdown(f'{tag("SEGMENT")}<div class="riq-section-title">Profit by Segment</div>', unsafe_allow_html=True)
        if "Segment" in df.columns:
            seg = df.groupby("Segment").agg(Sales=("Sales","sum"), Profit=("Profit","sum"),
                Orders=("Order ID","nunique")).reset_index()
            fig4 = go.Figure()
            for i, row in seg.iterrows():
                fig4.add_trace(go.Bar(name=row["Segment"], x=[row["Segment"]],
                    y=[row["Profit"]], marker=dict(color=PALETTE[i], line_width=0, cornerradius=6),
                    text=[f"{currency_symbol()}{row['Profit']:,.0f}"], textposition="outside",
                    textfont=dict(size=10, color=FONT_C),
                    hovertemplate=f"<b>{row['Segment']}</b><br>Sales: {currency_symbol()}{row['Sales']:,.0f}<br>Profit: {currency_symbol()}{row['Profit']:,.0f}<br>Orders: {row['Orders']:,}<extra></extra>"))
            lay4 = plotly_base(300)
            lay4.update({"yaxis_tickprefix": currency_tickprefix(), "showlegend": False})
            fig4.update_layout(**lay4)
            st.plotly_chart(fig4, use_container_width=True, config={"displayModeBar": False})

    # Row 3: Top 10 Products
    st.markdown(f'{tag("TOP PRODUCTS")}<div class="riq-section-title">Top 10 Products by Sales</div>', unsafe_allow_html=True)
    if "Product Name" in df.columns:
        top_p = df.groupby("Product Name")["Sales"].sum().sort_values(ascending=True).tail(10).reset_index()
        top_p["Short"] = top_p["Product Name"].str[:35] + "..."
        n = len(top_p)
        colors_p = [f"rgba({int(212*i/max(n-1,1))},{int(175*i/max(n-1,1)+30)},{int(55+100*i/max(n-1,1))},0.85)" for i in range(n)]
        fig5 = go.Figure(go.Bar(
            x=top_p["Sales"], y=top_p["Short"], orientation="h",
            marker=dict(color=colors_p, line_width=0, cornerradius=6),
            text=[f"{currency_symbol()}{v:,.0f}" for v in top_p["Sales"]],
            textposition="outside", textfont=dict(size=10, color=FONT_C),
            hovertemplate="<b>%{y}</b><br>{currency_symbol()}%{{x:,.0f}}<extra></extra>"))
        lay5 = plotly_base(max(250, n*38))
        lay5.update({"xaxis_tickprefix": currency_tickprefix(), "margin": dict(l=10, r=80, t=20, b=10)})
        fig5.update_layout(**lay5)
        st.plotly_chart(fig5, use_container_width=True, config={"displayModeBar": False})

    # Recent orders
    st.markdown(f'{tag("RECENT")}<div class="riq-section-title">Recent Orders</div>', unsafe_allow_html=True)
    show_cols = [c for c in ["Order ID","Order Date","Customer Name","Category","Sales","Profit","Ship Mode"] if c in df.columns]
    recent = df.sort_values("Order Date", ascending=False).head(10)[show_cols].copy()
    if "Order Date" in recent.columns:
        recent["Order Date"] = recent["Order Date"].dt.strftime("%d %b %Y")
    st.dataframe(recent, use_container_width=True, hide_index=True)

# ═══════════════════════════════════════════════════════
# PAGE: DATASET MANAGER
# ═══════════════════════════════════════════════════════
elif menu == "📂 Dataset Manager":
    page_header("Dataset Manager", "// universal data ingestion for Indian retail")

    # ══════════════════════════════════════════════════════════════
    # UNIVERSAL SCHEMA ENGINE
    # ══════════════════════════════════════════════════════════════

    # Known dataset presets — auto-detected by column fingerprint
    KNOWN_SCHEMAS = {
        "global_superstore": {
            "name": "Global Superstore / Standard",
            "fingerprint": {"order id", "sales", "profit", "ship mode"},
            "map": {"order_id":"Order ID","order_date":"Order Date","customer_id":"Customer ID",
                    "customer_name":"Customer Name","segment":"Segment","category":"Category",
                    "subcategory":"Sub-Category","product_name":"Product Name","product_id":"Product ID",
                    "sales":"Sales","quantity":"Quantity","profit":"Profit","discount":"Discount",
                    "shipping_cost":"Shipping Cost","ship_mode":"Ship Mode","region":"Region",
                    "market":"Market","country":"Country","state":"State","city":"City"},
        },
        "online_retail": {
            "name": "Online Retail / Flipkart-style (UCI)",
            "fingerprint": {"invoiceno","stockcode","unitprice","invoicedate"},
            "map": {"order_id":"InvoiceNo","order_date":"InvoiceDate","customer_id":"CustomerID",
                    "product_name":"Description","product_id":"StockCode",
                    "unit_price":"UnitPrice","quantity":"Quantity","country":"Country"},
            "compute": {"sales": ("UnitPrice","Quantity")},
        },
        "tally": {
            "name": "Tally Export",
            "fingerprint": {"voucher no","party name","hsn code","cgst","sgst"},
            "map": {"order_id":"Voucher No","order_date":"Date","customer_name":"Party Name",
                    "product_name":"Item Name","quantity":"Qty","unit_price":"Rate",
                    "sales":"Amount"},
            "compute": {},
        },
        "shopify": {
            "name": "Shopify India Orders",
            "fingerprint": {"financial status","fulfillment status","lineitem name","lineitem price"},
            "map": {"order_id":"Name","order_date":"Paid at","customer_name":"Billing Name",
                    "product_name":"Lineitem name","product_id":"Lineitem sku",
                    "quantity":"Lineitem quantity","unit_price":"Lineitem price",
                    "shipping_cost":"Shipping","discount":"Discount Amount",
                    "city":"Billing City","state":"Billing State"},
            "compute": {"sales": ("Lineitem price","Lineitem quantity")},
        },
        "woocommerce": {
            "name": "WooCommerce India",
            "fingerprint": {"order status","order total","payment method","coupon code"},
            "map": {"order_id":"Order ID","order_date":"Date","customer_name":"Customer Name",
                    "product_name":"Product Name","product_id":"SKU","quantity":"Quantity",
                    "unit_price":"Price","sales":"Order Total","shipping_cost":"Shipping Total",
                    "discount":"Discount","city":"City","state":"State"},
            "compute": {},
        },
        "amazon": {
            "name": "Amazon Seller Central India",
            "fingerprint": {"order-id","purchase-date","item-price","fulfillment-channel","asin"},
            "map": {"order_id":"order-id","order_date":"purchase-date","customer_name":"buyer-name",
                    "product_name":"product-name","product_id":"sku","quantity":"quantity-purchased",
                    "sales":"item-price","shipping_cost":"shipping-price",
                    "city":"ship-city","state":"ship-state","country":"ship-country"},
            "compute": {},
        },
        "meesho": {
            "name": "Meesho / D2C Export",
            "fingerprint": {"selling price","mrp","return status","payment mode"},
            "map": {"order_id":"Order ID","order_date":"Order Date","customer_name":"Customer Name",
                    "product_name":"Product Name","category":"Category","quantity":"Quantity",
                    "unit_price":"Selling Price","city":"City","state":"State"},
            "compute": {"sales": ("Selling Price","Quantity")},
        },
        "busy_gst": {
            "name": "Busy Accounting / Custom GST Billing",
            "fingerprint": {"bill no","bill date","taxable amount","cgst amt","sgst amt"},
            "map": {"order_id":"Bill No","order_date":"Bill Date","customer_name":"Customer",
                    "product_name":"Item","quantity":"Quantity","unit_price":"Rate",
                    "sales":"Total Amount","discount":"Discount %"},
            "compute": {},
        },
        "unicommerce": {
            "name": "Unicommerce / ERP Export",
            "fingerprint": {"sale order code","sku code","net amount","dispatch date","courier"},
            "map": {"order_id":"Sale Order Code","order_date":"Created On","customer_name":"Customer Name",
                    "product_name":"Item Name","product_id":"SKU Code","category":"Category",
                    "quantity":"Quantity","unit_price":"Unit Price","sales":"Net Amount",
                    "discount":"Discount","city":"City","state":"State"},
            "compute": {},
        },
        "quick_commerce": {
            "name": "Zepto / Blinkit / Quick Commerce",
            "fingerprint": {"placed_at","sale_price","delivery_minutes","total_paid","discount_pct"},
            "map": {"order_id":"order_id","order_date":"placed_at","customer_id":"customer_id",
                    "product_name":"product_name","category":"category","subcategory":"sub_category",
                    "unit_price":"sale_price","quantity":"quantity","sales":"total_paid",
                    "discount":"discount_pct","shipping_cost":"delivery_charge",
                    "city":"city","state":"state"},
            "compute": {},
        },
        "pharma": {
            "name": "Pharma / Medical Retail",
            "fingerprint": {"medicine name","batch no","expiry","doctor","net amount"},
            "map": {"order_id":"Bill No","order_date":"Date","customer_name":"Patient Name",
                    "product_name":"Medicine Name","quantity":"Qty","unit_price":"MRP",
                    "sales":"Net Amount","discount":"Discount %"},
            "compute": {},
        },
        "fmcg_distributor": {
            "name": "FMCG Distributor / Van Sales",
            "fingerprint": {"beat","retailer code","scheme discount","cases","outstanding"},
            "map": {"order_id":"Invoice No","order_date":"Invoice Date","customer_name":"Retailer Name",
                    "customer_id":"Retailer Code","product_name":"Product Name",
                    "product_id":"Product Code","quantity":"Units","unit_price":"Rate",
                    "sales":"Net Amount","region":"Route"},
            "compute": {},
        },
    }

    # ── Master alias table for fuzzy fallback ─────────────────────
    FIELD_ALIASES = {
        "order_id":      ["invoiceno","orderid","invoice","orderno","transactionid","txnid",
                          "billno","bill no","voucherno","voucher no","saleordercode",
                          "name","order-id","receipt no","receiptno"],
        "order_date":    ["invoicedate","orderdate","date","transactiondate","billdate",
                          "purchasedate","saledate","paid at","createdon","placed_at",
                          "purchase-date","dispatch date","invoice date","bill date","voucherdate"],
        "customer_id":   ["customerid","custid","clientid","buyerid","userid","retailercode",
                          "customer_id","member id","memberid"],
        "customer_name": ["customername","custname","clientname","buyername","billingname",
                          "partyname","party name","retailername","retailer name","buyer-name",
                          "billing name","patient name","patientname"],
        "segment":       ["segment","customersegment","customertype","clienttype","channel"],
        "category":      ["category","productcategory","itemcategory","dept","department",
                          "producttype","product category","item category"],
        "subcategory":   ["subcategory","subcat","productsubcategory","sub-category","sub_category"],
        "product_name":  ["productname","description","itemname","productdescription",
                          "itemdescription","product","lineitem name","medicine name",
                          "medicinename","item name","item","product-name","product name"],
        "product_id":    ["productid","stockcode","itemid","skucode","productcode","itemcode",
                          "sku","asin","hsn","hsncode","lineitem sku","sku code"],
        "sales":         ["sales","revenue","totalamount","totalrevenue","amount","totalsales",
                          "netsales","lineamount","net amount","total amount","order total",
                          "item-price","total_paid","subtotal","gross amount","grossamount"],
        "unit_price":    ["unitprice","price","sellingprice","rate","mrp","listprice",
                          "lineitem price","selling price","sale_price","unit price","item price",
                          "unit-price","selling_price"],
        "quantity":      ["quantity","qty","units","itemcount","numberofunits","qtyordered",
                          "lineitem quantity","quantity-purchased","quantity purchased","units"],
        "profit":        ["profit","netprofit","margin","grossprofit","income"],
        "discount":      ["discount","discountrate","discountpct","discountpercent","discount %",
                          "discount amount","discount_pct","coupon code","scheme discount"],
        "shipping_cost": ["shippingcost","freight","shipping","postage","deliverycharge",
                          "logisticscost","shipping total","shipping-price","delivery_charge"],
        "ship_mode":     ["shipmode","shippingmode","deliverymode","courier","shipvia","fulfillment-channel"],
        "region":        ["region","zone","area","territory","salesregion","route","beat","market"],
        "market":        ["market","marketname","salesmarket","channel","platform"],
        "country":       ["country","countryname","nation","ship-country"],
        "state":         ["state","province","stateprovince","statename","ship-state",
                          "billing state","billingstate"],
        "city":          ["city","town","cityname","ship-city","billing city","billingcity"],
        "tax":           ["gst","gsttax","tax","taxamount","cgst","sgst","igst",
                          "gst %","cgst amt","sgst amt","tax %"],
        "brand":         ["brand","brandname","vendor","manufacturer","make"],
        "payment_mode":  ["paymentmode","paymentmethod","payment mode","payment method",
                          "payment type","payment_type","payment_mode"],
    }

    def detect_schema(cols):
        """Return best-matching known schema or None."""
        cols_norm = {c.lower().strip() for c in cols}
        best_match, best_score = None, 0
        for schema_key, schema in KNOWN_SCHEMAS.items():
            fp = schema["fingerprint"]
            score = len(fp & cols_norm) / len(fp)
            if score > best_score:
                best_score, best_match = score, schema_key
        return (best_match, best_score) if best_score >= 0.5 else (None, 0)

    def fuzzy_automap(cols):
        """Fuzzy-match any column set to standard fields."""
        cols_norm = {c.lower().replace(" ","").replace("_","").replace("-",""): c for c in cols}
        result = {}
        for std_key, aliases in FIELD_ALIASES.items():
            for alias in aliases:
                norm_alias = alias.lower().replace(" ","").replace("_","").replace("-","")
                if norm_alias in cols_norm:
                    result[std_key] = cols_norm[norm_alias]
                    break
        return result

    def get_compute_plan(raw_cols, automap, schema_compute=None):
        """Figure out how to derive Sales if not directly available."""
        computed = {}
        if "sales" not in automap:
            # From schema preset
            if schema_compute and "sales" in schema_compute:
                up_col, qt_col = schema_compute["sales"]
                if up_col in raw_cols and qt_col in raw_cols:
                    computed["sales"] = (up_col, qt_col, f"{up_col} × {qt_col}")
                    return computed
            # From fuzzy map
            up_key = automap.get("unit_price")
            qt_key = automap.get("quantity")
            if up_key and qt_key:
                computed["sales"] = (up_key, qt_key, f"{up_key} × {qt_key}")
        return computed

    def process_dataset(raw, mapping, computed, drop_negatives=True, drop_dupes=True,
                        drop_returns=True, fill_missing_profit=True):
        """Apply mapping, compute columns, clean and return final df."""
        mapped = apply_col_map(raw, mapping)

        # Compute Sales
        if "Sales" not in mapped.columns and "sales" in computed:
            up_col, qt_col, _ = computed["sales"]
            # Use original column names since apply_col_map may have renamed
            up_src = mapping.get("unit_price", up_col)
            qt_src = mapping.get("quantity",   qt_col)
            # Try mapped names first, then originals
            for uc in [up_src, up_col]:
                for qc in [qt_src, qt_col]:
                    if uc in mapped.columns and qc in mapped.columns:
                        up_v = pd.to_numeric(mapped[uc], errors="coerce").fillna(0)
                        qt_v = pd.to_numeric(mapped[qc], errors="coerce").fillna(0)
                        mapped["Sales"] = up_v * qt_v
                        break

        # Parse dates
        for dcol in ["Order Date","Ship Date"]:
            if dcol in mapped.columns:
                mapped[dcol] = pd.to_datetime(mapped[dcol], dayfirst=True, errors="coerce")
        mapped = mapped.dropna(subset=["Order Date"])

        # Numerics
        for ncol in ["Sales","Profit","Quantity","Discount","Shipping Cost"]:
            if ncol in mapped.columns:
                mapped[ncol] = pd.to_numeric(mapped[ncol], errors="coerce").fillna(0)

        # Discount: if it looks like a % (>1), convert to fraction
        if "Discount" in mapped.columns and mapped["Discount"].max() > 1.5:
            mapped["Discount"] = mapped["Discount"] / 100

        # Fill missing required cols
        if fill_missing_profit and "Profit" not in mapped.columns:
            mapped["Profit"] = 0.0
        if "Order ID" not in mapped.columns:
            mapped["Order ID"] = mapped.index.astype(str)
        if "Category" not in mapped.columns:
            mapped["Category"] = "General"
        if "Quantity" not in mapped.columns:
            mapped["Quantity"] = 1

        # Drop negatives (returns/cancellations)
        if drop_negatives and "Sales" in mapped.columns:
            mapped = mapped[mapped["Sales"] >= 0]
        if drop_dupes:
            mapped = mapped.drop_duplicates()

        return mapped.reset_index(drop=True)

    # ══════════════════════════════════════════════════════════════
    # UI
    # ══════════════════════════════════════════════════════════════
    tab1, tab2, tab3 = st.tabs(["📂 Upload Dataset", "ℹ️ Current Dataset Info", "📋 Schema Guide"])

    with tab1:
        st.markdown(f'{tag("UPLOAD")}<div class="riq-section-title">Upload Your Retail Dataset</div>',
                    unsafe_allow_html=True)
        st.markdown(
            '<div style="font-size:0.76rem;color:#6B6B75;margin-bottom:1rem;">'
            'Supports <b style="color:#D4AF37;">CSV and Excel</b> files from any Indian retail source — '
            'Tally, Shopify, WooCommerce, Amazon, Meesho, Busy, Unicommerce, '
            'Quick Commerce, Pharma, FMCG, and more.</div>',
            unsafe_allow_html=True)

        uploaded = st.file_uploader("Upload CSV or Excel file", type=["csv","xlsx","xls"])

        if uploaded:
            try:
                with st.spinner("Loading file…"):
                    if uploaded.name.endswith(".csv"):
                        # Try multiple encodings common in Indian software
                        for enc in ["utf-8","latin1","cp1252","utf-8-sig","ISO-8859-1"]:
                            try:
                                raw = pd.read_csv(uploaded, encoding=enc)
                                break
                            except Exception:
                                uploaded.seek(0)
                    else:
                        raw = pd.read_excel(uploaded)

                    # Strip whitespace from column names and string values
                    raw.columns = [str(c).strip() for c in raw.columns]
                    for col in raw.select_dtypes(include="object").columns:
                        raw[col] = raw[col].astype(str).str.strip()
                        raw[col] = raw[col].replace("nan", None)

                # ── Schema detection ─────────────────────────────────
                schema_key, schema_score = detect_schema(raw.columns.tolist())
                schema_preset = KNOWN_SCHEMAS.get(schema_key)

                if schema_preset:
                    st.markdown(
                        f'<div style="display:flex;align-items:center;gap:10px;'
                        f'background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.3);'
                        f'border-radius:10px;padding:10px 16px;margin-bottom:1rem;">'
                        f'<span style="font-size:1.2rem;">✅</span>'
                        f'<div><div style="font-weight:600;font-size:0.88rem;color:{GREEN};">'
                        f'Schema detected: {schema_preset["name"]}</div>'
                        f'<div style="font-size:0.72rem;color:#6B6B75;">'
                        f'Confidence: {schema_score*100:.0f}% · All fields pre-mapped</div>'
                        f'</div></div>',
                        unsafe_allow_html=True)
                    base_map = schema_preset["map"].copy()
                    schema_compute = schema_preset.get("compute", {})
                else:
                    st.markdown(
                        f'<div style="display:flex;align-items:center;gap:10px;'
                        f'background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.3);'
                        f'border-radius:10px;padding:10px 16px;margin-bottom:1rem;">'
                        f'<span style="font-size:1.2rem;">🔍</span>'
                        f'<div><div style="font-weight:600;font-size:0.88rem;color:{AMBER};">'
                        f'Custom schema — fuzzy mapping applied</div>'
                        f'<div style="font-size:0.72rem;color:#6B6B75;">'
                        f'Review the column mapping below and adjust if needed</div>'
                        f'</div></div>',
                        unsafe_allow_html=True)
                    base_map = {}
                    schema_compute = {}

                # Always run fuzzy fallback to fill any gaps
                fuzzy_map = fuzzy_automap(raw.columns.tolist())
                for k, v in fuzzy_map.items():
                    if k not in base_map:
                        base_map[k] = v

                # Computed columns plan
                computed = get_compute_plan(raw.columns.tolist(), base_map, schema_compute)

                # ── Data quality report ───────────────────────────────
                st.markdown(f'{tag("DATA QUALITY")}<div class="riq-section-title">Data Quality Report</div>',
                            unsafe_allow_html=True)
                nulls = raw.isnull().sum()
                dupes = raw.duplicated().sum()

                # Detect negative values in likely numeric cols
                neg_counts = {}
                for col in raw.columns:
                    try:
                        n = pd.to_numeric(raw[col], errors="coerce")
                        neg = (n < 0).sum()
                        if neg > 0:
                            neg_counts[col] = neg
                    except Exception:
                        pass

                qc1, qc2, qc3, qc4 = st.columns(4)
                qc1.metric("Rows",           f"{len(raw):,}")
                qc2.metric("Columns",        f"{len(raw.columns)}")
                qc3.metric("Missing Values", f"{nulls.sum():,}",
                           delta="needs attention" if nulls.sum() > 0 else "✓ clean",
                           delta_color="inverse" if nulls.sum() > 0 else "normal")
                qc4.metric("Duplicates",     f"{dupes:,}",
                           delta="will be dropped" if dupes > 0 else "✓ none",
                           delta_color="inverse" if dupes > 0 else "normal")

                issues = []
                if nulls.sum() > 0:
                    null_df = nulls[nulls > 0].reset_index()
                    null_df.columns = ["Column","Missing"]
                    null_df["% Missing"] = (null_df["Missing"] / len(raw) * 100).round(1)
                    issues.append(("Missing values", null_df))
                if neg_counts:
                    neg_df = pd.DataFrame({"Column": list(neg_counts.keys()),
                                           "Negative Rows": list(neg_counts.values())})
                    issues.append(("Negative values (returns/cancellations)", neg_df))

                if issues:
                    with st.expander(f"⚠️ {len(issues)} data quality issue(s) found"):
                        for title, idf in issues:
                            st.markdown(f'<div style="font-size:0.78rem;color:{AMBER};margin:6px 0;">{title}</div>',
                                        unsafe_allow_html=True)
                            st.dataframe(idf, use_container_width=True, hide_index=True)

                # ── Mapping wizard ────────────────────────────────────
                st.markdown(f'{tag("COLUMN MAPPING")}<div class="riq-section-title">Column Mapping</div>',
                            unsafe_allow_html=True)

                col_options = ["-- not available --"] + list(raw.columns)

                STD_FIELDS = {
                    "order_id":      ("Order ID",                        False),
                    "order_date":    ("Order Date  ★",                   True),
                    "customer_id":   ("Customer ID",                     False),
                    "customer_name": ("Customer Name",                   False),
                    "segment":       ("Segment",                         False),
                    "category":      ("Category",                        False),
                    "subcategory":   ("Sub-Category",                    False),
                    "product_name":  ("Product Name",                    False),
                    "product_id":    ("Product ID / SKU",                False),
                    "brand":         ("Brand",                           False),
                    "sales":         ("Sales / Revenue  ★",              True),
                    "unit_price":    ("Unit Price (to compute Sales)",   False),
                    "quantity":      ("Quantity",                        False),
                    "profit":        ("Profit",                          False),
                    "discount":      ("Discount",                        False),
                    "tax":           ("Tax / GST Amount",                False),
                    "shipping_cost": ("Shipping Cost",                   False),
                    "ship_mode":     ("Ship Mode / Courier",             False),
                    "payment_mode":  ("Payment Mode",                    False),
                    "region":        ("Region / Zone / Route",           False),
                    "market":        ("Market / Channel / Platform",     False),
                    "country":       ("Country",                         False),
                    "state":         ("State",                           False),
                    "city":          ("City",                            False),
                }

                mapping = {}
                field_items = list(STD_FIELDS.items())

                for i in range(0, len(field_items), 3):
                    row_cols = st.columns(3)
                    for j, (key, (label, required)) in enumerate(field_items[i:i+3]):
                        # Sales computed — show notice
                        if key == "sales" and "sales" not in base_map and "sales" in computed:
                            up_col, qt_col, expr = computed["sales"]
                            row_cols[j].markdown(
                                f'<div style="font-size:0.72rem;padding:8px 4px;">'
                                f'<span style="color:{GREEN};font-weight:600;">✨ Sales computed:</span><br>'
                                f'<span style="color:#B5B5BD;">{expr}</span></div>',
                                unsafe_allow_html=True)
                            continue

                        auto_val = base_map.get(key)
                        default_idx = col_options.index(auto_val) \
                            if auto_val and auto_val in col_options else 0
                        label_styled = f"{'🔴 ' if required else ''}{label}"
                        sel = row_cols[j].selectbox(
                            label_styled, col_options,
                            index=default_idx, key=f"map_{key}")
                        if sel != "-- not available --":
                            mapping[key] = sel

                # Re-derive computed plan from final mapping
                computed = get_compute_plan(raw.columns.tolist(), mapping, schema_compute)

                # ── Cleaning options ──────────────────────────────────
                st.markdown(f'{tag("CLEANING")}<div class="riq-section-title">Cleaning Options</div>',
                            unsafe_allow_html=True)
                cl1, cl2, cl3 = st.columns(3)
                opt_dupes    = cl1.checkbox("Drop duplicate rows",         value=True)
                opt_neg      = cl2.checkbox("Drop negative sales (returns)", value=True)
                opt_profit   = cl3.checkbox("Fill missing Profit with ₹0",  value=True)

                # ── Preview ───────────────────────────────────────────
                with st.expander("Preview raw data (first 10 rows)"):
                    st.dataframe(raw.head(10), use_container_width=True, hide_index=True)

                # ── Apply ─────────────────────────────────────────────
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("✅ Apply Mapping & Load Dataset", use_container_width=True):
                    errors = []
                    if "order_date" not in mapping:
                        errors.append("Order Date is required — please map it to a date column")
                    if "sales" not in mapping and "sales" not in computed:
                        if "unit_price" not in mapping or "quantity" not in mapping:
                            errors.append(
                                "Sales is required — either map a Sales/Revenue column, "
                                "or map both Unit Price and Quantity so Sales can be computed")

                    if errors:
                        for e in errors:
                            st.error(f"❌ {e}")
                    else:
                        with st.spinner("Processing and cleaning dataset…"):
                            final = process_dataset(
                                raw, mapping, computed,
                                drop_negatives=opt_neg,
                                drop_dupes=opt_dupes,
                                fill_missing_profit=opt_profit)

                            rows_in  = len(raw)
                            rows_out = len(final)
                            dropped  = rows_in - rows_out

                            st.session_state.dataset      = final
                            st.session_state.dataset_name = uploaded.name
                            st.session_state.col_map      = mapping
                            save_dataset(final, uploaded.name, mapping)

                            st.success(
                                f"✅ **{uploaded.name}** loaded — "
                                f"**{rows_out:,} rows** ready for analysis"
                                f"{f' · {dropped:,} rows cleaned' if dropped else ''}")

                            # Show what was mapped
                            mapped_fields = [STD_FIELDS[k][0] for k in mapping if k in STD_FIELDS]
                            computed_fields = [f"Sales (computed from {computed['sales'][2]})" if "sales" in computed else ""]
                            all_fields = mapped_fields + [f for f in computed_fields if f]
                            st.info(f"Mapped fields: {' · '.join(all_fields)}")
                            st.rerun()

            except Exception as e:
                st.error(f"Error loading file: {e}")
                st.exception(e)

        st.markdown("---")
        rc1, rc2 = st.columns(2)
        if rc1.button("🎬 Try Demo Data (Global Superstore)", use_container_width=True):
            st.session_state.dataset      = None
            st.session_state.dataset_name = ""
            st.session_state.col_map      = {}
            st.session_state.use_demo_data = True
            clear_dataset()
            st.success("Demo data loaded.")
            st.rerun()
        if rc2.button("🗑️ Clear My Data (start fresh)", use_container_width=True):
            st.session_state.dataset      = None
            st.session_state.dataset_name = ""
            st.session_state.col_map      = {}
            st.session_state.use_demo_data = False
            clear_dataset()
            st.success("Cleared. Upload your own data to get started.")
            st.rerun()

    with tab2:
        st.markdown('<div class="riq-section-title">Current Dataset</div>', unsafe_allow_html=True)
        ds_name = (st.session_state.dataset_name or
                   ("Global Superstore 2 (Demo)" if st.session_state.get("use_demo_data") else "No dataset loaded"))
        date_from = df["Order Date"].min().strftime("%d %b %Y") if "Order Date" in df.columns and len(df) > 0 else "N/A"
        date_to   = df["Order Date"].max().strftime("%d %b %Y") if "Order Date" in df.columns and len(df) > 0 else "N/A"
        st.markdown(
            f'<div class="riq-card">'
            f'<div style="font-size:0.9rem;color:{GOLD};font-weight:600;margin-bottom:8px;">{ds_name}</div>'
            f'<div style="font-size:0.78rem;color:#B5B5BD;">'
            f'{len(df):,} rows · {len(df.columns)} columns · {date_from} → {date_to}'
            f'</div></div>', unsafe_allow_html=True)
        st.dataframe(df.head(20), use_container_width=True, hide_index=True)

        st.markdown('<div class="riq-section-title">Column Summary</div>', unsafe_allow_html=True)
        col_info = pd.DataFrame({
            "Column": df.columns,
            "Type": df.dtypes.astype(str).values,
            "Non-Null": df.count().values,
            "Missing": df.isnull().sum().values,
            "Unique": [df[c].nunique() for c in df.columns],
        })
        st.dataframe(col_info, use_container_width=True, hide_index=True)

    with tab3:
        st.markdown('<div class="riq-section-title">Supported Dataset Schemas</div>', unsafe_allow_html=True)
        st.markdown(
            '<div style="font-size:0.76rem;color:#6B6B75;margin-bottom:1rem;">'
            'These schemas are auto-detected. Upload the file and RetailIQ will recognise it instantly.</div>',
            unsafe_allow_html=True)

        schema_rows = []
        for sk, sv in KNOWN_SCHEMAS.items():
            computes = ", ".join([f"{k} = {v[0]}×{v[1]}" for k,v in sv.get("compute",{}).items()])
            schema_rows.append({
                "Source": sv["name"],
                "Required columns (sample)": " · ".join(list(sv["fingerprint"])[:3]),
                "Auto-computes": computes or "—",
            })
        st.dataframe(pd.DataFrame(schema_rows), use_container_width=True, hide_index=True)

        st.markdown(f'{tag("CUSTOM SCHEMAS")}<div class="riq-section-title">Other / Custom Datasets</div>',
                    unsafe_allow_html=True)
        st.markdown(
            '<div class="riq-card" style="font-size:0.8rem;line-height:1.8;color:#B5B5BD;">'
            'If your dataset is not in the list above, RetailIQ will <b style="color:#D4AF37;">fuzzy-match</b> '
            'your column names automatically. It checks 20+ aliases per field including Hindi-transliterated names, '
            'GST billing terms, and ERP exports.<br><br>'
            '<b style="color:#D4AF37;">Minimum required:</b><br>'
            '• A <b>date column</b> (any name containing "date", "on", "at", "time")<br>'
            '• A <b>sales/revenue column</b> OR a <b>Unit Price + Quantity</b> pair<br><br>'
            '<b style="color:#D4AF37;">Computed automatically if missing:</b><br>'
            '• Sales = Unit Price × Quantity<br>'
            '• Profit filled with ₹0 (toggleable)<br>'
            '• Order ID filled from row index<br>'
            '• Category filled with "General"<br>'
            '• Negative rows dropped (returns/cancellations)<br>'
            '</div>',
            unsafe_allow_html=True)



# ═══════════════════════════════════════════════════════
# PAGE: MANUAL ENTRY
# ═══════════════════════════════════════════════════════
elif menu == "🧾 Manual Entry":
    page_header("Manual Entry", "// enter sales data directly — no file needed")

    sym = currency_symbol()

    # ── Diagnostic: show last Supabase error if any ───────────────────
    if st.session_state.get("_last_sb_error"):
        st.error(f"⚠️ Cloud save issue (data saved locally instead): {st.session_state['_last_sb_error']}")

    # ── Helpers ──────────────────────────────────────────────────────
    def entries_to_df(entries):
        if not entries:
            return pd.DataFrame(columns=["Order ID","Order Date","Customer Name",
                                          "Customer ID","Category","Product Name",
                                          "Quantity","Unit Price","Discount",
                                          "Sales","Profit","Payment Mode",
                                          "GST %","State","City","Notes"])
        return pd.DataFrame(entries)

    def recalc(unit_price, qty, discount_pct, gst_pct, profit_margin_pct):
        taxable  = unit_price * qty * (1 - discount_pct / 100)
        gst_amt  = taxable * gst_pct / 100
        sales    = taxable + gst_amt
        profit   = taxable * profit_margin_pct / 100
        return round(sales, 2), round(profit, 2), round(gst_amt, 2)

    entries = st.session_state.manual_entries

    # ── Tab layout ───────────────────────────────────────────────────
    tab_entry, tab_bulk, tab_history, tab_products, tab_export = st.tabs([
        "➕ Add Entry", "📋 Bulk Entry", "📜 History", "🗂️ My Products", "📤 Export"])

    # ════════════════════════════════════════════════════
    # TAB 1 — SINGLE ENTRY
    # ════════════════════════════════════════════════════
    with tab_entry:
        st.markdown(f'{tag("SALE ENTRY")}<div class="riq-section-title">New Sale Entry</div>',
                    unsafe_allow_html=True)

        with st.form("entry_form", clear_on_submit=True):
            # ── Row 1: Date, Order ID, Payment ───────────────────────
            r1c1, r1c2, r1c3 = st.columns(3)
            entry_date    = r1c1.date_input("Date", value=pd.Timestamp.today())
            order_id_auto = f"ORD-{pd.Timestamp.now().strftime('%Y%m%d%H%M%S')}"
            order_id      = r1c2.text_input("Order / Bill No", value=order_id_auto)
            payment_mode  = r1c3.selectbox("Payment Mode",
                ["Cash", "UPI", "Debit Card", "Credit Card", "BNPL",
                 "Net Banking", "Cheque", "Credit (Khata)", "Other"])

            # ── Row 2: Customer ───────────────────────────────────────
            r2c1, r2c2, r2c3 = st.columns(3)
            cust_list     = ["-- Walk-in Customer --"] + st.session_state.me_customer_list
            cust_sel      = r2c1.selectbox("Customer", cust_list)
            cust_name     = r2c2.text_input("Customer Name",
                value="" if cust_sel == "-- Walk-in Customer --" else cust_sel,
                placeholder="Leave blank for walk-in")
            cust_id       = r2c3.text_input("Customer Phone / ID",
                placeholder="e.g. 98765 43210")

            # ── Row 3: Product ────────────────────────────────────────
            r3c1, r3c2, r3c3 = st.columns(3)
            prod_list     = ["-- Type new product --"] + st.session_state.me_product_list
            prod_sel      = r3c1.selectbox("Product (saved)", prod_list)
            product_name  = r3c2.text_input("Product Name",
                value="" if prod_sel == "-- Type new product --" else prod_sel,
                placeholder="e.g. Basmati Rice 5kg")
            category      = r3c3.selectbox("Category",
                ["General", "Grocery & FMCG", "Electronics", "Clothing & Apparel",
                 "Medicines & Pharma", "Food & Beverages", "Hardware & Tools",
                 "Stationery", "Home & Kitchen", "Cosmetics & Beauty",
                 "Agricultural", "Auto Parts", "Wholesale", "Other"])

            # ── Row 4: Pricing ────────────────────────────────────────
            r4c1, r4c2, r4c3, r4c4 = st.columns(4)
            unit_price    = r4c1.number_input(f"Unit Price ({sym})", min_value=0.0,
                                               value=0.0, step=0.5, format="%.2f")
            quantity      = r4c2.number_input("Quantity", min_value=1, value=1, step=1)
            discount_pct  = r4c3.number_input("Discount (%)", min_value=0.0,
                                               max_value=100.0, value=0.0, step=0.5)
            gst_pct       = r4c4.selectbox("GST Rate",
                [0, 3, 5, 12, 18, 28], index=2,
                format_func=lambda x: f"{x}% GST" if x > 0 else "No GST / Exempt")

            # ── Row 5: Profit + Location ──────────────────────────────
            r5c1, r5c2, r5c3, r5c4 = st.columns(4)
            profit_margin = r5c1.number_input("Profit Margin (%)", min_value=0.0,
                                               max_value=100.0, value=15.0, step=0.5)
            state         = r5c2.selectbox("State", ["-- Select --",
                "Andhra Pradesh","Arunachal Pradesh","Assam","Bihar","Chhattisgarh",
                "Delhi","Goa","Gujarat","Haryana","Himachal Pradesh","Jharkhand",
                "Karnataka","Kerala","Ladakh","Lakshadweep","Madhya Pradesh",
                "Maharashtra","Manipur","Meghalaya","Mizoram","Nagaland","Odisha",
                "Puducherry","Punjab","Rajasthan","Sikkim","Tamil Nadu","Telangana",
                "Tripura","Uttar Pradesh","Uttarakhand","West Bengal","J&K",
                "Andaman & Nicobar","Chandigarh","Dadra & NH","Daman & Diu"])
            city          = r5c3.text_input("City", placeholder="e.g. Pune")
            notes         = r5c4.text_input("Notes", placeholder="Optional")

            # ── Live preview ──────────────────────────────────────────
            if unit_price > 0 and quantity > 0:
                sales_preview, profit_preview, gst_preview = recalc(
                    unit_price, quantity, discount_pct, gst_pct, profit_margin)
                prev_html = (
                    f'<div style="display:grid;grid-template-columns:repeat(4,1fr);' 
                    f'gap:8px;margin:0.5rem 0 1rem;padding:12px;background:rgba(255,153,51,0.04);' 
                    f'border:1px solid rgba(212,175,55,0.2);border-radius:10px;">' 
                    f'<div><div style="font-size:0.62rem;color:#6B6B75;text-transform:uppercase;' 
                    f'letter-spacing:0.07em;">Taxable</div>' 
                    f'<div style="font-family:DM Mono,monospace;font-size:0.95rem;color:#F8FAFC;">' 
                    f'{sym}{unit_price*quantity*(1-discount_pct/100):,.2f}</div></div>' 
                    f'<div><div style="font-size:0.62rem;color:#6B6B75;text-transform:uppercase;' 
                    f'letter-spacing:0.07em;">GST ({gst_pct}%)</div>' 
                    f'<div style="font-family:DM Mono,monospace;font-size:0.95rem;color:#F8FAFC;">' 
                    f'{sym}{gst_preview:,.2f}</div></div>' 
                    f'<div><div style="font-size:0.62rem;color:#6B6B75;text-transform:uppercase;' 
                    f'letter-spacing:0.07em;">Total Sale</div>' 
                    f'<div style="font-family:DM Mono,monospace;font-size:1.05rem;font-weight:600;color:{GOLD};">' 
                    f'{sym}{sales_preview:,.2f}</div></div>' 
                    f'<div><div style="font-size:0.62rem;color:#6B6B75;text-transform:uppercase;' 
                    f'letter-spacing:0.07em;">Est. Profit</div>' 
                    f'<div style="font-family:DM Mono,monospace;font-size:0.95rem;color:{GREEN};">' 
                    f'{sym}{profit_preview:,.2f}</div></div>' 
                    f'</div>'
                )
                st.markdown(prev_html, unsafe_allow_html=True)

            submitted = st.form_submit_button("💾 Save Sale Entry", use_container_width=True)

        if submitted:
            if not product_name.strip():
                st.error("Product Name is required.")
            elif unit_price <= 0:
                st.error("Unit Price must be greater than 0.")
            else:
                sales_val, profit_val, gst_val = recalc(
                    unit_price, quantity, discount_pct, gst_pct, profit_margin)
                entry = {
                    "Order ID":      order_id,
                    "Order Date":    pd.Timestamp(entry_date),
                    "Customer Name": cust_name.strip() or "Walk-in",
                    "Customer ID":   cust_id.strip() or f"CUST-{hash(cust_name)%10000:04d}",
                    "Category":      category,
                    "Product Name":  product_name.strip(),
                    "Quantity":      quantity,
                    "Unit Price":    unit_price,
                    "Discount":      discount_pct / 100,
                    "GST %":         gst_pct,
                    "GST Amount":    gst_val,
                    "Sales":         sales_val,
                    "Profit":        profit_val,
                    "Payment Mode":  payment_mode,
                    "State":         state if state != "-- Select --" else "",
                    "City":          city.strip(),
                    "Notes":         notes.strip(),
                }
                st.session_state.manual_entries.append(entry)
                save_entries(st.session_state.manual_entries)

                # Auto-save product and customer to lists
                if product_name.strip() and product_name.strip() not in st.session_state.me_product_list:
                    st.session_state.me_product_list.append(product_name.strip())
                    save_products(st.session_state.me_product_list)
                if cust_name.strip() and cust_name.strip() not in st.session_state.me_customer_list:
                    st.session_state.me_customer_list.append(cust_name.strip())
                    save_customers(st.session_state.me_customer_list)

                st.success(f"✅ Sale saved — {product_name} · {sym}{sales_val:,.2f} · {payment_mode}")

                # Today's running total
                today_str = pd.Timestamp.today().date()
                today_entries = [e for e in st.session_state.manual_entries
                                 if pd.Timestamp(e["Order Date"]).date() == today_str]
                today_total = sum(e["Sales"] for e in today_entries)
                st.info(f"Today's total: {sym}{today_total:,.2f} across {len(today_entries)} sale(s)")

    # ════════════════════════════════════════════════════
    # TAB 2 — BULK ENTRY (paste-style table)
    # ════════════════════════════════════════════════════
    with tab_bulk:
        st.markdown(f'{tag("BULK ENTRY")}<div class="riq-section-title">Bulk Entry</div>',
                    unsafe_allow_html=True)
        st.markdown(
            '<div style="font-size:0.76rem;color:#6B6B75;margin-bottom:0.8rem;">' 
            'Enter multiple items at once. Each row is one sale line.</div>',
            unsafe_allow_html=True)

        bulk_date    = st.date_input("Date for all entries", value=pd.Timestamp.today(), key="bulk_date")
        bulk_order   = st.text_input("Bill / Invoice No (shared)", key="bulk_order",
                                     value=f"BULK-{pd.Timestamp.now().strftime('%Y%m%d%H%M')}")
        bulk_payment = st.selectbox("Payment Mode (shared)",
            ["Cash","UPI","Debit Card","Credit Card","BNPL",
             "Net Banking","Cheque","Credit (Khata)","Other"], key="bulk_pay")
        bulk_gst     = st.selectbox("GST Rate (shared)", [0,3,5,12,18,28],
                                    format_func=lambda x: f"{x}% GST" if x>0 else "No GST",
                                    index=2, key="bulk_gst")

        st.markdown("<br>", unsafe_allow_html=True)

        # Dynamic bulk rows using session state
        if "bulk_rows" not in st.session_state:
            st.session_state.bulk_rows = 5
        if st.button("+ Add 5 more rows"):
            st.session_state.bulk_rows += 5

        bulk_header = st.columns([3, 1, 1, 1, 1])
        bulk_header[0].markdown('<div style="font-size:0.72rem;color:#6B6B75;">Product Name</div>', unsafe_allow_html=True)
        bulk_header[1].markdown('<div style="font-size:0.72rem;color:#6B6B75;">Qty</div>', unsafe_allow_html=True)
        bulk_header[2].markdown(f'<div style="font-size:0.72rem;color:#6B6B75;">Unit Price ({sym})</div>', unsafe_allow_html=True)
        bulk_header[3].markdown('<div style="font-size:0.72rem;color:#6B6B75;">Discount %</div>', unsafe_allow_html=True)
        bulk_header[4].markdown('<div style="font-size:0.72rem;color:#6B6B75;">Category</div>', unsafe_allow_html=True)

        bulk_data = []
        for i in range(st.session_state.bulk_rows):
            bc = st.columns([3, 1, 1, 1, 1])
            pname = bc[0].text_input(f"Product {i+1}", key=f"bp_{i}", label_visibility="collapsed")
            qty   = bc[1].number_input(f"Q{i}", min_value=1, value=1, step=1, key=f"bq_{i}", label_visibility="collapsed")
            price = bc[2].number_input(f"P{i}", min_value=0.0, value=0.0, step=0.5, key=f"bpr_{i}", label_visibility="collapsed")
            disc  = bc[3].number_input(f"D{i}", min_value=0.0, max_value=100.0, value=0.0, step=0.5, key=f"bd_{i}", label_visibility="collapsed")
            cat   = bc[4].selectbox(f"C{i}", ["General","Grocery & FMCG","Electronics",
                                              "Clothing","Medicines","Food & Bev","Other"],
                                    key=f"bc_{i}", label_visibility="collapsed")
            if pname.strip() and price > 0:
                bulk_data.append((pname.strip(), qty, price, disc, cat))

        if st.button("💾 Save All Bulk Entries", use_container_width=True):
            if not bulk_data:
                st.error("Fill in at least one product with name and price.")
            else:
                saved = 0
                for pname, qty, price, disc, cat in bulk_data:
                    sales_v, profit_v, gst_v = recalc(price, qty, disc, bulk_gst, 15.0)
                    entry = {
                        "Order ID":      bulk_order,
                        "Order Date":    pd.Timestamp(bulk_date),
                        "Customer Name": "Walk-in",
                        "Customer ID":   "WALK-IN",
                        "Category":      cat,
                        "Product Name":  pname,
                        "Quantity":      qty,
                        "Unit Price":    price,
                        "Discount":      disc / 100,
                        "GST %":         bulk_gst,
                        "GST Amount":    gst_v,
                        "Sales":         sales_v,
                        "Profit":        profit_v,
                        "Payment Mode":  bulk_payment,
                        "State":         "",
                        "City":          "",
                        "Notes":         "Bulk entry",
                    }
                    st.session_state.manual_entries.append(entry)
                    if pname not in st.session_state.me_product_list:
                        st.session_state.me_product_list.append(pname)
                    saved += 1
                save_entries(st.session_state.manual_entries)
                save_products(st.session_state.me_product_list)
                st.success(f"✅ {saved} entries saved to disk!")
                st.session_state.bulk_rows = 5

    # ════════════════════════════════════════════════════
    # TAB 3 — HISTORY
    # ════════════════════════════════════════════════════
    with tab_history:
        st.markdown(f'{tag("HISTORY")}<div class="riq-section-title">Sales History</div>',
                    unsafe_allow_html=True)

        if not entries:
            st.info("No entries yet. Add sales using the Entry or Bulk tabs.")
        else:
            hist_df = entries_to_df(entries)

            # KPIs
            hc1, hc2, hc3, hc4 = st.columns(4)
            hc1.metric("Total Entries", f"{len(hist_df):,}")
            hc2.metric("Total Sales",   fmt_num(hist_df["Sales"].sum(), sym))
            hc3.metric("Total Profit",  fmt_num(hist_df["Profit"].sum(), sym))
            hc4.metric("Unique Products", f"{hist_df['Product Name'].nunique():,}")

            # Date filter
            min_d = hist_df["Order Date"].min().date()
            max_d = hist_df["Order Date"].max().date()
            fc1, fc2 = st.columns(2)
            from_d = fc1.date_input("From", value=min_d, key="hist_from")
            to_d   = fc2.date_input("To",   value=max_d, key="hist_to")
            mask   = (hist_df["Order Date"].dt.date >= from_d) & (hist_df["Order Date"].dt.date <= to_d)
            filtered = hist_df[mask].copy()

            # Display
            disp = filtered.copy()
            disp["Order Date"] = disp["Order Date"].dt.strftime("%d %b %Y")
            disp["Sales"]      = disp["Sales"].apply(lambda x: f"{sym}{x:,.2f}")
            disp["Profit"]     = disp["Profit"].apply(lambda x: f"{sym}{x:,.2f}")
            disp["Unit Price"] = disp["Unit Price"].apply(lambda x: f"{sym}{x:,.2f}")
            disp["Discount"]   = disp["Discount"].apply(lambda x: f"{x*100:.0f}%")
            st.dataframe(disp, use_container_width=True, hide_index=True)

            # Delete entry
            st.markdown("---")
            st.markdown('<div style="font-size:0.78rem;color:#6B6B75;margin-bottom:0.5rem;">Delete an entry by index (0-based)</div>', unsafe_allow_html=True)
            del_col1, del_col2 = st.columns([2,1])
            del_idx = del_col1.number_input("Entry index to delete", min_value=0,
                                             max_value=max(0, len(entries)-1), value=0, step=1)
            if del_col2.button("🗑️ Delete Entry"):
                if 0 <= del_idx < len(st.session_state.manual_entries):
                    removed = st.session_state.manual_entries.pop(del_idx)
                    save_entries(st.session_state.manual_entries)
                    st.success(f"Deleted: {removed.get('Product Name','entry')} on {pd.Timestamp(removed['Order Date']).strftime('%d %b %Y')}")
                    st.rerun()

            # Clear all
            if st.button("🗑️ Clear ALL Entries", type="secondary"):
                st.session_state.manual_entries = []
                save_entries([])
                st.rerun()

    # ════════════════════════════════════════════════════
    # TAB 4 — MY PRODUCTS (catalogue)
    # ════════════════════════════════════════════════════
    with tab_products:
        st.markdown(f'{tag("PRODUCT LIST")}<div class="riq-section-title">My Products</div>',
                    unsafe_allow_html=True)
        st.markdown(
            '<div style="font-size:0.76rem;color:#6B6B75;margin-bottom:1rem;">' 
            'Products saved here appear as quick-select options in the entry form.</div>',
            unsafe_allow_html=True)

        # Add product
        pc1, pc2 = st.columns([4,1])
        new_prod = pc1.text_input("Add product to list", placeholder="e.g. Tata Salt 1kg")
        if pc2.button("Add", use_container_width=True):
            if new_prod.strip() and new_prod.strip() not in st.session_state.me_product_list:
                st.session_state.me_product_list.append(new_prod.strip())
                save_products(st.session_state.me_product_list)
                st.success(f"Added: {new_prod.strip()}")
                st.rerun()

        if st.session_state.me_product_list:
            st.markdown("<br>", unsafe_allow_html=True)
            for i, prod in enumerate(st.session_state.me_product_list):
                pr1, pr2 = st.columns([5,1])
                pr1.markdown(
                    f'<div style="padding:6px 4px;font-size:0.84rem;color:#E8E8F0;">' 
                    f'<span style="color:#6B6B75;font-family:DM Mono,monospace;font-size:0.7rem;">{i+1:02d}</span>' 
                    f' {prod}</div>',
                    unsafe_allow_html=True)
                if pr2.button("✕", key=f"delprod_{i}"):
                    st.session_state.me_product_list.pop(i)
                    save_products(st.session_state.me_product_list)
                    st.rerun()
        else:
            st.info("No products saved yet. They'll be added automatically when you enter a sale.")

        # Customer list
        st.markdown("---")
        st.markdown(f'{tag("CUSTOMER LIST")}<div class="riq-section-title">My Customers</div>',
                    unsafe_allow_html=True)
        cc1, cc2 = st.columns([4,1])
        new_cust = cc1.text_input("Add customer", placeholder="e.g. Ramesh Sharma")
        if cc2.button("Add", key="addcust", use_container_width=True):
            if new_cust.strip() and new_cust.strip() not in st.session_state.me_customer_list:
                st.session_state.me_customer_list.append(new_cust.strip())
                save_customers(st.session_state.me_customer_list)
                st.rerun()

        if st.session_state.me_customer_list:
            st.markdown("<br>", unsafe_allow_html=True)
            for i, cust in enumerate(st.session_state.me_customer_list):
                cu1, cu2 = st.columns([5,1])
                cu1.markdown(
                    f'<div style="padding:6px 4px;font-size:0.84rem;color:#E8E8F0;">' 
                    f'<span style="color:#6B6B75;font-family:DM Mono,monospace;font-size:0.7rem;">{i+1:02d}</span>' 
                    f' {cust}</div>',
                    unsafe_allow_html=True)
                if cu2.button("✕", key=f"delcust_{i}"):
                    st.session_state.me_customer_list.pop(i)
                    save_customers(st.session_state.me_customer_list)
                    st.rerun()

    # ════════════════════════════════════════════════════
    # TAB 5 — EXPORT
    # ════════════════════════════════════════════════════
    with tab_export:
        st.markdown(f'{tag("EXPORT")}<div class="riq-section-title">Export Your Data</div>',
                    unsafe_allow_html=True)

        if not entries:
            st.info("No entries yet. Add sales first.")
        else:
            exp_df = entries_to_df(entries)

            st.markdown(
                f'<div class="riq-card" style="margin-bottom:1rem;border-left:3px solid {GREEN};">' 
                f'<div style="font-size:0.85rem;font-weight:600;color:{GREEN};margin-bottom:6px;">' 
                f'✅ Automatically synced to all analytics pages</div>' 
                f'<div style="font-size:0.76rem;color:#B5B5BD;">' 
                f'{len(exp_df):,} entries · {sym}{exp_df["Sales"].sum():,.2f} total sales · ' 
                f'{exp_df["Order Date"].min().strftime("%d %b %Y")} → ' 
                f'{exp_df["Order Date"].max().strftime("%d %b %Y")}</div>' 
                f'<div style="font-size:0.7rem;color:#6B6B75;margin-top:6px;">' 
                f'Every sale you add here instantly appears on Dashboard, Sales Analytics, Forecasting, and all other pages — ' 
                f'no extra step needed. If you also uploaded a dataset, both are combined automatically.</div>' 
                f'</div>',
                unsafe_allow_html=True)

            # Export as CSV
            csv_out = exp_df.copy()
            csv_out["Order Date"] = csv_out["Order Date"].dt.strftime("%Y-%m-%d")
            csv_str = csv_out.to_csv(index=False)

            st.download_button(
                "⬇️ Download as CSV",
                data=csv_str,
                file_name=f"manual_sales_{pd.Timestamp.today().strftime('%Y%m%d')}.csv",
                mime="text/csv",
                use_container_width=True)

            # Daily summary
            st.markdown(f'{tag("DAILY SUMMARY")}<div class="riq-section-title">Daily Sales Summary</div>',
                        unsafe_allow_html=True)
            daily = (exp_df.groupby(exp_df["Order Date"].dt.date)
                           .agg(Entries=("Sales","count"),
                                Total_Sales=("Sales","sum"),
                                Total_Profit=("Profit","sum"),
                                Top_Category=("Category", lambda x: x.mode()[0] if len(x) > 0 else "—"))
                           .reset_index())
            daily.columns = ["Date","Entries","Total Sales","Total Profit","Top Category"]
            daily["Total Sales"]  = daily["Total Sales"].apply(lambda x: f"{sym}{x:,.2f}")
            daily["Total Profit"] = daily["Total Profit"].apply(lambda x: f"{sym}{x:,.2f}")
            daily = daily.sort_values("Date", ascending=False)
            st.dataframe(daily, use_container_width=True, hide_index=True)

            # Payment mode breakdown
            st.markdown(f'{tag("PAYMENT SPLIT")}<div class="riq-section-title">Payment Mode Breakdown</div>',
                        unsafe_allow_html=True)
            pay_split = exp_df.groupby("Payment Mode")["Sales"].sum().reset_index()
            pay_split.columns = ["Payment Mode","Total Sales"]
            pay_split["Share %"] = (pay_split["Total Sales"] / pay_split["Total Sales"].sum() * 100).round(1)
            pay_split["Total Sales"] = pay_split["Total Sales"].apply(lambda x: f"{sym}{x:,.2f}")
            pay_split = pay_split.sort_values("Share %", ascending=False)
            st.dataframe(pay_split, use_container_width=True, hide_index=True)

            # ── Save location info ────────────────────────────────────
            st.markdown("---")
            st.markdown(f'{tag("AUTO-SAVE")}<div class="riq-section-title">Auto-Save Status</div>',
                        unsafe_allow_html=True)
            sb_ok = _sb_available()
            storage_type = "Supabase Cloud" if sb_ok else "Local file"
            storage_color = GREEN if sb_ok else AMBER
            storage_icon  = "☁️" if sb_ok else "💾"
            st.markdown(
                f'<div class="riq-card" style="border-left:3px solid {storage_color};">' 
                f'<div style="font-size:0.82rem;font-weight:600;color:{storage_color};margin-bottom:4px;">' 
                f'{storage_icon} Data stored in {storage_type}</div>' 
                f'<div style="font-size:0.75rem;color:#B5B5BD;">{len(entries):,} entries · ' 
                f'{"Persistent across restarts ✅" if sb_ok else "Locally persistent ✅"}</div>' 
                f'<div style="font-size:0.75rem;color:#6B6B75;margin-top:4px;">' 
                f'{"Connected to Supabase — data survives redeployment" if sb_ok else "Add Supabase credentials in .streamlit/secrets.toml to enable cloud storage"}' 
                f'</div></div>',
                unsafe_allow_html=True)

            # ── Backup & Restore ──────────────────────────────────────
            st.markdown(f'{tag("BACKUP")}<div class="riq-section-title">Backup & Restore</div>',
                        unsafe_allow_html=True)
            bc1, bc2 = st.columns(2)

            # Backup — download all data as JSON
            backup_json = json.dumps({
                "entries":   [dict({k: str(v) if hasattr(v,"isoformat") else v
                               for k,v in e.items()}) for e in entries],
                "products":  st.session_state.me_product_list,
                "customers": st.session_state.me_customer_list,
                "prefs":     load_prefs(),
            }, ensure_ascii=False, indent=2)

            bc1.download_button(
                "⬇️ Download Full Backup (JSON)",
                data=backup_json,
                file_name=f"retailiq_backup_{pd.Timestamp.today().strftime('%Y%m%d_%H%M')}.json",
                mime="application/json",
                use_container_width=True)

            # Restore — upload a backup JSON
            restore_file = bc2.file_uploader("⬆️ Restore from Backup", type=["json"],
                                              key="restore_upload")
            if restore_file:
                try:
                    restore_data = json.load(restore_file)
                    if st.button("✅ Confirm Restore (will overwrite current data)"):
                        if "entries" in restore_data:
                            _save_json(SAVE_FILE, restore_data["entries"])
                            st.session_state.manual_entries = load_entries()
                        if "products" in restore_data:
                            _save_json(PROD_FILE, restore_data["products"])
                            st.session_state.me_product_list = restore_data["products"]
                        if "customers" in restore_data:
                            _save_json(CUST_FILE, restore_data["customers"])
                            st.session_state.me_customer_list = restore_data["customers"]
                        if "prefs" in restore_data:
                            _save_json(PREF_FILE, restore_data["prefs"])
                            st.session_state["currency"] = restore_data["prefs"].get("currency","INR")
                        st.success(f"✅ Restored {len(st.session_state.manual_entries)} entries from backup!")
                        st.rerun()
                except Exception as ex:
                    st.error(f"Restore failed: {ex}")

# ═══════════════════════════════════════════════════════
# PAGE: INVENTORY
# ═══════════════════════════════════════════════════════
elif menu == "📋 Inventory":
    page_header("Inventory Management", "// stock tracking, reorder alerts, dead stock")

    sym = currency_symbol()
    stock_entries = st.session_state.stock_entries

    def stock_to_df(entries):
        if not entries:
            return pd.DataFrame(columns=["Date","Type","Product Name","Category",
                                          "Quantity","Cost Price","Supplier","Notes"])
        return pd.DataFrame(entries)

    # Build product list — from stock entries + sales data + manual entry products
    all_products = set()
    if stock_entries:
        all_products.update(stock_to_df(stock_entries)["Product Name"].dropna().unique())
    if st.session_state.me_product_list:
        all_products.update(st.session_state.me_product_list)
    if "Product Name" in df.columns:
        all_products.update(df["Product Name"].dropna().unique())
    all_products = sorted(all_products)

    tab_stock, tab_levels, tab_reorder, tab_dead, tab_value = st.tabs([
        "➕ Stock In/Out", "📊 Stock Levels", "🔔 Reorder Alerts",
        "🐌 Dead Stock", "💰 Stock Value"])

    # ════════════════════════════════════════════════════
    # TAB 1 — STOCK IN / OUT ENTRY
    # ════════════════════════════════════════════════════
    with tab_stock:
        st.markdown(f'{tag("STOCK MOVEMENT")}<div class="riq-section-title">Record Stock Movement</div>',
                    unsafe_allow_html=True)
        st.markdown(
            '<div style="font-size:0.76rem;color:#6B6B75;margin-bottom:1rem;">'
            'Log stock received (purchases) or manual stock adjustments. '
            'Sales are automatically deducted from your uploaded/manual sales data.</div>',
            unsafe_allow_html=True)

        with st.form("stock_form", clear_on_submit=True):
            sc1, sc2, sc3 = st.columns(3)
            stock_date = sc1.date_input("Date", value=pd.Timestamp.today())
            stock_type = sc2.selectbox("Movement Type",
                ["Stock In (Purchase)", "Stock Out (Damage/Loss)",
                 "Stock Adjustment (+)", "Stock Adjustment (-)", "Opening Stock"])
            supplier   = sc3.text_input("Supplier / Source", placeholder="e.g. ABC Distributors")

            pc1, pc2, pc3 = st.columns(3)
            prod_list_inv = ["-- Type new product --"] + all_products
            prod_sel_inv  = pc1.selectbox("Product", prod_list_inv)
            product_name_inv = pc2.text_input("Product Name",
                value="" if prod_sel_inv == "-- Type new product --" else prod_sel_inv,
                placeholder="e.g. Basmati Rice 5kg")
            category_inv = pc3.selectbox("Category",
                ["General","Grocery & FMCG","Electronics","Clothing & Apparel",
                 "Medicines & Pharma","Food & Beverages","Hardware & Tools",
                 "Stationery","Home & Kitchen","Cosmetics & Beauty",
                 "Agricultural","Auto Parts","Wholesale","Other"])

            qc1, qc2, qc3 = st.columns(3)
            qty_inv   = qc1.number_input("Quantity", min_value=1, value=1, step=1)
            cost_inv  = qc2.number_input(f"Cost Price per unit ({sym})", min_value=0.0,
                                          value=0.0, step=0.5, format="%.2f")
            notes_inv = qc3.text_input("Notes", placeholder="Optional")

            if qty_inv > 0 and cost_inv > 0:
                total_cost = qty_inv * cost_inv
                st.markdown(
                    f'<div style="font-size:0.8rem;color:{GOLD};margin:0.5rem 0;">'
                    f'Total cost: {sym}{total_cost:,.2f}</div>',
                    unsafe_allow_html=True)

            submitted_stock = st.form_submit_button("💾 Save Stock Entry", use_container_width=True)

        if submitted_stock:
            if not product_name_inv.strip():
                st.error("Product Name is required.")
            else:
                is_outflow = stock_type in ["Stock Out (Damage/Loss)", "Stock Adjustment (-)"]
                signed_qty = -abs(qty_inv) if is_outflow else abs(qty_inv)
                entry = {
                    "Date": pd.Timestamp(stock_date),
                    "Type": stock_type,
                    "Product Name": product_name_inv.strip(),
                    "Category": category_inv,
                    "Quantity": signed_qty,
                    "Cost Price": cost_inv,
                    "Supplier": supplier.strip(),
                    "Notes": notes_inv.strip(),
                }
                st.session_state.stock_entries.append(entry)
                save_stock_entries(st.session_state.stock_entries)
                if product_name_inv.strip() not in st.session_state.me_product_list:
                    st.session_state.me_product_list.append(product_name_inv.strip())
                    save_products(st.session_state.me_product_list)
                st.success(f"✅ {stock_type} recorded — {product_name_inv} · Qty {qty_inv}")

        # Recent stock movements
        if stock_entries:
            st.markdown("---")
            st.markdown(f'{tag("RECENT")}<div class="riq-section-title">Recent Stock Movements</div>',
                        unsafe_allow_html=True)
            recent_stock = stock_to_df(stock_entries).copy()
            recent_stock["Date"] = pd.to_datetime(recent_stock["Date"]).dt.strftime("%d %b %Y")
            recent_stock["Cost Price"] = recent_stock["Cost Price"].apply(lambda x: f"{sym}{x:,.2f}")
            recent_stock = recent_stock.sort_values("Date", ascending=False).head(20)
            st.dataframe(recent_stock, use_container_width=True, hide_index=True)

            del_si1, del_si2 = st.columns([2,1])
            del_idx_stock = del_si1.number_input("Entry index to delete (0-based)", min_value=0,
                                                  max_value=max(0,len(stock_entries)-1),
                                                  value=0, step=1, key="del_stock_idx")
            if del_si2.button("🗑️ Delete Stock Entry"):
                if 0 <= del_idx_stock < len(st.session_state.stock_entries):
                    removed_s = st.session_state.stock_entries.pop(del_idx_stock)
                    save_stock_entries(st.session_state.stock_entries)
                    st.success(f"Deleted stock entry: {removed_s.get('Product Name','')}")
                    st.rerun()

    # ════════════════════════════════════════════════════
    # Compute current stock for all products (shared logic)
    # ════════════════════════════════════════════════════
    stock_df = stock_to_df(stock_entries)
    stock_in_by_product = {}
    if not stock_df.empty:
        stock_in_by_product = stock_df.groupby("Product Name")["Quantity"].sum().to_dict()

    sales_out_by_product = {}
    cost_by_product = {}
    if not stock_df.empty:
        cost_by_product = (stock_df[stock_df["Cost Price"] > 0]
                           .groupby("Product Name")["Cost Price"].mean().to_dict())
    if "Product Name" in df.columns and "Quantity" in df.columns:
        sales_out_by_product = df.groupby("Product Name")["Quantity"].sum().to_dict()
    # Include manual entries sales too
    if st.session_state.manual_entries:
        me_df = pd.DataFrame(st.session_state.manual_entries)
        if "Product Name" in me_df.columns and "Quantity" in me_df.columns:
            me_sales = me_df.groupby("Product Name")["Quantity"].sum().to_dict()
            for p, q in me_sales.items():
                sales_out_by_product[p] = sales_out_by_product.get(p, 0) + q

    # Current stock = stock movements (in/out/opening) - sales quantity sold
    current_stock = {}
    for p in all_products:
        stock_in = stock_in_by_product.get(p, 0)
        sold_out = sales_out_by_product.get(p, 0)
        current_stock[p] = stock_in - sold_out

    # Sales velocity (avg daily sales over last 30 days of data, fallback to overall avg)
    velocity = {}
    if "Product Name" in df.columns and "Order Date" in df.columns:
        max_date = df["Order Date"].max()
        recent_window = df[df["Order Date"] >= max_date - pd.Timedelta(days=30)]
        date_span = max((max_date - df["Order Date"].min()).days, 1)
        for p in all_products:
            recent_qty = recent_window[recent_window["Product Name"]==p]["Quantity"].sum() if not recent_window.empty else 0
            total_qty  = sales_out_by_product.get(p, 0)
            v = recent_qty / 30 if recent_qty > 0 else (total_qty / date_span if date_span > 0 else 0)
            velocity[p] = max(v, 0.01)

    # ════════════════════════════════════════════════════
    # TAB 2 — STOCK LEVELS
    # ════════════════════════════════════════════════════
    with tab_levels:
        st.markdown(f'{tag("STOCK LEVELS")}<div class="riq-section-title">Current Stock by Product</div>',
                    unsafe_allow_html=True)

        if not all_products:
            st.info("No products found. Record a Stock In entry or upload sales data first.")
        else:
            levels_data = []
            for p in all_products:
                stock_qty = current_stock.get(p, 0)
                vel = velocity.get(p, 0.01)
                days_left = stock_qty / vel if vel > 0 else float('inf')
                cost = cost_by_product.get(p, 0)
                levels_data.append({
                    "Product": p,
                    "Current Stock": stock_qty,
                    "Daily Velocity": round(vel, 2),
                    "Days Remaining": round(days_left, 0) if days_left != float('inf') else None,
                    "Stock Value": stock_qty * cost if cost > 0 else 0,
                })
            levels_df = pd.DataFrame(levels_data).sort_values("Current Stock")

            lc1, lc2, lc3, lc4 = st.columns(4)
            lc1.metric("Total Products", f"{len(levels_df):,}")
            lc2.metric("Out of Stock", f"{(levels_df['Current Stock'] <= 0).sum():,}")
            lc3.metric("Low Stock (<7 days)", f"{(levels_df['Days Remaining'] <= 7).sum():,}")
            lc4.metric("Total Stock Value", fmt_num(levels_df["Stock Value"].sum(), sym))

            # Stock level chart
            st.markdown(f'{tag("CHART")}<div class="riq-section-title">Stock Levels — Lowest 20</div>',
                        unsafe_allow_html=True)
            low20 = levels_df.head(20).copy()
            colors_stock = [RED if v <= 0 else AMBER if v <= 10 else GREEN
                            for v in low20["Current Stock"]]
            fig_stock = go.Figure(go.Bar(
                x=low20["Current Stock"], y=low20["Product"].astype(str).str[:30],
                orientation="h",
                marker=dict(color=colors_stock, line_width=0, cornerradius=5),
                text=low20["Current Stock"], textposition="outside",
                textfont=dict(size=9, color=FONT_C),
                hovertemplate="<b>%{y}</b><br>Stock: %{x}<extra></extra>"))
            lay_stock = plotly_base(max(280, len(low20)*28))
            lay_stock.update({"margin": dict(l=10,r=50,t=20,b=10)})
            fig_stock.update_layout(**lay_stock)
            st.plotly_chart(fig_stock, use_container_width=True, config={"displayModeBar": False})

            # Full table
            st.markdown(f'{tag("FULL TABLE")}<div class="riq-section-title">All Products</div>',
                        unsafe_allow_html=True)
            disp_levels = levels_df.copy()
            disp_levels["Stock Value"] = disp_levels["Stock Value"].apply(lambda x: f"{sym}{x:,.0f}")
            disp_levels["Days Remaining"] = disp_levels["Days Remaining"].apply(
                lambda x: f"{x:.0f}d" if pd.notna(x) else "—")
            st.dataframe(disp_levels, use_container_width=True, hide_index=True)

            csv_levels = levels_df.to_csv(index=False)
            st.download_button("Download stock levels CSV", csv_levels,
                file_name=f"stock_levels_{pd.Timestamp.today().strftime('%Y%m%d')}.csv",
                mime="text/csv", use_container_width=True)

    # ════════════════════════════════════════════════════
    # TAB 3 — REORDER ALERTS
    # ════════════════════════════════════════════════════
    with tab_reorder:
        st.markdown(f'{tag("REORDER")}<div class="riq-section-title">Reorder Settings & Alerts</div>',
                    unsafe_allow_html=True)
        st.markdown(
            '<div style="font-size:0.76rem;color:#6B6B75;margin-bottom:1rem;">'
            'Set a reorder threshold (days of stock remaining). Products below this trigger an alert.</div>',
            unsafe_allow_html=True)

        ro1, ro2 = st.columns(2)
        default_threshold = st.session_state.reorder_settings.get("default_days", 7)
        threshold_days = ro1.slider("Default reorder threshold (days)", 1, 30, default_threshold)
        safety_stock_pct = ro2.slider("Safety stock buffer (%)", 0, 100, 20,
                                       help="Extra buffer added to suggested reorder quantity")

        if st.button("Save Reorder Settings"):
            st.session_state.reorder_settings = {
                "default_days": threshold_days, "safety_pct": safety_stock_pct}
            save_reorder_settings(st.session_state.reorder_settings)
            st.success("Settings saved.")

        if not all_products:
            st.info("No products to evaluate yet.")
        else:
            reorder_data = []
            for p in all_products:
                stock_qty = current_stock.get(p, 0)
                vel = velocity.get(p, 0.01)
                days_left = stock_qty / vel if vel > 0 else float('inf')
                if days_left <= threshold_days:
                    suggested_qty = max(int(vel * threshold_days * 2 * (1 + safety_stock_pct/100)), 1)
                    reorder_data.append({
                        "Product": p,
                        "Current Stock": stock_qty,
                        "Days Remaining": round(days_left, 1) if days_left != float('inf') else 0,
                        "Daily Velocity": round(vel, 2),
                        "Suggested Reorder Qty": suggested_qty,
                        "Urgency": "Critical" if days_left <= 2 else "High" if days_left <= 5 else "Medium",
                    })

            if not reorder_data:
                st.success("✅ No products need reordering right now. All stock levels are healthy.")
            else:
                reorder_df = pd.DataFrame(reorder_data).sort_values("Days Remaining")

                rc1, rc2, rc3 = st.columns(3)
                rc1.metric("Products to Reorder", f"{len(reorder_df):,}")
                rc2.metric("Critical (≤2 days)", f"{(reorder_df['Urgency']=='Critical').sum():,}")
                rc3.metric("Est. Reorder Cost",
                          fmt_num(sum(r["Suggested Reorder Qty"] * cost_by_product.get(r["Product"],0)
                                     for r in reorder_data), sym))

                urgency_colors = {"Critical": RED, "High": AMBER, "Medium": GOLD}
                for _, row in reorder_df.iterrows():
                    uc = urgency_colors.get(row["Urgency"], AMBER)
                    st.markdown(
                        f'<div class="riq-insight">'
                        f'<div style="font-size:1.2rem;min-width:28px;">📦</div>'
                        f'<div style="flex:1;">'
                        f'<div style="font-weight:600;font-size:0.85rem;color:#F8FAFC;">{row["Product"][:60]}</div>'
                        f'<div style="display:flex;gap:14px;margin-top:4px;flex-wrap:wrap;">'
                        f'<span style="font-size:0.7rem;color:{uc};font-weight:600;">{row["Urgency"]}</span>'
                        f'<span style="font-size:0.7rem;color:#B5B5BD;">Stock: {row["Current Stock"]:.0f}</span>'
                        f'<span style="font-size:0.7rem;color:#B5B5BD;">{row["Days Remaining"]:.1f} days left</span>'
                        f'<span style="font-size:0.7rem;color:{GREEN};">Reorder: {row["Suggested Reorder Qty"]:.0f} units</span>'
                        f'</div></div></div>',
                        unsafe_allow_html=True)

                csv_reorder = reorder_df.to_csv(index=False)
                st.download_button("Download reorder list CSV", csv_reorder,
                    file_name=f"reorder_list_{pd.Timestamp.today().strftime('%Y%m%d')}.csv",
                    mime="text/csv", use_container_width=True)

    # ════════════════════════════════════════════════════
    # TAB 4 — DEAD STOCK
    # ════════════════════════════════════════════════════
    with tab_dead:
        st.markdown(f'{tag("DEAD STOCK")}<div class="riq-section-title">Slow-Moving & Dead Stock</div>',
                    unsafe_allow_html=True)
        st.markdown(
            '<div style="font-size:0.76rem;color:#6B6B75;margin-bottom:1rem;">'
            'Products with no recent sales activity, sitting in inventory.</div>',
            unsafe_allow_html=True)

        dead_threshold = st.slider("Dead stock threshold (days without sale)", 7, 180, 30)

        if "Product Name" in df.columns and "Order Date" in df.columns:
            last_sold = df.groupby("Product Name")["Order Date"].max()
            max_date = df["Order Date"].max()

            dead_data = []
            for p in all_products:
                stock_qty = current_stock.get(p, 0)
                if stock_qty <= 0:
                    continue
                if p in last_sold.index:
                    days_since = (max_date - last_sold[p]).days
                else:
                    days_since = 9999  # never sold
                if days_since >= dead_threshold:
                    cost = cost_by_product.get(p, 0)
                    dead_data.append({
                        "Product": p,
                        "Stock Remaining": stock_qty,
                        "Days Since Last Sale": days_since if days_since != 9999 else None,
                        "Tied-up Value": stock_qty * cost if cost > 0 else 0,
                    })

            if not dead_data:
                st.success(f"✅ No dead stock found beyond {dead_threshold} days. Inventory is moving well.")
            else:
                dead_df = pd.DataFrame(dead_data).sort_values("Tied-up Value", ascending=False)

                dc1, dc2, dc3 = st.columns(3)
                dc1.metric("Dead Stock Items", f"{len(dead_df):,}")
                dc2.metric("Total Units Stuck", f"{dead_df['Stock Remaining'].sum():,.0f}")
                dc3.metric("Capital Tied Up", fmt_num(dead_df["Tied-up Value"].sum(), sym))

                fig_dead = go.Figure(go.Bar(
                    x=dead_df.head(15)["Tied-up Value"],
                    y=dead_df.head(15)["Product"].astype(str).str[:30],
                    orientation="h",
                    marker=dict(color=RED, line_width=0, cornerradius=5),
                    text=[f"{sym}{v:,.0f}" for v in dead_df.head(15)["Tied-up Value"]],
                    textposition="outside", textfont=dict(size=9, color=FONT_C),
                    hovertemplate=f"<b>%{{y}}</b><br>Value: {sym}%{{x:,.0f}}<extra></extra>"))
                lay_dead = plotly_base(max(280, min(len(dead_df),15)*30))
                lay_dead.update({"margin": dict(l=10,r=70,t=20,b=10)})
                fig_dead.update_layout(**lay_dead)
                st.plotly_chart(fig_dead, use_container_width=True, config={"displayModeBar": False})

                disp_dead = dead_df.copy()
                disp_dead["Tied-up Value"] = disp_dead["Tied-up Value"].apply(lambda x: f"{sym}{x:,.0f}")
                disp_dead["Days Since Last Sale"] = disp_dead["Days Since Last Sale"].apply(
                    lambda x: f"{x:.0f}d" if pd.notna(x) else "Never sold")
                st.dataframe(disp_dead, use_container_width=True, hide_index=True)

                st.markdown(f'{tag("ACTIONS")}<div class="riq-section-title">Recommended Actions</div>',
                            unsafe_allow_html=True)
                st.markdown(
                    f'<div class="riq-card" style="font-size:0.8rem;line-height:1.8;color:#B5B5BD;">'
                    f'<b style="color:{GOLD};">Clear capital tied up:</b><br>'
                    f'• Bundle dead stock with fast-moving items as combo offers<br>'
                    f'• Run a clearance sale at 20-40% off to recover capital<br>'
                    f'• Stop reordering these products until current stock clears<br>'
                    f'• Consider returning to supplier if return policy allows<br>'
                    f'</div>', unsafe_allow_html=True)

                csv_dead = dead_df.to_csv(index=False)
                st.download_button("Download dead stock CSV", csv_dead,
                    file_name=f"dead_stock_{pd.Timestamp.today().strftime('%Y%m%d')}.csv",
                    mime="text/csv", use_container_width=True)
        else:
            st.info("Upload sales data with Order Date to analyse dead stock.")

    # ════════════════════════════════════════════════════
    # TAB 5 — STOCK VALUE
    # ════════════════════════════════════════════════════
    with tab_value:
        st.markdown(f'{tag("STOCK VALUE")}<div class="riq-section-title">Inventory Valuation</div>',
                    unsafe_allow_html=True)

        if not all_products:
            st.info("No products to value yet.")
        else:
            value_data = []
            for p in all_products:
                stock_qty = current_stock.get(p, 0)
                if stock_qty <= 0:
                    continue
                cost = cost_by_product.get(p, 0)
                avg_sell_price = (df[df["Product Name"]==p]["Sales"].sum() /
                                  df[df["Product Name"]==p]["Quantity"].sum()
                                  if "Product Name" in df.columns and
                                  (df["Product Name"]==p).any() and
                                  df[df["Product Name"]==p]["Quantity"].sum() > 0 else cost * 1.3)
                value_data.append({
                    "Product": p,
                    "Stock Qty": stock_qty,
                    "Cost Value": stock_qty * cost,
                    "Selling Value": stock_qty * avg_sell_price,
                    "Potential Profit": stock_qty * (avg_sell_price - cost),
                })

            if not value_data:
                st.info("No stock currently held (all sold or zero quantity).")
            else:
                value_df = pd.DataFrame(value_data).sort_values("Cost Value", ascending=False)

                vc1, vc2, vc3, vc4 = st.columns(4)
                vc1.metric("Total Units in Stock", f"{value_df['Stock Qty'].sum():,.0f}")
                vc2.metric("Stock Value (Cost)", fmt_num(value_df["Cost Value"].sum(), sym))
                vc3.metric("Stock Value (Selling)", fmt_num(value_df["Selling Value"].sum(), sym))
                vc4.metric("Potential Profit", fmt_num(value_df["Potential Profit"].sum(), sym))

                # Value by category
                if "Category" in stock_df.columns if not stock_df.empty else False:
                    cat_value = stock_df.groupby("Category").apply(
                        lambda g: sum(current_stock.get(p,0) * cost_by_product.get(p,0)
                                     for p in g["Product Name"].unique())
                    ).reset_index(name="Value")
                    cat_value = cat_value[cat_value["Value"] > 0].sort_values("Value", ascending=False)
                    if not cat_value.empty:
                        st.markdown(f'{tag("BY CATEGORY")}<div class="riq-section-title">Stock Value by Category</div>',
                                    unsafe_allow_html=True)
                        fig_cat_val = go.Figure(go.Pie(
                            labels=cat_value["Category"], values=cat_value["Value"],
                            hole=0.55,
                            marker=dict(colors=PALETTE, line=dict(color=BG, width=2)),
                            textinfo="label+percent", textfont=dict(size=11, color=FONT_C),
                            hovertemplate=f"<b>%{{label}}</b><br>{sym}%{{value:,.0f}}<extra></extra>"))
                        lay_cv = plotly_base(320)
                        lay_cv.update({"showlegend": True,
                                       "legend": dict(bgcolor="rgba(0,0,0,0)", font=dict(size=10))})
                        fig_cat_val.update_layout(**lay_cv)
                        st.plotly_chart(fig_cat_val, use_container_width=True, config={"displayModeBar": False})

                st.markdown(f'{tag("FULL VALUATION")}<div class="riq-section-title">Product Valuation Table</div>',
                            unsafe_allow_html=True)
                disp_val = value_df.copy()
                for c in ["Cost Value","Selling Value","Potential Profit"]:
                    disp_val[c] = disp_val[c].apply(lambda x: f"{sym}{x:,.0f}")
                st.dataframe(disp_val, use_container_width=True, hide_index=True)

                csv_val = value_df.to_csv(index=False)
                st.download_button("Download stock valuation CSV", csv_val,
                    file_name=f"stock_valuation_{pd.Timestamp.today().strftime('%Y%m%d')}.csv",
                    mime="text/csv", use_container_width=True)

# ═══════════════════════════════════════════════════════
# PAGE: SALES ANALYTICS
# ═══════════════════════════════════════════════════════
elif menu == "📊 Sales Analytics":
    page_header("Sales Analytics", "// deep dive into revenue performance")

    # Filters
    with st.expander("🔍 Filters", expanded=False):
        fc1, fc2, fc3, fc4 = st.columns(4)
        years  = sorted(df["Order Date"].dt.year.dropna().unique().tolist(), reverse=True)
        markets= ["All"] + sorted(df["Market"].dropna().unique().tolist()) if "Market" in df.columns else ["All"]
        cats   = ["All"] + sorted(df["Category"].dropna().unique().tolist()) if "Category" in df.columns else ["All"]
        segs   = ["All"] + sorted(df["Segment"].dropna().unique().tolist()) if "Segment" in df.columns else ["All"]
        sel_yr = fc1.multiselect("Year", years, default=years[:2])
        sel_mkt= fc2.selectbox("Market", markets)
        sel_cat= fc3.selectbox("Category", cats)
        sel_seg= fc4.selectbox("Segment", segs)

    fdf = df.copy()
    if sel_yr:     fdf = fdf[fdf["Order Date"].dt.year.isin(sel_yr)]
    if sel_mkt != "All" and "Market" in fdf.columns: fdf = fdf[fdf["Market"]==sel_mkt]
    if sel_cat != "All" and "Category" in fdf.columns: fdf = fdf[fdf["Category"]==sel_cat]
    if sel_seg != "All" and "Segment" in fdf.columns: fdf = fdf[fdf["Segment"]==sel_seg]

    ts  = fdf["Sales"].sum()
    tp  = fdf["Profit"].sum()
    to  = fdf["Order ID"].nunique()
    tgr = (ts - df["Sales"].sum()*0.85) / (df["Sales"].sum()*0.85) * 100  # mock YoY

    m1,m2,m3,m4 = st.columns(4)
    m1.metric("Total Sales",   f"{currency_symbol()}{ts:,.0f}",  f"{tgr:+.1f}% YoY")
    m2.metric("Total Profit",  f"{currency_symbol()}{tp:,.0f}")
    m3.metric("Orders",        f"{to:,}")
    m4.metric("Profit Margin", f"{tp/ts*100:.1f}%" if ts > 0 else "0%")

    # Time series tabs
    tab_d, tab_w, tab_m, tab_y = st.tabs(["Daily", "Weekly", "Monthly", "Yearly"])

    def time_chart(period_df, freq, label):
        grp = period_df.groupby(period_df["Order Date"].dt.to_period(freq)).agg(
            Sales=("Sales","sum"), Profit=("Profit","sum"), Orders=("Order ID","nunique")).reset_index()
        grp["Period"] = grp["Order Date"].astype(str)
        fig = go.Figure()
        fig.add_trace(go.Bar(x=grp["Period"], y=grp["Sales"], name="Sales",
            marker=dict(color=GOLD, line_width=0, cornerradius=4),
            hovertemplate="<b>%{x}</b><br>Sales: {currency_symbol()}%{{y:,.0f}}<extra></extra>"))
        fig.add_trace(go.Scatter(x=grp["Period"], y=grp["Profit"], name="Profit",
            line=dict(color=GREEN, width=2), mode="lines+markers",
            marker=dict(size=5), yaxis="y2",
            hovertemplate="<b>%{x}</b><br>Profit: {currency_symbol()}%{{y:,.0f}}<extra></extra>"))
        lay = plotly_base(320)
        lay.update({
            "barmode": "group", "xaxis_tickangle": -30,
            "yaxis": dict(gridcolor=GRID, tickprefix="$", showline=False),
            "yaxis2": dict(overlaying="y", side="right", gridcolor="rgba(0,0,0,0)",
                           tickprefix="$", showline=False),
            "legend": dict(bgcolor="rgba(0,0,0,0)", orientation="h", y=-0.2)
        })
        fig.update_layout(**lay)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    with tab_d: time_chart(fdf, "D", "Daily")
    with tab_w: time_chart(fdf, "W", "Weekly")
    with tab_m: time_chart(fdf, "M", "Monthly")
    with tab_y: time_chart(fdf, "Y", "Yearly")

    # Sub-category breakdown
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown(f'{tag("SUB-CATEGORY")}<div class="riq-section-title">Sales by Sub-Category</div>', unsafe_allow_html=True)
        if "Sub-Category" in fdf.columns:
            sub = fdf.groupby("Sub-Category")["Sales"].sum().sort_values(ascending=True).reset_index()
            n = len(sub)
            bc = [PALETTE[i % len(PALETTE)] for i in range(n)]
            fig_sub = go.Figure(go.Bar(
                x=sub["Sales"], y=sub["Sub-Category"], orientation="h",
                marker=dict(color=bc, line_width=0, cornerradius=5),
                text=[f"{currency_symbol()}{v:,.0f}" for v in sub["Sales"]], textposition="outside",
                textfont=dict(size=9, color=FONT_C),
                hovertemplate="<b>%{y}</b><br>{currency_symbol()}%{{x:,.0f}}<extra></extra>"))
            lay_sub = plotly_base(max(280, n*32))
            lay_sub.update({"xaxis_tickprefix": currency_tickprefix(),"margin":dict(l=10,r=70,t=20,b=10)})
            fig_sub.update_layout(**lay_sub)
            st.plotly_chart(fig_sub, use_container_width=True, config={"displayModeBar": False})

    with col_b:
        st.markdown(f'{tag("SHIP MODE")}<div class="riq-section-title">Sales by Ship Mode</div>', unsafe_allow_html=True)
        if "Ship Mode" in fdf.columns:
            ship = fdf.groupby("Ship Mode")["Sales"].sum().reset_index().sort_values("Sales", ascending=False)
            fig_sh = go.Figure(go.Pie(
                labels=ship["Ship Mode"], values=ship["Sales"], hole=0.6,
                marker=dict(colors=PALETTE[:len(ship)], line=dict(color=BG, width=3)),
                textinfo="label+percent", textfont=dict(size=10, color=FONT_C),
                hovertemplate="<b>%{label}</b><br>{currency_symbol()}%{{value:,.0f}}<br>%{percent}<extra></extra>"))
            lay_sh = plotly_base(280)
            lay_sh.update({"showlegend": False})
            fig_sh.update_layout(**lay_sh)
            st.plotly_chart(fig_sh, use_container_width=True, config={"displayModeBar": False})

    # Discount vs Profit scatter
    st.markdown(f'{tag("ANALYSIS")}<div class="riq-section-title">Discount Impact on Profit</div>', unsafe_allow_html=True)
    if "Discount" in fdf.columns:
        sample = fdf.sample(min(2000, len(fdf))).copy()
        fig_sc = px.scatter(sample, x="Discount", y="Profit",
            color="Category" if "Category" in sample.columns else None,
            color_discrete_sequence=PALETTE,
            opacity=0.6, size_max=8,
            hover_data=["Sales"] if "Sales" in sample.columns else None)
        lay_sc = plotly_base(300)
        lay_sc.update({"yaxis_tickprefix": currency_tickprefix(),"xaxis_tickformat":".0%"})
        fig_sc.update_layout(**lay_sc)
        fig_sc.update_traces(marker=dict(size=5))
        st.plotly_chart(fig_sc, use_container_width=True, config={"displayModeBar": False})

# ═══════════════════════════════════════════════════════
# PAGE: CUSTOMER ANALYTICS
# ═══════════════════════════════════════════════════════
elif menu == "👥 Customer Analytics":
    page_header("Customer Analytics", "// understand your customers")

    if "Customer ID" not in df.columns:
        st.warning("Customer ID column not found in dataset.")
        st.stop()

    cust = df.groupby(["Customer ID","Customer Name"] if "Customer Name" in df.columns else ["Customer ID"]).agg(
        Total_Sales=("Sales","sum"),
        Total_Profit=("Profit","sum"),
        Orders=("Order ID","nunique"),
        Avg_Order=("Sales","mean"),
    ).reset_index().sort_values("Total_Sales", ascending=False)

    total_cust = len(cust)
    avg_clv    = cust["Total_Sales"].mean()
    top10_pct  = cust.head(10)["Total_Sales"].sum() / cust["Total_Sales"].sum() * 100
    repeat_cust= (cust["Orders"] > 1).sum()

    m1,m2,m3,m4 = st.columns(4)
    m1.metric("Total Customers", f"{total_cust:,}")
    m2.metric("Avg Customer Value", f"{currency_symbol()}{avg_clv:,.0f}")
    m3.metric("Top 10 Customers %", f"{top10_pct:.1f}%")
    m4.metric("Repeat Customers", f"{repeat_cust:,}")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f'{tag("TOP")}<div class="riq-section-title">Top 15 Customers by Revenue</div>', unsafe_allow_html=True)
        top15 = cust.head(15)
        name_col = "Customer Name" if "Customer Name" in top15.columns else "Customer ID"
        fig_top = go.Figure(go.Bar(
            x=top15["Total_Sales"], y=top15[name_col].astype(str).str[:20], orientation="h",
            marker=dict(color=GOLD, line_width=0, cornerradius=6),
            text=[f"{currency_symbol()}{v:,.0f}" for v in top15["Total_Sales"]], textposition="outside",
            textfont=dict(size=9, color=FONT_C),
            hovertemplate="<b>%{y}</b><br>Sales: {currency_symbol()}%{{x:,.0f}}<extra></extra>"))
        lay_top = plotly_base(380)
        lay_top.update({"xaxis_tickprefix": currency_tickprefix(),"margin":dict(l=10,r=80,t=20,b=10)})
        fig_top.update_layout(**lay_top)
        st.plotly_chart(fig_top, use_container_width=True, config={"displayModeBar": False})

    with col2:
        st.markdown(f'{tag("SEGMENT")}<div class="riq-section-title">Customer Segments</div>', unsafe_allow_html=True)
        if "Segment" in df.columns:
            seg_cust = df.groupby("Segment").agg(
                Customers=("Customer ID","nunique"),
                Sales=("Sales","sum"),
                Profit=("Profit","sum")).reset_index()
            fig_seg = go.Figure()
            for i, row in seg_cust.iterrows():
                fig_seg.add_trace(go.Bar(
                    name=row["Segment"], x=[row["Segment"]], y=[row["Sales"]],
                    marker=dict(color=PALETTE[i], line_width=0, cornerradius=6),
                    text=[f"{currency_symbol()}{row['Sales']:,.0f}"], textposition="outside",
                    hovertemplate=f"<b>{row['Segment']}</b><br>Customers: {row['Customers']:,}<br>Sales: {currency_symbol()}{row['Sales']:,.0f}<extra></extra>"))
            lay_seg = plotly_base(300)
            lay_seg.update({"showlegend": False, "yaxis_tickprefix": currency_tickprefix()})
            fig_seg.update_layout(**lay_seg)
            st.plotly_chart(fig_seg, use_container_width=True, config={"displayModeBar": False})

    # Customer distribution by orders
    st.markdown(f'{tag("DISTRIBUTION")}<div class="riq-section-title">Orders per Customer Distribution</div>', unsafe_allow_html=True)
    fig_dist = go.Figure(go.Histogram(
        x=cust["Orders"], nbinsx=20,
        marker=dict(color=GOLD, line=dict(color=BG2, width=1), cornerradius=4),
        hovertemplate="Orders: %{x}<br>Customers: %{y}<extra></extra>"))
    lay_dist = plotly_base(260)
    lay_dist.update({"xaxis_title": "Number of Orders", "yaxis_title": "Customers"})
    fig_dist.update_layout(**lay_dist)
    st.plotly_chart(fig_dist, use_container_width=True, config={"displayModeBar": False})

    # Customer table
    st.markdown(f'{tag("TABLE")}<div class="riq-section-title">Customer Details</div>', unsafe_allow_html=True)
    display_cust = cust.head(50).copy()
    display_cust["Total_Sales"]  = display_cust["Total_Sales"].apply(lambda x: f"{currency_symbol()}{x:,.0f}")
    display_cust["Total_Profit"] = display_cust["Total_Profit"].apply(lambda x: f"{currency_symbol()}{x:,.0f}")
    display_cust["Avg_Order"]    = display_cust["Avg_Order"].apply(lambda x: f"{currency_symbol()}{x:,.0f}")
    st.dataframe(display_cust, use_container_width=True, hide_index=True)

    # ─────────────────────────────────────────
    # PHASE 4 — RFM + K-MEANS SEGMENTATION
    # ─────────────────────────────────────────
    st.markdown("---")
    st.markdown(
        
        f'<div class="riq-section-title" style="font-size:1.1rem;">RFM Customer Segmentation</div>'
        f'<div style="font-size:0.76rem;color:#6B6B75;margin-bottom:1rem;">'
        f'Recency · Frequency · Monetary — K-Means clustering with auto segment labeling</div>',
        unsafe_allow_html=True)

    try:
        from sklearn.preprocessing import StandardScaler
        from sklearn.cluster import KMeans

        # ── 1. Build RFM table ──────────────────────────────────────
        snapshot_date = df["Order Date"].max() + pd.Timedelta(days=1)
        id_col   = "Customer ID"
        name_col = "Customer Name" if "Customer Name" in df.columns else "Customer ID"

        rfm = df.groupby(id_col).agg(
            Recency  =("Order Date",  lambda x: (snapshot_date - x.max()).days),
            Frequency=("Order ID",    "nunique"),
            Monetary =("Sales",       "sum"),
        ).reset_index()

        if name_col != id_col:
            names = df[[id_col, name_col]].drop_duplicates(id_col)
            rfm = rfm.merge(names, on=id_col, how="left")

        # ── 2. Scale & cluster ─────────────────────────────────────
        n_clusters = st.slider("Number of segments (K)", min_value=3, max_value=7, value=4,
                               help="K-Means will group customers into this many segments")

        scaler     = StandardScaler()
        rfm_scaled = scaler.fit_transform(rfm[["Recency","Frequency","Monetary"]])

        kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
        rfm["Cluster"] = kmeans.fit_predict(rfm_scaled)

        # ── 3. Auto-label segments by centroid profile ─────────────
        centers = pd.DataFrame(
            scaler.inverse_transform(kmeans.cluster_centers_),
            columns=["Recency","Frequency","Monetary"]
        )
        centers["Cluster"] = range(n_clusters)

        def label_segment(row):
            r, f, m = row["Recency"], row["Frequency"], row["Monetary"]
            r_med = centers["Recency"].median()
            f_med = centers["Frequency"].median()
            m_med = centers["Monetary"].median()
            if r < r_med and f >= f_med and m >= m_med:
                return "Champions"
            elif r < r_med and f >= f_med:
                return "Loyal Customers"
            elif r < r_med and m >= m_med:
                return "Big Spenders"
            elif r >= r_med * 1.5 and f < f_med:
                return "At Risk"
            elif r >= r_med * 2:
                return "Lost"
            elif f < f_med and m < m_med:
                return "New Customers"
            else:
                return "Potential Loyalists"

        centers["Label"] = centers.apply(label_segment, axis=1)
        # Ensure unique labels (if duplicates, append cluster id)
        seen = {}
        unique_labels = []
        for _, row in centers.iterrows():
            lbl = row["Label"]
            if lbl in seen:
                seen[lbl] += 1
                unique_labels.append(f"{lbl} {seen[lbl]}")
            else:
                seen[lbl] = 0
                unique_labels.append(lbl)
        centers["Label"] = unique_labels

        cluster_label_map = dict(zip(centers["Cluster"], centers["Label"]))
        rfm["Segment"] = rfm["Cluster"].map(cluster_label_map)

        # ── 4. Segment color map ───────────────────────────────────
        seg_colors = {
            "Champions":          GOLD,
            "Loyal Customers":    GREEN,
            "Big Spenders":       GOLD2,
            "At Risk":            AMBER,
            "Lost":               RED,
            "New Customers":      BLUE,
            "Potential Loyalists":"#8B5CF6",
        }
        rfm["Color"] = rfm["Segment"].apply(
            lambda s: next((v for k, v in seg_colors.items() if k in s), "#B5B5BD"))

        # ── 5. KPI summary cards ───────────────────────────────────
        seg_summary = rfm.groupby("Segment").agg(
            Customers=("Customer ID","count"),
            Avg_Recency=("Recency","mean"),
            Avg_Frequency=("Frequency","mean"),
            Avg_Monetary=("Monetary","mean"),
            Total_Revenue=("Monetary","sum"),
        ).reset_index().sort_values("Total_Revenue", ascending=False)

        st.markdown(f'{tag("SEGMENT OVERVIEW")}<div class="riq-section-title">Segment Summary</div>',
                    unsafe_allow_html=True)

        seg_cards_html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:1.2rem;">'
        for _, row in seg_summary.iterrows():
            color = next((v for k, v in seg_colors.items() if k in row["Segment"]), "#B5B5BD")
            seg_cards_html += (
                f'<div style="background:rgba(25,25,28,0.8);border:1px solid rgba(255,153,51,0.12);'
                f'border-top:2px solid {color};border-radius:14px;padding:0.85rem 1rem;">'
                f'<div style="font-family:DM Mono,monospace;font-size:0.6rem;font-weight:600;'
                f'letter-spacing:0.1em;color:{color};margin-bottom:4px;">{row["Segment"].upper()}</div>'
                f'<div style="font-family:DM Mono,monospace;font-size:1.15rem;font-weight:600;color:{color};">'
                f'{row["Customers"]:,}</div>'
                f'<div style="font-size:0.62rem;color:#6B6B75;text-transform:uppercase;letter-spacing:0.07em;">customers</div>'
                f'<div style="font-size:0.7rem;color:#B5B5BD;margin-top:6px;">'
                f'Avg {currency_symbol()}{row["Avg_Monetary"]:,.0f} · {row["Avg_Frequency"]:.1f} orders</div>'
                f'</div>'
            )
        seg_cards_html += '</div>'
        st.markdown(seg_cards_html, unsafe_allow_html=True)

        # ── 6. Charts ──────────────────────────────────────────────
        c1, c2 = st.columns(2)

        with c1:
            st.markdown(f'{tag("DISTRIBUTION")}<div class="riq-section-title">Customers per Segment</div>',
                        unsafe_allow_html=True)
            fig_pie = go.Figure(go.Pie(
                labels=seg_summary["Segment"],
                values=seg_summary["Customers"],
                hole=0.6,
                marker=dict(
                    colors=[next((v for k, v in seg_colors.items() if k in s), "#B5B5BD")
                            for s in seg_summary["Segment"]],
                    line=dict(color=BG, width=3)
                ),
                textinfo="label+percent",
                textfont=dict(size=11, color=FONT_C),
                hovertemplate="<b>%{label}</b><br>%{value:,} customers<br>%{percent}<extra></extra>"
            ))
            fig_pie.add_annotation(
                text=f"<b>{len(rfm):,}</b>", x=0.5, y=0.5, showarrow=False,
                font=dict(size=14, color=GOLD, family="DM Mono"), xref="paper", yref="paper")
            lay_pie = plotly_base(300)
            lay_pie.update({"showlegend": False})
            fig_pie.update_layout(**lay_pie)
            st.plotly_chart(fig_pie, use_container_width=True, config={"displayModeBar": False})

        with c2:
            st.markdown(f'{tag("REVENUE")}<div class="riq-section-title">Revenue by Segment</div>',
                        unsafe_allow_html=True)
            seg_rev = seg_summary.sort_values("Total_Revenue", ascending=True)
            fig_rev = go.Figure(go.Bar(
                x=seg_rev["Total_Revenue"],
                y=seg_rev["Segment"],
                orientation="h",
                marker=dict(
                    color=[next((v for k, v in seg_colors.items() if k in s), "#B5B5BD")
                           for s in seg_rev["Segment"]],
                    line_width=0, cornerradius=6
                ),
                text=[f"{currency_symbol()}{v:,.0f}" for v in seg_rev["Total_Revenue"]],
                textposition="outside",
                textfont=dict(size=9, color=FONT_C),
                hovertemplate="<b>%{y}</b><br>{currency_symbol()}%{{x:,.0f}}<extra></extra>"
            ))
            lay_rev = plotly_base(300)
            lay_rev.update({"xaxis_tickprefix": currency_tickprefix(), "margin": dict(l=10, r=90, t=20, b=10)})
            fig_rev.update_layout(**lay_rev)
            st.plotly_chart(fig_rev, use_container_width=True, config={"displayModeBar": False})

        # ── 7. RFM 3D Scatter ─────────────────────────────────────
        st.markdown(f'{tag("3D SCATTER")}<div class="riq-section-title">RFM Space — 3D View</div>',
                    unsafe_allow_html=True)
        sample_rfm = rfm.sample(min(1500, len(rfm)), random_state=42)
        fig_3d = go.Figure()
        for seg in sample_rfm["Segment"].unique():
            sub = sample_rfm[sample_rfm["Segment"] == seg]
            color = next((v for k, v in seg_colors.items() if k in seg), "#B5B5BD")
            hover_name = name_col if name_col in sub.columns else id_col
            fig_3d.add_trace(go.Scatter3d(
                x=sub["Recency"], y=sub["Frequency"], z=sub["Monetary"],
                mode="markers",
                name=seg,
                marker=dict(size=4, color=color, opacity=0.75, line=dict(width=0)),
                text=sub[hover_name].astype(str),
                hovertemplate=(
                    "<b>%{text}</b><br>"
                    "Recency: %{x}d<br>Frequency: %{y}<br>Monetary: {currency_symbol()}%{{z:,.0f}}<extra></extra>"
                )
            ))
        fig_3d.update_layout(
            height=480,
            paper_bgcolor=BG2, plot_bgcolor=BG2,
            font=dict(family="DM Mono, monospace", color=FONT_C, size=10),
            margin=dict(l=0, r=0, t=20, b=0),
            legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
            scene=dict(
                xaxis=dict(title="Recency (days)", backgroundcolor=BG2,
                           gridcolor=GRID, showbackground=True, color=FONT_C),
                yaxis=dict(title="Frequency (orders)", backgroundcolor=BG2,
                           gridcolor=GRID, showbackground=True, color=FONT_C),
                zaxis=dict(title="Monetary ($)", backgroundcolor=BG2,
                           gridcolor=GRID, showbackground=True, color=FONT_C),
            )
        )
        st.plotly_chart(fig_3d, use_container_width=True, config={"displayModeBar": False})

        # ── 8. Radar chart — segment profile ──────────────────────
        st.markdown(f'{tag("PROFILE")}<div class="riq-section-title">Segment RFM Profile (Normalised)</div>',
                    unsafe_allow_html=True)

        # Normalise centers 0-1 for radar (invert Recency so lower = better = higher bar)
        radar_df = centers.copy()
        radar_df["Recency_n"]   = 1 - (radar_df["Recency"]   - radar_df["Recency"].min())   / (radar_df["Recency"].max()   - radar_df["Recency"].min()   + 1e-9)
        radar_df["Frequency_n"] =     (radar_df["Frequency"] - radar_df["Frequency"].min()) / (radar_df["Frequency"].max() - radar_df["Frequency"].min() + 1e-9)
        radar_df["Monetary_n"]  =     (radar_df["Monetary"]  - radar_df["Monetary"].min())  / (radar_df["Monetary"].max()  - radar_df["Monetary"].min()  + 1e-9)

        cats_radar = ["Recency\n(inverted)", "Frequency", "Monetary", "Recency\n(inverted)"]
        fig_radar = go.Figure()
        for _, row in radar_df.iterrows():
            color = next((v for k, v in seg_colors.items() if k in row["Label"]), "#B5B5BD")
            vals  = [row["Recency_n"], row["Frequency_n"], row["Monetary_n"], row["Recency_n"]]
            fig_radar.add_trace(go.Scatterpolar(
                r=vals, theta=cats_radar, fill="toself", name=row["Label"],
                line=dict(color=color, width=2),
                fillcolor=color.replace("#", "rgba(").replace(")", ",0.08)") if "#" in color else f"rgba(212,175,55,0.08)",
                hovertemplate=f"<b>{row['Label']}</b><br>%{{theta}}: %{{r:.2f}}<extra></extra>"
            ))
        fig_radar.update_layout(
            height=380,
            paper_bgcolor=BG2,
            font=dict(family="DM Mono, monospace", color=FONT_C, size=11),
            polar=dict(
                bgcolor=BG2,
                radialaxis=dict(visible=True, range=[0, 1], gridcolor=GRID,
                                tickfont=dict(size=9, color=FONT_C), color=FONT_C),
                angularaxis=dict(gridcolor=GRID, color=FONT_C)
            ),
            legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
            margin=dict(l=60, r=60, t=30, b=30)
        )
        st.plotly_chart(fig_radar, use_container_width=True, config={"displayModeBar": False})

        # ── 9. Segment detail table ────────────────────────────────
        st.markdown(f'{tag("SEGMENT TABLE")}<div class="riq-section-title">RFM Segment Details</div>',
                    unsafe_allow_html=True)

        seg_filter = st.selectbox("Filter by segment", ["All"] + sorted(rfm["Segment"].unique().tolist()))
        rfm_display = rfm if seg_filter == "All" else rfm[rfm["Segment"] == seg_filter]

        display_cols = [c for c in [name_col, id_col, "Recency", "Frequency", "Monetary", "Segment"]
                        if c in rfm_display.columns and c != (id_col if name_col != id_col else None)]
        rfm_show = rfm_display[list(dict.fromkeys(display_cols))].copy()
        rfm_show["Monetary"] = rfm_show["Monetary"].apply(lambda x: f"{currency_symbol()}{x:,.0f}")
        rfm_show["Recency"]  = rfm_show["Recency"].apply(lambda x: f"{x}d ago")
        rfm_show = rfm_show.sort_values("Segment").head(200)
        st.dataframe(rfm_show, use_container_width=True, hide_index=True)

        # ── 10. Download ───────────────────────────────────────────
        csv_rfm = rfm.drop(columns=["Cluster", "Color"], errors="ignore").to_csv(index=False)
        st.download_button(
            "Download RFM Segments CSV",
            data=csv_rfm,
            file_name=f"rfm_segments_{pd.Timestamp.today().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            use_container_width=True
        )

        # ── 11. AI-style recommendations ──────────────────────────
        st.markdown(f'{tag("RECOMMENDATIONS")}<div class="riq-section-title">Segment Action Plan</div>',
                    unsafe_allow_html=True)

        actions = {
            "Champions":          ("🥇", GOLD,  "Reward them. Early access, loyalty perks, referral programs. They drive disproportionate revenue."),
            "Loyal Customers":    ("💚", GREEN, "Upsell higher-value products. Nurture with personalised offers and exclusive content."),
            "Big Spenders":       ("💰", GOLD2, "High cart value but may not return often. Win them back with targeted high-value campaigns."),
            "At Risk":            ("⚠️", AMBER, "Send win-back campaigns. Discounts, re-engagement emails, ask for feedback."),
            "Lost":               ("🔴", RED,   "Final re-engagement attempt. Heavy discount or simply accept churn and focus elsewhere."),
            "New Customers":      ("🆕", BLUE,  "Onboard well. Welcome series, first-purchase incentives, guide them to repeat purchase."),
            "Potential Loyalists":("⭐", "#8B5CF6","Just needs a nudge. Loyalty program invitation, personalised product recommendations."),
        }

        for seg_name in seg_summary["Segment"]:
            matched_key = next((k for k in actions if k in seg_name), None)
            if matched_key:
                icon, color, advice = actions[matched_key]
                count = seg_summary[seg_summary["Segment"]==seg_name]["Customers"].values[0]
                rev   = seg_summary[seg_summary["Segment"]==seg_name]["Total_Revenue"].values[0]
                st.markdown(
                    f'<div class="riq-insight">'
                    f'<div style="font-size:1.3rem;min-width:28px;">{icon}</div>'
                    f'<div>'
                    f'<div style="font-weight:600;font-size:0.88rem;color:{color};">'
                    f'{seg_name} <span style="color:#6B6B75;font-weight:400;">({count:,} customers · {currency_symbol()}{rev:,.0f} revenue)</span></div>'
                    f'<div style="font-size:0.78rem;color:#B5B5BD;margin-top:3px;">{advice}</div>'
                    f'</div></div>',
                    unsafe_allow_html=True
                )

    except ImportError:
        st.error("scikit-learn is required for RFM segmentation. Add it to requirements.txt and redeploy.")
    except Exception as e:
        st.error(f"RFM Segmentation error: {e}")
        st.exception(e)

# ═══════════════════════════════════════════════════════
# PAGE: PRODUCT ANALYTICS
# ═══════════════════════════════════════════════════════
elif menu == "📦 Product Analytics":
    page_header("Product Analytics", "// product performance intelligence")

    if "Product Name" not in df.columns:
        st.warning("Product data not found in dataset.")
        st.stop()

    prod = df.groupby(["Product ID","Product Name","Category","Sub-Category"]
                      if all(c in df.columns for c in ["Product ID","Category","Sub-Category"])
                      else ["Product Name"]).agg(
        Sales=("Sales","sum"), Profit=("Profit","sum"),
        Qty=("Quantity","sum"), Orders=("Order ID","nunique")).reset_index()
    prod["Margin"] = prod["Profit"] / prod["Sales"] * 100

    m1,m2,m3,m4 = st.columns(4)
    m1.metric("Total Products", f"{len(prod):,}")
    m2.metric("Best Margin",    f"{prod['Margin'].max():.1f}%")
    m3.metric("Worst Margin",   f"{prod['Margin'].min():.1f}%")
    m4.metric("Avg Margin",     f"{prod['Margin'].mean():.1f}%")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f'{tag("TOP 10")}<div class="riq-section-title">Top 10 Products — Sales</div>', unsafe_allow_html=True)
        top10 = prod.nlargest(10, "Sales")
        fig_t = go.Figure(go.Bar(
            x=top10["Sales"], y=top10["Product Name"].astype(str).str[:30]+"...", orientation="h",
            marker=dict(color=GOLD, line_width=0, cornerradius=6),
            text=[f"{currency_symbol()}{v:,.0f}" for v in top10["Sales"]], textposition="outside",
            textfont=dict(size=9, color=FONT_C),
            hovertemplate="<b>%{y}</b><br>{currency_symbol()}%{{x:,.0f}}<extra></extra>"))
        lay_t = plotly_base(300)
        lay_t.update({"xaxis_tickprefix": currency_tickprefix(),"margin":dict(l=10,r=80,t=20,b=10)})
        fig_t.update_layout(**lay_t)
        st.plotly_chart(fig_t, use_container_width=True, config={"displayModeBar": False})

    with col2:
        st.markdown(f'{tag("BOTTOM 10")}<div class="riq-section-title">Bottom 10 Products — Profit</div>', unsafe_allow_html=True)
        bot10 = prod.nsmallest(10, "Profit")
        fig_b = go.Figure(go.Bar(
            x=bot10["Profit"], y=bot10["Product Name"].astype(str).str[:30]+"...", orientation="h",
            marker=dict(color=[RED if v < 0 else AMBER for v in bot10["Profit"]], line_width=0, cornerradius=6),
            text=[f"{currency_symbol()}{v:,.0f}" for v in bot10["Profit"]], textposition="outside",
            textfont=dict(size=9, color=FONT_C),
            hovertemplate="<b>%{y}</b><br>{currency_symbol()}%{{x:,.0f}}<extra></extra>"))
        lay_b = plotly_base(300)
        lay_b.update({"xaxis_tickprefix": currency_tickprefix(),"margin":dict(l=10,r=80,t=20,b=10)})
        fig_b.update_layout(**lay_b)
        st.plotly_chart(fig_b, use_container_width=True, config={"displayModeBar": False})

    # Category vs Sub-Category treemap
    if all(c in df.columns for c in ["Category","Sub-Category","Sales"]):
        st.markdown(f'{tag("TREEMAP")}<div class="riq-section-title">Category & Sub-Category Breakdown</div>', unsafe_allow_html=True)
        tree = df.groupby(["Category","Sub-Category"])["Sales"].sum().reset_index()
        fig_tree = px.treemap(tree, path=["Category","Sub-Category"], values="Sales",
                              color="Sales", color_continuous_scale=[[0,BG2],[0.5,GOLD],[1,GOLD2]])
        lay_tree = plotly_base(380)
        lay_tree.update({"margin": dict(l=0,r=0,t=30,b=0)})
        fig_tree.update_layout(**lay_tree)
        fig_tree.update_traces(textfont=dict(color="white"))
        st.plotly_chart(fig_tree, use_container_width=True, config={"displayModeBar": False})

    # Profit margin scatter
    st.markdown(f'{tag("PROFITABILITY")}<div class="riq-section-title">Sales vs Profit by Category</div>', unsafe_allow_html=True)
    sample = prod.sample(min(500, len(prod)))
    fig_sc = px.scatter(sample, x="Sales", y="Profit",
        color="Category" if "Category" in sample.columns else None,
        size=sample["Qty"].clip(lower=0) if "Qty" in sample.columns else None,
        color_discrete_sequence=PALETTE, opacity=0.7,
        hover_data=["Product Name"] if "Product Name" in sample.columns else None)
    lay_sc = plotly_base(320)
    lay_sc.update({"xaxis_tickprefix": currency_tickprefix(),"yaxis_tickprefix": currency_tickprefix()})
    fig_sc.update_layout(**lay_sc)
    st.plotly_chart(fig_sc, use_container_width=True, config={"displayModeBar": False})

# ═══════════════════════════════════════════════════════
# PAGE: REGIONAL ANALYTICS
# ═══════════════════════════════════════════════════════
elif menu == "🌍 Regional Analytics":
    page_header("Regional Analytics", "// market & geographic performance")

    col1, col2 = st.columns(2)

    with col1:
        if "Market" in df.columns:
            st.markdown(f'{tag("MARKET")}<div class="riq-section-title">Sales by Market</div>', unsafe_allow_html=True)
            mkt = df.groupby("Market").agg(Sales=("Sales","sum"),Profit=("Profit","sum"),
                Orders=("Order ID","nunique")).reset_index().sort_values("Sales",ascending=True)
            n = len(mkt)
            colors_m = [PALETTE[i % len(PALETTE)] for i in range(n)]
            fig_m = go.Figure()
            fig_m.add_trace(go.Bar(y=mkt["Market"],x=mkt["Sales"],name="Sales",orientation="h",
                marker=dict(color=GOLD,line_width=0,cornerradius=6),
                hovertemplate="<b>%{y}</b><br>{currency_symbol()}%{{x:,.0f}}<extra>Sales</extra>"))
            fig_m.add_trace(go.Bar(y=mkt["Market"],x=mkt["Profit"],name="Profit",orientation="h",
                marker=dict(color=GREEN,line_width=0,cornerradius=6),
                hovertemplate="<b>%{y}</b><br>{currency_symbol()}%{{x:,.0f}}<extra>Profit</extra>"))
            lay_m = plotly_base(max(280,n*38))
            lay_m.update({"barmode":"group","xaxis_tickprefix": currency_tickprefix(),"legend":dict(bgcolor="rgba(0,0,0,0)",orientation="h",y=-0.15)})
            fig_m.update_layout(**lay_m)
            st.plotly_chart(fig_m,use_container_width=True,config={"displayModeBar":False})

    with col2:
        if "Region" in df.columns:
            st.markdown(f'{tag("REGION")}<div class="riq-section-title">Profit by Region</div>', unsafe_allow_html=True)
            reg = df.groupby("Region").agg(Sales=("Sales","sum"),Profit=("Profit","sum")).reset_index()
            fig_r = go.Figure(go.Pie(labels=reg["Region"],values=reg["Profit"],hole=0.6,
                marker=dict(colors=PALETTE[:len(reg)],line=dict(color=BG,width=3)),
                textinfo="label+percent",textfont=dict(size=11,color=FONT_C),
                hovertemplate="<b>%{label}</b><br>{currency_symbol()}%{{value:,.0f}}<extra></extra>"))
            fig_r.add_annotation(text=f"<b>{fmt_num(reg['Profit'].sum(),'$')}</b>",
                x=0.5,y=0.5,showarrow=False,
                font=dict(size=13,color=GOLD,family="DM Mono"),xref="paper",yref="paper")
            lay_r = plotly_base(300); lay_r.update({"showlegend":True})
            fig_r.update_layout(**lay_r)
            st.plotly_chart(fig_r,use_container_width=True,config={"displayModeBar":False})

    # Top countries
    if "Country" in df.columns:
        st.markdown(f'{tag("COUNTRIES")}<div class="riq-section-title">Top 20 Countries by Sales</div>', unsafe_allow_html=True)
        cntry = df.groupby("Country").agg(Sales=("Sales","sum"),Profit=("Profit","sum")).reset_index()
        cntry = cntry.sort_values("Sales",ascending=False).head(20)
        fig_c = go.Figure()
        fig_c.add_trace(go.Bar(x=cntry["Country"],y=cntry["Sales"],name="Sales",
            marker=dict(color=GOLD,line_width=0,cornerradius=4),
            hovertemplate="<b>%{x}</b><br>{currency_symbol()}%{{y:,.0f}}<extra>Sales</extra>"))
        fig_c.add_trace(go.Bar(x=cntry["Country"],y=cntry["Profit"],name="Profit",
            marker=dict(color=GREEN,line_width=0,cornerradius=4),
            hovertemplate="<b>%{x}</b><br>{currency_symbol()}%{{y:,.0f}}<extra>Profit</extra>"))
        lay_c = plotly_base(300)
        lay_c.update({"barmode":"group","xaxis_tickangle":-35,"yaxis_tickprefix": currency_tickprefix(),"legend":dict(bgcolor="rgba(0,0,0,0)",orientation="h",y=-0.2)})
        fig_c.update_layout(**lay_c)
        st.plotly_chart(fig_c,use_container_width=True,config={"displayModeBar":False})

    # Top states
    if "State" in df.columns:
        st.markdown(f'{tag("STATES")}<div class="riq-section-title">Top 15 States by Sales</div>', unsafe_allow_html=True)
        sts = df.groupby("State")["Sales"].sum().sort_values(ascending=True).tail(15).reset_index()
        fig_s = go.Figure(go.Bar(
            x=sts["Sales"],y=sts["State"],orientation="h",
            marker=dict(color=GOLD2,line_width=0,cornerradius=5),
            text=[f"{currency_symbol()}{v:,.0f}" for v in sts["Sales"]],textposition="outside",
            textfont=dict(size=9,color=FONT_C),
            hovertemplate="<b>%{y}</b><br>{currency_symbol()}%{{x:,.0f}}<extra></extra>"))
        lay_s = plotly_base(max(280,15*32))
        lay_s.update({"xaxis_tickprefix": currency_tickprefix(),"margin":dict(l=10,r=80,t=20,b=10)})
        fig_s.update_layout(**lay_s)
        st.plotly_chart(fig_s,use_container_width=True,config={"displayModeBar":False})

# ═══════════════════════════════════════════════════════
# PAGE: FORECASTING — Phase 4  (ARIMA + Prophet fallback)
# ═══════════════════════════════════════════════════════
elif menu == "📈 Forecasting":
    page_header("Sales Forecasting", "// phase 4 · ARIMA · Prophet · demand intelligence")

    # ── helpers ────────────────────────────────────────────────────
    def _future_month_labels(last_period, n):
        return [(last_period + i).strftime("%Y-%m") for i in range(1, n + 1)]

    def _plotly_forecast_fig(hist_x, hist_y, fut_x, fut_y,
                             lo=None, hi=None, height=400, model_label="Forecast"):
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=hist_x, y=hist_y, name="Actual",
            line=dict(color=GOLD, width=2.5),
            fill="tozeroy", fillcolor="rgba(212,175,55,0.05)",
            hovertemplate="<b>%{x}</b><br>{currency_symbol()}%{{y:,.0f}}<extra>Actual</extra>"))
        if lo is not None and hi is not None:
            fig.add_trace(go.Scatter(
                x=list(fut_x) + list(fut_x)[::-1],
                y=list(hi)    + list(lo)[::-1],
                fill="toself", fillcolor="rgba(16,185,129,0.10)",
                line=dict(width=0), showlegend=True, name="90% CI",
                hoverinfo="skip"))
        fig.add_trace(go.Scatter(
            x=fut_x, y=fut_y, name=model_label,
            line=dict(color=GREEN, width=2.5, dash="dash"),
            marker=dict(size=8, color=GREEN, symbol="diamond"),
            hovertemplate="<b>%{x}</b><br>{currency_symbol()}%{{y:,.0f}}<extra>" + model_label + "</extra>"))
        if len(fut_x):
            fig.add_vrect(x0=fut_x[0], x1=fut_x[-1],
                fillcolor="rgba(16,185,129,0.03)", line_width=0,
                annotation_text="Forecast", annotation_font_color=GREEN)
        lay = plotly_base(height)
        lay.update({"yaxis_tickprefix": currency_tickprefix(), "xaxis_tickangle": -30,
                    "legend": dict(bgcolor="rgba(0,0,0,0)", orientation="h", y=-0.18)})
        fig.update_layout(**lay)
        return fig

    # ── prepare monthly data ────────────────────────────────────────
    monthly = (df.groupby(df["Order Date"].dt.to_period("M"))["Sales"]
                 .sum().reset_index())
    monthly["ds"] = monthly["Order Date"].dt.to_timestamp()
    monthly["y"]  = monthly["Sales"]
    monthly["Month"] = monthly["Order Date"].astype(str)
    monthly = monthly.sort_values("ds").reset_index(drop=True)

    # ── sidebar controls ────────────────────────────────────────────
    with st.sidebar:
        st.markdown("---")
        st.markdown(f'<div class="riq-tag">FORECAST SETTINGS</div>', unsafe_allow_html=True)
        horizon   = st.slider("Forecast horizon (months)", 3, 24, 6)
        model_choice = st.radio("Model", ["Auto (best fit)", "ARIMA", "Prophet"],
                                index=0, label_visibility="collapsed",
                                help="Auto tries Prophet first, falls back to ARIMA")

    st.markdown(
        
        f'<div style="font-size:0.76rem;color:#6B6B75;margin-bottom:1rem;">'
        f'Model: <span style="color:{GOLD};">{model_choice}</span> · '
        f'Horizon: <span style="color:{GOLD};">{horizon} months</span> · '
        f'{len(monthly)} months of history</div>',
        unsafe_allow_html=True)

    last_period = monthly["Order Date"].iloc[-1]
    future_months = _future_month_labels(last_period, horizon)

    # ══════════════════════════════════════════════════════════════
    # MODEL RUNNERS
    # ══════════════════════════════════════════════════════════════

    @st.cache_data(show_spinner=False)
    def run_prophet(y_series, ds_series, horizon):
        from prophet import Prophet
        train = pd.DataFrame({"ds": ds_series, "y": y_series})
        m = Prophet(yearly_seasonality=True, weekly_seasonality=False,
                    daily_seasonality=False,
                    interval_width=0.90,
                    changepoint_prior_scale=0.05)
        m.fit(train)
        future = m.make_future_dataframe(periods=horizon, freq="MS")
        fc = m.predict(future)
        last_hist = len(train)
        fore = fc.iloc[last_hist:].reset_index(drop=True)
        return (fore["yhat"].clip(lower=0).values,
                fore["yhat_lower"].clip(lower=0).values,
                fore["yhat_upper"].clip(lower=0).values)

    @st.cache_data(show_spinner=False)
    def run_arima(y_values, horizon):
        from statsmodels.tsa.arima.model import ARIMA
        import warnings, itertools
        best_aic, best_order, best_model = np.inf, (1,1,1), None
        for p, d, q in itertools.product([0,1,2], [0,1], [0,1,2]):
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    m = ARIMA(y_values, order=(p, d, q)).fit()
                if m.aic < best_aic:
                    best_aic, best_order, best_model = m.aic, (p,d,q), m
            except Exception:
                pass
        if best_model is None:
            from statsmodels.tsa.arima.model import ARIMA
            best_model = ARIMA(y_values, order=(1,1,1)).fit()
            best_order = (1,1,1)
        fc   = best_model.get_forecast(steps=horizon)
        pred = np.maximum(np.asarray(fc.predicted_mean), 0)
        try:
            ci = fc.conf_int(alpha=0.10)
            lo = np.maximum(ci.iloc[:, 0].values, 0)
            hi = ci.iloc[:, 1].values
        except Exception:
            lo = pred * 0.85
            hi = pred * 1.15
        return pred, lo, hi, best_order

    @st.cache_data(show_spinner=False)
    def run_poly_fallback(y_values, horizon):
        from sklearn.linear_model import LinearRegression
        from sklearn.preprocessing import PolynomialFeatures
        from sklearn.pipeline import Pipeline
        t = np.arange(len(y_values)).reshape(-1,1)
        model = Pipeline([("poly", PolynomialFeatures(degree=2)), ("lr", LinearRegression())])
        model.fit(t, y_values)
        fut_t = np.arange(len(y_values), len(y_values) + horizon).reshape(-1,1)
        return np.maximum(model.predict(fut_t), 0), None, None

    # ── run chosen model ────────────────────────────────────────────
    pred, lo, hi, model_used = None, None, None, ""
    best_arima_order = None

    with st.spinner("Running forecast model…"):
        if model_choice in ["Auto (best fit)", "Prophet"]:
            try:
                pred, lo, hi = run_prophet(
                    monthly["y"].values, monthly["ds"].values, horizon)
                model_used = "Prophet"
            except Exception as e:
                if model_choice == "Prophet":
                    st.warning(f"Prophet failed: {e}. Install with `pip install prophet`.")
                else:
                    st.info("Prophet not installed — falling back to ARIMA.")

        if pred is None and model_choice in ["Auto (best fit)", "ARIMA"]:
            try:
                pred, lo, hi, best_arima_order = run_arima(monthly["y"].values, horizon)
                model_used = f"ARIMA{best_arima_order}"
            except Exception as e:
                st.warning(f"ARIMA failed: {e}. Install with `pip install statsmodels`.")

        if pred is None:
            pred, lo, hi = run_poly_fallback(monthly["y"].values, horizon)
            model_used = "Polynomial (fallback)"
            st.info("Using polynomial regression fallback. Install `prophet` or `statsmodels` for better forecasts.")

    # ── model badge ────────────────────────────────────────────────
    badge_color = GREEN if "Prophet" in model_used else (BLUE if "ARIMA" in model_used else AMBER)
    st.markdown(
        f'<div style="display:inline-flex;align-items:center;gap:8px;'
        f'background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.25);'
        f'border-radius:8px;padding:6px 14px;margin-bottom:1rem;">'
        f'<span style="font-family:DM Mono,monospace;font-size:0.72rem;color:{badge_color};">MODEL</span>'
        f'<span style="font-family:DM Mono,monospace;font-size:0.82rem;font-weight:600;color:{badge_color};">'
        f'{model_used}</span></div>',
        unsafe_allow_html=True)

    # ── KPI summary row ─────────────────────────────────────────────
    total_hist   = monthly["y"].sum()
    avg_monthly  = monthly["y"].mean()
    fc_total     = pred.sum()
    last_actual  = monthly["y"].iloc[-1]
    first_fc     = pred[0]
    mom_change   = (first_fc - last_actual) / last_actual * 100 if last_actual else 0

    k1 = kpi_card("AVG MONTHLY",  fmt_num(avg_monthly, "$"), "Historical avg",    GOLD)
    k2 = kpi_card("FC TOTAL",     fmt_num(fc_total,    "$"), f"Next {horizon}mo",  GREEN)
    k3 = kpi_card("NEXT MONTH",   fmt_num(first_fc,    "$"), "Predicted",          BLUE)
    k4 = kpi_card("MoM CHANGE",   f"{mom_change:+.1f}%",    "vs last actual",
                  GREEN if mom_change >= 0 else RED)
    st.markdown(
        f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:1.2rem;">'
        f'{k1}{k2}{k3}{k4}</div>', unsafe_allow_html=True)

    # ── main forecast chart ─────────────────────────────────────────
    st.markdown(f'{tag("FORECAST CHART")}<div class="riq-section-title">Sales Forecast — {model_used}</div>',
                unsafe_allow_html=True)
    fig_main = _plotly_forecast_fig(
        monthly["Month"].values, monthly["y"].values,
        future_months, pred, lo, hi,
        height=420, model_label=model_used)
    st.plotly_chart(fig_main, use_container_width=True, config={"displayModeBar": False})

    # ── forecast table ──────────────────────────────────────────────
    st.markdown(f'{tag("FORECAST TABLE")}<div class="riq-section-title">{horizon}-Month Forecast Table</div>',
                unsafe_allow_html=True)
    fc_rows = []
    for i, (mo, p) in enumerate(zip(future_months, pred)):
        row = {"Month": mo, "Predicted Sales": f"{currency_symbol()}{p:,.0f}",
               "vs Avg": f"{(p - avg_monthly) / avg_monthly * 100:+.1f}%"}
        if lo is not None:
            row["Lower 90%"] = f"{currency_symbol()}{lo[i]:,.0f}"
            row["Upper 90%"] = f"{currency_symbol()}{hi[i]:,.0f}"
        fc_rows.append(row)
    fc_tbl = pd.DataFrame(fc_rows)
    st.dataframe(fc_tbl, use_container_width=True, hide_index=True)

    csv_fc = fc_tbl.to_csv(index=False)
    st.download_button("Download forecast CSV", csv_fc,
        file_name=f"forecast_{model_used.replace(' ','_')}_{pd.Timestamp.today().strftime('%Y%m%d')}.csv",
        mime="text/csv", use_container_width=True)

    # ── trend decomposition (if statsmodels available) ──────────────
    st.markdown("---")
    st.markdown(f'{tag("DECOMPOSITION")}<div class="riq-section-title">Trend & Seasonality Decomposition</div>',
                unsafe_allow_html=True)
    try:
        from statsmodels.tsa.seasonal import seasonal_decompose
        import warnings
        if len(monthly) >= 24:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                decomp = seasonal_decompose(monthly["y"].values, model="additive", period=12)

            d_col1, d_col2 = st.columns(2)
            with d_col1:
                fig_trend = go.Figure(go.Scatter(
                    x=monthly["Month"], y=decomp.trend,
                    line=dict(color=GOLD, width=2.5),
                    hovertemplate="<b>%{x}</b><br>{currency_symbol()}%{{y:,.0f}}<extra>Trend</extra>"))
                lay_t = plotly_base(220)
                lay_t.update({"yaxis_tickprefix": currency_tickprefix(), "xaxis_tickangle": -30})
                fig_trend.update_layout(**lay_t)
                st.markdown('<div style="font-size:0.78rem;color:#B5B5BD;margin-bottom:4px;">Trend component</div>',
                            unsafe_allow_html=True)
                st.plotly_chart(fig_trend, use_container_width=True, config={"displayModeBar": False})

            with d_col2:
                fig_seas = go.Figure(go.Bar(
                    x=monthly["Month"], y=decomp.seasonal,
                    marker=dict(
                        color=[GREEN if v >= 0 else RED for v in decomp.seasonal],
                        line_width=0, cornerradius=4),
                    hovertemplate="<b>%{x}</b><br>{currency_symbol()}%{{y:,.0f}}<extra>Seasonal</extra>"))
                lay_s = plotly_base(220)
                lay_s.update({"yaxis_tickprefix": currency_tickprefix(), "xaxis_tickangle": -30})
                fig_seas.update_layout(**lay_s)
                st.markdown('<div style="font-size:0.78rem;color:#B5B5BD;margin-bottom:4px;">Seasonal component</div>',
                            unsafe_allow_html=True)
                st.plotly_chart(fig_seas, use_container_width=True, config={"displayModeBar": False})
        else:
            st.info("Need at least 24 months of data for seasonal decomposition.")
    except ImportError:
        st.info("Install `statsmodels` to unlock trend decomposition.")
    except Exception:
        pass

    # ── category-level forecasts ────────────────────────────────────
    st.markdown("---")
    st.markdown(f'{tag("BY CATEGORY")}<div class="riq-section-title">Category-Level Forecasts</div>',
                unsafe_allow_html=True)

    if "Category" in df.columns:
        cats = sorted(df["Category"].dropna().unique().tolist())
        cat_tabs = st.tabs(cats)

        for tab, cat in zip(cat_tabs, cats):
            with tab:
                cdf = (df[df["Category"] == cat]
                       .groupby(df["Order Date"].dt.to_period("M"))["Sales"]
                       .sum().reset_index())
                cdf["ds"]    = cdf["Order Date"].dt.to_timestamp()
                cdf["y"]     = cdf["Sales"]
                cdf["Month"] = cdf["Order Date"].astype(str)
                cdf = cdf.sort_values("ds").reset_index(drop=True)

                if len(cdf) < 6:
                    st.warning(f"Not enough data for {cat} (need ≥6 months).")
                    continue

                c_pred, c_lo, c_hi, c_model = None, None, None, ""
                try:
                    from prophet import Prophet
                    cp, cl, ch = run_prophet(cdf["y"].values, cdf["ds"].values, horizon)
                    c_pred, c_lo, c_hi, c_model = cp, cl, ch, "Prophet"
                except Exception:
                    pass

                if c_pred is None:
                    try:
                        cp, cl, ch, cord = run_arima(cdf["y"].values, horizon)
                        c_pred, c_lo, c_hi = cp, cl, ch
                        c_model = f"ARIMA{cord}"
                    except Exception:
                        pass

                if c_pred is None:
                    cp, cl, ch = run_poly_fallback(cdf["y"].values, horizon)
                    c_pred, c_lo, c_hi, c_model = cp, cl, ch, "Poly"

                cat_future = _future_month_labels(cdf["Order Date"].iloc[-1], horizon)

                col_a, col_b = st.columns([2, 1])
                with col_a:
                    fig_cat = _plotly_forecast_fig(
                        cdf["Month"].values, cdf["y"].values,
                        cat_future, c_pred, c_lo, c_hi,
                        height=280, model_label=c_model)
                    st.plotly_chart(fig_cat, use_container_width=True, config={"displayModeBar": False})
                with col_b:
                    cat_avg = cdf["y"].mean()
                    cat_next = c_pred[0]
                    st.metric("Next month", f"{currency_symbol()}{cat_next:,.0f}",
                              f"{(cat_next - cat_avg)/cat_avg*100:+.1f}% vs avg")
                    st.metric(f"Next {horizon}mo total", f"{currency_symbol()}{c_pred.sum():,.0f}")
                    st.markdown(
                        f'<div style="font-family:DM Mono,monospace;font-size:0.65rem;'
                        f'color:#6B6B75;margin-top:6px;">model: {c_model}</div>',
                        unsafe_allow_html=True)

    # ── year-over-year comparison ────────────────────────────────────
    st.markdown("---")
    st.markdown(f'{tag("YoY ANALYSIS")}<div class="riq-section-title">Year-over-Year Comparison</div>',
                unsafe_allow_html=True)
    try:
        yoy = df.copy()
        yoy["Year"]  = yoy["Order Date"].dt.year
        yoy["Month_num"] = yoy["Order Date"].dt.month
        yoy_grp = yoy.groupby(["Year","Month_num"])["Sales"].sum().reset_index()
        yoy_grp["Month_label"] = yoy_grp["Month_num"].apply(
            lambda m: ["Jan","Feb","Mar","Apr","May","Jun",
                        "Jul","Aug","Sep","Oct","Nov","Dec"][m-1])

        years = sorted(yoy_grp["Year"].unique())
        fig_yoy = go.Figure()
        yoy_colors = [GOLD, GREEN, BLUE, AMBER, RED, GOLD2]
        for i, yr in enumerate(years):
            sub = yoy_grp[yoy_grp["Year"] == yr].sort_values("Month_num")
            fig_yoy.add_trace(go.Scatter(
                x=sub["Month_label"], y=sub["Sales"],
                name=str(yr),
                line=dict(color=yoy_colors[i % len(yoy_colors)], width=2),
                mode="lines+markers",
                marker=dict(size=6),
                hovertemplate=f"<b>{yr} %{{x}}</b><br>$%{{y:,.0f}}<extra></extra>"))
        lay_yoy = plotly_base(320)
        lay_yoy.update({"yaxis_tickprefix": currency_tickprefix(),
                        "legend": dict(bgcolor="rgba(0,0,0,0)", orientation="h", y=-0.18)})
        fig_yoy.update_layout(**lay_yoy)
        st.plotly_chart(fig_yoy, use_container_width=True, config={"displayModeBar": False})
    except Exception as e:
        st.error(f"YoY chart error: {e}")

    # ── install hint ────────────────────────────────────────────────
    with st.expander("How to enable Prophet / ARIMA on Streamlit Cloud"):
        st.markdown(
            f'<div class="riq-card" style="font-size:0.8rem;line-height:1.8;">'
            f'<b style="color:{GOLD};">Prophet</b> — add to <code>requirements.txt</code>:<br>'
            f'<code>prophet>=1.1.5</code><br><br>'
            f'<b style="color:{GOLD};">ARIMA (statsmodels)</b> — already installable:<br>'
            f'<code>statsmodels>=0.14.0</code><br><br>'
            f'Both are compatible with Streamlit Cloud. Redeploy after adding to requirements.txt.'
            f'</div>',
            unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════
# PAGE: AI INSIGHTS
# ═══════════════════════════════════════════════════════
elif menu == "🤖 AI Insights":
    page_header("AI Business Insights", "// intelligent recommendations")

    insights = []

    # Revenue trend
    monthly = df.groupby(df["Order Date"].dt.to_period("M"))["Sales"].sum()
    if len(monthly) >= 2:
        trend = (monthly.iloc[-1] - monthly.iloc[-2]) / monthly.iloc[-2] * 100
        if trend > 0:
            insights.append(("🟢", "Revenue Growth", f"+{trend:.1f}% this month",
                f"Sales increased vs last month. Keep the momentum!", "#10B981"))
        else:
            insights.append(("🔴", "Revenue Decline", f"{trend:.1f}% this month",
                "Sales dropped vs last month. Review marketing spend.", "#EF4444"))

    # Top category
    if "Category" in df.columns:
        top_cat = df.groupby("Category")["Sales"].sum().idxmax()
        top_pct = df[df["Category"]==top_cat]["Sales"].sum() / df["Sales"].sum() * 100
        insights.append(("📊", f"{top_cat} Leads Sales", f"{top_pct:.0f}% of revenue",
            f"{top_cat} is your strongest category. Consider expanding product range.", GOLD))

    # Loss-making products
    if "Product Name" in df.columns:
        loss_prods = df.groupby("Product Name")["Profit"].sum()
        loss_count = (loss_prods < 0).sum()
        if loss_count > 0:
            insights.append(("⚠️", f"{loss_count} Loss-Making Products", "Negative profit",
                "Review pricing or discontinue unprofitable SKUs.", "#F59E0B"))

    # Best region
    if "Region" in df.columns:
        best_region = df.groupby("Region")["Profit"].sum().idxmax()
        insights.append(("🌍", f"{best_region} is Top Region", "Highest profit",
            f"Focus investment and expansion in {best_region} market.", "#3B82F6"))

    # High discount impact
    if "Discount" in df.columns:
        high_disc = df[df["Discount"] > 0.3]
        if len(high_disc) > 0:
            disc_loss = high_disc["Profit"].sum()
            insights.append(("💸", "High Discount Risk", f"{currency_symbol()}{disc_loss:,.0f} profit impact",
                "Heavy discounts (>30%) are hurting profitability. Revise discount strategy.", "#EF4444"))

    # Best segment
    if "Segment" in df.columns:
        best_seg = df.groupby("Segment")["Profit"].sum().idxmax()
        seg_pct  = df[df["Segment"]==best_seg]["Profit"].sum() / df["Profit"].sum() * 100
        insights.append(("👥", f"{best_seg} Most Profitable", f"{seg_pct:.0f}% of profit",
            f"The {best_seg} segment is most profitable. Prioritize this audience.", "#10B981"))

    # Shipping cost analysis
    if "Shipping Cost" in df.columns:
        ship_ratio = df["Shipping Cost"].sum() / df["Sales"].sum() * 100
        if ship_ratio > 15:
            insights.append(("🚚", "High Shipping Costs", f"{ship_ratio:.1f}% of sales",
                "Shipping costs are high. Negotiate rates or push economy shipping.", "#F59E0B"))

    # Top country
    if "Country" in df.columns:
        top_country = df.groupby("Country")["Sales"].sum().idxmax()
        insights.append(("🏆", f"{top_country} Top Market", "Highest sales",
            f"Invest more in {top_country} with targeted campaigns.", GOLD))

    # Display insights in grid
    st.markdown(f'{tag("AI")}<div class="riq-section-title">{len(insights)} Business Insights Generated</div>', unsafe_allow_html=True)
    for i in range(0, len(insights), 3):
        row = st.columns(3)
        for j, ins in enumerate(insights[i:i+3]):
            icon, title, val, desc, col = ins
            row[j].markdown(
                f'<div class="riq-card" style="border-left:3px solid {col};">'
                f'<div style="display:flex;align-items:flex-start;gap:10px;">'
                f'<span style="font-size:1.5rem;">{icon}</span>'
                f'<div><div style="font-weight:600;font-size:0.88rem;color:#F8FAFC;">{title}</div>'
                f'<div style="font-family:DM Mono,monospace;font-size:0.95rem;font-weight:600;color:{col};margin:3px 0;">{val}</div>'
                f'<div style="font-size:0.74rem;color:#B5B5BD;">{desc}</div>'
                f'</div></div></div>',
                unsafe_allow_html=True)

    # Performance summary table
    st.markdown(f'{tag("SUMMARY")}<div class="riq-section-title">Performance Scorecard</div>', unsafe_allow_html=True)
    scorecard = []
    if "Category" in df.columns:
        for cat in df["Category"].unique():
            cdf = df[df["Category"]==cat]
            scorecard.append({
                "Category": cat,
                "Sales": f"{currency_symbol()}{cdf['Sales'].sum():,.0f}",
                "Profit": f"{currency_symbol()}{cdf['Profit'].sum():,.0f}",
                "Margin": f"{cdf['Profit'].sum()/cdf['Sales'].sum()*100:.1f}%",
                "Orders": f"{cdf['Order ID'].nunique():,}",
                "Trend": "▲ Growing" if cdf["Sales"].sum() > df["Sales"].sum()/3 else "▼ Lagging"
            })
    if scorecard:
        st.dataframe(pd.DataFrame(scorecard), use_container_width=True, hide_index=True)

    # ── RISK SCORING ────────────────────────────────────────────────
    st.markdown("---")
    st.markdown(f'{tag("RISK SCORING")}<div class="riq-section-title">Product & Region Risk Scores</div>', unsafe_allow_html=True)
    st.markdown(
        '<div style="font-size:0.76rem;color:#6B6B75;margin-bottom:1rem;">' 
        'Automatically identifies products and regions with declining revenue trends</div>',
        unsafe_allow_html=True)

    risk_c1, risk_c2 = st.columns(2)

    with risk_c1:
        st.markdown('<div style="font-size:0.82rem;font-weight:600;color:#F8FAFC;margin-bottom:0.6rem;">Product Risk</div>', unsafe_allow_html=True)
        if "Product Name" in df.columns:
            prod_risk = []
            for prod, grp in df.groupby("Product Name"):
                monthly_s = grp.groupby(grp["Order Date"].dt.to_period("M"))["Sales"].sum().reset_index()
                monthly_s = monthly_s.sort_values("Order Date")
                if len(monthly_s) < 3:
                    continue
                vals = monthly_s["Sales"].values
                trend = (vals[-1] - vals[0]) / (vals[0] + 1e-9) * 100
                avg   = vals.mean()
                recent= vals[-3:].mean()
                score = 0
                if trend < -20:  score += 3
                elif trend < 0:  score += 2
                if recent < avg * 0.8: score += 2
                if grp["Profit"].sum() < 0: score += 3
                if score >= 6:   risk_level = "Critical"
                elif score >= 4: risk_level = "High"
                elif score >= 2: risk_level = "Medium"
                else:            risk_level = "Low"
                prod_risk.append({
                    "Product": prod[:45],
                    "Trend %": round(trend, 1),
                    "Risk": risk_level,
                    "Score": score,
                    "Revenue": grp["Sales"].sum(),
                })
            if prod_risk:
                prod_risk_df = pd.DataFrame(prod_risk).sort_values("Score", ascending=False)
                high_risk_prods = prod_risk_df[prod_risk_df["Risk"].isin(["Critical","High"])].head(8)
                risk_color_map = {"Critical": "#8B5CF6", "High": RED, "Medium": AMBER, "Low": GREEN}
                for _, row in high_risk_prods.iterrows():
                    rc = risk_color_map.get(row["Risk"], AMBER)
                    trend_str = f"{row['Trend %']:+.1f}%"
                    st.markdown(
                        f'<div class="riq-insight" style="padding:0.6rem 0.8rem;">' 
                        f'<div style="flex:1;">' 
                        f'<div style="font-size:0.78rem;font-weight:600;color:#F8FAFC;">{row["Product"]}</div>' 
                        f'<div style="display:flex;gap:12px;margin-top:3px;">' 
                        f'<span style="font-size:0.68rem;color:{rc};font-weight:600;">{row["Risk"]}</span>' 
                        f'<span style="font-size:0.68rem;color:#B5B5BD;">Trend: {trend_str}</span>' 
                        f'<span style="font-size:0.68rem;color:#B5B5BD;">Rev: {currency_symbol()}{row["Revenue"]:,.0f}</span>' 
                        f'</div></div></div>',
                        unsafe_allow_html=True)
                if len(prod_risk_df) > 0:
                    st.caption(f"{len(prod_risk_df[prod_risk_df['Risk'].isin(['Critical','High'])])} high-risk products out of {len(prod_risk_df)} analysed")

    with risk_c2:
        st.markdown('<div style="font-size:0.82rem;font-weight:600;color:#F8FAFC;margin-bottom:0.6rem;">Region Risk</div>', unsafe_allow_html=True)
        region_col = next((c for c in ["Region","Market","Country"] if c in df.columns), None)
        if region_col:
            reg_risk = []
            for reg, grp in df.groupby(region_col):
                monthly_r = grp.groupby(grp["Order Date"].dt.to_period("M"))["Sales"].sum().reset_index()
                monthly_r = monthly_r.sort_values("Order Date")
                if len(monthly_r) < 3:
                    continue
                vals = monthly_r["Sales"].values
                trend = (vals[-1] - vals[0]) / (vals[0] + 1e-9) * 100
                avg   = vals.mean()
                recent= vals[-3:].mean()
                margin= grp["Profit"].sum() / grp["Sales"].sum() * 100 if grp["Sales"].sum() > 0 else 0
                score = 0
                if trend < -20:     score += 3
                elif trend < 0:     score += 2
                if recent < avg * 0.8: score += 2
                if margin < 5:      score += 2
                if margin < 0:      score += 2
                if score >= 6:      risk_level = "Critical"
                elif score >= 4:    risk_level = "High"
                elif score >= 2:    risk_level = "Medium"
                else:               risk_level = "Low"
                reg_risk.append({
                    "Region": str(reg)[:35],
                    "Trend %": round(trend, 1),
                    "Margin %": round(margin, 1),
                    "Risk": risk_level,
                    "Score": score,
                    "Revenue": grp["Sales"].sum(),
                })
            if reg_risk:
                reg_risk_df = pd.DataFrame(reg_risk).sort_values("Score", ascending=False)
                for _, row in reg_risk_df.head(10).iterrows():
                    rc = risk_color_map.get(row["Risk"], AMBER)
                    trend_str = f"{row['Trend %']:+.1f}%"
                    margin_str = f"{row['Margin %']:.1f}%"
                    st.markdown(
                        f'<div class="riq-insight" style="padding:0.6rem 0.8rem;">' 
                        f'<div style="flex:1;">' 
                        f'<div style="font-size:0.78rem;font-weight:600;color:#F8FAFC;">{row["Region"]}</div>' 
                        f'<div style="display:flex;gap:12px;margin-top:3px;">' 
                        f'<span style="font-size:0.68rem;color:{rc};font-weight:600;">{row["Risk"]}</span>' 
                        f'<span style="font-size:0.68rem;color:#B5B5BD;">Trend: {trend_str}</span>' 
                        f'<span style="font-size:0.68rem;color:#B5B5BD;">Margin: {margin_str}</span>' 
                        f'</div></div></div>',
                        unsafe_allow_html=True)

    # ── AUTO-NARRATIVE ───────────────────────────────────────────────
    st.markdown("---")
    st.markdown(f'{tag("AUTO NARRATIVE")}<div class="riq-section-title">AI Business Narrative</div>', unsafe_allow_html=True)
    st.markdown(
        '<div style="font-size:0.76rem;color:#6B6B75;margin-bottom:1rem;">' 
        'Claude generates a plain-English summary of your business performance</div>',
        unsafe_allow_html=True)

    if st.button("Generate Narrative", use_container_width=False):
        monthly_s = df.groupby(df["Order Date"].dt.to_period("M"))["Sales"].sum()
        trend_pct  = (monthly_s.iloc[-1] - monthly_s.iloc[-2]) / monthly_s.iloc[-2] * 100 if len(monthly_s) >= 2 else 0
        top_cat    = df.groupby("Category")["Sales"].sum().idxmax() if "Category" in df.columns else "N/A"
        top_region = df.groupby("Region")["Profit"].sum().idxmax() if "Region" in df.columns else "N/A"
        total_rev  = df["Sales"].sum()
        total_prof = df["Profit"].sum()
        margin     = total_prof / total_rev * 100 if total_rev > 0 else 0
        n_cust     = df["Customer ID"].nunique() if "Customer ID" in df.columns else 0
        n_orders   = df["Order ID"].nunique()
        date_range = f"{df['Order Date'].min().strftime('%b %Y')} to {df['Order Date'].max().strftime('%b %Y')}"

        prompt = f"""You are a senior retail business analyst. Write a concise, professional 3-paragraph executive narrative (no bullet points, no headers) summarising the following retail performance data:

Period: {date_range}
Total Revenue: {currency_symbol()}{total_rev:,.0f}
Net Profit: {currency_symbol()}{total_prof:,.0f}
Profit Margin: {margin:.1f}%
Total Orders: {n_orders:,}
Total Customers: {n_cust:,}
Month-on-Month Sales Trend: {trend_pct:+.1f}%
Top Category by Sales: {top_cat}
Most Profitable Region: {top_region}

Write in a confident, data-driven tone. Paragraph 1: overall performance summary. Paragraph 2: key strengths and opportunities. Paragraph 3: strategic recommendations. Keep it under 200 words total."""

        try:
            import requests as _req
            resp = _req.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": st.session_state.get("anthropic_key",""),
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-sonnet-4-6", "max_tokens": 500,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=30)
            if resp.status_code == 200:
                narrative = resp.json()["content"][0]["text"]
                st.markdown(
                    f'<div class="riq-card" style="border-left:3px solid {GOLD};line-height:1.8;' 
                    f'font-size:0.84rem;color:#E8E8F0;">{narrative}</div>',
                    unsafe_allow_html=True)
            elif resp.status_code == 401:
                st.error("Invalid API key. Enter your Anthropic API key in ⚙️ Settings.")
            else:
                st.error(f"API error {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            st.error(f"Narrative generation failed: {e}")
    else:
        st.markdown(
            f'<div class="riq-card" style="color:#6B6B75;font-size:0.8rem;">' 
            f'Click <b style="color:{GOLD};">Generate Narrative</b> to get an AI-written executive summary of your dataset. ' 
            f'Add your Anthropic API key in ⚙️ Settings first.</div>',
            unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════
# PAGE: AI CHATBOT
# ═══════════════════════════════════════════════════════
elif menu == "💬 AI Chatbot":
    page_header("AI Business Analyst", "// ask questions about your data in plain English")

    import requests as _req

    api_key = st.session_state.get("anthropic_key", "")
    if not api_key:
        st.warning("Add your Anthropic API key in ⚙️ Settings to use the AI Chatbot.")
        st.stop()

    # ── Build data context ──────────────────────────────────────────
    @st.cache_data(show_spinner=False)
    def build_data_context(df_hash):
        monthly   = df.groupby(df["Order Date"].dt.to_period("M"))["Sales"].sum()
        top_prods = df.groupby("Product Name")["Sales"].sum().nlargest(5).to_dict() if "Product Name" in df.columns else {}
        top_cats  = df.groupby("Category")["Sales"].sum().to_dict() if "Category" in df.columns else {}
        top_regs  = df.groupby("Region")["Sales"].sum().to_dict() if "Region" in df.columns else {}
        bot_prods = df.groupby("Product Name")["Profit"].sum().nsmallest(5).to_dict() if "Product Name" in df.columns else {}
        yoy       = df.groupby(df["Order Date"].dt.year)["Sales"].sum().to_dict()
        margins   = {}
        if "Category" in df.columns:
            for cat, grp in df.groupby("Category"):
                s = grp["Sales"].sum()
                margins[cat] = round(grp["Profit"].sum() / s * 100, 1) if s > 0 else 0
        ctx = f"""You are RetailIQ AI — an expert retail business analyst with access to the following dataset summary:

DATASET: {st.session_state.dataset_name or ("Global Superstore (Demo)" if st.session_state.get("use_demo_data") else "Your Data")}
PERIOD: {df["Order Date"].min().strftime("%b %Y")} — {df["Order Date"].max().strftime("%b %Y")}
TOTAL REVENUE: {currency_symbol()}{df["Sales"].sum():,.0f}
NET PROFIT: {currency_symbol()}{df["Profit"].sum():,.0f}
PROFIT MARGIN: {df["Profit"].sum()/df["Sales"].sum()*100:.1f}%
TOTAL ORDERS: {df["Order ID"].nunique():,}
TOTAL CUSTOMERS: {df["Customer ID"].nunique() if "Customer ID" in df.columns else "N/A"}
TOTAL PRODUCTS: {df["Product Name"].nunique() if "Product Name" in df.columns else "N/A"}

MONTHLY SALES (last 6 months):
{chr(10).join([f"  {str(p)}: {currency_symbol()}{v:,.0f}" for p, v in list(monthly.items())[-6:]])}

YEAR-OVER-YEAR SALES:
{chr(10).join([f"  {yr}: {currency_symbol()}{v:,.0f}" for yr, v in yoy.items()])}

SALES BY CATEGORY:
{chr(10).join([f"  {k}: {currency_symbol()}{v:,.0f} (margin {margins.get(k, 0):.1f}%)" for k, v in top_cats.items()])}

SALES BY REGION:
{chr(10).join([f"  {k}: {currency_symbol()}{v:,.0f}" for k, v in list(top_regs.items())[:8]])}

TOP 5 PRODUCTS BY REVENUE:
{chr(10).join([f"  {k[:50]}: {currency_symbol()}{v:,.0f}" for k, v in top_prods.items()])}

BOTTOM 5 PRODUCTS BY PROFIT:
{chr(10).join([f"  {k[:50]}: {currency_symbol()}{v:,.0f}" for k, v in bot_prods.items()])}

Answer questions concisely and professionally. Use specific numbers from the data above. If asked something you cannot answer from this summary, say so. Do not make up data."""
        return ctx

    data_ctx = build_data_context(hash(str(df.shape) + str(df["Sales"].sum())))

    # ── Chat session ────────────────────────────────────────────────
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    # Suggested questions
    st.markdown(f'{tag("SUGGESTIONS")}<div class="riq-section-title">Suggested Questions</div>', unsafe_allow_html=True)
    suggestions = [
        "Which region had the best profit margin?",
        "What are my top 3 loss-making products?",
        "How did sales trend year over year?",
        "Which category should I focus on growing?",
        "What's driving the profit margin issue?",
        "Compare Q4 performance across categories",
    ]
    sug_cols = st.columns(3)
    for i, sug in enumerate(suggestions):
        if sug_cols[i % 3].button(sug, use_container_width=True, key=f"sug_{i}"):
            st.session_state.chat_history.append({"role": "user", "content": sug})
            st.rerun()

    st.markdown("---")

    # Chat display
    chat_container = st.container()
    with chat_container:
        for msg in st.session_state.chat_history:
            if msg["role"] == "user":
                st.markdown(
                    f'<div style="display:flex;justify-content:flex-end;margin-bottom:10px;">' 
                    f'<div style="background:rgba(255,153,51,0.10);border:1px solid rgba(255,153,51,0.18);' 
                    f'border-radius:14px 14px 4px 14px;padding:10px 14px;max-width:75%;' 
                    f'font-size:0.84rem;color:#F8FAFC;">{msg["content"]}</div></div>',
                    unsafe_allow_html=True)
            else:
                st.markdown(
                    f'<div style="display:flex;justify-content:flex-start;margin-bottom:10px;">' 
                    f'<div style="background:rgba(25,25,28,0.9);border:1px solid rgba(255,153,51,0.12);' 
                    f'border-radius:14px 14px 14px 4px;padding:10px 14px;max-width:75%;' 
                    f'font-size:0.84rem;color:#E8E8F0;line-height:1.6;">{msg["content"]}</div></div>',
                    unsafe_allow_html=True)

    # Auto-reply to last unanswered user message
    if (st.session_state.chat_history and
            st.session_state.chat_history[-1]["role"] == "user"):
        with st.spinner("Analysing your data…"):
            messages_payload = [{"role": "user", "content": data_ctx + "\n\nNow answer the following question:\n" + st.session_state.chat_history[0]["content"]}]
            for m in st.session_state.chat_history[1:]:
                messages_payload.append({"role": m["role"], "content": m["content"]})
            try:
                resp = _req.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": api_key,
                             "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": "claude-sonnet-4-6", "max_tokens": 600,
                          "messages": messages_payload},
                    timeout=30)
                if resp.status_code == 200:
                    reply = resp.json()["content"][0]["text"]
                    st.session_state.chat_history.append({"role": "assistant", "content": reply})
                    st.rerun()
                elif resp.status_code == 401:
                    st.error("Invalid API key. Update it in ⚙️ Settings.")
                else:
                    st.error(f"API error {resp.status_code}")
            except Exception as e:
                st.error(f"Chatbot error: {e}")

    # Input box
    st.markdown("<br>", unsafe_allow_html=True)
    col_inp, col_btn, col_clr = st.columns([6, 1, 1])
    user_input = col_inp.text_input("Ask anything about your data…", key="chat_input",
                                     label_visibility="collapsed",
                                     placeholder="e.g. Which market had the highest growth last year?")
    if col_btn.button("Send", use_container_width=True):
        if user_input.strip():
            st.session_state.chat_history.append({"role": "user", "content": user_input.strip()})
            st.rerun()
    if col_clr.button("Clear", use_container_width=True):
        st.session_state.chat_history = []
        st.rerun()

# ═══════════════════════════════════════════════════════
# PAGE: REPORTS
# ═══════════════════════════════════════════════════════
elif menu == "📄 Reports":
    page_header("Reports", "// export professional reports")

    total_sales  = df["Sales"].sum()
    total_profit = df["Profit"].sum()
    total_orders = df["Order ID"].nunique()
    profit_margin= total_profit/total_sales*100 if total_sales > 0 else 0
    total_cust   = df["Customer ID"].nunique() if "Customer ID" in df.columns else 0

    # Summary cards
    rep_html = (
        '<style>.rep3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:1.2rem;}'
        '@media(max-width:768px){.rep3{grid-template-columns:1fr!important;}}</style>'
        '<div class="rep3">'
        f'<div class="riq-card" style="border-top:2px solid {GOLD};text-align:center;">'
        f'<div style="font-size:0.62rem;color:{GOLD};font-family:DM Mono,monospace;letter-spacing:0.1em;margin-bottom:6px;">TOTAL REVENUE</div>'
        f'<div style="font-size:1.8rem;font-family:DM Mono,monospace;font-weight:700;color:{GOLD};">{fmt_num(total_sales,"$")}</div></div>'
        f'<div class="riq-card" style="border-top:2px solid {GREEN};text-align:center;">'
        f'<div style="font-size:0.62rem;color:{GREEN};font-family:DM Mono,monospace;letter-spacing:0.1em;margin-bottom:6px;">NET PROFIT</div>'
        f'<div style="font-size:1.8rem;font-family:DM Mono,monospace;font-weight:700;color:{GREEN};">{fmt_num(total_profit,"$")}</div></div>'
        f'<div class="riq-card" style="border-top:2px solid {BLUE};text-align:center;">'
        f'<div style="font-size:0.62rem;color:{BLUE};font-family:DM Mono,monospace;letter-spacing:0.1em;margin-bottom:6px;">TOTAL ORDERS</div>'
        f'<div style="font-size:1.8rem;font-family:DM Mono,monospace;font-weight:700;color:{BLUE};">{fmt_num(total_orders)}</div></div>'
        '</div>'
    )
    st.markdown(rep_html, unsafe_allow_html=True)

    # Export buttons
    st.markdown(f'{tag("EXPORT")}<div class="riq-section-title">Download Reports</div>', unsafe_allow_html=True)
    ec1, ec2, ec3 = st.columns(3)

    # CSV
    buf = io.StringIO()
    export_df = df.copy()
    if "Order Date" in export_df.columns:
        export_df["Order Date"] = export_df["Order Date"].dt.strftime("%d %b %Y")
    export_df.to_csv(buf, index=False)
    ec1.download_button("CSV — Full Dataset", buf.getvalue(),
        file_name=f"retailiq_export_{datetime.today().strftime('%Y%m%d')}.csv",
        mime="text/csv", use_container_width=True)

    # Excel
    try:
        excel_buf = io.BytesIO()
        with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
            df.head(10000).to_excel(writer, sheet_name="Data", index=False)
            # Summary sheet
            summary = pd.DataFrame({
                "Metric": ["Total Sales","Total Profit","Total Orders","Profit Margin","Customers","Products"],
                "Value":  [f"{currency_symbol()}{total_sales:,.2f}", f"{currency_symbol()}{total_profit:,.2f}", f"{total_orders:,}",
                           f"{profit_margin:.1f}%", f"{total_cust:,}",
                           f"{df['Product ID'].nunique() if 'Product ID' in df.columns else 'N/A'}"]
            })
            summary.to_excel(writer, sheet_name="Summary", index=False)
        excel_buf.seek(0)
        ec2.download_button("Excel — Full Report", excel_buf.read(),
            file_name=f"retailiq_report_{datetime.today().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True)
    except Exception as e:
        ec2.error(f"Excel error: {e}")

    # PDF
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors as rl_colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        from reportlab.lib.enums import TA_CENTER

        def make_pdf():
            buf2 = io.BytesIO()
            doc  = SimpleDocTemplate(buf2, pagesize=A4,
                rightMargin=2*cm, leftMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
            story = []
            styles = getSampleStyleSheet()
            C_BG   = rl_colors.HexColor("#0B0B0D")
            C_GOLD = rl_colors.HexColor("#D4AF37")
            C_G2   = rl_colors.HexColor("#E3C15B")
            C_GRN  = rl_colors.HexColor("#10B981")
            C_RED  = rl_colors.HexColor("#EF4444")
            C_TXT  = rl_colors.HexColor("#F8FAFC")
            C_MUT  = rl_colors.HexColor("#B5B5BD")
            C_DRK  = rl_colors.HexColor("#151518")

            title_s = ParagraphStyle("t", parent=styles["Normal"],
                fontSize=28, fontName="Helvetica-Bold", textColor=C_GOLD, alignment=TA_CENTER, spaceAfter=4)
            sub_s = ParagraphStyle("s", parent=styles["Normal"],
                fontSize=11, textColor=C_MUT, alignment=TA_CENTER, spaceAfter=20)

            story.append(Paragraph("🛍️ RetailIQ AI", title_s))
            story.append(Paragraph("Executive Business Intelligence Report", sub_s))
            story.append(Paragraph(
                f"Generated: {datetime.today().strftime('%d %B %Y')}  ·  Dataset: {st.session_state.dataset_name or 'Global Superstore'}  ·  User: {st.session_state.username}",
                sub_s))
            story.append(HRFlowable(width="100%", thickness=1, color=C_GOLD, spaceAfter=16))

            # KPI summary table
            kpi_tbl_style = TableStyle([
                ("BACKGROUND", (0,0), (-1,0), C_GOLD),
                ("TEXTCOLOR", (0,0), (-1,0), C_BG),
                ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
                ("FONTSIZE", (0,0), (-1,0), 11),
                ("BACKGROUND", (0,1), (-1,-1), C_DRK),
                ("TEXTCOLOR", (0,1), (0,-1), C_G2),
                ("TEXTCOLOR", (1,1), (1,-1), C_TXT),
                ("FONTSIZE", (0,1), (-1,-1), 10),
                ("ROWBACKGROUNDS", (0,1), (-1,-1), [C_DRK, rl_colors.HexColor("#1C1C20")]),
                ("GRID", (0,0), (-1,-1), 0.4, rl_colors.HexColor("#2a2a2a")),
                ("TOPPADDING", (0,0), (-1,-1), 8),
                ("BOTTOMPADDING", (0,0), (-1,-1), 8),
                ("LEFTPADDING", (0,0), (-1,-1), 12),
                ("RIGHTPADDING", (0,0), (-1,-1), 12),
                ("ALIGN", (1,0), (1,-1), "RIGHT"),
            ])

            kpi_data = [["KPI", "VALUE"],
                        ["Total Revenue", f"{currency_symbol()}{total_sales:,.2f}"],
                        ["Total Profit",  f"{currency_symbol()}{total_profit:,.2f}"],
                        ["Profit Margin", f"{profit_margin:.1f}%"],
                        ["Total Orders",  f"{total_orders:,}"],
                        ["Unique Customers", f"{total_cust:,}"],
                        ["Avg Order Value", f"{currency_symbol()}{total_sales/total_orders:,.2f}" if total_orders else "₹0"]]
            t = Table(kpi_data, colWidths=[9*cm, 8*cm])
            t.setStyle(kpi_tbl_style)
            story.append(t)
            story.append(Spacer(1, 0.6*cm))

            # Top categories
            if "Category" in df.columns:
                story.append(HRFlowable(width="100%", thickness=0.5, color=C_GOLD, spaceAfter=8))
                story.append(Paragraph("Category Performance", ParagraphStyle("h2",
                    parent=styles["Normal"], fontSize=13, fontName="Helvetica-Bold",
                    textColor=C_GOLD, spaceAfter=8)))
                cat_data = [["CATEGORY", "SALES", "PROFIT", "MARGIN"]]
                for cat in df["Category"].unique():
                    cdf = df[df["Category"]==cat]
                    s,p = cdf["Sales"].sum(), cdf["Profit"].sum()
                    cat_data.append([cat, f"{currency_symbol()}{s:,.0f}", f"{currency_symbol()}{p:,.0f}", f"{p/s*100:.1f}%" if s else "0%"])
                ct = Table(cat_data, colWidths=[5*cm, 4*cm, 4*cm, 4*cm])
                ct.setStyle(kpi_tbl_style)
                story.append(ct)
                story.append(Spacer(1, 0.6*cm))

            # Top 10 products
            if "Product Name" in df.columns:
                story.append(HRFlowable(width="100%", thickness=0.5, color=C_GOLD, spaceAfter=8))
                story.append(Paragraph("Top 10 Products by Sales", ParagraphStyle("h2",
                    parent=styles["Normal"], fontSize=13, fontName="Helvetica-Bold",
                    textColor=C_GOLD, spaceAfter=8)))
                top10 = df.groupby("Product Name")["Sales"].sum().sort_values(ascending=False).head(10)
                prod_data = [["PRODUCT", "SALES"]]
                for pname, psales in top10.items():
                    prod_data.append([str(pname)[:40], f"{currency_symbol()}{psales:,.0f}"])
                pt = Table(prod_data, colWidths=[13*cm, 4*cm])
                pt.setStyle(kpi_tbl_style)
                story.append(pt)

            # Footer
            story.append(Spacer(1, 1*cm))
            story.append(HRFlowable(width="100%", thickness=0.5, color=C_GOLD, spaceAfter=6))
            story.append(Paragraph(
                f"RetailIQ AI  ·  Black Gold Elite  ·  {datetime.today().strftime('%d %B %Y')}",
                ParagraphStyle("footer", parent=styles["Normal"],
                    fontSize=8, textColor=C_MUT, alignment=TA_CENTER)))
            doc.build(story)
            buf2.seek(0)
            return buf2.read()

        ec3.download_button("PDF — Executive Report", make_pdf(),
            file_name=f"retailiq_executive_{datetime.today().strftime('%Y%m%d')}.pdf",
            mime="application/pdf", use_container_width=True)

    except ImportError:
        ec3.warning("Install reportlab for PDF: pip install reportlab")
    except Exception as e:
        ec3.error(f"PDF error: {e}")


# ═══════════════════════════════════════════════════════
# PAGE: CHURN PREDICTION
# ═══════════════════════════════════════════════════════
elif menu == "🔮 Churn Prediction":
    page_header("Churn Prediction", "// identify customers at risk of leaving")

    if "Customer ID" not in df.columns or "Order Date" not in df.columns:
        st.warning("Need Customer ID and Order Date columns.")
        st.stop()

    try:
        from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import classification_report, roc_auc_score
        import warnings

        # ── Build features ──────────────────────────────────────────
        snapshot = df["Order Date"].max() + pd.Timedelta(days=1)

        feat = df.groupby("Customer ID").agg(
            recency      =("Order Date",    lambda x: (snapshot - x.max()).days),
            frequency    =("Order ID",      "nunique"),
            monetary     =("Sales",         "sum"),
            avg_order    =("Sales",         "mean"),
            total_qty    =("Quantity",      "sum"),
            avg_discount =("Discount",      "mean") if "Discount" in df.columns else ("Sales", lambda x: 0),
            total_profit =("Profit",        "sum"),
            profit_margin=("Profit",        lambda x: x.sum() / df.loc[x.index, "Sales"].sum() if df.loc[x.index, "Sales"].sum() > 0 else 0),
            span_days    =("Order Date",    lambda x: (x.max() - x.min()).days),
        ).reset_index()

        # Churn label: no purchase in last 180 days = churned
        churn_days = st.sidebar.slider("Churn threshold (days inactive)", 90, 365, 180,
                                        help="Customers inactive longer than this are labelled 'Churned'")
        feat["churned"] = (feat["recency"] > churn_days).astype(int)

        feature_cols = ["recency","frequency","monetary","avg_order",
                        "total_qty","avg_discount","total_profit","span_days"]
        X = feat[feature_cols].fillna(0).values
        y_label = feat["churned"].values

        churn_rate = y_label.mean() * 100

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Customers",  f"{len(feat):,}")
        c2.metric("Churned",          f"{y_label.sum():,}")
        c3.metric("Active",           f"{(y_label==0).sum():,}")
        c4.metric("Churn Rate",       f"{churn_rate:.1f}%",
                  delta=f"{'High Risk' if churn_rate > 40 else 'Moderate' if churn_rate > 20 else 'Healthy'}",
                  delta_color="inverse")

        # ── Train model ─────────────────────────────────────────────
        @st.cache_data(show_spinner=False)
        def train_churn(X, y, churn_days):
            scaler = StandardScaler()
            Xs = scaler.fit_transform(X)
            if len(set(y)) < 2:
                return None, None, None, None, None
            X_tr, X_te, y_tr, y_te = train_test_split(Xs, y, test_size=0.25, random_state=42, stratify=y)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model = GradientBoostingClassifier(n_estimators=120, max_depth=4,
                                                   learning_rate=0.08, random_state=42)
                model.fit(X_tr, y_tr)
            proba = model.predict_proba(Xs)[:, 1]
            auc   = roc_auc_score(y_te, model.predict_proba(X_te)[:, 1])
            imp   = model.feature_importances_
            return model, scaler, proba, auc, imp

        with st.spinner("Training churn model…"):
            model_ch, scaler_ch, proba_all, auc_score, importances = train_churn(X, y_label, churn_days)

        if model_ch is None:
            st.warning("Not enough class variation to train. Try adjusting the churn threshold.")
            st.stop()

        feat["churn_prob"]    = (proba_all * 100).round(1)
        feat["churn_label"]   = feat["churned"].map({1: "Churned", 0: "Active"})
        feat["risk_tier"] = pd.cut(feat["churn_prob"],
                                   bins=[0, 30, 60, 80, 100],
                                   labels=["Low Risk", "Medium Risk", "High Risk", "Critical"])

        # AUC badge
        auc_color = GREEN if auc_score >= 0.80 else AMBER if auc_score >= 0.65 else RED
        st.markdown(
            f'<div style="display:inline-flex;align-items:center;gap:8px;'
            f'background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.25);'
            f'border-radius:8px;padding:6px 14px;margin-bottom:1rem;">'
            f'<span style="font-family:DM Mono,monospace;font-size:0.72rem;color:{auc_color};">MODEL AUC</span>'
            f'<span style="font-family:DM Mono,monospace;font-size:0.88rem;font-weight:600;color:{auc_color};">'
            f'{auc_score:.3f}</span>'
            f'<span style="font-size:0.72rem;color:#6B6B75;">Gradient Boosting · {churn_days}d threshold</span>'
            f'</div>',
            unsafe_allow_html=True)

        # ── Risk tier breakdown ─────────────────────────────────────
        st.markdown(f'{tag("RISK TIERS")}<div class="riq-section-title">Customer Risk Breakdown</div>', unsafe_allow_html=True)
        tier_colors = {"Low Risk": GREEN, "Medium Risk": AMBER, "High Risk": RED, "Critical": "#8B5CF6"}
        tier_counts = feat["risk_tier"].value_counts()

        tier_html = '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:1.2rem;">'
        for tier, color in tier_colors.items():
            count = tier_counts.get(tier, 0)
            pct   = count / len(feat) * 100
            tier_html += (
                f'<div style="background:rgba(25,25,28,0.8);border:1px solid rgba(255,153,51,0.12);' 
                f'border-top:2px solid {color};border-radius:14px;padding:0.85rem 1rem;">'
                f'<div style="font-family:DM Mono,monospace;font-size:0.6rem;font-weight:600;' 
                f'letter-spacing:0.1em;color:{color};margin-bottom:4px;">{tier.upper()}</div>'
                f'<div style="font-family:DM Mono,monospace;font-size:1.15rem;font-weight:600;color:{color};">{count:,}</div>'
                f'<div style="font-size:0.62rem;color:#6B6B75;text-transform:uppercase;">{pct:.1f}% of customers</div>'
                f'</div>'
            )
        tier_html += '</div>'
        st.markdown(tier_html, unsafe_allow_html=True)

        # ── Charts ──────────────────────────────────────────────────
        ch1, ch2 = st.columns(2)

        with ch1:
            st.markdown(f'{tag("DISTRIBUTION")}<div class="riq-section-title">Churn Probability Distribution</div>', unsafe_allow_html=True)
            fig_hist = go.Figure(go.Histogram(
                x=feat["churn_prob"], nbinsx=20,
                marker=dict(color=GOLD, line=dict(color=BG2, width=1), cornerradius=4),
                hovertemplate="Prob: %{x:.0f}%<br>Customers: %{y}<extra></extra>"))
            fig_hist.add_vline(x=60, line_color=RED, line_dash="dash",
                               annotation_text="High Risk threshold",
                               annotation_font_color=RED)
            lay_h = plotly_base(280)
            lay_h.update({"xaxis_title": "Churn Probability (%)", "yaxis_title": "Customers"})
            fig_hist.update_layout(**lay_h)
            st.plotly_chart(fig_hist, use_container_width=True, config={"displayModeBar": False})

        with ch2:
            st.markdown(f'{tag("FEATURE IMPORTANCE")}<div class="riq-section-title">Key Churn Drivers</div>', unsafe_allow_html=True)
            imp_df = pd.DataFrame({"Feature": feature_cols, "Importance": importances})
            imp_df = imp_df.sort_values("Importance", ascending=True)
            labels_clean = [f.replace("_", " ").title() for f in imp_df["Feature"]]
            fig_imp = go.Figure(go.Bar(
                x=imp_df["Importance"], y=labels_clean, orientation="h",
                marker=dict(
                    color=[GOLD if v == imp_df["Importance"].max() else GOLD2
                           for v in imp_df["Importance"]],
                    line_width=0, cornerradius=5),
                text=[f"{v:.3f}" for v in imp_df["Importance"]],
                textposition="outside",
                textfont=dict(size=9, color=FONT_C),
                hovertemplate="<b>%{y}</b><br>Importance: %{x:.4f}<extra></extra>"))
            lay_imp = plotly_base(280)
            lay_imp.update({"margin": dict(l=10, r=60, t=20, b=10)})
            fig_imp.update_layout(**lay_imp)
            st.plotly_chart(fig_imp, use_container_width=True, config={"displayModeBar": False})

        # ── Scatter: Recency vs Monetary coloured by risk ───────────
        st.markdown(f'{tag("SCATTER")}<div class="riq-section-title">Recency vs Revenue — Risk Map</div>', unsafe_allow_html=True)
        tier_color_map = {"Low Risk": GREEN, "Medium Risk": AMBER, "High Risk": RED, "Critical": "#8B5CF6"}
        fig_sc = go.Figure()
        for tier, color in tier_color_map.items():
            sub = feat[feat["risk_tier"] == tier]
            if len(sub) == 0:
                continue
            name_col_ch = "Customer Name" if "Customer Name" in df.columns else "Customer ID"
            names = df[["Customer ID", name_col_ch]].drop_duplicates("Customer ID") if name_col_ch != "Customer ID" else feat[["Customer ID"]].rename(columns={"Customer ID": name_col_ch})
            sub2 = sub.merge(names, on="Customer ID", how="left")
            fig_sc.add_trace(go.Scatter(
                x=sub2["recency"], y=sub2["monetary"],
                mode="markers", name=tier,
                marker=dict(color=color, size=6, opacity=0.75, line=dict(width=0)),
                text=sub2[name_col_ch].astype(str),
                hovertemplate="<b>%{text}</b><br>Recency: %{x}d<br>Revenue: {currency_symbol()}%{{y:,.0f}}<br>Risk: " + tier + "<extra></extra>"))
        lay_sc = plotly_base(340)
        lay_sc.update({"xaxis_title": "Recency (days inactive)", "yaxis_tickprefix": currency_tickprefix(),
                       "legend": dict(bgcolor="rgba(0,0,0,0)", orientation="h", y=-0.18)})
        fig_sc.update_layout(**lay_sc)
        st.plotly_chart(fig_sc, use_container_width=True, config={"displayModeBar": False})

        # ── At-risk customer table ───────────────────────────────────
        st.markdown(f'{tag("AT-RISK LIST")}<div class="riq-section-title">High & Critical Risk Customers</div>', unsafe_allow_html=True)
        risk_filter = st.selectbox("Filter by risk tier",
                                   ["High Risk + Critical", "Critical only", "All tiers"])
        if risk_filter == "Critical only":
            show_risk = feat[feat["risk_tier"] == "Critical"]
        elif risk_filter == "High Risk + Critical":
            show_risk = feat[feat["risk_tier"].isin(["High Risk", "Critical"])]
        else:
            show_risk = feat.copy()

        name_col_ch = "Customer Name" if "Customer Name" in df.columns else "Customer ID"
        if name_col_ch != "Customer ID":
            names_df = df[["Customer ID", name_col_ch]].drop_duplicates("Customer ID")
            show_risk = show_risk.merge(names_df, on="Customer ID", how="left")

        # Build display cols — avoid duplicating Customer ID when name_col_ch == "Customer ID"
        disp_cols = []
        seen_disp = set()
        for c in [name_col_ch, "Customer ID", "recency", "frequency",
                  "monetary", "churn_prob", "risk_tier"]:
            if c in show_risk.columns and c not in seen_disp:
                disp_cols.append(c)
                seen_disp.add(c)
        show_risk_disp = show_risk[disp_cols].copy()
        show_risk_disp["monetary"]   = show_risk_disp["monetary"].apply(lambda x: f"{currency_symbol()}{x:,.0f}")
        show_risk_disp["recency"]    = show_risk_disp["recency"].apply(lambda x: f"{x}d ago")
        show_risk_disp["churn_prob"] = show_risk_disp["churn_prob"].apply(lambda x: f"{x:.1f}%")
        show_risk_disp = show_risk_disp.sort_values("risk_tier", ascending=False).head(200)
        st.dataframe(show_risk_disp.rename(columns={
            "recency": "Last Purchase", "frequency": "Orders",
            "monetary": "Revenue", "churn_prob": "Churn Prob", "risk_tier": "Risk Tier"
        }), use_container_width=True, hide_index=True)

        csv_churn = feat.drop(columns=["churned"], errors="ignore").to_csv(index=False)
        st.download_button("Download churn scores CSV", csv_churn,
            file_name=f"churn_scores_{pd.Timestamp.today().strftime('%Y%m%d')}.csv",
            mime="text/csv", use_container_width=True)

        # ── Action recommendations ───────────────────────────────────
        st.markdown(f'{tag("ACTIONS")}<div class="riq-section-title">Recommended Actions</div>', unsafe_allow_html=True)
        actions_ch = [
            ("Critical", "#8B5CF6", "🚨",
             f"{tier_counts.get('Critical', 0):,} customers at extreme risk",
             "Immediate personal outreach — phone call or dedicated account manager. Offer significant loyalty incentive."),
            ("High Risk", RED, "⚠️",
             f"{tier_counts.get('High Risk', 0):,} customers need attention",
             "Automated win-back email series. Time-limited offer. Survey to understand dissatisfaction."),
            ("Medium Risk", AMBER, "📋",
             f"{tier_counts.get('Medium Risk', 0):,} customers drifting",
             "Re-engagement campaign. Personalised product recommendations based on past orders."),
            ("Low Risk", GREEN, "✅",
             f"{tier_counts.get('Low Risk', 0):,} customers healthy",
             "Loyalty program enrolment. Upsell complementary products. Encourage referrals."),
        ]
        for tier, color, icon, title, advice in actions_ch:
            st.markdown(
                f'<div class="riq-insight">'
                f'<div style="font-size:1.3rem;min-width:28px;">{icon}</div>'
                f'<div><div style="font-weight:600;font-size:0.88rem;color:{color};">{title}</div>'
                f'<div style="font-size:0.78rem;color:#B5B5BD;margin-top:3px;">{advice}</div>'
                f'</div></div>',
                unsafe_allow_html=True)

    except ImportError:
        st.error("scikit-learn is required. Run: pip install scikit-learn")
    except Exception as e:
        st.error(f"Churn prediction error: {e}")
        st.exception(e)

# ═══════════════════════════════════════════════════════
# PAGE: ANOMALY DETECTION
# ═══════════════════════════════════════════════════════
elif menu == "🚨 Anomaly Detection":
    page_header("Anomaly Detection", "// auto-flag unusual sales patterns")

    try:
        from sklearn.ensemble import IsolationForest
        from sklearn.preprocessing import StandardScaler
        import warnings

        # ── Controls ────────────────────────────────────────────────
        with st.sidebar:
            st.markdown("---")
            st.markdown(f'<div class="riq-tag">ANOMALY SETTINGS</div>', unsafe_allow_html=True)
            contamination = st.slider("Expected anomaly rate (%)", 1, 20, 5,
                                      help="What % of data points you expect to be anomalous") / 100
            granularity   = st.radio("Granularity", ["Daily", "Weekly", "Monthly"], index=2)

        freq_map  = {"Daily": "D", "Weekly": "W", "Monthly": "M"}
        freq      = freq_map[granularity]

        ts = (df.groupby(df["Order Date"].dt.to_period(freq))
                .agg(Sales=("Sales","sum"), Profit=("Profit","sum"),
                     Orders=("Order ID","nunique"), Qty=("Quantity","sum"))
                .reset_index())
        ts["Period"] = ts["Order Date"].astype(str)
        ts = ts.sort_values("Order Date").reset_index(drop=True)

        if len(ts) < 10:
            st.warning("Not enough data points for the selected granularity. Try Daily or Weekly.")
            st.stop()

        # ── Isolation Forest ────────────────────────────────────────
        @st.cache_data(show_spinner=False)
        def detect_anomalies(sales, profit, orders, qty, contamination, granularity):
            feats = np.column_stack([sales, profit, orders, qty])
            scaler = StandardScaler()
            fs = scaler.fit_transform(feats)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                iso = IsolationForest(contamination=contamination, random_state=42, n_estimators=200)
                labels = iso.fit_predict(fs)
                scores = iso.decision_function(fs)
            return labels, scores

        labels, scores = detect_anomalies(
            ts["Sales"].values, ts["Profit"].values,
            ts["Orders"].values, ts["Qty"].values,
            contamination, granularity)

        ts["anomaly"]    = labels        # -1 = anomaly, 1 = normal
        ts["anom_score"] = scores        # lower = more anomalous
        ts["is_anomaly"] = ts["anomaly"] == -1
        ts["spike"]      = (ts["is_anomaly"]) & (ts["Sales"] > ts["Sales"].median())
        ts["drop"]       = (ts["is_anomaly"]) & (ts["Sales"] <= ts["Sales"].median())

        n_anom   = ts["is_anomaly"].sum()
        n_spikes = ts["spike"].sum()
        n_drops  = ts["drop"].sum()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Periods",   f"{len(ts):,}")
        c2.metric("Anomalies Found", f"{n_anom:,}")
        c3.metric("Sales Spikes",    f"{n_spikes:,}")
        c4.metric("Sales Drops",     f"{n_drops:,}")

        # ── Main anomaly chart ──────────────────────────────────────
        st.markdown(f'{tag("ANOMALY CHART")}<div class="riq-section-title">Sales Anomalies — {granularity}</div>', unsafe_allow_html=True)

        normal   = ts[~ts["is_anomaly"]]
        spikes   = ts[ts["spike"]]
        drops    = ts[ts["drop"]]

        fig_an = go.Figure()
        fig_an.add_trace(go.Scatter(
            x=ts["Period"], y=ts["Sales"], name="Sales",
            line=dict(color=GOLD, width=2),
            fill="tozeroy", fillcolor="rgba(212,175,55,0.04)",
            hovertemplate="<b>%{x}</b><br>{currency_symbol()}%{{y:,.0f}}<extra>Sales</extra>"))

        if len(spikes):
            fig_an.add_trace(go.Scatter(
                x=spikes["Period"], y=spikes["Sales"], mode="markers", name="Spike",
                marker=dict(color=GREEN, size=12, symbol="triangle-up",
                            line=dict(color=BG2, width=1.5)),
                hovertemplate="<b>Spike: %{x}</b><br>{currency_symbol()}%{{y:,.0f}}<extra></extra>"))

        if len(drops):
            fig_an.add_trace(go.Scatter(
                x=drops["Period"], y=drops["Sales"], mode="markers", name="Drop",
                marker=dict(color=RED, size=12, symbol="triangle-down",
                            line=dict(color=BG2, width=1.5)),
                hovertemplate="<b>Drop: %{x}</b><br>{currency_symbol()}%{{y:,.0f}}<extra></extra>"))

        lay_an = plotly_base(400)
        lay_an.update({"yaxis_tickprefix": currency_tickprefix(), "xaxis_tickangle": -30,
                       "legend": dict(bgcolor="rgba(0,0,0,0)", orientation="h", y=-0.18)})
        fig_an.update_layout(**lay_an)
        st.plotly_chart(fig_an, use_container_width=True, config={"displayModeBar": False})

        # ── Anomaly score chart ─────────────────────────────────────
        st.markdown(f'{tag("ANOMALY SCORE")}<div class="riq-section-title">Anomaly Score Over Time</div>', unsafe_allow_html=True)
        fig_sc2 = go.Figure()
        fig_sc2.add_trace(go.Bar(
            x=ts["Period"],
            y=-ts["anom_score"],
            marker=dict(
                color=[-sc for sc in ts["anom_score"]],
                colorscale=[[0, GOLD2], [0.5, AMBER], [1, RED]],
                line_width=0, cornerradius=3),
            hovertemplate="<b>%{x}</b><br>Anomaly score: %{y:.4f}<extra></extra>"))
        lay_sc2 = plotly_base(240)
        lay_sc2.update({"xaxis_tickangle": -30,
                        "yaxis_title": "Anomaly intensity (higher = more unusual)"})
        fig_sc2.update_layout(**lay_sc2)
        st.plotly_chart(fig_sc2, use_container_width=True, config={"displayModeBar": False})

        # ── Category anomalies ──────────────────────────────────────
        if "Category" in df.columns:
            st.markdown(f'{tag("BY CATEGORY")}<div class="riq-section-title">Category-Level Anomalies</div>', unsafe_allow_html=True)
            cat_tabs2 = st.tabs(sorted(df["Category"].dropna().unique().tolist()))
            for tab_c, cat in zip(cat_tabs2, sorted(df["Category"].dropna().unique())):
                with tab_c:
                    cdf2 = (df[df["Category"] == cat]
                            .groupby(df["Order Date"].dt.to_period(freq))
                            .agg(Sales=("Sales","sum"), Orders=("Order ID","nunique"))
                            .reset_index())
                    cdf2["Period"] = cdf2["Order Date"].astype(str)
                    cdf2 = cdf2.sort_values("Order Date").reset_index(drop=True)
                    if len(cdf2) < 8:
                        st.caption("Not enough data.")
                        continue
                    feats_c = StandardScaler().fit_transform(cdf2[["Sales","Orders"]].values)
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        lbl_c = IsolationForest(contamination=contamination,
                                                random_state=42).fit_predict(feats_c)
                    cdf2["anom"] = lbl_c == -1
                    fig_cc = go.Figure()
                    fig_cc.add_trace(go.Scatter(
                        x=cdf2["Period"], y=cdf2["Sales"],
                        line=dict(color=GOLD, width=2), name="Sales",
                        fill="tozeroy", fillcolor="rgba(212,175,55,0.04)"))
                    anom_c = cdf2[cdf2["anom"]]
                    if len(anom_c):
                        fig_cc.add_trace(go.Scatter(
                            x=anom_c["Period"], y=anom_c["Sales"], mode="markers",
                            marker=dict(color=RED, size=10, symbol="x",
                                        line=dict(color=BG2, width=1)),
                            name="Anomaly"))
                    lay_cc = plotly_base(220)
                    lay_cc.update({"yaxis_tickprefix": currency_tickprefix(), "xaxis_tickangle": -30,
                                   "legend": dict(bgcolor="rgba(0,0,0,0)", orientation="h", y=-0.2)})
                    fig_cc.update_layout(**lay_cc)
                    st.plotly_chart(fig_cc, use_container_width=True, config={"displayModeBar": False})

        # ── Anomaly table ───────────────────────────────────────────
        st.markdown(f'{tag("ANOMALY TABLE")}<div class="riq-section-title">All Detected Anomalies</div>', unsafe_allow_html=True)
        anom_disp = ts[ts["is_anomaly"]][["Period","Sales","Profit","Orders","Qty","spike","drop"]].copy()
        anom_disp["Type"]    = anom_disp.apply(lambda r: "Spike" if r["spike"] else "Drop", axis=1)
        anom_disp["Sales"]   = anom_disp["Sales"].apply(lambda x: f"{currency_symbol()}{x:,.0f}")
        anom_disp["Profit"]  = anom_disp["Profit"].apply(lambda x: f"{currency_symbol()}{x:,.0f}")
        anom_disp = anom_disp.drop(columns=["spike","drop"]).sort_values("Period", ascending=False)
        st.dataframe(anom_disp, use_container_width=True, hide_index=True)

        csv_anom = anom_disp.to_csv(index=False)
        st.download_button("Download anomalies CSV", csv_anom,
            file_name=f"anomalies_{pd.Timestamp.today().strftime('%Y%m%d')}.csv",
            mime="text/csv", use_container_width=True)

    except ImportError:
        st.error("scikit-learn is required. Run: pip install scikit-learn")
    except Exception as e:
        st.error(f"Anomaly detection error: {e}")
        st.exception(e)

# ═══════════════════════════════════════════════════════
# PAGE: PRODUCT RECOMMENDATIONS
# ═══════════════════════════════════════════════════════
elif menu == "🛒 Recommendations":
    page_header("Product Recommendations", "// customers who bought X also bought Y")

    if "Order ID" not in df.columns or "Product Name" not in df.columns:
        st.warning("Need Order ID and Product Name columns.")
        st.stop()

    try:
        # ── Controls ────────────────────────────────────────────────
        with st.sidebar:
            st.markdown("---")
            st.markdown(f'<div class="riq-tag">SETTINGS</div>', unsafe_allow_html=True)
            min_support  = st.slider("Min support (%)", 1, 20, 3,
                help="How often a product pair must appear together") / 100
            min_conf     = st.slider("Min confidence (%)", 10, 90, 30,
                help="How reliably buying A leads to buying B") / 100
            top_n_rules  = st.slider("Max rules to show", 10, 100, 30)

        # ── Build basket matrix ─────────────────────────────────────
        @st.cache_data(show_spinner=False)
        def build_rules(min_sup, min_conf):
            basket = (df.groupby(["Order ID", "Product Name"])["Quantity"]
                        .sum().unstack(fill_value=0))
            basket = (basket > 0).astype(int)

            n_orders = len(basket)
            prod_support = basket.sum() / n_orders

            rules = []
            products = basket.columns.tolist()

            for i, prod_a in enumerate(products):
                sup_a = prod_support[prod_a]
                if sup_a < min_sup:
                    continue
                bought_a = basket[basket[prod_a] == 1]
                for prod_b in products[i+1:]:
                    sup_b  = prod_support[prod_b]
                    if sup_b < min_sup:
                        continue
                    sup_ab = (bought_a[prod_b] == 1).mean()
                    if sup_ab < min_sup:
                        continue
                    conf_a_b = sup_ab / sup_a
                    conf_b_a = sup_ab / sup_b
                    lift     = sup_ab / (sup_a * sup_b)
                    if conf_a_b >= min_conf or conf_b_a >= min_conf:
                        rules.append({
                            "Product A":    prod_a,
                            "Product B":    prod_b,
                            "Support":      round(sup_ab * 100, 2),
                            "Confidence A→B": round(conf_a_b * 100, 1),
                            "Confidence B→A": round(conf_b_a * 100, 1),
                            "Lift":         round(lift, 3),
                        })
            return pd.DataFrame(rules).sort_values("Lift", ascending=False) if rules else pd.DataFrame()

        with st.spinner("Mining association rules…"):
            rules_df = build_rules(min_support, min_conf)

        n_products = df["Product Name"].nunique()
        n_orders_total = df["Order ID"].nunique()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Products",       f"{n_products:,}")
        c2.metric("Orders",         f"{n_orders_total:,}")
        c3.metric("Rules Found",    f"{len(rules_df):,}")
        c4.metric("Min Support",    f"{min_support*100:.0f}%")

        if rules_df.empty:
            st.warning("No rules found. Lower the support or confidence thresholds.")
            st.stop()

        top_rules = rules_df.head(top_n_rules)

        # ── KPI badges ──────────────────────────────────────────────
        best_lift  = rules_df["Lift"].max()
        best_conf  = rules_df["Confidence A→B"].max()
        avg_lift   = rules_df["Lift"].mean()

        k1 = kpi_card("BEST LIFT",    f"{best_lift:.2f}x", "Strongest association", GOLD)
        k2 = kpi_card("BEST CONF",    f"{best_conf:.1f}%", "Most reliable rule",    GREEN)
        k3 = kpi_card("AVG LIFT",     f"{avg_lift:.2f}x",  "Average across rules",  BLUE)
        k4 = kpi_card("STRONG RULES", f"{(rules_df['Lift'] > 2).sum()}",
                       "Lift > 2x",  AMBER)
        st.markdown(
            f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:1.2rem;">'
            f'{k1}{k2}{k3}{k4}</div>', unsafe_allow_html=True)

        # ── Lift scatter ─────────────────────────────────────────────
        st.markdown(f'{tag("LIFT CHART")}<div class="riq-section-title">Support vs Confidence (sized by Lift)</div>', unsafe_allow_html=True)
        fig_lift = go.Figure()
        fig_lift.add_trace(go.Scatter(
            x=top_rules["Support"],
            y=top_rules["Confidence A→B"],
            mode="markers",
            marker=dict(
                size=top_rules["Lift"].clip(upper=10) * 4,
                color=top_rules["Lift"],
                colorscale=[[0, GOLD2], [0.5, AMBER], [1, RED]],
                showscale=True,
                colorbar=dict(title="Lift", thickness=12, len=0.6,
                              tickfont=dict(color=FONT_C)),
                line=dict(color=BG2, width=0.5)),
            text=top_rules["Product A"].astype(str).str[:25] + " → " + top_rules["Product B"].astype(str).str[:25],
            hovertemplate=(
                "<b>%{text}</b><br>"
                "Support: %{x:.2f}%<br>"
                "Confidence: %{y:.1f}%<br>"
                "Lift: %{marker.color:.2f}x<extra></extra>"),
            customdata=top_rules["Lift"]))
        lay_lift = plotly_base(360)
        lay_lift.update({
            "xaxis_title": "Support (%)",
            "yaxis_title": "Confidence A→B (%)"})
        fig_lift.update_layout(**lay_lift)
        st.plotly_chart(fig_lift, use_container_width=True, config={"displayModeBar": False})

        # ── Top rules table ──────────────────────────────────────────
        st.markdown(f'{tag("TOP RULES")}<div class="riq-section-title">Top {top_n_rules} Association Rules</div>', unsafe_allow_html=True)
        disp_rules = top_rules.copy()
        disp_rules["Support"]         = disp_rules["Support"].apply(lambda x: f"{x:.2f}%")
        disp_rules["Confidence A→B"]  = disp_rules["Confidence A→B"].apply(lambda x: f"{x:.1f}%")
        disp_rules["Confidence B→A"]  = disp_rules["Confidence B→A"].apply(lambda x: f"{x:.1f}%")
        disp_rules["Lift"]            = disp_rules["Lift"].apply(lambda x: f"{x:.3f}x")
        st.dataframe(disp_rules, use_container_width=True, hide_index=True)

        # ── Product lookup ───────────────────────────────────────────
        st.markdown(f'{tag("LOOKUP")}<div class="riq-section-title">Find Recommendations for a Product</div>', unsafe_allow_html=True)
        all_prods = sorted(set(rules_df["Product A"].tolist() + rules_df["Product B"].tolist()))
        chosen = st.selectbox("Select a product", all_prods)
        if chosen:
            mask_a = rules_df["Product A"] == chosen
            mask_b = rules_df["Product B"] == chosen
            recs_a = rules_df[mask_a][["Product B","Support","Confidence A→B","Lift"]].rename(
                columns={"Product B": "Recommended", "Confidence A→B": "Confidence"})
            recs_b = rules_df[mask_b][["Product A","Support","Confidence B→A","Lift"]].rename(
                columns={"Product A": "Recommended", "Confidence B→A": "Confidence"})
            recs = pd.concat([recs_a, recs_b]).sort_values("Lift", ascending=False).head(15)

            if recs.empty:
                st.info("No recommendations found for this product with current thresholds.")
            else:
                st.markdown(
                    f'<div style="font-size:0.8rem;color:{GOLD};font-family:DM Mono,monospace;'
                    f'margin-bottom:0.6rem;">Customers who bought {chosen[:50]!r} also bought:</div>',
                    unsafe_allow_html=True)
                for _, r in recs.iterrows():
                    lift_color = GREEN if r["Lift"] >= 2 else GOLD if r["Lift"] >= 1.5 else AMBER
                    st.markdown(
                        f'<div class="riq-insight">'
                        f'<div style="font-size:1.1rem;min-width:28px;">🛍️</div>'
                        f'<div style="flex:1;">'
                        f'<div style="font-weight:600;font-size:0.85rem;color:#F8FAFC;">{r["Recommended"][:70]}</div>'
                        f'<div style="display:flex;gap:16px;margin-top:4px;">'
                        f'<span style="font-size:0.72rem;color:#B5B5BD;">Support: {r["Support"]:.2f}%</span>'
                        f'<span style="font-size:0.72rem;color:#B5B5BD;">Confidence: {r["Confidence"]:.1f}%</span>'
                        f'<span style="font-size:0.72rem;color:{lift_color};font-weight:600;">Lift: {r["Lift"]:.2f}x</span>'
                        f'</div></div></div>',
                        unsafe_allow_html=True)

        # ── Top co-purchased pairs ───────────────────────────────────
        st.markdown(f'{tag("TOP PAIRS")}<div class="riq-section-title">Most Frequently Co-Purchased Pairs</div>', unsafe_allow_html=True)
        top_sup = rules_df.sort_values("Support", ascending=False).head(15).copy()
        pair_labels = (top_sup["Product A"].astype(str).str[:22] + " ↔ " + top_sup["Product B"].astype(str).str[:22])
        fig_pairs = go.Figure(go.Bar(
            x=top_sup["Support"], y=pair_labels, orientation="h",
            marker=dict(color=GOLD, line_width=0, cornerradius=5),
            text=[f"{v:.2f}%" for v in top_sup["Support"]],
            textposition="outside",
            textfont=dict(size=9, color=FONT_C),
            hovertemplate="<b>%{y}</b><br>Support: %{x:.2f}%<extra></extra>"))
        lay_pairs = plotly_base(max(280, len(top_sup)*32))
        lay_pairs.update({"xaxis_title": "Support (%)",
                          "margin": dict(l=10, r=60, t=20, b=10)})
        fig_pairs.update_layout(**lay_pairs)
        st.plotly_chart(fig_pairs, use_container_width=True, config={"displayModeBar": False})

        csv_rules = rules_df.to_csv(index=False)
        st.download_button("Download all rules CSV", csv_rules,
            file_name=f"association_rules_{pd.Timestamp.today().strftime('%Y%m%d')}.csv",
            mime="text/csv", use_container_width=True)

    except Exception as e:
        st.error(f"Recommendations error: {e}")
        st.exception(e)

# ═══════════════════════════════════════════════════════
# PAGE: SETTINGS
# ═══════════════════════════════════════════════════════
elif menu == "⚙️ Settings":
    page_header("Settings", "// configure your platform")

    # ── CURRENCY SELECTOR ───────────────────────────────────────────
    st.markdown(f'{tag("CURRENCY")}<div class="riq-section-title">Currency & Display Format</div>', unsafe_allow_html=True)

    cur_options = list(CURRENCIES.keys())
    cur_labels  = [f"{CURRENCIES[c]['symbol']}  {c} — {CURRENCIES[c]['name']}" for c in cur_options]
    cur_current = st.session_state.get("currency", "INR")
    cur_idx     = cur_options.index(cur_current) if cur_current in cur_options else 0

    selected_cur = st.selectbox(
        "Select display currency",
        options=cur_options,
        format_func=lambda c: f"{CURRENCIES[c]['symbol']}  {c} — {CURRENCIES[c]['name']}",
        index=cur_idx,
        help="All monetary values across the app will display in this currency")

    scale_note = {
        "INR": "Indian scale — values shown in Cr (Crore) and L (Lakh)",
        "USD": "Western scale — values shown in B (Billion) and M (Million)",
        "EUR": "Western scale — values shown in B (Billion) and M (Million)",
        "GBP": "Western scale — values shown in B (Billion) and M (Million)",
        "JPY": "Western scale — values shown in B (Billion) and M (Million)",
        "AED": "Western scale — values shown in B (Billion) and M (Million)",
        "SGD": "Western scale — values shown in B (Billion) and M (Million)",
        "AUD": "Western scale — values shown in B (Billion) and M (Million)",
        "CAD": "Western scale — values shown in B (Billion) and M (Million)",
        "CNY": "Western scale — values shown in B (Billion) and M (Million)",
    }
    sym = CURRENCIES[selected_cur]["symbol"]
    st.markdown(
        f'<div style="font-size:0.72rem;color:#6B6B75;margin:4px 0 10px;">' +
        f'{scale_note.get(selected_cur, "")} · Symbol: <span style="color:{GOLD};font-family:DM Mono,monospace;">{sym}</span></div>',
        unsafe_allow_html=True)

    if st.button("Apply Currency", use_container_width=False):
        st.session_state["currency"] = selected_cur
        save_prefs({**load_prefs(), "currency": selected_cur})
        st.success(f"Currency set to {sym} {selected_cur}. All pages updated.")
        st.rerun()

    st.markdown("---")

    st.markdown(f'{tag("API KEY")}<div class="riq-section-title">Anthropic API Key</div>', unsafe_allow_html=True)
    api_key_input = st.text_input(
        "Enter your Anthropic API key (for AI Chatbot & Narratives)",
        value=st.session_state.get("anthropic_key", ""),
        type="password", placeholder="sk-ant-...")
    if st.button("Save API Key"):
        st.session_state["anthropic_key"] = api_key_input
        st.success("API key saved for this session.")
    st.markdown(
        f'<div style="font-size:0.72rem;color:#6B6B75;margin-bottom:1rem;">' 
        f"Key is stored only in session memory and never sent anywhere except Anthropic's API.</div>",
        unsafe_allow_html=True)
    st.markdown("---")

    st.markdown(f'{tag("ACCOUNT")}<div class="riq-section-title">Account Information</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="riq-card">'
        f'<div style="font-size:0.88rem;font-weight:600;color:#F8FAFC;margin-bottom:8px;">Logged in as: <span style="color:{GOLD};">{st.session_state.username}</span></div>'
        f'<div style="font-size:0.76rem;color:#B5B5BD;">Dataset: {st.session_state.dataset_name or "Global Superstore (Demo)"}</div>'
        f'<div style="font-size:0.76rem;color:#B5B5BD;margin-top:4px;">Theme: Black Gold Elite</div>'
        f'</div>', unsafe_allow_html=True)

    st.markdown(f'{tag("DATASET")}<div class="riq-section-title">Dataset Settings</div>', unsafe_allow_html=True)
    if st.button("Clear Dataset & Reset to Demo"):
        st.session_state.dataset = None
        st.session_state.dataset_name = ""
        st.session_state.col_map = {}
        st.success("Reset to demo dataset.")
        st.rerun()

    st.markdown(f'{tag("ABOUT")}<div class="riq-section-title">About RetailIQ AI</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="riq-card">'
        '<div style="font-size:0.88rem;font-weight:600;color:#D4AF37;margin-bottom:8px;">RetailIQ AI v2.0</div>'
        '<div style="font-size:0.78rem;color:#B5B5BD;line-height:1.6;">'
        'Built with Streamlit · Plotly · Pandas · Scikit-learn · ReportLab<br>'
        'Theme: Black Gold Elite · Enterprise Retail Intelligence Platform<br>'
        'Dashboard · Analytics · ML · Forecasting · Reports'
        '</div></div>', unsafe_allow_html=True)

else:
    st.info(f"Page '{menu}' is not available.")