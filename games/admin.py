from django.contrib import admin

# Register your models here.
from django.contrib import admin
from .models import Game, Move
from django.utils.html import format_html
from django.utils.text import Truncator

class MoveInline(admin.TabularInline):
    model = Move
    extra = 0
    ordering = ("move_number",)

    # какие поля показывать в таблице ходов игры
    fields = (
        "move_number",
        "rolled",
        "from_cell",
        "to_cell",
        "event_type",
        "player_answer_short",   # <-- НОВОЕ: колонка «Ответ игрока»
        "on_hold",
    )
    # обычно ходы внутри игры редактируют не здесь; делаем их read-only
    readonly_fields = (
        "move_number",
        "rolled",
        "from_cell",
        "to_cell",
        "event_type",
        "player_answer_short",
        "on_hold",
    )

    @admin.display(description="Ответ игрока")
    def player_answer_short(self, obj: Move):
        if not obj.player_answer:
            return format_html('<span style="color:#999;">—</span>')
        # обрежем до 120 символов, сохранив читаемость
        return Truncator(obj.player_answer).chars(120)

@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    list_display = ('id', 'player', 'game_type', 'game_name', 'status', 'is_active',
                    'last_move_number', 'current_cell', 'started_at', 'updated_at')
    list_filter = ('status', 'is_active', 'game_type')
    search_fields = ('id', 'player__email', 'player__telegram_username', 'game_name')
    readonly_fields = ('started_at', 'updated_at', 'finished_at', 'last_move_number')
    inlines = [MoveInline]

@admin.register(Move)
class MoveAdmin(admin.ModelAdmin):
    list_display = ('game', 'move_number', 'rolled', 'from_cell', 'to_cell', 'event_type', 'created_at')
    list_filter = ('event_type',)
    search_fields = ('game__id',)
    ordering = ('game', 'move_number')
