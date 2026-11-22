"""Sending account emails (verify your address, reset your password) over SMTP.

Works with any provider that offers SMTP: Resend, Postmark, Brevo, Amazon SES, Google Workspace and so on. Set
PEAKCUT_SMTP_HOST, PEAKCUT_SMTP_PORT (587), PEAKCUT_SMTP_USER, PEAKCUT_SMTP_PASSWORD_FILE (a file holding the
password or API key) and PEAKCUT_MAIL_FROM ("PeakCut <hello@yourdomain.com>"). Without them nothing is sent and the
link is written to the server log instead, which is enough for running PeakCut on your own machine."""
import html
import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parseaddr

from . import config

log = logging.getLogger("peakcut.mail")


def ready() -> bool:
    return bool(config.SMTP_HOST and config.MAIL_FROM)


LOGO = config.ROOT / "web" / "brand" / "logo-email.png"   # PNG: email apps don't show SVG
LOGO_CID = "peakcut-logo"


def _html(title: str, intro: str, button: str, link: str, outro: str, logo_src: str = "cid:" + LOGO_CID) -> str:
    """The branded email: JaxCliped logo, clean Apple glass card with the message, Apple blue button, and footer."""
    e = html.escape
    site = config.PUBLIC_URL
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only"><title>{e(title)}</title></head>
<body style="margin:0;padding:0;background:#F5F5F7;-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#F5F5F7">{e(intro)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F5F5F7"><tr><td align="center" style="padding:40px 16px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px">
    <tr><td style="padding:0 6px 20px;text-align:center"><a href="{e(site)}" style="text-decoration:none;font-family:{font};font-size:26px;font-weight:800;color:#1D1D1F;letter-spacing:-0.5px">
      Jax<span style="color:#0071E3">Cliped</span></a></td></tr>
    <tr><td style="background:#ffffff;border-radius:24px;border:1px solid #E5E5EA;padding:40px 36px;box-shadow:0 8px 30px rgba(0,0,0,0.04)">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        <tr><td style="font-family:{font};font-size:24px;line-height:1.25;font-weight:700;color:#1D1D1F;padding-bottom:12px">{e(title)}</td></tr>
        <tr><td style="font-family:{font};font-size:15px;line-height:1.6;color:#48484A;padding-bottom:28px">{e(intro)}</td></tr>
        <tr><td style="padding-bottom:28px">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="border-radius:999px;background:#0071E3">
            <a href="{e(link)}" style="display:inline-block;padding:14px 32px;font-family:{font};font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;border-radius:999px">{e(button)}</a>
          </td></tr></table></td></tr>
        <tr><td style="font-family:{font};font-size:13px;line-height:1.6;color:#86868B;padding-bottom:22px">{e(outro)}</td></tr>
        <tr><td style="border-top:1px solid #F2F2F7;padding-top:18px;font-family:{font};font-size:12px;line-height:1.6;color:#AEAEB2">
          Button not working? Copy this link into your browser:<br><a href="{e(link)}" style="color:#0071E3;word-break:break-all">{e(link)}</a></td></tr>
      </table></td></tr>
    <tr><td align="center" style="padding:24px 10px 0;font-family:{font};font-size:12px;line-height:1.7;color:#86868B">
      You're receiving this regarding your JaxCliped account.<br>
      <a href="{e(site)}" style="color:#0071E3;text-decoration:none">JaxCliped AI Studio</a> &nbsp;·&nbsp;
      <a href="{e(site)}/privacy" style="color:#86868B;text-decoration:none">Privacy</a> &nbsp;·&nbsp;
      <a href="{e(site)}/terms" style="color:#86868B;text-decoration:none">Terms</a></td></tr>
  </table></td></tr></table></body></html>"""


def preview(title="Confirm your email", intro="Thanks for joining JaxCliped. Confirm your email address below to start clipping viral moments.",
            button="Confirm Email Address", link=None, outro="This verification link works for 3 days. If you didn't create an account, you can safely ignore this email.") -> str:
    """The same email for viewing in a browser."""
    return _html(title, intro, button, link or config.PUBLIC_URL + "/auth/verify?t=example", outro)


def send(to: str, subject: str, title: str, intro: str, button: str, link: str, outro: str) -> bool:
    """Send in the background. Returns False when no mail service is set up (the link goes to the log)."""
    if [a for _n, a in getaddresses([to])] != [to]:   # one address only: never mail someone the address list sneaks in
        log.warning("Not sending %r: %r is not a single email address", subject, to)
        return False
    if not ready():
        if config.HTTPS:      # a real site: a sign-in link must never sit in a log file
            log.warning("Email not set up, so nothing was sent: %s", subject)
        else:                 # on your own computer the link is printed so you can use it
            print(f"[JaxCliped mail] {subject} for {to}: {link}", flush=True)
        return False
    msg = EmailMessage()
    name, addr = parseaddr(config.MAIL_FROM)
    msg["From"] = formataddr((name or "PeakCut", addr))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=addr.split("@")[-1] if "@" in addr else None)
    msg.set_content(f"{title}\n\n{intro}\n\n{button}: {link}\n\n{outro}\n")
    msg.add_alternative(_html(title, intro, button, link, outro), subtype="html")
    if LOGO.is_file():   # the logo travels inside the email, so it shows without loading anything from the web
        msg.get_payload()[1].add_related(LOGO.read_bytes(), maintype="image", subtype="png", cid=f"<{LOGO_CID}>",
                                         filename="peakcut.png", disposition="inline")

    def work():
        try:
            if config.SMTP_PORT == 465:
                with smtplib.SMTP_SSL(config.SMTP_HOST, 465, context=ssl.create_default_context(), timeout=20) as s:
                    if config.SMTP_USER:
                        s.login(config.SMTP_USER, config.smtp_password())
                    s.send_message(msg)
            else:
                with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
                    s.starttls(context=ssl.create_default_context())
                    if config.SMTP_USER:
                        s.login(config.SMTP_USER, config.smtp_password())
                    s.send_message(msg)
        except Exception as e:  # noqa: BLE001
            log.error("Could not send %r to %s: %s", subject, to, e)
            print(f"[PeakCut mail] could not send {subject!r} to {to}: {e}", flush=True)

    threading.Thread(target=work, daemon=True).start()
    return True
