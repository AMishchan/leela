from django.contrib import admin
from .models import Player

@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ('email', 'telegram_id', 'telegram_username',
                    'main_status', 'payment_status', 'player_type',
                    'game_type', 'game_name', 'registered_at')
    list_filter = ('main_status', 'payment_status', 'player_type', 'game_type')
    search_fields = ('email', 'telegram_username', 'telegram_id', 'game_name')
    ordering = ('-registered_at',)
    readonly_fields = ('registered_at',)
