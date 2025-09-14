from django.urls import path
from .views import ping
from .views import roll_dice
from .views import create_player


urlpatterns = [
    path("ping", ping),
    path("game/roll", roll_dice),
path("players", create_player),
]
