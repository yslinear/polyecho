"""polyecho.push — Web-push-sender service.

Polls the events table and delivers VAPID Web Push notifications (PRD §5.8).
This process holds no private keys and opens no network ports.
"""

from polyecho.push.sender import WebPushSender

__all__ = ["WebPushSender"]
