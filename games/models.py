from django.db import models

# Create your models here.
import uuid
from datetime import timedelta
from django.db import models, transaction
from django.utils import timezone
from django.db.models import Q
from players.models import Player

class Game(models.Model):
    class Status(models.TextChoices):
        ACTIVE = 'active', 'Активна'
        PAUSED = 'paused', 'Пауза'
        FINISHED = 'finished', 'Завершена'
        INACTIVE = 'inactive', 'Неактивна'
        ABORTED = 'aborted', 'Прервана'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='games', db_index=True)

    game_type = models.CharField('Тип игры', max_length=100, blank=True)
    game_name = models.CharField('Название игры', max_length=150, blank=True)

    status = models.CharField('Статус', max_length=16, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    is_active = models.BooleanField('Актуальная', default=True, db_index=True)

    current_cell = models.IntegerField('Текущая клетка', default=0)
    current_six_number = models.IntegerField('Количество выпавших шестерок на данный момент', default=0)
    last_move_number = models.IntegerField('№ последнего хода', default=0)

    meta = models.JSONField('Метаданные', default=dict, blank=True)

    started_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)  # 👈 срок действия

    class Meta:
        ordering = ('-updated_at',)
        indexes = [
            models.Index(fields=['player', 'is_active', '-updated_at']),
            models.Index(fields=['player', 'status', '-updated_at']),
            models.Index(fields=['expires_at']),
        ]
        # Только одна активная игра на игрока
        constraints = [
            models.UniqueConstraint(
                fields=['player'],
                condition=Q(is_active=True),
                name='uniq_active_game_per_player'
            )
        ]

    def __str__(self):
        base = self.game_name or self.game_type or 'Game'
        return f'{base} | {self.player.email} | {self.status} | #{self.last_move_number}'

    # ---- Вспомогательная логика срока действия ----
    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and timezone.now() >= self.expires_at)

    def expire_if_needed(self) -> bool:
        """Если истек срок — переводим в INACTIVE и снимаем актуальность. Возвращает True, если истекла."""
        if self.is_active and self.is_expired:
            self.status = self.Status.INACTIVE
            self.is_active = False
            self.finished_at = self.finished_at or timezone.now()
            self.save(update_fields=['status', 'is_active', 'finished_at', 'updated_at'])
            return True
        return False

    # ---- Фабрики/операции ----
    @classmethod
    def resume_last(cls, player, game_type: str = None, game_name: str = None):
        qs = cls.objects.filter(player=player, is_active=True, status__in=[cls.Status.ACTIVE, cls.Status.PAUSED])
        if game_type:
            qs = qs.filter(game_type=game_type)
        if game_name:
            qs = qs.filter(game_name=game_name)
        game = qs.order_by('-updated_at').first()
        if game:
            game.expire_if_needed()
            if not game.is_active:
                return None
        return game

    @classmethod
    def start_new(cls, player, game_type: str = '', game_name: str = '', meta: dict = None, ttl_days: int = 30):
        meta = meta or {}
        # деактивируем все прочие актуальные
        cls.objects.filter(player=player, is_active=True).update(is_active=False, status=cls.Status.INACTIVE)
        return cls.objects.create(
            player=player,
            game_type=game_type,
            game_name=game_name,
            meta=meta,
            status=cls.Status.ACTIVE,
            is_active=True,
            current_cell=0,
            last_move_number=0,
            expires_at=timezone.now() + timedelta(days=ttl_days),
        )

    @transaction.atomic
    def add_move(self, rolled: int, from_cell: int, to_cell: int,
                 event_type: str = 'normal', note: str = '', state_after: dict = None):
        # проверяем срок перед записью
        if self.expire_if_needed():
            raise ValueError("Игра неактивна: срок действия истёк.")
        state_after = state_after or {}
        next_num = self.last_move_number + 1
        Move.objects.create(
            game=self,
            move_number=next_num,
            rolled=rolled,
            from_cell=from_cell,
            to_cell=to_cell,
            event_type=event_type,
            note=note,
            state_snapshot=state_after,
        )
        self.last_move_number = next_num
        self.current_cell = to_cell
        self.save(update_fields=['last_move_number', 'current_cell', 'updated_at'])

    def pause(self):
        self.status = self.Status.PAUSED
        self.is_active = True
        self.save(update_fields=['status', 'is_active', 'updated_at'])

    def finish(self):
        self.status = self.Status.FINISHED
        self.is_active = False
        self.finished_at = timezone.now()
        self.save(update_fields=['status', 'is_active', 'finished_at', 'updated_at'])


class Move(models.Model):
    class EventType(models.TextChoices):
        NORMAL = 'normal', 'Обычный ход'
        SNAKE = 'snake', 'Змея'
        LADDER = 'ladder', 'Стрела/лестница'
        BONUS = 'bonus', 'Бонус'
        PENALTY = 'penalty', 'Штраф'

    id = models.BigAutoField(primary_key=True)
    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name='moves', db_index=True)
    move_number = models.PositiveIntegerField('№ хода')
    rolled = models.PositiveSmallIntegerField('Бросок', default=0)
    from_cell = models.IntegerField('С клетки', default=0)
    to_cell = models.IntegerField('На клетку', default=0)
    event_type = models.CharField('Событие', max_length=16, choices=EventType.choices, default=EventType.NORMAL)
    note = models.TextField('Заметка', blank=True)
    state_snapshot = models.JSONField('Состояние после хода', default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Сырой вебхук (весь JSON как есть)
    webhook_payload = models.JSONField('Webhook payload', default=dict, blank=True)

    # Telegram meta
    tg_from_id = models.BigIntegerField('Telegram From ID', null=True, blank=True, db_index=True)
    tg_message_date = models.DateTimeField('Дата сообщения (UTC)', null=True, blank=True, db_index=True)

    class Meta:
        ordering = ('move_number',)
        unique_together = (('game', 'move_number'),)
        indexes = [
            models.Index(fields=['game', 'move_number']),
            models.Index(fields=['-created_at']),
            models.Index(fields=['tg_from_id']),  # 👈 быстро искать по отправителю
            models.Index(fields=['tg_message_date']),  # 👈 быстро фильтровать по дате
        ]

    def __str__(self):
        return f'#{self.move_number} {self.event_type} {self.from_cell}->{self.to_cell}'
