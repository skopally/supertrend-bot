@app.route("/callback")
def callback():
    request_token = request.args.get("request_token")
    status = request.args.get("status")

    if status != "success":
        return f"Login cancelled or failed: {status}", 400

    if not request_token:
        return "Missing request_token", 400

    try:
        session = kite.generate_session(
            request_token,
            api_secret=API_SECRET
        )
        state["access_token"] = session["access_token"]
        kite.set_access_token(state["access_token"])

        # Send email in background after responding
        # to avoid timeout
        import threading
        def send_login_email():
            try:
                send_email(
                    "BOT LOGIN SUCCESS",
                    "Supertrend NatGas bot is now live and ready to trade."
                )
            except Exception:
                pass
        threading.Thread(target=send_login_email).start()

        return """
        <html><body>
        <h2 style='color:green'>Login Successful!</h2>
        <p>Supertrend bot is now active.</p>
        <p>You can close this tab.</p>
        </body></html>
        """, 200

    except Exception as e:
        logging.error(f"Callback error: {e}")
        return f"Login failed: {str(e)}", 500
        import threading
import time

def keep_alive():
    while True:
        time.sleep(840)  # ping every 14 minutes
        try:
            import urllib.request
            urllib.request.urlopen(
                "https://supertrend-bot-av8n.onrender.com/"
            )
            logging.info("Keep-alive ping sent")
        except Exception as e:
            logging.error(f"Keep-alive error: {e}")

threading.Thread(target=keep_alive, daemon=True).start()
