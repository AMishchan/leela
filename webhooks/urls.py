from django.urls import path
from .views import  telegram_dice_webhook


urlpatterns = [
    path("telegram/webhook/diceResult", telegram_dice_webhook),
]
