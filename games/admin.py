from django.contrib import admin
from django.utils.html import format_html
from django.utils.text import Truncator
from django import forms
from django.urls import reverse

from .models import Game, Move

from django.contrib import admin

admin.site.site_header = "Лила — админка"
admin.site.site_title  = "Лила | Админ"
admin.site.index_title = "Управление игрой"
admin.site.site_url    = "/"  # куда ведёт «View site»

# ---------- индикатор-точка (зелёная/красная) ----------
def _dot(ok: bool) -> str:
    color = "#22c55e" if ok else "#ef4444"   # green / red
    return format_html(
        '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{};"></span>',
        color
    )


# ---------- inline ходов в карточке игры ----------
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

    # порядок колонок: ссылка, №, бросок, индикатор ответа, с/на, событие, индикатор on_hold, ответ
    fields = (
        "move_link",
        "move_number",
        "rolled",
        "answered_dot",     # ← точка вместо чекбокса «Ответ?»
        "from_cell",
        "to_cell",
        "event_type",
        "on_hold_dot",      # ← точка вместо чекбокса «Остаться после 6»
        "player_answer",    # текст ответа (редактируемый по желанию)
    )
    readonly_fields = ("move_link", "answered_dot", "on_hold_dot")

    @admin.display(description="Ход")
    def move_link(self, obj: Move):
        url = reverse("admin:games_move_change", args=[obj.id])
        return format_html('<a href="{}">#{} открыть</a>', url, obj.move_number)

    @admin.display(description="Ответ?")
    def answered_dot(self, obj: Move):
        return _dot(bool(obj.player_answer))

    @admin.display(description="Остаться после 6")
    def on_hold_dot(self, obj: Move):
        return _dot(bool(getattr(obj, "on_hold", False)))


# ---------- карточка игры ----------
@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    list_display = ('id', 'player', 'game_type', 'game_name', 'status', 'is_active',
                    'last_move_number', 'current_cell', 'started_at', 'updated_at')
    list_filter = ('status', 'is_active', 'game_type')
    search_fields = ('id', 'player__email', 'player__telegram_username', 'game_name')
    readonly_fields = ('started_at', 'updated_at', 'finished_at', 'last_move_number')
    inlines = [MoveInline]


# ---------- список/форма хода ----------
@admin.register(Move)
class MoveAdmin(admin.ModelAdmin):
    # в списке показываем точки вместо чекбоксов
    list_display = (
        "link", "game", "move_number", "rolled",
        "answered_dot",          # индикатор ответа
        "from_cell", "to_cell", "event_type",
        "on_hold_dot",           # индикатор on_hold
        "answer_prompt_msg_id", "player_answer_at"
    )
    list_select_related = ("game",)
    search_fields = ("game__id", "player_answer", "note", "answer_prompt_msg_id", "tg_from_id")
    list_filter = (("player_answer", admin.EmptyFieldListFilter), "event_type")

    # форма изменения хода: убираем реальный on_hold, показываем только индикатор
    fields = (
        ("game", "move_number", "event_type"),
        ("from_cell", "to_cell", "rolled"),
        "note",
        "image_url",
        ("player_answer", "player_answer_at", "answer_prompt_msg_id"),  # 👈 здесь
        "state_snapshot",
        ("tg_from_id", "tg_message_date"),
        ("qa_status", "qa_sequence_in_combo", "qa_combo_id"),
        "webhook_payload",

    )
    readonly_fields = ("answered_dot", "on_hold_dot")
    exclude = ("on_hold",)  # <-- реальное чекбокс-поле скрываем, чтобы нельзя было менять руками

    @admin.display(description="Открыть")
    def link(self, obj: Move):
        url = reverse("admin:games_move_change", args=[obj.id])
        return format_html('<a href="{}">#{}</a>', url, obj.move_number)

    @admin.display(description="Ответ?")
    def answered_dot(self, obj: Move):
        return _dot(bool(obj.player_answer))

    @admin.display(description="Остаться после 6")
    def on_hold_dot(self, obj: Move):
        return _dot(bool(getattr(obj, "on_hold", False)))
