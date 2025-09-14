from rest_framework import serializers
from players.models import Player

class PlayerCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Player
        fields = [
            "email",
            "telegram_id",
            "telegram_username",
            "bot_token",
            "main_status",
            "payment_status",
            "player_type",
            "game_type",
            "game_name",
        ]

    def validate(self, attrs):
        # нормализация
        email = (attrs.get("email") or "").strip().lower()
        if not email:
            raise serializers.ValidationError({"email": "Обязательно"})
        attrs["email"] = email

        # username без @ и в нижний регистр (если задан)
        tuser = attrs.get("telegram_username")
        if tuser:
            tuser = tuser.lstrip("@").strip().lower()
            attrs["telegram_username"] = tuser

        # проверки дублей (case-insensitive)
        if Player.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError({"email": "Пользователь с такой почтой уже существует"})

        tg_id = attrs.get("telegram_id")
        if tg_id is None:
            raise serializers.ValidationError({"telegram_id": "Обязательно"})
        if Player.objects.filter(telegram_id=tg_id).exists():
            raise serializers.ValidationError({"telegram_id": "Пользователь с таким Telegram ID уже существует"})

        if tuser:
            if Player.objects.filter(telegram_username__iexact=tuser).exists():
                raise serializers.ValidationError({"telegram_username": "Ник уже занят"})

        return attrs
