from django.contrib import admin

# Register your models here.
from django.contrib import admin
from .models import Game, Move
from django.utils.html import format_html
from django.utils.text import Truncator
from django import forms
from django.urls import reverse


class MoveInlineForm(forms.ModelForm):
    class Meta:
        model = Move
        fields = "__all__"
        widgets = {
            "player_answer": forms.Textarea(attrs={"rows": 2, "cols": 60}),
            "note": forms.TextInput(attrs={"size": 18, "style": "width:16ch;"}),  # компактная «Заметка»
        }

class MoveInline(admin.TabularInline):
    model = Move
    form = MoveInlineForm
    extra = 0
    ordering = ("move_number",)

    # какие поля показывать в таблице ходов игры
    fields = (
        "move_link",        # ссылка на форму редактирования хода
        "move_number", "rolled", "from_cell", "to_cell", "event_type",
        "player_answer",    # показываем реальный текст ответа (редактируемый)
        "on_hold",
    )
    readonly_fields = ("move_link",)
    @admin.display(description="Ход")
    def move_link(self, obj: Move):
        url = reverse("admin:games_move_change", args=[obj.id])
        return format_html('<a href="{}">#{} открыть</a>', url, obj.move_number)

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
    list_display = ("link", "game", "move_number", "rolled", "from_cell", "to_cell", "event_type", "answered")
    list_select_related = ("game",)
    search_fields = ("game__id", "player_answer", "note")
    list_filter = (("player_answer", admin.EmptyFieldListFilter), "event_type")

    @admin.display(description="Открыть")
    def link(self, obj: Move):
        url = reverse("admin:games_move_change", args=[obj.id])
        return format_html('<a href="{}">#{}</a>', url, obj.move_number)

    @admin.display(boolean=True, description="Есть ответ?")
    def answered(self, obj: Move):
        return bool(obj.player_answer)
