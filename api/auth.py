from django.utils.deprecation import MiddlewareMixin
from rest_framework.authentication import BaseAuthentication
from rest_framework import exceptions
from .models import ApiKey

class ApiKeyAuthentication(BaseAuthentication):
    keyword = "Bearer"  # чтобы работало с Authorization: Bearer <ключ>

    def authenticate(self, request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith(f"{self.keyword} "):
            return None  # DRF попробует другие схемы, если есть

        token = auth.split(" ", 1)[1].strip()
        try:
            key = ApiKey.objects.get(key=token, is_active=True)
        except ApiKey.DoesNotExist:
            raise exceptions.AuthenticationFailed("Invalid API key")

        # опциональная проверка IP
        if key.allowed_ips:
            ip = request.META.get("REMOTE_ADDR", "")
            allowed = [x.strip() for x in key.allowed_ips.split(",") if x.strip()]
            if ip not in allowed:
                raise exceptions.AuthenticationFailed("IP not allowed")

        # В DRF нужно вернуть (user, auth). Пользователя у нас нет — используем Anonymous.
        from django.contrib.auth.models import AnonymousUser
        return (AnonymousUser(), key)
