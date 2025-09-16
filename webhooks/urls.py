from django.urls import path
from .views import sendpulse_webhook, telegram_dice_webhook



urlpatterns = [
    path("telegram/webhook/diceResult", sendpulse_webhook),
]
