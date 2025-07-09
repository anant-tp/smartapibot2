from logzero import logger
from SmartApi.smartConnect import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
import pyotp
import time
from datetime import datetime, timedelta
import os
import json
import pytz
import math
from keep_alive import keep_alive
import threading

# === LOGIN DETAILS ===
api_key = os.environ.get("API_KEY")
username = os.environ.get("USERNAME")
pwd = os.environ.get("PASSWORD")
token = os.environ.get("TOTP_TOKEN")

smartApi = SmartConnect(api_key)
resume_file = "resume.json"
FEED_TOKEN = None
CLIENT_CODE = os.environ.get("USERNAME")
OPEN_PRICE = None
OPEN_PRICE_LOCKED = False

# === WebSocket for capturing 9:15:00 LTP ===

# === Get open price via WebSocket, fallback to LTP ===


def is_order_executed(order_id):
  try:
    orders = smartApi.orderBook()
    for order in orders["data"]:
      if order["orderid"] == order_id and order["status"].lower(
      ) == "complete":
        return True
    return False
  except Exception as e:
    logger.error(f"Error checking order status: {e}")
    return False


def cancel_order(order_id):
  try:
    smartApi.cancelOrder({"orderid": order_id, "variety": "NORMAL"})
    logger.info(f"❌ Entry Order Cancelled at 9:29 (ID: {order_id})")
  except Exception as e:
    logger.error(f"Error cancelling order: {e}")


def save_resume_data(data):
  with open(resume_file, "w") as f:
    json.dump(data, f)


def delete_resume_data():
  if os.path.exists(resume_file):
    os.remove(resume_file)


def load_resume_data():
  if os.path.exists(resume_file):
    with open(resume_file, "r") as f:
      return json.load(f)
  return None


def resume_trailing(data):
  tradingsymbol = data["tradingsymbol"]
  symboltoken = data["symboltoken"]
  entry_price = data["entry_price"]
  sl_orderid = data["sl_orderid"]
  IS_BUY = data["IS_BUY"]
  quantity = data["quantity"]

  logger.info("🚀 Resuming Trailing SL...")
  time.sleep(2.5)

  try:
    orders = smartApi.orderBook()
    sl_status = None
    for order in orders["data"]:
      if order["orderid"] == sl_orderid:
        sl_status = order["status"].lower()
        break

    if sl_status not in ["open", "trigger pending"]:
      logger.warning(f"SL Order already {sl_status}. Stopping trailing.")
      delete_resume_data()
      return
  except Exception as e:
    logger.error(f"Failed to verify SL order status: {e}")
    return

  current_sl_price = round(entry_price - 5.00, 2) if IS_BUY else round(
      entry_price + 5.00, 2)
  first_trigger_done = False
  next_trail_trigger = round(entry_price + 2.10, 2) if IS_BUY else round(
      entry_price - 2.10, 2)
  last_sl_price = None
  loop_counter = 0

  logger.info(f"last_sl_price: {last_sl_price}")

  while True:
    try:
      loop_counter += 1

      # Re-check SL order status every 6 loops (~30 sec)
      if last_sl_price is None or loop_counter % 6 == 0:
        logger.debug("🔍 Checking SL order status...")
        orders = smartApi.orderBook()
        for order in orders["data"]:
          if order["orderid"] == sl_orderid:
            sl_status = order["status"].lower()
            if sl_status == "complete":
              logger.info(
                  f"🎯 Trade complete ✅ — SL executed successfully. Order ID: {sl_orderid}"
              )
              delete_resume_data()
              return
            break

      # 🔄 Fetch LTP
      ltp_data = smartApi.ltpData("NSE", tradingsymbol, symboltoken)
      ltp = float(ltp_data["data"]["ltp"])

      # 📈 Trailing Logic
      if IS_BUY:
        if not first_trigger_done and ltp >= round(entry_price + 2.10, 2):
          current_sl_price = round(entry_price + 2.00, 2)
          first_trigger_done = True
          next_trail_trigger = round(current_sl_price + 1.10, 2)
        elif first_trigger_done and ltp >= next_trail_trigger:
          current_sl_price = round(current_sl_price + 1.00, 2)
          next_trail_trigger = round(current_sl_price + 1.10, 2)
      else:
        if not first_trigger_done and ltp <= round(entry_price - 2.10, 2):
          current_sl_price = round(entry_price - 2.00, 2)
          first_trigger_done = True
          next_trail_trigger = round(current_sl_price - 1.10, 2)
        elif first_trigger_done and ltp <= next_trail_trigger:
          current_sl_price = round(current_sl_price - 1.00, 2)
          next_trail_trigger = round(current_sl_price - 1.10, 2)

      # 🛠️ Modify SL if changed
      if current_sl_price != last_sl_price:
        modify_order = {
            "variety": "STOPLOSS",
            "orderid": sl_orderid,
            "tradingsymbol": tradingsymbol,
            "symboltoken": symboltoken,
            "transactiontype": "SELL" if IS_BUY else "BUY",
            "exchange": "NSE",
            "ordertype": "STOPLOSS_LIMIT",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": current_sl_price,
            "triggerprice": current_sl_price,
            "quantity": quantity,
        }
        smartApi.modifyOrder(modify_order)
        last_sl_price = current_sl_price
        logger.info(f"🔁 Trailed SL to ₹{current_sl_price}")

      time.sleep(5)

    except Exception as e:
      if "exceeding access rate" in str(e).lower():
        logger.warning("⚠️ Rate limit exceeded, sleeping 10 seconds...")
        time.sleep(10)
      else:
        logger.error(f"⛔ Error in trailing loop: {e}")
        time.sleep(3)


