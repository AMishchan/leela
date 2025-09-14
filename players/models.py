from django.db import models
from django.db.models import Q

class Player(models.Model):
    email = models.EmailField('Почта', unique=True)                # ← уникально
    telegram_id = models.BigIntegerField('Telegram ID', unique=True)  # ← уникально
    telegram_username = models.CharField('Никнейм в Telegram', max_length=150, blank=True, db_index=True)

    registered_at = models.DateTimeField('Дата регистрации', auto_now_add=True)
    bot_token = models.CharField('Токен бота', max_length=255, blank=True)

    class MainStatus(models.TextChoices):
        ACTIVE = 'active', 'Активен'
        INACTIVE = 'inactive', 'Неактивен'
        BANNED = 'banned', 'Заблокирован'

    class PaymentStatus(models.TextChoices):
        NONE = 'none', 'Нет оплаты'
        PENDING = 'pending', 'Ожидает'
        PAID = 'paid', 'Оплачено'
        REFUNDED = 'refunded', 'Возврат'

    class PlayerType(models.TextChoices):
        FREE = 'free', 'Бесплатный'
        TRIAL = 'trial', 'Триал'
        PREMIUM = 'premium', 'Премиум'
        ADMIN = 'admin', 'Админ'

    main_status = models.CharField('Главный статус', max_length=16, choices=MainStatus.choices, default=MainStatus.ACTIVE)
    payment_status = models.CharField('Статус оплаты', max_length=16, choices=PaymentStatus.choices, default=PaymentStatus.NONE)
    player_type = models.CharField('Тип игрока', max_length=16, choices=PlayerType.choices, default=PlayerType.FREE)

    game_type = models.CharField('Тип игры', max_length=100, blank=True)
    game_name = models.CharField('Название игры', max_length=150, blank=True)

    def __str__(self):
        return f'{self.email} ({self.telegram_username or self.telegram_id})'

    class Meta:
        constraints = [
            # Никнейм должен быть уникален, если он задан (не пустая строка)
            models.UniqueConstraint(
                fields=['telegram_username'],
                condition=~Q(telegram_username=''),
                name='uniq_player_telegram_username_not_blank'
            ),
        ]
