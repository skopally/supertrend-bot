import os
import json
import pytz
import logging
from datetime import datetime
from flask import Flask, request, jsonify, redirect
from kiteconnect import KiteConnect
import requests

# ── CONFIG — set these as Environment Variables in Render ──────
API_KEY      = os.environ.get("KITE_API_KEY")
API_SECRET   = os.environ.get("KITE_API_SECRET")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "mysecret123")

# MCX Natural Gas contract details
SYMBOL       = "NATURALGAS26MAYFUT"   # update monthly
EXCHANGE     = "MCX"
PRODUCT      = "NRML"                 # Normal for overnight MCX
LOTS         = 1                      # set to 5 when ready
LOT_SIZE     = 1250
SL_PTS       = 8.0                    # 8 pts x 1250 x 1 lot = Rs.10,000
TP_PTS       = SL_PTS * 3            # 24 pts = Rs.30,000

# Monthly loss lock
MAX_LOSSES   = 5

# ── STATE (in-memory, resets on server restart) ────────────────
state = {
    "access_token": None,
    "loss_count": 0,
    "current_month": datetime.now().month,
    "position": None   # "long" / "short" / None
}

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
kite = KiteConnect(api_key=API_KEY)


# ── HELPERS ────────────────────────────────────────────────────

def send_telegram(msg):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT, "text": msg})
    logging.info(f"TELEGRAM: {msg}")


def check_month_reset():
    now_month = datetime.now(pytz.timezone("Asia/Kolkata")).month
    if now_month != state["current_month"]:
        state["loss_count"]    = 0
        state["current_month"] = now_month
        logging.info("Monthly loss counter reset")


def is_locked():
    check_month_reset()
    return state["loss_count"] >= MAX_LOSSES


def get_ltp():
    try:
        data = kite.ltp([f"{EXCHANGE}:{SYMBOL}"])
        return data[f"{EXCHANGE}:{SYMBOL}"]["last_price"]
    except Exception as e:
        logging.error(f"LTP error: {e}")
        return None


def place_order(direction):
    if not state["access_token"]:
        send_telegram("ERROR: Not logged in. Visit /login to authenticate.")
        return False

    kite.set_access_token(state["access_token"])
    ltp = get_ltp()
    if not ltp:
        send_telegram("ERROR: Could not fetch LTP")
        return False

    qty           = LOTS * LOT_SIZE
    txn_type      = kite.TRANSACTION_TYPE_SELL if direction == "short" else kite.TRANSACTION_TYPE_BUY
    sl_price      = round(ltp + SL_PTS, 1) if direction == "short" else round(ltp - SL_PTS, 1)
    target_price  = round(ltp - TP_PTS, 1) if direction == "short" else round(ltp + TP_PTS, 1)

    try:
        order_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = EXCHANGE,
            tradingsymbol    = SYMBOL,
            transaction_type = txn_type,
            quantity         = qty,
            product          = PRODUCT,
            order_type       = kite.ORDER_TYPE_MARKET,
            validity         = kite.VALIDITY_DAY
        )

        msg = (
            f"ORDER PLACED\n"
            f"Direction : {direction.upper()}\n"
            f"Symbol    : {SYMBOL}\n"
            f"Qty       : {qty} units ({LOTS} lot)\n"
            f"LTP       : Rs.{ltp}\n"
            f"SL        : Rs.{sl_price} ({SL_PTS} pts)\n"
            f"TP        : Rs.{target_price} ({TP_PTS} pts)\n"
            f"Order ID  : {order_id}\n"
            f"Losses    : {state['loss_count']} / {MAX_LOSSES}"
        )
        send_telegram(msg)
        state["position"] = direction
        return True

    except Exception as e:
        send_telegram(f"ORDER FAILED: {e}")
        logging.error(f"Order error: {e}")
        return False


def close_position():
    if not state["position"]:
        return
    kite.set_access_token(state["access_token"])
    ltp = get_ltp()
    txn_type = kite.TRANSACTION_TYPE_BUY if state["position"] == "short" else kite.TRANSACTION_TYPE_SELL
    qty = LOTS * LOT_SIZE

    try:
        order_id = kite.place_order(
            variety          = kite.VARIETY_REGULAR,
            exchange         = EXCHANGE,
            tradingsymbol    = SYMBOL,
            transaction_type = txn_type,
            quantity         = qty,
            product          = PRODUCT,
            order_type       = kite.ORDER_TYPE_MARKET,
            validity         = kite.VALIDITY_DAY
        )
        send_telegram(f"POSITION CLOSED | {state['position'].upper()} | LTP Rs.{ltp} | Order {order_id}")
        state["position"] = None
    except Exception as e:
        send_telegram(f"CLOSE FAILED: {e}")


# ── ROUTES ─────────────────────────────────────────────────────

@app.route("/")
def home():
    status = "LOCKED" if is_locked() else "ACTIVE"
    return jsonify({
        "status"      : status,
        "losses"      : state["loss_count"],
        "max_losses"  : MAX_LOSSES,
        "position"    : state["position"],
        "logged_in"   : state["access_token"] is not None
    })


@app.route("/login")
def login():
    login_url = kite.login_url()
    return redirect(login_url)


@app.route("/callback")
def callback():
    request_token = request.args.get("request_token")
    if not request_token:
        return "Missing request_token", 400
    try:
        session = kite.generate_session(request_token, api_secret=API_SECRET)
        state["access_token"] = session["access_token"]
        send_telegram("LOGIN SUCCESS — Supertrend bot is live and ready to trade.")
        return "Login successful. Bot is now active.", 200
    except Exception as e:
        return f"Login failed: {e}", 500


@app.route("/webhook", methods=["POST"])
def webhook():
    # Verify secret to prevent unauthorised calls
    secret = request.args.get("secret")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorised"}), 401

    data = request.get_json(force=True)
    logging.info(f"Webhook received: {data}")

    signal = str(data.get("signal", "")).lower()

    if is_locked():
        send_telegram(f"SIGNAL {signal.upper()} received but MONTH IS LOCKED ({state['loss_count']}/{MAX_LOSSES} losses)")
        return jsonify({"status": "locked"}), 200

    if signal in ("short", "s"):
        if state["position"] == "long":
            close_position()
        place_order("short")

    elif signal in ("long", "l"):
        if state["position"] == "short":
            close_position()
        place_order("long")

    elif signal in ("close", "exit"):
        close_position()

    else:
        return jsonify({"error": f"Unknown signal: {signal}"}), 400

    return jsonify({"status": "ok", "signal": signal}), 200


@app.route("/postback", methods=["POST"])
def postback():
    data = request.get_json(force=True)
    logging.info(f"Postback: {data}")

    status = data.get("status", "")
    order_id = data.get("order_id", "")

    if status == "REJECTED":
        send_telegram(f"ORDER REJECTED | ID: {order_id} | Reason: {data.get('status_message')}")

    if status in ("COMPLETE",):
        avg_price  = data.get("average_price", 0)
        txn        = data.get("transaction_type", "")
        pnl_approx = data.get("pnl", "")
        send_telegram(f"ORDER FILLED | {txn} | Avg: Rs.{avg_price} | ID: {order_id}")

        # Count losses via PnL if available
        if pnl_approx and float(str(pnl_approx).replace(",","")) < 0:
            state["loss_count"] += 1
            send_telegram(f"LOSS recorded. Monthly count: {state['loss_count']}/{MAX_LOSSES}")
            if state["loss_count"] >= MAX_LOSSES:
                send_telegram("MONTH LOCKED — 5 losses reached. No more trades this month.")

    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