def get_open_price(tradingsymbol, symboltoken, hour, minute):
  now = datetime.now()
  from_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
  to_time = from_time + timedelta(minutes=1)

  try:
    logger.info("🕒 Trying to fetch 1-minute candle for 9:15...")
    candle_data = smartApi.getCandleData({
        "exchange":
        "NSE",
        "symboltoken":
        symboltoken,
        "interval":
        "ONE_MINUTE",
        "fromdate":
        from_time.strftime("%Y-%m-%d %H:%M"),
        "todate":
        to_time.strftime("%Y-%m-%d %H:%M"),
        "tradingsymbol":
        tradingsymbol,
    })

    if candle_data["status"] and candle_data["data"]:
      open_price = float(candle_data["data"][0][1])
      logger.info(f"✅ 1-min Candle Open Price: ₹{open_price}")
      return open_price
    else:
      raise Exception("Candle not available yet")

  except Exception as e:
    logger.warning(f"⚠️ Candle open not available: {e}. Using LTP instead.")
    ltp_data = smartApi.ltpData("NSE", tradingsymbol, symboltoken)
    open_price = float(ltp_data["data"]["ltp"])
    logger.info(f"⚡ Using Live LTP as Open Price: ₹{open_price}")
    return open_price


def execute_strategy(hour, minute):
  try:
    instrument = smartApi.searchScrip("NSE", "AXISBANK-EQ")
    symboltoken = instrument["data"][0]["symboltoken"]
    tradingsymbol = instrument["data"][0]["tradingsymbol"]

    ltp_data = smartApi.ltpData("NSE", tradingsymbol, symboltoken)
    open_price = get_open_price(tradingsymbol, symboltoken, hour, minute)
    logger.info(f"Live Open Price: ₹{open_price}")

    while True:
      ltp = float(
          smartApi.ltpData("NSE", tradingsymbol, symboltoken)["data"]["ltp"])
      if ltp != open_price:
        break
      logger.info(f"Waiting for breakout... LTP: ₹{ltp}")
      time.sleep(1)

    IS_BUY = ltp < open_price
    transaction_type = "BUY" if IS_BUY else "SELL"
    entry_price = round(open_price +
                        0.10, 2) if IS_BUY else round(open_price - 0.10, 2)
    logger.info(f"{transaction_type} condition met. Entry: ₹{entry_price}")

    rms_data = smartApi.rmsLimit()
    available_cash = float(rms_data["data"].get(
        "availablecash", 0)) if rms_data["status"] else 0
    quantity = math.floor((available_cash * 5) / entry_price)
    if quantity <= 0:
      logger.warning("Insufficient margin.")
      return

    ltp = float(
        smartApi.ltpData("NSE", tradingsymbol, symboltoken)["data"]["ltp"])
    diff = abs(ltp - entry_price)
    if (IS_BUY
        and ltp >= entry_price - 0.05) or (not IS_BUY
                                           and ltp <= entry_price + 0.05):
      ordertype = "MARKET"
    else:
      ordertype = "STOPLOSS_LIMIT"

    entry_orderparams = {
        "variety": "NORMAL" if ordertype == "MARKET" else "STOPLOSS",
        "tradingsymbol": tradingsymbol,
        "symboltoken": symboltoken,
        "transactiontype": transaction_type,
        "exchange": "NSE",
        "ordertype": ordertype,
        "producttype": "INTRADAY",
        "duration": "DAY",
        "quantity": quantity
    }
    if ordertype != "MARKET":
      entry_orderparams.update({
          "price": entry_price,
          "triggerprice": entry_price
      })

    entry_orderid = smartApi.placeOrder(entry_orderparams)
    logger.info(
        f"🛒 Entry Order Placed → Type: {ordertype} | ID: {entry_orderid}")

    for _ in range(780):
      if is_order_executed(entry_orderid):
        logger.info("Entry Executed.")
        break
      now = datetime.now(pytz.timezone("Asia/Kolkata"))
      if now.hour == 9 and now.minute == 29:
        if not is_order_executed(entry_orderid):
          cancel_order(entry_orderid)
          logger.warning(
              "❌ Entry not executed till 9:29, cancelling & waiting for 9:30..."
          )
          wait_until_time(9, 30)
          return
      logger.info("⏳ Waiting for entry execution...")
      time.sleep(5)
    else:
      logger.warning("Entry not executed.")
      return

    initial_sl = round(entry_price -
                       5.00, 2) if IS_BUY else round(entry_price + 5.00, 2)

    sl_orderparams = {
        "variety": "STOPLOSS",
        "tradingsymbol": tradingsymbol,
        "symboltoken": symboltoken,
        "transactiontype": "SELL" if IS_BUY else "BUY",
        "exchange": "NSE",
        "ordertype": "STOPLOSS_LIMIT",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": initial_sl,
        "triggerprice": initial_sl,
        "quantity": quantity
    }
    sl_orderid = smartApi.placeOrder(sl_orderparams)
    logger.info(f"Initial SL placed at ₹{initial_sl} (ID: {sl_orderid})")

    save_resume_data({
        "tradingsymbol": tradingsymbol,
        "symboltoken": symboltoken,
        "entry_price": entry_price,
        "sl_orderid": sl_orderid,
        "IS_BUY": IS_BUY,
        "quantity": quantity
    })

    resume_trailing({
        "tradingsymbol": tradingsymbol,
        "symboltoken": symboltoken,
        "entry_price": entry_price,
        "sl_orderid": sl_orderid,
        "IS_BUY": IS_BUY,
        "quantity": quantity
    })

  except Exception as e:
    logger.error(f"Strategy Error: {e}")


def wait_until_time(hour, minute):
  logger.info(f"Waiting for {hour:02d}:{minute:02d}...")
  while True:
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz)
    logger.debug(f"Checking time: {now.strftime('%H:%M:%S')}")
    if now.hour == hour and now.minute == minute:
      logger.info("Time matched. Running strategy.")
      execute_strategy(hour, minute)
      break
    time.sleep(1)


if __name__ == "__main__":
  try:
    keep_alive()
    logger.info("🌐 Keep-alive server started on port 5000")

    totp = pyotp.TOTP(token).now()
    data = smartApi.generateSession(username, pwd, totp)
    if not data["status"]:
      logger.error("Login failed.")
      exit()

    FEED_TOKEN = smartApi.getfeedToken()
    smartApi.generateToken(data["data"]["refreshToken"])
    smartApi.getProfile(data["data"]["refreshToken"])

    resume = load_resume_data()
    if resume:
      logger.info("Resuming from saved state...")
      resume_trailing(resume)
    else:
      wait_until_time(9, 15)

  except Exception as e:
    logger.error(f"Startup Error: {e}")
