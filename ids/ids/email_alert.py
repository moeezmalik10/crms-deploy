import os
import smtplib
from email.mime.text import MIMEText

SENDER_EMAIL = os.environ.get("SMTP_EMAIL", "")
APP_PASSWORD = os.environ.get("SMTP_APP_PASSWORD", "")

ADMIN_EMAIL = os.environ.get("ALERT_EMAIL", SENDER_EMAIL)

def send_email_alert(email, attack_type, ip_address, country, city):
    if not (SENDER_EMAIL and APP_PASSWORD):
        return  # email alerts are optional (set SMTP_EMAIL and SMTP_APP_PASSWORD on Render)
    try:
        subject = f"Security Alert - {attack_type}"

        body = f"""
Attack Type: {attack_type}
Email: {email}
IP Address: {ip_address}
Country: {country}
City: {city}
"""

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = SENDER_EMAIL
        msg["To"] = ADMIN_EMAIL

        server = smtplib.SMTP("smtp.gmail.com", 587, timeout=5)
        server.starttls()
        server.login(SENDER_EMAIL, APP_PASSWORD)
        server.sendmail(SENDER_EMAIL, ADMIN_EMAIL, msg.as_string())
        server.quit()

        print("Email alert sent successfully")

    except Exception as e:
        print("SMTP Email Failed:", str(e))