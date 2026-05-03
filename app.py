import os
import pytz
import logging
import smtplib
from email.mime.text import MIMEText
from datetime import datetime
from flask import Flask, request, jsonify, redirect
from kiteconnect import KiteConnect

API_KEY       = os.environ.get("KITE_API_KEY")
API_SECRET    = os.environ.get("KITE_API_SECRET")
WEBHOOK_SECRET= os.environ.get("WEBHOOK_SECRET", "natgas2026")
GMAIL_USER    = os.environ.get("GMAIL_USER")
GMAIL_PASS    = os.environ.get("GMAIL_PASS")
NOTIFY_EMAIL  = os.environ.get("NOTIFY_EMAIL", "skopally@gmail.com")

SYMBOL     = "NATURALGAS26MAYFUT"
EXCHANGE   = "MCX"
PRODUCT    = "NRML"
LOTS       = 1
LOT_SIZE   = 1250
SL_PTS     = 8.0
TP_PTS     = SL_PTS * 3
MAX_LOSSES = 5

state = {
    "access_token"  : None,
    "loss_count"    : 0,
    "current_month" : datetime.now().month,
    "position"      : None
}

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
kite = KiteConnect(api_key=API_KEY)


def send_email(subject, body):
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = NOTIFY_EMAIL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASS)
            server.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())
        logging.info(f"Email sent: {subject}")
    except Exception as e:
        logging.error(f"Email error: {e}")


def notify(subject, body):
    send_email(subject, body)
    logging.info(f"NOTIFY | {subject} | {body}")


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
        notify("BOT ERROR", "Not logged in. Visit /login to authenticate.")
        return False

    kite.set_access_token(state["access_token"])
    ltp = get_ltp()
    if not ltp:
        notify("BOT ERROR", "Could not fetch LTP from MCX.")
        return False

    qty          = LOTS * LOT_SIZE
    txn_type     = kite.TRANSACTION_TYPE_SELL if direction == "short" else kite.TRANSACTION_TYPE_BUY
    sl_price     = round(ltp + SL_PTS, 1) if direction == "short" else round(ltp - SL_PTS, 1)
    target_price = round(ltp - TP_PTS, 1) if direction == "short" else round(ltp + TP_PTS, 1)

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

        body = (
            f"Direction  : {direction.upper()}\n"
            f"Symbol     : {SYMBOL}\n"
            f"Qty        : {qty} units ({LOTS} lot)\n"
            f"LTP        : Rs.{ltp}\n"
            f"Stop Loss  : Rs.{sl_price} ({SL_PTS} pts)\n"
            f"Target     : Rs.{target_price} ({TP_PTS} pts)\n"
            f"Order ID   : {order_id}\n"
            f"Losses     : {state['loss_count']} / {MAX_LOSSES}"
        )
        notify(f"ORDER PLACED — {direction.upper()} {SYMBOL}", body)
        state["position"] = direction
        return True

    except Exception as e:
        notify("ORDER FAILED", str(e))
        logging.error(f"Order error: {e}")
        return False


def close_position():
    if not state["position"]:
        return
    kite.set_access_token(state["access_token"])
    ltp      = get_ltp()
    txn_type = kite.TRANSACTION_TYPE_BUY if state["position"] == "short" else kite.TRANSACTION_TYPE_SELL
    qty      = LOTS * LOT_SIZE

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
        notify(
            f"POSITION CLOSED — {state['position'].upper()}",
            f"LTP      : Rs.{ltp}\nOrder ID : {order_id}"
        )
        state["position"] = None
    except Exception as e:
        notify("CLOSE FAILED", str(e))


@app.route("/")
def home():
    return jsonify({
        "status"    : "LOCKED" if is_locked() else "ACTIVE",
        "losses"    : state["loss_count"],
        "max_losses": MAX_LOSSES,
        "position"  : state["position"],
        "logged_in" : state["access_token"] is not None
    })


@app.route("/login")
def login():
    return redirect(kite.login_url())


@app.route("/callback")
def callback():
    request_token = request.args.get("request_token")
    if not request_token:
        return "Missing request_token", 400
    try:
        session = kite.generate_session(request_token, api_secret=API_SECRET)
        state["access_token"] = session["access_token"]
        notify(
            "BOT LOGIN SUCCESS",
            "Supertrend NatGas bot is now live and ready to trade."
        )
        return "Login successful. Bot is now active. You can close this tab.", 200
    except Exception as e:
        return f"Login failed: {e}", 500


@app.route("/webhook", methods=["POST"])
def webhook():
    secret = request.args.get("secret")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorised"}), 401

    data   = request.get_json(force=True)
    signal = str(data.get("signal", "")).lower()
    logging.info(f"Webhook received: {data}")

    if is_locked():
        notify(
            "SIGNAL BLOCKED — MONTH LOCKED",
            f"Signal {signal.upper()} received but {state['loss_count']}/{MAX_LOSSES} losses reached."
        )
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
    data     = request.get_json(force=True)
    status   = data.get("status", "")
    order_id = data.get("order_id", "")
    logging.info(f"Postback: {data}")

    if status == "REJECTED":
        notify(
            "ORDER REJECTED",
            f"Order ID : {order_id}\nReason   : {data.get('status_message', 'Unknown')}"
        )

    if status == "COMPLETE":
        avg_price = data.get("average_price", 0)
        txn       = data.get("transaction_type", "")
        pnl       = data.get("pnl", 0)
        notify(
            f"ORDER FILLED — {txn}",
            f"Avg price : Rs.{avg_price}\nOrder ID  : {order_id}"
        )
        try:
            if float(str(pnl).replace(",", "")) < 0:
                state["loss_count"] += 1
                notify(
                    f"LOSS RECORDED — {state['loss_count']}/{MAX_LOSSES}",
                    f"Monthly loss count is now {state['loss_count']} of {MAX_LOSSES}."
                )
                if state["loss_count"] >= MAX_LOSSES:
                    notify(
                        "MONTH LOCKED — TRADING STOPPED",
                        "5 losses reached. No more trades will be placed this month."
                    )
        except Exception:
            pass

    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
