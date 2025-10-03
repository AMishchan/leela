from django.urls import path
from .views import  telegram_dice_webhook, telegram_answer_webhook


urlpatterns = [
    path("telegram/diceResult", telegram_dice_webhook),
    path("webhooks/telegram/answer", telegram_answer_webhook),
]
