import json
import os
import matplotlib.pyplot as plt
import pandas as pd
import requests

try:
  import yfinance as yf
except ImportError:
  import yfinance as yf

# Load secrets securely from environment variables (injected via GitHub Secrets)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOLS = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDJPY": "AUDJPY=X",
    "JP225": "^N225",
}

STATE_FILE = "state.json"


def load_state():
  if os.path.exists(STATE_FILE):
    try:
      with open(STATE_FILE, "r") as f:
        return json.load(f)
    except Exception:
      return {}
  return {}


def save_state(state):
  with open(STATE_FILE, "w") as f:
    json.dump(state, f, indent=4)


def send_telegram_photo(caption, image_path):
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
  try:
    with open(image_path, "rb") as photo:
      payload = {
          "chat_id": TELEGRAM_CHAT_ID,
          "caption": caption,
          "parse_mode": "Markdown",
      }
      files = {"photo": photo}
      response = requests.post(url, data=payload, files=files, timeout=15)
      if response.status_code == 200:
        print("Telegram chart alert sent successfully.")
      else:
        print(f"Failed to send photo alert: {response.text}")
  except Exception as e:
    print(f"Telegram connection error: {e}")


def fetch_candles(ticker, interval, period="1mo"):
  try:
    data = yf.download(
        ticker, interval=interval, period=period, progress=False
    )
    if data.empty:
      return None
    if isinstance(data.columns, pd.MultiIndex):
      data.columns = data.columns.droplevel(1)
    df = pd.DataFrame()
    df["open"] = data["Open"]
    df["high"] = data["High"]
    df["low"] = data["Low"]
    df["close"] = data["Close"]
    df = df.dropna()
    return df
  except Exception as e:
    print(f"Error fetching {ticker}: {e}")
    return None


def generate_sweep_chart(df_1h, asset_name, bias, r_level):
  df_plot = df_1h.tail(15).reset_index(drop=True)

  fig, ax = plt.subplots(figsize=(10, 6))
  bg_color = "#b2b5be"
  ax.set_facecolor(bg_color)
  fig.patch.set_facecolor(bg_color)

  for i, row in df_plot.iterrows():
    is_bullish = row["close"] >= row["open"]
    face_color = "white" if is_bullish else "#363a45"
    edge_color = "#363a45"

    ax.vlines(
        i, row["low"], row["high"], color=edge_color, linewidth=1.2, zorder=1
    )
    body_bottom = min(row["open"], row["close"])
    body_height = max(abs(row["close"] - row["open"]), 0.00001)
    ax.bar(
        i,
        body_height,
        bottom=body_bottom,
        color=face_color,
        edgecolor=edge_color,
        linewidth=1.2,
        width=0.6,
        zorder=2,
    )

  ax.axhline(
      y=r_level,
      color="#1e222d",
      linestyle="--",
      linewidth=1.2,
      label=f"Ref Level: {r_level}",
  )
  ax.set_title(
      f"{asset_name} - 1H Sweep ({bias})",
      color="#1e222d",
      fontsize=14,
      fontweight="bold",
  )
  ax.tick_params(colors="#1e222d")
  for spine in ax.spines.values():
    spine.set_color("#1e222d")

  ax.grid(color="#9598a1", linestyle=":", linewidth=0.5, alpha=0.5)
  ax.legend(loc="upper left", facecolor="#ffffff", labelcolor="#1e222d")
  plt.tight_layout()

  chart_path = "sweep_chart.png"
  plt.savefig(chart_path, dpi=150, facecolor=fig.get_facecolor())
  plt.close()
  return chart_path


def identify_htf_key_levels_and_rejection(df_daily, df_weekly, current_bias):
  if df_daily is None or df_weekly is None or len(df_daily) < 30:
    return current_bias, "No state change (insufficient data)"

  w_high = df_weekly["high"].iloc[-5:-1].max()
  w_low = df_weekly["low"].iloc[-5:-1].min()

  d_open = df_daily["open"].iloc[-1]
  d_high = df_daily["high"].iloc[-10:-1].max()
  d_low = df_daily["low"].iloc[-10:-1].min()

  curr_price = df_daily["close"].iloc[-1]
  tolerance = 0.0025

  tapped_demand = (
      abs(curr_price - d_low) / d_low <= tolerance
      or abs(curr_price - w_low) / w_low <= tolerance
      or abs(curr_price - d_open) / d_open <= tolerance
  )
  tapped_supply = (
      abs(curr_price - d_high) / d_high <= tolerance
      or abs(curr_price - w_high) / w_high <= tolerance
      or abs(curr_price - d_open) / d_open <= tolerance
  )

  if tapped_demand and not tapped_supply:
    return "BULLISH", "Tapped HTF Demand / Bullish KL Rejection"
  elif tapped_supply and not tapped_demand:
    return "BEARISH", "Tapped HTF Supply / Bearish KL Rejection"
  else:
    if current_bias in ["BULLISH", "BEARISH"]:
      return (
          current_bias,
          "Floating in space (retaining persistent HTF KL direction)",
      )
    return "BULLISH", "Default Initial Bias"


def check_1h_crt_sweep(df_1h, bias):
  if df_1h is None or len(df_1h) < 3:
    return False, "", 0

  ref_candle = df_1h.iloc[-3]
  sweep_candle = df_1h.iloc[-2]

  r_high, r_low = ref_candle["high"], ref_candle["low"]
  s_high, s_low, s_close = (
      sweep_candle["high"],
      sweep_candle["low"],
      sweep_candle["close"],
  )

  if bias == "BULLISH":
    if s_low < r_low and s_close > r_low:
      return (
          True,
          f"Swept 1H Low ({r_low}) & Closed Inside ({s_close})",
          r_low,
      )
  elif bias == "BEARISH":
    if s_high > r_high and s_close < r_high:
      return (
          True,
          f"Swept 1H High ({r_high}) & Closed Inside ({r_high})",
          r_high,
      )

  return False, "", 0


def run_scanner():
  state = load_state()
  updated = False

  print("Executing 1H Sweep Scan...")

  for name, ticker in SYMBOLS.items():
    df_weekly = fetch_candles(ticker, interval="1wk", period="3mo")
    df_daily = fetch_candles(ticker, interval="1d", period="1mo")
    df_1h = fetch_candles(ticker, interval="60m", period="5d")

    current_bias = state.get(name, "BULLISH")
    new_bias, bias_reason = identify_htf_key_levels_and_rejection(
        df_daily, df_weekly, current_bias
    )

    if new_bias != current_bias:
      state[name] = new_bias
      updated = True
    else:
      state[name] = current_bias

    has_crt, details, ref_level = check_1h_crt_sweep(df_1h, state[name])

    if has_crt:
      icon = "🟢" if state[name] == "BULLISH" else "🔴"
      msg = (
          f"{icon} *1H SWEEP ALERT*\n\n"
          f"*Asset:* `{name}`\n"
          f"*Persistent Bias:* `{state[name]}`\n"
          f"*HTF Context:* {bias_reason}\n"
          f"*Trigger:* {details}"
      )
      print(f"ALERT TRIGGERED: {name}")
      chart_img = generate_sweep_chart(df_1h, name, state[name], ref_level)
      send_telegram_photo(msg, chart_img)
    else:
      print(f"[{name}] Checked: No sweep matching {state[name]} bias.")

  if updated or not os.path.exists(STATE_FILE):
    save_state(state)


if __name__ == "__main__":
  run_scanner()
