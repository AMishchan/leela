from django.urls import path
from .views import card_image

urlpatterns = [
    path("card/<str:signed>/<path:rel_path>", card_image, name="card_image"),
]
